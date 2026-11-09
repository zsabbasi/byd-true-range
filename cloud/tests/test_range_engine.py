"""
Tests for range_engine. Run from cloud/:   py -m unittest discover -s tests -v

The simulator drives a car with a KNOWN true consumption, reports SOC as a whole percent (like the BYD app),
and checks that the estimate is right on average, the 80% band covers the truth, and the band narrows with use.
"""
import os
import random
import statistics
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import range_engine as re_  # noqa: E402

CAP = 60.0          # true usable kWh in the simulation
TRUE_CONS = 15.0    # kWh/100 km, true mean consumption


def simulate(rng, km_driven, start_soc=90.0, poll_km=1.0, cv=0.10, cons=TRUE_CONS, odo0=1000.0):
    """Drive km_driven with per-5km consumption varying (lognormal-ish, sd=cv). Returns (readings, true_soc)."""
    soc_true, odo = start_soc, odo0
    readings = [{"soc": round(soc_true), "odometer_km": float(round(odo))}]
    chunk_cons = cons
    since = 0.0
    while odo - odo0 < km_driven:
        if since <= 0:
            chunk_cons = cons * max(0.5, rng.gauss(1.0, cv))
            since = 5.0
        step = min(poll_km, km_driven - (odo - odo0))
        soc_true -= chunk_cons * step / 100.0 / CAP * 100.0
        odo += step
        since -= step
        readings.append({"soc": round(soc_true), "odometer_km": float(round(odo))})
    return readings, soc_true


def true_range(soc_true, cons=TRUE_CONS):
    return soc_true / 100.0 * CAP / cons * 100.0


def actual_remaining_km(rng, soc_true, regime_sd=0.10, cv=0.10):
    """Keep driving until empty. The future has its own conditions: a regime multiplier (cold day, motorway)
    applies to the whole remaining drive, on top of chunk-level variation. Returns km actually achieved."""
    regime = max(0.6, rng.gauss(1.0, regime_sd))
    km, soc, since, chunk = 0.0, soc_true, 0.0, TRUE_CONS
    while soc > 0:
        if since <= 0:
            chunk = TRUE_CONS * regime * max(0.5, rng.gauss(1.0, cv))
            since = 5.0
        soc -= chunk * 1.0 / 100.0 / CAP * 100.0
        km += 1.0
        since -= 1.0
    return km


class TelescopingMath(unittest.TestCase):
    def test_exact_case(self):
        # 80% -> 70% over 50 km => 5 km per % => at 70% SOC: 350 km
        rows = [{"soc": 80 - i, "odometer_km": 1000 + 5 * i} for i in range(11)]
        r = re_.estimate(rows)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.km_per_pct, 5.0)
        self.assertAlmostEqual(r.true_range_km, 350.0)
        self.assertLess(r.low_km, 350.0)
        self.assertGreater(r.high_km, 350.0)

    def test_step_noise_does_not_bias(self):
        # Whole-percent SOC: most per-step deltas are 0. Per-step methods break here; ours must not.
        rng = random.Random(1)
        readings, soc_true = simulate(rng, 60, poll_km=0.4)
        r = re_.estimate(readings, usable_kwh=CAP)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.consumption_kwh_per_100km, TRUE_CONS, delta=TRUE_CONS * 0.20)


class OdometerResolution(unittest.TestCase):
    def test_coarse_odometer_widens_short_windows(self):
        rows = [{"soc": 80 - i, "odometer_km": 1000 + 2 * i} for i in range(11)]  # 10% over 20 km
        fine = re_.estimate(rows, odo_resolution_km=0.0)
        coarse = re_.estimate(rows, odo_resolution_km=1.0)
        w = lambda r: r.high_km - r.low_km
        self.assertGreater(w(coarse), w(fine))
        self.assertAlmostEqual(coarse.true_range_km, fine.true_range_km)  # point estimate unchanged


class SawtoothFix(unittest.TestCase):
    """SOC is a whole percent: the plain method creeps up between ticks and drops at each tick."""

    def _mean_swing(self, use_transitions, seeds=25, km=90):
        steps = []
        for s in range(seeds):
            d, _ = simulate(random.Random(300 + s), km, start_soc=96, poll_km=0.4)
            prev = None
            for k in range(len(d) // 3, len(d)):
                e = re_.estimate(d[: k + 1], usable_kwh=CAP, use_transitions=use_transitions)
                if e.status != "ok":
                    continue
                if prev:
                    steps.append(abs(e.true_range_km / prev - 1))
                prev = e.true_range_km
        return statistics.mean(steps)

    def test_tick_anchored_estimate_is_much_smoother(self):
        old, new = self._mean_swing(False), self._mean_swing(True)
        self.assertLess(new, old * 0.25)          # measured: about 8x smoother
        self.assertLess(new, 0.01)                # under 1% poll-to-poll

    def test_real_drive_replay_does_not_creep_between_ticks(self):
        # The owner's first real trip (SOC %, odometer minus a constant). The old method showed 296 -> 318 -> 341 km
        # with no battery tick; the new one stays put.
        seq = [(95, 8), (92, 20), (92, 20), (92, 20)] + [(91, 21)] * 4 + [(91, 22)] * 4 + [(91, 23)] * 8
        rows = [{"soc": s, "odometer_km": float(o)} for s, o in seq]
        tail = range(len(rows) - 12, len(rows))
        new = [re_.estimate(rows[: k + 1], usable_kwh=57.5).true_range_km for k in tail]
        old = [re_.estimate(rows[: k + 1], usable_kwh=57.5, use_transitions=False).true_range_km for k in tail]
        self.assertTrue(all(x is not None for x in new + old))
        self.assertLess(max(new) / min(new) - 1, 0.03)
        self.assertGreater(max(old) / min(old) - 1, 0.05)   # old creeps about 7% over this 12-row tail (15% over the full replay)

    def test_range_does_not_rise_while_driving_between_ticks(self):
        # same SOC shown, more km driven: the remaining range must not go UP
        base = [{"soc": 80 - i, "odometer_km": 1000.0 + 5 * i} for i in range(11)]   # 5 km per %
        base.append({"soc": 70, "odometer_km": 1051.0})
        a = re_.estimate(base, soc_now=70)
        base.append({"soc": 70, "odometer_km": 1053.0})
        b = re_.estimate(base, soc_now=70)
        self.assertLessEqual(b.true_range_km, a.true_range_km + 1e-9)

    def test_falls_back_when_no_tick_in_current_run(self):
        # right after charging there is no tick yet: must still return something sensible or insufficient
        rows = [{"soc": 100, "odometer_km": 1000.0 + i} for i in range(5)]
        self.assertIn(re_.estimate(rows).status, ("ok", "insufficient"))

    def test_fractional_soc_stays_consistent_with_display(self):
        rows = [{"soc": 80 - i, "odometer_km": 1000.0 + 5 * i} for i in range(11)]
        rows.append({"soc": 70, "odometer_km": 1080.0})        # drove 30 km with no tick: more than a % worth
        r = re_.estimate(rows, soc_now=70)
        self.assertGreaterEqual(r.remaining_kwh, (70 - 0.5) / 100 * re_.DEFAULT_USABLE_KWH - 1e-9)  # clipped at display


class Insufficient(unittest.TestCase):
    def test_short_drive(self):
        readings, _ = simulate(random.Random(2), 3)
        r = re_.estimate(readings)
        self.assertEqual(r.status, "insufficient")
        self.assertIsNone(r.true_range_km)
        self.assertIsNone(r.low_km)

    def test_empty(self):
        self.assertEqual(re_.estimate([]).status, "insufficient")

    def test_missing_values_ignored(self):
        rows = [{"soc": None, "odometer_km": 1}, {"soc": 50, "odometer_km": None}]
        self.assertEqual(re_.estimate(rows, soc_now=50).status, "insufficient")

    def test_parked_only(self):
        rows = [{"soc": 80, "odometer_km": 1000}] * 10
        self.assertEqual(re_.estimate(rows).status, "insufficient")


class ChargingAndGlitches(unittest.TestCase):
    def test_charging_starts_new_run(self):
        a, _ = simulate(random.Random(3), 40, start_soc=60)
        b, _ = simulate(random.Random(4), 40, start_soc=95, odo0=a[-1]["odometer_km"])
        readings = a + b  # SOC jumps up between them = charge
        runs = re_.split_runs(readings)
        self.assertEqual(len(runs), 2)
        r = re_.estimate(readings, usable_kwh=CAP)
        self.assertEqual(r.status, "ok")
        self.assertGreater(r.soc_drop_pct, 0)
        self.assertGreater(r.km_per_pct, 0)

    def test_one_percent_jitter_tolerated(self):
        rows = [{"soc": s, "odometer_km": 1000 + 2 * i} for i, s in enumerate([80, 79, 80, 78, 77, 76, 75, 74, 73, 72, 71])]
        self.assertEqual(len(re_.split_runs(rows)), 1)

    def test_odometer_glitch_splits(self):
        rows = [{"soc": 80, "odometer_km": 1000}, {"soc": 78, "odometer_km": 1010},
                {"soc": 77, "odometer_km": 5}, {"soc": 75, "odometer_km": 20}]
        self.assertEqual(len(re_.split_runs(rows)), 2)

    def test_parked_drain_included_not_crash(self):
        rng = random.Random(5)
        readings, _ = simulate(rng, 50)
        last = readings[-1]
        for _ in range(24):  # a day parked, hourly, odometer unchanged
            readings.append({"soc": last["soc"], "odometer_km": last["odometer_km"]})
        r = re_.estimate(readings, usable_kwh=CAP)
        self.assertEqual(r.status, "ok")


class BandBehaviour(unittest.TestCase):
    def _trials(self, km, n=150, start_soc=90.0):
        out = []
        for seed in range(n):
            rng = random.Random(1000 + seed)
            drive, soc_true = simulate(rng, km, start_soc=start_soc)
            # the future: truth is the mean consumption (variability averages out over the remaining drive)
            truth = true_range(soc_true)
            r = re_.estimate(drive, usable_kwh=CAP)
            out.append((r, truth))
        return out

    def test_band_covers_truth_most_of_the_time(self):
        trials = [t for t in self._trials(60) if t[0].status == "ok"]
        self.assertGreater(len(trials), 100)
        covered = sum(1 for r, truth in trials if r.low_km <= truth <= r.high_km)
        # Nominal 80%. Allow slack for simulator/heuristic mismatch, but it must not be badly over-confident.
        self.assertGreaterEqual(covered / len(trials), 0.75)


    def test_band_covers_actual_future_range(self):
        """Honest coverage: truth = km actually achieved on a future drive with shifted conditions."""
        covered = n = 0
        for seed in range(300):
            rng = random.Random(5000 + seed)
            drive, soc_true = simulate(rng, 60, start_soc=95)
            r = re_.estimate(drive, usable_kwh=CAP)
            if r.status != "ok":
                continue
            n += 1
            actual = actual_remaining_km(rng, soc_true)
            covered += r.low_km <= actual <= r.high_km
        self.assertGreater(n, 200)
        cov = covered / n
        print(f"future-range coverage of nominal-80% band: {cov:.2f} (n={n})")
        self.assertGreaterEqual(cov, 0.75)  # must not be over-confident

    def test_point_estimate_is_unbiased_enough(self):
        trials = [t for t in self._trials(60) if t[0].status == "ok"]
        errs = [(r.true_range_km - truth) / truth for r, truth in trials]
        self.assertLess(abs(statistics.mean(errs)), 0.05)

    def test_band_narrows_with_more_driving(self):
        widths = []
        for km in (25, 50, 120):
            trials = [t for t in self._trials(km, n=100) if t[0].status == "ok"]
            self.assertGreater(len(trials), 50, f"too few ok trials at {km} km")
            widths.append(statistics.mean((r.high_km - r.low_km) / r.true_range_km for r, _ in trials))
        self.assertGreater(widths[0], widths[1])
        self.assertGreater(widths[1], widths[2])

    def test_band_has_variability_floor(self):
        # even with a long, clean history the band never collapses to a point
        rows = [{"soc": 100 - i, "odometer_km": 1000 + 5 * i} for i in range(81)]
        r = re_.estimate(rows, window_km=400, max_km=400)
        self.assertGreater((r.high_km - r.low_km) / r.true_range_km, 2 * re_.Z80 * re_.CV_FLOOR * 0.95)

    def test_trust_rises_with_data(self):
        t = []
        for km in (25, 120):
            trials = [x for x in self._trials(km, n=60) if x[0].status == "ok"]
            t.append(statistics.mean(r.trust_pct for r, _ in trials))
        self.assertGreater(t[1], t[0])

    def test_low_soc_widens_band(self):
        rows = [{"soc": 40 - i, "odometer_km": 1000 + 5 * i} for i in range(31)]  # 40% -> 10%
        hi = re_.estimate(rows, soc_now=40)
        lo = re_.estimate(rows, soc_now=10)
        rel = lambda r: (r.high_km - r.low_km) / r.true_range_km
        self.assertGreater(rel(lo), rel(hi))

    def test_band_ordering_and_bounds(self):
        for r, _ in self._trials(60, n=30):
            if r.status == "ok":
                self.assertLessEqual(r.low_km, r.true_range_km)
                self.assertLessEqual(r.true_range_km, r.high_km)
                self.assertGreaterEqual(r.low_km, 0)
                self.assertTrue(0 <= r.trust_pct <= 100)


if __name__ == "__main__":
    unittest.main()
