"""Tests for charging.py. NOTE: no real charge has been recorded on the owner's car yet; these are simulator tests."""
import datetime as dt
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import charging  # noqa: E402

USABLE = 49.92
T0 = dt.datetime(2026, 11, 8, 20, 0, 0, tzinfo=dt.timezone.utc)


def row(sec, soc, odo=19000.0, state="CHARGING", power_w=None, driving=0, speed=0.0):
    t = (T0 + dt.timedelta(seconds=sec)).isoformat()
    return {"ts": t, "car_ts": t, "soc": float(soc), "odometer_km": float(odo), "speed_kmh": speed, "is_driving": driving,
            "power_w": power_w, "charging_state": state, "lat": None, "lon": None, "r50_rate": None}


def charge(start_s, soc0, kw, minutes, poll_s=300, odo=19000.0, state="CHARGING", with_power=False, tail=0):
    """Rows for a charge at constant kW into the battery; SOC reported as a whole percent. `tail` flat readings after."""
    rows, t, soc = [], start_s, soc0
    rows.append(row(t, round(soc), odo, state, kw * 1000 if with_power else None))
    end = start_s + minutes * 60
    prev = t
    while t < end:
        t = min(t + poll_s, end)
        soc += kw * ((t - prev) / 3600.0) / USABLE * 100.0          # the last step may be shorter than a poll
        prev = t
        rows.append(row(t, round(min(soc, 100)), odo, state, kw * 1000 if with_power else None))
    for k in range(tail):
        rows.append(row(end + (k + 1) * poll_s, round(min(soc, 100)), odo, "CONNECTED"))
    return rows, soc


class Detection(unittest.TestCase):
    def test_ac_overnight_charge(self):
        rows, soc = charge(0, 40.0, 7.4, 200, poll_s=300)
        out = charging.analyze(rows, USABLE)
        self.assertEqual(len(out), 1)
        s = out[0]
        self.assertTrue(s["observed"])
        self.assertAlmostEqual(s["gained_pct"], round(soc) - 40, delta=1)
        self.assertAlmostEqual(s["energy_kwh"], 7.4 * 200 / 60, delta=0.6)
        self.assertEqual(s["kw_method"], "ticks")
        self.assertAlmostEqual(s["avg_kw"], 7.4, delta=0.5)
        self.assertEqual(s["type_guess"], "AC")
        self.assertIsNotNone(s["avg_kw_pm"])

    def test_dc_fast_charge(self):
        rows, _ = charge(0, 20.0, 60.0, 25, poll_s=120)
        s = charging.analyze(rows, USABLE)[0]
        self.assertAlmostEqual(s["avg_kw"], 60.0, delta=6.0)
        self.assertEqual(s["type_guess"], "DC")

    def test_sparse_polling_still_works_with_a_wider_plus_minus(self):
        dense = charging.analyze(charge(0, 30.0, 7.4, 300, poll_s=300)[0], USABLE)[0]
        sparse = charging.analyze(charge(0, 30.0, 7.4, 300, poll_s=900)[0], USABLE)[0]
        self.assertAlmostEqual(sparse["avg_kw"], 7.4, delta=1.2)
        self.assertGreater(sparse["avg_kw_pm"], dense["avg_kw_pm"])

    def test_power_uncertainty_is_honest_across_many_charges(self):
        rng = random.Random(3)
        inside = n = 0
        for i in range(60):
            kw = rng.uniform(4, 11)
            s = charging.analyze(charge(0, rng.uniform(15, 40), kw, rng.uniform(120, 260), poll_s=rng.choice([180, 300, 600]))[0], USABLE)
            if not s or s[0]["avg_kw"] is None:
                continue
            n += 1
            inside += abs(s[0]["avg_kw"] - kw) <= 2 * s[0]["avg_kw_pm"] + 0.3
        self.assertGreater(n, 40)
        self.assertGreaterEqual(inside / n, 0.85)

    def test_trailing_flat_readings_do_not_stretch_the_session(self):
        rows, _ = charge(0, 60.0, 7.4, 120, poll_s=300, tail=40)               # then 3+ h plugged in at 100%
        s = charging.analyze(rows, USABLE)[0]
        self.assertLess(s["minutes"], 135)

    def test_battery_jitter_is_not_a_charge(self):
        rows = [row(i * 300, 80 + (i % 2), state="") for i in range(30)]
        self.assertEqual(charging.analyze(rows, USABLE), [])

    def test_byds_charging_state_alone_is_a_session_but_a_single_reading_is_not(self):
        # Verified on the owner's first real charge: BYD's CHARGING label is trustworthy. Two readings make a session.
        rows = [row(i * 300, 80, state="CHARGING") for i in range(20)]
        self.assertEqual(len(charging.analyze(rows, USABLE)), 1)
        self.assertEqual(charging.analyze([row(0, 80, state="CONNECTED"), row(300, 80, state="CHARGING"),
                                           row(600, 80, state="CONNECTED")], USABLE), [])

    def test_a_drive_ends_the_session(self):
        a, soc = charge(0, 30.0, 7.4, 120)
        drive = [row(a[-1]["_t"] if "_t" in a[-1] else 8000 + i * 30, round(soc) - i // 4, odo=19000 + i, driving=1, speed=50.0, state="")
                 for i in range(1, 12)]
        b, _ = charge(20000, round(soc) - 3, 7.4, 90, odo=19011)
        out = charging.analyze(a + drive + b, USABLE)
        self.assertEqual(sum(1 for s in out if s["observed"]), 2)

    def test_two_plug_ins_with_a_short_break_merge(self):
        a, soc = charge(0, 30.0, 7.4, 60)
        b, _ = charge(60 * 60 + 1200, soc, 7.4, 60)                           # 20 min pause: same session
        out = charging.analyze(a + b, USABLE)
        self.assertEqual(len([s for s in out if s["observed"]]), 1)

    def test_a_charge_nobody_watched_is_reported_not_hidden(self):
        rows = [row(0, 30, state="CONNECTED"), row(8 * 3600, 95, state="CONNECTED"), row(8 * 3600 + 300, 95, state="CONNECTED")]
        out = charging.analyze(rows, USABLE)
        self.assertEqual(len(out), 1)
        s = out[0]
        self.assertFalse(s["observed"])
        self.assertEqual(s["quality"], "not watched")
        self.assertEqual(s["gained_pct"], 65)
        self.assertAlmostEqual(s["energy_kwh"], 0.65 * USABLE, places=1)
        self.assertIsNone(s["avg_kw"])
        self.assertIsNone(s["minutes"])

    def test_silence_in_the_middle_gives_watched_gap_watched(self):
        a, soc = charge(0, 20.0, 7.4, 60)
        gap_rows = [row(a[-1]["_x"] if "_x" in a[-1] else 3600 + 3 * 3600, round(soc) + 20)]
        b, _ = charge(3600 + 3 * 3600 + 300, round(soc) + 20, 7.4, 60)
        out = charging.analyze(a + gap_rows + b, USABLE)
        kinds = sorted("watched" if s["observed"] else "gap" for s in out)
        self.assertEqual(kinds.count("gap"), 1)
        self.assertGreaterEqual(kinds.count("watched"), 1)

    def test_a_big_fall_ends_it_and_a_one_percent_dip_does_not(self):
        rows, _ = charge(0, 30.0, 7.4, 120)
        i = len(rows) // 2
        rows[i]["soc"] = rows[i - 1]["soc"] - 1                               # display dips 1%
        s = charging.analyze(rows, USABLE)
        self.assertEqual(len([x for x in s if x["observed"]]), 1)

    def test_stale_duplicates_and_empty_snapshots_are_ignored(self):
        rows, _ = charge(0, 40.0, 7.4, 120)
        clean = charging.analyze(rows, USABLE)[0]
        dirty = list(rows)
        dirty.insert(5, {**rows[4]})                                           # same car_ts: stale snapshot
        dirty.insert(9, {**rows[8], "soc": 0.0, "odometer_km": 0.0, "car_ts": ""})
        out = charging.analyze(dirty, USABLE)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["gained_pct"], clean["gained_pct"])

    def test_ongoing_when_data_ends_mid_charge(self):
        rows, _ = charge(0, 40.0, 7.4, 90)
        self.assertTrue(charging.analyze(rows, USABLE)[0]["ongoing"])
        rows2, _ = charge(0, 40.0, 7.4, 90, tail=6)
        self.assertFalse(charging.analyze(rows2, USABLE)[0]["ongoing"])


class Numbers(unittest.TestCase):
    def setUp(self):
        self.rows, _ = charge(0, 40.0, 7.4, 200, with_power=True)

    def test_battery_percent_energy_scales_with_usable_capacity(self):
        rows, _ = charge(0, 40.0, 7.4, 200, with_power=False)                  # no power readings: energy is gain % x usable
        a = charging.analyze(rows, 49.92)[0]
        b = charging.analyze(rows, 55.0)[0]
        self.assertEqual(a["energy_method"], "battery %")
        self.assertAlmostEqual(b["energy_kwh"] / a["energy_kwh"], 55.0 / 49.92, places=2)

    def test_battery_percent_energy_uncertainty_is_stated(self):
        rows, _ = charge(0, 40.0, 7.4, 200, with_power=False)
        s = charging.analyze(rows, USABLE)[0]
        self.assertAlmostEqual(s["energy_pm_kwh"], 0.408 * USABLE / 100, delta=0.01)

    def test_with_power_readings_energy_comes_from_them(self):
        s = charging.analyze(self.rows, USABLE)[0]
        self.assertEqual(s["energy_method"], "BYD power")
        self.assertAlmostEqual(s["energy_kwh"], 7.4 * 200 / 60, delta=0.5)

    def test_cost_needs_a_tariff_and_nothing_is_assumed(self):
        s = charging.analyze(self.rows, USABLE)[0]
        self.assertIsNone(s["cost"])
        self.assertIsNone(s["wall_kwh"])
        t = charging.analyze(self.rows, USABLE, tariff=0.30)[0]
        self.assertAlmostEqual(t["cost"], t["energy_kwh"] * 0.30, places=2)
        self.assertAlmostEqual(t["wall_kwh"], t["energy_kwh"], places=2)       # loss defaults to 0: battery-side

    def test_charger_loss_is_the_owners_number(self):
        t = charging.analyze(self.rows, USABLE, tariff=0.30, loss_pct=10.0)[0]
        self.assertAlmostEqual(t["wall_kwh"], t["energy_kwh"] / 0.9, places=2)
        self.assertAlmostEqual(t["cost"], t["wall_kwh"] * 0.30, places=2)

    def test_sampled_power_is_reported_separately(self):
        s = charging.analyze(self.rows, USABLE)[0]
        self.assertAlmostEqual(s["sampled_kw"], 7.4, delta=0.1)

    def test_totals(self):
        a, soc = charge(0, 30.0, 7.4, 120)
        b, _ = charge(40000, 50.0, 7.4, 90)
        out = charging.analyze(a + b, USABLE, tariff=0.25)
        tot = charging.totals(out)
        self.assertEqual(tot["sessions"], 2)
        self.assertAlmostEqual(tot["energy_kwh"], sum(s["energy_kwh"] for s in out), delta=0.1)
        self.assertAlmostEqual(tot["cost"], sum(s["cost"] for s in out), delta=0.02)
        self.assertIsNone(charging.totals(charging.analyze(a + b, USABLE))["cost"])

    def test_empty_and_garbage(self):
        self.assertEqual(charging.analyze([], USABLE), [])
        self.assertEqual(charging.analyze([{"ts": "x"}, {}], USABLE), [])
        self.assertEqual(charging.totals([])["sessions"], 0)


class Detail(unittest.TestCase):
    def setUp(self):
        self.rows, _ = charge(0, 40.0, 7.4, 120, with_power=True)
        self.s = charging.analyze(self.rows, USABLE)[0]

    def test_curve_ticks_and_power_samples(self):
        d = charging.detail(self.rows, self.s["id"], USABLE)
        self.assertGreater(len(d["curve"]), 10)
        self.assertEqual(d["curve"][0]["s"], 0)
        self.assertGreater(len(d["ticks"]), 5)
        self.assertTrue(all(t["soc"] % 1 == 0.5 for t in d["ticks"]))          # the true charge at a tick is level - 0.5
        self.assertGreater(len(d["power_samples"]), 5)

    def test_unknown_and_unwatched_ids_have_no_detail(self):
        self.assertIsNone(charging.detail(self.rows, 12345, USABLE))
        rows = [row(0, 30, state=""), row(9 * 3600, 90, state="")]
        gap = charging.analyze(rows, USABLE)[0]
        self.assertFalse(gap["observed"])
        self.assertIsNone(charging.detail(rows, gap["id"], USABLE))


if __name__ == "__main__":
    unittest.main()
