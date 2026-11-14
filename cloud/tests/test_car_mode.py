"""Car mode: detection, live refresh and a larger layout, all in the page. It talks to nobody and is not a way around the car's own limits."""
import http.client
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402

WEB = server.WEB_DIR
CAR = (WEB / "car.js").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
UI = (WEB / "ui.js").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
SW = (WEB / "sw.js").read_text(encoding="utf-8")


def code(js):
    return re.sub(r"(?<![:\"'])//[^\n]*", "", js)


class Page(unittest.TestCase):
    def test_car_js_loads_first_so_the_class_is_set_before_anything_is_drawn(self):
        order = re.findall(r'<script src="(/[a-z]+\.js)"', HTML)
        self.assertEqual(order[0], "/car.js")
        self.assertLess(order.index("/car.js"), order.index("/ui.js"))
        self.assertLess(order.index("/car.js"), order.index("/app.js"))

    def test_there_is_a_button_to_override_the_detection(self):
        self.assertIn('id="carbtn"', HTML)
        self.assertIn('getElementById("carbtn")', CAR)

    def test_the_shell_cache_and_routes_know_about_it(self):
        self.assertIn('"/car.js"', SW)
        self.assertIn("/car.js", server.STATIC)
        self.assertNotIn("/car.js", server.PUBLIC)                    # like every app script: behind the login

    def test_a_larger_layout_exists(self):
        for rule in ("html.car body", "html.car main", "html.car .tabs a", "html.car .big"):
            self.assertIn(rule, CSS)


class Behaviour(unittest.TestCase):
    def test_it_talks_to_nobody_and_never_parses_text_as_html(self):
        c = code(CAR)
        for bad in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "innerHTML", "eval(", "new Function", "importScripts"):
            self.assertNotIn(bad, c, bad)

    def test_detection_rests_on_the_signature_seen_on_the_real_head_unit_and_can_be_overridden(self):
        c = code(CAR)
        self.assertIn("X11; Linux", c)                                 # a desktop-style user agent ...
        self.assertIn("Linux (arm|aarch)", c)                          # ... on a platform that is really ARM
        self.assertIn("maxTouchPoints", c)                             # ... with a touch screen
        self.assertIn("!/Android/.test(ua)", c)                        # a normal Android phone is never mistaken for it
        self.assertIn('"on"', c)
        self.assertIn('"off"', c)
        self.assertIn("car=", CAR)                                     # ?car=1, ?car=0 and ?car=auto

    def test_the_choice_is_remembered_only_in_this_browser(self):
        c = code(CAR)
        self.assertIn("localStorage.setItem(KEY", c)
        self.assertNotIn("document.cookie", c)

    def test_the_screen_is_kept_awake_only_where_allowed_and_only_in_car_mode(self):
        c = code(CAR)
        self.assertIn('navigator.wakeLock.request("screen")', c)
        self.assertIn("window.isSecureContext", c)
        self.assertIn("!CAR.on ||", c)                                 # never outside car mode
        self.assertIn("document.hidden", c)                            # never while the page is not visible

    def test_every_refresh_gets_faster_in_the_car(self):
        self.assertIn("carMs(SLOW_MS, 3000)", APP)
        self.assertIn("carMs(FAST_MS, 3000)", APP)
        self.assertIn("carMs(60000, 15000)", APP)
        self.assertIn("if (window.CAR && window.CAR.on) ms = Math.min(ms, 10000);", UI)

    def test_outside_the_car_nothing_changes(self):
        self.assertIn("(window.CAR ? window.CAR.ms(normal, car) : normal)", APP)       # no car.js: the normal interval
        self.assertEqual(re.findall(r"const FAST_MS = (\d+), SLOW_MS = (\d+);", APP), [("5000", "20000")])

    def test_it_does_not_bypass_anything_on_the_car(self):
        c = code(CAR)
        found = re.findall(r"\b(speed|gear|is_driving|geolocation|devicemotion|deviceorientation|vehicle)\b", c, flags=re.I)
        self.assertEqual(found, [])                                    # it never looks at whether the car is moving, or at the car at all
        self.assertNotIn("/api/", c)


class Serving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = (db.DB_PATH, settings.SETTINGS_FILE)
        db.DB_PATH = Path(self.tmp.name) / "data" / "h.sqlite"
        self.addCleanup(lambda: (setattr(db, "DB_PATH", old[0]), setattr(settings, "SETTINGS_FILE", old[1])))

    def start(self, token=None):
        srv = server.make_server("127.0.0.1", 0, token=token, capacity_kwh=49.92, usable_fraction=1.0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def get(self, port, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def test_served_and_private_on_a_private_server(self):
        port = self.start()
        st, h, b = self.get(port, "/car.js")
        self.assertEqual((st, h["Content-Type"]), (200, "application/javascript; charset=utf-8"))
        self.assertIn("connect-src 'self';", h["Content-Security-Policy"])        # no policy exception: it makes no requests
        port = self.start("t" * 32)
        self.assertEqual(self.get(port, "/car.js")[0], 401)
        self.assertEqual(self.get(port, "/car.js", {"Cookie": "byd_token=" + "t" * 32})[0], 200)


if __name__ == "__main__":
    unittest.main()
