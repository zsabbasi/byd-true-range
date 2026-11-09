"""
BYD sign-in for the poller. The credentials come from ONE place: the encrypted store written by the app's sign-in page
(credstore.py). There is no plain-text file.

They stay on this machine and are only ever sent to the BYD host, over HTTPS, by pyBYD.

Security choices:
- We read our OWN store, not the process environment, so a stray BYD_BASE_URL in the shell cannot silently redirect traffic.
  base_url is passed explicitly, and a base_url that is not a BYD regional host is refused (see base_url_for / is_byd_host).
- No value is printed or logged by this module.
"""
from __future__ import annotations

import re

import credstore


# BYD runs one backend per region; an account exists only on its own region's server (a wrong region answers
# "code=3008 Wrong email or password" even with correct credentials). DNS records exist for these hosts; which COUNTRIES use
# which is only confirmed where listed in CONFIRMED.
REGIONS = {
    "eu": "Europe",
    "au": "Australia",
    "sg": "Singapore / Asia-Pacific",
    "sa": "Saudi Arabia / Middle East",
    "in": "India",
    "id": "Indonesia",
    "jp": "Japan",
    "kr": "Korea",
    "br": "Brazil",
    "mx": "Mexico",
    "tr": "Turkey",
    "vn": "Vietnam",
}
CONFIRMED = {"PK": "sg"}                       # country -> region, confirmed by a real account
_HOST_RE = re.compile(r"^https://dilinkappoversea-[a-z]{2,4}\.byd\.auto$")


def base_url_for(region: str) -> str:
    if region not in REGIONS:
        raise ValueError("unknown region")
    return f"https://dilinkappoversea-{region}.byd.auto"


def is_byd_host(url: str) -> bool:
    """Only BYD's own regional hosts may ever receive a password."""
    return bool(_HOST_RE.match(url or ""))


def region_of(base_url: str) -> str | None:
    m = re.match(r"^https://dilinkappoversea-([a-z]{2,4})\.byd\.auto$", base_url or "")
    return m.group(1) if m else None


def load_credentials() -> dict | None:
    """The stored sign-in, or None if not signed in."""
    c = credstore.load()
    return {**c, "source": "app"} if c else None


def credentials_present() -> bool:
    return load_credentials() is not None


def credentials_version() -> tuple:
    """Changes when a sign-in is saved, replaced or removed (the poller watches this)."""
    return (credstore.version(),)


def config_from(creds: dict, *, mqtt_enabled: bool = False):
    """A pyBYD BydConfig from a credentials dict. Refuses any host that is not a BYD regional host."""
    from pybyd import BydConfig  # imported lazily so this module is importable without pybyd installed

    if not is_byd_host(creds.get("base_url", "")):
        raise ValueError("refusing to send a password to a host that is not a BYD regional server")
    # Deliberately no control PIN: this project is read-only (docs/PROJECT_PLAN.md section 6a).
    return BydConfig(
        username=creds["username"], password=creds["password"], base_url=creds["base_url"],
        country_code=creds["country_code"], language=creds["language"], time_zone=creds["time_zone"],
        mqtt_enabled=mqtt_enabled,
    )


def build_config(*, mqtt_enabled: bool = False, base_url: str | None = None, country_code: str | None = None):
    """A BydConfig from the stored sign-in. base_url / country_code override (used by find_region.py)."""
    creds = load_credentials()
    if creds is None:
        raise SystemExit("Not signed in. Open the app on this computer and sign in to BYD.")
    creds = dict(creds)
    if base_url:
        creds["base_url"] = base_url
    if country_code:
        creds["country_code"] = country_code
    return config_from(creds, mqtt_enabled=mqtt_enabled)
