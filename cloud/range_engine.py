"""
True-range estimator from BYD cloud snapshots, reported as a band with a trust score.

Design (see docs/PROJECT_PLAN.md sections 6c/6d):

* SOC from BYD is a WHOLE percent, so per-step deltas are useless. We use *runs*: stretches of
  readings with no SOC rise (a rise means charging/regen) and a monotonic odometer. Within a run only the
  two endpoints matter -- the SOC drops telescope to ``soc_start - soc_end`` -- so whole-percent rounding
  contributes ~+/-1% to the run, not per step. A longer window therefore means a tighter estimate.

* Capacity cancels out of range:

      km_per_pct = km_in_window / soc_used_in_window
      range_km   = soc_now * km_per_pct

  so the range band does not depend on battery capacity / SOH. Only the kWh/100 km figure does.

* Uncertainty (relative, independent terms added in quadrature):
    - measurement : rounding of the window's SOC drop and of its odometer distance (both shrink as the
                    window grows)
    - variability : spread of km-per-% between chunks of the window (speed, temperature, A/C); shrunk
                    towards a wide prior when there are few chunks, never below a floor
    - soc_now     : rounding of today's SOC (matters at low SOC)
  The band is an 80% prediction interval. ``trust_pct`` is a heuristic tightness score derived from the
  band width -- it is NOT a probability.

* Below the minimum data we return ``status="insufficient"`` and no range; we never invent a number.

Pure functions, standard library only, no I/O -- unit-testable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

# BYD Atto 3 usable battery (kWh). Extended Range = 60.48, Standard Range = 49.92. Only affects the
# kWh/100 km figure, not the range. Real usable capacity is lower than nominal; make it configurable.
DEFAULT_USABLE_KWH = 49.92      # Atto 3 Standard Range, the usual one in Pakistan; the app says so when the owner has not set it

Z80 = 1.2816                      # two-sided 80% interval
ENDPOINT_SD = 1 / math.sqrt(12)   # std of whole-percent rounding error, per endpoint
RUN_DROP_SD = ENDPOINT_SD * math.sqrt(2)  # std of a run's SOC drop (two endpoints)

ODO_RESOLUTION_KM = 1.0           # BYD reports the odometer in whole km (seen in the first real probe)

TRANSITION_SOC_SD = 0.10          # std of TRUE soc at the instant the displayed % ticks over (assumes the display
                                  # rounds to nearest; if it floors, there is a constant 0.5% offset, see PROJECT_PLAN)
MIN_DROP_TRANSITION = 3.0         # min SOC drop (percent points) between the window endpoints

PRIOR_CV = 0.20                   # driving-variability assumed before there is data
PRIOR_WEIGHT = 3.0                # how many chunks the prior is worth
CV_FLOOR = 0.05                   # real-world variability never reduces below this
TRUST_ZERO_HALF_WIDTH = 0.40      # band half-width (fraction of range) that scores 0% trust


@dataclass(frozen=True)
class RangeResult:
    status: str                       # "ok" | "insufficient"
    basis: str                        # "window" | "extended" | "insufficient"
    true_range_km: float | None
    low_km: float | None              # 80% band
    high_km: float | None
    trust_pct: int | None             # heuristic tightness score 0-100
    km_per_pct: float | None
    consumption_kwh_per_100km: float | None
    window_km: float                  # distance the estimate is based on
    soc_drop_pct: float               # SOC used over that distance
    n_runs: int
    n_chunks: int
    soc: float | None
    remaining_kwh: float | None
    est_rel_sd: float | None = None   # relative error of the ESTIMATE itself (shrinks with data/better inputs)
    cv: float | None = None           # driving variability, shared by every estimator (it is about the future)
    parts: dict | None = None         # for a combined result: the two inputs and their weights


def _insufficient(soc_now: float | None, usable_kwh: float) -> RangeResult:
    return RangeResult(
        status="insufficient", basis="insufficient", true_range_km=None, low_km=None, high_km=None,
        trust_pct=None, km_per_pct=None, consumption_kwh_per_100km=None, window_km=0.0,
        soc_drop_pct=0.0, n_runs=0, n_chunks=0, soc=soc_now,
        remaining_kwh=(soc_now / 100.0 * usable_kwh) if soc_now is not None else None,
    )


def split_runs(readings: list[dict], charge_rise: float = 2.0) -> list[list[dict]]:
    """
    readings: oldest-first dicts with ``soc`` and ``odometer_km``. Returns runs (oldest-first lists).
    A new run starts when SOC rises by >= charge_rise (charging / big regen) or the odometer goes backwards
    (glitch). A 1% rise is display jitter and is tolerated. Readings with missing values are dropped.
    """
    runs: list[list[dict]] = []
    cur: list[dict] = []
    for r in readings:
        soc, odo = r.get("soc"), r.get("odometer_km")
        if soc is None or odo is None:
            continue
        if cur:
            prev = cur[-1]
            if soc - prev["soc"] >= charge_rise or odo < prev["odometer_km"]:
                runs.append(cur)
                cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    return runs


def _tail_within(run: list[dict], need_km: float) -> list[dict]:
    """Newest portion of a run spanning at most need_km of odometer travel."""
    end_odo = run[-1]["odometer_km"]
    for i, r in enumerate(run):
        if end_odo - r["odometer_km"] <= need_km:
            return run[i:]
    return run[-1:]


def _gather(runs: list[list[dict]], target_km: float) -> list[list[dict]]:
    """Newest-first tails of runs until target_km of driving is covered."""
    picked: list[list[dict]] = []
    km = 0.0
    for run in reversed(runs):
        if len(run) < 2:
            continue
        tail = _tail_within(run, target_km - km)
        span = tail[-1]["odometer_km"] - tail[0]["odometer_km"]
        if len(tail) < 2 or span <= 0:
            continue
        picked.append(tail)
        km += span
        if km >= target_km:
            break
    return picked


def _chunks(tails: list[list[dict]], chunk_drop: float) -> list[float]:
    """km-per-% for anchored chunks (>= chunk_drop % SOC each) inside the picked tails."""
    out: list[float] = []
    for tail in tails:
        anchor = tail[0]
        for r in tail[1:]:
            drop = anchor["soc"] - r["soc"]
            if drop >= chunk_drop:
                km = r["odometer_km"] - anchor["odometer_km"]
                if km > 0:
                    out.append(km / drop)
                anchor = r
    return out


def _cv(values: list[float]) -> tuple[float, int]:
    """Variability (coefficient of variation), shrunk towards the prior, floored."""
    n = len(values)
    if n >= 2:
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        sample_cv = math.sqrt(var) / mean if mean > 0 else PRIOR_CV
    else:
        sample_cv = PRIOR_CV
    eff = (n * sample_cv + PRIOR_WEIGHT * PRIOR_CV) / (n + PRIOR_WEIGHT) if n >= 2 else PRIOR_CV
    return max(eff, CV_FLOOR), n


def _estimate_legacy(
    readings: list[dict],
    soc_now: float | None = None,
    *,
    usable_kwh: float = DEFAULT_USABLE_KWH,
    window_km: float = 50.0,
    max_km: float = 150.0,
    min_km: float = 8.0,
    min_soc_drop: float = 4.0,
    chunk_drop: float = 3.0,
    odo_resolution_km: float = ODO_RESOLUTION_KM,
) -> RangeResult:
    """
    readings: OLDEST-first dicts {soc, odometer_km, ...}. soc_now defaults to the newest reading's SOC.
    window_km: preferred distance of recent driving. If that is not enough (distance < min_km or SOC drop <
    min_soc_drop) the window is extended up to max_km before giving up.
    """
    valid = [r for r in readings if r.get("soc") is not None and r.get("odometer_km") is not None]
    if soc_now is None and valid:
        soc_now = valid[-1]["soc"]
    if soc_now is None or not valid:
        return _insufficient(soc_now, usable_kwh)

    runs = split_runs(valid)

    basis = "window"
    tails = _gather(runs, window_km)
    for attempt in ("window", "extended"):
        km = sum(t[-1]["odometer_km"] - t[0]["odometer_km"] for t in tails)
        drop = sum(t[0]["soc"] - t[-1]["soc"] for t in tails)
        if km >= min_km and drop >= min_soc_drop:
            basis = attempt
            break
        if attempt == "window":
            tails = _gather(runs, max_km)
    else:
        return _insufficient(soc_now, usable_kwh)

    km_per_pct = km / drop
    rng = soc_now * km_per_pct

    rel_soc_drop = (RUN_DROP_SD * math.sqrt(len(tails))) / drop
    rel_odo = (ENDPOINT_SD * odo_resolution_km * math.sqrt(2) * math.sqrt(len(tails))) / km
    rel_meas = math.sqrt(rel_soc_drop**2 + rel_odo**2)
    # Variability is learned from ALL history up to max_km (more chunks -> better estimate), even though
    # the mean uses only the recent window. This is what lets the band keep narrowing past window_km.
    chunk_vals = _chunks(_gather(runs, max_km), chunk_drop)
    cv, n_chunks = _cv(chunk_vals)
    rel_soc = ENDPOINT_SD / soc_now if soc_now > 0 else 1.0
    rel_sd = math.sqrt(rel_meas**2 + cv**2 + rel_soc**2)

    half = Z80 * rel_sd
    low = max(0.0, rng * (1 - half))
    high = rng * (1 + half)
    trust = int(round(100 * max(0.0, 1 - half / TRUST_ZERO_HALF_WIDTH)))

    return RangeResult(
        status="ok", basis=basis, true_range_km=rng, low_km=low, high_km=high, trust_pct=trust,
        km_per_pct=km_per_pct, consumption_kwh_per_100km=usable_kwh / km_per_pct,
        window_km=km, soc_drop_pct=drop, n_runs=len(tails), n_chunks=n_chunks, soc=soc_now,
        remaining_kwh=soc_now / 100.0 * usable_kwh,
        est_rel_sd=math.sqrt(rel_meas**2 + rel_soc**2), cv=cv,
    )


@dataclass(frozen=True)
class _Pt:
    odo: float
    soc: float        # best estimate of the TRUE soc at this point
    soc_sd: float
    odo_sd: float
    is_tick: bool     # True when this is the moment the displayed percent changed
    r50: float | None = None   # BYD's last-50-km consumption (kWh/100 km) at the reading that showed this tick
    t: object = None           # the car's clock at that reading


def _points(run: list[dict], odo_res: float) -> list[_Pt]:
    """
    Window endpoints inside one run. The first reading is a plain reading (true soc = shown +/-0.5). Every time the
    shown percent DROPS between two readings, the true soc crossed a rounding boundary somewhere in that gap, so at
    that moment true soc = new_level + 0.5 exactly; we place it mid-gap and carry the gap as position uncertainty.
    """
    odo_sd0 = ENDPOINT_SD * odo_res
    first = run[0]
    pts = [_Pt(first["odometer_km"], first["soc"], ENDPOINT_SD, odo_sd0, False)]
    for prev, cur in zip(run, run[1:]):
        if cur["soc"] < prev["soc"]:
            gap = cur["odometer_km"] - prev["odometer_km"]
            odo = (cur["odometer_km"] + prev["odometer_km"]) / 2
            pts.append(_Pt(odo, cur["soc"] + 0.5, TRANSITION_SOC_SD,
                           math.sqrt(gap**2 / 12 + odo_sd0**2), True, cur.get("r50"), cur.get("t")))
    return pts


def _estimate_transitions(run, all_runs, soc_now, *, usable_kwh, window_km, max_km, min_km, min_drop,
                          chunk_drop, odo_res):
    """
    Window ends at the LATEST tick (exact true soc), and the charge since that tick is tracked from the distance
    driven (fractional soc). This removes the sawtooth of the simple method, where the estimate crept up between
    ticks and dropped a whole percent at each one. Returns None if the run has no usable window yet.
    """
    if len(run) < 2:
        return None
    pts = _points(run, odo_res)
    tick_idx = [i for i, p in enumerate(pts) if p.is_tick]
    if not tick_idx:
        return None
    e = pts[tick_idx[-1]]

    best, basis = None, "window"
    for limit, name in ((window_km, "window"), (max_km, "extended")):
        for p in pts[: tick_idx[-1]]:
            km = e.odo - p.odo
            d = p.soc - e.soc
            if km < min_km or km > limit or d < min_drop:
                continue
            rel = math.sqrt((p.soc_sd**2 + e.soc_sd**2) / d**2 + (p.odo_sd**2 + e.odo_sd**2) / km**2)
            if best is None or rel < best[0]:
                best = (rel, km, d)
        if best:
            basis = name
            break
    if best is None:
        return None

    rel_meas, km, drop = best
    km_per_pct = km / drop
    since = max(0.0, run[-1]["odometer_km"] - e.odo)
    soc_frac = e.soc - since / km_per_pct
    soc_frac = min(max(soc_frac, soc_now - 0.5), soc_now + 0.5)   # must still round to what the car shows
    rng = soc_frac * km_per_pct

    cv, n_chunks = _cv(_chunks(_gather(all_runs, max_km), chunk_drop))
    rel_sd = math.sqrt(rel_meas**2 + cv**2 + (TRANSITION_SOC_SD / max(soc_frac, 1e-9)) ** 2)
    half = Z80 * rel_sd
    return RangeResult(
        status="ok", basis=basis, true_range_km=rng, low_km=max(0.0, rng * (1 - half)), high_km=rng * (1 + half),
        trust_pct=int(round(100 * max(0.0, 1 - half / TRUST_ZERO_HALF_WIDTH))),
        km_per_pct=km_per_pct, consumption_kwh_per_100km=usable_kwh / km_per_pct, window_km=km,
        soc_drop_pct=drop, n_runs=1, n_chunks=n_chunks, soc=soc_now, remaining_kwh=soc_frac / 100.0 * usable_kwh,
        est_rel_sd=math.sqrt(rel_meas**2 + (TRANSITION_SOC_SD / max(soc_frac, 1e-9)) ** 2), cv=cv,
    )


def estimate(
    readings: list[dict],
    soc_now: float | None = None,
    *,
    usable_kwh: float = DEFAULT_USABLE_KWH,
    window_km: float = 50.0,
    max_km: float = 150.0,
    min_km: float = 8.0,
    min_soc_drop: float = 4.0,
    chunk_drop: float = 3.0,
    odo_resolution_km: float = ODO_RESOLUTION_KM,
    use_transitions: bool = True,
    min_drop_transition: float = MIN_DROP_TRANSITION,
) -> RangeResult:
    """
    readings: OLDEST-first dicts {soc, odometer_km, ...}. soc_now defaults to the newest reading's SOC.
    Preferred: tick-anchored window (smooth, tighter). Falls back to the plain reading-to-reading method when the
    current run has no tick-anchored window yet (e.g. right after charging), or if use_transitions=False.
    """
    valid = [r for r in readings if (r.get("soc") or 0) > 0 and (r.get("odometer_km") or 0) > 0]
    if soc_now is None and valid:
        soc_now = valid[-1]["soc"]
    if soc_now is None or not valid:
        return _insufficient(soc_now, usable_kwh)
    if use_transitions:
        runs = split_runs(valid)
        res = _estimate_transitions(runs[-1], runs, soc_now, usable_kwh=usable_kwh, window_km=window_km,
                                    max_km=max_km, min_km=min_km, min_drop=min_drop_transition,
                                    chunk_drop=chunk_drop, odo_res=odo_resolution_km)
        if res is not None:
            return res
    return _estimate_legacy(readings, soc_now, usable_kwh=usable_kwh, window_km=window_km, max_km=max_km,
                            min_km=min_km, min_soc_drop=min_soc_drop, chunk_drop=chunk_drop,
                            odo_resolution_km=odo_resolution_km)


# ---------------------------------------------------------------------------------------------------------
# Provisional estimate for when there is not yet enough of OUR OWN driving data (e.g. a single reading).
# Uses BYD's own last-50-km consumption (kWh/100 km). Deliberately wide, clearly labelled "provisional".
# Unlike the main estimate this DOES depend on battery capacity, so capacity uncertainty is added.
# ---------------------------------------------------------------------------------------------------------
MIN_PLAUSIBLE_RATE = 6.0          # kWh/100 km: a recorded "best consumption" below this is a sentinel, not driving
CAP_UNCERTAINTY = 0.07            # relative std of "usable kWh" (real usable is below nominal, unknown)
CONS_RESOLUTION = 0.1             # BYD reports consumption to 0.1 kWh/100 km


def provisional_from_consumption(
    soc_now: float | None,
    kwh_per_100km: float | None,
    *,
    usable_kwh: float = DEFAULT_USABLE_KWH,
    cap_uncertainty: float = CAP_UNCERTAINTY,
    best_kwh_per_100: float | None = None,
) -> RangeResult:
    """
    Range from BYD's own last-50-km consumption (measured energy, 0.1 resolution) and the configured battery size.
    BYD's consumption figure is trustworthy; the unknown is the usable capacity, hence cap_uncertainty.

    The band is symmetric by construction, which wrongly allows the road ahead to be far BETTER than anything the car has
    done. best_kwh_per_100 (the best last-50-km consumption ever recorded on this car) caps the UPSIDE only: the high end
    cannot exceed the range at that best consumption (plus the battery-size uncertainty). Measured data, not BYD's range
    estimate. The downside (highway, A/C, wind) is untouched.
    """
    if soc_now is None or soc_now <= 0 or not kwh_per_100km or kwh_per_100km <= 0:
        return _insufficient(soc_now, usable_kwh)
    remaining = soc_now / 100.0 * usable_kwh
    rng = remaining / kwh_per_100km * 100.0
    est = math.sqrt(
        cap_uncertainty**2                              # unknown true usable capacity
        + (ENDPOINT_SD / soc_now) ** 2                  # today's SOC rounding
        + (CONS_RESOLUTION / math.sqrt(12) / kwh_per_100km) ** 2
    )
    rel = math.sqrt(PRIOR_CV**2 + est**2)               # PRIOR_CV: how much the road ahead may differ
    half = Z80 * rel
    low, high, parts = max(0.0, rng * (1 - half)), rng * (1 + half), None
    if best_kwh_per_100 and best_kwh_per_100 >= MIN_PLAUSIBLE_RATE:
        best = min(best_kwh_per_100, kwh_per_100km)     # right now may itself be the best the car has done
        capped = max(rng, min(high, remaining / best * 100.0 * (1 + Z80 * est)))
        if capped < high:
            high, parts = capped, {"best_kwh_per_100": best}
    half_eff = (high - low) / (2.0 * rng)               # trust follows the width actually shown
    return RangeResult(
        status="provisional", basis="byd-consumption", true_range_km=rng, low_km=low, high_km=high,
        trust_pct=int(round(100 * max(0.0, 1 - half_eff / TRUST_ZERO_HALF_WIDTH))),
        km_per_pct=rng / soc_now, consumption_kwh_per_100km=kwh_per_100km, window_km=50.0,
        soc_drop_pct=0.0, n_runs=0, n_chunks=0, soc=soc_now, remaining_kwh=remaining,
        est_rel_sd=est, cv=PRIOR_CV, parts=parts,
    )


def fuse(own: RangeResult, byd: RangeResult, *, soc_now: float | None, usable_kwh: float) -> RangeResult:
    """
    Combine the two independent estimates by inverse-variance weighting (in log space, so errors are relative):
      own: your driving, km per % of battery. Capacity-independent, but noisy early (whole-percent SOC).
      byd: BYD measured kWh/100 km x your battery size. Tight on consumption, limited by the unknown usable capacity.
    The variability of the road AHEAD (cv) is about the future and is shared, so it is added once, not averaged away.
    If the two disagree by more than their errors allow, the band is widened: a disagreement means one input is off
    (wrong battery size, stale BYD figure), and we would rather say so than look more certain than we are.
    """
    if byd.status != "provisional" or byd.true_range_km is None:
        return own
    if own.status != "ok" or own.true_range_km is None or not own.est_rel_sd:
        return byd
    ra, rb = own.true_range_km, byd.true_range_km
    wa, wb = 1.0 / own.est_rel_sd**2, 1.0 / byd.est_rel_sd**2
    rng = math.exp((wa * math.log(ra) + wb * math.log(rb)) / (wa + wb))
    disagree = abs(math.log(ra) - math.log(rb))
    est = max(1.0 / math.sqrt(wa + wb), disagree / 2.0)
    cv = own.cv if own.cv is not None else PRIOR_CV
    half = Z80 * math.sqrt(est**2 + cv**2)
    return RangeResult(
        status="ok", basis="combined", true_range_km=rng,
        low_km=max(0.0, rng * (1 - half)), high_km=rng * (1 + half),
        trust_pct=int(round(100 * max(0.0, 1 - half / TRUST_ZERO_HALF_WIDTH))),
        km_per_pct=rng / soc_now if soc_now else own.km_per_pct,
        consumption_kwh_per_100km=own.consumption_kwh_per_100km, window_km=own.window_km,
        soc_drop_pct=own.soc_drop_pct, n_runs=own.n_runs, n_chunks=own.n_chunks, soc=own.soc,
        remaining_kwh=(soc_now / 100.0 * usable_kwh) if soc_now else own.remaining_kwh,
        est_rel_sd=est, cv=cv,
        parts={"own_km": ra, "byd_km": rb, "own_weight": wa / (wa + wb), "disagree_pct": (rb / ra - 1) * 100.0},
    )


OWN_MIN_KM = 50.0   # distance the app must have WATCHED before it trusts its own driving data
NEW_CAR_KM = 50.0   # odometer below this: BYD has no consumption history either, so its own range is all there is


def new_car_from_byd_range(soc_now: float | None, byd_range_km: float | None, *, usable_kwh: float) -> RangeResult:
    """
    BYD's own range estimate, for a brand-new car only (nothing else exists yet). Deliberately wide and low-trust:
    BYD's range assumes a bigger battery than some cars have (about 57 kWh in one Atto 3 Standard Range owner's data).
    """
    if soc_now is None or soc_now <= 0 or not byd_range_km or byd_range_km <= 0:
        return _insufficient(soc_now, usable_kwh)
    est, cv = 0.20, 0.15
    half = Z80 * math.sqrt(est**2 + cv**2)
    return RangeResult(
        status="provisional", basis="byd-new-car", true_range_km=byd_range_km,
        low_km=max(0.0, byd_range_km * (1 - half)), high_km=byd_range_km * (1 + half),
        trust_pct=int(round(100 * max(0.0, 1 - half / TRUST_ZERO_HALF_WIDTH))),
        km_per_pct=byd_range_km / soc_now, consumption_kwh_per_100km=None, window_km=0.0, soc_drop_pct=0.0,
        n_runs=0, n_chunks=0, soc=soc_now, remaining_kwh=soc_now / 100.0 * usable_kwh, est_rel_sd=est, cv=cv,
    )


def apply_ceiling(result: RangeResult, best_kwh_per_100: float | None) -> RangeResult:
    """
    Your own estimate can come out better than anything the car has ever measured: a short window on whole-percent battery data
    (or a battery that drops slowly near the top) can imply, say, 11 kWh/100 km when BYD's own measured last-50-km figure has
    never been below 13. BYD's measured consumption is real energy, so it is a ceiling: the range cannot exceed what the
    remaining energy gives at the best consumption ever recorded on this car. Only the optimistic side is touched (a worse
    estimate is never raised) and BYD's range ESTIMATE is not used. parts["clamped_from_km"] records the original number.
    """
    if (result.status != "ok" or not best_kwh_per_100 or best_kwh_per_100 < MIN_PLAUSIBLE_RATE
            or result.true_range_km is None or not result.remaining_kwh or not result.soc):
        return result
    limit = result.remaining_kwh / best_kwh_per_100 * 100.0
    point = min(result.true_range_km, limit)
    # the band top may exceed the best-ever range only by the battery-size uncertainty (the same rule as the BYD-consumption path)
    top = max(point, min(result.high_km if result.high_km is not None else point, limit * (1 + Z80 * (result.est_rel_sd or 0.0))))
    if point >= result.true_range_km and (result.high_km is None or top >= result.high_km):
        return result
    parts = dict(result.parts or {})
    parts.update({"best_kwh_per_100": best_kwh_per_100})
    if point < result.true_range_km:
        parts["clamped_from_km"] = result.true_range_km
    return replace(
        result, true_range_km=point, low_km=min(result.low_km, point) if result.low_km is not None else None,
        high_km=top, km_per_pct=point / result.soc, parts=parts,
    )


def _tag(result: RangeResult, policy: str, observed_km: float) -> RangeResult:
    parts = dict(result.parts or {})
    parts.update({"policy": policy, "own_observed_km": observed_km, "own_needed_km": OWN_MIN_KM})
    return replace(result, parts=parts)


def policy_estimate(readings: list[dict], soc_now: float | None = None, *, usable_kwh: float = DEFAULT_USABLE_KWH,
                    byd_kwh_per_100: float | None = None, byd_range_km: float | None = None,
                    odometer_km: float | None = None, own_observed_km: float = 0.0,
                    cap_uncertainty: float = CAP_UNCERTAINTY, best_kwh_per_100: float | None = None,
                    **kw) -> RangeResult:
    """
    The owner's rule (docs/PROJECT_PLAN.md 6n), no blending:
      * Once the app has watched OWN_MIN_KM of driving, use ONLY your own data ("own").
      * Before that, or whenever your own estimate is missing for any reason, use BYD's measured last-50-km
        consumption on YOUR battery size ("immature" / "missing-own"). BYD's consumption is real measured energy.
      * BYD's range estimate is NEVER used, except on a brand-new car (odometer < NEW_CAR_KM) with no consumption
        figure at all ("new-car").
    parts["policy"] says which rule produced the number, so the page can tell you why.
    """
    own = estimate(readings, soc_now, usable_kwh=usable_kwh, **kw)
    soc = soc_now if soc_now is not None else own.soc
    byd = provisional_from_consumption(soc, byd_kwh_per_100, usable_kwh=usable_kwh, cap_uncertainty=cap_uncertainty,
                                       best_kwh_per_100=best_kwh_per_100)
    mature = own_observed_km >= OWN_MIN_KM
    if mature:
        if own.status == "ok":
            return _tag(apply_ceiling(own, best_kwh_per_100), "own", own_observed_km)
        if byd.status == "provisional":
            return _tag(byd, "missing-own", own_observed_km)
        return _tag(own, "none", own_observed_km)
    if byd.status == "provisional":
        return _tag(byd, "immature", own_observed_km)
    if odometer_km is not None and 0 < odometer_km < NEW_CAR_KM:
        nc = new_car_from_byd_range(soc, byd_range_km, usable_kwh=usable_kwh)
        if nc.status == "provisional":
            return _tag(nc, "new-car", own_observed_km)
    if own.status == "ok":
        return _tag(apply_ceiling(own, best_kwh_per_100), "own-no-byd", own_observed_km)
    return _tag(own, "none", own_observed_km)


def best_estimate(readings: list[dict], soc_now: float | None = None, *, usable_kwh: float = DEFAULT_USABLE_KWH,
                  byd_kwh_per_100: float | None = None, byd_range_km: float | None = None,
                  odometer_km: float | None = None, own_observed_km: float = 0.0, mode: str = "policy",
                  cap_uncertainty: float = CAP_UNCERTAINTY, best_kwh_per_100: float | None = None,
                  **kw) -> RangeResult:
    """
    The number to show.
      mode="policy":   the owner's rule, see policy_estimate (default).
      mode="own":      your own driving alone; BYD consumption only when there is no own estimate.
      mode="combined": your driving blended with BYD's consumption (experimental; not more accurate in simulation).
    """
    if mode == "policy":
        return policy_estimate(readings, soc_now, usable_kwh=usable_kwh, byd_kwh_per_100=byd_kwh_per_100,
                               byd_range_km=byd_range_km, odometer_km=odometer_km,
                               own_observed_km=own_observed_km, cap_uncertainty=cap_uncertainty,
                               best_kwh_per_100=best_kwh_per_100, **kw)
    own = estimate(readings, soc_now, usable_kwh=usable_kwh, **kw)
    soc = soc_now if soc_now is not None else own.soc
    byd = provisional_from_consumption(soc, byd_kwh_per_100, usable_kwh=usable_kwh, cap_uncertainty=cap_uncertainty,
                                       best_kwh_per_100=best_kwh_per_100)
    if mode == "own":
        return apply_ceiling(own, best_kwh_per_100) if own.status == "ok" or byd.status != "provisional" else byd
    return fuse(own, byd, soc_now=soc, usable_kwh=usable_kwh)
