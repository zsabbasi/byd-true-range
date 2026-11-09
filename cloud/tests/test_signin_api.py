"""Sign-in API: the password goes only to BYD, only from the local machine, is stored encrypted, and is never echoed."""
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
sys.path.insert(0, os.path.dirname(__file__))

import byd_config  # noqa: E402
import credstore  # noqa: E402
import db  # noqa: E402
import pollstatus  # noqa: E402
import server  # noqa: E402

PASSWORD = "Sup3r-S3cret-pa$$word!"
GOOD = {"username": "sub.account@example.com", "password": PASSWORD, "region": "sg", "country": "pk",
        "time_zone": "Asia/Karachi", "acknowledged": True}
HDRS = {"Content-Type": "application/json", "X-BYD-App": "1"}


class BydAuthenticationError(Exception):
    code = "3008"


class BydTransportError(Exception):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        db.DB_PATH = self.root / "data" / "history.sqlite"
        key = self.root / "k.key"
        key.write_bytes(b"0123456789abcdef0123456789abcdef")
        os.chmod(key, 0o600)
        self.env = mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(key)})
        self.env.start()
        os.environ.pop("CREDENTIALS_DIRECTORY", None)
        # The deployment target is Linux: test the AES store everywhere, including on a Windows development machine.
        self.scheme = mock.patch.object(credstore, "default_scheme", lambda: "aes")
        self.scheme.start()
        server._login_times.clear()
        self.calls = []
        self.fake_result = [{"model": "ATTO3", "vin_tail": "0000"}]

        def fake_login(creds, timeout=25.0):
            self.calls.append(dict(creds))
            if isinstance(self.fake_result, Exception):
                raise self.fake_result
            return self.fake_result

        self.login = mock.patch.object(server, "do_login", fake_login)
        self.login.start()

    def tearDown(self):
        self.scheme.stop()
        self.login.stop()
        self.env.stop()
        self.tmp.cleanup()

    def start(self, token=None):
        srv = server.make_server("127.0.0.1", 0, token=token, capacity_kwh=49.92, usable_fraction=1.0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def req(self, port, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=headers if headers is not None else {})
        r = c.getresponse()
        out = (r.status, r.read())
        c.close()
        return out

    def post(self, port, path="/api/login", body=None, headers=None, raw=None):
        h = dict(HDRS) if headers is None else headers
        st, b = self.req(port, "POST", path, GOOD if body is None and raw is None else body, h, raw)
        try:
            return st, json.loads(b)
        except ValueError:
            return st, {"raw": b.decode(errors="replace")}


class Success(Base):
    def test_a_good_sign_in_is_stored_encrypted_and_reported(self):
        port = self.start()
        st, d = self.post(port)
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        self.assertEqual(d["vehicles"], [{"model": "ATTO3", "vin_tail": "0000"}])
        stored = credstore.load()
        self.assertEqual(stored["username"], "sub.account@example.com")
        self.assertEqual(stored["password"], PASSWORD)
        self.assertEqual(stored["base_url"], "https://dilinkappoversea-sg.byd.auto")
        self.assertEqual(stored["country_code"], "PK")                       # upper-cased
        self.assertEqual(pollstatus.read()["state"], "starting")             # the poller is told something changed

    def test_what_is_sent_to_byd_is_exactly_what_was_typed_plus_the_region_host(self):
        self.post(self.start())
        self.assertEqual(len(self.calls), 1)
        c = self.calls[0]
        self.assertEqual((c["username"], c["password"], c["base_url"]), (GOOD["username"], PASSWORD, "https://dilinkappoversea-sg.byd.auto"))
        self.assertTrue(byd_config.is_byd_host(c["base_url"]))

    def test_the_password_is_never_in_any_response(self):
        port = self.start()
        for result in ([{"model": "ATTO3", "vin_tail": "0000"}], BydAuthenticationError("bad " + PASSWORD), BydTransportError("net " + PASSWORD)):
            self.fake_result = result
            server._login_times.clear()
            st, b = self.req(port, "POST", "/api/login", GOOD, HDRS)
            self.assertNotIn(PASSWORD.encode(), b, st)
            self.assertNotIn(b"Sup3r", b)
        st, b = self.req(port, "GET", "/api/account")
        self.assertNotIn(PASSWORD.encode(), b)
        st, b = self.req(port, "GET", "/api/status")
        self.assertNotIn(PASSWORD.encode(), b)

    def test_the_password_is_not_readable_in_the_stored_file(self):
        self.post(self.start())
        raw = credstore.path().read_bytes()
        for secret in (PASSWORD, "Sup3r", "sub.account", "example.com"):
            self.assertNotIn(secret.encode(), raw)

    def test_a_time_zone_that_is_not_a_time_zone_falls_back_to_utc(self):
        self.post(self.start(), body=dict(GOOD, time_zone="../../etc/passwd"))
        self.assertEqual(credstore.load()["time_zone"], "UTC")


class Failures(Base):
    def test_byd_rejecting_the_login_is_reported_with_its_code_and_nothing_is_stored(self):
        self.fake_result = BydAuthenticationError("Login failed: code=3008 message=Wrong email or password")
        st, d = self.post(self.start())
        self.assertEqual(st, 401)
        self.assertEqual(d["error"], "rejected")
        self.assertEqual(d["code"], "3008")
        self.assertIn("region", d["hint"])
        self.assertFalse(credstore.exists())

    def test_a_missing_pybyd_says_so_instead_of_blaming_the_network(self):
        self.fake_result = ModuleNotFoundError("No module named 'pybyd'")
        st, d = self.post(self.start())
        self.assertEqual((st, d["error"]), (500, "setup"))
        self.assertIn("require-hashes", d["message"])
        self.assertFalse(credstore.exists())

    def test_an_unreachable_byd_is_a_502_and_nothing_is_stored(self):
        self.fake_result = BydTransportError("getaddrinfo failed")
        st, d = self.post(self.start())
        self.assertEqual(st, 502)
        self.assertEqual(d["error"], "unreachable")
        self.assertFalse(credstore.exists())

    def test_a_storage_failure_after_a_good_login_saves_nothing_and_says_why(self):
        port = self.start()
        with mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(self.root / "missing.key")}):
            st, d = self.post(port)
        self.assertEqual(st, 500)
        self.assertEqual(d["error"], "store")
        self.assertIn("Nothing was saved", d["message"])
        self.assertFalse(credstore.exists())

    def test_failed_attempts_do_not_replace_an_existing_sign_in(self):
        port = self.start()
        self.post(port)
        self.fake_result = BydAuthenticationError("bad")
        self.post(port, body=dict(GOOD, username="other@example.com"))
        self.assertEqual(credstore.load()["username"], "sub.account@example.com")


class Validation(Base):
    def test_invalid_input_is_a_400_and_never_reaches_byd(self):
        port = self.start()
        for field, bad in (("username", ""), ("username", "ab"), ("username", 5), ("username", "a\x00b@c.com"), ("password", ""),
                           ("password", 7), ("password", "x" * 300), ("password", "a\nb"), ("region", "evil"), ("region", "../x"),
                           ("region", None), ("country", "PAK"), ("country", "1"), ("country", None), ("acknowledged", False),
                           ("acknowledged", "yes"), ("acknowledged", None)):
            server._login_times.clear()
            st, d = self.post(port, body=dict(GOOD, **{field: bad}))
            self.assertEqual(st, 400, (field, bad))
            self.assertEqual(d["error"], "invalid")
            self.assertEqual(d["field"], field)
            self.assertNotIn(PASSWORD, json.dumps(d))
        self.assertEqual(self.calls, [])

    def test_the_sub_account_confirmation_is_enforced_by_the_server_not_just_the_page(self):
        st, d = self.post(self.start(), body={k: v for k, v in GOOD.items() if k != "acknowledged"})
        self.assertEqual(st, 400)
        self.assertEqual(d["field"], "acknowledged")
        self.assertIn("sub-account", d["message"])

    def test_the_host_is_always_one_of_byds_regional_servers(self):
        for region in byd_config.REGIONS:
            self.assertTrue(byd_config.is_byd_host(byd_config.base_url_for(region)), region)
        for evil in ("https://evil.example.com", "http://dilinkappoversea-sg.byd.auto", "https://dilinkappoversea-sg.byd.auto.evil.com",
                     "https://dilinkappoversea-sg.byd.auto@evil.com", "https://xdilinkappoversea-sg.byd.auto", "", None):
            self.assertFalse(byd_config.is_byd_host(evil), evil)

    def test_malformed_bodies(self):
        port = self.start()
        self.assertEqual(self.post(port, raw=b"{not json")[0], 400)
        self.assertEqual(self.post(port, raw=b"[1,2,3]")[0], 400)
        self.assertEqual(self.post(port, raw=b"x" * (server.MAX_BODY + 10))[0], 413)
        h = dict(HDRS)
        self.assertEqual(self.post(port, raw=b"", headers=h)[0], 400)           # empty object: invalid fields, not a crash

    def test_a_missing_content_length_is_refused(self):
        port = self.start()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.putrequest("POST", "/api/login")
        for k, v in HDRS.items():
            c.putheader(k, v)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 411)
        c.close()


class Attempts(Base):
    def test_at_most_five_attempts_in_ten_minutes_and_the_sixth_never_reaches_byd(self):
        port = self.start()
        self.fake_result = BydAuthenticationError("bad")
        codes = [self.post(port)[0] for _ in range(7)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5:], [429, 429])
        self.assertEqual(len(self.calls), 5)

    def test_invalid_input_does_not_burn_attempts(self):
        port = self.start()
        for _ in range(10):
            self.post(port, body=dict(GOOD, username=""))
        self.assertEqual(self.post(port)[0], 200)

    def test_the_window_expires(self):
        port = self.start()
        self.fake_result = BydAuthenticationError("bad")
        for _ in range(5):
            self.post(port)
        self.assertEqual(self.post(port)[0], 429)
        server._login_times[:] = [t - server.LOGIN_WINDOW_S - 1 for t in server._login_times]
        self.assertEqual(self.post(port)[0], 401)


class LocalOnly(Base):
    """Passwords never travel through a tunnel, a proxy, or the network."""

    def test_a_tunnel_or_proxy_header_means_not_local(self):
        port = self.start()
        for h in ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Via", "X-Real-Ip", "CF-Connecting-IP",
                  "True-Client-IP", "X-Original-Host", "X-Ngrok-Trace-Id"):
            st, d = self.post(port, headers=dict(HDRS, **{h: "203.0.113.9"}))
            self.assertEqual(st, 403, h)
            self.assertEqual(d["error"], "local-only")
        self.assertEqual(self.calls, [])

    def test_a_non_loopback_host_header_is_refused(self):
        port = self.start()
        for host in ("abc123.ngrok-free.app", "192.168.1.5:8765", "evil.example.com"):
            st, d = self.post(port, headers=dict(HDRS, Host=host))
            self.assertEqual(st, 403, host)
        self.assertEqual(self.calls, [])

    def test_a_non_loopback_client_address_is_refused(self):
        port = self.start()
        with mock.patch.object(server, "is_local_client", lambda addr: False):
            st, d = self.post(port)
        self.assertEqual(st, 403)
        self.assertEqual(d["error"], "local-only")

    def test_even_a_valid_token_does_not_unlock_remote_sign_in(self):
        tok = "t" * 32
        port = self.start(tok)
        h = dict(HDRS, Cookie=f"byd_token={tok}", Host="abc.ngrok-free.app")
        self.assertEqual(self.post(port, headers=h)[0], 403)
        h2 = dict(HDRS, Cookie=f"byd_token={tok}", **{"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(self.post(port, headers=h2)[0], 403)
        self.assertEqual(self.calls, [])

    def test_in_token_mode_the_token_is_still_required_locally(self):
        tok = "t" * 32
        port = self.start(tok)
        self.assertEqual(self.post(port)[0], 401)                                   # no cookie
        self.assertEqual(self.post(port, headers=dict(HDRS, Cookie="byd_token=wrong"))[0], 401)
        self.assertEqual(self.post(port, headers=dict(HDRS, Cookie=f"byd_token={tok}"))[0], 200)   # e.g. over an SSH tunnel

    def test_the_account_page_tells_the_browser_whether_it_may_sign_in(self):
        port = self.start()
        local = json.loads(self.req(port, "GET", "/api/account")[1])
        self.assertTrue(local["local"])
        st, b = self.req(port, "GET", "/api/account", headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertFalse(json.loads(b)["local"])

    def test_ipv4_mapped_ipv6_loopback_counts_as_local(self):
        for ok in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "127.5.5.5"):
            self.assertTrue(server.is_local_client(ok), ok)
        for bad in ("192.168.1.5", "8.8.8.8", "::ffff:8.8.8.8", "fe80::1", "", "garbage"):
            self.assertFalse(server.is_local_client(bad), bad)


class Csrf(Base):
    """A web page in the owner's browser must not be able to sign this app in to someone else's account."""

    def test_the_custom_header_is_required(self):
        port = self.start()
        st, d = self.post(port, headers={"Content-Type": "application/json"})
        self.assertEqual((st, d["error"]), (403, "csrf"))
        self.assertEqual(self.post(port, headers={"Content-Type": "application/json", "X-BYD-App": "0"})[0], 403)
        self.assertEqual(self.calls, [])

    def test_a_cross_site_origin_is_refused(self):
        port = self.start()
        h = dict(HDRS, Origin="https://evil.example.com")
        self.assertEqual(self.post(port, headers=h)[0], 403)
        h = dict(HDRS, Origin=f"http://localhost:{port}", Host=f"localhost:{port}")
        self.assertEqual(self.post(port, headers=h)[0], 200)                       # the real page: same origin
        self.assertEqual(len(self.calls), 1)

    def test_only_json_is_accepted(self):
        port = self.start()
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            h = {"X-BYD-App": "1", "Content-Type": ctype} if ctype else {"X-BYD-App": "1"}
            self.assertEqual(self.post(port, headers=h)[0], 415, ctype)
        self.assertEqual(self.calls, [])

    def test_get_on_the_sign_in_endpoints_does_nothing(self):
        port = self.start()
        self.assertEqual(self.req(port, "GET", "/api/login")[0], 404)
        self.assertEqual(self.req(port, "GET", "/api/logout")[0], 404)
        self.assertEqual(self.calls, [])

    def test_other_methods_and_paths_stay_read_only(self):
        port = self.start()
        for m in ("PUT", "DELETE", "PATCH"):
            self.assertEqual(self.req(port, m, "/api/login", {}, HDRS)[0], 405, m)
        self.assertEqual(self.req(port, "POST", "/api/status", {}, HDRS)[0], 405)
        self.assertEqual(self.req(port, "POST", "/api/trips", {}, HDRS)[0], 405)


class SignOut(Base):
    def test_sign_out_deletes_the_stored_sign_in(self):
        port = self.start()
        self.post(port)
        self.assertTrue(credstore.exists())
        st, d = self.post(port, "/api/logout", body={})
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d, {"ok": True})
        self.assertFalse(credstore.exists())
        self.assertEqual(pollstatus.read()["state"], "waiting-for-signin")
        acct = json.loads(self.req(port, "GET", "/api/account")[1])
        self.assertFalse(acct["signed_in"])

    def test_sign_out_keeps_the_csrf_protections(self):
        port = self.start()
        self.post(port)
        self.assertEqual(self.post(port, "/api/logout", body={}, headers={"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.post(port, "/api/logout", body={}, headers=dict(HDRS, Origin="http://evil.example"))[0], 403)
        self.assertTrue(credstore.exists())

    def test_sign_out_works_from_the_phone_but_sign_in_does_not(self):
        """Signing out only stops reading the car, so it works through a tunnel or proxy (phone). Signing in never does."""
        port = self.start()
        self.post(port)
        tunnel = dict(HDRS, **{"X-Forwarded-For": "1.1.1.1"})
        self.assertEqual(self.post(port, "/api/login", body=GOOD, headers=tunnel)[0], 403)
        self.assertEqual(self.post(port, "/api/logout", body={}, headers=tunnel)[0], 200)
        self.assertFalse(credstore.exists())


class Account(Base):
    def test_not_signed_in(self):
        d = json.loads(self.req(self.start(), "GET", "/api/account")[1])
        self.assertFalse(d["signed_in"])
        self.assertIsNone(d["user"])
        self.assertIn("sg", [r["id"] for r in d["regions"]])
        self.assertEqual(d["confirmed"], {"PK": "sg"})

    def test_signed_in_shows_a_masked_user_the_region_and_the_protection(self):
        port = self.start()
        self.post(port)
        d = json.loads(self.req(port, "GET", "/api/account")[1])
        self.assertTrue(d["signed_in"])
        self.assertNotIn("source", d)
        self.assertEqual(d["region"], "sg")
        self.assertEqual(d["country"], "PK")
        self.assertTrue(d["store"]["readable"])
        self.assertIn("AES-256-GCM", d["store"]["protection"])
        self.assertIn("key", (d["store"]["key_source"] or ""))
        self.assertNotIn("sub.account", json.dumps(d))                            # masked
        self.assertNotIn("example.com", json.dumps(d))
        self.assertTrue(d["user"].startswith("s***@"))

    def test_status_carries_signed_in_so_the_page_can_offer_sign_in(self):
        port = self.start()
        self.assertFalse(json.loads(self.req(port, "GET", "/api/status")[1])["signed_in"])
        self.post(port)
        self.assertTrue(json.loads(self.req(port, "GET", "/api/status")[1])["signed_in"])

    def test_mask_username(self):
        self.assertEqual(server.mask_username("sub.account@example.com"), "s***@e***.com")
        self.assertEqual(server.mask_username("+923001234567"), "+9***67")
        self.assertEqual(server.mask_username("ab"), "***")
        self.assertEqual(server.mask_username("a@b"), "a***@***")

    def test_account_needs_the_token_when_exposed(self):
        tok = "t" * 32
        port = self.start(tok)
        self.assertEqual(self.req(port, "GET", "/api/account")[0], 401)
        self.assertEqual(self.req(port, "GET", "/api/account", headers={"Cookie": f"byd_token={tok}"})[0], 200)

    def test_an_unreadable_stored_sign_in_is_reported_not_crashed_on(self):
        port = self.start()
        self.post(port)
        credstore.path().write_bytes(credstore.path().read_bytes()[:-5])
        d = json.loads(self.req(port, "GET", "/api/account")[1])
        self.assertFalse(d["signed_in"])
        self.assertFalse(d["store"]["readable"])
        self.assertTrue(d["store"]["reason"])

    def test_poller_state_is_passed_on(self):
        port = self.start()
        pollstatus.write("auth-failed", "Login failed: code=3008")
        d = json.loads(self.req(port, "GET", "/api/account")[1])
        self.assertEqual(d["poller"]["state"], "auth-failed")


class Credentials(Base):
    def test_there_is_no_plain_text_credentials_file_any_more(self):
        self.assertFalse(hasattr(byd_config, "SECRETS_FILE"))
        self.assertIsNone(byd_config.load_credentials())          # nothing stored: not signed in, whatever files lie around
        credstore.save({"username": "new@example.com", "password": "newpw1234", "base_url": "https://dilinkappoversea-sg.byd.auto",
                        "country_code": "PK", "language": "en", "time_zone": "UTC"})
        self.assertEqual(byd_config.load_credentials()["username"], "new@example.com")

    def test_config_from_refuses_a_non_byd_host_before_building_anything(self):
        fake = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"pybyd": fake}):
            with self.assertRaises(ValueError):
                byd_config.config_from({"username": "a", "password": "b", "base_url": "https://evil.example.com",
                                        "country_code": "PK", "language": "en", "time_zone": "UTC"})
        fake.BydConfig.assert_not_called()

    def test_config_from_never_passes_a_control_pin(self):
        fake = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"pybyd": fake}):
            byd_config.config_from({"username": "a", "password": "b", "base_url": "https://dilinkappoversea-sg.byd.auto",
                                    "country_code": "PK", "language": "en", "time_zone": "UTC"})
        self.assertNotIn("control_pin", fake.BydConfig.call_args.kwargs)

    def test_the_version_changes_when_a_sign_in_is_saved_or_removed(self):
        v0 = byd_config.credentials_version()
        credstore.save({"username": "a@b.c", "password": "pw123456", "base_url": "https://dilinkappoversea-sg.byd.auto",
                        "country_code": "PK", "language": "en", "time_zone": "UTC"})
        v1 = byd_config.credentials_version()
        self.assertNotEqual(v0, v1)
        credstore.delete()
        self.assertNotEqual(byd_config.credentials_version(), v1)


class Page(Base):
    """The sign-in page itself: the warning is visible, the consent is required, the password is handled carefully."""

    def get(self, port, path):
        st, b = self.req(port, "GET", path)
        self.assertEqual(st, 200, path)
        return b.decode()

    def test_the_account_script_is_served_and_safe(self):
        port = self.start()
        js = self.get(port, "/account.js")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "document.write", "localStorage", "sessionStorage",
                       "console.log", "likely"):
            self.assertNotIn(banned, js, banned)                              # a password must never be stored or logged by the page
        self.assertIn("/account.js", self.get(port, "/"))
        self.assertIn('id="view-account"', self.get(port, "/"))
        self.assertIn('href="#/account"', self.get(port, "/"))

    def test_the_warning_is_text_on_the_page_not_just_a_tooltip(self):
        js = self.get(self.start(), "/account.js")
        self.assertIn("Never use your primary BYD login here.", js)
        self.assertIn("dedicated sub-account", js)
        self.assertIn('"warnbox"', js)                                        # a boxed, always-visible element
        self.assertIn("Your password goes to BYD and nowhere else", js)

    def test_the_sub_account_confirmation_is_required_before_the_button_enables(self):
        js = self.get(self.start(), "/account.js")
        self.assertIn('ack.type = "checkbox"', js)
        self.assertIn("!ack.checked", js)                                     # part of the disabled condition
        self.assertIn("acknowledged: ack.checked", js)                        # and sent, so the server enforces it too

    def test_the_form_sends_the_header_the_server_requires_and_never_keeps_the_password(self):
        js = self.get(self.start(), "/account.js")
        self.assertIn('"X-BYD-App": "1"', js)
        self.assertIn('pw.type = "password"', js)
        self.assertIn('pw.autocomplete = "current-password"', js)
        self.assertIn('pw.value = ""', js)                                    # cleared as soon as it is submitted
        self.assertIn('body.password = ""', js)

    def test_a_remote_visitor_is_told_how_to_sign_in_safely(self):
        js = self.get(self.start(), "/account.js")
        self.assertIn("ssh -L 8765:localhost:8765", js)
        self.assertIn("does not take a BYD password over the network", js)
        self.assertIn("remoteWhy", js)
        self.assertIn("run your own copy", js)                       # a guest on a shared server is told what to do
        self.assertNotIn("the computer that runs the app, so", js)    # no single-self-hoster assumption
        self.assertIn("!a.local", js)

    def test_the_now_page_points_to_sign_in_when_nothing_is_signed_in(self):
        js = self.get(self.start(), "/app.js")
        self.assertIn("signInBanner", js)
        self.assertIn("#/account", js)
        self.assertIn("poller_state", js)

    def test_status_reports_the_poller_state_for_that_banner(self):
        port = self.start()
        pollstatus.write("auth-failed", "code=3008")
        d = json.loads(self.req(port, "GET", "/api/status")[1])
        self.assertEqual(d["poller_state"], "auth-failed")
        self.assertFalse(d["signed_in"])


if __name__ == "__main__":
    unittest.main()


class RemoteReason(Base):
    def test_account_says_why_a_request_is_not_local(self):
        tok = "t" * 32
        port = self.start(tok)                       # remote visitors only exist in token mode (otherwise a non-local Host is refused)
        ck = {"Cookie": f"byd_token={tok}"}
        d = json.loads(self.req(port, "GET", "/api/account", None, ck)[1])
        self.assertEqual((d["local"], d["remote_reason"]), (True, None))
        d = json.loads(self.req(port, "GET", "/api/account", None, dict(ck, Host="abc.ngrok-free.app"))[1])
        self.assertEqual((d["local"], d["remote_reason"], d["host"]), (False, "host", "abc.ngrok-free.app"))
        d = json.loads(self.req(port, "GET", "/api/account", None, dict(ck, **{"X-Forwarded-For": "1.2.3.4"}))[1])
        self.assertEqual((d["local"], d["remote_reason"]), (False, "proxy"))
