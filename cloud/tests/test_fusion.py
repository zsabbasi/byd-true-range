"""Tests for the combined (own driving + BYD consumption) estimate. Run from cloud/: py -m unittest discover -s tests"""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import range_engine as re_  # noqa: E402
import test_range_engine as tre  # noqa: E402

CAP = tre.CAP


def readings(km, start_soc=96.0, seed=1, silent_km=0.0):
    """A simulated drive; optionally the first silent_km are not reported (like the owner's real first trip)."""
    rng = random.Random(seed)
    soc_true, odo, since, chunk, done = start_soc, 1000.0, 0.0, tre.TRUE_CONS, 0.0
    out = [{"soc": round(soc_true), "odometer_km": float(round(odo)), "true_soc": soc_true, "true_odo": odo}]
    while done < silent_km + km:
        if since <= 0:
            chunk, since = tre.TRUE_CONS * max(0.5, rng.gauss(1.0, 0.10)), 5.0
        step = min(0.5, silent_km + km - done)
        soc_true -= chunk * step / 100.0 / CAP * 100.0
        odo += step
        done += step
        since -= step
        if done > silent_km:
            out.append({"soc": round(soc_true), "odometer_km": float(round(odo)), "true_soc": soc_true, "true_odo": odo})
    return out, soc_true


def byd_rate(rows):
    """What BYD reports: real energy over the last <= 50 km, rounded to 0.1 kWh/100 km."""
    end = rows[-1]
    j = next(i for i, x in enumerate(rows) if end["true_odo"] - x["true_odo"] <= 50)
    km = end["true_odo"] - rows[j]["true_odo"]
    return round((rows[j]["true_soc"] - end["true_soc"]) / 100 * CAP / km * 100, 1)


class Fuse(unittest.TestCase):
    def setUp(self):
        self.rows, self.soc_true = readings(30, seed=3, silent_km=12)
        self.rate = byd_rate(self.rows)

    def test_combined_sits_between_its_two_inputs(self):
        r = re_.best_estimate(self.rows, usable_kwh=CAP, byd_kwh_per_100=self.rate, mode="combined")
        self.assertEqual(r.basis, "combined")
        lo, hi = sorted((r.parts["own_km"], r.parts["byd_km"]))
        self.assertLessEqual(lo - 1e-6, r.true_range_km)
        self.assertLessEqual(r.true_range_km, hi + 1e-6)
        self.assertTrue(r.low_km < r.true_range_km < r.high_km)
        self.assertAlmostEqual(r.parts["own_weight"] + 0, r.parts["own_weight"])
        self.assertTrue(0 < r.parts["own_weight"] < 1)

    def test_weight_on_own_driving_grows_with_distance(self):
        weights = []
        for km in (5, 15, 40):
            rows, _ = readings(km, seed=5, silent_km=12)
            weights.append(re_.best_estimate(rows, usable_kwh=CAP, byd_kwh_per_100=byd_rate(rows), mode="combined").parts["own_weight"])
        self.assertLess(weights[0], weights[1])
        self.assertLess(weights[1], weights[2])

    def test_disagreement_widens_the_band(self):
        agree = re_.best_estimate(self.rows, usable_kwh=CAP, byd_kwh_per_100=self.rate, mode="combined")
        clash = re_.best_estimate(self.rows, usable_kwh=CAP, byd_kwh_per_100=self.rate * 1.45, mode="combined")   # BYD figure far off
        self.assertGreater(clash.parts["disagree_pct"] * -1, 15)                                   # BYD-based is much lower
        w = lambda x: (x.high_km - x.low_km) / x.true_range_km
        self.assertGreater(w(clash), w(agree))
        self.assertLess(clash.trust_pct, agree.trust_pct)

    def test_wrong_battery_size_is_flagged_not_hidden(self):
        wrong = re_.best_estimate(self.rows, usable_kwh=CAP * 1.30, byd_kwh_per_100=self.rate, mode="combined")    # config says 30% too big
        self.assertGreater(wrong.parts["disagree_pct"], 15)
        ok = re_.best_estimate(self.rows, usable_kwh=CAP, byd_kwh_per_100=self.rate, mode="combined")
        self.assertLess(wrong.trust_pct, ok.trust_pct)

    def test_without_byd_consumption_it_is_just_the_own_estimate(self):
        a = re_.best_estimate(self.rows, usable_kwh=CAP, byd_kwh_per_100=None)
        b = re_.estimate(self.rows, usable_kwh=CAP)
        self.assertEqual(a.basis, b.basis)
        self.assertEqual(a.true_range_km, b.true_range_km)

    def test_without_own_data_it_is_the_byd_consumption_estimate(self):
        rows = [{"soc": 91, "odometer_km": 1000.0}]
        r = re_.best_estimate(rows, usable_kwh=47.4, byd_kwh_per_100=14.4)
        self.assertEqual(r.status, "provisional")
        self.assertAlmostEqual(r.true_range_km, 91 / 100 * 47.4 / 14.4 * 100, delta=0.5)

    def test_nothing_at_all_is_insufficient(self):
        self.assertEqual(re_.best_estimate([], usable_kwh=47.4, byd_kwh_per_100=None).status, "insufficient")
        self.assertEqual(re_.best_estimate([{"soc": 50, "odometer_km": 1.0}], byd_kwh_per_100=0).status, "insufficient")

    def test_own_mode_ignores_byd_when_own_is_ok(self):
        a = re_.best_estimate(self.rows, usable_kwh=CAP, byd_kwh_per_100=self.rate, mode="own")
        self.assertEqual(a.basis, re_.estimate(self.rows, usable_kwh=CAP).basis)
        self.assertNotEqual(a.basis, "combined")

    def test_own_mode_still_falls_back_to_byd_when_no_own_estimate(self):
        r = re_.best_estimate([{"soc": 91, "odometer_km": 1000.0}], usable_kwh=47.4, byd_kwh_per_100=14.4, mode="own")
        self.assertEqual(r.status, "provisional")

    def test_log_space_fusion_is_symmetric(self):
        own = re_.RangeResult("ok", "window", 400.0, 300.0, 500.0, 50, 4.0, 15.0, 30.0, 8.0, 1, 3, 90.0, 40.0,
                              est_rel_sd=0.10, cv=0.10)
        byd = re_.RangeResult("provisional", "byd-consumption", 300.0, 200.0, 400.0, 30, 3.0, 14.0, 50.0, 0.0, 0, 0,
                              90.0, 40.0, est_rel_sd=0.10, cv=0.20)
        f = re_.fuse(own, byd, soc_now=90.0, usable_kwh=45.0)
        self.assertAlmostEqual(f.true_range_km, math.sqrt(400.0 * 300.0), places=6)   # equal weights: geometric mean
        self.assertAlmostEqual(f.parts["own_weight"], 0.5)

    def test_every_result_reports_its_estimation_error(self):
        for r in (re_.estimate(self.rows, usable_kwh=CAP, use_transitions=True),
                  re_.estimate(self.rows, usable_kwh=CAP, use_transitions=False),
                  re_.provisional_from_consumption(90, 14.0)):
            self.assertIsNotNone(r.est_rel_sd)
            self.assertGreater(r.est_rel_sd, 0)
            self.assertIsNotNone(r.cv)

    def test_real_owner_numbers(self):
        # first real trip: own 324 km vs BYD-consumption 300 km at 91% on a 47.4 kWh usable pack
        own = re_.RangeResult("ok", "window", 324.0, 232.0, 416.0, 29, 3.6, 13.3, 12.0, 3.5, 1, 0, 91.0, 43.1,
                              est_rel_sd=0.11, cv=0.20)
        byd = re_.provisional_from_consumption(91, 14.4, usable_kwh=47.4)
        f = re_.fuse(own, byd, soc_now=91.0, usable_kwh=47.4)
        self.assertTrue(299 < f.true_range_km < 325)
        self.assertEqual(f.basis, "combined")


if __name__ == "__main__":
    unittest.main()
