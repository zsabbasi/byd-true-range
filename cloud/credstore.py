"""
Where the BYD sign-in lives on THIS computer, encrypted at rest. The password is never written anywhere else, never logged,
and never returned by any API.

Two schemes, chosen by platform:

* Linux / macOS (the deployment target): AES-256-GCM via `cryptography` (already a locked dependency of pyBYD, so nothing new).
  The key is NOT stored next to the data. It is read, in this order, from
    1. the file named by $BYD_APP_KEY_FILE,
    2. a systemd credential: $CREDENTIALS_DIRECTORY/byd_credential_key  (systemd `LoadCredential=` from a root-owned file),
    3. ~/.config/byd-range/credential.key, created with mode 0600 on first sign-in (outside the data folder).
  What this protects: a copied or backed-up data folder, a database dump, an accidentally shared folder. With (1) or (2) the key is
  also unreachable to other users. What it does NOT protect against: root, or a compromise of the app itself (it must decrypt
  to sign in). The sign-in page says which key source is in use.
* Windows (development): DPAPI (CryptProtectData), encrypted for this Windows user on this computer.

A stored file that cannot be read (wrong key, another machine, tampering, a key file that is missing or readable by others) is
treated as "not signed in", never a crash, and the Account page says why. There is deliberately no plain-text store.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import sys
from pathlib import Path

import db

MAGIC = b"BYDCRED1"
KIND_DPAPI = b"D"
KIND_AES = b"A"
_ENTROPY = b"byd-range-app credential store v1"     # extra DPAPI input: a different app's blob is not accepted
REQUIRED = ("username", "password", "base_url", "country_code", "language", "time_zone")
MAX_FIELD = 512
SALT_LEN, NONCE_LEN = 16, 12
MIN_KEY_LEN = 16
KEY_LABELS = {"env": "key file named by BYD_APP_KEY_FILE", "systemd": "systemd credential (byd_credential_key)",
              "default": "key file in your config folder, outside the data folder"}


def path() -> Path:
    return db.current_dir() / "credentials.bin"


# ----------------------------------------------------------------------------------------------------- key file ----
def default_key_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "byd-range" / "credential.key"


def key_source() -> tuple[Path, str]:
    """(path, label key) of the key file in use, by priority."""
    env = os.environ.get("BYD_APP_KEY_FILE")
    if env:
        return Path(env), "env"
    cd = os.environ.get("CREDENTIALS_DIRECTORY")
    if cd and (Path(cd) / "byd_credential_key").is_file():
        return Path(cd) / "byd_credential_key", "systemd"
    return default_key_path(), "default"


def _read_key_secret(create_default: bool) -> bytes:
    p, label = key_source()
    if not p.exists():
        if label != "default" or not create_default:
            raise FileNotFoundError("key file missing")
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p.parent, 0o700)
        except OSError:
            pass
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(_secrets.token_hex(32).encode())
    if sys.platform != "win32" and (p.stat().st_mode & 0o077):
        raise PermissionError("key file is readable by other users (chmod 600 it)")
    data = p.read_bytes().strip()
    if len(data) < MIN_KEY_LEN:
        raise ValueError("key file is too short")
    return data


def _derive(secret: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(secret, salt=salt, n=2**14, r=8, p=1, dklen=32)


def _aes_encrypt(payload: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    key = _derive(_read_key_secret(create_default=True), salt)
    return salt + nonce + AESGCM(key).encrypt(nonce, payload, MAGIC + KIND_AES)


def _aes_decrypt(body: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(body) < SALT_LEN + NONCE_LEN + 16:
        raise ValueError("truncated")
    salt, nonce, ct = body[:SALT_LEN], body[SALT_LEN: SALT_LEN + NONCE_LEN], body[SALT_LEN + NONCE_LEN:]
    key = _derive(_read_key_secret(create_default=False), salt)
    return AESGCM(key).decrypt(nonce, ct, MAGIC + KIND_AES)          # raises InvalidTag on a wrong key or any tampering


# ------------------------------------------------------------------------------------------------------- DPAPI ----
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt32.CryptProtectData.restype = crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    def blob(b: bytes) -> tuple[Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    inb, keep1 = blob(data)
    ent, keep2 = blob(_ENTROPY)
    out = Blob()
    ui_forbidden = 0x1                                   # never pop a Windows dialog from a background process
    ok = (crypt32.CryptProtectData(ctypes.byref(inb), "BYD range app", ctypes.byref(ent), None, None, ui_forbidden,
                                   ctypes.byref(out)) if protect else
          crypt32.CryptUnprotectData(ctypes.byref(inb), None, ctypes.byref(ent), None, None, ui_forbidden, ctypes.byref(out)))
    if not ok:
        raise OSError("DPAPI refused")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
        del keep1, keep2


def default_scheme() -> str:
    return "dpapi" if sys.platform == "win32" else "aes"


def method_name(scheme: str | None = None) -> str:
    if (scheme or default_scheme()) == "dpapi":
        return "Windows DPAPI: encrypted for this Windows user on this computer"
    return "AES-256-GCM, key kept in a " + KEY_LABELS[key_source()[1]]


# --------------------------------------------------------------------------------------------------- public API ----
def _valid(creds: object) -> bool:
    return (isinstance(creds, dict) and all(isinstance(creds.get(k), str) and 0 < len(creds[k]) <= MAX_FIELD for k in REQUIRED))


def save(creds: dict, scheme: str | None = None) -> str:
    """Store the sign-in, replacing any previous one. Returns the protection used. Raises ValueError on bad input."""
    if not _valid(creds):
        raise ValueError("incomplete credentials")
    scheme = scheme or default_scheme()
    payload = json.dumps({k: creds[k] for k in REQUIRED}).encode("utf-8")
    if scheme == "dpapi":
        blob = MAGIC + KIND_DPAPI + _dpapi(payload, True)
    elif scheme == "aes":
        blob = MAGIC + KIND_AES + _aes_encrypt(payload)
    else:
        raise ValueError("unknown scheme")
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp, p)                                 # atomic: a crash never leaves half a file
    finally:
        if tmp.exists():
            tmp.unlink()
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return method_name(scheme)


def exists() -> bool:
    return path().exists()


def _try_load() -> tuple[dict | None, str | None]:
    """(credentials, None) or (None, short reason it cannot be read). The reason never contains a secret."""
    try:
        raw = path().read_bytes()
    except FileNotFoundError:
        return None, "none stored"
    except OSError:
        return None, "the stored sign-in file cannot be read"
    try:
        if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 2:
            return None, "the stored sign-in file is damaged"
        kind, body = raw[len(MAGIC): len(MAGIC) + 1], raw[len(MAGIC) + 1:]
        if kind == KIND_DPAPI:
            if sys.platform != "win32":
                return None, "the stored sign-in was made on Windows and cannot be opened here: sign in again"
            body = _dpapi(body, False)
        elif kind == KIND_AES:
            try:
                body = _aes_decrypt(body)
            except FileNotFoundError:
                return None, "the key file for the stored sign-in is missing: sign in again"
            except PermissionError:
                return None, "the key file is readable by other users: run chmod 600 on it"
            except ValueError as e:
                return None, "the key file is unusable (" + str(e) + ")"
            except Exception:                                   # noqa: BLE001  (InvalidTag: wrong key or tampering)
                return None, "the stored sign-in cannot be decrypted with this key: sign in again"
        else:
            return None, "the stored sign-in file is damaged"
        creds = json.loads(body.decode("utf-8"))
        if not _valid(creds):
            return None, "the stored sign-in file is damaged"
        return {k: creds[k] for k in REQUIRED}, None
    except OSError:
        return None, "the stored sign-in could not be decrypted: sign in again"
    except (ValueError, KeyError, UnicodeDecodeError):
        return None, "the stored sign-in file is damaged"


def load() -> dict | None:
    """The stored sign-in, or None if there is none or it cannot be read here."""
    return _try_load()[0]


def stored_scheme() -> str | None:
    """How the file on disk is protected ("dpapi" or "aes"), from its header; None if absent or unrecognised."""
    try:
        head = path().read_bytes()[: len(MAGIC) + 1]
    except OSError:
        return None
    if not head.startswith(MAGIC) or len(head) <= len(MAGIC):
        return None
    return {KIND_DPAPI: "dpapi", KIND_AES: "aes"}.get(head[len(MAGIC): len(MAGIC) + 1])


def describe() -> dict:
    """What the Account page may show. No secrets, no paths. Describes the file that is actually stored."""
    creds, reason = _try_load()
    present = exists()
    scheme = stored_scheme() or default_scheme()
    return {"present": present, "readable": creds is not None, "reason": None if creds is not None or not present else reason,
            "protection": method_name(scheme), "key_source": KEY_LABELS[key_source()[1]] if scheme == "aes" else None}


def delete() -> bool:
    try:
        path().unlink()
        return True
    except FileNotFoundError:
        return False


def version() -> int | None:
    """Changes whenever the stored sign-in is saved or removed (the poller watches this to pick up a new sign-in)."""
    try:
        return path().stat().st_mtime_ns
    except OSError:
        return None
