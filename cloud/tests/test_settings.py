"""Tests for settings.py (battery size and usable fraction)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import settings  # noqa: E402


class Settings(unittest.TestCase):
    def _load(self, content):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            if content is not None:
                p.write_text(content, encoding="utf-8")
            return settings.load(p)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(self._load(None), dict(settings.DEFAULTS, battery_set=False))

    def test_valid_values_are_used(self):
        s = self._load(json.dumps({"battery_kwh": 49.92, "usable_fraction": 0.93}))
        self.assertEqual(s["battery_kwh"], 49.92)
        self.assertEqual(s["usable_fraction"], 0.93)
        self.assertEqual(s["range_mode"], "policy")
        self.assertAlmostEqual(settings.usable_kwh(s), 49.92 * 0.93)

    def test_integers_are_accepted(self):
        self.assertEqual(self._load('{"battery_kwh": 60}')["battery_kwh"], 60.0)

    def test_bad_values_are_ignored_not_fatal(self):
        for bad in ('{"battery_kwh": "49.92"}', '{"battery_kwh": 5}', '{"battery_kwh": 9999}',
                    '{"battery_kwh": true}', '{"usable_fraction": 1.5}', '{"usable_fraction": 0}',
                    "not json", "[1, 2]", "null", ""):
            self.assertEqual(self._load(bad), dict(settings.DEFAULTS, battery_set=False), bad)

    def test_partial_file_keeps_other_defaults(self):
        s = self._load('{"battery_kwh": 49.92}')
        self.assertEqual(s["battery_kwh"], 49.92)
        self.assertEqual(s["usable_fraction"], settings.DEFAULTS["usable_fraction"])

    def test_range_mode(self):
        for ok in ("policy", "own", "combined"):
            self.assertEqual(self._load('{"range_mode": "%s"}' % ok)["range_mode"], ok)
        for bad in ('{"range_mode": "yolo"}', '{"range_mode": 3}', '{"range_mode": null}'):
            self.assertEqual(self._load(bad)["range_mode"], "policy", bad)

    def test_example_file_is_valid(self):
        ex = settings.LEGACY_FILE.with_name("settings.example.json")
        s = settings.load(ex)
        self.assertEqual(set(s), {"battery_kwh", "usable_fraction", "range_mode", "tariff_per_kwh", "currency",
                                  "charging_loss_pct", "battery_set"})
        self.assertEqual(s["battery_kwh"], 49.92)


class ChargingSettings(unittest.TestCase):
    def _load(self, content):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(content, encoding="utf-8")
            return settings.load(p)

    def test_nothing_is_assumed_by_default(self):
        s = settings.load(Path("does-not-exist.json"))
        self.assertIsNone(s["tariff_per_kwh"])
        self.assertEqual(s["currency"], "")
        self.assertEqual(s["charging_loss_pct"], 0.0)

    def test_valid_tariff_currency_and_loss(self):
        s = self._load('{"tariff_per_kwh": 62.5, "currency": "PKR", "charging_loss_pct": 10}')
        self.assertEqual((s["tariff_per_kwh"], s["currency"], s["charging_loss_pct"]), (62.5, "PKR", 10.0))

    def test_zero_tariff_is_allowed(self):
        self.assertEqual(self._load('{"tariff_per_kwh": 0}')["tariff_per_kwh"], 0.0)       # free charging

    def test_bad_values_are_ignored(self):
        for bad in ('{"tariff_per_kwh": -1}', '{"tariff_per_kwh": "cheap"}', '{"tariff_per_kwh": true}',
                    '{"tariff_per_kwh": 1e9}', '{"currency": 5}', '{"currency": "WAYTOOLONGCURRENCY"}',
                    '{"charging_loss_pct": 80}', '{"charging_loss_pct": -3}', '{"charging_loss_pct": "x"}'):
            s = self._load(bad)
            self.assertIsNone(s["tariff_per_kwh"], bad)
            self.assertEqual(s["currency"], "", bad)
            self.assertEqual(s["charging_loss_pct"], 0.0, bad)


if __name__ == "__main__":
    unittest.main()
