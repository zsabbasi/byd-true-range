"""
Adaptive BYD cloud poller. Records a history row each poll (the history BYD doesn't keep),
and prints the live true-range estimate.

Cadence (adaptive, to protect the car's 12V battery):
  - driving (speed > 0)      -> poll every ~30 s
  - parked, recently active  -> poll every ~hour  (gives the "GPS every hour" history)
  - long idle                -> stays hourly

Run:  python poller.py            (uses the sign-in stored by the app)
Stop: Ctrl+C.  Data goes to data/history.sqlite.

This is the Phase-1 reference + data capture tool. The phone app will reuse this exact logic.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

import db
import byd_config
import capacity
import pollstatus
import range_engine
import settings

FAST_INTERVAL = 30       # seconds, while driving
SLOW_INTERVAL = 900      # seconds, while parked and nobody is watching (a missed trip start costs little:
                         # the last parked reading is a valid anchor for the next one)
VIEWER_PARKED_INTERVAL = 120  # seconds, while parked but someone has the app open
VIEWER_MIN_GAP = 30      # seconds: refresh at once when the app is opened, unless polled this recently
VIEWER_WINDOW = 90       # the app counts as open if it asked for status within this many seconds
ENERGY_MIN_INTERVAL = 300  # seconds; energy endpoint is an extra request, so rate-limit it

ACTIVE_GRACE = 600       # seconds of fast polling after the last driving signal
CHARGE_INTERVAL = 300    # seconds while charging: ticks are then resolved to a few minutes (power needs this)
CHARGE_GRACE = 1200      # keep that cadence through a plateau so the final battery tick is seen

_last_energy_fetch = float("-inf")   # NOT 0.0: on Linux monotonic() counts from boot, so 0.0 would mean "fetched just now" for the first 5 minutes of uptime
_last_active = float("-inf")
_last_charging = float("-inf")


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _rate(v):
    """BYD sends consumption as a string, with '--' when unavailable. Returns float or None."""
    if isinstance(v, str):
        v = v.strip()
        if v in ("", "--", "-", "N/A"):
            return None
    n = _num(v)
    return n if n is not None and n > 0 else None


def viewer_active(now: float | None = None) -> bool:
    """True if the web app asked for status recently. server.py touches data/viewer_active on each request."""
    try:
        mtime = (db.DB_PATH.parent / "viewer_active").stat().st_mtime
    except OSError:
        return False
    return (now if now is not None else time.time()) - mtime <= VIEWER_WINDOW


SLEEP_TICK = 5.0         # how often a sleeping poller looks for the app being opened or the sign-in changing


async def sleep_interruptible(interval: float, started: float, *, min_gap: float = VIEWER_MIN_GAP,
                              tick: float | None = None, stop=None) -> str:
    """
    Sleep until `started + interval`, but wake early (and return "viewer") as soon as the app is open and the
    last poll is at least `min_gap` old. This is why opening the app in the car refreshes within seconds even
    though the poller was idling. Returns "timeout" or "viewer".
    """
    tick = SLEEP_TICK if tick is None else tick
    deadline = started + interval
    while True:
        now = time.monotonic()
        if now >= deadline:
            return "timeout"
        if stop is not None and stop():
            return "stop"                                 # e.g. the sign-in changed: do not sleep through it
        if now >= started + min_gap and viewer_active():
            return "viewer"
        await asyncio.sleep(max(0.01, min(tick, deadline - now)))


def _name(v) -> str:
    """Enum member name ('ON'), not str(): pyBYD enums are IntEnum, so str() gives '2'."""
    if v is None:
        return ""
    return str(getattr(v, "name", None) or v)


def _is_driving(rt) -> bool:
    """
    Driving = moving, or the car's power gear is ON (so a stop at a red light still counts).
    We deliberately ignore vehicle_state: pyBYD marks VehicleState.ON as "not confirmed", and the probe
    showed it reading ON while parked and plugged in. power_gear ON is read from real data on first drives
    (the history table logs gear + vehicle_state so this can be checked).
    """
    spd = _num(getattr(rt, "speed", None))
    if spd is not None and spd > 0:
        return True
    return _name(getattr(rt, "power_gear", None)).upper() == "ON"


def _charge_name(rt) -> str:
    """First informative charging state name. 'CONNECTED' (15) is NOT charging: pyBYD notes it is unreliable."""
    for attr in ("charge_state", "charging_state"):
        n = _name(getattr(rt, attr, None)).upper()
        if n and n != "UNKNOWN":
            return n
    return ""


def _last_car_ts(conn) -> str | None:
    row = conn.execute("SELECT car_ts FROM readings ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


async def poll_once(client, vin: str, conn) -> int:
    """Do one poll, store a row, print range. Returns the next interval in seconds."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    # The car can take 20+ s to answer a wake request; pyBYD default wait is 15 s and it then hands back the
    # previous snapshot. Wait up to ~30 s so a slow wake is caught on THIS poll, not the next one.
    rt = await client.get_vehicle_realtime(vin, poll_attempts=20, poll_interval=1.5)

    # BYD sometimes answers with an EMPTY snapshot (SOC 0, odometer 0, no timestamp), typically for a sleeping car.
    # It is not a reading. Storing it would show "battery 0%" and invent a 19000 km trip.
    soc_raw, odo_raw = _num(getattr(rt, "elec_percent", None)), _num(getattr(rt, "total_mileage", None))
    if soc_raw is None or odo_raw is None or soc_raw <= 0 or odo_raw <= 0:
        print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] BYD returned an empty snapshot (car asleep or offline); "
              f"nothing stored")
        return VIEWER_PARKED_INTERVAL if viewer_active() else SLOW_INTERVAL

    # Same car timestamp as the last stored row = BYD returned the old snapshot, the car did not report.
    car_ts_now = str(getattr(rt, "timestamp", "") or "")
    if car_ts_now and car_ts_now == _last_car_ts(conn):
        print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] car has not reported since {car_ts_now} "
              f"(BYD online_state={_name(getattr(rt, 'online_state', None)) or '?'}, "
              f"connect_state={_name(getattr(rt, 'connect_state', None)) or '?'}); nothing new stored")
        return VIEWER_PARKED_INTERVAL if viewer_active() else SLOW_INTERVAL

    lat = lon = None
    try:
        gps = await client.get_gps_info(vin)
        lat = _num(getattr(gps, "latitude", None))
        lon = _num(getattr(gps, "longitude", None))
    except Exception as e:  # noqa: BLE001
        print(f"  (gps unavailable: {type(e).__name__})")

    # BYD's own last-50km energy: independent of SOC, so it is the key input for SOH (see PROJECT_PLAN 6c).
    r50_rate = _rate(getattr(rt, "recent_50km_energy_ev", None))
    if r50_rate is None:
        r50_rate = _rate(getattr(rt, "recent_50km_energy", None))
    r50_kwh = r50_air = None
    global _last_energy_fetch
    if time.monotonic() - _last_energy_fetch >= ENERGY_MIN_INTERVAL:
        _last_energy_fetch = time.monotonic()
        try:
            near = (await client.get_energy_consumption(vin)).nearest_energy_consumption
            if near is not None:
                r50_kwh = _rate(near.ev_consumption)
                r50_air = _num(near.air_distribution)
                if r50_rate is None:
                    r50_rate = _rate(near.avg_ev_consumption)
        except Exception as e:  # noqa: BLE001
            print(f"  (energy unavailable: {type(e).__name__})")

    driving = _is_driving(rt)
    charging_state = _charge_name(rt)

    row = {
        "ts": now,
        "car_ts": str(getattr(rt, "timestamp", "") or ""),
        "vin_tail": vin[-4:] if vin else None,
        "soc": _num(getattr(rt, "elec_percent", None)),
        "odometer_km": _num(getattr(rt, "total_mileage", None)),
        "byd_range_km": _num(getattr(rt, "endurance_mileage", None)),
        "speed_kmh": _num(getattr(rt, "speed", None)),
        "power_w": _num(getattr(rt, "gl", None)),
        "charging_state": charging_state,
        "recent_50km": str(getattr(rt, "recent_50km_energy", "") or ""),
        "r50_rate": r50_rate,
        "r50_kwh": r50_kwh,
        "r50_air_pct": r50_air,
        "lat": lat,
        "lon": lon,
        "is_driving": 1 if driving else 0,
        # logged raw so the driving/charging rules can be checked against real drives
        "gear": _name(getattr(rt, "power_gear", None)),
        "vehicle_state": _name(getattr(rt, "vehicle_state", None)),
    }
    prev = conn.execute("SELECT soc, odometer_km FROM readings WHERE soc > 0 AND odometer_km > 0 "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    db.insert_reading(conn, row)
    global _last_charging
    moving = (row["speed_kmh"] or 0) > 0           # speed, not gear: plugged in with the gear ON is still charging
    rising = bool(prev and row["soc"] and row["odometer_km"] and not moving
                  and row["soc"] > prev[0] and abs(row["odometer_km"] - prev[1]) <= 0.5)
    if charging_state == "CHARGING" or rising:          # BYD says so, or the battery rose while parked: physics
        _last_charging = time.monotonic()

    cfg = settings.load()
    cap = capacity.learn_from_db(conn, cfg["battery_kwh"], settings.usable_kwh(cfg))
    result = range_engine.best_estimate(db.recent_readings(conn), row["soc"], usable_kwh=cap["usable_kwh"],
                                        cap_uncertainty=cap["sd_rel"], best_kwh_per_100=db.best_r50(conn),
                                        byd_kwh_per_100=r50_rate, byd_range_km=row["byd_range_km"],
                                        odometer_km=row["odometer_km"], own_observed_km=db.observed_km(conn),
                                        mode=cfg["range_mode"])

    stamp = dt.datetime.now().strftime("%H:%M:%S")
    soc = row["soc"]
    bydr = row["byd_range_km"]
    charging_note = " | CHARGING" if charging_state == "CHARGING" else ""
    state = f"{'DRIVING' if driving else 'parked'}{charging_note}"
    if result.true_range_km is not None:
        why = (result.parts or {}).get("policy") or result.basis
        print(
            f"[{stamp}] SOC {soc}%  true-range {result.true_range_km:.0f} km "
            f"({result.low_km:.0f}-{result.high_km:.0f}, trust {result.trust_pct}%)  [{why}]"
        )
        ours = (f"ours {result.consumption_kwh_per_100km:.1f} kWh/100km" if result.consumption_kwh_per_100km is not None
                else "no consumption figure")
        print(
            f"           BYD says {bydr} km (not used), {r50_rate} kWh/100km (last 50 km) | {ours} "
            f"({result.basis}) | {state}"
        )
    else:
        print(f"[{stamp}] SOC {soc}%  true-range: no estimate possible yet")
        print(f"           BYD says {bydr} km (not used) | {state}")
    global _last_active
    if driving:
        _last_active = time.monotonic()
    # Stay fast for a while after the last driving signal so a long red light / short stop
    # doesn't drop us to hourly polling mid-trip.
    if driving or time.monotonic() - _last_active < ACTIVE_GRACE:
        return FAST_INTERVAL
    # parked: poll more often only while someone is actually looking (12 V battery, see PROJECT_PLAN)
    base = VIEWER_PARKED_INTERVAL if viewer_active() else SLOW_INTERVAL
    if time.monotonic() - _last_charging < CHARGE_GRACE:
        return min(base, CHARGE_INTERVAL)               # charging: the car is awake anyway; resolve the session
    return base


CRED_POLL_S = 3.0        # how often to look for a new or changed sign-in while there is nothing to do
RETRY_S = 60.0           # after a network error (NOT after a rejected login): wait, then try again


def _auth_rejected(e: BaseException) -> bool:
    """BYD rejected the login itself (wrong password, wrong region, account locked). NOT a session that merely timed out."""
    names = {c.__name__ for c in type(e).__mro__}
    return "BydAuthenticationError" in names and "BydSessionExpiredError" not in names


async def _wait_for_change(version) -> None:
    while byd_config.credentials_version() == version:
        await asyncio.sleep(CRED_POLL_S)


def _real_client(config):
    from pybyd import BydClient
    return BydClient(config)


async def run_session(creds: dict, version, conn, client_factory=None) -> str:
    """
    One signed-in session. Returns why it ended: "changed" (sign-in saved/replaced/removed), "auth-failed" (BYD rejected it),
    "no-vehicle", "bad-config" or "retry" (network trouble: try again).

    A rejected login is NEVER retried by itself: repeated wrong logins can lock a BYD account. It waits until the sign-in
    changes (you sign in again). Network errors are retried.
    """
    pollstatus.write("starting")
    try:
        config = byd_config.config_from(creds)
    except ValueError as e:                               # a host that is not a BYD regional server: never send a password
        pollstatus.write("error", str(e))
        print(f"Cannot start: {e}")
        await _wait_for_change(version)
        return "bad-config"
    factory = client_factory or _real_client
    try:
        async with factory(config) as client:
            await client.login()
            vehicles = await client.get_vehicles()
            if not vehicles:
                pollstatus.write("no-vehicle", "This BYD account has no vehicle.")
                print("No vehicles on account.")
                await _wait_for_change(version)
                return "no-vehicle"
            vin = vehicles[0].vin
            print(f"Polling vehicle ...{vin[-4:] if vin else '?'}\n")
            pollstatus.write("ok")
            while True:
                if byd_config.credentials_version() != version:
                    return "changed"
                started = time.monotonic()
                try:
                    interval = await poll_once(client, vin, conn)
                    pollstatus.write("ok")
                except Exception as e:  # noqa: BLE001
                    if _auth_rejected(e):
                        pollstatus.write("auth-failed", str(e))
                        print("BYD rejected the stored sign-in. Not retrying: sign in again on the Account page.")
                        await _wait_for_change(version)
                        return "auth-failed"
                    print(f"  poll error: {type(e).__name__}: {e}; retrying in 60s")
                    pollstatus.write("error", f"{type(e).__name__}")
                    interval = 60
                await sleep_interruptible(interval, started, stop=lambda: byd_config.credentials_version() != version)
    except Exception as e:  # noqa: BLE001
        if _auth_rejected(e):
            pollstatus.write("auth-failed", str(e))
            print("BYD rejected the sign-in. Not retrying: sign in again on the Account page.")
            await _wait_for_change(version)
            return "auth-failed"
        pollstatus.write("error", f"{type(e).__name__}")
        print(f"  cannot reach BYD ({type(e).__name__}); trying again in {RETRY_S:.0f}s")
        return "retry"


async def main() -> None:
    conn = db.connect()
    print(f"Poller started. DB: {db.DB_PATH}. Ctrl+C to stop.\n")
    said_waiting = False
    try:
        while True:
            creds = byd_config.load_credentials()
            version = byd_config.credentials_version()
            if creds is None:
                pollstatus.write("waiting-for-signin", "Open the app on this computer and sign in to BYD.")
                if not said_waiting:
                    print("Not signed in. Open the app on this computer and sign in to BYD. Waiting ...")
                    said_waiting = True
                await _wait_for_change(version)
                continue
            said_waiting = False
            if await run_session(creds, version, conn) == "retry":
                await asyncio.sleep(RETRY_S)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
