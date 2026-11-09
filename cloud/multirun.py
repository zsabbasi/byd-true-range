"""
Shared-server mode, process side: one poller per user, each in its own data folder (BYD_DATA_DIR=<data>/users/<id>), started when a
user signs in and stopped, with their folder wiped, when they delete their data. Separate processes are the isolation: a poller
only ever sees its own user's folder, sign-in and history.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import users

HERE = Path(__file__).parent
MIN_UPTIME_S = 60          # a poller that dies sooner than this is restarted only after BACKOFF_S
BACKOFF_S = 30


def spawn_poller(uid: str):
    env = dict(os.environ, BYD_DATA_DIR=str(users.user_dir(uid)))
    return subprocess.Popen([sys.executable, str(HERE / "poller.py")], cwd=HERE, env=env)


def stop_process(p, timeout: float = 10.0) -> None:
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=timeout)


class Supervisor:
    def __init__(self, spawn=spawn_poller, clock=time.monotonic):
        self.spawn = spawn
        self.clock = clock
        self.children: dict[str, tuple] = {}        # uid -> (process, started)
        self.next_try: dict[str, float] = {}

    def tick(self) -> None:
        now = self.clock()
        want = users.active_users()
        for uid in want:
            ch = self.children.get(uid)
            if ch is not None and ch[0].poll() is None:
                continue
            if ch is not None:                       # it exited by itself
                self.children.pop(uid)
                if now - ch[1] < MIN_UPTIME_S:
                    self.next_try[uid] = now + BACKOFF_S
            if now >= self.next_try.get(uid, 0.0):
                try:
                    self.children[uid] = (self.spawn(uid), now)
                except OSError as e:
                    print(f"could not start a poller ({type(e).__name__}); will retry")
                    self.next_try[uid] = now + BACKOFF_S
        for uid in list(self.children):
            if uid not in want:
                stop_process(self.children.pop(uid)[0])
        for uid in users.pending_wipes():            # their poller is stopped above (or never ran): now it is safe to wipe
            ch = self.children.pop(uid, None)
            if ch:
                stop_process(ch[0])
            if users.purge(uid):
                self.next_try.pop(uid, None)
            else:
                print("could not wipe a deleted person's folder yet; will retry")

    def stop_all(self) -> None:
        for uid in list(self.children):
            stop_process(self.children.pop(uid)[0])
