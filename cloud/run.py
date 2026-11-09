"""
Start the poller (records BYD data) and the web server (the app) together. Ctrl+C stops both.

    python run.py                  # app at http://localhost:8765 (this PC only)
    python run.py --host 0.0.0.0   # also reachable from your phone; prints a URL with a secret token

Any extra arguments (--host, --port, --capacity, --usable-fraction) are passed to server.py.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def main() -> int:
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
