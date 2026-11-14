"""Parked battery drain: what the displayed battery loses while the car sits still and unplugged."""
import datetime as dt
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import drain  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402

T0 = dt.datetime(2026, 11, 10, 20, 0, tzinfo=dt.timezone.utc)
NOW = T0 + dt.timedelta(days=3)


def rows(start, hours, soc0, soc1, odo=1000.0, every_min=15, driving=0, charging=None):
    """Readings every `every_min` minutes for `hours`, the battery falling linearly from soc0 to soc1 (whole percents)."""
    n = int(hours * 60 / every_min)
    out = []
    for i in range(n + 1):
        t = start + dt.timedelta(minutes=every_min * i)
        soc = round(soc0 + (soc1 - soc0) * i / max(n, 1))
        out.append((t.isoformat(), soc, odo, driving, charging))
    return out


class Stretches(unittest.TestCase):
    def test_a_quiet_night_is_one_stretch_with_its_drop(self):
        s = drain.stretches(rows(T0, 10, 91, 89))
        self.assertEqual(len(s), 1)
        self.assertAlmostEqual(s[0]["hours"], 10.0)
        self.assertEqual((s[0]["soc_start"], s[0]["soc_end"], s[0]["drop"]), (91, 89, 2))

    def test_the_owners_real_overnight_stretch_shows_no_drain(self):
        s = drain.summary(rows(T0, 10.2, 91, 91, every_min=30), NOW)
        self.assertEqual(s["pct_per_day"], 0.0)
        self.assertEqual(s["stretches_count"], 1)

    def test_driving_charging_a_moved_odometer_or_a_rising_battery_each_end_a_stretch(self):
        a = rows(T0, 4, 90, 88)
        b = rows(T0 + dt.timedelta(hours=4, minutes=15), 4, 88, 86)
        for split in ([(x[0], x[1], x[2], 1, None) for x in b[:1]],                       # a driving reading between them
                      [(x[0], x[1], x[2], 0, "CHARGING") for x in b[:1]],                 # a charging reading between them
                      [(x[0], x[1], x[2] + 5, 0, None) for x in b[:1]]):                  # the odometer moved
            s = drain.stretches(a + split + b[1:])
            self.assertGreaterEqual(len(s), 1)
            self.assertTrue(all(x["hours"] <= 4.1 for x in s), split)
        rise = rows(T0, 4, 80, 80) + rows(T0 + dt.timedelta(hours=4, minutes=15), 4, 82, 82)    # a battery rise while parked: charging
        self.assertTrue(all(x["hours"] <= 4.1 for x in drain.stretches(rise)))

    def test_short_stretches_are_not_counted(self):
        self.assertEqual(drain.stretches(rows(T0, 1.5, 90, 89)), [])

    def test_a_lone_reading_and_bad_rows_are_harmless(self):
        self.assertEqual(drain.stretches([(T0.isoformat(), 90, 1000.0, 0, None)]), [])
        bad = [("nonsense", 90, 1000.0, 0, None), (None, 90, 1000.0, 0, None), (T0.isoformat(), None, 1000.0, 0, None)]
        self.assertEqual(drain.stretches(bad), [])


class Summary(unittest.TestCase):
    def test_no_rate_is_shown_without_enough_parked_time(self):
        s = drain.summary(rows(T0, 5, 90, 89), NOW)
        self.assertIsNone(s["pct_per_day"])
        self.assertEqual(s["hours"], 5.0)

    def test_the_rate_is_the_drop_over_the_time_scaled_to_a_day(self):
        s = drain.summary(rows(T0, 24, 90, 88), NOW)
        self.assertEqual((s["pct_per_day"], s["drop_pct"], s["hours"]), (2.0, 2.0, 24.0))

    def test_several_stretches_add_up_and_the_latest_come_first(self):
        r = []
        for i in range(8):
            r += rows(T0 + dt.timedelta(hours=10 * i), 4, 90 - i, 90 - i, odo=1000.0 + 10 * i)
        s = drain.summary(r, T0 + dt.timedelta(days=5))
        self.assertEqual(s["stretches_count"], 8)
        self.assertEqual(len(s["recent"]), drain.RECENT)
        self.assertGreater(s["recent"][0]["start"], s["recent"][1]["start"])

    def test_old_stretches_fall_out_of_the_window(self):
        old = rows(T0 - dt.timedelta(days=30), 10, 90, 80)
        s = drain.summary(old, NOW)
        self.assertEqual((s["stretches_count"], s["pct_per_day"]), (0, None))

    def test_the_reading_density_tells_how_often_the_car_was_checked(self):
        five = drain.summary(rows(T0, 12, 90, 90, every_min=5), NOW)
        fifteen = drain.summary(rows(T0, 12, 90, 90, every_min=15), NOW)
        self.assertGreater(five["readings_per_hour"], 2 * fifteen["readings_per_hour"])

    def test_an_empty_history_is_fine(self):
        s = drain.summary([], NOW)
        self.assertEqual((s["stretches_count"], s["hours"], s["pct_per_day"], s["readings_per_hour"]), (0, 0.0, None, None))


class FromTheDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = (db.DB_PATH, settings.SETTINGS_FILE)
        db.DB_PATH = Path(self.tmp.name) / "data" / "h.sqlite"
        self.addCleanup(lambda: (setattr(db, "DB_PATH", old[0]), setattr(settings, "SETTINGS_FILE", old[1])))
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def test_it_reads_the_stored_readings(self):
        for ts, soc, odo, drv, ch in rows(T0, 12, 90, 88):
            db.insert_reading(self.conn, {"ts": ts, "soc": float(soc), "odometer_km": odo, "is_driving": drv, "charging_state": ch})
        self.conn.commit()
        s = drain.for_db(self.conn, NOW)
        self.assertEqual((s["stretches_count"], s["drop_pct"]), (1, 2.0))

    def test_a_connection_that_cannot_be_read_gives_an_empty_summary_not_an_error(self):
        class Broken:
            def execute(self, *a, **k):
                raise RuntimeError("database is locked")
        self.assertEqual(drain.for_db(Broken(), NOW)["stretches_count"], 0)


class InTheApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = (db.DB_PATH, settings.SETTINGS_FILE)
        db.DB_PATH = Path(self.tmp.name) / "data" / "h.sqlite"
        self.addCleanup(lambda: (setattr(db, "DB_PATH", old[0]), setattr(settings, "SETTINGS_FILE", old[1])))

    def get(self, path):
        srv = server.make_server("127.0.0.1", 0, token=None, capacity_kwh=49.92, usable_fraction=1.0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read())

    def test_the_battery_api_carries_the_parked_drain_even_with_no_history(self):
        st, d = self.get("/api/battery")
        self.assertEqual(st, 200)
        pd = d["parked_drain"]
        for k in ("days", "stretches_count", "hours", "drop_pct", "pct_per_day", "readings_per_hour", "recent"):
            self.assertIn(k, pd)
        self.assertIsNone(pd["pct_per_day"])

    def test_it_never_carries_a_coordinate(self):
        db.connect().close()
        st, d = self.get("/api/battery")
        self.assertNotIn("lat", json.dumps(d["parked_drain"]).lower())


if __name__ == "__main__":
    unittest.main()
