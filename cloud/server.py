"""
Local web server for the BYD true-range app. Reads the poller's SQLite history (read-only) and serves a
phone-friendly dashboard plus a small JSON API. Standard library only.

    python server.py                       # http://127.0.0.1:8765  (this PC only, no token needed)
    python server.py --host 0.0.0.0        # reachable from other devices; REQUIRES an access token
    python server.py --require-token       # localhost + token: use this behind ngrok / Cloudflare Tunnel

Privacy / security choices (the API carries car data, so these matter):
- Binds to 127.0.0.1 by default. Exposing it (--host other) generates an access token, stored in
  data/access_token.txt, and every request must present it (URL once, then an HttpOnly cookie).
- On loopback, the Host header is checked to defeat DNS-rebinding from web pages in your browser.
- The API NEVER returns GPS coordinates, VIN, or account details. Position history stays in the database.
- Strict Content-Security-Policy, no external scripts/fonts/CDNs, no cookies except the access token.
- The database is opened read-only. This server cannot change history and sends nothing to BYD.
- Plain HTTP: on an untrusted network use a private tunnel (Tailscale/WireGuard) instead of exposing it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import threading
import re
import secrets
import socket
import sqlite3
import sys
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape as html_escape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import db
import byd_config
import capacity
import charging
import credstore
import range_engine
import pollstatus
import settings
import trips
import users

WEB_DIR = Path(__file__).with_name("web")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/trips.js": ("trips.js", "application/javascript; charset=utf-8"),
    "/battery.js": ("battery.js", "application/javascript; charset=utf-8"),
    "/charging.js": ("charging.js", "application/javascript; charset=utf-8"),
    "/ui.js": ("ui.js", "application/javascript; charset=utf-8"),
    "/account.js": ("account.js", "application/javascript; charset=utf-8"),
    "/pwa.js": ("pwa.js", "application/javascript; charset=utf-8"),
    "/sw.js": ("sw.js", "application/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
    "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
    "/icons/social-card.png": ("icons/social-card.png", "image/png"),
}
# Fetched by the browser itself WITHOUT the login cookie (manifest, icons, service worker script), or needed by the sign-in page.
# They hold no data and no secret: the same files are in the open-source repository.
PUBLIC = {"/manifest.webmanifest", "/sw.js", "/app.css", "/icons/icon-192.png", "/icons/icon-512.png",
          "/icons/icon-maskable-512.png", "/icons/apple-touch-icon.png", "/icons/social-card.png"}
COOKIE_MAX_AGE = 365 * 24 * 3600     # the installed app keeps its login
SESSION_COOKIE = "byd_session"       # shared-server mode: a random token whose hash is in users.sqlite
SESSION_MAX_AGE = users.SESSION_DAYS * 86400
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
COOKIE_NAME = "byd_token"
DEFAULT_USABLE_FRACTION = 1.0     # no invented usable share: start from the battery size the owner gave; capacity.py measures it

TILE_HOST = "https://tile.openstreetmap.org"


def csp(map_tiles: bool, form_self: bool = False) -> str:
    """Strict policy. OpenStreetMap tile images are the ONLY external resource, and only when map tiles are on."""
    img = "img-src 'self' data:" + (f" {TILE_HOST}" if map_tiles else "")
    return ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            f"{img}; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
            f"form-action {chr(39)}{'self' if form_self else 'none'}{chr(39)}; frame-ancestors 'none'")


# Link previews (WhatsApp, Telegram, Slack ...): Open Graph tags on the home page, and a plain 200 page for the preview bots when the
# real page is private. Nothing here is private: it is the same generic text for everybody.
OG_TITLE = "BYD true range"
OG_DESC = ("An honest range band, trips, charging log and battery check for your BYD Atto 3, from BYD's cloud. "
           "Unofficial and read-only.")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:\d{1,5})?$")
PREVIEW_UA = re.compile(r"whatsapp|facebookexternalhit|facebot|twitterbot|telegrambot|slackbot|discordbot|linkedinbot|skypeuripreview",
                        re.I)


def og_meta(origin: str) -> str:
    """Open Graph / Twitter card tags. The image must be an absolute URL, so they need the address the page was opened on."""
    e = html_escape
    tags = ['<meta property="og:type" content="website">', f'<meta property="og:site_name" content="{e(OG_TITLE)}">',
            f'<meta property="og:title" content="{e(OG_TITLE)}">', f'<meta property="og:description" content="{e(OG_DESC)}">',
            f'<meta name="twitter:card" content="{"summary_large_image" if origin else "summary"}">',
            f'<meta name="twitter:title" content="{e(OG_TITLE)}">', f'<meta name="twitter:description" content="{e(OG_DESC)}">']
    if origin:
        img = e(origin + "/icons/social-card.png")
        tags += [f'<meta property="og:url" content="{e(origin + "/")}">', f'<meta property="og:image" content="{img}">',
                 '<meta property="og:image:type" content="image/png">', '<meta property="og:image:width" content="1200">',
                 '<meta property="og:image:height" content="630">', f'<meta property="og:image:alt" content="{e(OG_TITLE)} icon">',
                 f'<meta name="twitter:image" content="{img}">']
    return "\n".join(tags)


PREVIEW_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>BYD true range</title>
<meta name="robots" content="noindex"><!--OG--></head><body><p>BYD true range. Open this link in your browser.</p></body></html>"""


TOKEN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><!--OG-->
<meta name="robots" content="noindex"><meta name="theme-color" content="#0b0f14"><title>BYD true range</title><link rel="stylesheet" href="/app.css"></head>
<body><main><h1 class="tripname">BYD true range</h1>
<form method="get" action="/" class="signin"><div class="note">This app is private. Paste your access token to open it. It is the line in access_token.txt in the app's data folder.</div>
<label class="field"><span class="flabel">Access token</span><input type="password" name="token" autocomplete="off" autocapitalize="off" spellcheck="false" required></label>
<button class="primary" type="submit">Open</button></form></main></body></html>"""


def shell_version() -> str:
    """Hash of the files the service worker caches, so a changed file gives a new cache name."""
    h = hashlib.sha256()
    for name in sorted({v[0] for k, v in STATIC.items() if k != "/sw.js"}):
        try:
            h.update((WEB_DIR / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]


_last_touch = float("-inf")
_last_touch_by_dir: dict[str, float] = {}


def touch_viewer() -> None:
    """Tell the poller someone has the app open (it then refreshes sooner). At most once per 5 s."""
    global _last_touch
    now = time.monotonic()
    if db.dir_override():                                    # shared server: throttle per user, never across users
        key = str(db.current_dir())
        if now - _last_touch_by_dir.get(key, float("-inf")) < 5:
            return
        if len(_last_touch_by_dir) > 1000:
            _last_touch_by_dir.clear()
        _last_touch_by_dir[key] = now
    else:
        if now - _last_touch < 5:
            return
        _last_touch = now
    try:
        f = db.current_dir() / "viewer_active"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
    except OSError:
        pass


# ----------------------------------------------------------------------------------------------- data ---
def _connect_ro() -> sqlite3.Connection | None:
    p = db.current_db_path()
    if not p.exists():
        return None
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_ts(s: str | None) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def build_status(conn: sqlite3.Connection | None, *, now: dt.datetime | None = None,
                 capacity_kwh: float = range_engine.DEFAULT_USABLE_KWH,
                 usable_fraction: float = DEFAULT_USABLE_FRACTION, range_mode: str = "policy") -> dict:
    """Everything the dashboard needs. Contains no coordinates, VIN or credentials."""
    now = now or dt.datetime.now(dt.timezone.utc)
    row = None
    if conn is not None:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM readings WHERE soc > 0 AND odometer_km > 0 ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"has_data": False, "server_time": now.isoformat()}

    # Usable capacity: the configured battery, refined by what your own driving shows (capacity.py). With no
    # qualifying drive yet this is exactly the configured value, with its wide prior uncertainty.
    cap = capacity.learn_from_db(conn, capacity_kwh, capacity_kwh * usable_fraction)
    usable = cap["usable_kwh"]
    soc = row["soc"]
    # Your own driving combined with BYD's measured consumption (which is right) on YOUR battery size (which BYD's
    # own range gets wrong: it assumes about 57 kWh). See docs/PROJECT_PLAN.md 6l.
    observed = db.observed_km(conn)
    best = db.best_r50(conn)
    est = range_engine.best_estimate(db.recent_readings(conn), soc, usable_kwh=usable, byd_kwh_per_100=row["r50_rate"],
                                     byd_range_km=row["byd_range_km"], odometer_km=row["odometer_km"],
                                     own_observed_km=observed, mode=range_mode, cap_uncertainty=cap["sd_rel"],
                                     best_kwh_per_100=best)

    ts = _parse_ts(row["ts"])
    age = max(0.0, (now - ts).total_seconds()) if ts else None
    # How old is the CAR data? (Our poll time says nothing: BYD can return a cached snapshot.)
    car_ts = _parse_ts(row["car_ts"])
    data_age = max(0.0, (now - car_ts).total_seconds()) if car_ts else age
    byd = row["byd_range_km"]
    vs_byd = None
    if est.true_range_km is not None and byd:
        vs_byd = (est.true_range_km / byd - 1) * 100

    def r(v, n=1):
        return None if v is None else round(v, n)

    return {
        "has_data": True,
        "server_time": now.isoformat(),
        "reading": {
            "ts": row["ts"], "car_ts": row["car_ts"] or None, "age_s": r(age, 0),
            "data_age_s": r(data_age, 0), "soc": soc, "odometer_km": row["odometer_km"],
            "speed_kmh": row["speed_kmh"], "is_driving": bool(row["is_driving"]),
            "gear": row["gear"], "charging_state": row["charging_state"],
            "byd_range_km": byd, "byd_consumption": row["r50_rate"], "byd_kwh_50km": row["r50_kwh"],
        },
        "range": {
            "status": est.status, "basis": est.basis,
            "km": r(est.true_range_km, 0), "low_km": r(est.low_km, 0), "high_km": r(est.high_km, 0),
            "trust_pct": est.trust_pct, "vs_byd_pct": r(vs_byd, 0),
            "consumption_kwh_per_100km": r(est.consumption_kwh_per_100km),
            "window_km": r(est.window_km, 0), "soc_drop_pct": r(est.soc_drop_pct, 0),
            "assumed_usable_kwh": r(usable, 1), "usable_learned": not cap["prior_only"],
            "own_km": r((est.parts or {}).get("own_km"), 0), "byd_based_km": r((est.parts or {}).get("byd_km"), 0),
            "own_weight_pct": r((est.parts["own_weight"]) * 100, 0) if est.parts and "own_weight" in est.parts else None,
            "policy": (est.parts or {}).get("policy"), "own_observed_km": r(observed, 1),
            "upside_capped": bool((est.parts or {}).get("best_kwh_per_100")),
            "clamped_from_km": r((est.parts or {}).get("clamped_from_km"), 0),
            "best_kwh_per_100": r((est.parts or {}).get("best_kwh_per_100"), 1),
            "own_needed_km": range_engine.OWN_MIN_KM,
            "disagree_pct": r((est.parts or {}).get("disagree_pct"), 0),
        },
    }


def build_series(conn: sqlite3.Connection | None, hours: float = 24.0, max_points: int = 240,
                 now: dt.datetime | None = None) -> list[dict]:
    if conn is None:
        return []
    conn.row_factory = sqlite3.Row
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = (now - dt.timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT ts, soc, byd_range_km FROM readings WHERE ts >= ? AND soc > 0 ORDER BY id", (cutoff,)
    ).fetchall()
    step = max(1, -(-len(rows) // max_points))
    return [{"ts": r["ts"], "soc": r["soc"], "byd_range_km": r["byd_range_km"]} for r in rows[::step]]


# ---------------------------------------------------------------------------------------------- server ---
def _host_only(host_header: str) -> str:
    h = (host_header or "").strip().lower()
    if h.startswith("["):                      # [::1]:8765
        return h[1:].split("]")[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


# ------------------------------------------------------------------------------------------------ sign-in ------
# The BYD password is typed ONLY on the computer that runs the app (or through an SSH tunnel to its localhost). It goes from
# the browser to this process and from here to BYD over HTTPS. It is never accepted through a tunnel, proxy or LAN.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "forwarded", "via", "x-real-ip",
                 "cf-connecting-ip", "cf-ray", "true-client-ip", "x-original-host", "x-ngrok-trace-id")
MAX_BODY = 4096
LOGIN_WINDOW_S, LOGIN_MAX = 600.0, 5          # failed logins can lock a BYD account: never more than 5 tries per 10 minutes
TZ_RE = re.compile(r"^(UTC|[A-Za-z][A-Za-z_]+(/[A-Za-z0-9_+\-]+){1,2})$")
_login_times: list[float] = []
_login_lock = threading.Lock()


def is_local_client(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)       # ::ffff:127.0.0.1 is IPv4 loopback in IPv6 clothing
        return (mapped or ip).is_loopback
    except ValueError:
        return False


def mask_username(u: str) -> str:
    """Enough to recognise your own account, not enough to identify it to someone who only has the page."""
    if "@" in u:
        local, _, domain = u.partition("@")
        head, _, tld = domain.rpartition(".")
        return f"{local[:1]}***@{head[:1]}***.{tld}" if head else f"{local[:1]}***@***"
    return f"{u[:2]}***{u[-2:]}" if len(u) >= 6 else "***"


def login_rate_limited() -> bool:
    now = time.monotonic()
    with _login_lock:
        _login_times[:] = [t for t in _login_times if now - t < LOGIN_WINDOW_S]
        if len(_login_times) >= LOGIN_MAX:
            return True
        _login_times.append(now)
        return False


def safe_eq(a: str, b: str) -> bool:
    """Constant-time comparison that cannot raise on non-ASCII text (hmac.compare_digest does for str)."""
    return hmac.compare_digest(str(a).encode("utf-8", "replace"), str(b).encode("utf-8", "replace"))


_mu_attempts: dict[str, list[float]] = {}
MU_LIMITS = (("u", 5), ("i", 20), ("all", 120))     # per BYD account, per client address, whole server; each per 10 minutes


def _mu_key(kind: str, uid: str, ip: str) -> str:
    return {"u": f"u:{uid}", "i": f"i:{ip}", "all": "all"}[kind]


def multi_limited(uid: str, ip: str, kinds=("u", "i", "all")) -> bool:
    """Is any of these budgets used up? (A check only: nothing is counted here.)"""
    now = time.monotonic()
    limits = dict(MU_LIMITS)
    with _login_lock:
        for kind in kinds:
            lst = _mu_attempts.get(_mu_key(kind, uid, ip))
            if lst is None:
                continue
            lst[:] = [t for t in lst if now - t < LOGIN_WINDOW_S]
            if len(lst) >= limits[kind]:
                return True
    return False


def multi_note(uid: str, ip: str, kinds) -> None:
    """Count one event. An address is charged for every attempt it makes; a BYD account and the server only for a login BYD REJECTED,
    so a stranger cannot use up somebody's budget with logins that never reached BYD or that succeeded."""
    now = time.monotonic()
    with _login_lock:
        while len(_mu_attempts) > 20000:                       # forget the oldest entries, never everybody's counters at once
            _mu_attempts.pop(next(iter(_mu_attempts)))
        for kind in kinds:
            _mu_attempts.setdefault(_mu_key(kind, uid, ip), []).append(now)


def _byd_login(creds: dict, timeout: float = 25.0) -> list[dict]:
    """One login attempt against BYD. Returns [{"model", "vin_tail"}]. Raises pyBYD's exceptions."""
    import asyncio
    from pybyd import BydClient

    config = byd_config.config_from(creds)

    async def go():
        async with BydClient(config) as client:
            await client.login()
            vs = await client.get_vehicles()
            return [{"model": getattr(v, "model", None) or getattr(v, "out_model_type", None),
                     "vin_tail": (v.vin or "")[-4:]} for v in vs]

    return asyncio.run(asyncio.wait_for(go(), timeout))


_users_lock = threading.Lock()
do_login = _byd_login            # a module attribute so tests can replace it; the real one talks only to BYD


def store_problem(e: Exception) -> str:
    """A plain reason for a failed secure save. Used by both the single-owner and the shared sign-in."""
    if isinstance(e, PermissionError):
        return "the encryption key file is readable by other users (it must be chmod 600, owned by the service user)"
    if isinstance(e, FileNotFoundError):
        return "the encryption key is missing (set BYD_APP_KEY_FILE, or LoadCredential= in the service, see deploy/README.md)"
    if isinstance(e, ValueError) and "too short" in str(e):
        return "the encryption key file is too short (it needs 32 random bytes or more)"
    if isinstance(e, OSError):
        return f"the data folder could not be written ({type(e).__name__}: check the folder exists and the service user owns it)"
    return "the encryption key could not be used"


class Handler(BaseHTTPRequestHandler):
    server_version = "BYDRange"
    sys_version = ""

    # injected by make_server
    token: str | None = None
    capacity_kwh = range_engine.DEFAULT_USABLE_KWH
    usable_fraction = DEFAULT_USABLE_FRACTION
    multi_user = False     # shared server: every person signs in with their own BYD account (--multi-user)
    max_users = 10
    invite_code: str | None = None
    assume_https = False   # the operator says a proxy terminates TLS but does not send X-Forwarded-Proto
    uid: str | None = None
    location = True        # False: never send coordinates or routes (--no-location)
    map_tiles = True       # False: never load map images from OpenStreetMap (--no-map-tiles)

    def log_message(self, fmt, *args):  # never log paths: they can contain the access token
        pass

    # -- helpers --
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None, form_self: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp(self.map_tiles, form_self))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, extra: dict | None = None):
        self._send(code, json.dumps(obj, separators=(",", ":")).encode(), "application/json", extra)

    def cap_kwh(self) -> float:
        """Nominal battery size: per user on a shared server (their own setting), else the one chosen at start / saved."""
        return settings.load()["battery_kwh"] if self.multi_user else self.capacity_kwh

    def _origin(self) -> str:
        """scheme://host the page was opened on, or "" if the Host header does not look like a host name (it goes into HTML)."""
        host = (self.headers.get("Host") or "").strip()
        if not HOST_RE.match(host):
            return ""
        https = self.assume_https or self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        return ("https" if https else "http") + "://" + host

    def _cookie_value(self, name: str) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            m = cookies.SimpleCookie(raw).get(name)
        except cookies.CookieError:
            return None
        return m.value if m else None

    def _client_ip(self) -> str:
        ip = self.client_address[0]
        if is_local_client(ip):                       # behind a proxy on this machine: its last X-Forwarded-For entry is the client
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[-1].strip()[:64]
        return ip

    def _secure_transport(self) -> bool:
        """A password must not cross plain HTTP: HTTPS (as reported by the proxy in front) or this very machine."""
        if self.assume_https or self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            return True
        return self._is_local()

    def _session_cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if (self.assume_https or self.headers.get("X-Forwarded-Proto", "").lower() == "https") else ""
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def _serve(self, fn):
        """Shared server: find the user from the session cookie and run the handler inside THEIR data folder (and only theirs)."""
        self.uid = users.lookup_session(self._cookie_value(SESSION_COOKIE)) if self.multi_user else None
        if self.uid is None:
            return fn()
        with db.use_dir(users.user_dir(self.uid)):
            return fn()

    def _text(self, code, msg):
        self._send(code, msg.encode(), "text/plain; charset=utf-8")

    def _cookie_ok(self) -> bool:
        raw = self.headers.get("Cookie")
        if not raw or not self.token:
            return False
        try:
            c = cookies.SimpleCookie(raw)
        except cookies.CookieError:
            return False
        m = c.get(COOKIE_NAME)
        return bool(m) and safe_eq(m.value, self.token)

    # -- routing --
    def do_GET(self):  # noqa: N802
        return self._serve(self._do_get)

    def _do_get(self):
        url = urlsplit(self.path)

        if self.multi_user:
            pass                      # public pages only; every data endpoint below needs a session
        elif self.token is None:
            # loopback-only mode: defeat DNS rebinding
            if _host_only(self.headers.get("Host", "")) not in LOOPBACK_HOSTS:
                return self._text(403, "Forbidden host")
        else:
            qtok = parse_qs(url.query).get("token", [""])[0]
            if qtok and safe_eq(qtok, self.token):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
                return self._send(302, b"", "text/plain", {
                    "Location": url.path or "/",
                    "Set-Cookie": f"{COOKIE_NAME}={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_MAX_AGE}{secure}",
                })
            if not self._cookie_ok() and url.path not in PUBLIC:
                if url.path == "/":
                    meta = og_meta(self._origin())
                    if PREVIEW_UA.search(self.headers.get("User-Agent", "")):      # a link-preview bot: tell it about the page
                        return self._send(200, PREVIEW_PAGE.replace("<!--OG-->", meta).encode(), "text/html; charset=utf-8")
                    return self._send(401, TOKEN_PAGE.replace("<!--OG-->", meta).encode(), "text/html; charset=utf-8", form_self=True)
                return self._text(401, "Unauthorized. Open the URL printed by server.py (it includes ?token=...).")

        path = url.path
        if path in STATIC:
            name, ctype = STATIC[path]
            f = WEB_DIR / name
            if not f.is_file():
                return self._text(404, "Not found")
            body = f.read_bytes()
            if path == "/":
                body = body.replace(b"<!--OG-->", og_meta(self._origin()).encode())
            if path == "/sw.js":
                body = body.replace(b"__VERSION__", shell_version().encode())
                return self._send(200, body, ctype, {"Service-Worker-Allowed": "/"})
            return self._send(200, body, ctype)
        if self.multi_user and self.uid is None and path.startswith("/api/") and path != "/api/account":
            if path == "/api/status":          # not signed in: the page shows the sign-in, nobody else's data
                return self._json({"has_data": False, "signed_in": False, "multi_user": True,
                                   "server_time": dt.datetime.now(dt.timezone.utc).isoformat()})
            return self._json({"error": "login-required"}, 401)
        if path == "/api/status":
            touch_viewer()
            conn = _connect_ro()
            try:
                data = build_status(conn, capacity_kwh=self.cap_kwh(), usable_fraction=self.usable_fraction,
                                    range_mode=settings.load()["range_mode"])
            finally:
                if conn:
                    conn.close()                 # close BEFORE replying: the client may delete the file the moment it hears back
            data["battery_kwh"] = self.cap_kwh()
            data["battery_set"] = settings.load()["battery_set"]
            data["signed_in"] = byd_config.credentials_present()
            data["poller_state"] = pollstatus.read()["state"]
            return self._json(data)
        if path == "/api/trips" or path.startswith("/api/trips/"):
            return self._trips(path)
        if path == "/api/battery":
            return self._battery()
        if path == "/api/account":
            return self._account()
        if path == "/api/charging" or path.startswith("/api/charging/"):
            return self._charging(path)
        if path == "/api/series":
            conn = _connect_ro()
            try:
                data = build_series(conn)
            finally:
                if conn:
                    conn.close()
            return self._json(data)
        return self._text(404, "Not found")

    def _trips(self, path: str):
        """Trip history. Deliberately does NOT touch the viewer flag: browsing history must not wake the car."""
        usable = self.cap_kwh() * self.usable_fraction
        conn = _connect_ro()
        try:
            rows = trips.load_rows(conn) if conn else []
            if conn:
                usable = capacity.learn_from_db(conn, self.cap_kwh(), usable)["usable_kwh"]
        finally:
            if conn:
                conn.close()
        if path == "/api/trips":
            items = trips.analyze(rows, usable)
            return self._json({"trips": items, "totals": trips.totals(items),
                               "location": self.location, "map_tiles": self.map_tiles})
        m = re.fullmatch(r"/api/trips/(\d{1,12})", path)
        if not m:
            return self._text(404, "Not found")
        d = trips.detail(rows, int(m.group(1)), usable, include_location=self.location)
        if d is None:
            return self._text(404, "Not found")
        d["location"] = self.location
        d["map_tiles"] = self.map_tiles
        return self._json(d)

    def _charging(self, path: str):
        """Charging sessions. Does not touch the viewer flag, and carries no coordinates."""
        cfg = settings.load()
        usable = self.cap_kwh() * self.usable_fraction
        conn = _connect_ro()
        try:
            rows = trips.load_rows(conn) if conn else []
            if conn:
                usable = capacity.learn_from_db(conn, self.cap_kwh(), usable)["usable_kwh"]
        finally:
            if conn:
                conn.close()
        kw = {"tariff": cfg["tariff_per_kwh"], "loss_pct": cfg["charging_loss_pct"]}
        extra = {"currency": cfg["currency"], "has_tariff": cfg["tariff_per_kwh"] is not None,
                 "loss_pct": cfg["charging_loss_pct"], "usable_kwh": round(usable, 2)}
        if path == "/api/charging":
            items = charging.analyze(rows, usable, **kw)
            if any(s["ongoing"] for s in items):
                touch_viewer()          # a charge in progress is being watched: poll for it (the car is awake charging anyway)
            return self._json({"sessions": items, "totals": charging.totals(items), **extra})
        m = re.fullmatch(r"/api/charging/(\d{1,12})", path)
        if not m:
            return self._text(404, "Not found")
        d = charging.detail(rows, int(m.group(1)), usable, **kw)
        if d is None:
            return self._text(404, "Not found")
        d.update(extra)
        if d["ongoing"]:
            touch_viewer()
        return self._json(d)

    def _battery(self):
        """Learned usable capacity. Does not touch the viewer flag: reading it must not wake the car."""
        conn = _connect_ro()
        try:
            prior = self.cap_kwh() * self.usable_fraction
            if conn is None:
                post = capacity.posterior([], prior, self.cap_kwh())
                post["samples"] = []
            else:
                post = dict(capacity.learn_from_db(conn, self.cap_kwh(), prior))
        finally:
            if conn:
                conn.close()

        def r(v, n=1):
            return None if v is None else round(v, n)

        battery = {k: (r(v, 2) if isinstance(v, float) else v) for k, v in post.items() if k != "samples"}
        battery["sd_rel_pct"] = r(post["sd_rel"] * 100, 1)
        samples = [{k: v for k, v in s.items() if k != "sd_rel"} for s in post["samples"]]
        return self._json({"battery": battery, "samples": samples})

    # -- account ---------------------------------------------------------------------------------------------------
    def _remote_reason(self) -> str | None:
        """None for a genuine local request; otherwise why not: "address" (another device), "host" (opened by a name that is not
        localhost) or "proxy" (a tunnel or proxy added its headers)."""
        if not is_local_client(self.client_address[0]):
            return "address"
        if _host_only(self.headers.get("Host", "")) not in LOOPBACK_HOSTS:
            return "host"
        if any(self.headers.get(h) is not None for h in PROXY_HEADERS):
            return "proxy"
        return None

    def _is_local(self) -> bool:
        """A genuine local request: loopback address, a loopback Host header, and no header a tunnel or proxy adds."""
        return self._remote_reason() is None

    def _account(self):
        """Who is signed in (masked), how it is stored, and what the poller says. Never a password."""
        if self.multi_user and self.uid is None:          # not signed in on a shared server: only what the sign-in form needs
            return self._json({
                "signed_in": False, "multi_user": True, "local": True, "user": None, "region": None, "country": None, "store": None,
                "poller": {"state": "unknown"}, "regions": [{"id": k, "label": v} for k, v in byd_config.REGIONS.items()],
                "confirmed": byd_config.CONFIRMED, "invite_required": bool(self.invite_code),
                "full": users.count() >= self.max_users,
                "battery": {"kwh": settings.DEFAULT_BATTERY_KWH, "set": False,
                            "options": [{"id": "standard", "label": "Standard Range", "kwh": 49.92},
                                        {"id": "extended", "label": "Extended Range", "kwh": 60.48}]},
            })
        creds = byd_config.load_credentials()
        return self._json({
            "multi_user": self.multi_user,
            "signed_in": creds is not None,
            "user": mask_username(creds["username"]) if creds else None,
            "region": byd_config.region_of(creds["base_url"]) if creds else None,
            "country": creds["country_code"] if creds else None,
            "store": credstore.describe(),
            "poller": pollstatus.read(),
            "local": self._is_local(),
            "remote_reason": self._remote_reason(),
            "host": _host_only(self.headers.get("Host", ""))[:80],
            "regions": [{"id": k, "label": v} for k, v in byd_config.REGIONS.items()],
            "confirmed": byd_config.CONFIRMED,
            "battery": {"kwh": self.cap_kwh(), "set": settings.load()["battery_set"],
                        "options": [{"id": "standard", "label": "Standard Range", "kwh": 49.92},
                                    {"id": "extended", "label": "Extended Range", "kwh": 60.48}]},
        })

    def _take_body(self):
        """
        Read the request body BEFORE deciding anything. Replying and closing with unread bytes still in the socket makes the OS
        reset the connection, and the client (or an SSH tunnel) may never see the reply. Small bodies are read whole, oversized ones
        are drained up to a bound and the connection is closed. Returns (body, problem) with problem None, "length" or "too-large".
        """
        cl = self.headers.get("Content-Length") or ""
        if not cl.isdigit():
            return b"", "length"
        n = int(cl)
        if n <= MAX_BODY:
            return self.rfile.read(n), None
        self.rfile.read(min(n, 65536))
        self.close_connection = True
        return b"", "too-large"

    def do_POST(self):  # noqa: N802
        return self._serve(self._do_post)

    def _do_post(self):
        body, problem = self._take_body()
        path = urlsplit(self.path).path
        if path not in ("/api/login", "/api/logout", "/api/settings"):
            return self._text(405, "Read-only")
        if self.multi_user:
            if path != "/api/login" and self.uid is None:
                return self._json({"error": "unauthorized", "message": "Sign in first."}, 401)
        elif self.token is not None and not self._cookie_ok():
            return self._json({"error": "unauthorized"}, 401)
        if path == "/api/login" and not self.multi_user and not self._is_local():     # signing in is local-only; signing out and settings are not
            return self._json({"error": "local-only", "message": "Sign in on the computer that runs the app (or through an SSH "
                               "tunnel to its localhost). Your password is never accepted over a tunnel, proxy or network."}, 403)
        # Login CSRF: a web page in your browser must not be able to sign this app in to someone else's account.
        if self.headers.get("X-BYD-App") != "1":
            return self._json({"error": "csrf"}, 403)
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return self._json({"error": "csrf"}, 403)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._json({"error": "content-type"}, 415)
        if problem == "length":
            return self._json({"error": "length"}, 411)
        if problem == "too-large":
            return self._json({"error": "too-large"}, 413)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return self._json({"error": "invalid", "message": "Not valid JSON."}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "invalid", "message": "Expected an object."}, 400)
        if path == "/api/logout":
            return self._logout(data)
        if path == "/api/settings":
            return self._settings(data)
        return self._login(data)

    def _settings(self, data: dict):
        """The battery size (the only setting the page can change). Needs the login cookie and the CSRF header, like sign-in."""
        try:
            kwh = settings.save_battery(data.get("battery_kwh"))
        except ValueError as e:
            return self._json({"error": "invalid", "field": "battery_kwh", "message": str(e)}, 400)
        except OSError:
            return self._json({"error": "write", "message": "Could not save the setting on the server."}, 500)
        if not self.multi_user:                  # a shared server keeps the size per user, in their own folder
            type(self).capacity_kwh = kwh        # takes effect at once for every page; the poller re-reads settings itself
        return self._json({"ok": True, "battery_kwh": kwh})

    def _logout(self, data: dict | None = None):
        if self.multi_user:
            # "Sign out" ends this device's session only (tracking continues). "forget": delete everything and stop tracking.
            forget = isinstance(data, dict) and data.get("forget") is True
            users.end_session(self._cookie_value(SESSION_COOKIE))
            if forget and self.uid:
                users.request_delete(self.uid)               # the supervisor stops their poller and wipes their folder
            return self._json({"ok": True, "forgotten": bool(forget)}, extra={"Set-Cookie": self._session_cookie("", 0)})
        credstore.delete()
        pollstatus.write("waiting-for-signin", "Signed out. Sign in to start reading your car.")
        return self._json({"ok": True})

    def _login(self, data: dict):
        def bad(field, message):
            return self._json({"error": "invalid", "field": field, "message": message}, 400)

        username, password = data.get("username"), data.get("password")
        region, country, tz = data.get("region"), data.get("country"), data.get("time_zone")
        if not (isinstance(username, str) and 3 <= len(username.strip()) <= 128 and username.isprintable()):
            return bad("username", "Enter your BYD account (email or phone).")
        if not (isinstance(password, str) and 1 <= len(password) <= 256 and password.isprintable()):
            return bad("password", "Enter your BYD password.")
        if region not in byd_config.REGIONS:
            return bad("region", "Choose a region.")
        if not (isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country)):
            return bad("country", "Enter a two-letter country code, for example PK.")
        if not (isinstance(tz, str) and len(tz) <= 64 and TZ_RE.match(tz)):
            tz = "UTC"
        if data.get("acknowledged") is not True:
            return bad("acknowledged", "Confirm that this is a dedicated sub-account, not your primary BYD login.")
        uid = None
        if self.multi_user:
            if not self._secure_transport():
                return self._json({"error": "insecure", "message": "Sign in needs a secure (https) connection, so your password is "
                                   "not sent in the clear. Open this server's https address."}, 403)
            if data.get("shared_risk_ack") is not True:
                return bad("shared_risk_ack", "Confirm that you understand the person running this server can see your BYD login "
                           "and your car's data.")
            uid = users.user_id(username, region)
            ip = self._client_ip()
            if self.invite_code:
                # The code is needed by EVERYONE, member or not, and a wrong one is refused before anything else happens. So it cannot be
                # used to find out who has an account, to use up a member's sign-in budget, or to make this server relay guesses to BYD.
                if multi_limited(uid, ip, ("i",)):
                    return self._json({"error": "rate-limit", "message": "Too many attempts. Wait 10 minutes."}, 429)
                if not safe_eq(str(data.get("invite") or ""), self.invite_code):
                    multi_note(uid, ip, ("i",))
                    return self._json({"error": "invite", "field": "invite", "message": "This server needs an invite code. Ask the "
                                       "person who runs it."}, 403)
            limited = multi_limited(uid, ip)
            if not limited:
                multi_note(uid, ip, ("i",))                    # every attempt counts against the address it came from
        else:
            limited = login_rate_limited()
        if limited:
            return self._json({"error": "rate-limit", "message": "Too many sign-in attempts. Wait 10 minutes: repeated wrong logins "
                               "can lock a BYD account."}, 429)
        if uid is not None:
            st = users.state(uid)
            if st == "removing":
                return self._json({"error": "removing", "message": "Your earlier data is still being removed. Try again in a minute."}, 409)
            if st == "none":
                if users.count() >= self.max_users:
                    return self._json({"error": "full", "message": "This server has reached its limit of users."}, 403)
        creds = {"username": username.strip(), "password": password, "base_url": byd_config.base_url_for(region),
                 "country_code": country.upper(), "language": "en", "time_zone": tz}
        try:
            vehicles = do_login(creds)
        except Exception as e:  # noqa: BLE001
            names = {c.__name__ for c in type(e).__mro__}
            if "BydAuthenticationError" in names:
                if uid is not None:
                    multi_note(uid, ip, ("u", "all"))          # only a login BYD rejected counts against the account and the server
                msg = str(e).replace(password, "***")[:200]
                return self._json({"error": "rejected", "code": str(getattr(e, "code", "") or ""), "message": msg,
                                   "hint": "BYD runs one server per region: with correct details, try another region."}, 401)
            if isinstance(e, ImportError):
                return self._json({"error": "setup", "message": "pyBYD is not installed in this environment, so the app cannot "
                                   "talk to BYD. Install the locked requirements: pip install --require-hashes -r requirements.txt"}, 500)
            return self._json({"error": "unreachable", "message": f"Could not reach BYD ({type(e).__name__}). Check this "
                               "computer's internet connection and the region."}, 502)
        if uid is not None:
            return self._login_shared(uid, creds, vehicles)
        try:
            protection = credstore.save(creds)
        except (OSError, ValueError) as e:
            why = ("the key file is readable by other users (chmod 600 it)" if isinstance(e, PermissionError)
                   else "the key file named for encryption is missing" if isinstance(e, FileNotFoundError)
                   else "the encryption key could not be used")
            return self._json({"error": "store", "message": f"BYD accepted the sign-in, but it could not be stored securely: {why}. "
                               "Nothing was saved."}, 500)
        finally:
            creds.clear()
        pollstatus.write("starting", "Signed in. Starting to read your car.")
        return self._json({"ok": True, "vehicles": vehicles, "protection": protection,
                           "key_source": credstore.describe()["key_source"]})

    def _login_shared(self, uid: str, creds: dict, vehicles: list):
        """BYD accepted this person: give them their own folder, store their login there (encrypted), start a session."""
        was_new = users.state(uid) == "none"
        with _users_lock:
            if was_new and users.count() >= self.max_users:
                creds.clear()
                return self._json({"error": "full", "message": "This server has reached its limit of users."}, 403)
            users.add(uid)
        try:
            with db.use_dir(users.user_dir(uid)):
                credstore.save(creds)
                pollstatus.write("starting", "Signed in. Starting to read your car.")
        except (OSError, ValueError) as e:
            if was_new:
                users.purge(uid)
            why = store_problem(e)
            # The operator's log gets the exact cause (no password, no account name); the person gets the plain reason.
            print(f"could not store a sign-in: {type(e).__name__}: {e} -> {why}", flush=True)
            return self._json({"error": "store", "message": f"BYD accepted the sign-in, but this server could not store it securely: "
                               f"{why}. Nothing was saved. Tell the person who runs the server."}, 500)
        finally:
            creds.clear()
        token = users.create_session(uid)
        return self._json({"ok": True, "vehicles": vehicles}, extra={"Set-Cookie": self._session_cookie(token, SESSION_MAX_AGE)})

    def _read_only(self):
        self._take_body()                       # read it first, so the reply is not lost to a connection reset
        self._text(405, "Read-only")

    do_PUT = do_DELETE = do_PATCH = _read_only


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def load_or_create_token(path: Path) -> str:
    if path.exists():
        t = path.read_text(encoding="utf-8").strip()
        if len(t) >= 16:
            return t
    path.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(24)
    path.write_text(t + "\n", encoding="utf-8")
    return t


def make_server(host: str, port: int, *, token: str | None, capacity_kwh: float, usable_fraction: float,
                location: bool = True, map_tiles: bool = True, multi_user: bool = False, max_users: int = 10,
                invite_code: str | None = None, assume_https: bool = False):
    if multi_user and token is not None:
        raise ValueError("--multi-user has its own sign-in; do not combine it with an access token")
    handler = type("BoundHandler", (Handler,), {
        "token": token, "capacity_kwh": capacity_kwh, "usable_fraction": usable_fraction,
        "location": location, "map_tiles": map_tiles, "multi_user": multi_user, "max_users": max_users,
        "invite_code": invite_code or None, "assume_https": assume_https,
    })
    return ThreadingHTTPServer((host, port), handler)


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # no packet is sent; just selects the outbound interface
            return s.getsockname()[0]
    except OSError:
        return "<this-pc-ip>"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    cfg = settings.load()
    ap.add_argument("--capacity", type=float, default=cfg["battery_kwh"],
                    help="nominal battery kWh (49.92 Standard, 60.48 Extended); default from settings.json")
    ap.add_argument("--usable-fraction", type=float, default=cfg["usable_fraction"],
                    help="assumed usable share of nominal capacity; default from settings.json")
    ap.add_argument("--require-token", action="store_true",
                    help="require the access token even on localhost (use this behind ngrok / Cloudflare Tunnel)")
    ap.add_argument("--no-location", action="store_true",
                    help="never send coordinates or routes to the page (trip stats still work)")
    ap.add_argument("--no-map-tiles", action="store_true",
                    help="draw routes on a plain background; never request OpenStreetMap map images")
    ap.add_argument("--multi-user", action="store_true",
                    help="SHARED server: every person signs in with their own BYD account and sees only their own car "
                         "(use run.py, which also starts one poller per user). Put HTTPS in front of it.")
    ap.add_argument("--max-users", type=int, default=10, help="shared server: how many people may sign up (default 10)")
    ap.add_argument("--assume-https", action="store_true",
                    help="shared server: a proxy in front terminates HTTPS but does not send X-Forwarded-Proto")
    a = ap.parse_args()
    if a.multi_user and a.require_token:
        ap.error("--multi-user has its own sign-in; do not combine it with --require-token")

    exposed = not is_loopback(a.host) or a.require_token
    token = (load_or_create_token(db.DB_PATH.parent / "access_token.txt")
             if exposed and not a.multi_user else None)
    invite = os.environ.get("BYD_APP_INVITE") or None          # an environment variable: never visible in the process list
    srv = make_server(a.host, a.port, token=token, capacity_kwh=a.capacity, usable_fraction=a.usable_fraction,
                      location=not a.no_location, map_tiles=not a.no_map_tiles and not a.no_location,
                      multi_user=a.multi_user, max_users=a.max_users, invite_code=invite, assume_https=a.assume_https)
    if a.multi_user:
        print(f"SHARED SERVER on {a.host}:{a.port}: up to {a.max_users} people, each with their own BYD sign-in and their own data.")
        print("Invite code: " + ("required (BYD_APP_INVITE)" if invite else "none: anyone who can open the page may sign up"))
        print("Run it with run.py (it starts one poller per person and wipes the data of people who delete themselves).")
        print("Passwords are only accepted over https (X-Forwarded-Proto from your proxy"
              + (", or --assume-https" if a.assume_https else "") + ") or from this machine.")
        print("Ctrl+C to stop.\n")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            srv.server_close()
        return

    print(f"BYD range app on {a.host}:{a.port}   (database: {db.DB_PATH})")
    print("Trip routes: " + ("OFF (--no-location)" if a.no_location else
          "on, plain background (--no-map-tiles)" if a.no_map_tiles else
          "on, map images from tile.openstreetmap.org (use --no-map-tiles to stop that)"))
    print(f"Battery: {a.capacity} kWh nominal x {a.usable_fraction} usable = {a.capacity * a.usable_fraction:.1f} kWh"
          + ("" if cfg["battery_set"] else "   (NOT SET: assuming Standard Range. Choose yours on the Account page.)"))
    show_token = sys.stdout.isatty()     # never write the secret into a redirected log file
    tok = token if show_token else "<token is in data/access_token.txt>"
    if exposed and is_loopback(a.host):
        print(f"Token mode on localhost. Local URL:  http://localhost:{a.port}/?token={tok}")
        print(f"Tunnel it (example):  ngrok http {a.port}   then open  https://<your-ngrok-host>/?token={tok}")
        print("The token is the ONLY thing protecting the public tunnel URL. Anyone with the full link sees your")
        print("range, battery and speed, and the tunnel provider can see the traffic. Prefer Tailscale if you can.")
    elif exposed:
        shown = _lan_ip() if a.host in ("0.0.0.0", "::") else a.host
        print(f"Open on your phone:  http://{shown}:{a.port}/?token={tok}")
        print("This is plain HTTP with a secret token. Only use it on a network you trust, or through a private")
        print("tunnel such as Tailscale. Do NOT port-forward it to the internet.")
    else:
        print(f"Open:  http://localhost:{a.port}/")
    print("Ctrl+C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
