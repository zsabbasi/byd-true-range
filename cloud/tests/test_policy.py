"""The owner's rule: BYD measured consumption until 50 km of own data, then own data only; never BYD's range, except new car."""
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
import test_fusion as tf  # noqa: E402
from test_server import probe_row  # noqa: E402

USABLE = 49.92 * 0.95
BYD_RANGE = 393.0          # BYD's displayed range: must never feed an estimate on a car that is not new


def good_rows():
    rows, _ = tf.readings(60, seed=11)             # a real own estimate exists
    return rows


class PolicyMatrix(unittest.TestCase):
    def est(self, rows=None, **kw):
        kw.setdefault("usable_kwh", USABLE)
        kw.setdefault("byd_range_km", BYD_RANGE)
        return re_.best_estimate(good_rows() if rows is None else rows, **kw)

    def test_immature_uses_byd_consumption_even_when_own_estimate_exists(self):
        r = self.est(byd_kwh_per_100=14.4, odometer_km=19000, own_observed_km=20)
        self.assertEqual(r.parts["policy"], "immature")
        self.assertEqual(r.basis, "byd-consumption")
        self.assertAlmostEqual(r.true_range_km, r.soc / 100 * USABLE / 14.4 * 100, delta=0.5)

    def test_mature_uses_only_own_data(self):
        own = re_.estimate(good_rows(), usable_kwh=USABLE)
        r = self.est(byd_kwh_per_100=14.4, odometer_km=19000, own_observed_km=60)
        self.assertEqual(r.parts["policy"], "own")
        self.assertEqual(r.true_range_km, own.true_range_km)          # not blended with anything
        self.assertNotIn("own_weight", r.parts)

    def test_mature_but_own_data_missing_falls_back_to_byd_consumption(self):
        r = self.est(rows=[{"soc": 91, "odometer_km": 19000.0}], byd_kwh_per_100=14.4, odometer_km=19000, own_observed_km=500)
        self.assertEqual(r.parts["policy"], "missing-own")
        self.assertEqual(r.basis, "byd-consumption")

    def test_threshold_is_exactly_50_km(self):
        below = self.est(byd_kwh_per_100=14.4, odometer_km=19000, own_observed_km=49.9)
        at = self.est(byd_kwh_per_100=14.4, odometer_km=19000, own_observed_km=50.0)
        self.assertEqual(below.parts["policy"], "immature")
        self.assertEqual(at.parts["policy"], "own")

    def test_byd_range_is_never_used_on_a_car_that_is_not_new(self):
        for kw in (dict(byd_kwh_per_100=14.4, own_observed_km=0), dict(byd_kwh_per_100=None, own_observed_km=0),
                   dict(byd_kwh_per_100=14.4, own_observed_km=900), dict(byd_kwh_per_100=None, own_observed_km=900)):
            for rows in (good_rows(), [{"soc": 91, "odometer_km": 19000.0}], []):
                r = self.est(rows=rows, odometer_km=19000, **kw)
                self.assertNotEqual(r.basis, "byd-new-car", kw)
                self.assertNotEqual(r.true_range_km, BYD_RANGE, kw)

    def test_no_byd_consumption_on_an_old_car_means_own_or_nothing(self):
        r = self.est(byd_kwh_per_100=None, odometer_km=19000, own_observed_km=10)
        self.assertEqual(r.parts["policy"], "own-no-byd")
        r = self.est(rows=[{"soc": 91, "odometer_km": 19000.0}], byd_kwh_per_100=None, odometer_km=19000, own_observed_km=10)
        self.assertEqual(r.status, "insufficient")
        self.assertIsNone(r.true_range_km)

    def test_new_car_may_use_byd_range_when_nothing_else_exists(self):
        r = self.est(rows=[{"soc": 95, "odometer_km": 12.0}], byd_kwh_per_100=None, odometer_km=12, own_observed_km=0)
        self.assertEqual(r.parts["policy"], "new-car")
        self.assertEqual(r.basis, "byd-new-car")
        self.assertEqual(r.true_range_km, BYD_RANGE)
        self.assertLess(r.trust_pct, 40)                                # wide and low-trust, on purpose
        self.assertLess(r.low_km, BYD_RANGE * 0.75)

    def test_new_car_prefers_byd_consumption_when_it_exists(self):
        r = self.est(rows=[{"soc": 95, "odometer_km": 40.0}], byd_kwh_per_100=14.0, odometer_km=40, own_observed_km=5)
        self.assertEqual(r.parts["policy"], "immature")
        self.assertNotEqual(r.true_range_km, BYD_RANGE)

    def test_car_at_exactly_50_km_is_not_new(self):
        r = self.est(rows=[{"soc": 95, "odometer_km": 50.0}], byd_kwh_per_100=None, odometer_km=50, own_observed_km=0)
        self.assertNotEqual(r.basis, "byd-new-car")

    def test_zero_or_missing_odometer_is_not_treated_as_a_new_car(self):
        for odo in (None, 0, -1):
            r = self.est(rows=[{"soc": 95, "odometer_km": 1.0}], byd_kwh_per_100=None, odometer_km=odo, own_observed_km=0)
            self.assertNotEqual(r.basis, "byd-new-car", odo)


class ObservedKm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "o.sqlite"
        self.conn = db.connect()
        self.t0 = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add(self, sec, odo, soc=80.0):
        db.insert_reading(self.conn, probe_row(ts=(self.t0 + dt.timedelta(seconds=sec)).isoformat(), soc=soc, odometer_km=odo))

    def test_counts_watched_driving(self):
        for i in range(11):
            self.add(i * 30, 1000.0 + i)                              # 10 km in 5 minutes
        self.assertAlmostEqual(db.observed_km(self.conn), 10.0)

    def test_a_silent_stretch_does_not_count(self):
        self.add(0, 1000.0)
        self.add(60, 1001.0)
        self.add(60 + 2640, 1013.0)                                    # 44 minutes later, 12 km further: not watched
        self.add(60 + 2670, 1014.0)
        self.assertAlmostEqual(db.observed_km(self.conn), 2.0)

    def test_owners_real_day_has_only_3_km_watched(self):
        self.add(0, 19708.0, 95)
        self.add(2640, 19720.0, 92)                                    # the 12 km nobody saw
        for i, odo in enumerate((19720, 19720, 19721, 19721, 19722, 19722, 19723, 19723)):
            self.add(2640 + 30 * (i + 1), float(odo), 91)
        self.assertAlmostEqual(db.observed_km(self.conn), 3.0)

    def test_stale_snapshots_with_a_ticking_poll_clock_do_not_make_a_silent_drive_look_watched(self):
        """The owner's real day: poll time kept advancing while BYD returned the 16:02:20 snapshot, then the car
        reported again 12 km further on. Measured on poll time that looked like 12 km in 3 minutes (228 km/h)."""
        def add(poll_s, car_s, odo, soc):
            db.insert_reading(self.conn, probe_row(ts=(self.t0 + dt.timedelta(seconds=poll_s)).isoformat(),
                                                  car_ts=(self.t0 + dt.timedelta(seconds=car_s)).isoformat(),
                                                  soc=soc, odometer_km=odo))
        add(0, 0, 19708.0, 95)
        for k in range(1, 7):                                         # six polls, same old car snapshot
            add(k * 30, 19, 19708.0, 95)
        add(240, 2640, 19720.0, 92)                                   # car reports again: 44 min on ITS clock
        add(270, 2670, 19721.0, 91)
        add(300, 2700, 19722.0, 91)
        self.assertAlmostEqual(db.observed_km(self.conn), 2.0)        # only the two watched km, not 14

    def test_falls_back_to_poll_time_when_there_is_no_car_timestamp(self):
        for i in range(6):
            db.insert_reading(self.conn, probe_row(ts=(self.t0 + dt.timedelta(seconds=30 * i)).isoformat(), car_ts="",
                                                  soc=80.0, odometer_km=1000.0 + i))
        self.assertAlmostEqual(db.observed_km(self.conn), 5.0)

    def test_empty_snapshots_stale_duplicates_and_impossible_jumps_do_not_count(self):
        self.add(0, 1000.0)
        self.add(30, 0.0, soc=0.0)                                     # BYD empty snapshot
        self.add(60, 1000.0)                                           # stale duplicate (no movement)
        self.add(90, 1500.0)                                           # 500 km in 30 s: bad data
        self.assertEqual(db.observed_km(self.conn), 0.0)

    def test_empty_database(self):
        self.assertEqual(db.observed_km(self.conn), 0.0)

    def test_odometer_going_backwards_does_not_count(self):
        self.add(0, 1000.0)
        self.add(30, 990.0)
        self.assertEqual(db.observed_km(self.conn), 0.0)


if __name__ == "__main__":
    unittest.main()
