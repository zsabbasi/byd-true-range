"""How often the parked car is checked: a setting the user can change, read by the poller every time."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import poller  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402

HDRS = {"Content-Type": "application/json", "X-BYD-App": "1"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        old = (db.DB_PATH, settings.SETTINGS_FILE, settings.LEGACY_FILE)
        db.DB_PATH = root / "data" / "history.sqlite"
        settings.SETTINGS_FILE = None
        settings.LEGACY_FILE = root / "nope.json"
        self.addCleanup(lambda: (setattr(db, "DB_PATH", old[0]), setattr(settings, "SETTINGS_FILE", old[1]),
                                 setattr(settings, "LEGACY_FILE", old[2])))
        self.file = root / "data" / "settings.json"


class Saving(Base):
    def test_the_default_is_fifteen_minutes_and_not_marked_as_chosen(self):
        self.assertEqual(settings.DEFAULT_PARKED_MIN, 15)
        self.assertEqual(settings.parked_interval_min(), 15)
        self.assertIsNone(settings.load()["parked_interval_min"])
        self.assertEqual(poller.SLOW_INTERVAL, 900)

    def test_a_chosen_value_is_stored_and_read_back_keeping_other_keys(self):
        settings.save_battery(60.48)
        self.assertEqual(settings.save_parked_interval(10), 10)
        data = json.loads(self.file.read_text())
        self.assertEqual((data["parked_interval_min"], data["battery_kwh"]), (10, 60.48))
        self.assertEqual(settings.parked_interval_min(), 10)

    def test_only_sensible_whole_minutes_are_accepted(self):
        for bad in (1, 0, -5, 121, 2.5, "5", None, True, float("nan"), [5]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                settings.save_parked_interval(bad)
        self.assertFalse(self.file.exists())
        for ok in (2, 5, 5.0, 60, 120):
            self.assertEqual(settings.save_parked_interval(ok), int(ok))

    def test_a_bad_value_in_the_file_is_ignored(self):
        self.file.parent.mkdir(parents=True)
        for bad in ('{"parked_interval_min": 1}', '{"parked_interval_min": "soon"}', '{"parked_interval_min": 999}', '{"parked_interval_min": true}'):
            self.file.write_text(bad)
            self.assertEqual(settings.parked_interval_min(), 15, bad)

    def test_the_options_offered_are_all_valid(self):
        lo, hi = settings.LIMITS["parked_interval_min"]
        self.assertIn(settings.DEFAULT_PARKED_MIN, settings.PARKED_OPTIONS)
        self.assertTrue(all(lo <= o <= hi for o in settings.PARKED_OPTIONS))


class InThePoller(Base):
    def setUp(self):
        super().setUp()
        v = mock.patch.object(poller, "viewer_active", lambda now=None: False)
        v.start()
        self.addCleanup(v.stop)

    def test_the_poller_uses_the_setting_at_once_without_a_restart(self):
        self.assertEqual(poller.parked_interval_s(), 900.0)
        settings.save_parked_interval(5)
        self.assertEqual(poller.parked_interval_s(), 300.0)
        self.assertEqual(poller.parked_delay(None), 300.0)
        settings.save_parked_interval(2)
        self.assertEqual(poller.parked_delay(None), 120.0)

    def test_an_unreadable_settings_file_never_stops_polling(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b"\xff\xfe not json")
        self.assertEqual(poller.parked_interval_s(), 900.0)
        with mock.patch.object(settings, "load", side_effect=RuntimeError("boom")):
            self.assertEqual(poller.parked_interval_s(), 900.0)

    def test_someone_watching_the_app_still_wins(self):
        settings.save_parked_interval(60)
        with mock.patch.object(poller, "viewer_active", lambda now=None: True):
            self.assertEqual(poller.parked_delay(None), poller.VIEWER_PARKED_INTERVAL)

    def test_charging_still_polls_faster_than_the_parked_default(self):
        self.assertLess(poller.CHARGE_INTERVAL, poller.SLOW_INTERVAL)


class Api(Base):
    def start(self, **kw):
        srv = server.make_server("127.0.0.1", 0, token=None, capacity_kwh=49.92, usable_fraction=1.0, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def call(self, port, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=HDRS if body is not None else {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data)

    def test_the_account_page_gets_the_current_value_the_default_and_the_options(self):
        port = self.start()
        st, a = self.call(port, "GET", "/api/account")
        self.assertEqual(a["parked"], {"min": 15, "default": 15, "custom": False, "options": [2, 3, 5, 10, 15, 30, 60]})

    def test_saving_it_changes_what_the_account_reports_and_what_the_poller_reads(self):
        port = self.start()
        st, d = self.call(port, "POST", "/api/settings", {"parked_interval_min": 10})
        self.assertEqual((st, d), (200, {"ok": True, "parked_interval_min": 10}))
        a = self.call(port, "GET", "/api/account")[1]["parked"]
        self.assertEqual((a["min"], a["custom"]), (10, True))
        self.assertEqual(settings.parked_interval_min(), 10)

    def test_battery_and_interval_can_be_saved_together_and_alone(self):
        port = self.start()
        st, d = self.call(port, "POST", "/api/settings", {"battery_kwh": 60.48, "parked_interval_min": 3})
        self.assertEqual((st, d["battery_kwh"], d["parked_interval_min"]), (200, 60.48, 3))
        st, d = self.call(port, "POST", "/api/settings", {"battery_kwh": 49.92})
        self.assertEqual((st, d), (200, {"ok": True, "battery_kwh": 49.92}))
        self.assertEqual(settings.parked_interval_min(), 3)                                   # untouched by the battery-only request

    def test_a_bad_value_saves_nothing_at_all(self):
        port = self.start()
        st, d = self.call(port, "POST", "/api/settings", {"battery_kwh": 60.48, "parked_interval_min": 1})
        self.assertEqual((st, d["field"]), (400, "parked_interval_min"))
        self.assertFalse(self.file.exists())                                                  # not even the valid battery size
        for bad in ({"parked_interval_min": "often"}, {"parked_interval_min": 200}, {"parked_interval_min": 2.5}, {}):
            self.assertEqual(self.call(port, "POST", "/api/settings", bad)[0], 400, bad)
        self.assertFalse(self.file.exists())

    def test_each_person_on_a_shared_server_has_their_own_interval(self):
        import users
        import credstore
        key = Path(self.tmp.name) / "k.key"
        key.write_bytes(b"0123456789abcdef0123456789abcdef")
        os.chmod(key, 0o600)
        with mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(key)}), mock.patch.object(credstore, "default_scheme", lambda: "aes"):
            a, b = users.user_id("a@example.com", "sg"), users.user_id("b@example.com", "sg")
            users.add(a)
            users.add(b)
            with db.use_dir(users.user_dir(a)):
                settings.save_parked_interval(30)
            with db.use_dir(users.user_dir(b)):
                self.assertEqual(settings.parked_interval_min(), 15)
            with db.use_dir(users.user_dir(a)):
                self.assertEqual(poller.parked_interval_s(), 1800.0)


if __name__ == "__main__":
    unittest.main()
