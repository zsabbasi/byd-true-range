"""
Charging sessions from the stored readings. Pure functions, standard library only. Used by server.py (/api/charging).

What counts as charging. Two independent kinds of evidence, either is enough:
* BYD says CHARGING (verified on the owner's first real charge: the state reads CHARGING and the battery power reads a steady
  positive ~6.3 kW) while the car is stopped. Two such readings make a session, so a charge shows up within a minute.
* The car is stopped (speed zero AND odometer unchanged) and the battery percent RISES. Physics, not a label, so it still
  works if BYD's label is ever missing. A session found this way needs a 2% gain, to ignore display jitter.
A rise while the car is MOVING is regen, never charging.

A session is a run of such readings with no silence longer than IDLE_GAP_S. Trailing flat readings (full, still plugged in)
are trimmed. If the car is silent and the battery rose across the silence, that is reported as a charge that was NOT WATCHED
(gain and energy only). It is never hidden.

Energy into the BATTERY, two ways (the larger source of error is named so you can judge):
* From BYD's live battery power, integrated over time (trapezoid between readings, plus half a poll interval at each unseen
  edge). Charging power is steady, so 30 s to 5 min samples capture it well. This is the primary figure when there are two or
  more power readings. It is battery-side: it excludes charger and cable losses, which we cannot see.
* From the battery percent: gain % x usable capacity, +/- 0.41% of the pack. Used when there is no power, and as a cross-check
  ("check") on sessions that gain 5% or more.
Average power is the time-weighted mean of the readings (or, without power, from the moments the percent ticked up). AC vs DC
is a guess from that power and labelled one. Cost needs YOUR tariff; nothing is assumed.
"""
from __future__ import annotations

import math
import statistics

import range_engine
import trips

IDLE_GAP_S = 2700          # 45 min: longest silence allowed inside one session
MIN_GAIN = 2               # percent points a battery-only session needs (display jitter below this)
MIN_STATE_ROWS = 2         # readings saying CHARGING that make a session on BYD's word alone
MIN_TICK_SPAN = 2          # percent points between the first and last tick for a tick-based power figure
POWER_GAP_S = 900          # do not integrate power across a silence longer than this (a pause, not charging)
MIN_CHECK_GAIN = 5         # percent points before the power-vs-battery cross-check is meaningful
DC_KW = 25.0               # above this average power we call it DC (a guess, labelled as one)
ENERGY_SD_PCT = range_engine.ENDPOINT_SD * math.sqrt(2)     # one session's percent uncertainty (two endpoints)


def _still(a: dict, b: dict) -> bool:
    return abs(b["odo"] - a["odo"]) <= 0.5


def _moving(r: dict) -> bool:
    """Speed, not the gear: a car can sit plugged in with its gear ON, but it cannot regenerate while stopped."""
    return (r.get("speed") or 0) > 0


def segment(rows: list[dict]) -> list[tuple]:
    """
    [("session", rows, rises, next_row) | ("gap", before, after), ...] in time order. rows come from trips.prepare().
    next_row is the first reading after the session (or None while it is still going): charging had stopped by then.
    """
    out: list[tuple] = []
    cur: dict | None = None

    def close():
        nonlocal cur
        if cur is not None:
            kept = cur["rows"][: cur["last"] + 1]                      # drop trailing flat readings
            nstate = sum(1 for r in kept if r["charge"])
            gain = kept[-1]["soc"] - kept[0]["soc"]
            if (cur["rises"] and gain >= MIN_GAIN) or nstate >= MIN_STATE_ROWS:
                nxt = cur["rows"][cur["last"] + 1] if cur["last"] + 1 < len(cur["rows"]) else None
                out.append(("session", kept, cur["rises"], nxt))
        cur = None

    prev = None
    for b in rows:
        a, prev = prev, b
        if a is None:
            continue
        gap = (b["t"] - a["t"]).total_seconds()
        rise = b["soc"] - a["soc"] >= 1
        if not _still(a, b) or _moving(a) or _moving(b) or a["soc"] - b["soc"] >= 2:
            # It drove (the odometer is whole km, so equal odometers are NOT proof of standing still: speed decides),
            # or the battery clearly fell. A battery rise while moving is REGEN, never a charge.
            close()
        elif gap > IDLE_GAP_S:
            close()
            if b["soc"] - a["soc"] >= MIN_GAIN:
                out.append(("gap", a, b))                              # charged while we were not told
        elif rise or b["charge"]:
            if cur is None:
                cur = {"rows": [a], "rises": [], "last": 0}
            cur["rows"].append(b)
            if rise:
                cur["rises"].append((a, b))
            cur["last"] = len(cur["rows"]) - 1
        elif cur is not None:
            cur["rows"].append(b)                                      # flat reading inside the session
    close()
    return out


def _power_from_ticks(rises: list[tuple], usable_kwh: float):
    """(kW, one-sigma kW) from the first to the last battery tick, or (None, None). Used when there is no power reading."""
    if len(rises) < 2:
        return None, None
    (a1, b1), (a2, b2) = rises[0], rises[-1]
    span = b2["soc"] - b1["soc"]                                       # levels at the two ticks: an exact integer
    t1 = (a1["t"] + (b1["t"] - a1["t"]) / 2).timestamp()
    t2 = (a2["t"] + (b2["t"] - a2["t"]) / 2).timestamp()
    dt = t2 - t1
    if span < MIN_TICK_SPAN or dt <= 0:
        return None, None
    kw = span / 100.0 * usable_kwh / (dt / 3600.0)
    sd_t = math.sqrt(((b1["t"] - a1["t"]).total_seconds()) ** 2 + ((b2["t"] - a2["t"]).total_seconds()) ** 2) / math.sqrt(12)
    return kw, kw * (sd_t / dt)


def _energy_from_power(srows: list[dict], nxt: dict | None):
    """
    Integrate BYD's live battery power: (kWh, one-sigma kWh, mean kW, one-sigma kW) or None with under two readings.
    Trapezoid between consecutive CHARGING readings; the unseen start (after the anchor) and end (before the next reading)
    each add half a poll interval at the nearest power, with their uncertainty.
    """
    idx = [i for i, r in enumerate(srows) if r["charge"] and (r.get("power_w") or 0) > 0]
    if len(idx) < 2:
        return None
    pts = [(srows[i]["t"], float(srows[i]["power_w"])) for i in idx]
    e_ws = span = sq = 0.0
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        dt = (t1 - t0).total_seconds()
        if dt <= 0 or dt > POWER_GAP_S:
            continue                                                   # a pause, not charging
        e_ws += 0.5 * (p0 + p1) * dt
        span += dt
        sq += dt * dt
    if span <= 0:
        return None
    powers = [p for _, p in pts]
    sd_p = statistics.pstdev(powers) if len(powers) > 1 else 0.0
    extra = var_edge = 0.0
    if idx[0] > 0:                                                     # charging began somewhere after the anchor reading
        g = (srows[idx[0]]["t"] - srows[idx[0] - 1]["t"]).total_seconds()
        extra += powers[0] * g / 2.0
        var_edge += (powers[0] * g / math.sqrt(12)) ** 2
    if nxt is not None:                                                # and ended somewhere before the next reading
        g = (nxt["t"] - srows[idx[-1]]["t"]).total_seconds()
        extra += powers[-1] * g / 2.0
        var_edge += (powers[-1] * g / math.sqrt(12)) ** 2
    energy = (e_ws + extra) / 3.6e6
    sd = math.sqrt((sd_p * math.sqrt(sq)) ** 2 + var_edge) / 3.6e6
    return energy, sd, e_ws / span / 1000.0, sd_p / math.sqrt(len(powers)) / 1000.0


def _cost(energy_kwh: float | None, tariff: float | None, loss_pct: float):
    if energy_kwh is None or tariff is None:
        return None, None
    wall = energy_kwh / (1.0 - loss_pct / 100.0)
    return wall, wall * tariff


def _summary(kind: str, a: dict, b: dict, rows: list[dict] | None, rises: list, usable_kwh: float,
             tariff, loss_pct, ongoing: bool, nxt: dict | None = None) -> dict:
    gained = b["soc"] - a["soc"]
    energy_soc = gained / 100.0 * usable_kwh
    sd_soc = ENERGY_SD_PCT / 100.0 * usable_kwh
    energy, sd_e, method = energy_soc, sd_soc, "battery %"
    kw = pm = None
    kw_method = None
    check = None
    minutes = None
    sampled = None
    if kind == "session":
        first_charge = next((r for r in rows if r["charge"]), None)
        start_t = first_charge["t"] if first_charge is not None else a["t"]
        minutes = (b["t"] - start_t).total_seconds() / 60.0
        pw = _energy_from_power(rows, nxt)
        if pw is not None:
            energy, sd_e, method = pw[0], pw[1], "BYD power"
            kw, pm, kw_method = pw[2], pw[3], "power"
            if gained >= MIN_CHECK_GAIN:
                tol = 2.0 * math.sqrt(sd_e**2 + sd_soc**2)
                if tol < energy_soc:                                   # only judge when the check can fail
                    check = "consistent" if abs(energy - energy_soc) <= tol else "inconsistent"
        else:
            kw, pm = _power_from_ticks(rises, usable_kwh)
            kw_method = "ticks" if kw is not None else None
        pos = [r["power_w"] / 1000.0 for r in rows if r.get("power_w") and r["power_w"] > 0]
        sampled = sum(pos) / len(pos) if len(pos) >= 2 else None
    wall, cost = _cost(energy, tariff, loss_pct)
    return {
        "id": int(a["t"].timestamp()), "observed": kind == "session", "ongoing": ongoing,
        "start": a["t"].isoformat(), "end": b["t"].isoformat(),
        "minutes": None if minutes is None else round(minutes, 0),
        "soc_start": a["soc"], "soc_end": b["soc"], "gained_pct": gained,
        "energy_kwh": round(energy, 2), "energy_pm_kwh": round(sd_e, 2), "energy_method": method,
        "energy_soc_kwh": round(energy_soc, 2), "check": check,
        "avg_kw": None if kw is None else round(kw, 1), "avg_kw_pm": None if pm is None else round(pm, 1),
        "kw_method": kw_method,
        "type_guess": None if kw is None else ("DC" if kw >= DC_KW else "AC"),
        "sampled_kw": None if sampled is None else round(sampled, 1),
        "wall_kwh": None if wall is None else round(wall, 2), "cost": None if cost is None else round(cost, 2),
        "quality": "ok" if kind == "session" else "not watched",
    }


def _is_ongoing(srows: list[dict], rises: list, last: dict | None) -> bool:
    return srows[-1] is last and (srows[-1]["charge"] or bool(rises and rises[-1][1] is last))


def analyze(rows: list[dict], usable_kwh: float, *, tariff: float | None = None, loss_pct: float = 0.0) -> list[dict]:
    """All charging sessions (newest first). rows: raw DB-style dicts, oldest first."""
    prepared = trips.prepare(rows)
    last = prepared[-1] if prepared else None
    out = []
    for item in segment(prepared):
        if item[0] == "session":
            _, srows, rises, nxt = item
            out.append(_summary("session", srows[0], srows[-1], srows, rises, usable_kwh, tariff, loss_pct,
                                _is_ongoing(srows, rises, last), nxt))
        else:
            out.append(_summary("gap", item[1], item[2], None, [], usable_kwh, tariff, loss_pct, False))
    out.sort(key=lambda s: s["start"], reverse=True)
    return out


def totals(sessions: list[dict]) -> dict:
    energy = sum(s["energy_kwh"] for s in sessions)
    costs = [s["cost"] for s in sessions if s["cost"] is not None]
    walls = [s["wall_kwh"] for s in sessions if s["wall_kwh"] is not None]
    return {
        "sessions": len(sessions), "watched": sum(1 for s in sessions if s["observed"]),
        "ongoing": any(s["ongoing"] for s in sessions),
        "energy_kwh": round(energy, 1), "wall_kwh": round(sum(walls), 1) if walls else None,
        "cost": round(sum(costs), 2) if costs else None,
    }


def detail(rows: list[dict], session_id: int, usable_kwh: float, *, tariff: float | None = None,
           loss_pct: float = 0.0) -> dict | None:
    """One watched session with its battery curve; None if the id is unknown or the charge was not watched."""
    prepared = trips.prepare(rows)
    last = prepared[-1] if prepared else None
    for item in segment(prepared):
        if item[0] != "session":
            continue
        _, srows, rises, nxt = item
        s = _summary("session", srows[0], srows[-1], srows, rises, usable_kwh, tariff, loss_pct,
                     _is_ongoing(srows, rises, last), nxt)
        if s["id"] != session_id:
            continue
        t0 = srows[0]["t"]
        s["curve"] = [{"s": round((r["t"] - t0).total_seconds()), "soc": r["soc"]} for r in srows]
        s["ticks"] = [{"s": round((a["t"] + (b["t"] - a["t"]) / 2 - t0).total_seconds()), "soc": b["soc"] - 0.5}
                      for a, b in rises]
        s["power_samples"] = [{"s": round((r["t"] - t0).total_seconds()), "kw": round(r["power_w"] / 1000.0, 1)}
                              for r in srows if r.get("power_w") and r["power_w"] > 0]
        return s
    return None
