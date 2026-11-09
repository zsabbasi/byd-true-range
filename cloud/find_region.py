"""
Find which BYD regional backend your account lives on.

BYD runs one backend per region (dilinkappoversea-eu / -au / -sg / -sa ...  (-in is deliberately not tried by default)). An account only exists on
its own region's server, so the wrong one answers "code=3008 Wrong email or password" even with correct
credentials. This tries a SHORT list of hosts, ONE login attempt each, with a pause between, and stops at the
first success. It prints only the outcome (error class + BYD's code/message), never credentials.

Safety:
- Only hosts of the form https://dilinkappoversea-<2-4 letters>.byd.auto are allowed, so your password can
  only ever go to a BYD-owned host of the same kind as pyBYD's default. Anything else is refused.
- Uses the sign-in stored by the app. Nothing is written to disk.
- Don't loop this. Repeated failed logins can lock an account. Each host is tried once per run.

Usage:
    python find_region.py                 # tries sg, sa (country code PK)
    python find_region.py --hosts sg sa   # custom list
    python find_region.py --country PK --pause 60
    python find_region.py --dry-run       # show what would be tried, send nothing
"""
from __future__ import annotations

import argparse
import asyncio
import re

from byd_config import build_config

DEFAULT_HOSTS = ["sg", "sa"]
SUFFIX_RE = re.compile(r"^[a-z]{2,4}$")
URL_RE = re.compile(r"^https://dilinkappoversea-[a-z]{2,4}\.byd\.auto$")


def url_for(suffix: str) -> str:
    if not SUFFIX_RE.match(suffix):
        raise SystemExit(f"Refusing host suffix {suffix!r}: must be 2-4 lowercase letters.")
    url = f"https://dilinkappoversea-{suffix}.byd.auto"
    assert URL_RE.match(url)
    return url


async def try_host(url: str, country: str) -> tuple[bool, str]:
    from pybyd import BydClient

    config = build_config(mqtt_enabled=False, base_url=url, country_code=country)
    try:
        async with BydClient(config) as client:
            await client.login()
            vehicles = await client.get_vehicles()
            return True, f"login OK, {len(vehicles)} vehicle(s)"
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", "")
        msg = str(e)[:140]
        return False, f"{type(e).__name__} code={code} {msg}"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hosts", nargs="+", default=DEFAULT_HOSTS)
    ap.add_argument("--country", default="PK")
    ap.add_argument("--pause", type=int, default=45, help="seconds between attempts")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    urls = [url_for(h.lower()) for h in a.hosts]
    print(f"Country code: {a.country}. Will try {len(urls)} host(s), one attempt each:")
    for u in urls:
        print(f"  {u}")
    if a.dry_run:
        print("Dry run: nothing sent.")
        return

    for i, url in enumerate(urls):
        print(f"\n[{i + 1}/{len(urls)}] {url} ...")
        ok, info = await try_host(url, a.country)
        print(f"  {'SUCCESS' if ok else 'failed'}: {info}")
        if ok:
            print("\nChoose this region (and this country code) on the app's Account page, then sign in:")
            print(f"  region host: {url}")
            print(f"  country code: {a.country}")
            return
        if i < len(urls) - 1:
            print(f"  waiting {a.pause}s before the next host ...")
            await asyncio.sleep(a.pause)
    print("\nNo host accepted the login. See the notes in cloud/README.md (Troubleshooting).")


if __name__ == "__main__":
    asyncio.run(main())
