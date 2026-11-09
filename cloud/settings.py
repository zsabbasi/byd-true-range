"""
Non-secret user settings, shared by the poller and the web server. Read from cloud/settings.json if present
(see settings.example.json). No credentials live here (the sign-in is in the encrypted store).

    battery_kwh      nominal pack size: 49.92 (Atto 3 Standard Range) or 60.48 (Extended Range)
    usable_fraction  share of the nominal energy that is really usable. Leave at 1.0 unless you KNOW it: the app starts
                     from the battery size you give and the Battery tab measures the real usable energy from your drives.
    tariff_per_kwh   OPTIONAL. Your price per kWh. Only then does the charging log show a cost. Nothing is assumed.
    currency         OPTIONAL label for the cost, for example "PKR".
    charging_loss_pct OPTIONAL. Your charger/cable loss in percent (0-40). Default 0: cost is for energy INTO the battery.
    range_mode       "policy" (default: BYD's measured consumption until 50 km of your own data, then your data only),
                     "own" (your driving, BYD consumption only if there is none) or "combined" (experimental blend).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

LEGACY_FILE = Path(__file__).with_name("settings.json")      # where older versions kept it (next to the code)
SETTINGS_FILE = None                                          # tests may point this at a file; None = <data dir>/settings.json
DEFAULT_BATTERY_KWH = 49.92                                   # Standard Range: the usual one in Pakistan. NOT silent: see battery_set
DEFAULTS = {"battery_kwh": DEFAULT_BATTERY_KWH, "usable_fraction": 1.0, "range_mode": "policy",
            "tariff_per_kwh": None, "currency": "", "charging_loss_pct": 0.0}
RANGE_MODES = ("policy", "own", "combined")
LIMITS = {"battery_kwh": (20.0, 150.0), "usable_fraction": (0.5, 1.0), "charging_loss_pct": (0.0, 40.0)}


def write_path() -> Path:
    if SETTINGS_FILE:
        return Path(SETTINGS_FILE)
    import db
    return Path(db.DB_PATH).parent / "settings.json"      # same folder as the history (tests redirect DB_PATH)


def settings_path() -> Path:
    """<data dir>/settings.json (the data dir is writable in a service); an older file next to the code is still read."""
    p = write_path()
    return p if p.exists() or not LEGACY_FILE.exists() else LEGACY_FILE


def load(path: Path | None = None) -> dict:
    """Defaults overlaid with valid values from the file. Invalid or missing values are ignored, never fatal.
    `battery_set` is True only if the file holds a valid battery size, so the app can say when it is assuming one."""
    out = dict(DEFAULTS)
    out["battery_set"] = False
    f = Path(path) if path else settings_path()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    for key, (lo, hi) in LIMITS.items():
        v = data.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= float(v) <= hi:
            out[key] = float(v)
            if key == "battery_kwh":
                out["battery_set"] = True
    if data.get("range_mode") in RANGE_MODES:
        out["range_mode"] = data["range_mode"]
    t = data.get("tariff_per_kwh")
    if isinstance(t, (int, float)) and not isinstance(t, bool) and 0 <= float(t) <= 100000:
        out["tariff_per_kwh"] = float(t)
    c = data.get("currency")
    if isinstance(c, str) and len(c) <= 8 and c.isprintable():
        out["currency"] = c
    return out


def usable_kwh(s: dict | None = None) -> float:
    s = s or load()
    return s["battery_kwh"] * s["usable_fraction"]



def save_battery(kwh, path: Path | None = None) -> float:
    """Store the battery size, keeping every other key in the file. Raises ValueError outside the allowed range."""
    lo, hi = LIMITS["battery_kwh"]
    if isinstance(kwh, bool) or not isinstance(kwh, (int, float)) or kwh != kwh or not lo <= float(kwh) <= hi:
        raise ValueError(f"battery size must be between {lo:g} and {hi:g} kWh")
    dest = Path(path) if path else write_path()
    try:
        data = json.loads((Path(path) if path else settings_path()).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["battery_kwh"] = round(float(kwh), 2)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    return data["battery_kwh"]
