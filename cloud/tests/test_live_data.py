"""Tyre pressures are recorded, and BYD's power sample reaches the status API with the limits a dashboard bar needs."""
import asyncio
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import db  # noqa: E402
import poller  # noqa: E402
import server  # noqa: E402
from test_poller_db import FakeClient, _rt  # noqa: E402
from test_server import probe_row  # noqa: E402

NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone.utc)


def iso(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.sqlite"
        self.addCleanup(lambda: setattr(db, "DB_PATH", old))
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def status(self):
        self.conn.commit()
        return server.build_status(self.conn, now=NOW)


class Recording(Base):
    def test_the_columns_exist_and_round_trip(self):
        db.insert_reading(self.conn, probe_row(tyre_lf=37.2, tyre_rf=36.9, tyre_lr=36.9, tyre_rr=37.2, tyre_unit="PSI"))
        r = self.conn.execute("SELECT tyre_lf, tyre_rf, tyre_lr, tyre_rr, tyre_unit FROM readings").fetchone()
        self.assertEqual(r, (37.2, 36.9, 36.9, 37.2, "PSI"))

    def test_an_older_database_gets_the_columns_added_and_keeps_its_data(self):
        p = Path(self.tmp.name) / "old.sqlite"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, ts TEXT, car_ts TEXT, vin_tail TEXT, soc REAL, odometer_km REAL, "
                  "byd_range_km REAL, speed_kmh REAL, power_w REAL, charging_state TEXT, recent_50km TEXT, lat REAL, lon REAL, is_driving INTEGER)")
        c.execute("INSERT INTO readings (ts, soc, odometer_km) VALUES ('2026-11-01T00:00:00+00:00', 80, 1000)")
        c.commit()
        c.close()
        db.DB_PATH = p
        conn = db.connect()
        have = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
        self.assertTrue({"tyre_lf", "tyre_rf", "tyre_lr", "tyre_rr", "tyre_unit"} <= have)
        self.assertEqual(conn.execute("SELECT soc FROM readings").fetchone()[0], 80)
        conn.close()

    def poll(self, **kw):
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        asyncio.run(poller.poll_once(FakeClient(_rt(**kw), near), "V", self.conn))
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute("SELECT * FROM readings ORDER BY id DESC LIMIT 1").fetchone()

    def test_the_poller_stores_what_pybyd_parses(self):
        class Unit:
            name = "PSI"
        row = self.poll(left_front_tire_pressure=37.2, right_front_tire_pressure=36.9, left_rear_tire_pressure=36.9,
                        right_rear_tire_pressure=37.2, tire_press_unit=Unit())
        self.assertEqual((row["tyre_lf"], row["tyre_rf"], row["tyre_lr"], row["tyre_rr"], row["tyre_unit"]), (37.2, 36.9, 36.9, 37.2, "PSI"))

    def test_placeholders_and_missing_values_are_not_stored_as_pressures(self):
        row = self.poll(left_front_tire_pressure=0, right_front_tire_pressure=-1, left_rear_tire_pressure=None)
        self.assertEqual((row["tyre_lf"], row["tyre_rf"], row["tyre_lr"], row["tyre_rr"], row["tyre_unit"]), (None, None, None, None, None))
        self.assertIsNone(poller._pressure("--"))
        self.assertEqual(poller._pressure("36.5"), 36.5)


class StatusTyres(Base):
    def test_none_until_a_reading_carries_them(self):
        db.insert_reading(self.conn, probe_row(ts=iso(5)))
        self.assertIsNone(self.status()["tyres"])

    def test_the_latest_values_with_their_age_and_the_spread(self):
        db.insert_reading(self.conn, probe_row(ts=iso(30), tyre_lf=30.0, tyre_rf=30.0, tyre_lr=30.0, tyre_rr=30.0, tyre_unit="PSI"))
        db.insert_reading(self.conn, probe_row(ts=iso(10), tyre_lf=37.2, tyre_rf=36.9, tyre_lr=33.0, tyre_rr=37.2, tyre_unit="PSI"))
        db.insert_reading(self.conn, probe_row(ts=iso(1)))                         # a newer reading without tyre data
        t = self.status()["tyres"]
        self.assertEqual((t["lf"], t["rf"], t["lr"], t["rr"], t["unit"]), (37.2, 36.9, 33.0, 37.2, "PSI"))
        self.assertEqual(t["spread"], 4.2)
        self.assertEqual(t["age_s"], 600)

    def test_an_older_database_without_the_columns_does_not_break_the_status(self):
        p = Path(self.tmp.name) / "legacy.sqlite"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, ts TEXT, car_ts TEXT, vin_tail TEXT, soc REAL, odometer_km REAL, "
                  "byd_range_km REAL, speed_kmh REAL, power_w REAL, charging_state TEXT, recent_50km TEXT, r50_rate REAL, r50_kwh REAL, "
                  "r50_air_pct REAL, lat REAL, lon REAL, is_driving INTEGER, gear TEXT, vehicle_state TEXT)")
        c.execute("INSERT INTO readings (ts, soc, odometer_km, power_w, is_driving) VALUES (?, 80, 1000, -5000, 1)", (iso(2),))
        c.commit()
        s = server.build_status(c, now=NOW)
        c.close()
        self.assertIsNone(s["tyres"])
        self.assertEqual(s["power"]["kw"], -5.0)


class StatusPower(Base):
    def add(self, minutes_ago, power_w, driving=1, charging=None, odo=1000.0):
        db.insert_reading(self.conn, probe_row(ts=iso(minutes_ago), power_w=power_w, is_driving=driving, charging_state=charging,
                                               odometer_km=odo))

    def test_the_latest_sample_keeps_its_sign_positive_into_the_battery(self):
        self.add(3, -12400.0)
        p = self.status()["power"]
        self.assertEqual((p["kw"], p["age_s"]), (-12.4, 180))
        self.add(1, 8100.0)
        self.assertEqual(self.status()["power"]["kw"], 8.1)

    def test_the_trend_is_the_last_20_minutes_oldest_first_without_charging(self):
        self.add(50, -62000.0)                                     # too old
        self.add(15, 41000.0)
        self.add(10, 6300.0, driving=1, charging="CHARGING")       # charging power is not driving power
        self.add(5, -12000.0)
        self.add(1, -3000.0)
        p = self.status()["power"]
        self.assertEqual([q["kw"] for q in p["trend"]], [41.0, -12.0, -3.0])
        self.assertEqual([q["s"] for q in p["trend"]], [-840, -240, 0])
        self.assertNotIn("drive_max_kw", p)

    def test_a_single_old_reading_gives_a_one_point_trend(self):
        self.add(2, 0.0, driving=0)
        self.assertEqual(len(self.status()["power"]["trend"]), 1)

    def test_a_reading_with_no_power_value_is_not_zero(self):
        db.insert_reading(self.conn, probe_row(ts=iso(2), power_w=None))
        self.assertIsNone(self.status()["power"]["kw"])


if __name__ == "__main__":
    unittest.main()
