"""
Phase 1 validation: confirm we can log in to BYD's cloud for THIS account/region and see
which data fields are populated, before building the poller or the phone app.

Run:  python probe_login.py
Uses the sign-in stored by the app. Prints a field report. Redacts VIN and exact GPS in the
console (shows only that they're present + coarse rounding) so a screenshot is safe to share.

Nothing is written to disk by this script.
"""
from __future__ import annotations

import asyncio

from byd_config import build_config

# Fields we care about for a "true range" tool, grouped for a readable report.
REALTIME_FIELDS = [
    ("elec_percent", "Battery SOC %"),
    ("endurance_mileage", "BYD's own remaining range (km)"),
    ("total_mileage", "Odometer (km)"),
    ("speed", "Speed (km/h)"),
    ("gl", "Instant battery power (W)"),
    ("power_battery", "Battery power"),
    ("charging_state", "Charging state"),
    ("charge_state", "Charging state (authoritative)"),
    ("recent_50km_energy", "BYD's last-50km energy"),
    ("nearest_energy_consumption", "Recent consumption"),
    ("energy_consumption", "Energy consumption"),
    ("temp_in_car", "Cabin temp (C)"),
    ("vehicle_state", "Vehicle state"),
    ("power_gear", "Gear"),
    ("timestamp", "Reading timestamp"),
]


def show(label: str, value) -> str:
    if value is None:
        return f"  [ - ]  {label}: (none)"
    return f"  [SET]  {label}: {value!r}"


async def main() -> None:
    config = build_config(mqtt_enabled=False)
    print(f"Logging in to {config.base_url} (country={config.country_code}) ...")

    from pybyd import BydClient

    async with BydClient(config) as client:
        await client.login()
        print("Login OK.\n")

        vehicles = await client.get_vehicles()
        if not vehicles:
            print("No vehicles on this account.")
            return
        print(f"Vehicles on account: {len(vehicles)}")
        v = vehicles[0]
        vin = v.vin
        print(f"  Using vehicle 1. VIN present: {bool(vin)} (last 4: ...{vin[-4:] if vin else '?'})")
        model = getattr(v, "model", None) or getattr(v, "out_model_type", None)
        if model:
            print(f"  Model: {model}")
        print()

        print("Fetching realtime snapshot (this wakes the car; may take 5-15s)...")
        rt = await client.get_vehicle_realtime(vin)
        print("Realtime fields:")
        for attr, label in REALTIME_FIELDS:
            print(show(label, getattr(rt, attr, None)))
        print()

        print("Fetching GPS...")
        try:
            gps = await client.get_gps_info(vin)
            lat = getattr(gps, "latitude", None)
            lon = getattr(gps, "longitude", None)
            if lat is not None and lon is not None:
                # coarse rounding only, so a shared screenshot doesn't reveal exact location
                print(f"  [SET]  GPS present. Coarse: {round(lat, 1)}, {round(lon, 1)}")
            else:
                print("  [ - ]  GPS not populated")
        except Exception as e:  # noqa: BLE001
            print(f"  GPS fetch failed: {type(e).__name__}")

        print()
        print("Extra realtime fields (consumption, A/C, drive mode):")
        for attr, label in [
            ("recent_50km_energy", "recent_50km_energy (raw)"),
            ("recent_50km_energy_ev", "recent_50km_energy_ev"),
            ("recent_50km_energy_ev_unit", "recent_50km_energy_ev unit"),
            ("nearest_energy_consumption_ev", "nearest_energy_consumption_ev"),
            ("total_energy_ev", "lifetime avg consumption"),
            ("power_battery", "power_battery"),
            ("online_state", "online_state"),
            ("connect_state", "connect_state"),
        ]:
            print(show(label, getattr(rt, attr, None)))
        print()

        print("Fetching energy endpoint (absolute kWh over last 50 km, A/C share) ...")
        try:
            en = await client.get_energy_consumption(vin)
            near = en.nearest_energy_consumption
            if near is None:
                print("  [ - ]  nearest_energy_consumption not populated")
            else:
                for attr, label in [
                    ("ev_consumption", "kWh used, last 50 km"),
                    ("ev_value_unit", "  unit of the above"),
                    ("avg_ev_consumption", "avg kWh/100km, last 50 km"),
                    ("ev_unit", "  unit of the above"),
                    ("air_distribution", "% of last 50 km with A/C on"),
                    ("drive_distribution", "% normal drive mode"),
                    ("elect_distribution", "% electric"),
                    ("other_distribution", "% other"),
                ]:
                    print(show(label, getattr(near, attr, None)))
        except Exception as e:  # noqa: BLE001
            print(f"  Energy fetch failed: {type(e).__name__}")

        print("\nDone. If the important fields above show [SET], Phase 1 works and we can build the poller.")


if __name__ == "__main__":
    asyncio.run(main())
