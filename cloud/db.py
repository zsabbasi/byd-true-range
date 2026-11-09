"""
Local SQLite history for BYD cloud readings. This is the history BYD itself doesn't keep.
One row per successful poll. All data stays in  data/history.sqlite  on this machine.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

def data_dir() -> Path:
    """Where history, the encrypted sign-in and the poller status live. $BYD_DATA_DIR relocates it (a systemd service
    keeps it under /var/lib/byd-range); the default is the data/ folder next to the code."""
    env = os.environ.get("BYD_DATA_DIR")
    return Path(env) if env else Path(__file__).with_name("data")


DB_PATH = data_dir() / "history.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,              -- ISO8601 UTC, when we recorded it
    car_ts          TEXT,                       -- BYD's own data timestamp, if present
    vin_tail        TEXT,                       -- last 4 of VIN only (never the full VIN)
    soc             REAL,                       -- battery %
    odometer_km     REAL,                       -- total_mileage
    byd_range_km    REAL,                       -- endurance_mileage (BYD's own estimate)
    speed_kmh       REAL,
    power_w         REAL,                       -- gl (instant battery power)
    charging_state  TEXT,
    recent_50km     TEXT,                       -- BYD's own recent_50km_energy, raw
    r50_rate        REAL,                       -- BYD avg consumption, last 50 km (kWh/100km)
    r50_kwh         REAL,                       -- BYD energy used over last 50 km (kWh), energy endpoint
    r50_air_pct     REAL,                       -- % of last 50 km with HVAC active
    gear            TEXT,                       -- power_gear name (OFF/ON): logged to verify driving detection
    vehicle_state   TEXT,                       -- vehicle_state name (pyBYD: ON "not confirmed")
    lat             REAL,
    lon             REAL,
    is_driving      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
"""


NEW_COLUMNS = {"r50_rate": "REAL", "r50_kwh": "REAL", "r50_air_pct": "REAL",
               "gear": "TEXT", "vehicle_state": "TEXT"}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to databases created earlier."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
    for name, typ in NEW_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {name} {typ}")
    conn.commit()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def insert_reading(conn: sqlite3.Connection, row: dict) -> None:
    cols = ("ts", "car_ts", "vin_tail", "soc", "odometer_km", "byd_range_km", "speed_kmh",
            "power_w", "charging_state", "recent_50km", "r50_rate", "r50_kwh", "r50_air_pct",
            "lat", "lon", "is_driving", "gear", "vehicle_state")
    conn.execute(
        f"INSERT INTO readings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        tuple(row.get(c) for c in cols),
    )
    conn.commit()


def recent_readings(conn: sqlite3.Connection, history_km: float = 400.0, limit: int = 5000) -> list[dict]:
    """
    Recent readings, OLDEST first, for range math. Goes back until the odometer is history_km behind the
    newest reading (charging gaps are handled by the engine, which splits on SOC rises), capped at `limit` rows.
    """
    rows = conn.execute(
        "SELECT ts, soc, odometer_km, charging_state FROM readings "
        "WHERE odometer_km > 0 AND soc > 0 ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[dict] = []
    newest_odo = None
    for ts, soc, odo, charging in rows:
        if newest_odo is None:
            newest_odo = odo
        if newest_odo - odo > history_km:
            break
        out.append({"ts": ts, "soc": soc, "odometer_km": odo, "charging_state": charging})
    out.reverse()
    return out


def observed_km(conn: sqlite3.Connection, max_gap_s: float = 600.0, max_kmh: float = 300.0) -> float:
    """
    Distance the app has actually WATCHED: odometer gains between consecutive readings at most max_gap_s apart,
    measured on the CAR's own clock (car_ts). Our poll time is useless here: while the car is silent BYD hands back the
    same old snapshot and our clock keeps ticking, which would make an unreported 12 km look like a 3 minute drive.
    Unreported stretches (the car was silent), stale duplicates, and empty snapshots do not count. This is the
    "own data" the owner's rule measures against OWN_MIN_KM.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(d), 0) FROM ("
        " SELECT odometer_km - LAG(odometer_km) OVER w AS d,"
        "        (julianday(COALESCE(NULLIF(car_ts, ''), ts))"
        "         - julianday(LAG(COALESCE(NULLIF(car_ts, ''), ts)) OVER w)) * 86400.0 AS gap"
        " FROM readings WHERE soc > 0 AND odometer_km > 0 WINDOW w AS (ORDER BY id))"
        " WHERE d > 0 AND gap > 0 AND gap <= ? AND d / (gap / 3600.0) <= ?",
        (max_gap_s, max_kmh),
    ).fetchone()
    return float(row[0] or 0.0)


def best_r50(conn: sqlite3.Connection, floor: float = 6.0) -> float | None:
    """
    The best (lowest) last-50-km consumption BYD has ever reported for this car, in kWh/100 km: measured energy, so it
    bounds how good the road ahead can plausibly be. Values below `floor` are sentinels, not driving.
    """
    row = conn.execute(
        "SELECT MIN(r50_rate) FROM readings WHERE r50_rate >= ? AND soc > 0 AND odometer_km > 0", (floor,)
    ).fetchone()
    return None if row is None or row[0] is None else float(row[0])
