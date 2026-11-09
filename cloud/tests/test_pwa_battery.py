"""Battery size (default, 'not set' flag, settings endpoint) and the installable-app (PWA) plumbing."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import range_engine  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402

HDRS = {"Content-Type": "application/json", "X-BYD-App": "1"}
TOKEN = "t" * 32


def req(port, path, method="GET", headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    c.close()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        old = (db.DB_PATH, settings.SETTINGS_FILE)
        db.DB_PATH = root / "data" / "history.sqlite"
        settings.SETTINGS_FILE = None
        self.addCleanup(lambda: (setattr(db, "DB_PATH", old[0]), setattr(settings, "SETTINGS_FILE", old[1])))
        self.settings_file = root / "data" / "settings.json"
        # an old settings.json next to the code must not leak into these tests
        self._legacy = settings.LEGACY_FILE
        settings.LEGACY_FILE = root / "nope.json"
        self.addCleanup(setattr, settings, "LEGACY_FILE", self._legacy)

    def start(self, token=None):
        srv = server.make_server("127.0.0.1", 0, token=token, capacity_kwh=49.92, usable_fraction=1.0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]


class BatterySettings(Base):
    def test_default_is_standard_range_and_says_it_is_not_set(self):
        self.assertEqual(range_engine.DEFAULT_USABLE_KWH, 49.92)
        s = settings.load()
        self.assertEqual(s["battery_kwh"], 49.92)
        self.assertFalse(s["battery_set"])

    def test_save_sets_the_flag_and_keeps_other_keys(self):
        self.settings_file.parent.mkdir(parents=True)
        self.settings_file.write_text(json.dumps({"range_mode": "own", "currency": "PKR"}), encoding="utf-8")
        self.assertEqual(settings.save_battery(60.48), 60.48)
        s = settings.load()
        self.assertEqual((s["battery_kwh"], s["battery_set"], s["range_mode"], s["currency"]), (60.48, True, "own", "PKR"))

    def test_save_rejects_nonsense(self):
        for bad in (True, "49.92", None, float("nan"), 5, 500, [1]):
            with self.assertRaises(ValueError):
                settings.save_battery(bad)
        self.assertFalse(self.settings_file.exists())

    def test_old_file_next_to_the_code_is_still_read_and_new_one_wins(self):
        legacy = Path(self.tmp.name) / "legacy.json"
        legacy.write_text('{"battery_kwh": 60.48}', encoding="utf-8")
        settings.LEGACY_FILE = legacy
        self.assertEqual(settings.load()["battery_kwh"], 60.48)
        self.assertTrue(settings.load()["battery_set"])
        settings.save_battery(49.92)                       # written to the data dir, not to the legacy file
        self.assertEqual(json.loads(legacy.read_text())["battery_kwh"], 60.48)
        self.assertEqual(settings.load()["battery_kwh"], 49.92)


class BatteryApi(Base):
    def test_status_and_account_report_the_size_and_whether_it_is_set(self):
        port = self.start()
        st = json.loads(req(port, "/api/status")[2])
        self.assertEqual((st["battery_kwh"], st["battery_set"]), (49.92, False))
        acct = json.loads(req(port, "/api/account")[2])
        self.assertEqual(acct["battery"]["kwh"], 49.92)
        self.assertEqual([o["kwh"] for o in acct["battery"]["options"]], [49.92, 60.48])

    def test_post_saves_and_takes_effect_at_once(self):
        port = self.start()
        c, _, body = req(port, "/api/settings", "POST", HDRS, json.dumps({"battery_kwh": 60.48}))
        self.assertEqual(c, 200, body)
        st = json.loads(req(port, "/api/status")[2])
        self.assertEqual((st["battery_kwh"], st["battery_set"]), (60.48, True))
        self.assertEqual(json.loads(self.settings_file.read_text())["battery_kwh"], 60.48)

    def test_post_validation_and_csrf(self):
        port = self.start()
        for bad in ({"battery_kwh": 5}, {"battery_kwh": "x"}, {"battery_kwh": True}, {}):
            self.assertEqual(req(port, "/api/settings", "POST", HDRS, json.dumps(bad))[0], 400, bad)
        no_csrf = {"Content-Type": "application/json"}
        self.assertEqual(req(port, "/api/settings", "POST", no_csrf, '{"battery_kwh": 60}')[0], 403)
        evil = dict(HDRS, Origin="http://evil.example")
        self.assertEqual(req(port, "/api/settings", "POST", evil, '{"battery_kwh": 60}')[0], 403)
        self.assertFalse(self.settings_file.exists())

    def test_post_needs_the_login_cookie_in_token_mode(self):
        port = self.start(TOKEN)
        self.assertEqual(req(port, "/api/settings", "POST", HDRS, '{"battery_kwh": 60}')[0], 401)
        ok = dict(HDRS, Cookie=f"byd_token={TOKEN}")
        self.assertEqual(req(port, "/api/settings", "POST", ok, '{"battery_kwh": 60}')[0], 200)

    def test_other_posts_are_still_refused(self):
        port = self.start()
        self.assertEqual(req(port, "/api/anything", "POST", HDRS, "{}")[0], 405)


class Pwa(Base):
    ICONS = ("/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png", "/icons/apple-touch-icon.png")

    def test_manifest_icons_and_worker_are_served(self):
        port = self.start()
        c, h, b = req(port, "/manifest.webmanifest")
        self.assertEqual(c, 200)
        self.assertIn("manifest+json", h["Content-Type"])
        m = json.loads(b)
        self.assertEqual((m["display"], m["start_url"], m["scope"]), ("standalone", "/", "/"))
        sizes = {i["sizes"] for i in m["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        self.assertTrue(any(i["purpose"] == "maskable" for i in m["icons"]))
        for i in m["icons"]:
            self.assertEqual(req(port, i["src"])[0], 200, i["src"])
        for p in self.ICONS:
            c, h, b = req(port, p)
            self.assertEqual((c, b[:8]), (200, b"\x89PNG\r\n\x1a\n"), p)
        c, h, b = req(port, "/sw.js")
        self.assertEqual(c, 200)
        self.assertNotIn(b"__VERSION__", b)
        self.assertEqual(h["Service-Worker-Allowed"], "/")

    def test_csp_allows_manifest_and_worker_but_nothing_external(self):
        port = self.start()
        csp = req(port, "/")[1]["Content-Security-Policy"]
        for want in ("manifest-src 'self'", "worker-src 'self'", "default-src 'none'", "script-src 'self'", "form-action 'none'"):
            self.assertIn(want, csp)

    def test_worker_never_caches_data(self):
        text = (server.WEB_DIR / "sw.js").read_text(encoding="utf-8")
        self.assertIn('url.pathname.startsWith("/api/")) return;', text)
        shell = text[text.index("const SHELL"):text.index("];", text.index("const SHELL"))]
        self.assertNotIn("/api", shell)
        self.assertIn("if (!SHELL.includes(url.pathname)) return;", text)      # only the shell is ever answered from cache
        self.assertNotIn("caches.match(req", text)

    def test_worker_cache_name_changes_with_the_shell(self):
        a = server.shell_version()
        self.assertRegex(a, r"^[0-9a-f]{12}$")
        orig = server.WEB_DIR
        with tempfile.TemporaryDirectory() as d:
            for name in {v[0] for v in server.STATIC.values()}:
                src = orig / name
                dst = Path(d) / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            server.WEB_DIR = Path(d)
            try:
                self.assertEqual(server.shell_version(), a)
                (Path(d) / "app.js").write_bytes(b"changed")
                self.assertNotEqual(server.shell_version(), a)
            finally:
                server.WEB_DIR = orig

    def test_every_file_the_worker_caches_exists(self):
        import re
        text = (server.WEB_DIR / "sw.js").read_text(encoding="utf-8")
        shell = re.findall(r'"(/[^"]*)"', text[text.index("const SHELL"):text.index("];", text.index("const SHELL"))])
        self.assertIn("/", shell)
        for p in shell:
            self.assertIn(p, server.STATIC, p)

    def test_index_links_manifest_icons_and_script(self):
        html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
        for want in ('rel="manifest" href="/manifest.webmanifest"', 'rel="apple-touch-icon"', 'src="/pwa.js"', 'id="installbtn"'):
            self.assertIn(want, html)

    def test_token_mode_public_assets_vs_private_ones(self):
        port = self.start(TOKEN)
        # the browser fetches these without the cookie
        for p in ("/manifest.webmanifest", "/sw.js", "/app.css") + self.ICONS:
            self.assertEqual(req(port, p)[0], 200, p)
        # everything else stays private, including the app's own scripts and every data endpoint
        for p in ("/app.js", "/pwa.js", "/api/status", "/api/trips", "/api/account", "/api/charging", "/api/battery"):
            self.assertEqual(req(port, p)[0], 401, p)

    def test_unauthorised_home_page_is_a_token_form_not_a_dead_end(self):
        port = self.start(TOKEN)
        c, h, b = req(port, "/")
        self.assertEqual(c, 401)
        self.assertIn("text/html", h["Content-Type"])
        self.assertIn(b'name="token"', b)
        self.assertIn("form-action 'self'", h["Content-Security-Policy"])
        self.assertNotIn(TOKEN.encode(), b)

    def test_token_link_sets_a_long_lived_cookie(self):
        port = self.start(TOKEN)
        c, h, _ = req(port, f"/?token={TOKEN}")
        self.assertEqual(c, 302)
        self.assertIn("Max-Age=31536000", h["Set-Cookie"])
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertIn("SameSite=Strict", h["Set-Cookie"])

    def test_wrong_token_on_the_form_gets_the_form_again(self):
        port = self.start(TOKEN)
        c, h, b = req(port, "/?token=wrong")
        self.assertEqual(c, 401)
        self.assertIn(b'name="token"', b)
        self.assertNotIn("Set-Cookie", h)


if __name__ == "__main__":
    unittest.main()
