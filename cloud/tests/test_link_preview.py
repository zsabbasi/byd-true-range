"""Link previews (WhatsApp, Telegram ...): Open Graph tags, a public preview image, and a 200 page for preview bots."""
import http.client
import os
import re
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402

TOKEN = "t" * 32


def get(port, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    c.close()
    return out


class Preview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = (db.DB_PATH, settings.SETTINGS_FILE)
        db.DB_PATH = Path(self.tmp.name) / "data" / "h.sqlite"
        self.addCleanup(lambda: (setattr(db, "DB_PATH", old[0]), setattr(settings, "SETTINGS_FILE", old[1])))

    def start(self, token=None, **kw):
        srv = server.make_server("127.0.0.1", 0, token=token, capacity_kwh=49.92, usable_fraction=1.0, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_the_home_page_has_open_graph_tags_with_an_absolute_image_address(self):
        port = self.start()
        st, _, b = get(port, "/")
        t = b.decode()
        self.assertEqual(st, 200)
        for want in ('property="og:title" content="BYD true range"', 'property="og:description"', 'property="og:type"',
                     'name="twitter:card" content="summary_large_image"', 'name="description"',
                     f'property="og:image" content="http://127.0.0.1:{port}/icons/social-card.png"',
                     'property="og:image:width" content="1200"', 'property="og:image:height" content="630"'):
            self.assertIn(want, t)
        self.assertNotIn("<!--OG-->", t)

    def test_https_is_used_when_the_proxy_says_so(self):
        port = self.start()
        t = get(port, "/", {"X-Forwarded-Proto": "https"})[2].decode()
        self.assertIn(f'content="https://127.0.0.1:{port}/icons/social-card.png"', t)
        port2 = self.start(assume_https=True)
        self.assertIn("https://127.0.0.1", get(port2, "/")[2].decode())

    def test_the_host_header_cannot_inject_html_into_the_tags(self):
        port = self.start(TOKEN)
        for evil in ('x"><script>alert(1)</script>', "a b", "x/../y", "<b>", "a.b.c:99999999", "-bad.example", ""):
            _, _, b = get(port, "/?probe=1", {"Host": evil, "Cookie": f"byd_token={TOKEN}"})
            t = b.decode()
            self.assertNotIn("<script>alert", t, evil)
            self.assertNotIn('og:image" content="http', t, evil)           # not a host name: no image address is built from it
            self.assertIn('og:title', t)
        _, _, b = get(port, "/", {"Host": "my-byd.example.com", "Cookie": f"byd_token={TOKEN}"})
        self.assertIn('content="http://my-byd.example.com/icons/social-card.png"', b.decode())

    def test_the_preview_image_is_a_public_png_of_a_sensible_size(self):
        port = self.start(TOKEN)
        st, h, b = get(port, "/icons/social-card.png")          # no cookie: the preview bot has none
        self.assertEqual((st, h["Content-Type"]), (200, "image/png"))
        self.assertEqual(b[:8], b"\x89PNG\r\n\x1a\n")
        w, hgt = struct.unpack(">II", b[16:24])
        self.assertEqual((w, hgt), (1200, 630))
        self.assertLess(len(b), 300_000)                           # WhatsApp ignores big preview images

    def test_a_private_server_still_gives_preview_bots_a_200_page_and_people_a_401(self):
        port = self.start(TOKEN)
        st, _, b = get(port, "/", {"User-Agent": "WhatsApp/2.24.1 A"})
        t = b.decode()
        self.assertEqual(st, 200)
        self.assertIn('og:title', t)
        self.assertNotIn(TOKEN, t)
        self.assertNotIn("<form", t)                              # nothing to sign in with, nothing private
        for ua in ("facebookexternalhit/1.1", "TelegramBot (like TwitterBot)", "Slackbot-LinkExpanding 1.0", "Twitterbot/1.0"):
            self.assertEqual(get(port, "/", {"User-Agent": ua})[0], 200, ua)
        st, h, b = get(port, "/", {"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120"})
        self.assertEqual(st, 401)                                  # a person without the token still gets the token form
        self.assertIn(b'name="token"', b)
        self.assertIn(b'og:title', b)                              # which carries the tags too

    def test_preview_bots_get_nothing_but_the_home_page(self):
        port = self.start(TOKEN)
        for p in ("/api/status", "/api/trips", "/api/account", "/app.js"):
            self.assertEqual(get(port, p, {"User-Agent": "WhatsApp/2.24"})[0], 401, p)

    def test_a_tokened_link_ends_at_a_preview_the_bot_can_read(self):
        port = self.start(TOKEN)
        st, h, _ = get(port, f"/?token={TOKEN}", {"User-Agent": "WhatsApp/2.24"})
        self.assertEqual((st, h["Location"]), (302, "/"))
        self.assertEqual(get(port, h["Location"], {"User-Agent": "WhatsApp/2.24"})[0], 200)   # the bot follows, without the cookie

    def test_a_shared_server_page_carries_the_tags_too(self):
        port = self.start(multi_user=True)
        t = get(port, "/", {"Host": "friends.example.com", "X-Forwarded-Proto": "https"})[2].decode()
        self.assertIn('content="https://friends.example.com/icons/social-card.png"', t)


if __name__ == "__main__":
    unittest.main()
