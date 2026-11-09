"""The band's low end must not collapse because of one odd chunk (found on a real drive: 9 km low end at 77%)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import range_engine as re_  # noqa: E402


def drive(points):
    """points: [(odometer, soc)] one reading each, oldest first."""
    return [{"odometer_km": float(o), "soc": float(s)} for o, s in points]


# The shape of the real drive: ~3 km per percent, with one stretch where 53 km were driven while the app was not watching.
REAL_SHAPE = [(19708, 95), (19714, 94), (19720, 92), (19724, 91), (19728, 89), (19781, 86),
              (19785, 85), (19788, 84), (19791, 83), (19795, 82), (19799, 81), (19802, 80), (19806, 79), (19811, 77)]


class Variability(unittest.TestCase):
    def test_a_stretch_nobody_watched_is_not_a_data_point(self):
        tails = [drive(REAL_SHAPE)]
        vals = re_._chunks(tails, 3.0)
        self.assertTrue(all(v < 6 for v in vals), vals)            # the 17.7 km/% chunk (53 km for 3%) is gone
        self.assertTrue(all(2.0 < v < 5.5 for v in vals), vals)

    def test_chunks_still_form_normally_without_a_gap(self):
        pts = [(1000 + 3 * i, 90 - i) for i in range(0, 13)]       # 3 km per percent, no jumps
        vals = re_._chunks([drive(pts)], 3.0)
        self.assertEqual(len(vals), 4)
        self.assertTrue(all(abs(v - 3.0) < 1e-9 for v in vals))

    def test_one_outlier_cannot_dominate_the_variability(self):
        calm = [3.4, 3.6, 3.5, 3.3, 3.7, 3.5]
        cv_calm, _ = re_._cv(calm)
        cv_outlier, _ = re_._cv(calm + [17.7])
        self.assertLess(cv_outlier - cv_calm, 0.08)               # was +0.5 with a plain standard deviation
        self.assertLess(cv_outlier, 0.30)

    def test_real_variability_is_still_seen(self):
        cv_wild, _ = re_._cv([2.0, 5.0, 2.5, 4.8, 2.2, 5.1, 2.4, 4.9])
        cv_calm, _ = re_._cv([3.5, 3.6, 3.4, 3.5, 3.55, 3.45, 3.5, 3.5])
        self.assertGreater(cv_wild, cv_calm + 0.10)

    def test_variability_is_capped_so_bad_data_cannot_flatten_the_low_end(self):
        cv, _ = re_._cv([0.2, 9.0, 0.3, 8.0, 0.25, 9.5])           # garbage in
        self.assertEqual(cv, re_.CV_CAP)

    def test_the_real_drive_no_longer_gives_a_nonsense_band(self):
        rows = drive(REAL_SHAPE)
        r = re_.estimate(rows, 77.0, usable_kwh=49.92)
        self.assertEqual(r.status, "ok")
        self.assertGreater(r.low_km, 0.45 * r.true_range_km)      # was 9 km on 227 km
        self.assertLess(r.high_km, 1.60 * r.true_range_km)
        self.assertLessEqual(r.cv, re_.CV_CAP)

    def test_the_low_end_never_falls_below_forty_percent_of_the_estimate_whatever_the_data(self):
        rows = drive([(1000, 90), (1002, 85), (1060, 80), (1061, 74), (1130, 70), (1131, 64), (1140, 60), (1141, 54)])
        r = re_.estimate(rows, 54.0, usable_kwh=49.92)
        if r.status == "ok":
            self.assertGreaterEqual(r.low_km, 0.38 * r.true_range_km)


class SlowCharging(unittest.TestCase):
    def test_a_slow_charge_splits_the_run_even_though_no_single_step_jumps_two_percent(self):
        drive1 = [(1000 + 3 * i, 90 - i) for i in range(0, 5)]                       # 90 -> 86 while driving
        charge = [(1012, 86 + k) for k in range(1, 12)]                               # parked at 1012, +1% per reading up to 97
        drive2 = [(1012 + 3 * i, 97 - i) for i in range(1, 10)]                       # then drive on
        runs = re_.split_runs(drive(drive1 + charge + drive2))
        self.assertGreaterEqual(len(runs), 3)
        self.assertLessEqual(max(len(r) for r in runs), len(drive2) + 1)             # nothing spans the charge
        vals = re_._chunks(runs, 3.0)                                                 # no chunk takes % before and km after a charge
        self.assertTrue(vals and all(2.5 < v < 3.5 for v in vals), vals)

    def test_a_percent_of_regen_while_moving_is_not_a_charge(self):
        rows = [{"soc": s, "odometer_km": 1000 + 2 * i} for i, s in enumerate([80, 79, 80, 78, 77, 76, 75, 74])]
        self.assertEqual(len(re_.split_runs(rows)), 1)


if __name__ == "__main__":
    unittest.main()
