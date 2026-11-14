"""Tests for server.py: status building, provisional estimate, and the real HTTP server (auth, headers)."""
import datetime as dt
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import db  # noqa: E402
import range_engine as re_  # noqa: E402
import server  # noqa: E402

NOW = dt.datetime(2026, 11, 8, 15, 5, 0, tzinfo=dt.timezone.utc)


def probe_row(**kw):
    """The first real reading from the owner's car (see PROJECT_PLAN 6e)."""
    row = dict(ts="2026-11-08T15:01:53+00:00", car_ts="", vin_tail="0000", soc=95.0, odometer_km=19708.0,
               byd_range_km=393.0, speed_kmh=0.0, power_w=0.0, charging_state="CONNECTED",
               recent_50km="14.0kW·h/100km", r50_rate=14.0, r50_kwh=7.0, r50_air_pct=None,
               lat=25.2, lon=55.3, is_driving=0, gear="OFF", vehicle_state="ON")
    row.update(kw)
    return row


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "t.sqlite"
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class ProvisionalEstimate(unittest.TestCase):
    def test_probe_values_match_byd_closely_and_are_wide(self):
        r = re_.provisional_from_consumption(95, 14.0, usable_kwh=60.48 * 0.95)
        self.assertEqual(r.status, "provisional")
        self.assertAlmostEqual(r.true_range_km, 393, delta=25)       # BYD says 393
        self.assertTrue(r.low_km < 393 < r.high_km)  # BYD's own figure sits inside the band
        self.assertLess(r.low_km, r.true_range_km)
        self.assertGreater(r.high_km, r.true_range_km)
        self.assertLess(r.trust_pct, 50)                              # honestly low trust

    def test_provisional_is_wider_than_a_data_backed_estimate(self):
        prov = re_.provisional_from_consumption(70, 14.0)
        rows = [{"soc": 100 - i, "odometer_km": 1000 + 5 * i} for i in range(41)]
        real = re_.estimate(rows, soc_now=70)
        self.assertGreater((prov.high_km - prov.low_km) / prov.true_range_km,
                           (real.high_km - real.low_km) / real.true_range_km)

    def test_missing_inputs(self):
        for soc, c in ((None, 14), (0, 14), (50, None), (50, 0), (50, -1)):
            self.assertEqual(re_.provisional_from_consumption(soc, c).status, "insufficient")


class BuildStatus(Base):
    def test_empty_db(self):
        self.assertFalse(server.build_status(self.conn, now=NOW)["has_data"])
        self.assertFalse(server.build_status(None, now=NOW)["has_data"])

    def test_single_row_gives_provisional_range(self):
        db.insert_reading(self.conn, probe_row())
        s = server.build_status(self.conn, now=NOW)
        self.assertTrue(s["has_data"])
        self.assertEqual(s["range"]["status"], "provisional")
        self.assertEqual(s["range"]["basis"], "byd-consumption")
        self.assertTrue(s["range"]["low_km"] < s["range"]["km"] < s["range"]["high_km"])
        self.assertAlmostEqual(s["reading"]["age_s"], 187, delta=1)
        self.assertEqual(s["reading"]["byd_range_km"], 393.0)

    def test_status_never_contains_location_or_vin(self):
        db.insert_reading(self.conn, probe_row())
        status = server.build_status(self.conn, now=NOW)

        def walk(o, path=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield from walk(v, path + "/" + k)
            else:
                yield path, o

        words = {w for p, _ in walk(status) for seg in p.lower().split("/") for w in seg.split("_") if w}
        banned = {"lat", "lon", "lng", "vin", "gps", "latitude", "longitude", "user", "password", "token"}
        self.assertEqual(words & banned, set())  # whole key words, so 'is_driving' is fine
        values = json.dumps(status)
        for secret in ("0000", "25.2", "55.3"):
            self.assertNotIn(secret, values)

    def _drive_rows(self, km, step_s=30):
        """km of watched driving: 1 km per 30 s poll (120 km/h), 1% battery per 3 km, newest row 3 s before NOW."""
        n = int(km)
        for i in range(n + 1):
            ts = (NOW - dt.timedelta(seconds=3 + (n - i) * step_s)).isoformat()
            db.insert_reading(self.conn, probe_row(ts=ts, car_ts=ts, soc=95 - i // 3, odometer_km=19000.0 + i,
                                                  speed_kmh=60.0, is_driving=1, gear="ON"))

    def test_before_50_km_of_own_data_it_uses_byd_consumption_on_your_battery(self):
        self._drive_rows(30)                                          # only 30 km watched
        s = server.build_status(self.conn, now=NOW, capacity_kwh=49.92, usable_fraction=0.95)
        g = s["range"]
        self.assertEqual(g["policy"], "immature")
        self.assertEqual(g["basis"], "byd-consumption")
        self.assertAlmostEqual(g["own_observed_km"], 30, delta=1)
        self.assertEqual(g["own_needed_km"], 50)
        soc = s["reading"]["soc"]
        self.assertAlmostEqual(g["km"], soc / 100 * 49.92 * 0.95 / 14.0 * 100, delta=1)   # BYD rate x YOUR pack

    def test_after_50_km_of_own_data_it_uses_only_own_data(self):
        self._drive_rows(70)
        g = server.build_status(self.conn, now=NOW, capacity_kwh=49.92, usable_fraction=0.95)["range"]
        self.assertEqual(g["policy"], "own")
        self.assertEqual(g["basis"], "window")
        self.assertIsNone(g["own_weight_pct"])                        # no blending any more
        self.assertGreaterEqual(g["own_observed_km"], 50)

    def test_byd_range_estimate_is_never_the_headline(self):
        self._drive_rows(70)
        for mode in ("policy", "own", "combined"):
            g = server.build_status(self.conn, now=NOW, capacity_kwh=49.92, usable_fraction=0.95, range_mode=mode)["range"]
            self.assertNotEqual(g["km"], 393.0, mode)                 # 393 is BYD's displayed range in probe_row

    def test_no_range_when_no_energy_and_no_driving(self):
        db.insert_reading(self.conn, probe_row(r50_rate=None))
        s = server.build_status(self.conn, now=NOW)
        self.assertIsNone(s["range"]["km"])
        self.assertEqual(s["range"]["status"], "insufficient")

    def test_data_age_comes_from_the_cars_timestamp_not_our_poll_time(self):
        # we polled 40 s ago, but the CAR last reported 65 minutes before NOW
        db.insert_reading(self.conn, probe_row(ts=(NOW - dt.timedelta(seconds=40)).isoformat(),
                                              car_ts="2026-11-08 14:00:00+00:00"))
        rd = server.build_status(self.conn, now=NOW)["reading"]
        self.assertAlmostEqual(rd["age_s"], 40, delta=1)
        self.assertGreater(rd["data_age_s"], 3000)
        self.assertGreater(rd["data_age_s"], rd["age_s"] * 10)

    def test_data_age_falls_back_to_poll_age_without_car_timestamp(self):
        db.insert_reading(self.conn, probe_row(car_ts=""))
        rd = server.build_status(self.conn, now=NOW)["reading"]
        self.assertEqual(rd["data_age_s"], rd["age_s"])

    def test_series_downsamples(self):
        for i in range(300):
            db.insert_reading(self.conn, probe_row(ts=(NOW - dt.timedelta(minutes=i)).isoformat(), soc=50))
        s = server.build_series(self.conn, hours=24, max_points=100, now=NOW)
        self.assertLessEqual(len(s), 100)
        self.assertEqual(set(s[0]), {"ts", "soc", "byd_range_km"})  # no coordinates


def request(port, path, headers=None, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    out = (r.status, dict(r.getheaders()), body)
    c.close()
    return out


class HttpServer(Base):
    def _start(self, token, **kw):
        srv = server.make_server("127.0.0.1", 0, token=token, capacity_kwh=60.48, usable_fraction=0.95, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_loopback_mode_serves_without_token(self):
        db.insert_reading(self.conn, probe_row())
        port = self._start(None)
        st, h, body = request(port, "/api/status")
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["has_data"])
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Cache-Control"], "no-store")

    def test_status_requests_signal_the_poller(self):
        db.insert_reading(self.conn, probe_row())
        flag = db.DB_PATH.parent / "viewer_active"
        server._last_touch = float("-inf")
        self.assertFalse(flag.exists())
        port = self._start(None)
        request(port, "/api/series")                  # chart requests must NOT count as "someone is watching"
        self.assertFalse(flag.exists())
        request(port, "/api/status")
        self.assertTrue(flag.exists())
        request(port, "/")                            # nor does loading static files
        flag.unlink()
        server._last_touch = float("-inf")
        request(port, "/")
        self.assertFalse(flag.exists())

    def test_unauthorised_requests_do_not_wake_the_car(self):
        tok = "t" * 32
        flag = db.DB_PATH.parent / "viewer_active"
        server._last_touch = float("-inf")
        port = self._start(tok)
        self.assertEqual(request(port, "/api/status")[0], 401)
        self.assertFalse(flag.exists())               # an unauthenticated stranger cannot trigger polling

    def test_static_files(self):
        port = self._start(None)
        for path, ctype in (("/", "text/html"), ("/app.js", "application/javascript"), ("/app.css", "text/css")):
            st, h, body = request(port, path)
            self.assertEqual(st, 200, path)
            self.assertTrue(h["Content-Type"].startswith(ctype))
            self.assertGreater(len(body), 100)
        for js in ("/app.js", "/trips.js", "/battery.js", "/charging.js", "/ui.js", "/account.js"):
            st, _, src = request(port, js)
            self.assertEqual(st, 200, js)                              # a missing file must fail here, not pass vacuously
            self.assertGreater(len(src), 500, js)
            self.assertNotIn(b"innerHTML", src, js)                    # data is never injected as HTML
            self.assertNotIn(b"outerHTML", src, js)
            self.assertNotIn(b"insertAdjacentHTML", src, js)
            self.assertNotIn(b"eval(", src, js)
            self.assertNotIn(b"document.write", src, js)
        for name in ("/", "/app.js", "/trips.js", "/battery.js", "/charging.js", "/ui.js", "/account.js"):
            self.assertNotIn("likely", request(port, name)[2].decode().lower(), name)   # owner: the word "likely" can go
        # layout rules the owner asked for: key stats first, explanations in floating tooltips
        html = request(port, "/")[2].decode()
        css = request(port, "/app.css")[2].decode()
        self.assertNotIn('class="note"', html)                           # no visible explanatory paragraph on the main page
        self.assertIn('data-tip="basis"', html)                          # the calculation note is a tooltip...
        self.assertRegex(html, r'<div id="basis" class="tip"[^>]* hidden')   # ...closed by default
        self.assertRegex(css, r'\.tip\{position:absolute')                 # ...and it FLOATS: opening it never moves the stats
        self.assertLess(html.index('id="soc"'), html.index('id="basis"') + 400)   # the key tiles follow the hero directly
        ui = request(port, "/ui.js")[2].decode()
        self.assertIn("window.UI", ui)
        self.assertIn("aria-expanded", ui)                                # accessible: a button that says whether it is open
        self.assertIn("Escape", ui)
        app = request(port, "/app.js")[2].decode()
        self.assertIn("AbortController", app)                            # a hung request can never stop the refresh loop
        self.assertIn("finally", app)                                    # ...the next refresh is always scheduled
        for ev in ("visibilitychange", "pageshow", "online", "focus"):
            self.assertIn(ev, app)                                       # screen back on / network back: refresh at once
        self.assertIn("dotpulse", css)                                   # a visible pulse on every refresh
        self.assertIn("wakeLock", app)                                   # optional keep-screen-on switch...
        self.assertIn("isSecureContext", app)                            # ...only where the browser allows it
        self.assertIn('id="wakebtn"', html)
        self.assertRegex(html, r'id="wakebtn"[^>]* hidden')              # hidden unless supported
        self.assertIn("refreshLater", request(port, "/ui.js")[2].decode())
        self.assertIn("refreshLater", request(port, "/charging.js")[2].decode())
        self.assertIn("refreshLater", request(port, "/trips.js")[2].decode())
        trips_js = request(port, "/trips.js")[2].decode()
        self.assertIn('className = "maptile"', trips_js)                # not "tile": that class styles stat cards
        self.assertNotIn('className = "tile"', trips_js)
        self.assertEqual(request(port, "/trips.js")[1]["Content-Type"].split(";")[0], "application/javascript")
        self.assertIn("/trips.js", request(port, "/")[2].decode())
        css = request(port, "/app.css")[2].decode()
        self.assertIn(".map .maptile", css)
        html = request(port, "/")[2].decode()
        html = "\n".join(l for l in html.splitlines() if 'property="og:' not in l and 'name="twitter:' not in l)   # tags naming this very address
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))  # no external resources

    def test_rebinding_host_is_rejected(self):
        port = self._start(None)
        st, _, _ = request(port, "/api/status", {"Host": "evil.example.com"})
        self.assertEqual(st, 403)
        self.assertEqual(request(port, "/api/status", {"Host": f"localhost:{port}"})[0], 200)

    def test_path_traversal_and_unknown(self):
        port = self._start(None)
        for p in ("/../server.py", "/..%2fserver.py", "/web/app.js", "/secrets.env", "/data/history.sqlite", "/nope"):
            self.assertEqual(request(port, p)[0], 404, p)

    def test_read_only_methods(self):
        port = self._start(None)
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(request(port, "/api/status", method=m)[0], 405, m)

    def test_token_required_when_exposed(self):
        db.insert_reading(self.conn, probe_row())
        tok = "t" * 32
        port = self._start(tok)
        self.assertEqual(request(port, "/api/status")[0], 401)
        self.assertEqual(request(port, "/api/status?token=wrong")[0], 401)
        self.assertEqual(request(port, "/api/status", {"Cookie": "byd_token=wrong"})[0], 401)

        st, h, _ = request(port, f"/?token={tok}")           # first visit: token in URL
        self.assertEqual(st, 302)
        self.assertEqual(h["Location"], "/")                  # token stripped from the URL
        cookie = h["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

        st, _, body = request(port, "/api/status", {"Cookie": f"byd_token={tok}"})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["has_data"])

    def test_token_mode_works_behind_a_tunnel_host(self):
        """ngrok forwards its own Host header. With a token that is fine; without one it must still fail."""
        db.insert_reading(self.conn, probe_row())
        tok = "t" * 32
        port = self._start(tok)
        tunnel = {"Host": "abc123.ngrok-free.app", "X-Forwarded-Proto": "https"}
        self.assertEqual(request(port, "/api/status", tunnel)[0], 401)           # no token: refused
        st, h, _ = request(port, f"/?token={tok}", tunnel)
        self.assertEqual(st, 302)
        self.assertIn("Secure", h["Set-Cookie"])                                  # https via the tunnel
        self.assertEqual(request(port, "/api/status", {**tunnel, "Cookie": f"byd_token={tok}"})[0], 200)
        st, h, _ = request(port, f"/?token={tok}")                                # plain http: no Secure flag
        self.assertNotIn("Secure", h["Set-Cookie"])

    def test_loopback_without_token_still_rejects_tunnel_host(self):
        port = self._start(None)
        self.assertEqual(request(port, "/api/status", {"Host": "abc123.ngrok-free.app"})[0], 403)

    def test_missing_db_is_not_an_error(self):
        db.DB_PATH = Path(self.tmp.name) / "does_not_exist.sqlite"
        port = self._start(None)
        st, _, body = request(port, "/api/status")
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(body)["has_data"])

    def test_server_cannot_modify_database(self):
        db.insert_reading(self.conn, probe_row())
        ro = server._connect_ro()
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("DELETE FROM readings")
        ro.close()


class TripsApi(Base):
    def _fill(self):
        import test_trips  # noqa: E402
        rows, _, _ = test_trips.drive(1000, 40, start_soc=90)
        for r in rows:
            db.insert_reading(self.conn, r)
        return rows

    def _start(self, token=None, **kw):
        srv = server.make_server("127.0.0.1", 0, token=token, capacity_kwh=49.92, usable_fraction=0.95, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_list_and_detail(self):
        self._fill()
        port = self._start()
        st, _, body = request(port, "/api/trips")
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertEqual(len(data["trips"]), 1)
        t = data["trips"][0]
        self.assertTrue(t["observed"])
        self.assertAlmostEqual(t["kwh_per_100"], 15.0, delta=1.5)
        self.assertEqual(data["totals"]["trips"], 1)
        self.assertNotIn("points", t)                                    # the list never carries coordinates
        self.assertNotIn("lat", json.dumps(data["trips"]))
        st, _, body = request(port, f"/api/trips/{t['id']}")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertGreater(len(d["points"]), 10)
        self.assertTrue(d["location"] and d["map_tiles"])

    def test_no_location_removes_every_coordinate(self):
        self._fill()
        port = self._start(location=False, map_tiles=False)
        tid = json.loads(request(port, "/api/trips")[2])["trips"][0]["id"]
        st, _, body = request(port, f"/api/trips/{tid}")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(d["points"], [])
        self.assertFalse(d["has_route"])
        self.assertFalse(d["location"])
        for banned in ('"lat"', '"lon"'):
            self.assertNotIn(banned, body.decode())
            self.assertNotIn(banned, request(port, "/api/trips")[2].decode())

    def test_csp_allows_the_tile_host_only_when_tiles_are_on(self):
        on = self._start(map_tiles=True)
        off = self._start(map_tiles=False)
        self.assertIn("https://tile.openstreetmap.org", request(on, "/")[1]["Content-Security-Policy"])
        self.assertNotIn("openstreetmap", request(off, "/")[1]["Content-Security-Policy"])
        for port in (on, off):
            csp = request(port, "/")[1]["Content-Security-Policy"]
            self.assertIn("script-src 'self'", csp)                      # never relaxed
            self.assertNotIn("unsafe", csp)

    def test_bad_ids_are_404(self):
        self._fill()
        port = self._start()
        for p in ("/api/trips/abc", "/api/trips/99999999999999", "/api/trips/1;DROP", "/api/trips/-5",
                  "/api/trips/", "/api/trips/1/2", "/api/trips/12345678"):
            self.assertEqual(request(port, p)[0], 404, p)

    def test_trips_need_the_token_when_exposed(self):
        self._fill()
        tok = "t" * 32
        port = self._start(tok)
        self.assertEqual(request(port, "/api/trips")[0], 401)
        self.assertEqual(request(port, "/api/trips/1")[0], 401)
        self.assertEqual(request(port, "/api/trips", {"Cookie": f"byd_token={tok}"})[0], 200)

    def test_browsing_history_does_not_wake_the_car(self):
        self._fill()
        flag = db.DB_PATH.parent / "viewer_active"
        if flag.exists():
            flag.unlink()
        server._last_touch = float("-inf")
        port = self._start()
        request(port, "/api/trips")
        request(port, "/trips.js")
        self.assertFalse(flag.exists())

    def test_empty_database(self):
        port = self._start()
        d = json.loads(request(port, "/api/trips")[2])
        self.assertEqual(d["trips"], [])
        self.assertEqual(d["totals"]["trips"], 0)


class TripRegenApi(Base):
    def _fill(self):
        import test_regen
        import test_trips
        import random as _r
        rows, _, _ = test_trips.drive(1000, 40, start_soc=90)
        P = test_regen.power_process(_r.Random(4), 5000)
        for i, r in enumerate(rows):
            r["power_w"] = P[(i * 30) % 5000]
            db.insert_reading(self.conn, r)

    def _start(self, **kw):
        srv = server.make_server("127.0.0.1", 0, token=None, capacity_kwh=49.92, usable_fraction=1.0, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_trip_page_data_has_regen_and_a_power_series_and_no_coordinates_in_them(self):
        self._fill()
        port = self._start()
        tid = json.loads(request(port, "/api/trips")[2])["trips"][0]["id"]
        d = json.loads(request(port, f"/api/trips/{tid}")[2])
        self.assertIsNotNone(d["regen"])
        self.assertGreaterEqual(d["regen"]["samples"], 8)
        for k in ("regen_kwh", "regen_pm", "used_kwh", "used_pm", "net_kwh", "regen_share_pct", "check"):
            self.assertIn(k, d["regen"])
        self.assertGreater(len(d["power_series"]), 10)
        self.assertEqual(set(d["power_series"][0]), {"s", "kw"})
        self.assertNotIn("lat", json.dumps(d["regen"]) + json.dumps(d["power_series"]))

    def test_regen_stays_even_when_location_is_off(self):
        self._fill()
        port = self._start(location=False)
        tid = json.loads(request(port, "/api/trips")[2])["trips"][0]["id"]
        d = json.loads(request(port, f"/api/trips/{tid}")[2])
        self.assertEqual(d["points"], [])                               # no route...
        self.assertIsNotNone(d["regen"])                                # ...but regen is not location data

    def test_the_trip_list_stays_light(self):
        self._fill()
        d = json.loads(request(self._start(), "/api/trips")[2])
        for t in d["trips"]:
            self.assertNotIn("regen", t)
            self.assertNotIn("power_series", t)

    def test_page_script_shows_regen_and_says_it_is_never_charging(self):
        port = self._start()
        js = request(port, "/trips.js")[2].decode()
        self.assertIn("drawRegenSection", js)
        self.assertIn("never counted as charging", js)
        self.assertIn("regen", request(port, "/charging.js")[2].decode().lower())


class BatteryApi(Base):
    def _fill(self, true_cap, km=150):
        import test_capacity  # noqa: E402
        rows, _ = test_capacity.sim_rows(km, true_cap, start_soc=92.0)
        for r in rows:
            db.insert_reading(self.conn, r)
        return rows

    def _start(self, token=None, **kw):
        srv = server.make_server("127.0.0.1", 0, token=token, capacity_kwh=49.92, usable_fraction=0.95, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_prior_only_before_any_qualifying_drive(self):
        db.insert_reading(self.conn, probe_row())
        d = json.loads(request(self._start(), "/api/battery")[2])
        b = d["battery"]
        self.assertTrue(b["prior_only"])
        self.assertAlmostEqual(b["usable_kwh"], 49.92 * 0.95, delta=0.01)
        self.assertEqual(d["samples"], [])
        self.assertEqual(b["n_samples"], 0)
        self.assertLessEqual(b["confidence_pct"], 5)

    def test_full_battery_range_is_the_same_engine_at_100_percent(self):
        db.insert_reading(self.conn, probe_row())                     # one reading, below 100%
        self.conn.commit()
        port = self._start()
        d = json.loads(request(port, "/api/battery")[2])
        st = json.loads(request(port, "/api/status")[2])
        f = d["full_range"]
        self.assertGreater(f["km"], st["range"]["km"])                # more battery, more range
        self.assertLessEqual(f["low_km"], f["km"])
        self.assertLessEqual(f["km"], f["high_km"])
        self.assertEqual(f["kwh_per_100"], st["range"]["consumption_kwh_per_100km"])
        self.assertNotIn("lat", json.dumps(f))

    def test_full_range_at_your_own_average_comes_from_the_recorded_trips(self):
        self._fill(56.0)
        f = json.loads(request(self._start(), "/api/battery")[2])["full_range"]
        o = f["own_average"]
        self.assertIsNotNone(o)
        self.assertGreater(o["measured_km"], 20)
        self.assertAlmostEqual(o["km"], f["usable_kwh"] / o["kwh_per_100"] * 100, delta=1.5)    # usable energy over the average consumption
        self.assertLess(o["low_km"], o["km"])
        self.assertLess(o["km"], o["high_km"])

    def test_no_own_average_before_any_trip(self):
        db.insert_reading(self.conn, probe_row())
        self.conn.commit()
        self.assertIsNone(json.loads(request(self._start(), "/api/battery")[2])["full_range"]["own_average"])

    def test_no_full_range_without_a_reading(self):
        self.assertIsNone(json.loads(request(self._start(), "/api/battery")[2])["full_range"])

    def test_learned_capacity_after_a_long_drive(self):
        self._fill(56.0)                                              # true usable 56, setting says 47.4
        b = json.loads(request(self._start(), "/api/battery")[2])
        battery, samples = b["battery"], b["samples"]
        self.assertFalse(battery["prior_only"])
        self.assertGreaterEqual(len(samples), 2)
        self.assertGreater(battery["usable_kwh"], 52)                 # pulled most of the way from 47.4 to 56
        self.assertLess(battery["usable_kwh"], 58)
        self.assertGreater(battery["sd_rel_pct"], 2.9)                # never claims better than the systematic floor
        for k in ("when", "km", "drop_pct", "byd_kwh_per_100", "usable_kwh", "sd_kwh"):
            self.assertIn(k, samples[0])
        self.assertNotIn("sd_rel", samples[0])

    def test_learned_capacity_feeds_the_range_number(self):
        self._fill(56.0)
        s = server.build_status(self.conn, capacity_kwh=49.92, usable_fraction=0.95)
        self.assertTrue(s["range"]["usable_learned"])
        self.assertGreater(s["range"]["assumed_usable_kwh"], 52)      # not the 47.4 from settings

    def test_range_uses_the_setting_when_nothing_is_learned(self):
        db.insert_reading(self.conn, probe_row())
        s = server.build_status(self.conn, capacity_kwh=49.92, usable_fraction=0.95)
        self.assertFalse(s["range"]["usable_learned"])
        self.assertAlmostEqual(s["range"]["assumed_usable_kwh"], 47.4, delta=0.05)

    def test_battery_needs_the_token_when_exposed(self):
        tok = "t" * 32
        port = self._start(tok)
        self.assertEqual(request(port, "/api/battery")[0], 401)
        self.assertEqual(request(port, "/api/battery", {"Cookie": f"byd_token={tok}"})[0], 200)

    def test_reading_the_battery_does_not_wake_the_car(self):
        flag = db.DB_PATH.parent / "viewer_active"
        if flag.exists():
            flag.unlink()
        server._last_touch = float("-inf")
        port = self._start()
        request(port, "/api/battery")
        request(port, "/battery.js")
        self.assertFalse(flag.exists())

    def test_no_coordinates_in_the_battery_api(self):
        self._fill(50.0)
        body = request(self._start(), "/api/battery")[2].decode()
        for banned in ('"lat"', '"lon"', "vin", "token"):
            self.assertNotIn(banned, body)

    def test_cache_refreshes_when_new_rows_arrive(self):
        self._fill(50.0, km=60)
        first = server.capacity.learn_from_db(self.conn, 49.92, 47.4)
        import test_capacity
        more, _ = test_capacity.sim_rows(150, 50.0, start_soc=92.0, start_odo=5000.0,
                                         t0=test_capacity.T0 + dt.timedelta(days=1))
        for r in more:
            db.insert_reading(self.conn, r)
        second = server.capacity.learn_from_db(self.conn, 49.92, 47.4)
        self.assertGreater(second["n_samples"], first["n_samples"])


class Helpers(unittest.TestCase):
    def test_is_loopback(self):
        for h in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(server.is_loopback(h))
        for h in ("0.0.0.0", "192.168.1.5", "::"):
            self.assertFalse(server.is_loopback(h))

    def test_host_only(self):
        self.assertEqual(server._host_only("localhost:8765"), "localhost")
        self.assertEqual(server._host_only("[::1]:8765"), "::1")
        self.assertEqual(server._host_only("evil.com"), "evil.com")

    def test_token_file_created_and_reused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "tok.txt"
            t1 = server.load_or_create_token(p)
            self.assertGreaterEqual(len(t1), 24)
            self.assertEqual(server.load_or_create_token(p), t1)


class InvalidRowsEverywhere(Base):
    def _zero_row(self):
        db.insert_reading(self.conn, probe_row(ts="2026-11-08T15:04:00+00:00", car_ts="", soc=0.0, odometer_km=0.0,
                                              byd_range_km=0.0, r50_rate=None, gear="UNKNOWN"))

    def test_status_skips_an_empty_snapshot_even_if_it_is_the_newest_row(self):
        db.insert_reading(self.conn, probe_row())
        self._zero_row()                                              # newest id, but not a reading
        s = server.build_status(self.conn, now=NOW)
        self.assertTrue(s["has_data"])
        self.assertEqual(s["reading"]["soc"], 95.0)                   # not 0%

    def test_only_empty_snapshots_means_no_data(self):
        self._zero_row()
        self.assertFalse(server.build_status(self.conn, now=NOW)["has_data"])

    def test_chart_skips_empty_snapshots(self):
        db.insert_reading(self.conn, probe_row())
        self._zero_row()
        series = server.build_series(self.conn, hours=24, now=NOW)
        self.assertTrue(all(p["soc"] > 0 for p in series))

    def test_recent_readings_skip_them(self):
        db.insert_reading(self.conn, probe_row())
        self._zero_row()
        self.assertTrue(all(r["soc"] > 0 and r["odometer_km"] > 0 for r in db.recent_readings(self.conn)))

    def test_range_engine_ignores_them(self):
        rows = [{"soc": 80 - i, "odometer_km": 1000.0 + 5 * i} for i in range(11)]
        clean = re_.estimate(rows, soc_now=70)
        dirty = list(rows)
        dirty.insert(5, {"soc": 0.0, "odometer_km": 0.0})
        dirty.append({"soc": 0.0, "odometer_km": 0.0})
        r = re_.estimate(dirty, soc_now=70)
        self.assertEqual(r.status, "ok")
        self.assertAlmostEqual(r.true_range_km, clean.true_range_km, delta=1.0)


class ChargingApi(Base):
    def _fill(self):
        import test_charging
        rows, _ = test_charging.charge(0, 40.0, 7.4, 150, poll_s=300, with_power=True)
        for r in rows:
            db.insert_reading(self.conn, r)
        return rows

    def _start(self, token=None):
        srv = server.make_server("127.0.0.1", 0, token=token, capacity_kwh=49.92, usable_fraction=1.0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _with_settings(self, text):
        import tempfile as _t
        d = _t.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "s.json"
        p.write_text(text, encoding="utf-8")
        old = server.settings.SETTINGS_FILE
        server.settings.SETTINGS_FILE = p
        self.addCleanup(setattr, server.settings, "SETTINGS_FILE", old)

    def test_list_and_detail(self):
        self._fill()
        port = self._start()
        d = json.loads(request(port, "/api/charging")[2])
        self.assertEqual(len(d["sessions"]), 1)
        s = d["sessions"][0]
        self.assertTrue(s["observed"])
        self.assertAlmostEqual(s["energy_kwh"], 7.4 * 150 / 60, delta=0.7)
        self.assertAlmostEqual(s["avg_kw"], 7.4, delta=0.6)
        self.assertEqual(d["totals"]["sessions"], 1)
        self.assertFalse(d["has_tariff"])
        self.assertIsNone(s["cost"])                                     # nothing assumed
        st, _, body = request(port, f"/api/charging/{s['id']}")
        det = json.loads(body)
        self.assertEqual(st, 200)
        self.assertGreater(len(det["curve"]), 10)
        self.assertGreater(len(det["ticks"]), 5)

    def test_tariff_from_settings_gives_a_cost(self):
        self._fill()
        self._with_settings('{"tariff_per_kwh": 60, "currency": "PKR", "charging_loss_pct": 10}')
        d = json.loads(request(self._start(), "/api/charging")[2])
        s = d["sessions"][0]
        self.assertTrue(d["has_tariff"])
        self.assertEqual(d["currency"], "PKR")
        self.assertAlmostEqual(s["wall_kwh"], s["energy_kwh"] / 0.9, places=1)
        self.assertAlmostEqual(s["cost"], s["wall_kwh"] * 60, delta=0.6)       # wall_kwh is rounded to 0.01 kWh (x60 = 0.6)
        self.assertAlmostEqual(d["totals"]["cost"], s["cost"], delta=0.6)

    def test_no_coordinates_anywhere(self):
        self._fill()
        port = self._start()
        sid = json.loads(request(port, "/api/charging")[2])["sessions"][0]["id"]
        for path in ("/api/charging", f"/api/charging/{sid}"):
            body = request(port, path)[2].decode()
            for banned in ('"lat"', '"lon"', "vin", "token"):
                self.assertNotIn(banned, body, path)

    def test_bad_ids_are_404(self):
        self._fill()
        port = self._start()
        for p in ("/api/charging/abc", "/api/charging/99999999999999", "/api/charging/-1", "/api/charging/",
                  "/api/charging/1/2", "/api/charging/12345678"):
            self.assertEqual(request(port, p)[0], 404, p)

    def test_needs_the_token_when_exposed(self):
        self._fill()
        tok = "t" * 32
        port = self._start(tok)
        self.assertEqual(request(port, "/api/charging")[0], 401)
        self.assertEqual(request(port, "/api/charging", {"Cookie": f"byd_token={tok}"})[0], 200)

    def test_reading_a_finished_log_does_not_wake_the_car(self):
        import test_charging
        rows, _ = test_charging.charge(0, 40.0, 7.4, 150, poll_s=300, with_power=True, tail=3)    # unplugged long ago
        for r in rows:
            db.insert_reading(self.conn, r)
        flag = db.DB_PATH.parent / "viewer_active"
        if flag.exists():
            flag.unlink()
        server._last_touch = float("-inf")
        port = self._start()
        self.assertFalse(json.loads(request(port, "/api/charging")[2])["totals"]["ongoing"])
        request(port, "/charging.js")
        self.assertFalse(flag.exists())

    def test_watching_a_charge_in_progress_does_ask_the_poller_to_keep_polling(self):
        self._fill()                                                            # the data ends mid-charge
        flag = db.DB_PATH.parent / "viewer_active"
        if flag.exists():
            flag.unlink()
        server._last_touch = float("-inf")
        port = self._start()
        d = json.loads(request(port, "/api/charging")[2])
        self.assertTrue(d["totals"]["ongoing"])
        self.assertTrue(flag.exists())                                          # the car is awake charging: poll for the page
        flag.unlink()
        server._last_touch = float("-inf")
        sid = d["sessions"][0]["id"]
        request(port, f"/api/charging/{sid}")
        self.assertTrue(flag.exists())

    def test_an_unauthenticated_request_never_triggers_polling_even_while_charging(self):
        self._fill()
        flag = db.DB_PATH.parent / "viewer_active"
        if flag.exists():
            flag.unlink()
        server._last_touch = float("-inf")
        port = self._start("t" * 32)
        self.assertEqual(request(port, "/api/charging")[0], 401)
        self.assertFalse(flag.exists())

    def test_empty_database(self):
        d = json.loads(request(self._start(), "/api/charging")[2])
        self.assertEqual(d["sessions"], [])
        self.assertEqual(d["totals"]["sessions"], 0)

def load_fresh(name):
    """A new copy of cloud/<name>.py, so its INITIAL module-level values can be checked (other tests change the shared copy)."""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / (name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_fresh", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FreshBootServer(unittest.TestCase):
    def test_the_first_request_after_boot_still_asks_the_poller_to_poll(self):
        import time as _time
        from types import SimpleNamespace as _NS
        fresh = load_fresh("server")
        self.assertEqual(fresh._last_touch, float("-inf"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = db.DB_PATH
        db.DB_PATH = Path(tmp.name) / "h.sqlite"
        self.addCleanup(setattr, db, "DB_PATH", old)
        with unittest.mock.patch.object(fresh, "time", _NS(monotonic=lambda: 1.0, time=_time.time)):
            fresh.touch_viewer()                                                    # one second since "boot"
        self.assertTrue((db.DB_PATH.parent / "viewer_active").exists())


if __name__ == "__main__":
    unittest.main()
