"""
Learned usable battery capacity ("SOH"), with an uncertainty and a confidence. Pure functions plus one loader.

Idea: BYD reports its last-50-km consumption c (kWh/100 km). That is MEASURED ENERGY and the owner trusts it. Our own
driving gives km per 1% of battery, k. Over the same driving, energy = c x km / 100 and the battery used = d %, so

    usable kWh (= 100% of the battery) = c x km / d        (= c x k)

Care taken so the numbers can be trusted:
* Windows run from one battery TICK to another. The percent difference d is then an exact integer, and it does not
  depend on whether BYD rounds or floors its displayed percent (that offset cancels).
* Windows are 30 to 50 km, as close to 50 as possible, so they match BYD's own "last 50 km" window.
* Only the middle of the battery is used (15-92%): the display is least linear near the ends.
* Windows never overlap, so the samples are independent and the uncertainty is not understated.
* Combined with a prior (your configured battery) in log space, weighted by each sample's uncertainty.

This measures USABLE capacity against the nominal pack. A brand-new pack may itself deliver less than nominal, so the
percentage is not by itself a measure of degradation: the TREND over months is. It is an estimate, not a diagnostic.
"""
from __future__ import annotations

import math

import range_engine
import trips

WIN_MIN_KM = 30.0
WIN_MAX_KM = 50.0
MIN_DROP = 5               # battery percent points in a window
SOC_LO, SOC_HI = 15.0, 92.0
PRIOR_SD_REL = 0.08        # how sure we are of the configured capacity before any data
MISMATCH_REL = 0.12        # relative consumption difference between BYD's window and ours, per unit of non-overlap
MISMATCH_FLOOR = 0.01      # BYD update lag, tick position
PLAUSIBLE = (0.5, 1.6)     # a sample outside this share of nominal is bad data, not a battery
CONFIDENCE_ZERO_SD = 0.08  # a relative sd at or above this scores 0% confidence
SYSTEMATIC_REL = 0.03      # floor for errors no amount of data removes: how accurate BYD's energy figure really is, and
                           # how linear its battery percent is. UNVALIDATED against a real car, so it is not optimistic.


def _readings(rows: list[dict]) -> list[dict]:
    return [{"soc": r["soc"], "odometer_km": r["odo"], "r50": r["r50"], "t": r["t"]} for r in trips.prepare(rows)]


def samples(rows: list[dict], nominal_kwh: float) -> list[dict]:
    """Independent capacity samples from DB-style rows (oldest first)."""
    out: list[dict] = []
    for run in range_engine.split_runs(_readings(rows)):
        if len(run) < 2:
            continue
        ticks = [p for p in range_engine._points(run, range_engine.ODO_RESOLUTION_KM) if p.is_tick]
        i = 0
        while i < len(ticks) - 1:
            s = ticks[i]
            best = None
            for j in range(i + 1, len(ticks)):
                e = ticks[j]
                km = e.odo - s.odo
                if km > WIN_MAX_KM:
                    break
                drop = round(s.soc - e.soc)               # both are level + 0.5, so the difference is an integer
                if (km >= WIN_MIN_KM and drop >= MIN_DROP and e.r50 and 0 < e.r50 < 60
                        and s.soc - 0.5 <= SOC_HI and e.soc - 0.5 >= SOC_LO):
                    best = (j, km, drop, e)
            if best is None:
                i += 1
                continue
            j, km, drop, e = best
            usable = e.r50 * km / drop
            if not (PLAUSIBLE[0] * nominal_kwh <= usable <= PLAUSIBLE[1] * nominal_kwh):
                i += 1
                continue
            rel = math.sqrt(
                ((s.odo_sd**2 + e.odo_sd**2) / km**2)                              # odometer rounding + tick position
                + ((0.1 / math.sqrt(12)) / e.r50) ** 2                              # BYD reports 0.1 kWh/100 km
                + (MISMATCH_REL * (WIN_MAX_KM - km) / WIN_MAX_KM + MISMATCH_FLOOR) ** 2
            )
            out.append({"when": e.t.isoformat() if e.t is not None else None, "km": round(km, 1), "drop_pct": drop,
                        "byd_kwh_per_100": e.r50, "usable_kwh": round(usable, 2), "sd_rel": rel,
                        "sd_kwh": round(usable * rel, 2)})
            i = j
    return out


def posterior(sample_list: list[dict], prior_kwh: float, nominal_kwh: float, prior_sd_rel: float = PRIOR_SD_REL) -> dict:
    """Combine the samples with the prior (log space, inverse-variance weights)."""
    w0 = 1.0 / prior_sd_rel**2
    num = w0 * math.log(prior_kwh)
    den = w0
    for s in sample_list:
        w = 1.0 / s["sd_rel"] ** 2
        num += w * math.log(s["usable_kwh"])
        den += w
    mean = math.exp(num / den)
    sd_rel = math.sqrt(1.0 / den + (SYSTEMATIC_REL**2 if sample_list else 0.0))
    return {
        "usable_kwh": mean, "sd_kwh": mean * sd_rel, "sd_rel": sd_rel,
        "pct_of_nominal": mean / nominal_kwh * 100.0,
        "confidence_pct": int(round(100 * max(0.0, 1 - sd_rel / CONFIDENCE_ZERO_SD))),
        "n_samples": len(sample_list), "km_total": round(sum(s["km"] for s in sample_list), 1),
        "prior_only": not sample_list, "prior_kwh": prior_kwh, "nominal_kwh": nominal_kwh,
    }


def learn(rows: list[dict], nominal_kwh: float, prior_kwh: float) -> dict:
    """Samples plus the combined estimate, for the API and for the range estimate."""
    sm = samples(rows, nominal_kwh)
    post = posterior(sm, prior_kwh, nominal_kwh)
    post["samples"] = sm
    return post


_cache: dict = {}          # database path -> (key, value); bounded, so a shared server cannot grow it without limit
_CACHE_MAX = 64


def learn_from_db(conn, nominal_kwh: float, prior_kwh: float) -> dict:
    """learn() on the whole history, cached until a new row arrives (status is polled every few seconds)."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    count, last = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM readings").fetchone()
    key = (path, count, last, round(nominal_kwh, 4), round(prior_kwh, 4))
    hit = _cache.get(path)
    if hit is None or hit[0] != key:
        if len(_cache) >= _CACHE_MAX and path not in _cache:
            _cache.pop(next(iter(_cache)))
        hit = (key, learn(trips.load_rows(conn), nominal_kwh, prior_kwh))
        _cache[path] = hit
    return hit[1]
