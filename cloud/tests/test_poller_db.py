"""Tests for poller parsing + db migration, using a fake client (no BYD account, no network)."""
import asyncio
import enum
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import poller  # noqa: E402


# IntEnums shaped like pyBYD's: str(member) is '2', NOT 'ON' (this is what broke the old substring check).
class VehicleState(enum.IntEnum):
    UNKNOWN = -1
    OFF = 0
    ON = 2


class PowerGear(enum.IntEnum):
    UNKNOWN = -1
    OFF = 1
    ON = 3


class ChargingState(enum.IntEnum):
    UNKNOWN = -1
    NOT_CHARGING = 0
    CHARGING = 1
    CONNECTED = 15


class FakeClient:
    def __init__(self, rt, near=None, energy_raises=False):
        self.rt, self.near, self.energy_raises = rt, near, energy_raises
        self.calls = []

    async def get_vehicle_realtime(self, vin, **kw):
        self.calls.append("realtime")
        self.rt_kwargs = kw
        return self.rt

    async def get_gps_info(self, vin):
        self.calls.append("gps")
        return NS(latitude=1.0, longitude=2.0)

    async def get_energy_consumption(self, vin):
        self.calls.append("energy")
        if self.energy_raises:
            raise RuntimeError("boom")
        return NS(nearest_energy_consumption=self.near)


def _rt(**kw):
    base = dict(elec_percent=70, total_mileage=1000, endurance_mileage=300, speed=0, gl=0,
                charge_state=None, charging_state=None, vehicle_state=None, power_gear=None,
                timestamp=None,
                recent_50km_energy="15.5", recent_50km_energy_ev=None)
    base.update(kw)
    return NS(**base)


class Parse(unittest.TestCase):
    def test_rate(self):
        self.assertEqual(poller._rate("15.5"), 15.5)
        self.assertEqual(poller._rate(15.5), 15.5)
        for bad in ("--", "", "-", None, "abc", 0, "0", -3):
            self.assertIsNone(poller._rate(bad), bad)


class Migration(unittest.TestCase):
    def test_old_db_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                        "soc REAL, odometer_km REAL, charging_state TEXT, recent_50km TEXT)")
            old.execute("INSERT INTO readings (ts, soc, odometer_km) VALUES ('t', 50, 10)")
            old.commit(); old.close()
            db.DB_PATH = path
            c = db.connect()
            cols = {r[1] for r in c.execute("PRAGMA table_info(readings)")}
            self.assertTrue({"r50_rate", "r50_kwh", "r50_air_pct"} <= cols)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1)  # data kept
            c.close()


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "t.sqlite"
        self.conn = db.connect()
        poller._last_energy_fetch = float("-inf")
        poller._last_active = float("-inf")
        poller._last_charging = float("-inf")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _row(self):
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute("SELECT * FROM readings ORDER BY id DESC LIMIT 1").fetchone()


class PollOnce(_DbCase):
    def test_stores_rate_kwh_and_air(self):
        near = NS(ev_consumption=7.8, avg_ev_consumption=15.6, air_distribution=40)
        asyncio.run(poller.poll_once(FakeClient(_rt(), near), "VIN1234", self.conn))
        r = self._row()
        self.assertEqual(r["r50_rate"], 15.5)
        self.assertEqual(r["r50_kwh"], 7.8)
        self.assertEqual(r["r50_air_pct"], 40.0)
        self.assertEqual(r["vin_tail"], "1234")

    def test_dashes_become_null_and_energy_fills_rate(self):
        near = NS(ev_consumption=None, avg_ev_consumption=14.2, air_distribution=None)
        asyncio.run(poller.poll_once(FakeClient(_rt(recent_50km_energy="--"), near), "V", self.conn))
        r = self._row()
        self.assertEqual(r["r50_rate"], 14.2)  # fell back to the energy endpoint
        self.assertIsNone(r["r50_kwh"])

    def test_energy_failure_does_not_break_poll(self):
        asyncio.run(poller.poll_once(FakeClient(_rt(), energy_raises=True), "V", self.conn))
        r = self._row()
        self.assertEqual(r["soc"], 70)
        self.assertEqual(r["r50_rate"], 15.5)
        self.assertIsNone(r["r50_kwh"])

    def test_energy_endpoint_is_rate_limited(self):
        near = NS(ev_consumption=7.8, avg_ev_consumption=15.6, air_distribution=40)
        c = FakeClient(_rt(), near)
        asyncio.run(poller.poll_once(c, "V", self.conn))
        asyncio.run(poller.poll_once(c, "V", self.conn))
        self.assertEqual(c.calls.count("energy"), 1)
        self.assertEqual(c.calls.count("realtime"), 2)

    def test_interval_driving_vs_parked(self):
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        self.assertEqual(asyncio.run(poller.poll_once(FakeClient(_rt(speed=50), near), "V", self.conn)),
                         poller.FAST_INTERVAL)
        poller._last_active = float("-inf")  # no recent driving (grace period is tested in PollCadence)
        self.assertEqual(asyncio.run(poller.poll_once(FakeClient(_rt(speed=0), near), "V", self.conn)),
                         poller.SLOW_INTERVAL)


class DrivingAndChargingDetection(unittest.TestCase):
    def test_the_name_is_used_never_str_of_the_enum(self):
        # str(IntEnum member) is "2" on Python 3.11+ and "VehicleState.ON" on 3.10: version-dependent, so the app never uses it.
        self.assertEqual(poller._name(VehicleState.ON), "ON")
        self.assertEqual(poller._name(PowerGear.OFF), "OFF")
        self.assertEqual(poller._name(None), "")
        if sys.version_info >= (3, 11):
            self.assertEqual(str(VehicleState.ON), "2")                     # the trap, where it exists

    def test_probe_snapshot_is_parked(self):
        # Exact values from the first real probe: plugged in, parked, but vehicle_state reads ON.
        rt = _rt(speed=0.0, vehicle_state=VehicleState.ON, power_gear=PowerGear.OFF,
                 charge_state=ChargingState.CONNECTED, charging_state=ChargingState.UNKNOWN)
        self.assertFalse(poller._is_driving(rt))

    def test_moving_is_driving(self):
        self.assertTrue(poller._is_driving(_rt(speed=40, power_gear=PowerGear.OFF)))

    def test_stopped_at_light_with_gear_on_is_driving(self):
        self.assertTrue(poller._is_driving(_rt(speed=0, power_gear=PowerGear.ON)))

    def test_vehicle_state_alone_never_means_driving(self):
        self.assertFalse(poller._is_driving(_rt(speed=0, vehicle_state=VehicleState.ON)))

    def test_connected_is_not_charging(self):
        rt = _rt(charge_state=ChargingState.CONNECTED, charging_state=ChargingState.UNKNOWN)
        self.assertEqual(poller._charge_name(rt), "CONNECTED")

    def test_charging_name_prefers_informative(self):
        rt = _rt(charge_state=ChargingState.UNKNOWN, charging_state=ChargingState.CHARGING)
        self.assertEqual(poller._charge_name(rt), "CHARGING")
        self.assertEqual(poller._charge_name(_rt()), "")


class PollCadence(_DbCase):
    def _poll(self, **kw):
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        return asyncio.run(poller.poll_once(FakeClient(_rt(**kw), near), "V", self.conn))

    def test_red_light_does_not_drop_to_hourly(self):
        self.assertEqual(self._poll(speed=50, power_gear=PowerGear.ON), poller.FAST_INTERVAL)
        # stopped, gear still ON
        self.assertEqual(self._poll(speed=0, power_gear=PowerGear.ON), poller.FAST_INTERVAL)

    def test_grace_period_then_slow(self):
        self.assertEqual(self._poll(speed=50, power_gear=PowerGear.ON), poller.FAST_INTERVAL)
        # car switched off right after driving: still fast within grace window
        self.assertEqual(self._poll(speed=0, power_gear=PowerGear.OFF), poller.FAST_INTERVAL)
        # pretend the grace window has passed
        poller._last_active -= poller.ACTIVE_GRACE + 1
        self.assertEqual(self._poll(speed=0, power_gear=PowerGear.OFF), poller.SLOW_INTERVAL)

    def test_parked_from_start_is_slow(self):
        self.assertEqual(self._poll(speed=0, power_gear=PowerGear.OFF,
                                    vehicle_state=VehicleState.ON), poller.SLOW_INTERVAL)

    def test_logs_gear_and_state_names(self):
        self._poll(speed=0, power_gear=PowerGear.OFF, vehicle_state=VehicleState.ON,
                   charge_state=ChargingState.CONNECTED)
        r = self._row()
        self.assertEqual((r["gear"], r["vehicle_state"], r["charging_state"]), ("OFF", "ON", "CONNECTED"))


class ViewerWake(_DbCase):
    def _flag(self):
        return db.DB_PATH.parent / "viewer_active"

    def test_viewer_active_reflects_flag_file_age(self):
        self.assertFalse(poller.viewer_active())                       # no file
        self._flag().touch()
        self.assertTrue(poller.viewer_active())
        import time as _t
        old = _t.time() - poller.VIEWER_WINDOW - 5
        os.utime(self._flag(), (old, old))
        self.assertFalse(poller.viewer_active())                       # stale

    def test_parked_with_viewer_polls_every_two_minutes_not_fifteen(self):
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        parked = lambda: asyncio.run(poller.poll_once(FakeClient(_rt(speed=0, power_gear=PowerGear.OFF), near), "V", self.conn))
        self.assertEqual(parked(), poller.SLOW_INTERVAL)
        self._flag().touch()
        poller._last_active = float("-inf")
        self.assertEqual(parked(), poller.VIEWER_PARKED_INTERVAL)
        self.assertLess(poller.VIEWER_PARKED_INTERVAL, poller.SLOW_INTERVAL)

    def test_driving_stays_fast_regardless(self):
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        self._flag().touch()
        self.assertEqual(asyncio.run(poller.poll_once(FakeClient(_rt(speed=50), near), "V", self.conn)),
                         poller.FAST_INTERVAL)

    def test_sleep_times_out_without_viewer(self):
        import time as _t
        r = asyncio.run(poller.sleep_interruptible(0.3, _t.monotonic(), min_gap=0.0, tick=0.05))
        self.assertEqual(r, "timeout")

    def test_sleep_wakes_early_when_app_is_opened(self):
        import time as _t

        async def scenario():
            started = _t.monotonic()
            async def open_app():
                await asyncio.sleep(0.2)
                self._flag().touch()
            asyncio.ensure_future(open_app())
            t0 = _t.monotonic()
            r = await poller.sleep_interruptible(30.0, started, min_gap=0.1, tick=0.05)
            return r, _t.monotonic() - t0

        r, took = asyncio.run(scenario())
        self.assertEqual(r, "viewer")
        self.assertLess(took, 3.0)          # woke long before the 30 s deadline

    def test_sleep_respects_min_gap_so_we_do_not_spam_the_car(self):
        import time as _t

        async def scenario():
            self._flag().touch()            # app already open
            started = _t.monotonic()
            t0 = _t.monotonic()
            r = await poller.sleep_interruptible(30.0, started, min_gap=0.4, tick=0.05)
            return r, _t.monotonic() - t0

        r, took = asyncio.run(scenario())
        self.assertEqual(r, "viewer")
        self.assertGreaterEqual(took, 0.35)  # waited for the minimum gap since the last poll


class StaleSnapshot(_DbCase):
    def _poll(self, ts, **kw):
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        c = FakeClient(_rt(timestamp=ts, **kw), near)
        r = asyncio.run(poller.poll_once(c, "V", self.conn))
        return r, c

    def _count(self):
        return self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def test_waits_longer_than_pybyd_default_for_a_slow_wake(self):
        _, c = self._poll("2026-11-08 16:02:20+00:00")
        self.assertGreaterEqual(c.rt_kwargs["poll_attempts"], 20)   # about 30 s, not the pyBYD 15 s

    def test_same_car_timestamp_is_not_stored_twice(self):
        self._poll("2026-11-08 16:02:20+00:00")
        self.assertEqual(self._count(), 1)
        poller._last_energy_fetch = float("-inf")
        r, _ = self._poll("2026-11-08 16:02:20+00:00")                # car did not report: old snapshot
        self.assertEqual(self._count(), 1)                              # nothing new stored
        self.assertIn(r, (poller.SLOW_INTERVAL, poller.VIEWER_PARKED_INTERVAL))

    def test_new_car_timestamp_is_stored(self):
        self._poll("2026-11-08 16:02:20+00:00")
        self._poll("2026-11-08 16:45:00+00:00")
        self.assertEqual(self._count(), 2)

    def test_stale_snapshot_does_not_refresh_driving_grace(self):
        self._poll("t1", speed=0)
        poller._last_active = float("-inf")
        self._poll("t1", speed=50)       # stale snapshot claiming speed: must be ignored entirely
        self.assertEqual(poller._last_active, float("-inf"))


class EmptySnapshot(_DbCase):
    """BYD answers a sleeping car with zeros and no timestamp. That is not a reading."""

    def _poll(self, **kw):
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        c = FakeClient(_rt(**kw), near)
        return asyncio.run(poller.poll_once(c, "V", self.conn))

    def _count(self):
        return self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def test_all_zero_snapshot_is_not_stored(self):
        r = self._poll(elec_percent=0.0, total_mileage=0.0, endurance_mileage=0.0, timestamp=None)
        self.assertEqual(self._count(), 0)
        self.assertIn(r, (poller.SLOW_INTERVAL, poller.VIEWER_PARKED_INTERVAL))

    def test_missing_values_are_not_stored(self):
        self._poll(elec_percent=None)
        self._poll(total_mileage=None)
        self.assertEqual(self._count(), 0)

    def test_a_normal_reading_is_still_stored(self):
        self._poll(timestamp="2026-11-08 16:00:00+00:00")
        self.assertEqual(self._count(), 1)


class PollerPrintsEveryResult(_DbCase):
    """The log line must agree with the page: provisional results are printed, and a missing consumption figure
    (a brand-new car) must not crash the poll."""

    def _run(self, **kw):
        import io
        import contextlib
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(poller.poll_once(FakeClient(_rt(**kw), near), "V", self.conn))
        return buf.getvalue()

    def test_immature_byd_consumption_result_is_printed_with_its_reason(self):
        out = self._run(timestamp="2026-11-08 16:00:00+00:00")
        self.assertIn("true-range", out)
        self.assertNotIn("not enough driving data", out)
        self.assertIn("immature", out)
        self.assertIn("(not used)", out)                                 # BYD's range is shown but flagged as unused

    def test_new_car_without_consumption_figure_does_not_crash(self):
        out = self._run(timestamp="2026-11-08 16:00:00+00:00", total_mileage=12.0, recent_50km_energy=None,
                        recent_50km_energy_ev=None, endurance_mileage=380.0)
        self.assertIn("new-car", out)
        self.assertIn("no consumption figure", out)

    def test_nothing_to_estimate_says_so(self):
        out = self._run(timestamp="2026-11-08 16:00:00+00:00", recent_50km_energy=None, recent_50km_energy_ev=None)
        self.assertIn("no estimate possible yet", out)


class ChargingCadence(_DbCase):
    """While charging, poll every 5 minutes so battery ticks (and so power) are resolved."""

    def _poll(self, **kw):
        near = NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        return asyncio.run(poller.poll_once(FakeClient(_rt(**kw), near), "V", self.conn))

    def test_byd_charging_state_speeds_up_polling(self):
        r = self._poll(timestamp="2026-11-08 20:00:00+00:00", speed=0, charge_state=ChargingState.CHARGING)
        self.assertEqual(r, poller.CHARGE_INTERVAL)
        self.assertLess(poller.CHARGE_INTERVAL, poller.SLOW_INTERVAL)

    def test_a_battery_rise_while_parked_speeds_up_polling_even_without_a_label(self):
        db.insert_reading(self.conn, {"ts": "2026-11-08T19:00:00+00:00", "car_ts": "2026-11-08 19:00:00+00:00",
                                      "soc": 50.0, "odometer_km": 1000.0})
        r = self._poll(timestamp="2026-11-08 19:30:00+00:00", elec_percent=52, total_mileage=1000.0, speed=0)
        self.assertEqual(r, poller.CHARGE_INTERVAL)

    def test_a_rise_while_driving_is_regen_not_charging(self):
        db.insert_reading(self.conn, {"ts": "2026-11-08T19:00:00+00:00", "car_ts": "2026-11-08 19:00:00+00:00",
                                      "soc": 50.0, "odometer_km": 1000.0})
        r = self._poll(timestamp="2026-11-08 19:00:30+00:00", elec_percent=51, total_mileage=1001.0, speed=40,
                       power_gear=PowerGear.ON)
        self.assertEqual(r, poller.FAST_INTERVAL)                       # driving cadence, not the charging one
        self.assertEqual(poller._last_charging, float("-inf"))

    def test_a_rise_with_the_car_moved_is_not_charging(self):
        db.insert_reading(self.conn, {"ts": "2026-11-08T19:00:00+00:00", "car_ts": "2026-11-08 19:00:00+00:00",
                                      "soc": 50.0, "odometer_km": 1000.0})
        self._poll(timestamp="2026-11-08 19:30:00+00:00", elec_percent=52, total_mileage=1010.0, speed=0)
        self.assertEqual(poller._last_charging, float("-inf"))

    def test_cadence_holds_through_a_plateau_then_relaxes(self):
        self._poll(timestamp="2026-11-08 20:00:00+00:00", speed=0, charge_state=ChargingState.CHARGING)
        self.assertEqual(self._poll(timestamp="2026-11-08 20:05:00+00:00", elec_percent=70, speed=0), poller.CHARGE_INTERVAL)
        poller._last_charging -= poller.CHARGE_GRACE + 1
        self.assertEqual(self._poll(timestamp="2026-11-08 21:00:00+00:00", elec_percent=70, speed=0), poller.SLOW_INTERVAL)

    def test_not_charging_keeps_the_slow_cadence(self):
        self.assertEqual(self._poll(timestamp="2026-11-08 20:00:00+00:00", speed=0), poller.SLOW_INTERVAL)

def load_fresh(name):
    """A new copy of cloud/<name>.py, so its INITIAL module-level values can be checked (other tests change the shared copy)."""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / (name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_fresh", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FreshBoot(_DbCase):
    """On Linux time.monotonic() counts from boot. A "never happened" timestamp of 0.0 then means "just now" for the first minutes
    of uptime, and the energy reading was skipped. Simulate a machine that booted 10 seconds ago."""

    def test_nothing_starts_out_looking_like_it_just_happened(self):
        fresh = load_fresh("poller")
        for name in ("_last_energy_fetch", "_last_active", "_last_charging"):
            self.assertEqual(getattr(fresh, name), float("-inf"), name)

    def test_the_energy_reading_is_fetched_on_the_first_poll_of_a_freshly_booted_machine(self):
        import time as _time
        fresh = load_fresh("poller")
        shim = NS(monotonic=lambda: 10.0, time=_time.time)                          # up for 10 seconds
        near = NS(ev_consumption=7.8, avg_ev_consumption=15.6, air_distribution=40)
        c = FakeClient(_rt(timestamp="2026-11-09 05:00:00+00:00"), near)
        with unittest.mock.patch.object(fresh, "time", shim):
            asyncio.run(fresh.poll_once(c, "V", self.conn))
        self.assertIn("energy", c.calls)
        self.assertEqual(self.conn.execute("SELECT r50_kwh FROM readings").fetchone()[0], 7.8)


if __name__ == "__main__":
    unittest.main()
