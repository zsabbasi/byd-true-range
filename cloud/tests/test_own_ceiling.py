"""Your own estimate may not beat the best consumption BYD has ever measured on this car (found on a real drive: 399 km at 91%)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import range_engine as re_  # noqa: E402


def own(range_km=399.0, soc=91.0, usable=49.92, lo=280.0, hi=519.0):
    rem = soc / 100 * usable
    return re_.RangeResult(status="ok", basis="window", true_range_km=range_km, low_km=lo, high_km=hi, trust_pct=0,
                           km_per_pct=range_km / soc, consumption_kwh_per_100km=11.4, window_km=35.0, soc_drop_pct=8.0,
                           n_runs=1, n_chunks=1, soc=soc, remaining_kwh=rem, est_rel_sd=0.1, cv=0.1)


class Ceiling(unittest.TestCase):
    def test_an_estimate_better_than_the_best_ever_measured_is_clamped(self):
        r = re_.apply_ceiling(own(), 13.1)
        self.assertAlmostEqual(r.true_range_km, 0.91 * 49.92 / 13.1 * 100, places=6)       # 346.8 km
        self.assertAlmostEqual(r.high_km, r.true_range_km * (1 + re_.Z80 * 0.1), places=6)    # upside only by the battery-size uncertainty
        self.assertLess(r.high_km, 519.0)
        self.assertEqual(r.parts["clamped_from_km"], 399.0)
        self.assertEqual(r.parts["best_kwh_per_100"], 13.1)
        self.assertLessEqual(r.low_km, r.true_range_km)
        self.assertAlmostEqual(r.km_per_pct, r.true_range_km / 91.0)

    def test_a_plausible_estimate_is_untouched(self):
        r0 = own(range_km=300.0, hi=380.0)
        self.assertIs(re_.apply_ceiling(r0, 13.1), r0)

    def test_it_never_raises_a_low_estimate_or_acts_without_a_best_figure(self):
        r0 = own(range_km=250.0, lo=22.0, hi=477.0)
        self.assertIs(re_.apply_ceiling(r0, None), r0)
        self.assertIs(re_.apply_ceiling(r0, 3.0), r0)            # below the plausibility floor: a sentinel, not driving
        r = re_.apply_ceiling(r0, 13.1)                          # limit at 91% is ~347: the 250 stays, the absurd band top comes down
        self.assertEqual((r.true_range_km, r.low_km), (250.0, 22.0))
        self.assertAlmostEqual(r.high_km, 0.91 * 49.92 / 13.1 * 100 * (1 + re_.Z80 * 0.1), places=6)
        self.assertNotIn("clamped_from_km", r.parts)

    def test_insufficient_results_pass_through(self):
        r0 = re_._insufficient(80.0, 49.92)
        self.assertIs(re_.apply_ceiling(r0, 13.1), r0)

    def test_policy_applies_it_to_own_data_and_the_page_can_say_so(self):
        import test_fusion as tf
        rows, _ = tf.readings(60, seed=11)
        free = re_.best_estimate(rows, usable_kwh=49.92, byd_kwh_per_100=14.4, odometer_km=19000, own_observed_km=80)
        best = free.remaining_kwh / free.true_range_km * 100 * 1.25      # a "best ever" 25% worse than own implies: must clamp
        r = re_.best_estimate(rows, usable_kwh=49.92, byd_kwh_per_100=14.4, odometer_km=19000, own_observed_km=80,
                              best_kwh_per_100=best)
        self.assertEqual(r.parts["policy"], "own")
        self.assertLess(r.true_range_km, free.true_range_km)
        self.assertAlmostEqual(r.true_range_km, free.remaining_kwh / best * 100, places=6)
        self.assertEqual(r.parts["clamped_from_km"], free.true_range_km)


if __name__ == "__main__":
    unittest.main()
