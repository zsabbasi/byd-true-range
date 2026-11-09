"""Tests for trips.py: trip detection, unobserved drives, consumption, totals, detail."""
import datetime as dt
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import trips  # noqa: E402

USABLE = 49.92 * 0.95
T0 = dt.datetime(2026, 11, 8, 16, 0, 0, tzinfo=dt.timezone.utc)


def row(sec, soc, odo, speed=0.0, driving=None, lat=None, lon=None, car_ts=True, r50=14.0):
    t = T0 + dt.timedelta(seconds=sec)
    return {
        "ts": t.isoformat(), "car_ts": t.isoformat() if car_ts is True else (car_ts or ""),
        "soc": soc, "odometer_km": odo, "speed_kmh": speed, "gear": "ON" if (driving or speed) else "OFF",
        "is_driving": 1 if (driving if driving is not None else speed > 0) else 0,
        "lat": lat, "lon": lon, "r50_rate": r50, "charging_state": "",
    }


def drive(start_sec, km, kwh_per_100=15.0, start_soc=90.0, odo0=1000.0, poll_s=30, kmh=60.0, usable=USABLE, gps=True):
    """Rows for one drive at constant consumption; SOC reported as a whole percent. Ends with a parked reading."""
    rows, soc, odo, t = [], start_soc, odo0, start_sec
    rows.append(row(t - poll_s, round(soc), round(odo)))                       # parked anchor before the drive
    done = 0.0
    while done < km:
        step = min(kmh * poll_s / 3600.0, km - done)
        t += poll_s
        done += step
        odo += step
        soc -= kwh_per_100 * step / 100.0 / usable * 100.0
        rows.append(row(t, round(soc), round(odo), speed=kmh, lat=25.2 + done * 0.001 if gps else None,
                        lon=55.3 + done * 0.001 if gps else None))
    rows.append(row(t + poll_s, round(soc), round(odo)))                       # parked end anchor
    return rows, soc, odo


class RealTimeline(unittest.TestCase):
    """The owner's first real day: the car went silent, then reported again 12 km further on."""

    def setUp(self):
        r = [row(0, 95, 8), row(19, 95, 8, car_ts=T0.replace(second=19).isoformat())]       # parked, then stale snapshot
        for i in range(5):                                                                  # six stale polls, same car_ts
            r.append({**row(2400 + i * 30, 95, 8), "car_ts": r[1]["car_ts"]})
        # car reports again, already 12 km further, gear ON
        seq = [(2745, 92, 20, 0), (2775, 92, 20, 0), (2805, 92, 20, 6), (2835, 91, 21, 52), (2865, 91, 21, 2),
               (2895, 91, 21, 15), (2925, 91, 21, 33), (2955, 91, 22, 36), (2985, 91, 22, 46), (3015, 91, 22, 29),
               (3045, 91, 22, 34), (3075, 91, 23, 5), (3105, 91, 23, 32), (3135, 91, 23, 0)]
        for sec, soc, odo, spd in seq:
            r.append(row(sec, soc, odo, speed=spd, driving=True, lat=25.2 + (sec - 2745) * 1e-5, lon=55.3))
        r.append(row(3165, 91, 23, 0, driving=False))                                       # gear OFF, parked
        r.append(row(3195, 91, 23, 0, driving=False))
        self.result = trips.analyze(r, USABLE)

    def test_two_items_newest_first(self):
        self.assertEqual(len(self.result), 2)
        newest, oldest = self.result
        self.assertTrue(newest["observed"])
        self.assertFalse(oldest["observed"])

    def test_the_silent_drive_is_reported_not_hidden(self):
        gap = self.result[1]
        self.assertEqual(gap["quality"], "unobserved")
        self.assertEqual(gap["km"], 12.0)
        self.assertEqual((gap["soc_start"], gap["soc_end"]), (95, 92))
        self.assertFalse(gap["has_route"])
        self.assertAlmostEqual(gap["kwh_per_100"], 3 / 100 * USABLE / 12 * 100, delta=0.1)
        self.assertIsNotNone(gap["kwh_per_100_pm"])               # uncertainty is given, and it is large
        self.assertGreater(gap["kwh_per_100_pm"] / gap["kwh_per_100"], 0.10)    # about 14%: a 3% drop is coarse

    def test_the_short_trip_has_no_consumption_figure(self):
        t = self.result[0]
        self.assertEqual(t["km"], 3.0)
        self.assertEqual(t["quality"], "short")
        self.assertIsNone(t["kwh_per_100"])
        self.assertTrue(t["has_route"])
        self.assertFalse(t["ongoing"])
        self.assertEqual(t["max_kmh"], 52)

    def test_stale_duplicates_do_not_create_anything(self):
        self.assertEqual(sum(1 for r in self.result if r["observed"]), 1)


class Consumption(unittest.TestCase):
    def test_long_trip_measures_consumption_with_ticks(self):
        rows, _, _ = drive(1000, 60, kwh_per_100=15.0)
        out = trips.analyze(rows, USABLE)
        self.assertEqual(len(out), 1)
        t = out[0]
        self.assertEqual(t["method"], "ticks")
        self.assertAlmostEqual(t["kwh_per_100"], 15.0, delta=1.2)
        self.assertLess(t["kwh_per_100_pm"], 2.0)
        self.assertEqual(t["quality"], "ok")
        self.assertAlmostEqual(t["km"], 60, delta=1.5)
        self.assertAlmostEqual(t["avg_kmh"], 60, delta=6)

    def test_uncertainty_shrinks_with_distance(self):
        pms = []
        for km in (25, 60, 150):
            rows, _, _ = drive(1000, km, kwh_per_100=15.0, start_soc=95)
            pms.append(trips.analyze(rows, USABLE)[0]["kwh_per_100_pm"] / 15.0)
        self.assertGreater(pms[0], pms[1])
        self.assertGreater(pms[1], pms[2])

    def test_a_battery_rise_while_moving_is_regen_not_negative(self):
        rows = [row(0, 50, 1000), row(30, 50, 1000, speed=50), row(60, 55, 1001, speed=50), row(90, 55, 1002)]
        out = trips.analyze(rows, USABLE)
        self.assertEqual(out[0]["quality"], "regen")                       # the car was moving: that is regen, not a charge
        self.assertIsNone(out[0]["kwh"])
        self.assertIsNone(out[0]["kwh_per_100"])

    def test_totals_are_accurate_even_when_every_trip_is_too_short(self):
        rng = random.Random(4)
        rows, soc, odo, t, all_short = [], 95.0, 1000.0, 0, True
        out_rows = []
        for i in range(30):                                     # 30 trips of about 4 km with a parked gap between
            d, soc, odo = drive(t, 4.0 + rng.random(), kwh_per_100=15.0, start_soc=soc, odo0=odo)
            out_rows += d
            t += 4000
        result = trips.analyze(out_rows, USABLE)
        self.assertEqual(len(result), 30)
        measured = sum(1 for r in result if r["kwh_per_100"] is not None)
        self.assertLess(measured, 20)                           # most individual trips are too short
        tot = trips.totals(result)
        self.assertAlmostEqual(tot["kwh_per_100"], 15.0, delta=1.5)
        self.assertEqual(tot["trips"], 30)


class Segmentation(unittest.TestCase):
    def test_idle_with_gear_on_is_not_a_trip(self):
        rows = [row(i * 30, 80, 1000, speed=0, driving=True) for i in range(40)]
        self.assertEqual(trips.analyze(rows, USABLE), [])

    def test_silence_mid_trip_gives_trip_gap_trip(self):
        a, soc, odo = drive(0, 20, start_soc=90, odo0=1000)
        b, _, _ = drive(1800, 20, start_soc=soc - 4, odo0=odo + 10)          # 10 km driven unobserved in between
        out = trips.analyze(a[:-1] + b[1:], USABLE)
        kinds = sorted(["trip" if r["observed"] else "gap" for r in out])
        self.assertEqual(kinds.count("trip"), 2)
        self.assertEqual(kinds.count("gap"), 1)

    def test_odometer_glitch_never_links(self):
        rows = [row(0, 80, 1000), row(30, 80, 1001, speed=50), row(60, 79, 5, speed=50), row(90, 79, 6, speed=50),
                row(120, 79, 6)]
        for t in trips.analyze(rows, USABLE):
            self.assertGreaterEqual(t["km"], 0)

    def test_ongoing_trip_is_marked(self):
        rows, _, _ = drive(0, 20, start_soc=90)
        rows = rows[:-1]                                                      # still driving at the last reading
        out = trips.analyze(rows, USABLE)
        self.assertTrue(out[0]["ongoing"])

    def test_empty_and_garbage(self):
        self.assertEqual(trips.analyze([], USABLE), [])
        junk = [{"ts": "nope", "soc": None, "odometer_km": 1}, {"ts": None}, {}]
        self.assertEqual(trips.analyze(junk, USABLE), [])

    def test_trip_start_time_is_the_drive_not_a_long_ago_parked_reading(self):
        rows = [row(0, 80, 1000), row(500, 80, 1000), row(530, 80, 1000, speed=40, driving=True),
                row(560, 79, 1001, speed=40), row(590, 79, 1002, speed=40), row(620, 79, 1002)]
        out = trips.analyze(rows, USABLE)
        self.assertEqual(len(out), 1)
        start = dt.datetime.fromisoformat(out[0]["start"])
        self.assertGreaterEqual((start - T0).total_seconds(), 500)


class Detail(unittest.TestCase):
    def setUp(self):
        self.rows, _, _ = drive(1000, 30, start_soc=90)
        self.trip = trips.analyze(self.rows, USABLE)[0]

    def test_detail_has_route_and_speed_series(self):
        d = trips.detail(self.rows, self.trip["id"], USABLE)
        self.assertGreater(len(d["points"]), 10)
        self.assertEqual(set(d["points"][0]), {"lat", "lon", "kmh"})
        self.assertGreater(len(d["speed_series"]), 10)
        self.assertEqual(d["km"], self.trip["km"])

    def test_detail_without_location_has_no_coordinates(self):
        d = trips.detail(self.rows, self.trip["id"], USABLE, include_location=False)
        self.assertEqual(d["points"], [])
        self.assertFalse(d["has_route"])
        self.assertNotIn("lat", str(d["speed_series"]))

    def test_unknown_id_is_none(self):
        self.assertIsNone(trips.detail(self.rows, 12345, USABLE))

    def test_unobserved_drive_has_no_detail_page(self):
        r = [row(0, 95, 8), row(3000, 92, 20, speed=0, driving=True), row(3030, 92, 20)]
        out = trips.analyze(r, USABLE)
        gap = [x for x in out if not x["observed"]][0]
        self.assertIsNone(trips.detail(r, gap["id"], USABLE))

    def test_invalid_gps_is_dropped(self):
        rows, _, _ = drive(1000, 20, start_soc=90, gps=False)
        for r in rows[3:8]:
            r["lat"], r["lon"] = 0.0, 0.0                                      # the (0, 0) "no fix" value
        d = trips.detail(rows, trips.analyze(rows, USABLE)[0]["id"], USABLE)
        self.assertEqual(d["points"], [])


class BadSnapshots(unittest.TestCase):
    """Row 51 of the owner's real database: BYD's empty snapshot (zeros) between two normal parked readings."""

    def test_zero_row_does_not_invent_a_19000_km_drive(self):
        rows = [row(0, 91, 19723), row(900, 0, 0), row(1800, 91, 19723)]
        rows[1]["car_ts"] = ""
        self.assertEqual(trips.analyze(rows, USABLE), [])

    def test_zero_row_in_the_middle_of_a_trip_is_ignored(self):
        rows, _, _ = drive(0, 30, start_soc=90, odo0=1000)
        mid = len(rows) // 2
        bad = row(rows_t(rows[mid]) + 1, 0, 0, car_ts="")
        rows.insert(mid + 1, bad)
        out = trips.analyze(rows, USABLE)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["km"], 30, delta=1.5)

    def test_physically_impossible_jump_is_not_a_drive(self):
        rows = [row(0, 80, 1000), row(60, 80, 9000, speed=50)]        # 8000 km in a minute
        for t in trips.analyze(rows, USABLE):
            self.assertLess(t["km"], 1000)

    def test_a_long_real_gap_with_plausible_distance_is_still_reported(self):
        rows = [row(0, 95, 1000), row(7200, 90, 1150)]                  # 150 km over 2 h = 75 km/h
        out = trips.analyze(rows, USABLE)
        self.assertEqual([t["quality"] for t in out], ["unobserved"])
        self.assertEqual(out[0]["km"], 150.0)

    def test_negative_or_null_battery_rows_are_dropped(self):
        rows = [row(0, 80, 1000), {**row(30, 0, 1001), "soc": -1}, {**row(60, 80, 1002), "soc": None}, row(90, 80, 1000)]
        self.assertEqual(trips.analyze(rows, USABLE), [])


def rows_t(r):
    return int((dt.datetime.fromisoformat(r["ts"]) - T0).total_seconds())


if __name__ == "__main__":
    unittest.main()
