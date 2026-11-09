"""Shared-server mode: every person signs in with their own BYD account and can only ever see their own car."""
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import credstore  # noqa: E402
import db  # noqa: E402
import multirun  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402
import users  # noqa: E402
from test_server import probe_row  # noqa: E402

PW_A, PW_B = "Alice-S3cret-pa$$word!", "Bob-S3cret-pa$$word!"
HDRS = {"Content-Type": "application/json", "X-BYD-App": "1"}
HTTPS = {"X-Forwarded-Proto": "https"}


def creds(name="alice@example.com", pw=PW_A, **kw):
    d = {"username": name, "password": pw, "region": "sg", "country": "PK", "time_zone": "Asia/Karachi",
         "acknowledged": True, "shared_risk_ack": True}
    d.update(kw)
    return d


class BydAuthenticationError(Exception):
    code = "3008"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._old = (db.DB_PATH, settings.SETTINGS_FILE, settings.LEGACY_FILE)
        db.DB_PATH = self.root / "data" / "history.sqlite"
        settings.SETTINGS_FILE = None
        settings.LEGACY_FILE = self.root / "nope.json"
        self.addCleanup(lambda: (setattr(db, "DB_PATH", self._old[0]), setattr(settings, "SETTINGS_FILE", self._old[1]),
                                 setattr(settings, "LEGACY_FILE", self._old[2])))
        key = self.root / "k.key"
        key.write_bytes(b"0123456789abcdef0123456789abcdef")
        os.chmod(key, 0o600)
        env = mock.patch.dict(os.environ, {"BYD_APP_KEY_FILE": str(key)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CREDENTIALS_DIRECTORY", None)
        sch = mock.patch.object(credstore, "default_scheme", lambda: "aes")
        sch.start()
        self.addCleanup(sch.stop)
        server._mu_attempts.clear()
        self.calls = []
        self.fail_with = None

        def fake_login(c, timeout=25.0):
            self.calls.append(dict(c))
            if self.fail_with:
                raise self.fail_with
            return [{"model": "ATTO3", "vin_tail": "0000"}]

        lg = mock.patch.object(server, "do_login", fake_login)
        lg.start()
        self.addCleanup(lg.stop)

    def start(self, **kw):
        kw.setdefault("max_users", 10)
        srv = server.make_server("127.0.0.1", 0, token=None, capacity_kwh=49.92, usable_fraction=1.0, multi_user=True, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def call(self, port, method, path, body=None, cookie=None, headers=None):
        h = dict(headers or {})
        if body is not None:
            h = dict(HDRS, **h)
        if cookie:
            h["Cookie"] = f"byd_session={cookie}"
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        raw = r.read()
        hd = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"raw": raw.decode(errors="replace")}
        return r.status, data, hd

    def login(self, port, **kw):
        st, d, hd = self.call(port, "POST", "/api/login", creds(**kw), headers=HTTPS)
        token = None
        if "set-cookie" in hd:
            token = hd["set-cookie"].split(";")[0].split("=", 1)[1]
        return st, d, hd, token

    def seed(self, name, region="sg", **row):
        """Give a user some history of their own, written straight into THEIR folder."""
        uid = users.user_id(name, region)
        p = users.user_dir(uid) / "history.sqlite"
        c = sqlite3.connect(p)
        c.executescript(db.SCHEMA)
        db._migrate(c)
        for i in range(3):
            db.insert_reading(c, probe_row(ts=f"2026-11-08T15:0{i}:00+00:00", odometer_km=19000.0 + i, **row))
        c.commit()
        c.close()
        return uid


class Anonymous(Base):
    def test_a_visitor_who_is_not_signed_in_sees_the_sign_in_and_no_data(self):
        port = self.start()
        st, d, _ = self.call(port, "GET", "/api/status")
        self.assertEqual((st, d["signed_in"], d["has_data"], d["multi_user"]), (200, False, False, True))
        for p in ("/api/trips", "/api/charging", "/api/battery", "/api/series", "/api/trips/1"):
            st, d, _ = self.call(port, "GET", p)
            self.assertEqual((st, d["error"]), (401, "login-required"), p)
        st, d, _ = self.call(port, "GET", "/api/account")
        self.assertEqual((st, d["signed_in"], d["multi_user"], d["invite_required"], d["full"]), (200, False, True, False, False))
        self.assertIsNone(d["user"])
        self.assertEqual(self.call(port, "GET", "/")[0], 200)

    def test_any_host_name_is_accepted_because_every_data_endpoint_needs_a_session(self):
        port = self.start()
        self.assertEqual(self.call(port, "GET", "/", headers={"Host": "friends.example.com"})[0], 200)

    def test_posts_need_a_session_except_signing_in(self):
        port = self.start()
        for p, b in (("/api/logout", {}), ("/api/settings", {"battery_kwh": 49.92})):
            self.assertEqual(self.call(port, "POST", p, b, headers=HTTPS)[0], 401, p)


class SignIn(Base):
    def test_requirements(self):
        port = self.start()
        # CSRF header
        st, d, _ = self.call(port, "POST", "/api/login", creds(), headers=dict(HTTPS, **{"X-BYD-App": "0"}))
        self.assertEqual(st, 403)
        # both confirmations
        self.assertEqual(self.login(port, shared_risk_ack=False)[0], 400)
        self.assertEqual(self.login(port, acknowledged=False)[0], 400)
        # a password never crosses plain http: not from another host name without https
        st, d, _ = self.call(port, "POST", "/api/login", creds(), headers={"Host": "friends.example.com"})
        self.assertEqual((st, d["error"]), (403, "insecure"))
        self.assertEqual(self.calls, [])                                     # BYD was never contacted
        self.assertEqual(users.count(), 0)

    def test_https_via_the_proxy_header_is_accepted(self):
        port = self.start()
        st, d, hd, tok = self.login(port)
        self.assertEqual(st, 200, d)
        self.assertTrue(tok)

    def test_plain_http_is_accepted_only_on_the_machine_itself_or_when_the_operator_says_so(self):
        port = self.start()
        st, d, _ = self.call(port, "POST", "/api/login", creds())             # loopback, localhost Host: development
        self.assertEqual(st, 200, d)
        port2 = self.start(assume_https=True)
        st, d, _ = self.call(port2, "POST", "/api/login", creds("b@example.com", PW_B), headers={"Host": "x.example.com"})
        self.assertEqual(st, 200, d)

    def test_a_good_login_gives_a_private_folder_an_encrypted_login_and_a_session(self):
        port = self.start()
        st, d, hd, tok = self.login(port)
        self.assertEqual(st, 200)
        self.assertEqual(d["vehicles"][0]["vin_tail"], "0000")
        sc = hd["set-cookie"]
        for want in ("byd_session=", "HttpOnly", "SameSite=Strict", "Max-Age=7776000", "Secure"):
            self.assertIn(want, sc)
        uid = users.user_id("alice@example.com", "sg")
        folder = users.user_dir(uid)
        blob = (folder / "credentials.bin").read_bytes()
        self.assertNotIn(PW_A.encode(), blob)
        self.assertNotIn(b"alice@example.com", blob)
        self.assertEqual(users.active_users(), [uid])
        # the password is in no response and in no file anywhere under the data folder
        self.assertNotIn(PW_A, json.dumps(d))
        for f in (self.root / "data").rglob("*"):
            if f.is_file():
                self.assertNotIn(PW_A.encode(), f.read_bytes(), str(f))
        # the registry stores only a hash of the session token
        self.assertNotIn(tok.encode(), (self.root / "data" / "users.sqlite").read_bytes())
        st, a, _ = self.call(port, "GET", "/api/account", cookie=tok)
        self.assertEqual((a["signed_in"], a["user"]), (True, "a***@e***.com"))

    def test_a_wrong_password_creates_nothing(self):
        port = self.start()
        self.fail_with = BydAuthenticationError("Login failed: code=3008 Wrong email or password")
        st, d, hd, tok = self.login(port)
        self.assertEqual((st, d["error"], d["code"]), (401, "rejected", "3008"))
        self.assertIsNone(tok)
        self.assertEqual((users.count(), list((self.root / "data").glob("users/*"))), (0, []))

    def test_the_same_person_signing_in_again_gets_the_same_folder(self):
        port = self.start()
        self.login(port)
        self.login(port, name="  alice@example.com ")                      # surrounding spaces do not make a second user
        self.assertEqual(users.count(), 1)
        self.assertNotEqual(users.user_id("Alice@example.com", "sg"), users.user_id("alice@example.com", "sg"))  # no guessing about case
        self.assertNotEqual(users.user_id("alice@example.com", "sg"), users.user_id("alice@example.com", "eu"))

    def test_user_limit_and_invite_code(self):
        port = self.start(max_users=2, invite_code="open-sesame")
        st, d, _, _ = self.login(port)
        self.assertEqual((st, d["error"]), (403, "invite"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.login(port, invite="wrong")[0], 403)
        self.assertEqual(self.login(port, invite="open-sesame")[0], 200)
        self.assertEqual(self.login(port, name="b@example.com", pw=PW_B, invite="open-sesame")[0], 200)
        st, d, _, _ = self.login(port, name="c@example.com", invite="open-sesame")
        self.assertEqual((st, d["error"]), (403, "full"))
        self.assertTrue(self.call(port, "GET", "/api/account")[1]["full"])
        # people already in are not locked out by the user limit (but, like everyone, need the invite code)
        self.assertEqual(self.login(port, invite="open-sesame")[0], 200)
        self.assertEqual(self.login(port, invite="")[0], 403)
        self.assertTrue(self.call(self.start(invite_code="x"), "GET", "/api/account")[1]["invite_required"])

    def test_wrong_logins_are_limited_per_account_and_one_account_never_blocks_another(self):
        port = self.start()
        self.fail_with = BydAuthenticationError("bad")
        codes = [self.login(port)[0] for _ in range(6)]
        self.assertEqual(codes, [401] * 5 + [429])
        self.assertEqual(len(self.calls), 5)                                  # the sixth never reached BYD
        self.fail_with = None
        self.assertEqual(self.login(port, name="bob@example.com", pw=PW_B)[0], 200)

    def test_a_server_with_a_dead_proxy_header_cannot_be_used_to_dodge_the_per_account_limit(self):
        port = self.start()
        self.fail_with = BydAuthenticationError("bad")
        for i in range(5):
            st, _, _ = self.call(port, "POST", "/api/login", creds(), headers=dict(HTTPS, **{"X-Forwarded-For": f"9.9.9.{i}"}))
        st, _, _ = self.call(port, "POST", "/api/login", creds(), headers=dict(HTTPS, **{"X-Forwarded-For": "9.9.9.99"}))
        self.assertEqual(st, 429)


class Isolation(Base):
    def test_two_people_see_only_their_own_car(self):
        port = self.start()
        ta = self.login(port, name="alice@example.com", pw=PW_A)[3]
        tb = self.login(port, name="bob@example.com", pw=PW_B)[3]
        self.assertNotEqual(ta, tb)
        self.seed("alice@example.com", soc=80.0, vin_tail="AAAA")
        self.seed("bob@example.com", soc=33.0, vin_tail="BBBB")
        sa = self.call(port, "GET", "/api/status", cookie=ta)[1]
        sb = self.call(port, "GET", "/api/status", cookie=tb)[1]
        self.assertEqual((sa["reading"]["soc"], sb["reading"]["soc"]), (80.0, 33.0))
        self.assertNotEqual(json.dumps(sa["reading"]), json.dumps(sb["reading"]))
        aa = self.call(port, "GET", "/api/account", cookie=ta)[1]
        ab = self.call(port, "GET", "/api/account", cookie=tb)[1]
        self.assertEqual((aa["user"], ab["user"]), ("a***@e***.com", "b***@e***.com"))
        # each person's own endpoints answer from their own history
        for p in ("/api/series", "/api/trips", "/api/charging", "/api/battery"):
            self.assertEqual(self.call(port, "GET", p, cookie=ta)[0], 200, p)

    def test_a_session_is_not_a_door_to_anyone_elses_data_even_with_ids_in_the_url(self):
        port = self.start()
        ta = self.login(port, name="alice@example.com", pw=PW_A)[3]
        self.login(port, name="bob@example.com", pw=PW_B)
        self.seed("bob@example.com", soc=33.0)
        uid_b = users.user_id("bob@example.com", "sg")
        for p in (f"/api/status?uid={uid_b}", f"/users/{uid_b}/history.sqlite", f"/../users/{uid_b}/credentials.bin",
                  "/api/trips/../../users"):
            st, d, _ = self.call(port, "GET", p, cookie=ta)
            self.assertNotIn(b"33", json.dumps(d).encode() if st == 200 and d.get("has_data") else b"", p)
            self.assertIn(st, (200, 404), p)
        self.assertFalse(self.call(port, "GET", f"/api/status?uid={uid_b}", cookie=ta)[1].get("has_data"))

    def test_each_person_has_their_own_battery_size(self):
        port = self.start()
        ta = self.login(port, name="alice@example.com", pw=PW_A)[3]
        tb = self.login(port, name="bob@example.com", pw=PW_B)[3]
        self.assertEqual(self.call(port, "GET", "/api/account", cookie=ta)[1]["battery"]["set"], False)
        self.assertEqual(self.call(port, "POST", "/api/settings", {"battery_kwh": 60.48}, cookie=ta, headers=HTTPS)[0], 200)
        ka = self.call(port, "GET", "/api/account", cookie=ta)[1]["battery"]
        kb = self.call(port, "GET", "/api/account", cookie=tb)[1]["battery"]
        self.assertEqual((ka["kwh"], ka["set"]), (60.48, True))
        self.assertEqual((kb["kwh"], kb["set"]), (49.92, False))               # Bob's is untouched
        self.assertEqual(self.call(port, "GET", "/api/status", cookie=tb)[1].get("battery_kwh", 49.92), 49.92)
        # and the host's own settings file (an old one next to the code) is never inherited by a user
        self.assertTrue((users.user_dir(users.user_id("alice@example.com", "sg")) / "settings.json").exists())

    def test_asking_to_watch_wakes_only_your_own_poller(self):
        port = self.start()
        ta = self.login(port, name="alice@example.com", pw=PW_A)[3]
        self.login(port, name="bob@example.com", pw=PW_B)
        server._last_touch_by_dir.clear()
        self.seed("alice@example.com")
        self.call(port, "GET", "/api/status", cookie=ta)
        fa = users.user_dir(users.user_id("alice@example.com", "sg")) / "viewer_active"
        fb = users.user_dir(users.user_id("bob@example.com", "sg")) / "viewer_active"
        self.assertTrue(fa.exists())
        self.assertFalse(fb.exists())

    def test_the_data_folder_follows_the_request_thread_and_nothing_else(self):
        seen = {}
        barrier = threading.Barrier(2)

        def work(name, folder):
            with db.use_dir(folder):
                barrier.wait()
                seen[name] = (db.current_dir(), db.current_db_path())
                barrier.wait()

        a, b = self.root / "a", self.root / "b"
        t1 = threading.Thread(target=work, args=("a", a))
        t2 = threading.Thread(target=work, args=("b", b))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(seen["a"], (a, a / "history.sqlite"))
        self.assertEqual(seen["b"], (b, b / "history.sqlite"))
        self.assertIsNone(db.dir_override())                                # nothing leaks out of the block


class SignOutAndForget(Base):
    def test_sign_out_ends_this_device_only(self):
        port = self.start()
        t1 = self.login(port)[3]
        t2 = self.login(port)[3]                                            # a second device
        st, d, hd = self.call(port, "POST", "/api/logout", {}, cookie=t1)
        self.assertEqual((st, d["forgotten"]), (200, False))
        self.assertIn("Max-Age=0", hd["set-cookie"])
        self.assertEqual(self.call(port, "GET", "/api/account", cookie=t1)[1]["signed_in"], False)
        self.assertEqual(self.call(port, "GET", "/api/account", cookie=t2)[1]["signed_in"], True)
        self.assertEqual(users.count(), 1)                                  # tracking continues

    def test_forget_me_removes_the_sessions_and_the_data(self):
        port = self.start()
        t1 = self.login(port)[3]
        t2 = self.login(port)[3]
        uid = users.user_id("alice@example.com", "sg")
        self.seed("alice@example.com")
        st, d, _ = self.call(port, "POST", "/api/logout", {"forget": True}, cookie=t1)
        self.assertEqual((st, d["forgotten"]), (200, True))
        self.assertEqual(users.state(uid), "removing")
        for t in (t1, t2):                                                  # every device is signed out at once
            self.assertEqual(self.call(port, "GET", "/api/status", cookie=t)[1]["signed_in"], False)
        self.assertEqual(self.call(port, "POST", "/api/login", creds(), headers=HTTPS)[0], 409)   # still being removed
        self.assertEqual(users.count(), 0)                                  # the place is free again
        # the supervisor wipes the folder once it has stopped their poller
        sup = multirun.Supervisor(spawn=lambda u: FakeProc())
        sup.tick()
        self.assertFalse(users.user_dir(uid).exists())
        self.assertEqual((users.state(uid), users.pending_wipes()), ("none", []))
        self.assertEqual(self.login(port)[0], 200)                          # and they can sign up again from scratch

    def test_expired_and_unknown_sessions_are_refused(self):
        port = self.start()
        tok = self.login(port)[3]
        self.assertEqual(self.call(port, "GET", "/api/account", cookie="nonsense")[1]["signed_in"], False)
        with users._db() as c:
            c.execute("UPDATE sessions SET expires = 1")
        self.assertEqual(self.call(port, "GET", "/api/account", cookie=tok)[1]["signed_in"], False)


class FakeProc:
    def __init__(self, alive=True):
        self.alive = alive
        self.stopped = False

    def poll(self):
        return None if self.alive and not self.stopped else 1

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.stopped = True


class Supervisor(Base):
    def test_one_poller_per_user_in_their_own_folder_started_and_stopped_as_people_come_and_go(self):
        spawned = []
        clock = [0.0]
        sup = multirun.Supervisor(spawn=lambda u: spawned.append(u) or FakeProc(), clock=lambda: clock[0])
        sup.tick()
        self.assertEqual(spawned, [])
        a, b = users.user_id("a@example.com", "sg"), users.user_id("b@example.com", "sg")
        users.add(a); users.add(b)
        sup.tick(); sup.tick()
        self.assertEqual(sorted(spawned), sorted([a, b]))                   # exactly one each, not restarted every tick
        pa = sup.children[a][0]
        users.request_delete(a)
        sup.tick()
        self.assertTrue(pa.stopped)
        self.assertEqual(list(sup.children), [b])

    def test_a_poller_that_keeps_dying_is_not_restarted_in_a_tight_loop(self):
        n = []
        clock = [0.0]
        sup = multirun.Supervisor(spawn=lambda u: n.append(u) or FakeProc(alive=False), clock=lambda: clock[0])
        users.add(users.user_id("a@example.com", "sg"))
        sup.tick()
        self.assertEqual(len(n), 1)
        for _ in range(5):
            clock[0] += 5
            sup.tick()
        self.assertEqual(len(n), 1)                                         # backing off
        clock[0] += multirun.BACKOFF_S
        sup.tick()
        self.assertEqual(len(n), 2)

    def test_the_real_spawner_gives_each_poller_only_its_own_folder(self):
        uid = users.user_id("a@example.com", "sg")
        users.add(uid)
        with mock.patch.object(multirun.subprocess, "Popen") as popen:
            multirun.spawn_poller(uid)
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["BYD_DATA_DIR"], str(users.user_dir(uid)))


class Registry(Base):
    def test_ids_are_stable_keyed_and_safe_as_folder_names(self):
        a = users.user_id("alice@example.com", "sg")
        self.assertEqual(a, users.user_id("  alice@example.com ", "sg"))
        self.assertRegex(a, r"^[0-9a-f]{20}$")
        self.assertNotIn("alice", a)
        for bad in ("../x", "", "A" * 20, "g" * 20, "a" * 19):
            with self.assertRaises(ValueError):
                users.user_dir(bad)
        if os.name == "posix":
            self.assertEqual(oct((self.root / "data" / "users_salt").stat().st_mode & 0o777), "0o600")

    def test_single_owner_mode_cannot_be_mixed_with_an_access_token(self):
        with self.assertRaises(ValueError):
            server.make_server("127.0.0.1", 0, token="t" * 32, capacity_kwh=49.92, usable_fraction=1.0, multi_user=True)


class ReviewFindings(Base):
    """Found by an independent review of this mode; each one is a real hole that was closed."""

    def test_the_invite_code_leaks_nothing_about_who_has_an_account(self):
        port = self.start(invite_code="open-sesame")
        self.login(port, invite="open-sesame")                              # alice is a member
        self.calls.clear()
        self.fail_with = BydAuthenticationError("bad")
        member = self.login(port, invite="wrong")[:2]
        stranger = self.login(port, name="nobody@example.com", invite="wrong")[:2]
        self.assertEqual(member[0], stranger[0])
        self.assertEqual(member[1]["error"], stranger[1]["error"])          # the same reply for a member and a stranger
        self.assertEqual(self.calls, [])                                    # and BYD was never contacted for either

    def test_a_stranger_cannot_use_up_a_members_sign_in_budget(self):
        port = self.start(invite_code="open-sesame")
        self.login(port, invite="open-sesame")
        for i in range(30):                                                 # junk aimed at alice's account name, from 30 addresses
            self.call(port, "POST", "/api/login", creds(invite="wrong"), headers=dict(HTTPS, **{"X-Forwarded-For": f"7.7.7.{i}"}))
        st, _, _ = self.call(port, "POST", "/api/login", creds(invite="open-sesame"),
                             headers=dict(HTTPS, **{"X-Forwarded-For": "8.8.8.8"}))
        self.assertEqual(st, 200)                                           # alice's own budget was never touched

    def test_successful_logins_never_use_up_an_accounts_wrong_password_budget(self):
        port = self.start()
        for _ in range(8):
            self.assertEqual(self.login(port)[0], 200)

    def test_only_logins_that_reached_byd_and_failed_count_against_the_account(self):
        port = self.start()
        for _ in range(10):
            self.login(port, shared_risk_ack=False)                         # refused before BYD: free
            self.login(port, acknowledged=False)
        self.fail_with = BydAuthenticationError("bad")
        self.assertEqual([self.login(port)[0] for _ in range(5)], [401] * 5)

    def test_the_invite_code_cannot_be_guessed_by_one_address(self):
        port = self.start(invite_code="open-sesame")
        codes = [self.login(port, invite=f"guess{i}")[0] for i in range(25)]
        self.assertEqual(codes[:20], [403] * 20)
        self.assertEqual(set(codes[20:]), {429})                            # then that address is blocked, even for the right code
        self.assertEqual(self.login(port, invite="open-sesame")[0], 429)

    def test_a_non_ascii_invite_code_or_token_does_not_crash_the_handler(self):
        port = self.start(invite_code="open-sesame")
        st, d, _, _ = self.login(port, invite="é€")
        self.assertEqual((st, d["error"]), (403, "invite"))
        port2 = self.start(invite_code="clé-€")                             # the operator's own code may be non-ASCII too
        self.assertEqual(self.login(port2, invite="clé-€")[0], 200)
        self.assertTrue(server.safe_eq("é", "é") and not server.safe_eq("é", "e"))

    def test_the_throttle_table_forgets_the_oldest_entries_not_everybodys(self):
        for i in range(20050):
            server.multi_note(f"u{i:06d}", f"10.0.{i % 250}.{i % 200}", ("u",))
        self.assertLessEqual(len(server._mu_attempts), 20001)
        self.assertIn(server._mu_key("u", "u020049", "x"), server._mu_attempts)

    def test_a_wipe_that_failed_leaves_the_person_marked_removing_so_it_is_retried(self):
        uid = users.user_id("a@example.com", "sg")
        users.add(uid)
        users.request_delete(uid)
        with mock.patch.object(users.shutil, "rmtree", lambda *a, **k: None):     # a locked file: the folder survives
            self.assertFalse(users.purge(uid))
        self.assertEqual((users.state(uid), users.pending_wipes()), ("removing", [uid]))
        self.assertTrue(users.purge(uid))                                          # next try succeeds
        self.assertEqual(users.state(uid), "none")


if __name__ == "__main__":
    unittest.main()
