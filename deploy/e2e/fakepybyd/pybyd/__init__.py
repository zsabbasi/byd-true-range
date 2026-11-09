"""A stand-in for pyBYD, for an end-to-end test on Linux without real BYD credentials. It talks to nobody."""
import datetime as dt
import time
from types import SimpleNamespace as NS


class BydAuthenticationError(Exception):
    code = "3008"


class BydConfig:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Vehicle:
    vin = "LGXFAKE0000000000"
    model = "ATTO3"


class BydClient:
    def __init__(self, config):
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def login(self):
        if self.config.password != "correct-horse-battery":
            raise BydAuthenticationError("Login failed: code=3008 message=Wrong email or password, please key in again.")

    async def get_vehicles(self):
        return [_Vehicle()]

    async def get_vehicle_realtime(self, vin, **kw):
        stamp = dt.datetime.fromtimestamp(time.time(), dt.timezone.utc).isoformat()
        return NS(elec_percent=80.0, total_mileage=12345.0, endurance_mileage=350.0, speed=0.0, gl=0.0, charge_state=None,
                  charging_state=None, vehicle_state=None, power_gear=None, timestamp=stamp, recent_50km_energy="14.2",
                  recent_50km_energy_ev=14.2, online_state=None, connect_state=None)

    async def get_gps_info(self, vin):
        raise RuntimeError("no gps in the fake")

    async def get_energy_consumption(self, vin):
        return NS(nearest_energy_consumption=None)
