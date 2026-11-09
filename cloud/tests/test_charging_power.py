"""Charging from BYD's own CHARGING state and live battery power (verified on the owner's first real charge)."""
import datetime as dt
import os
import random
import statistics
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import charging  # noqa: E402
import test_charging as tcg  # noqa: E402

USABLE = 49.92
T0 = dt.datetime(2026, 11, 9, 1, 0, 0, tzinfo=dt.timezone.utc)


def mk(sec, soc, state, power_w, odo=19735.0, speed=0.0):
    ts = (T0 + dt.timedelta(seconds=sec)).isoformat()
    return {"ts": ts, "car_ts": ts, "soc": float(soc), "odometer_km": odo, "speed_kmh": speed, "is_driving": 0,
            "power_w": power_w, "charging_state": state, "lat": None, "lon": None, "r50_rate": None}


# --------------------------------------------------------------- a simulator with noise, a ramp, random start/end --
def profile(t, minutes, kw, taper):
    ramp = min(1.0, t / 20.0)
    frac = t / (minutes * 60.0)
    f = 1.0 if (not taper or frac < 0.75) else max(0.35, 1 - (frac - 0.75) * 2.4)
    return kw * ramp * f


def simulate(rng, kw, minutes, poll_s, soc0, noise=0.02, taper=False, bias=1.0):
    """Rows for one charge. Start and end fall at random moments between polls. Returns (rows, true kWh into the battery)."""
    off = rng.uniform(0, poll_s)
    t_start = off + rng.uniform(0, poll_s)
    t_end = t_start + minutes * 60.0
    cum, table = 0.0, {}
    for s in range(0, int(t_end + 3 * poll_s) + 1):
        p = profile(s - t_start, minutes, kw, taper) if t_start <= s <= t_end else 0.0
        cum += p / 3600.0
        table[s] = (cum, p)
    truth = table[int(t_end)][0]
    rows, t = [], off
    while t <= t_end + 2.5 * poll_s:
        c, p = table[int(t)]
        on = t_start <= t <= t_end
        pw = p * 1000 * bias * (1 + noise * rng.gauss(0, 1)) if on else 0.0
        rows.append(mk(t, round(soc0 + c / USABLE * 100), "CHARGING" if on else "CONNECTED", pw))
        t += poll_s
    return rows, truth


class RealCharge(unittest.TestCase):
    """The owner's first real charge (03:43-03:49Z): CONNECTED, NOT_CHARGING, then CHARGING at a steady ~6.3 kW."""

    def setUp(self):
        powers = [6229.2, 6351.8, 6333.6, 6368.4, 6386.7, 6386.7, 6421.6, 6386.7, 6316.9, 6351.8, 6316.9, 6386.7, 6316.9]
        rows = [mk(0, 87, "CONNECTED", 0.0), mk(30, 87, "CONNECTED", 0.0), mk(60, 87, "NOT_CHARGING", 0.0)]
        for i, p in enumerate(powers):
            rows.append(mk(90 + 30 * i, 87 if i < 8 else 88, "CHARGING", p))
        self.rows = rows
        self.out = charging.analyze(rows, USABLE)

    def test_it_is_listed_straight_away_even_before_the_battery_gains_2_percent(self):
        self.assertEqual(len(self.out), 1)
        s = self.out[0]
        self.assertTrue(s["ongoing"])
        self.assertEqual((s["soc_start"], s["soc_end"], s["gained_pct"]), (87, 88, 1))   # only 1%: the old rule hid this

    def test_energy_and_power_come_from_byds_power_reading(self):
        s = self.out[0]
        self.assertEqual(s["energy_method"], "BYD power")
        self.assertEqual(s["kw_method"], "power")
        self.assertAlmostEqual(s["avg_kw"], 6.35, delta=0.1)
        self.assertEqual(s["type_guess"], "AC")
        self.assertAlmostEqual(s["energy_kwh"], 6.35 * (12 * 30 / 3600.0) + 6.23 * 15 / 3600.0, delta=0.06)
        self.assertAlmostEqual(s["energy_soc_kwh"], 0.5, delta=0.01)                    # the battery % says 1% = 0.50 kWh

    def test_not_charging_and_connected_rows_are_not_part_of_it(self):
        s = self.out[0]
        self.assertGreater(s["start"], self.rows[1]["ts"])                                # starts at/after the NOT_CHARGING anchor
        self.assertLess(s["minutes"], 7)

    def test_the_state_value_matches_what_the_poller_stores(self):
        import poller
        class Ch:  # IntEnum-like member, as pyBYD's
            name = "CHARGING"
        class Rt:
            charge_state = Ch()
            charging_state = None
        self.assertEqual(poller._charge_name(Rt()), "CHARGING")


class StateEvidence(unittest.TestCase):
    def test_two_charging_readings_make_a_session_one_does_not(self):
        two = [mk(0, 50, "CONNECTED", 0.0), mk(30, 50, "CHARGING", 7000.0), mk(60, 50, "CHARGING", 7000.0)]
        one = [mk(0, 50, "CONNECTED", 0.0), mk(30, 50, "CHARGING", 7000.0), mk(60, 50, "CONNECTED", 0.0)]
        self.assertEqual(len(charging.analyze(two, USABLE)), 1)
        self.assertEqual(charging.analyze(one, USABLE), [])                               # a plug glitch, not a session

    def test_state_alone_without_power_or_a_rise_still_counts_but_uses_the_battery_percent(self):
        rows = [mk(0, 50, "CONNECTED", None)] + [mk(30 * i, 50, "CHARGING", None) for i in range(1, 6)]
        s = charging.analyze(rows, USABLE)[0]
        self.assertEqual(s["energy_method"], "battery %")
        self.assertEqual(s["energy_kwh"], 0.0)                                            # no rise and no power: nothing measured

    def test_battery_rise_without_the_label_still_works(self):
        rows, _ = tcg.charge(0, 40.0, 7.4, 120, state="")
        self.assertEqual(len([s for s in charging.analyze(rows, USABLE) if s["observed"]]), 1)

    def test_charging_label_while_moving_is_ignored(self):
        rows = [mk(i * 30, 50, "CHARGING", 7000.0, odo=19000 + i, speed=40.0) for i in range(6)]
        self.assertEqual(charging.analyze(rows, USABLE), [])

    def test_a_flicker_inside_a_session_does_not_split_it(self):
        rows = [mk(0, 50, "CONNECTED", 0.0)]
        for i in range(1, 20):
            st = "NOT_CHARGING" if i == 9 else "CHARGING"
            rows.append(mk(30 * i, 50 + i // 6, st, 0.0 if i == 9 else 6500.0))
        out = charging.analyze(rows, USABLE)
        self.assertEqual(len(out), 1)

    def test_a_pause_longer_than_the_gap_limit_is_not_integrated(self):
        a = [mk(0, 50, "CONNECTED", 0.0)] + [mk(30 * i, 50, "CHARGING", 6000.0) for i in range(1, 5)]
        b = [mk(30 * 4 + 1000 + 30 * i, 52, "CHARGING", 6000.0) for i in range(1, 5)]
        s = charging.analyze(a + b, USABLE)
        total = sum(x["energy_kwh"] for x in s)
        self.assertLess(total, 6.0 * 2 * 1.5 / 60 * 1.6)                                  # about 2 x 90 s of charging, not 1100 s


class PowerCalibration(unittest.TestCase):
    def _run(self, taper, kws, polls, n, minutes):
        rng = random.Random(21)
        errs, cov2, done = [], 0, 0
        for _ in range(n):
            kw = rng.uniform(*kws)
            poll = rng.choice(polls)
            mins = rng.uniform(*minutes)
            soc0 = rng.uniform(8, 40)
            if soc0 + kw * mins / 60 / USABLE * 100 > 96:
                continue
            rows, truth = simulate(rng, kw, mins, poll, soc0, taper=taper)
            out = charging.analyze(rows, USABLE)
            if not out or out[0]["energy_method"] != "BYD power":
                continue
            s = out[0]
            done += 1
            errs.append(abs(s["energy_kwh"] - truth) / truth)
            cov2 += abs(s["energy_kwh"] - truth) <= 2 * s["energy_pm_kwh"]
        self.assertGreater(done, 80)
        return statistics.mean(errs), cov2 / done

    def test_ac_energy_from_power_is_accurate_at_the_cadences_the_app_uses(self):
        err, cov = self._run(False, (3.5, 11), (30, 120, 300), 250, (15, 240))
        self.assertLess(err, 0.025)
        self.assertGreaterEqual(cov, 0.93)

    def test_tapering_fast_charge_energy_is_good_and_its_uncertainty_is_honest(self):
        err, cov = self._run(True, (40, 90), (30, 120, 300), 250, (15, 40))
        self.assertLess(err, 0.06)
        self.assertGreaterEqual(cov, 0.93)

    def test_the_check_catches_a_power_reading_that_is_biased_by_half(self):
        rng = random.Random(8)
        flagged = ok = n = 0
        for _ in range(120):
            kw = rng.uniform(5, 11)
            mins = rng.uniform(60, 200)
            soc0 = rng.uniform(10, 35)
            if soc0 + kw * mins / 60 / USABLE * 100 > 96:
                continue
            good, _ = simulate(rng, kw, mins, 120, soc0)
            bad, _ = simulate(random.Random(rng.random()), kw, mins, 120, soc0, bias=1.5)
            g = charging.analyze(good, USABLE)[0]
            b = charging.analyze(bad, USABLE)[0]
            if g["check"] and b["check"]:
                n += 1
                ok += g["check"] == "consistent"
                flagged += b["check"] == "inconsistent"
        self.assertGreater(n, 40)
        self.assertGreaterEqual(ok / n, 0.93)                                            # a good meter is rarely accused
        self.assertGreaterEqual(flagged / n, 0.95)                                       # a 50% bias is caught

    def test_power_based_energy_does_not_depend_on_the_configured_battery_size(self):
        rows, _ = simulate(random.Random(2), 7.4, 120, 120, 30.0)
        a = charging.analyze(rows, 49.92)[0]
        b = charging.analyze(rows, 60.48)[0]
        self.assertEqual(a["energy_kwh"], b["energy_kwh"])
        self.assertNotEqual(a["energy_soc_kwh"], b["energy_soc_kwh"])                    # only the battery-% figure scales


if __name__ == "__main__":
    unittest.main()
