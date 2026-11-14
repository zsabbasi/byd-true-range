"""
Parked battery drain: how much the displayed battery percentage falls while the car sits still and is not charging.

It is built only from readings the app already stores (no new request to BYD). A "stretch" is a run of consecutive readings with the car
not driving, not charging, the odometer unchanged and the battery never rising. Its drain is the fall between its first and last reading
over the time between them. Only stretches of at least MIN_STRETCH_H hours count, because the battery percent is a whole number: a
fall of one percent over a few hours says almost nothing, while a few days of stretches say something.

This is for comparing: change how often the parked car is checked (Account page), wait a few days, and see whether the drain moved.
It cannot see the 12 V battery, and it cannot tell BYD's own parked drain from anything the checks cause.
"""
from __future__ import annotations

import datetime as dt

MIN_STRETCH_H = 2.0
LOOKBACK_DAYS = 14
MIN_TOTAL_H = 6.0              # below this much parked time in total there is no honest rate to show
RECENT = 6


def _parse(ts: str | None) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def stretches(rows, min_hours: float = MIN_STRETCH_H) -> list[dict]:
    """rows: oldest-first (ts_iso, soc, odometer_km, is_driving, charging_state)."""
    out: list[dict] = []
    cur: list[tuple] = []

    def flush():
        nonlocal cur
        if len(cur) >= 2:
            hours = (cur[-1][0] - cur[0][0]).total_seconds() / 3600.0
            if hours >= min_hours:
                out.append({"start": cur[0][0].isoformat(), "hours": hours, "soc_start": cur[0][1], "soc_end": cur[-1][1],
                            "drop": cur[0][1] - cur[-1][1], "readings": len(cur)})
        cur = []

    for ts, soc, odo, driving, charging in rows:
        t = _parse(ts)
        if t is None or soc is None or odo is None:
            continue
        if driving or str(charging or "").upper() == "CHARGING":
            flush()
            continue
        if cur and (odo != cur[-1][2] or soc > cur[-1][1]):
            flush()
        cur.append((t, float(soc), float(odo)))
    flush()
    return out


def summary(rows, now: dt.datetime | None = None, days: int = LOOKBACK_DAYS) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    horizon = now - dt.timedelta(days=days)
    st = [s for s in stretches(rows) if _parse(s["start"]) >= horizon]
    hours = sum(s["hours"] for s in st)
    drop = sum(s["drop"] for s in st)
    readings = sum(s["readings"] for s in st)
    return {
        "days": days,
        "stretches_count": len(st),
        "hours": round(hours, 1),
        "drop_pct": round(drop, 1),
        "pct_per_day": round(drop / hours * 24.0, 2) if hours >= MIN_TOTAL_H else None,
        "readings_per_hour": round(readings / hours, 1) if hours > 0 else None,
        "min_stretch_h": MIN_STRETCH_H,
        "min_total_h": MIN_TOTAL_H,
        "recent": [{"start": s["start"], "hours": round(s["hours"], 1), "soc_start": s["soc_start"], "soc_end": s["soc_end"]}
                   for s in reversed(st[-RECENT:])],
    }


def for_db(conn, now: dt.datetime | None = None) -> dict:
    """The summary for an open database connection (an empty summary if the readings cannot be read)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(days=LOOKBACK_DAYS + 1)).isoformat()
    try:
        rows = conn.execute("SELECT ts, soc, odometer_km, is_driving, charging_state FROM readings "
                            "WHERE ts >= ? AND soc > 0 AND odometer_km > 0 ORDER BY id", (since,)).fetchall()
    except Exception:  # noqa: BLE001 - no readings table yet, a locked file: show "not enough yet", never an error
        rows = []
    return summary(rows, now)
