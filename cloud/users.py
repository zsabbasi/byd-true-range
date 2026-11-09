"""
Shared-server mode: who the users are, their web sessions, and where each user's data lives.

Every user has their OWN folder (<data>/users/<id>/) holding their history database, their encrypted BYD sign-in and their
settings, and their OWN poller process. Nothing is shared between users except this small registry, which holds only an opaque
user id and hashed session tokens: never a name, an email, a password or any car data.

The id is a keyed hash of region + BYD account name (key = a random per-server salt), so the folder name does not reveal the
account and is the same each time the same person signs in again.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import db

SESSION_DAYS = 90
SLIDE_BELOW_DAYS = 60                   # a session with less time left than this is extended when used
ID_RE = re.compile(r"^[0-9a-f]{20}$")
_lock = threading.Lock()


def base_dir() -> Path:
    return Path(db.DB_PATH).parent       # NOT current_dir(): the registry is the server's own, not a user's


def _conn() -> sqlite3.Connection:
    base_dir().mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(base_dir() / "users.sqlite", timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, created REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS sessions (h TEXT PRIMARY KEY, uid TEXT NOT NULL, expires REAL NOT NULL)")
    return c


@contextlib.contextmanager
def _db():
    """One short connection per call, committed and CLOSED (sqlite's own context manager does not close: that locks files on Windows)."""
    c = _conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _salt() -> bytes:
    f = base_dir() / "users_salt"
    try:
        return f.read_bytes()
    except OSError:
        pass
    base_dir().mkdir(parents=True, exist_ok=True)
    s = secrets.token_bytes(32)
    try:
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(s)
    except FileExistsError:
        return f.read_bytes()
    return s


def user_id(username: str, region: str) -> str:
    return hmac.new(_salt(), f"{region}|{username.strip().lower()}".encode(), hashlib.sha256).hexdigest()[:20]


def user_dir(uid: str) -> Path:
    if not ID_RE.match(uid or ""):
        raise ValueError("bad user id")
    return base_dir() / "users" / uid


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# -------------------------------------------------------------------------------------------------- users
def state(uid: str) -> str:
    """"none", "active" or "removing" (deleted by its owner, waiting for the supervisor to wipe the folder)."""
    with _lock, _db() as c:
        r = c.execute("SELECT deleted FROM users WHERE id=?", (uid,)).fetchone()
    return "none" if r is None else ("removing" if r[0] else "active")


def count() -> int:
    with _lock, _db() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE deleted=0").fetchone()[0]


def add(uid: str) -> None:
    with _lock, _db() as c:
        c.execute("INSERT OR IGNORE INTO users (id, created, deleted) VALUES (?, ?, 0)", (uid, time.time()))
    user_dir(uid).mkdir(parents=True, exist_ok=True)


def active_users() -> list[str]:
    with _lock, _db() as c:
        return [r[0] for r in c.execute("SELECT id FROM users WHERE deleted=0 ORDER BY created")]


def pending_wipes() -> list[str]:
    with _lock, _db() as c:
        return [r[0] for r in c.execute("SELECT id FROM users WHERE deleted=1")]


def request_delete(uid: str) -> None:
    """The user asked to be forgotten: sessions go at once, the supervisor stops their poller and wipes the folder."""
    with _lock, _db() as c:
        c.execute("DELETE FROM sessions WHERE uid=?", (uid,))
        c.execute("UPDATE users SET deleted=1 WHERE id=?", (uid,))


def purge(uid: str) -> None:
    """Wipe the folder and forget the user completely (the supervisor calls this once their poller has stopped)."""
    shutil.rmtree(user_dir(uid), ignore_errors=True)
    with _lock, _db() as c:
        c.execute("DELETE FROM sessions WHERE uid=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))


# ------------------------------------------------------------------------------------------------ sessions
def create_session(uid: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock, _db() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        c.execute("INSERT INTO sessions (h, uid, expires) VALUES (?, ?, ?)", (_h(token), uid, now + SESSION_DAYS * 86400))
    return token


def lookup_session(token: str | None) -> str | None:
    """The user id for a valid session token, else None. Only the hash of a token is ever stored."""
    if not token or len(token) > 200:
        return None
    now = time.time()
    with _lock, _db() as c:
        r = c.execute("SELECT s.uid, s.expires FROM sessions s JOIN users u ON u.id = s.uid "
                      "WHERE s.h=? AND u.deleted=0", (_h(token),)).fetchone()
        if r is None or r[1] < now:
            return None
        if r[1] - now < SLIDE_BELOW_DAYS * 86400:
            c.execute("UPDATE sessions SET expires=? WHERE h=?", (now + SESSION_DAYS * 86400, _h(token)))
        return r[0]


def end_session(token: str | None) -> None:
    if token:
        with _lock, _db() as c:
            c.execute("DELETE FROM sessions WHERE h=?", (_h(token),))
