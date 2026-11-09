"""
What the poller is doing, for the Account page: written by poller.py, read by server.py. One tiny JSON file next to the history.
It never contains credentials: only a state and a short message.

States: starting, ok, waiting-for-signin, auth-failed, no-vehicle, error.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import db

STATES = ("starting", "ok", "waiting-for-signin", "auth-failed", "no-vehicle", "error", "unknown")


def path() -> Path:
    return Path(db.DB_PATH).parent / "poller_status.json"


def write(state: str, detail: str = "") -> None:
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"state": state, "detail": detail[:300],
                                   "updated": dt.datetime.now(dt.timezone.utc).isoformat()}), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass                                           # a status file that cannot be written must never stop the poller


def read() -> dict:
    try:
        d = json.loads(path().read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("state") in STATES:
            return {"state": d["state"], "detail": str(d.get("detail", ""))[:300], "updated": d.get("updated")}
    except (OSError, ValueError):
        pass
    return {"state": "unknown", "detail": "", "updated": None}
