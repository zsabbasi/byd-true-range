"""
Start the poller (records BYD data) and the web server (the app) together. Ctrl+C stops both.

    python run.py                  # app at http://localhost:8765 (this PC only)
    python run.py --host 0.0.0.0   # also reachable from your phone; prints a URL with a secret token
    python run.py --multi-user     # a SHARED server: each person signs in with their own BYD account (see deploy/README.md)

Any extra arguments (--host, --port, --capacity, --usable-fraction) are passed to server.py.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def main_multi() -> int:
    """Shared server: the web server plus one poller per signed-in user, started and stopped by the supervisor."""
    import multirun

    sup = multirun.Supervisor()
    server = subprocess.Popen([sys.executable, str(HERE / "server.py"), *sys.argv[1:]], cwd=HERE)
    try:
        while True:
            code = server.poll()
            if code is not None:
                print(f"\nserver.py exited with code {code}; stopping.")
                return code or 1
            try:
                sup.tick()
            except Exception as e:  # noqa: BLE001 - one bad tick must not take every user's polling down
                print(f"supervisor: {type(e).__name__}: {e}")
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopping ...")
        return 0
    finally:
        sup.stop_all()
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


def main() -> int:
    if "--multi-user" in sys.argv:
        return main_multi()
    py = sys.executable
    procs = [
        subprocess.Popen([py, str(HERE / "poller.py")], cwd=HERE),
        subprocess.Popen([py, str(HERE / "server.py"), *sys.argv[1:]], cwd=HERE),
    ]
    try:
        while True:
            for p in procs:
                code = p.poll()
                if code is not None:
                    print(f"\n{Path(p.args[1]).name} exited with code {code}; stopping the other process.")
                    return code or 1
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping ...")
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
