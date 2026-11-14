# BYD true range via the cloud API

Reads your car's data from BYD's cloud (the backend the BYD phone app uses) with the open-source
[`pyBYD`](https://github.com/jkaberg/pyBYD) library, records a local history BYD doesn't keep, and shows a
**true range as a band with a trust %** that narrows as you drive. **Read-only: it never sends commands to
the car.** Status: working on one real Atto 3 (Standard Range, Pakistan); see `docs/SAFETY.md` for what is and is not verified. Not affiliated with BYD.
Estimates only; never rely on it alone for range planning.

## Setup (Windows, PowerShell) - about 10 minutes

You need Python 3.12 (`py --version`) and your **dedicated BYD sub-account** (so the phone app is not logged
out).

### 1. Create the environment and install the locked dependencies
```powershell
cd D:\Vibe\BYDRangeApp\cloud
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
```
`--require-hashes` makes pip refuse any package whose hash is not in `requirements.txt` (supply-chain
protection). Do **not** run a plain `pip install pybyd`. A "new pip available" notice is harmless.

### 2. Run the tests (no account needed)
```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
Expect `OK` (about 57 tests).

### 3. Sign in
There is no credentials file. Start the app (`run.py`), open it on this computer and sign in on the **Account** page with the
**sub-account** (see "Signing in to BYD" below). The login is stored encrypted and only ever sent to BYD over HTTPS.

### 4. Check that login works (the probe, optional)
```powershell
.\.venv\Scripts\python.exe probe_login.py
```
It uses the sign-in you stored in step 3, wakes the car (5-15 s) and prints which fields BYD returns for your car. Nothing is written to disk.

- **`Login OK`** and several `[SET]` lines: success. Go to step 5.
- **Auth or region error:** sign in again on the Account page with another country code or region (for example `PK`, `AE`, `TH`,
  `AU`). Do not retry many times in a row; repeated failed logins can lock the account.
- **"Not signed in":** you skipped step 3.

### 5. Send me the probe output
The probe hides the VIN (last 4 only) and rounds GPS to 0.1 degrees, so the text is safe to paste. Check it
for anything personal first. What I need to see:
`elec_percent`, `total_mileage`, `endurance_mileage`, `charge_state`, `vehicle_state`, `power_gear`, and
the "last 50 km" fields (rate, kWh, unit, A/C %). Those decide the SOH and range-accuracy work.

### 6. Start recording
```powershell
.\.venv\Scripts\python.exe poller.py
```
Polls about every 30 s while driving and every 15 min while parked (2 min while the app is open); this protects the car's 12 V battery. Each reading
goes to `data\history.sqlite`; the live line shows your true-range band next to BYD's number. Stop with
Ctrl+C. Run it on an always-on PC to build history; the range estimate needs a few drives (it says "not
enough driving data yet" until then).

## The app (see your range on a phone or PC)

```powershell
.\.venv\Scripts\python.exe run.py
```
Starts the poller **and** the web app together; open <http://localhost:8765>. It shows a large range figure
with a **likely band** and a **trust %**, BYD's own range for comparison, battery, speed, consumption, and a
24 h battery chart. Ctrl+C stops both. (Run `poller.py` and `server.py` separately if you prefer.)

- **With only a reading or two of data** it shows a clearly labelled *provisional* range (from BYD's last-50-km
  consumption and your configured battery size), with a deliberately wide band and low trust. After about
  25 km of your own driving it switches to a range built from your driving, and the band narrows with use.
- **Treat the band, not the number.** SOC is a whole percent, so any estimate is approximate.
- **No GPS, VIN or account data is ever sent to the page.** The server opens the database read-only and sends
  nothing to BYD.

### Trips, maps and consumption
The **Trips** tab lists every drive (newest first) with distance, duration, battery used, average speed and
**consumption in kWh/100 km with a +/- uncertainty**. Tap a trip for its route on a map (coloured by speed), a speed
chart and the full figures. The header shows overall consumption across all recorded driving.

- **Honest numbers.** Battery is a whole percent, so a 3 km trip says "too short to measure" instead of inventing a
  figure. The overall total is reliable because rounding errors average out across trips.
- **Unrecorded drives.** If the car goes silent and the odometer then jumps, the list shows an "unrecorded drive"
  with its distance and battery change, but no route (we were not told where it went). Nothing is hidden.
- **Privacy switches.** Routes use map images from OpenStreetMap, so your browser asks `tile.openstreetmap.org` for
  tiles around your route (they can see roughly where you look, not who you are; no cookies or referrer are sent).
  `--no-map-tiles` draws routes on a plain background and contacts nobody. `--no-location` removes coordinates and routes
  entirely (trip statistics still work). Pass either to `run.py`, for example `run.py --require-token --no-map-tiles`.
- **If you share the token link** (ngrok etc.), anyone holding it can now also see your **route history**, not only
  the live range. Prefer Tailscale, or use `--no-location` when exposing the app.
- Browsing trips does **not** wake the car; only the Now tab does.

### How often it polls (and why the app wakes it)
Every poll wakes the car's TBOX and draws a little 12 V battery, so polling is adaptive:

| Situation | Interval |
|---|---|
| Driving (speed > 0 or gear ON), and for 10 min afterwards | 30 s |
| Parked, **app open on a visible screen** | 2 min |
| Parked, nobody watching | 15 min |

Opening the app **wakes the poller within seconds** (at most one poll per 30 s), so open it when you get in
the car. A hidden tab or a locked phone stops asking, so it won't keep the car awake from a pocket. A trip you
don't watch is still captured: the last parked reading is a valid starting point for the next one.
Only a signed-in app can trigger this (an unauthenticated request cannot).

**Run only ONE copy of `run.py`.** Two pollers logging in with the same sub-account can log each other out and
write duplicate rows.

### Seeing it on your phone, and in the car
The app runs on the machine running the poller. Your phone needs a route to it:
- **Same Wi-Fi (at home):** `run.py --host 0.0.0.0` prints a URL with a secret token. Plain HTTP, so only on a
  network you trust. Windows may ask to allow Python through the firewall.
- **In the car (phone on mobile data):** use a private tunnel such as [Tailscale](https://tailscale.com/)
  (install on the PC and phone, then browse to the PC's Tailscale address, port 8765). It is encrypted and not
  exposed to the internet. **Do not port-forward this app to the internet.**
- **ngrok / Cloudflare Tunnel:** these forward their own hostname, so the plain localhost mode (correctly)
  answers `Forbidden host`. Run `run.py --require-token` (token on localhost), then
  `ngrok http 8765` and open `https://<your-ngrok-host>/?token=<token>` as printed. The token is then the only
  protection on a public URL, and the tunnel provider can see the traffic, so Tailscale is the safer choice.
  Consider adding the tunnel's own sign-in on top.
- Anyone with the token URL can see your range, battery and speed; treat it like a password
  (`dataccess_token.txt`, gitignored; delete the file to rotate it).
- Look at it only while parked or as a passenger. It is an estimate, not a safety system.

## Battery size (important)
Copy `settings.example.json` to `settings.json` and set `battery_kwh` to your pack: **49.92** (Atto 3 Standard
Range) or **60.48** (Extended Range). The default is 60.48, so on a Standard Range car the *provisional*
estimate and the kWh/100 km figures are too optimistic until you set it. The main range estimate
(from your own driving) does not depend on it. `usable_fraction` (default 1.0, no assumption) is only for people who know their pack's usable share; otherwise the
SOH estimator learns it. Restart `run.py` after editing.

## Charging log (the Charging tab)
Every charge the app can see, with the battery gained, **energy into the battery**, an estimated **average power** (with a +/-,
and an AC/DC guess), and a battery curve per session. A charge nobody watched (the car was silent) is still listed with its gain and
energy, but no power or curve. Energy is battery-side: it excludes charger and cable losses, which the app cannot see.

For a cost, add your own numbers to `settings.json` (nothing is assumed):
```json
{ "tariff_per_kwh": 60, "currency": "PKR", "charging_loss_pct": 10 }
```
`charging_loss_pct` is optional (default 0 = cost of the energy that reached the battery). While charging, the app polls every
5 minutes so the battery ticks, and so the power, are resolved. **No real charge has been recorded yet**, so the first one is the
first real test of this log; BYD's own power reading while charging is shown separately as "BYD live power" until it is verified.

## Charging: what the app trusts
BYD reports `CHARGING` and the battery's live power; a charge is listed within about a minute of it starting, and its energy is
the power integrated over time (with the battery % as a cross-check). A battery rise while parked also counts if the label is
ever missing. The page refreshes itself every 20 s while a charge is in progress.

## Keeping the phone updating
A web page cannot run while the phone's screen is off; it catches up the moment you wake it. The status dot pulses each time
fresh data arrives and "car data ... ago" keeps counting, so you can see it is alive. If you want it to keep updating on a
dashboard-mounted phone, tap **Keep screen on** at the bottom (it needs the HTTPS ngrok link or localhost, and uses battery).

## Regen vs charging
- **Charging** only counts when the car is **stopped** (speed zero and the odometer unchanged) and the battery percent rises.
  A battery gain while driving is **regen** and is never counted as charging, even though the odometer is whole km and can
  repeat between readings. A battery rise inside a trip is shown as "net gain from regen (downhill)", not as a charge.
- **Regen on the trip page:** energy recovered and drawn, the regen share, and a power chart (up = into the battery), from BYD's
  live battery power sampled about every 30 s. It needs 8+ readings. Per-trip regen is **rough** (typically about 35% off,
  because short bursts fall between samples); the page shows a wider +/- and says "none seen" instead of 0 when no sample
  caught a burst. A "check vs battery %" appears only on trips long enough for it to be meaningful.
- BYD does not report your regen **setting** (standard/high) or drive mode, so they are not shown.

## Tooltips
Explanations are behind the (i) buttons, so the key numbers come first. Tap (i) to open one, tap again or press Escape to close.
Add `?tips=open` to the address to show them all.

## Why the range band is not symmetric
The band is an 80% interval: a 10% chance of getting less than the low end, 10% more than the high end. For "will I make it",
the low end is the number that matters. The low end allows for the road ahead being worse than your last 50 km (highway,
A/C, wind). The high end is **capped by the best consumption BYD has ever recorded on your car** (measured energy), so the
band cannot promise more than your car has actually achieved. The page says when the cap applies.

## Battery capacity (the Battery tab)
The app learns your **usable battery capacity** from your own drives and shows it with a +/- and a confidence, plus the
30-50 km windows it used. It needs a watched drive of 30+ km that uses 5%+ of the battery (inside 15-92%) while BYD reports
its consumption; until then it shows your `settings.json` value, labelled "not measured yet". Once measured, it replaces
the setting in the BYD-consumption range estimate. The percentage compares usable energy with the nominal pack: a new pack may
deliver less than nominal, so degradation shows as a trend over months, not as one number. Confidence is capped near 60% until
the method has been checked on real drives. Estimate, not a diagnostic.

## What the range number is made of
The rule (owner-defined, see `docs/PROJECT_PLAN.md` section 6n):
1. **Once the app has watched 50 km of your driving**, the number comes from **your own driving only**: km per 1% of
   battery over your recent drives. It does not depend on battery size.
2. **Before that, or whenever your own data is missing for any reason** (the car was silent, you just charged), it uses
   **BYD's measured consumption** (its "last 50 km" kWh/100 km, which is real measured energy) on **your** battery size
   (`settings.json`). The page says which one you are looking at and how many of the 50 km are recorded.
3. **BYD's own range figure is never used**: it assumes a bigger battery (about 57 kWh in one owner's data), so it reads
   about 20% high on a 49.92 kWh car. It is shown only for comparison. The one exception is a brand-new car
   (odometer under 50 km) with no consumption figure at all, where it is shown wide and labelled "new car".

"Watched" means distance the app actually saw, with readings at most 10 minutes apart. Unreported stretches do not count.
`settings.json` also accepts `"range_mode": "own"` or `"combined"` (an experimental blend that was not more accurate in
simulation: `tools/fusion_experiment.py`).

## Signing in to BYD (in the app)
Open the app **on the computer that runs it** and go to **Account** (bottom of the page). On a server with no screen, use an SSH
tunnel from your own computer (`ssh -L 8765:localhost:8765 you@server`, then open `http://localhost:8765`): see `deploy/README.md`.

- **Never use your primary BYD login.** Use a dedicated sub-account: share your car with a second BYD account in the BYD app and
  sign in with that one. The page makes you confirm it before it lets you sign in.
- **Your password goes to BYD and nowhere else.** It is typed on the computer that runs the app, sent to BYD over HTTPS, and stored
  *encrypted* on that computer so you stay signed in: AES-256-GCM on Linux, with the key kept **outside** the data folder (a
  systemd credential, or `~/.config/byd-range/credential.key`); Windows DPAPI on Windows. It is never logged and no page or API
  ever returns it.
- **You stay signed in.** The app signs itself back in when BYD's session expires, and survives restarts. If BYD ever **rejects**
  the stored login, it stops and waits for you; it never retries by itself, because repeated wrong logins can lock a BYD account.
- **Sign out** (same page) deletes the stored login. It works from the phone app too; signing in does not (computer only).
- The form defaults to **Pakistan**: country PK and the confirmed Singapore / Asia-Pacific server are filled in, and the server address is shown. Choose "Other country" for anything else.
- **Why only on the computer that runs the app?** (This matters if friends or family use your server: they cannot enter their own BYD password on it, and it reads one BYD account. They run their own copy, or you sign in with a sub-account shared from their BYD app.) A web page cannot sign in to BYD itself (BYD's servers refuse calls from other
  sites, and the login uses encryption that exists only in pyBYD), so the sign-in has to happen next to the poller. The app also
  refuses to take a password through a tunnel (ngrok, Cloudflare), a reverse proxy, or the network, and allows 5 attempts per 10
  minutes. A web page in your browser cannot sign the app in to someone else's account either (it needs a header such as a forged
  page cannot send).
- Choose your battery size on the same page (Standard Range 49.92, Extended Range 60.48, or another size). Until you do, the app assumes 49.92 kWh and says so on the Now page.

## Troubleshooting login
- **`code=3008 Wrong email or password` with correct credentials:** your account probably lives on a
  different BYD regional server than the default EU one (BYD runs one backend per region). Run
  `find_region.py --dry-run` to see what it would try, then `find_region.py`. It tries each host once,
  allows only `dilinkappoversea-<xx>.byd.auto` hosts, and prints the region and country code to choose on the Account page.
  The country code is the plain ISO code (for example `PK`); there is no special mapping.
  Don't loop it: repeated failed logins can lock the account.
- **Confirmed working regions** (add yours via a pull request):

  | Account country | Region on the Account page | Country code |
  |---|---|---|
  | Pakistan | Singapore / Asia-Pacific (`sg`, host `dilinkappoversea-sg.byd.auto`) | `PK` |

- **The sub-account must log into the phone app on its own** before it will work here.
- Background: [pyBYD issue #24](https://github.com/jkaberg/pyBYD/issues/24) (same 3008 pattern, Australia).

## Tips
- **Keep logging at INFO.** DEBUG logging in pyBYD can write VIN and GPS to log files.
- **Don't run it aggressively while parked.** Every poll wakes the car.
- **Back up** `data\history.sqlite` if you value the history. It is gitignored.
- **Standard Range car?** Set `battery_kwh` in `settings.json` (see above).

## Parked checks (how often the parked car is looked at)

The Account page has a **Parked checks** card: choose how often the app checks the car while it is parked and nobody is looking at the app (2, 3, 5, 10, 15, 30 or 60 minutes; the file accepts any whole number
from 2 to 120). It is per person (on a shared server each person has their own), and the poller reads it at every check, so a change applies at the next one without a restart. The default is **15 minutes**; 5 catches more short trips. A shorter interval catches short trips that would otherwise fall between two checks ("unrecorded drives") but wakes the car's system more often and may use more of the
12 V battery. Charging is checked every 5 minutes whatever the setting.

## Parked battery drain (the Battery tab)

The **Parked battery drain** card shows how much the displayed battery percentage falls while the car sits parked and unplugged, in percent per day, from readings the app already stored. Only stretches of 2 hours or more count (the percent is
a whole number, so a one-percent fall over a few hours says almost nothing), and nothing is shown until there are 6 hours of parked time in the last 14 days. It also says how many readings it had per parked hour, so you can
compare before and after changing the parked interval. It cannot see the 12 V battery, and it cannot tell BYD's own parked drain from anything the checks cause. Measured once by hand on the owner's car: a 10-hour night lost no visible battery.

## Car mode (on the car's screen)

The app recognises a head unit (a desktop-style Linux user agent on an ARM platform with a touch screen: the signature of the one unit seen so far; a normal Android phone does not match) and switches to **car mode**: the status
refreshes every 3 seconds (20 normally, 5 while driving), the chart every 15 seconds, every other page at least every 10 seconds; the screen is kept awake where the browser allows it; and the layout is wider and larger (the installed app
on the unit lays out at 980 px). The footer button **Car mode: auto / on / off** overrides the detection (the page reloads), and `?car=1`, `?car=0` or `?car=auto` sets it once. Nothing is sent anywhere. Note what it is not: the page can
only refresh as fast as the poller gets new readings from BYD (about every 30 seconds while the car is on), and the head unit's own browser lock while driving is not something this works around.

**Car layout** (car mode, Now page): everything fits one landscape screen with no scrolling. The GPS speed sits on the right of the range, then one row of tiles (battery, BYD range, consumption, tyres, heading) and the battery power chart of the last 20 minutes (the same +/- bars as the Trips tab) filling the rest. Speed and heading come from the screen's own GPS, computed on the screen, never sent anywhere or stored; tap Heading to show the position (hidden by default). GPS starts by itself only where the permission is already granted, otherwise you tap "Start GPS". Power and tyre pressures are BYD's readings with their age, dimmed when older than 90 s (BYD reports a spot value about every 30 s). No accelerometer is used: the unit's gives a fixed value, and it cannot give speed anyway. The range calculation is unchanged. Tyre pressures are recorded with every reading; `/api/status` carries `power` (with a 20-minute `trend`) and `tyres`. The Battery page also shows the range on a full battery (`full_range` in `/api/battery`), from the same engine at 100%.

## Files
| File | What |
|---|---|
| `requirements.in` / `requirements.txt` | Direct dependency / hash-pinned lock (19 packages). |
| `byd_config.py` | Loads the stored sign-in, builds the pyBYD config. No control PIN (read-only). |
| `probe_login.py` | Login + field-presence check (step 4). |
| `poller.py` | Adaptive poller -> SQLite history + live range. |
| `range_engine.py` | True-range maths: band + trust %. Pure, tested. |
| `db.py` | SQLite history schema and queries. |
| `server.py` + `web/` | Local web app and read-only JSON API. |
| `trips.py` | Trip detection and per-trip consumption (pure, tested). |
| `run.py` | Starts poller + web app together. |
| `find_region.py` | Finds your BYD regional server (see Troubleshooting). |
| `tests/` | Simulator-based tests; no account needed. |

## Updating dependencies (maintainers)
Edit `requirements.in`, then regenerate the lock with `pip-compile --generate-hashes --allow-unsafe
--strip-extras` and review the diff. See `..\docs\PYBYD_AUDIT.md` for the audit; it covers the pyBYD
`main` branch, not the pinned 0.0.79 release specifically.
