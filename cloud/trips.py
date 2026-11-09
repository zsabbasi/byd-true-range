"""
Trip detection and per-trip consumption from the stored readings. Pure functions plus one loader; standard library
only. Used by server.py (/api/trips).

Rules (all driven by what the real data showed, see docs/PROJECT_PLAN.md):
* A pair of consecutive readings is ACTIVE if either is driving (speed > 0 or gear ON) or the odometer advanced.
  A trip is a run of active pairs with no silence longer than GAP_MAX_S. The first reading before the first driving
  reading is kept as the start anchor, and the first non-driving reading after is kept as the end anchor.
* If the car goes silent (long gap) and the odometer advanced, that is reported as an UNOBSERVED drive: distance and
  battery change are known, the route and start time are not. It is never hidden and never given a route.
* Consumption needs a minimum battery drop and distance. Below that, the figure is None ("too short"): SOC is a whole
  percent, so a 3 km trip cannot yield a meaningful kWh/100 km. Totals across many trips average out and are reliable.
* When a trip has two or more battery ticks, consumption is measured tick to tick (exact SOC difference); otherwise
  from the start and end readings. Each figure carries a one-sigma uncertainty.
"""
from __future__ import annotations

import datetime as dt
import math
import sqlite3

import range_engine

GAP_MAX_S = 600          # longest silence allowed inside one trip (polls are 30 s while driving)
MAX_PLAUSIBLE_KMH = 300.0   # an odometer jump implying more than this is bad data, not a drive
MIN_TRIP_KM = 0.5
MIN_CONS_KM = 3.0
MIN_CONS_DROP = 2.0      # percent points of battery
MIN_REGEN_SAMPLES = 8    # power samples a trip needs before a regen figure is shown
SAMPLE_CAP_S = 90        # one power reading stands for at most this long (the poll interval while driving is 30 s)
REGEN_SE_INFLATION = 1.25  # regen is short bursts, so a plain standard error is a little too tight: 93% -> 96% coverage
                           # at 2 sd in simulation (tools: see tests/test_regen.py). Per-trip regen is still ROUGH (~35% error).
ODO_RES_KM = range_engine.ODO_RESOLUTION_KM
ENERGY_SD_PCT = range_engine.ENDPOINT_SD * math.sqrt(2)     # one trip's percent uncertainty (two endpoints)

COLUMNS = ("id, ts, car_ts, soc, odometer_km, speed_kmh, gear, is_driving, lat, lon, r50_rate, charging_state, power_w")


def load_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT {COLUMNS} FROM readings ORDER BY id")]


def _parse(s) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _valid_gps(lat, lon) -> bool:
    if lat is None or lon is None:
        return False
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def prepare(rows: list[dict]) -> list[dict]:
    """Clean rows: parse the car's own time, drop stale duplicates and rows without battery or odometer."""
    out: list[dict] = []
    last_car_ts = None
    for r in rows:
        car_ts = r.get("car_ts") or ""
        if car_ts and car_ts == last_car_ts:
            continue                                   # BYD returned the old snapshot again
        t = _parse(car_ts) or _parse(r.get("ts"))
        if t is None or (r.get("soc") or 0) <= 0 or (r.get("odometer_km") or 0) <= 0:
            continue                                       # missing, or BYD's empty snapshot (zeros)
        last_car_ts = car_ts or last_car_ts
        speed = r.get("speed_kmh")
        out.append({
            "t": t, "soc": float(r["soc"]), "odo": float(r["odometer_km"]),
            "speed": float(speed) if speed is not None else None,
            "driving": bool(r.get("is_driving")) or (speed or 0) > 0,
            "lat": r.get("lat") if _valid_gps(r.get("lat"), r.get("lon")) else None,
            "lon": r.get("lon") if _valid_gps(r.get("lat"), r.get("lon")) else None,
            "r50": r.get("r50_rate"),
            "power_w": r.get("power_w"),
            "charge": str(r.get("charging_state") or "").upper() == "CHARGING",   # BYD says so (unverified until seen)
        })
    return out


def segment(rows: list[dict]) -> list[tuple]:
    """Returns [("trip", [rows...]) | ("gap", before_row, after_row), ...] in time order. rows come from prepare()."""
    out: list[tuple] = []
    cur: list[dict] | None = None

    def close():
        nonlocal cur
        if cur is not None and len(cur) >= 2 and cur[-1]["odo"] - cur[0]["odo"] >= MIN_TRIP_KM:
            out.append(("trip", cur))
        cur = None

    prev = None
    for r in rows:
        if prev is None:
            prev = r
            continue
        gap_s = (r["t"] - prev["t"]).total_seconds()
        moved = r["odo"] - prev["odo"]
        if moved < 0 or (gap_s > 0 and moved / (gap_s / 3600.0) > MAX_PLAUSIBLE_KMH):
            # odometer glitch (backwards, or faster than any car): do not link across it
            close()
            prev = r
            continue
        if gap_s > GAP_MAX_S:
            close()
            if moved >= 1.0:                                 # the car moved while we were not told
                out.append(("gap", prev, r))
            cur = [r] if r["driving"] else None              # no anchor: the silence is not part of any trip
            prev = r
            continue
        active = prev["driving"] or r["driving"] or moved > 0
        if active:
            if cur is None:
                cur = [prev, r]
            else:
                cur.append(r)
        else:
            close()
        prev = r
    close()
    return out


def _consumption(pts_rows: list[dict], usable_kwh: float) -> tuple[float | None, float | None, str | None]:
    """(kWh/100 km, one-sigma, method) or (None, None, None) if the trip is too short to measure."""
    readings = [{"soc": r["soc"], "odometer_km": r["odo"]} for r in pts_rows]
    pts = range_engine._points(readings, ODO_RES_KM)
    ticks = [p for p in pts if p.is_tick]
    if len(ticks) >= 2:
        d = ticks[0].soc - ticks[-1].soc
        km = ticks[-1].odo - ticks[0].odo
        if d >= MIN_CONS_DROP and km >= MIN_CONS_KM:
            rel = math.sqrt((ticks[0].soc_sd**2 + ticks[-1].soc_sd**2) / d**2
                            + (ticks[0].odo_sd**2 + ticks[-1].odo_sd**2) / km**2)
            c = d / 100.0 * usable_kwh / km * 100.0
            return c, c * rel, "ticks"
    used = pts_rows[0]["soc"] - pts_rows[-1]["soc"]
    km = pts_rows[-1]["odo"] - pts_rows[0]["odo"]
    if used >= MIN_CONS_DROP and km >= MIN_CONS_KM:
        rel = math.sqrt(2 * range_engine.ENDPOINT_SD**2 / used**2 + 2 * (range_engine.ENDPOINT_SD * ODO_RES_KM) ** 2 / km**2)
        c = used / 100.0 * usable_kwh / km * 100.0
        return c, c * rel, "endpoints"
    return None, None, None


def _summary(kind: str, rows: list[dict], usable_kwh: float, *, ongoing: bool = False) -> dict:
    a, b = rows[0], rows[-1]
    km = b["odo"] - a["odo"]
    used = a["soc"] - b["soc"]
    start_t = a["t"]
    if kind == "trip":
        driving_rows = [r for r in rows if r["driving"]]
        if driving_rows and (driving_rows[0]["t"] - a["t"]).total_seconds() > 120:
            start_t = driving_rows[0]["t"]                  # the anchor was a parked reading long before the drive
    minutes = max(0.0, (b["t"] - start_t).total_seconds() / 60.0)
    quality = "ok"
    kwh = cons = pm = method = None
    if used < 0:
        # Battery went UP. Inside a trip the car is moving, so that is net REGEN (a long descent), not a charge.
        # Across an unreported gap it can only be a charge we were not told about.
        quality = "regen" if kind == "trip" else "charged"
    else:
        kwh = used / 100.0 * usable_kwh
        cons, pm, method = _consumption(rows, usable_kwh)
        if cons is None:
            quality = "short"
    if kind == "gap":
        quality = "unobserved" if quality != "charged" else "charged"
        if used >= 0:
            cons = used / 100.0 * usable_kwh / km * 100.0 if km > 0 and used >= MIN_CONS_DROP else None
            pm = None
            if cons is not None:
                rel = math.sqrt(2 * range_engine.ENDPOINT_SD**2 / used**2 + 2 * (range_engine.ENDPOINT_SD * ODO_RES_KM) ** 2 / km**2)
                pm = cons * rel
                method = "endpoints"
    speeds = [r["speed"] for r in rows if r["speed"] is not None]
    gps = [r for r in rows if r["lat"] is not None]
    return {
        "id": int(start_t.timestamp()), "observed": kind == "trip", "ongoing": ongoing,
        "start": start_t.isoformat(), "end": b["t"].isoformat(), "minutes": round(minutes, 1),
        "km": round(km, 1), "soc_start": a["soc"], "soc_end": b["soc"],
        "used_pct": None if used < 0 else round(used, 1),
        "kwh": None if kwh is None else round(kwh, 2),
        "kwh_per_100": None if cons is None else round(cons, 1),
        "kwh_per_100_pm": None if pm is None else round(pm, 1),
        "method": method, "quality": quality,
        "avg_kmh": round(km / (minutes / 60.0), 0) if minutes > 1 and kind == "trip" else None,
        "max_kmh": max(speeds) if speeds and kind == "trip" else None,
        "byd_kwh_per_100_end": b["r50"] if kind == "trip" else None,
        "has_route": kind == "trip" and len(gps) >= 2,
    }


def regen_stats(rows: list[dict], usable_kwh: float, used_pct: float | None) -> dict | None:
    """
    Energy recovered by regen vs energy drawn, from BYD's instantaneous battery power (positive = into the battery,
    negative = out). Evidence for that sign: with the car on but standing still it read -0.72 kW, and a standing car cannot
    regenerate, so negative is power drawn. (On the owner's first trip the samples netted 0.54 kWh against 0.50 kWh from the
    battery percent, but that is WEAK evidence: one 30 s sample at -60 kW was already 0.50 kWh.) A sign flipped by mistake is
    only caught on trips long enough for the check to be able to fail; see "check". The power is a SPOT reading about every
    30 s, so each total carries a one-sigma from the spread of the samples and needs MIN_REGEN_SAMPLES or more.
    """
    drv = [i for i, r in enumerate(rows) if r["driving"]]
    if not drv:
        return None
    w = [r for r in rows[drv[0]: drv[-1] + 1] if r.get("power_w") is not None]
    n = len(w)
    if n < MIN_REGEN_SAMPLES:
        return None
    dts = [min((w[i + 1]["t"] - w[i]["t"]).total_seconds(), SAMPLE_CAP_S) for i in range(n - 1)]
    dts.append(dts[-1])
    reg = [max(r["power_w"], 0.0) * d for r, d in zip(w, dts)]                  # W*s
    use = [max(-r["power_w"], 0.0) * d for r, d in zip(w, dts)]
    net = [u - g for u, g in zip(use, reg)]

    def total_and_se(y):
        mean = sum(y) / n
        var = sum((v - mean) ** 2 for v in y) / (n - 1)
        return sum(y) / 3.6e6, math.sqrt(n * var) / 3.6e6

    regen, regen_se = total_and_se(reg)
    regen_se *= REGEN_SE_INFLATION
    used, used_se = total_and_se(use)
    net_kwh, net_se = total_and_se(net)
    caught = sum(1 for r in w if r["power_w"] > 0)                              # samples that landed in a regen burst
    out = {
        "samples": n, "regen_samples": caught, "regen_kwh": round(regen, 2),
        # No burst caught is NOT "zero regen": short bursts fall between 30 s samples. Say "none seen", with no false +/-.
        "regen_pm": round(regen_se, 2) if caught else None,
        "used_kwh": round(used, 2), "used_pm": round(used_se, 2),
        "net_kwh": round(net_kwh, 2), "net_pm": round(net_se, 2),
        "regen_share_pct": round(regen / used * 100.0, 0) if used > 0 and caught else None,
        "soc_kwh": None, "check": None,
    }
    if used_pct is not None:                                                    # cross-check against the battery percent
        soc_kwh = used_pct / 100.0 * usable_kwh
        tol = 2.0 * math.sqrt(net_se**2 + (ENERGY_SD_PCT / 100.0 * usable_kwh) ** 2)
        out["soc_kwh"] = round(soc_kwh, 2)
        # Only judge when the check CAN fail: if the allowed gap is as big as the energy itself, agreement means nothing.
        if tol < abs(soc_kwh):
            out["check"] = "consistent" if abs(net_kwh - soc_kwh) <= tol else "inconsistent"
    return out


def analyze(rows: list[dict], usable_kwh: float) -> list[dict]:
    """All trips and unobserved drives, newest first, with summary stats. rows: raw DB-style dicts."""
    prepared = prepare(rows)
    last_row = prepared[-1] if prepared else None
    out = []
    for item in segment(prepared):
        if item[0] == "trip":
            rows_ = item[1]
            out.append(_summary("trip", rows_, usable_kwh, ongoing=rows_[-1] is last_row and rows_[-1]["driving"]))
        else:
            out.append(_summary("gap", [item[1], item[2]], usable_kwh))
    out.sort(key=lambda s: s["start"], reverse=True)
    return out


def totals(trips: list[dict]) -> dict:
    """Aggregate consumption. Per-trip rounding errors are independent, so the sum is far more reliable than
    any short trip: that is why it includes trips that are individually 'too short to measure'."""
    usable = [t for t in trips if t["kwh"] is not None and t["km"] > 0]
    km = sum(t["km"] for t in usable)
    kwh = sum(t["kwh"] for t in usable)
    return {
        "trips": len(trips), "km": round(sum(t["km"] for t in trips), 1),
        "kwh": round(kwh, 1), "kwh_per_100": round(kwh / km * 100.0, 1) if km >= MIN_CONS_KM else None,
        "measured_km": round(km, 1),
    }


def detail(rows: list[dict], trip_id: int, usable_kwh: float, *, include_location: bool = True) -> dict | None:
    """One observed trip with its route and speed series; None if the id is unknown or unobserved."""
    prepared = prepare(rows)
    last_row = prepared[-1] if prepared else None
    for item in segment(prepared):
        if item[0] != "trip":
            continue
        rows_ = item[1]
        s = _summary("trip", rows_, usable_kwh, ongoing=rows_[-1] is last_row and rows_[-1]["driving"])
        if s["id"] != trip_id:
            continue
        t0 = rows_[0]["t"]
        s["speed_series"] = [{"s": round((r["t"] - t0).total_seconds()), "kmh": r["speed"]}
                             for r in rows_ if r["speed"] is not None]
        s["regen"] = regen_stats(rows_, usable_kwh, s["used_pct"])
        drv = [i for i, r in enumerate(rows_) if r["driving"]]
        s["power_series"] = ([{"s": round((r["t"] - t0).total_seconds()), "kw": round(r["power_w"] / 1000.0, 1)}
                              for r in rows_[drv[0]: drv[-1] + 1] if r.get("power_w") is not None] if drv else [])
        s["points"] = ([{"lat": round(r["lat"], 5), "lon": round(r["lon"], 5), "kmh": r["speed"]}
                        for r in rows_ if r["lat"] is not None] if include_location else [])
        if not include_location:
            s["has_route"] = False
        return s
    return None
