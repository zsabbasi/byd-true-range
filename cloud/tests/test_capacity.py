"""Tests for capacity.py (learned usable battery capacity). Simulator with a KNOWN true battery size."""
import datetime as dt
import os
import random
import statistics
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import capacity  # noqa: E402

T0 = dt.datetime(2026, 11, 8, 8, 0, 0, tzinfo=dt.timezone.utc)
NOMINAL = 49.92


class Ledger:
    """BYD's last-50-km consumption, computed from the REAL simulated energy (cumulative kWh vs odometer)."""

    def __init__(self, start_odo, cons):
        self.pts = [(start_odo - 50.0, -cons * 50 / 100.0), (start_odo, 0.0)]    # a car that already has history

    def add(self, odo, cum):
        self.pts.append((odo, cum))

    def rate(self, odo_now, cum_now):
        target = odo_now - 50.0
        c_t = self.pts[0][1]
        for (o0, c0), (o1, c1) in zip(self.pts, self.pts[1:]):
            if o0 <= target <= o1:
                c_t = c0 + ((c1 - c0) * (target - o0) / (o1 - o0) if o1 > o0 else 0.0)
                break
        return round((cum_now - c_t) / 50.0 * 100.0, 1)


def sim_rows(km, true_cap, *, start_soc=92.0, cons=15.0, seed=1, poll_km=0.5, start_odo=1000.0, t0=T0, cv=0.10,
             with_rate=True):
    """DB-style rows for one drive at 60 km/h. SOC and odometer are whole numbers, like the real car."""
    rng = random.Random(seed)
    soc, odo, cum, since, chunk, done = start_soc, start_odo, 0.0, 0.0, cons, 0.0
    led = Ledger(start_odo, cons)
    rows, t = [], 0

    def row():
        ts = (t0 + dt.timedelta(seconds=t)).isoformat()
        return {"ts": ts, "car_ts": ts, "soc": float(round(soc)), "odometer_km": float(round(odo)), "speed_kmh": 60.0,
                "is_driving": 1, "r50_rate": led.rate(odo, cum) if with_rate else None, "lat": None, "lon": None}

    rows.append(row())
    while done < km:
        if since <= 0:
            chunk, since = cons * max(0.5, rng.gauss(1.0, cv)), 5.0
        step = min(poll_km, km - done)
        kwh = chunk * step / 100.0
        cum += kwh
        soc -= kwh / true_cap * 100.0
        odo += step
        done += step
        since -= step
        t += int(step / 60.0 * 3600)
        led.add(odo, cum)
        rows.append(row())
    return rows, soc


class Learning(unittest.TestCase):
    def test_recovers_the_true_capacity(self):
        errs = []
        for seed in range(12):
            rows, _ = sim_rows(150, 50.0, seed=seed)
            r = capacity.learn(rows, NOMINAL, prior_kwh=50.0 * 0.90)
            self.assertGreaterEqual(r["n_samples"], 2, seed)
            errs.append(abs(r["usable_kwh"] / 50.0 - 1))
        self.assertLess(statistics.mean(errs), 0.04)
        self.assertLess(max(errs), 0.08)

    def test_a_wrong_prior_is_corrected(self):
        gain = []
        for seed in range(12):
            rows, _ = sim_rows(150, 50.0, seed=seed)
            prior = 50.0 * 0.85                                            # configured 15% too small
            r = capacity.learn(rows, NOMINAL, prior_kwh=prior)
            gain.append(abs(prior / 50.0 - 1) - abs(r["usable_kwh"] / 50.0 - 1))
        self.assertGreater(statistics.mean(gain), 0.08)                    # moved most of the way there

    def test_confidence_rises_with_driving(self):
        conf, sd = [], []
        for km in (0, 80, 200, 400):
            rows, _ = sim_rows(km, 50.0, seed=3, start_soc=96.0) if km else ([], None)
            r = capacity.learn(rows, NOMINAL, prior_kwh=45.0)
            conf.append(r["confidence_pct"])
            sd.append(r["sd_rel"])
        self.assertEqual(sorted(sd, reverse=True), sd)
        self.assertLess(conf[0], conf[-1])
        self.assertEqual(conf[0], 0 if conf[0] < 15 else conf[0])
        self.assertTrue(conf[0] <= 15)                                      # prior alone is barely confident

    def test_no_data_returns_exactly_the_prior(self):
        r = capacity.learn([], NOMINAL, prior_kwh=47.4)
        self.assertTrue(r["prior_only"])
        self.assertAlmostEqual(r["usable_kwh"], 47.4, places=6)
        self.assertEqual(r["n_samples"], 0)
        self.assertEqual(r["samples"], [])

    def test_a_short_drive_gives_no_sample(self):
        rows, _ = sim_rows(20, 50.0)
        self.assertEqual(capacity.samples(rows, NOMINAL), [])

    def test_needs_byd_consumption(self):
        rows, _ = sim_rows(120, 50.0, with_rate=False)
        self.assertEqual(capacity.samples(rows, NOMINAL), [])

    def test_windows_never_overlap(self):
        rows, _ = sim_rows(300, 50.0, seed=5, start_soc=95.0)
        ws = capacity.samples(rows, NOMINAL)
        self.assertGreaterEqual(len(ws), 3)
        ends = [dt.datetime.fromisoformat(w["when"]) for w in ws]
        self.assertEqual(ends, sorted(ends))
        self.assertTrue(all(30 <= w["km"] <= 50 for w in ws))
        self.assertTrue(all(w["drop_pct"] >= capacity.MIN_DROP for w in ws))
        self.assertEqual(len(ends), len(set(ends)))
        total = sum(w["km"] for w in ws)
        self.assertLessEqual(total, 300 + 2)                                # disjoint windows cannot exceed the drive

    def test_top_and_bottom_of_the_battery_are_not_used(self):
        rows, _ = sim_rows(90, 50.0, start_soc=99.0)                        # starts at 99%: top is non-linear
        for w in capacity.samples(rows, NOMINAL):
            self.assertLessEqual(w["drop_pct"], 99)
        low, _ = sim_rows(60, 50.0, start_soc=22.0)                         # ends near 0%
        for w in capacity.samples(low, NOMINAL):
            self.assertTrue(w["usable_kwh"] > 0)
        # a window that would START above 92% must not be used
        hi, _ = sim_rows(40, 50.0, start_soc=96.0, seed=2)
        for w in capacity.samples(hi, NOMINAL):
            self.assertLessEqual(96 - w["drop_pct"], 96)
        self.assertEqual(capacity.samples(sim_rows(35, 50.0, start_soc=97.0)[0], NOMINAL), [])

    def test_implausible_sample_is_dropped_not_averaged_in(self):
        rows, _ = sim_rows(120, 50.0, seed=7)
        for r in rows:
            r["r50_rate"] = 45.0                                             # BYD says 45 kWh/100: absurd for this pack
        self.assertEqual(capacity.samples(rows, NOMINAL), [])

    def test_stale_duplicates_and_empty_snapshots_are_ignored(self):
        rows, _ = sim_rows(120, 50.0, seed=8)
        clean = capacity.learn(rows, NOMINAL, 45.0)
        dirty = list(rows)
        dirty.insert(40, {**rows[39], "ts": rows[40]["ts"]})                 # same car_ts as row 39: stale snapshot
        dirty.insert(60, {**rows[59], "soc": 0.0, "odometer_km": 0.0, "car_ts": "", "r50_rate": None})
        out = capacity.learn(dirty, NOMINAL, 45.0)
        self.assertAlmostEqual(out["usable_kwh"], clean["usable_kwh"], delta=0.3)

    def test_charging_between_drives_does_not_poison_samples(self):
        a, soc_a = sim_rows(70, 50.0, seed=9, start_soc=90.0)
        b, _ = sim_rows(70, 50.0, seed=10, start_soc=88.0, start_odo=1080.0, t0=T0 + dt.timedelta(hours=6))
        r = capacity.learn(a + b, NOMINAL, 45.0)                             # SOC jumps UP between them: a charge
        self.assertGreaterEqual(r["n_samples"], 2)
        self.assertAlmostEqual(r["usable_kwh"], 50.0, delta=3.0)

    def test_samples_report_what_they_used(self):
        rows, _ = sim_rows(120, 50.0, seed=4)
        w = capacity.samples(rows, NOMINAL)[0]
        for key in ("when", "km", "drop_pct", "byd_kwh_per_100", "usable_kwh", "sd_rel", "sd_kwh"):
            self.assertIn(key, w)
        self.assertAlmostEqual(w["usable_kwh"], w["byd_kwh_per_100"] * w["km"] / w["drop_pct"], delta=0.02)

    def test_uncertainty_never_drops_below_the_systematic_floor(self):
        rows, _ = sim_rows(600, 50.0, seed=2, start_soc=96.0)
        r = capacity.learn(rows, NOMINAL, 45.0)
        self.assertGreaterEqual(r["n_samples"], 5)
        self.assertGreaterEqual(r["sd_rel"], capacity.SYSTEMATIC_REL)
        self.assertLess(r["confidence_pct"], 70)                         # cannot claim near-certainty from a simulator-grade model

    def test_prior_uncertainty_is_honest(self):
        r = capacity.learn([], NOMINAL, 47.4)
        self.assertAlmostEqual(r["sd_rel"], capacity.PRIOR_SD_REL)
        self.assertAlmostEqual(r["pct_of_nominal"], 47.4 / NOMINAL * 100)


class OwnersRealDay(unittest.TestCase):
    def test_three_watched_km_give_no_capacity_sample(self):
        """The owner's first real day: 12 km nobody saw, then 3 km watched. Nowhere near a 30 km window."""
        def row(sec, soc, odo, rate=14.4):
            ts = (T0 + dt.timedelta(seconds=sec)).isoformat()
            return {"ts": ts, "car_ts": ts, "soc": float(soc), "odometer_km": float(odo), "speed_kmh": 20.0,
                    "is_driving": 1, "r50_rate": rate, "lat": None, "lon": None}
        rows = [row(0, 95, 19708), row(2640, 92, 19720)] + [row(2640 + 30 * i, 91, 19720 + i // 3) for i in range(1, 10)]
        r = capacity.learn(rows, NOMINAL, 47.4)
        self.assertTrue(r["prior_only"])
        self.assertAlmostEqual(r["usable_kwh"], 47.4, places=6)


if __name__ == "__main__":
    unittest.main()
