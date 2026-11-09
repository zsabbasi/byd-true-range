"""The top of the band is limited by the best consumption actually recorded on the car; the downside is untouched."""
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import db  # noqa: E402
import range_engine as re_  # noqa: E402
import server  # noqa: E402
from test_server import probe_row  # noqa: E402

PACK = 49.92


class Cap(unittest.TestCase):
    def prov(self, rate=14.4, best=None, soc=91.0, cap=0.08):
        return re_.provisional_from_consumption(soc, rate, usable_kwh=PACK, cap_uncertainty=cap, best_kwh_per_100=best)

    def test_owner_numbers_top_of_band_drops_below_the_uncapped_403(self):
        plain = self.prov()
        capped = self.prov(best=14.0)
        self.assertAlmostEqual(plain.high_km, 403, delta=3)
        self.assertLess(capped.high_km, 375)                                   # below what BYD itself displays (374)
        self.assertGreater(capped.high_km, capped.true_range_km)               # still above the centre
        self.assertEqual(capped.parts["best_kwh_per_100"], 14.0)

    def test_the_downside_is_exactly_unchanged(self):
        plain, capped = self.prov(), self.prov(best=13.0)
        self.assertEqual(plain.low_km, capped.low_km)
        self.assertEqual(plain.true_range_km, capped.true_range_km)

    def test_never_above_the_uncapped_band_and_never_below_the_centre(self):
        plain = self.prov()
        for best in (6.0, 8.0, 10.0, 12.0, 14.0, 14.4, 20.0, 40.0):
            r = self.prov(best=best)
            self.assertLessEqual(r.high_km, plain.high_km + 1e-9, best)
            self.assertGreaterEqual(r.high_km, r.true_range_km - 1e-9, best)

    def test_a_better_best_means_a_higher_cap(self):
        highs = [self.prov(best=b).high_km for b in (14.2, 13.0, 11.0)]
        self.assertEqual(highs, sorted(highs))

    def test_when_now_is_the_best_ever_only_the_battery_uncertainty_remains_above(self):
        r = self.prov(rate=14.0, best=14.0)
        self.assertAlmostEqual(r.high_km, r.true_range_km * (1 + re_.Z80 * r.est_rel_sd), delta=0.5)
        self.assertLess(r.high_km, self.prov(rate=14.0).high_km)

    def test_best_worse_than_now_is_treated_as_now(self):
        a, b = self.prov(rate=14.0, best=15.4), self.prov(rate=14.0, best=14.0)
        self.assertAlmostEqual(a.high_km, b.high_km, places=6)

    def test_absurd_best_values_are_ignored(self):
        plain = self.prov()
        for bad in (0.0, 0.1, 2.0, 5.9, None, -3.0):
            self.assertEqual(self.prov(best=bad).high_km, plain.high_km, bad)

    def test_trust_follows_the_width_actually_shown(self):
        self.assertGreater(self.prov(best=14.0).trust_pct, self.prov().trust_pct)

    def test_a_better_best_allows_more_upside_so_trust_never_rises(self):
        # lower recorded best consumption = the car has done better = more upside is credible = wider band = less trust
        trusts = [self.prov(best=b).trust_pct for b in (14.4, 14.0, 13.0, 12.0)]
        self.assertEqual(trusts, sorted(trusts, reverse=True))
        self.assertGreater(trusts[0], trusts[-1])

    def test_capacity_uncertainty_still_widens_the_capped_top(self):
        narrow, wide = self.prov(best=14.0, cap=0.03), self.prov(best=14.0, cap=0.12)
        self.assertLess(narrow.high_km, wide.high_km)


class Policy(unittest.TestCase):
    def test_policy_passes_the_cap_to_the_byd_consumption_estimate(self):
        r = re_.best_estimate([{"soc": 91, "odometer_km": 1000.0}], usable_kwh=PACK, byd_kwh_per_100=14.4,
                              odometer_km=19000, own_observed_km=3, best_kwh_per_100=14.0)
        self.assertEqual(r.parts["policy"], "immature")
        self.assertLess(r.high_km, 375)

    def test_never_uses_byd_range_even_with_the_cap(self):
        r = re_.best_estimate([{"soc": 91, "odometer_km": 1000.0}], usable_kwh=PACK, byd_kwh_per_100=14.4,
                              byd_range_km=374.0, odometer_km=19000, own_observed_km=3, best_kwh_per_100=14.0)
        self.assertNotEqual(r.true_range_km, 374.0)
        self.assertNotEqual(r.high_km, 374.0)


class BestR50(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "b.sqlite"
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, rate, soc=80.0, odo=1000.0, i=[0]):
        i[0] += 1
        db.insert_reading(self.conn, probe_row(ts=f"2026-11-08T10:{i[0] % 60:02d}:00+00:00", soc=soc, odometer_km=odo,
                                              r50_rate=rate))

    def test_lowest_recorded_value(self):
        for v in (14.4, 14.0, 14.3, 15.2):
            self.add(v)
        self.assertEqual(db.best_r50(self.conn), 14.0)

    def test_ignores_none_zero_sentinels_and_empty_snapshots(self):
        self.add(None)
        self.add(0.0)
        self.add(0.1)
        self.add(2.0)
        self.add(9.0, soc=0.0, odo=0.0)                 # BYD's empty snapshot
        self.assertIsNone(db.best_r50(self.conn))
        self.add(13.7)
        self.assertEqual(db.best_r50(self.conn), 13.7)

    def test_empty_database(self):
        self.assertIsNone(db.best_r50(self.conn))


class StatusUsesIt(BestR50):
    def test_status_top_of_band_is_capped_and_says_so(self):
        for v in (14.4, 14.0, 14.3):
            self.add(v)
        db.insert_reading(self.conn, probe_row(ts="2026-11-08T11:30:00+00:00", soc=91.0, odometer_km=1000.0, r50_rate=14.4))
        s = server.build_status(self.conn, capacity_kwh=PACK, usable_fraction=1.0)["range"]
        self.assertEqual(s["policy"], "immature")
        self.assertTrue(s["upside_capped"])
        self.assertEqual(s["best_kwh_per_100"], 14.0)
        self.assertLess(s["high_km"], 375)
        self.assertGreater(s["high_km"], s["km"])

    def test_status_without_history_is_uncapped_flag_false_when_not_binding(self):
        db.insert_reading(self.conn, probe_row(ts="2026-11-08T11:30:00+00:00", soc=91.0, odometer_km=1000.0, r50_rate=14.4))
        s = server.build_status(self.conn, capacity_kwh=PACK, usable_fraction=1.0)["range"]
        self.assertTrue(s["upside_capped"])                # now IS the best ever seen, so only battery uncertainty remains
        self.assertEqual(s["best_kwh_per_100"], 14.4)


if __name__ == "__main__":
    unittest.main()
