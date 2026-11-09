"""Regen vs charging, and regen figures on the trip page."""
import asyncio
import datetime as dt
import os
import random
import statistics
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import charging  # noqa: E402
import db  # noqa: E402
import poller  # noqa: E402
import trips  # noqa: E402
import test_charging as tcg  # noqa: E402
import test_poller_db as tpd  # noqa: E402
import test_trips as tt  # noqa: E402

USABLE = 49.92
T0 = dt.datetime(2026, 11, 8, 12, 0, 0, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------------------------------------ the power process --
def power_process(rng, seconds):
    """1 s battery power in W (positive = into the battery): accelerate, cruise, brake (regen), stand."""
    p = []
    while len(p) < seconds:
        ph = rng.choices(["acc", "cruise", "brake", "stop"], weights=[3, 5, 3, 2])[0]
        if ph == "acc":
            n, lo, hi = rng.randint(5, 15), -60000, -25000
        elif ph == "cruise":
            n, lo, hi = rng.randint(20, 70), -15000, -6000
        elif ph == "brake":
            n, lo, hi = rng.randint(3, 10), 8000, 40000
        else:
            n, lo, hi = rng.randint(10, 40), -1500, -500
        base = rng.uniform(lo, hi)
        p += [base * rng.uniform(0.8, 1.2) for _ in range(n)]
    return p[:seconds]


def prepared_rows(P, poll=30, offset=0):
    rows = []
    for k, sec in enumerate(range(offset, len(P), poll)):
        rows.append({"t": T0 + dt.timedelta(seconds=sec), "soc": 80.0, "odo": 1000.0 + k, "speed": 40.0, "driving": True,
                     "lat": None, "lon": None, "r50": None, "power_w": P[sec], "charge": False})
    return rows


class RegenFigures(unittest.TestCase):
    def test_estimates_are_calibrated_across_many_simulated_drives(self):
        rng = random.Random(5)
        reg_cov = used_cov = net_cov = n_reg = n = 0
        none_seen = 0
        for _ in range(500):
            P = power_process(rng, rng.randint(900, 3000))
            true_reg = sum(max(x, 0) for x in P) / 3.6e6
            true_use = sum(max(-x, 0) for x in P) / 3.6e6
            s = trips.regen_stats(prepared_rows(P, 30, rng.randint(0, 29)), USABLE, None)
            self.assertIsNotNone(s)
            n += 1
            used_cov += abs(s["used_kwh"] - true_use) <= 2 * s["used_pm"]
            net_cov += abs(s["net_kwh"] - (true_use - true_reg)) <= 2 * s["net_pm"]
            if s["regen_pm"] is None:
                none_seen += 1
                continue
            n_reg += 1
            reg_cov += abs(s["regen_kwh"] - true_reg) <= 2 * s["regen_pm"]
        self.assertGreaterEqual(used_cov / n, 0.93)
        self.assertGreaterEqual(net_cov / n, 0.93)
        self.assertGreaterEqual(reg_cov / n_reg, 0.93)                   # calibrated once a burst has been caught
        self.assertLess(none_seen / n, 0.10)                            # and the "none seen" case is rare

    def test_no_burst_caught_says_none_seen_not_zero_plus_minus_zero(self):
        P = [-12000.0] * 1200                                          # steady cruise, never regenerates
        s = trips.regen_stats(prepared_rows(P), USABLE, None)
        self.assertEqual(s["regen_samples"], 0)
        self.assertEqual(s["regen_kwh"], 0.0)
        self.assertIsNone(s["regen_pm"])                                # unknown, NOT 0 +/- 0
        self.assertIsNone(s["regen_share_pct"])
        self.assertAlmostEqual(s["used_kwh"], 12 * 1200 / 3600, delta=0.05)

    def test_too_few_samples_gives_nothing(self):
        P = [-5000.0, 20000.0] * 4
        rows = prepared_rows(P, poll=1)
        self.assertEqual(len(rows), 8)
        self.assertIsNotNone(trips.regen_stats(rows, USABLE, None))
        self.assertIsNone(trips.regen_stats(rows[:7], USABLE, None))

    def test_owners_first_real_trip(self):
        """Real values from the owner's first drive: the samples net 0.54 kWh, the battery % says 0.50 kWh."""
        powers = [-724.5, -724.5, -2380.5, -59767.5, -897.0, -20381.4, 25236.9, -8447.4, -8960.4, 8987.3, 18392.4,
                  -862.5, -12627.0, -1587.0]
        rows = [{"t": T0 + dt.timedelta(seconds=30 * i), "soc": 92.0, "odo": 20.0, "speed": 10.0, "driving": True,
                 "lat": None, "lon": None, "r50": None, "power_w": p, "charge": False} for i, p in enumerate(powers)]
        s = trips.regen_stats(rows, USABLE, used_pct=1.0)
        self.assertEqual(s["samples"], 14)
        self.assertEqual(s["regen_samples"], 3)
        self.assertAlmostEqual(s["regen_kwh"], 0.44, delta=0.01)
        self.assertAlmostEqual(s["used_kwh"], 0.98, delta=0.01)
        self.assertAlmostEqual(s["net_kwh"], 0.54, delta=0.01)
        self.assertAlmostEqual(s["soc_kwh"], 0.50, delta=0.01)
        # Agreement on ONE short trip is weak evidence (a single -60 kW sample is the whole trip), so the page says nothing:
        self.assertIsNone(s["check"])
        self.assertGreater(s["regen_pm"], 0.2)                           # and it admits that 3 bursts is thin evidence

    def test_the_check_can_tell_a_flipped_sign_on_a_long_trip_and_stays_silent_on_a_short_one(self):
        ok = bad = n = 0
        for seed in range(40):
            rng = random.Random(100 + seed)
            P = power_process(rng, 2700)
            net_true = sum(-x for x in P) / 3.6e6                       # kWh drawn net (positive = consumed)
            used_pct = net_true / USABLE * 100.0
            good = trips.regen_stats(prepared_rows(P, 30, rng.randint(0, 29)), USABLE, used_pct)
            flip = trips.regen_stats(prepared_rows([-x for x in P], 30, rng.randint(0, 29)), USABLE, used_pct)
            self.assertIsNotNone(good["check"], "a 45 min trip is long enough for the check to be able to fail")
            n += 1
            ok += good["check"] == "consistent"
            bad += flip["check"] == "inconsistent"
        self.assertGreaterEqual(ok / n, 0.90)                           # a correct sign is rarely accused
        self.assertGreaterEqual(bad / n, 0.95)                          # a flipped one is caught
        short = trips.regen_stats(prepared_rows(power_process(random.Random(1), 400), 30), USABLE, 1.0)
        self.assertIsNone(short["check"])                               # too short to judge: silent, not "agrees"

    def test_check_is_skipped_without_a_battery_figure(self):
        rows = prepared_rows(power_process(random.Random(1), 900))
        s = trips.regen_stats(rows, USABLE, None)
        self.assertIsNone(s["soc_kwh"])
        self.assertIsNone(s["check"])

    def test_standing_before_and_after_the_drive_is_not_counted(self):
        P = [-12000.0] * 600
        rows = prepared_rows(P)
        for r in (rows[0], rows[-1]):
            r["driving"] = False
            r["power_w"] = 5_000_000.0                                   # absurd anchor values must be ignored
        s = trips.regen_stats(rows, USABLE, None)
        self.assertLess(s["regen_kwh"], 0.01)

    def test_rows_without_power_are_skipped(self):
        rows = prepared_rows(power_process(random.Random(2), 900))
        for r in rows[::2]:
            r["power_w"] = None
        s = trips.regen_stats(rows, USABLE, None)
        self.assertEqual(s["samples"], len([r for r in rows if r["power_w"] is not None]))


class TripPage(unittest.TestCase):
    def rows_with_power(self, km=30, seed=3):
        rows, _, _ = tt.drive(1000, km, start_soc=90)
        rng = random.Random(seed)
        P = power_process(rng, 4000)
        for i, r in enumerate(rows):
            r["power_w"] = P[(i * 30) % 4000]
        return rows

    def test_detail_has_regen_and_a_power_series(self):
        rows = self.rows_with_power()
        trip = trips.analyze(rows, USABLE)[0]
        d = trips.detail(rows, trip["id"], USABLE)
        self.assertIsNotNone(d["regen"])
        self.assertGreaterEqual(d["regen"]["samples"], trips.MIN_REGEN_SAMPLES)
        self.assertGreater(len(d["power_series"]), 10)
        self.assertEqual(set(d["power_series"][0]), {"s", "kw"})          # no coordinates in the regen data
        self.assertNotIn("lat", str(d["power_series"]))
        self.assertNotIn("lat", str(d["regen"]))

    def test_trips_without_power_data_have_no_regen_section(self):
        rows, _, _ = tt.drive(1000, 30, start_soc=90)
        d = trips.detail(rows, trips.analyze(rows, USABLE)[0]["id"], USABLE)
        self.assertIsNone(d["regen"])
        self.assertEqual(d["power_series"], [])

    def test_a_net_gain_inside_a_trip_is_regen_not_a_charge(self):
        rows = [tt.row(0, 50, 1000), tt.row(30, 50, 1000, speed=50), tt.row(60, 55, 1001, speed=50), tt.row(90, 55, 1002)]
        t = trips.analyze(rows, USABLE)[0]
        self.assertEqual(t["quality"], "regen")
        self.assertIsNone(t["kwh"])
        self.assertIsNone(t["kwh_per_100"])

    def test_a_rise_across_an_unreported_gap_is_still_a_charge(self):
        rows = [tt.row(0, 50, 1000), tt.row(7200, 90, 1000 + 20)]
        out = trips.analyze(rows, USABLE)
        self.assertEqual([t["quality"] for t in out if not t["observed"]], ["charged"])


class ChargingIsNotRegen(unittest.TestCase):
    def test_regen_ticks_while_driving_never_make_a_charging_session(self):
        # Whole-km odometer: many consecutive driving readings share the same odometer. Three regen ticks in a row.
        rows = [tcg.row(i * 30, 60 + i, odo=19000, state="", driving=1, speed=45.0) for i in range(6)]
        self.assertEqual(charging.analyze(rows, USABLE), [])

    def test_regen_with_the_gear_flag_only_is_still_not_charging(self):
        rows = [tcg.row(i * 30, 60 + i, odo=19000, state="", driving=1, speed=20.0) for i in range(6)]
        self.assertEqual(charging.analyze(rows, USABLE), [])

    def test_plugged_in_with_the_gear_on_is_still_charging(self):
        rows, _ = tcg.charge(0, 40.0, 7.4, 120)
        for r in rows:
            r["is_driving"] = 1                                           # gear ON, but speed is 0
        out = charging.analyze(rows, USABLE)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["observed"])

    def test_speed_none_counts_as_stopped(self):
        rows, _ = tcg.charge(0, 40.0, 7.4, 120)
        for r in rows:
            r["speed_kmh"] = None
        self.assertEqual(len(charging.analyze(rows, USABLE)), 1)

    def test_a_downhill_then_a_charge_gives_exactly_one_session(self):
        hill = [tcg.row(i * 30, 60 + i // 2, odo=19000 + i // 4, state="", driving=1, speed=50.0) for i in range(10)]
        park_soc = hill[-1]["soc"]
        plug, _ = tcg.charge(10 * 30 + 600, park_soc, 7.4, 90, odo=19002)
        out = charging.analyze(hill + plug, USABLE)
        self.assertEqual(len([s for s in out if s["observed"]]), 1)
        self.assertGreater(out[0]["start"], hill[-1]["ts"])               # it starts after the drive, not on the hill

    def test_a_moving_reading_in_the_middle_ends_the_session(self):
        rows, soc = tcg.charge(0, 30.0, 7.4, 120)
        rows[len(rows) // 2]["speed_kmh"] = 12.0
        out = charging.analyze(rows, USABLE)
        for s in out:
            self.assertTrue(s["minutes"] is None or s["minutes"] < 120)


class PollerCadence(tpd._DbCase):
    def _poll(self, **kw):
        near = tpd.NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None)
        return asyncio.run(poller.poll_once(tpd.FakeClient(tpd._rt(**kw), near), "V", self.conn))

    def _prev(self):
        db.insert_reading(self.conn, {"ts": "2026-11-08T19:00:00+00:00", "car_ts": "2026-11-08 19:00:00+00:00",
                                      "soc": 50.0, "odometer_km": 1000.0})

    def test_plugged_in_with_the_gear_on_is_still_recognised_as_charging(self):
        self._prev()
        r = self._poll(timestamp="2026-11-08 19:30:00+00:00", elec_percent=52, total_mileage=1000.0, speed=0,
                       power_gear=tpd.PowerGear.ON)
        self.assertNotEqual(poller._last_charging, float("-inf"))        # the evidence is recorded despite the gear flag
        self.assertLessEqual(r, poller.CHARGE_INTERVAL)                  # (the gear-on 30 s cadence is even faster)

    def test_regen_while_moving_never_triggers_the_charging_cadence(self):
        self._prev()
        self._poll(timestamp="2026-11-08 19:00:30+00:00", elec_percent=51, total_mileage=1000.0, speed=30,
                   power_gear=tpd.PowerGear.ON)                          # same whole-km odometer, but moving
        self.assertEqual(poller._last_charging, float("-inf"))


if __name__ == "__main__":
    unittest.main()
