# True range for a BYD Atto 3

A real-world range estimate, trips, charging log and battery-capacity check for a **BYD Atto 3**, computed from **what you actually drove** instead of the figure the car shows. It reads the car's data from BYD's cloud (the backend the BYD phone app uses), keeps a local history BYD does not, and shows the result as a mobile-first web app you can install on your phone.

> **Experimental hobby project. Read [docs/SAFETY.md](docs/SAFETY.md) before using it.**
> It is **read-only** (it never sends a command to the car), but it signs in to BYD with an account and polls the car. It is **not** a safety system. Never rely on it alone for range planning. Not affiliated with, or endorsed by, BYD.

## What is in here

| Folder | What it is |
|---|---|
| `cloud/` | **The app.** A poller (reads the car from BYD's cloud through the audited, hash-locked `pyBYD` library, adaptively: fast while driving, slow while parked) and a small web server and page: range as a **band with a trust %** that narrows as you drive, trips with maps, consumption and regen, a charging log with energy and cost, a learned usable battery capacity, an installable phone app (PWA). Python standard library plus `pyBYD` and `cryptography`. |
| `deploy/` | Linux deployment: a hardened systemd unit, install guide, and an end-to-end test that runs the real app against a **fake** pyBYD (no BYD account, no network). |
| `docs/` | The decision log and plan, the supply-chain audit of pyBYD, the hosting and security notes, and the safety guide. |

## How it works

```
car -> BYD cloud -> pyBYD (one library, audited, hash-locked)
                        |  poller: 30 s while driving, 15 min parked, faster while the page is open
                        v
                 history.sqlite (local)  ->  range engine, trips, charging, capacity
                        |
                 small web server  ->  your browser / installed phone app
```

- **Range policy:** BYD's measured consumption (kWh/100 km) until the app has watched 50 km of your own driving, then only your own data. **BYD's own range estimate is never used** (except on a brand-new car with nothing else). Your own estimate can never beat the best consumption BYD has ever measured on your car.
- **Whole-percent battery data** is why the answer is a band with a trust figure, not a single number.
- **Battery size is your choice.** BYD's cloud does not say which pack you have. The app asks (Standard Range 49.92 kWh, Extended Range 60.48 kWh, or another size) and, until you choose, says it is assuming Standard Range. The Battery tab then measures the real usable energy from your drives.
- **Read-only.** No control commands exist in the code. Credentials only ever go to BYD, over HTTPS.
- **Sign-in is local-only and encrypted at rest.** The password is typed on the computer that runs the app (or through an SSH tunnel to it), never over a tunnel, proxy or the network, and stored AES-256-GCM encrypted with the key outside the data folder. There is no credentials file.

## Compatibility: what has actually been tested

| Item | Value |
|---|---|
| Car | BYD Atto 3, Standard Range (49.92 kWh), Pakistan |
| BYD region | Singapore / Asia-Pacific (`sg`), country code `PK` (confirmed). Other regions are listed because their servers exist, **not** because they were tested. |
| Library | `pyBYD` 0.0.79 (hash-locked in `cloud/requirements.txt`) |
| Server | Python 3.11 or newer; Linux is the target (tested on Ubuntu 22.04, x86-64 and ARM64 dependencies verified); Windows works for development |
| Phone | Installed and working from Chrome on Android. iPhone untested. |

Anything else may work or may not. Reports of other regions, models and chargers are the most useful contribution; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start

```bash
cd cloud
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt   # never a plain `pip install pybyd`
.venv/bin/python -m unittest discover -s tests               # about 400 tests, no BYD account needed
.venv/bin/python run.py                                      # then open http://localhost:8765 and use the Account page
```

Windows development and the full walkthrough are in [cloud/README.md](cloud/README.md). Running it as an always-on Linux service, reaching it from your phone and installing it as an app are in [deploy/README.md](deploy/README.md).

**Use a dedicated BYD sub-account, never your primary login.** Share your car with a second BYD account in the BYD app and sign in with that one.

## What is deliberately not in this repository

Credentials, tokens, sign-in stores and keys; your recorded history (it contains where you drive); the BYD phone app and any decompiled or captured BYD traffic; screenshots with personal data. The `.gitignore` excludes them. Do not add them. BYD's cloud interface is undocumented and used without BYD's permission; check the terms and the law where you live before relying on it or redistributing anything of BYD's.

## Documentation

- [docs/SAFETY.md](docs/SAFETY.md): what can go wrong and how it is contained
- [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md): decisions, findings, bugs and how each was verified
- [docs/PYBYD_AUDIT.md](docs/PYBYD_AUDIT.md): supply-chain audit of the one dependency that talks to BYD
- [docs/HOSTING_SECURITY.md](docs/HOSTING_SECURITY.md): hosting options and what is and is not safe to share
- [CONTRIBUTING.md](CONTRIBUTING.md): ground rules, tests, how to help

## License

Apache License 2.0, see [LICENSE](LICENSE). The software comes with no warranty; read [docs/SAFETY.md](docs/SAFETY.md) before using it with a real car account.
