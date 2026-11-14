"""The Live card (car mode): this screen's own sensors, computed and drawn on the screen. It sends nothing anywhere and keeps no coordinates."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server  # noqa: E402

WEB = server.WEB_DIR
LIVE = (WEB / "carlive.js").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
SW = (WEB / "sw.js").read_text(encoding="utf-8")
CSS = (WEB / "app.css").read_text(encoding="utf-8")


def code(js):
    return re.sub(r"(?<![:\"'])//[^\n]*", "", js)


class Wiring(unittest.TestCase):
    def test_it_loads_after_car_js_and_app_js(self):
        order = re.findall(r'<script src="(/[a-z]+\.js)"', HTML)
        self.assertLess(order.index("/car.js"), order.index("/carlive.js"))
        self.assertLess(order.index("/app.js"), order.index("/carlive.js"))

    def test_there_is_a_place_for_it_on_the_now_page_hidden_until_car_mode(self):
        self.assertIn('<section id="live" class="hero live" hidden></section>', HTML)
        self.assertIn("if (!CAR || !CAR.on || !card) return;", LIVE)             # not in the car: nothing is built, nothing runs

    def test_routes_and_shell_cache(self):
        self.assertIn("/carlive.js", server.STATIC)
        self.assertNotIn("/carlive.js", server.PUBLIC)
        self.assertIn('"/carlive.js"', SW)

    def test_app_js_hands_over_the_status_it_already_fetched_and_nothing_more(self):
        self.assertIn("if (window.CARLIVE && window.CARLIVE.onStatus) window.CARLIVE.onStatus(s);", APP)
        self.assertNotIn("CARLIVE", re.sub(r"if \(window\.CARLIVE && window\.CARLIVE\.onStatus\) window\.CARLIVE\.onStatus\(s\);", "", APP))

    def test_the_styles_exist(self):
        for rule in (".live .spd", "#ptrend", ".live .v.pre", "html.carnow"):
            self.assertIn(rule, CSS)


class Privacy(unittest.TestCase):
    def test_it_talks_to_nobody(self):
        c = code(LIVE)
        for bad in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.serviceWorker", "importScripts", "postMessage("):
            self.assertNotIn(bad, c, bad)
        self.assertEqual(re.findall(r"https?://", c), [])

    def test_it_keeps_no_coordinates_anywhere(self):
        c = code(LIVE)
        for bad in ("localStorage", "sessionStorage", "document.cookie", "indexedDB", "caches.", "console.log", "console.info"):
            self.assertNotIn(bad, c, bad)

    def test_the_position_is_hidden_until_asked_and_only_roughly(self):
        self.assertIn("showPos: false", LIVE)
        self.assertIn("tap to show position", LIVE)                                # tapping the Heading tile is the only way to see it
        self.assertIn("toFixed(4)", LIVE)                                          # four decimals: about 10 m, not more

    def test_text_is_never_parsed_as_html_or_code(self):
        c = code(LIVE)
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(bad, c, bad)

    def test_it_never_starts_a_camera_microphone_or_device_connection(self):
        c = code(LIVE)
        for bad in ("getUserMedia", "requestDevice", "requestPort", "navigator.bluetooth", "navigator.usb", "navigator.serial", "navigator.hid", "mediaDevices"):
            self.assertNotIn(bad, c, bad)


class Behaviour(unittest.TestCase):
    def test_speed_comes_from_gps_never_from_integrating_the_accelerometer(self):
        c = code(LIVE)
        self.assertIn("fix.speed * 3.6", c)                                        # the browser's own GPS speed
        self.assertIn("derivedKmh", c)                                             # or worked out from two positions
        self.assertNotIn("velocity", c.lower())                                    # no dead reckoning from acceleration
        self.assertIsNone(re.search(r"\b(speed|vel\w*|v)\s*\+=", c))                    # nothing accumulates acceleration into a speed

    def test_the_sensors_run_only_while_the_now_page_is_visible(self):
        self.assertIn("if (show && !document.hidden) start(); else stop();", LIVE)
        self.assertIn('window.addEventListener("hashchange", sync)', LIVE)
        self.assertIn('document.addEventListener("visibilitychange", sync)', LIVE)
        self.assertIn("navigator.geolocation.clearWatch(watchId)", LIVE)

    def test_it_does_not_prompt_by_surprise(self):
        c = code(LIVE)
        self.assertIn('p.state === "granted"', c)                                  # starts by itself only where the permission is already granted
        self.assertIn("Start GPS", LIVE)                                           # otherwise the person taps

    def test_no_motion_sensors_are_used(self):
        c = code(LIVE)
        for bad in ("devicemotion", "deviceorientation", "Accelerometer", "DeviceMotionEvent"):
            self.assertNotIn(bad, c, bad)

    def test_byd_readings_always_carry_their_age_and_go_dim_when_old(self):
        self.assertIn("s ago", LIVE)
        self.assertIn("STATUS_OLD_S", LIVE)
        self.assertIn('classList.toggle("dimmed", old)', LIVE)

    def test_there_is_no_power_bar_and_the_trend_is_on_the_main_page(self):
        self.assertNotIn("pbar", LIVE + CSS)
        self.assertNotIn("drive_max_kw", LIVE)
        self.assertIn("function renderPowerTrend", APP)
        self.assertIn('id="ptrend"', HTML)

    def test_the_speed_is_gps_beside_the_range_and_byds_reading_is_labelled_as_byds(self):
        self.assertIn('el("div", "label", "Speed (GPS)")', LIVE)
        self.assertIn("BYD last said ", LIVE)
        self.assertIn('document.querySelector("#view-now > .hero[aria-live]")', LIVE)     # the speed goes into the range card
        self.assertIn("origTiles.hidden = true", LIVE)                                    # BYD's own speed tile is replaced, not duplicated

    def test_the_now_page_has_a_no_scroll_car_layout(self):
        self.assertIn('document.documentElement.classList.toggle("carnow", show)', LIVE)
        self.assertIn("html.carnow,html.carnow body{height:100%;overflow:hidden}", CSS)
        self.assertIn("html.carnow #ptsec{flex:1", CSS)


if __name__ == "__main__":
    unittest.main()
