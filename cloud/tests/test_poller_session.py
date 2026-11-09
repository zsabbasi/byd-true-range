"""The poller's session logic: wait for a sign-in, never retry a rejected login, notice a changed sign-in, no secrets in output."""
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import byd_config  # noqa: E402
import db  # noqa: E402
import pollstatus  # noqa: E402
import poller  # noqa: E402
import test_poller_db as tpd  # noqa: E402

PASSWORD = "Sup3r-S3cret-pa$$word!"
CREDS = {"username": "sub.account@example.com", "password": PASSWORD, "base_url": "https://dilinkappoversea-sg.byd.auto",
         "country_code": "PK", "language": "en", "time_zone": "Asia/Karachi", "source": "app"}


# exception classes named like pyBYD's, so poller._auth_rejected sees the same MRO names
class BydAuthenticationError(Exception):
    pass


class BydSessionExpiredError(BydAuthenticationError):
    pass


class BydTransportError(Exception):
    pass


class Session(tpd.FakeClient):
    """A fake BydClient usable as an async context manager."""
    logins = 0

    def __init__(self, behaviour="ok", polls_before=None):
        super().__init__(tpd._rt(timestamp="2026-11-09 04:00:00+00:00"), NS(ev_consumption=None, avg_ev_consumption=None, air_distribution=None))
        self.behaviour = behaviour
        self.polls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def login(self):
        type(self).logins += 1
        if self.behaviour == "reject":
            raise BydAuthenticationError("Login failed: code=3008 message=Wrong email or password")
        if self.behaviour == "network":
            raise BydTransportError("getaddrinfo failed")

    async def get_vehicles(self):
        return [] if self.behaviour == "novehicle" else [NS(vin="LGXXXXXXXXXXXX0000")]

    async def get_vehicle_realtime(self, vin, **kw):
        self.polls += 1
        if self.behaviour == "reject-midway" and self.polls >= 2:
            raise BydAuthenticationError("Login failed: code=3008")
        if self.behaviour == "expired-midway" and self.polls == 2:
            raise BydSessionExpiredError("session expired")
        r = tpd._rt(timestamp=f"2026-11-09 04:{self.polls:02d}:00+00:00", total_mileage=19000.0 + self.polls)
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "h.sqlite"
        self.conn = db.connect()
        Session.logins = 0
        self.version = [("v1",)]
        self.patches = [
            mock.patch.object(byd_config, "credentials_version", lambda: self.version[0]),
            mock.patch.object(byd_config, "config_from", lambda creds, **kw: NS(creds=creds)),
            mock.patch.object(poller, "CRED_POLL_S", 0.02),
            mock.patch.object(poller, "SLEEP_TICK", 0.02),
            mock.patch.object(poller, "RETRY_S", 0.05),
            # the real intervals are 30 s to 15 min: shrink them so a test can see several polls
            mock.patch.object(poller, "SLOW_INTERVAL", 0.05),
            mock.patch.object(poller, "FAST_INTERVAL", 0.05),
            mock.patch.object(poller, "VIEWER_PARKED_INTERVAL", 0.05),
            mock.patch.object(poller, "CHARGE_INTERVAL", 0.05),
        ]
        for p in self.patches:
            p.start()
        poller._last_energy_fetch = float("-inf")
        poller._last_active = float("-inf")
        poller._last_charging = float("-inf")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.conn.close()
        self.tmp.cleanup()

    def run_it(self, coro, timeout=3.0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = asyncio.run(asyncio.wait_for(coro, timeout))
        return result, out.getvalue()


class RejectedLogin(Base):
    def test_a_rejected_login_is_attempted_once_and_then_waits_for_a_new_sign_in(self):
        async def scenario():
            task = asyncio.ensure_future(poller.run_session(CREDS, self.version[0], self.conn, lambda c: Session("reject")))
            await asyncio.sleep(0.4)                                       # 20x the credential poll interval
            attempts_while_waiting = Session.logins
            still_waiting = not task.done()
            self.version[0] = ("v2",)                                       # the user signs in again
            return attempts_while_waiting, still_waiting, await asyncio.wait_for(task, 2.0)

        (attempts, waiting, result), out = self.run_it(scenario())
        self.assertEqual(attempts, 1)                                      # NOT retried while the sign-in is unchanged
        self.assertTrue(waiting)
        self.assertEqual(result, "auth-failed")
        self.assertEqual(pollstatus.read()["state"], "auth-failed")
        self.assertIn("3008", pollstatus.read()["detail"])

    def test_a_rejection_in_the_middle_of_a_session_also_stops_retrying(self):
        async def scenario():
            task = asyncio.ensure_future(poller.run_session(CREDS, self.version[0], self.conn, lambda c: Session("reject-midway")))
            await asyncio.sleep(0.6)
            waiting = not task.done()
            self.version[0] = ("v2",)
            return waiting, await asyncio.wait_for(task, 2.0)

        (waiting, result), _ = self.run_it(scenario())
        self.assertTrue(waiting)
        self.assertEqual(result, "auth-failed")

    def test_a_merely_expired_session_is_retried_not_treated_as_a_rejected_login(self):
        self.assertFalse(poller._auth_rejected(BydSessionExpiredError("expired")))
        self.assertTrue(poller._auth_rejected(BydAuthenticationError("bad")))
        self.assertFalse(poller._auth_rejected(BydTransportError("net")))
        self.assertFalse(poller._auth_rejected(ValueError("x")))


class OtherEnds(Base):
    def test_a_network_error_is_retried_and_is_not_an_auth_failure(self):
        result, out = self.run_it(poller.run_session(CREDS, self.version[0], self.conn, lambda c: Session("network")))
        self.assertEqual(result, "retry")
        self.assertEqual(pollstatus.read()["state"], "error")
        self.assertNotEqual(pollstatus.read()["state"], "auth-failed")

    def test_an_account_without_a_vehicle_waits_for_a_change(self):
        async def scenario():
            task = asyncio.ensure_future(poller.run_session(CREDS, self.version[0], self.conn, lambda c: Session("novehicle")))
            await asyncio.sleep(0.2)
            waiting = not task.done()
            self.version[0] = ("v2",)
            return waiting, await asyncio.wait_for(task, 2.0)

        (waiting, result), _ = self.run_it(scenario())
        self.assertTrue(waiting)
        self.assertEqual(result, "no-vehicle")
        self.assertEqual(pollstatus.read()["state"], "no-vehicle")

    def test_signing_out_or_in_ends_a_running_session_promptly(self):
        async def scenario():
            task = asyncio.ensure_future(poller.run_session(CREDS, self.version[0], self.conn, lambda c: Session("ok")))
            await asyncio.sleep(0.3)
            running = not task.done()
            self.version[0] = ("signed-out",)
            return running, await asyncio.wait_for(task, 1.5)

        (running, result), _ = self.run_it(scenario())
        self.assertTrue(running)
        self.assertEqual(result, "changed")

    def test_a_working_session_reports_ok_and_stores_readings(self):
        async def scenario():
            task = asyncio.ensure_future(poller.run_session(CREDS, self.version[0], self.conn, lambda c: Session("ok")))
            await asyncio.sleep(0.3)
            state = pollstatus.read()["state"]
            self.version[0] = ("v2",)
            await asyncio.wait_for(task, 1.5)
            return state

        state, _ = self.run_it(scenario())
        self.assertEqual(state, "ok")
        self.assertGreaterEqual(self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1)

    def test_a_host_that_is_not_byd_is_refused_before_any_login(self):
        def refuse(creds, **kw):
            raise ValueError("refusing to send a password to a host that is not a BYD regional server")
        with mock.patch.object(byd_config, "config_from", refuse):
            async def scenario():
                task = asyncio.ensure_future(poller.run_session(dict(CREDS, base_url="https://evil.example.com"), self.version[0],
                                                                self.conn, lambda c: Session("ok")))
                await asyncio.sleep(0.2)
                self.version[0] = ("v2",)
                return await asyncio.wait_for(task, 1.5)
            result, _ = self.run_it(scenario())
        self.assertEqual(result, "bad-config")
        self.assertEqual(Session.logins, 0)


class NoSecretsLeak(Base):
    def test_the_password_never_reaches_the_log_or_the_status_file(self):
        async def scenario():
            task = asyncio.ensure_future(poller.run_session(CREDS, self.version[0], self.conn, lambda c: Session("reject")))
            await asyncio.sleep(0.2)
            self.version[0] = ("v2",)
            await asyncio.wait_for(task, 1.5)

        _, out = self.run_it(scenario())
        status_text = pollstatus.path().read_text(encoding="utf-8")
        for leak in (PASSWORD, "Sup3r", "sub.account", "example.com"):
            self.assertNotIn(leak, out)
            self.assertNotIn(leak, status_text)


class WaitingForSignIn(Base):
    def test_main_waits_when_not_signed_in_then_starts_when_a_sign_in_appears(self):
        creds = {"v": None}
        with mock.patch.object(byd_config, "load_credentials", lambda: creds["v"]), \
             mock.patch.object(poller, "_real_client", lambda c: Session("ok")):
            async def scenario():
                task = asyncio.ensure_future(poller.main())
                await asyncio.sleep(0.3)
                waiting_state = pollstatus.read()["state"]
                done_early = task.done()                                   # it did NOT exit: that used to kill the whole app
                creds["v"] = CREDS
                self.version[0] = ("signed-in",)
                await asyncio.sleep(0.5)
                ok_state = pollstatus.read()["state"]
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return waiting_state, done_early, ok_state

            (waiting_state, done_early, ok_state), out = self.run_it(scenario(), 5.0)
        self.assertEqual(waiting_state, "waiting-for-signin")
        self.assertFalse(done_early)
        self.assertEqual(ok_state, "ok")
        self.assertIn("Not signed in", out)


class Status(Base):
    def test_status_file_round_trip_and_garbage(self):
        pollstatus.write("ok", "fine")
        self.assertEqual(pollstatus.read()["state"], "ok")
        pollstatus.path().write_text("not json")
        self.assertEqual(pollstatus.read()["state"], "unknown")
        pollstatus.path().write_text('{"state": "evil", "detail": "x"}')
        self.assertEqual(pollstatus.read()["state"], "unknown")                # unknown states are not trusted

    def test_a_long_detail_is_cut(self):
        pollstatus.write("error", "x" * 5000)
        self.assertLessEqual(len(pollstatus.read()["detail"]), 300)


if __name__ == "__main__":
    unittest.main()
