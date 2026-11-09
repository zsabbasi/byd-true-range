# BYD True Range — Project Plan

_Last updated: 2026-11-08_

## 1. Goal

A "true range" tool for the BYD Atto 3, like the Lingbox project in `D:\Research\ev-range-headunit`:
show the **real** remaining range computed from recent driving (not BYD's optimistic estimate), plus a
**driving / GPS history** that BYD itself does not keep, and **live speed / mileage** while driving.

## 2. How we get the data

Three approaches were explored. We chose the cloud API.

| Approach | Status | Why |
|---|---|---|
| Head-unit hook (DiLink 3.0) | **Parked** | adb is locked behind a manufacturer-gated unlock; on-device access is fragile and firmware-specific. Recon/probe tooling kept in `recon/`, `probe/` for reference. |
| ADB unlock via third-party app | **Analyzed, not pursued** | Decompiled the user's own `BYD Secret.apk`; reimplemented the code generator in `adb_unlock/` (works), but the whole adb route was abandoned in favour of the cloud. |
| **BYD cloud API (`pyBYD`)** | **Chosen** | Clean, documented, no car modification. Reads SOC, odometer, speed, range, GPS, charging from BYD's backend (same one the BYD phone app uses). |

### pyBYD
- Open-source, MIT, pip-installable ([`jkaberg/pyBYD`](https://github.com/jkaberg/pyBYD)).
- **Source-audited** (`docs/PYBYD_AUDIT.md`): talks only to BYD, no telemetry/exfiltration; password used
  only at login, never written to disk, redacted from logs. 4 blue-chip runtime deps.
- Key fields: `elec_percent` (SOC), `total_mileage` (odometer), `endurance_mileage` (BYD's own range),
  `speed`, `gl` (power), `charge_state`, `recent_50km_energy`; plus `get_gps_info()` for lat/lon.

### Auth model (drives the hosting design)
- The **password is needed only at login**. After that, tokens (`signToken`, `encryToken`, `userId`) do
  all the work; the MQTT credential derives from `signToken`, not the password.
- **No token-only refresh exists** — when tokens expire, a new `login()` (needing the password) is
  required. Client-side TTL defaults to 12 h; real server-side token lifetime is **unknown / to be
  measured**.

## 3. True-range math

Same idea as Lingbox, adapted to coarser cloud snapshots (SOC + odometer deltas instead of V×I):

```
between two non-charging readings:
  distance   = odometer_2 - odometer_1
  energy     = (soc_1 - soc_2)/100 * usable_capacity_kWh
  consumption= energy / distance * 100          (kWh/100km)
average consumption over the most recent ~50 km of driving, then:
  true_range = (soc_now/100 * usable_capacity_kWh) / consumption * 100
```
- Usable capacity: **60.48 kWh** (Atto 3 Extended Range) or 49.92 kWh (Standard).
- Fallbacks: recent-50 km → all-data → default (16 kWh/100 km) until enough data exists.
- Charging segments skipped. Cross-checked against BYD's own `endurance_mileage` and `recent_50km_energy`.
- Implemented and unit-tested in `cloud/range_engine.py`.

## 4. "Realtime" expectation

Cloud polling **wakes the car's TBOX** (~5–15 s per poll) and draws the **12 V battery**. So live data
refreshes every ~30 s while driving — not every 2 s like the CAN-bus Lingbox project. Adaptive cadence:
fast (~30 s) while driving, hourly while parked (also serves as the hourly GPS history sampler).

## 5. Phases

### Phase 1 — Python reference + data capture ✅ built, ⏳ awaiting first real run
Location: `cloud/`.
- [x] `byd_config.py` — safe credential loading from gitignored `secrets.env`; `base_url` passed
      explicitly so the shell env can't redirect traffic.
- [x] `probe_login.py` — validates login for the user's account/region, reports populated fields.
- [x] `range_engine.py` — true-range math (unit-tested).
- [x] `db.py` — SQLite history (the history BYD doesn't keep).
- [x] `poller.py` — adaptive poller → history + live range.
- [ ] **User action:** create venv, `pip install pybyd`, fill `secrets.env`, run `probe_login.py`.
- [ ] Confirm field mappings & region from real output; fix any field-name gaps.
- [ ] Let the poller run to accumulate a few real drives for validation.
- [ ] **Measure real token lifetime** (how long before re-login is forced) — decides hosting design.

### Phase 2 — Hosting / architecture decision ✅ DECIDED (2026-11-08)

**Decision: one open-source codebase, three deployment modes, built in this order.**
1. **Self-host, single user (Option C)** — default and safest; first release.
2. **Community shared server, multi-tenant (Option A)** — many users per instance.
3. **Federated** — anyone runs an instance; users choose one they trust (no single operator holds everyone's data).

Build **single-tenant first with a tenant-aware schema (`user_id` on every table) from day one**, so
multi-tenant is an addition, not a rewrite. The web dashboard served by the poller replaces the native
phone app as the primary UI (avoids porting the Bangcle crypto).

**Trust model (be honest in docs/consent copy):** open source + CI/CD gives *verifiable builds and operator
accountability*, not *provable blindness*. The operator's process still holds each user's plaintext
location/tokens while polling. Real proof would need confidential computing (TEE + remote attestation) —
out of scope.

**Multi-tenant isolation requirements**
- Per-user encryption keys for tokens + history (key derived from a user passphrase via Argon2); DB dump
  alone is unreadable. Trade-off: forgotten passphrase = lost history; user must unlock after server restart.
- Postgres row-level security scoped by `user_id` (not app-code-only); per-tenant rate limits; tokens
  stored apart from history.
- BYD password never stored or sent to the server — client logs in locally, hands over tokens only.
- Dedicated BYD sub-account per user; consent copy covers location + car-control token custody.
- Retention limit, one-click full delete, token revoke/expiry reminders.
- No DEBUG logging in production; automated test fails if VIN/userId/GPS reach logs.

**CI/CD requirements (the trust story)**
- Every PR: tests, `pip-audit`, secret scanning, SAST.
- Dependencies pinned by hash; pyBYD vendored/pinned to a reviewed commit.
- Reproducible image build, signed (cosign/sigstore) with SLSA provenance; deploys pull signed digests only.
- Infrastructure as code in-repo; `SECURITY.md` with disclosure process and threat model.

**Open decisions:** (a) is passphrase-derived key acceptable; (b) will the owner run a flagship shared
instance (becomes a data processor: GDPR-type duties) or only ship software.

_Original option analysis kept below for reference._

#### Original analysis (superseded by decision above)
The hard constraints (see `docs/HOSTING_SECURITY.md`):
- The password need never reach a server (token handoff is possible).
- But any server that polls BYD **sees the car data in plaintext** — a "blind" polling server is
  impossible. Encryption-at-rest limits breach damage only.
- "Server does MQTT only" does **not** give reliable hourly GPS history (MQTT is passive; GPS needs HTTP
  triggers; decryption needs the tokens anyway).

Options on the table:
- **A. Token handoff** — hosted server polls using phone-supplied tokens; password stays on phone; data +
  tokens encrypted at rest. You become a data processor (location + car-control tokens = sensitive PII).
- **B. Zero-knowledge sync** — phone does all BYD comms; server stores only phone-encrypted blobs; never
  readable. Lowest data liability, but no 24/7 background history (only when phone is awake).
- **C. Self-hosted poller** — ship the poller as software; each user runs it on their own always-on box;
  phone app is a UI over the user's own instance; **you host nothing sensitive**. Lowest liability overall.

_Decision made — see above. B remains a fallback only._

### Phase 2b — Range engine hardening & validation ⏳ next
- [x] **DONE 2026-11-08:** `cloud/range_engine.py` rewritten (runs + telescoping SOC, range band, trust %,
      "insufficient" state; capacity-independent range) with 18 simulator-based tests in
      `cloud/tests/` (`py -m unittest discover -s tests`); `db.recent_readings` + poller wired to it.
      Simulator-only validation: band covers ~82% of simulated future ranges (nominal 80%). NOT yet
      validated on real BYD data. Still open: moving vs overall consumption split, learned capacity (SOH),
      drive-mode split, `charging_state` handling (values unknown until probe).
- [ ] **Confirmed 2026-11-08: BYD app shows whole-percent SOC (no decimals).** Per-step SOC deltas are
      unusable. Replace the per-pair method in `range_engine.py` with Lingbox-style anchored segments
      (min SOC drop, default ~5%, restart anchor on charging), as in `D:\Research\ev_range.py`.
      Quantisation error is +/-0.5% per endpoint, so a 5% segment carries up to ~20% error on its own;
      only the aggregate over many segments is reliable. Prefer the 20-90% SOC band.
- [ ] Minimum-segment rules (min distance / min SOC drop) to cope with whole-percent SOC noise.
- [ ] Configurable usable capacity (not just two presets); allow degradation factor.
- [ ] Replay/validation tool: score true range vs BYD `endurance_mileage` on captured drives.
- [ ] Handle charging gaps and coarse ~30 s snapshots; consider temperature/AC effects.
- [ ] Tenant-aware schema (`user_id`) in `db.py`.

### Phase 2c — Open-source release prep ⏳ later
- [ ] Docker / one-command setup + local web dashboard.
- [ ] Public README, LICENSE, disclaimer (unofficial, not BYD-affiliated, account-lockout risk).
- [ ] Repo scrub: drop or keep private `adb_unlock/`, `recon/`, `probe/`, decompiled-APK work.
- [ ] Sample/anonymized data so contributors can develop without a BYD car.

### Phase 3 — Phone app ⏳ not started (deprioritised: web dashboard first)
- Reuse the verified auth + `range_engine` logic. **The BYD login crypto (AES + "Bangcle" white-box table
  codec) must be re-ported to the app's language** — the riskiest porting task; the Python reference is the
  test oracle.
- Live driving view (speed, SOC, true range, BYD range) refreshed adaptively.
- GPS/driving history with map + trip list.
- Background polling: ~hourly on Android (WorkManager); best-effort on iOS (OS-limited).
- Stack TBD with the hosting decision (native Android-first vs Flutter).

### Phase 4 — Hardening ⏳ later
- Pin/vendor pyBYD (or the re-port) to a reviewed commit.
- Per-user encryption, retention limits, user-delete, consent copy.
- Optional: one-time mitmproxy run to confirm runtime hosts.
- Dedicated BYD sub-account guidance for users (revocable; not their main account).

## 6. Open questions / risks

1. **Region** — default endpoint is EU-oversea (`dilinkappoversea-eu.byd.auto`). The owner's market may
   need a different `BYD_COUNTRY_CODE` / `base_url`. Confirmed only by running `probe_login.py`.
2. **Token lifetime** — unknown; determines how often the phone must re-authenticate for background work.
3. **Account logout** — same-account logins kick each other out → users need a dedicated sub-account.
4. **12 V battery drain** — aggressive polling while parked is unsafe; adaptive cadence mitigates.
5. **Legal** — storing real-time location + car-control tokens is sensitive PII with breach/consent duties
   under options A/B.

## 6e. First real probe results (2026-11-08, Atto 3, Pakistan account) - FACTS, not assumptions

- **Login works** once the right regional host is used. BYD runs one backend per region
  (`dilinkappoversea-<xx>.byd.auto`; DNS exists for eu, au, sg, sa, in, id, jp, kr, br, mx, tr, vn; none for
  pk). The default EU host answered `code=3008 Wrong email or password` for a correct Pakistan account
  (same pattern as pyBYD issue #24). `cloud/find_region.py` tries hosts safely, one attempt each.
  **CONFIRMED 2026-11-08: Pakistan account works on `https://dilinkappoversea-sg.byd.auto` (Singapore
  backend) with `BYD_COUNTRY_CODE=PK`.** Ship this in the README as the Pakistan setting; keep EU as the default.
- **Populated:** SOC (95.0), odometer (19708.0, appears whole km), `endurance_mileage` (393 km at 95% SOC),
  speed, `power_gear`, `vehicle_state`, `charge_state`, GPS, `recent_50km_energy` (`14.0kW·h/100km`),
  `recent_50km_energy_ev` 14.0 kWh/100km.
- **Energy endpoint:** `ev_consumption` 7.0 kWh over the last 50 km = 14.0 kWh/100 km (consistent), one
  decimal place (0.1 kWh resolution). So the SOH input exists and is usable.
- **NOT populated on this car/region:** cabin temp, `air_distribution` (A/C share), drive-mode
  distribution. The planned "A/C explains range loss" feature is **not available from this endpoint**;
  `get_hvac_status` still to be probed.
- **State enums (pyBYD IntEnum, so `str()` gives digits - use `.name`):** `vehicle_state` read `ON` while
  parked and plugged in (pyBYD itself flags ON as "not confirmed") -> not used for driving detection.
  `charge_state` read `CONNECTED` (15), which pyBYD says does not track the plug -> never treated as
  charging. Driving = `speed > 0` or `power_gear == ON` (gear values: OFF=1, ON=3); gear/state names are
  now logged per reading so this can be verified on real drives.
- **Bugs this found and fixed:** old substring check on `str(enum)` could never match (poller would have
  dropped to hourly polling at the first red light); plugged-in `15` was printed as "charging". Fixed with
  a 10-minute fast-poll grace period after the last driving signal.
- Range engine now also models odometer whole-km rounding (matters on short windows).

## 6f. First version of server + app (built 2026-11-08)

- `cloud/server.py` + `cloud/web/` (stdlib only, no new dependencies, hash lock unchanged) and `cloud/run.py`
  (poller + server together). Mobile-first dashboard: big range + likely band + trust chip, BYD range tick
  for comparison, battery/speed/consumption tiles, 24 h battery chart, data-age + driving/parked status,
  "estimate only" footer. Dark/light, no external resources.
- **Provisional estimate** (`range_engine.provisional_from_consumption`) when there is not yet enough own
  driving data (works from a single reading): BYD last-50-km consumption + assumed usable capacity
  (nominal x 0.95, ASSUMPTION until SOH is learned) with a deliberately wide band. Probe row gave 390 km
  (284-496, trust 32%) vs BYD 393.
- **Security:** loopback by default; non-loopback needs an access token (URL once, then HttpOnly cookie);
  Host-header check against DNS rebinding; strict CSP; read-only DB handle; read-only HTTP methods; the API
  never returns coordinates, VIN or account data (tested). Plain HTTP: use Tailscale/WireGuard off-LAN.
- Verified by 20 server tests, 57 total, and headless-Edge screenshots of a one-row and a simulated-drive state.
- **Not done / next:** real on-road validation; trips list and map (needs GPS in a separate authenticated
  endpoint); SOH estimator (replaces the assumed 0.95); PWA install/offline; multi-user (`user_id`).

## 6g. Field bug: parked poller slept an hour and missed the drive (2026-11-08)

First real drive: the app never refreshed. DB showed 3 rows, odometer unchanged. Cause: parked cadence was
3600 s, so a trip starting mid-sleep was invisible. Fixes: parked 900 s; **viewer-driven wake** (server touches
`data/viewer_active` on authenticated `/api/status`; poller wakes within seconds, min 30 s between polls, 2 min
cadence while the app is open and parked); web app stops polling in hidden tabs. 8 new tests. Also found two
`run.py` instances (2 pollers + 2 servers) running: document "one copy only"; consider a lock file next.
**Follow-up from the real timeline (same day):** the car answered nothing between 16:02:20 and 16:46:46 UTC
(BYD returned the old snapshot every time, even with a 30 s wait and the app open), then reported normally
once it was started (gear ON, odometer already +12 km, SOC 95 -> 92). So the first 12 km of that trip were
never observed, only bracketed by the 16:02 parked reading, which the range engine correctly uses as an
anchor. Observation (one episode, not proven): a parked/asleep car may not answer cloud wake requests until it
is switched on; earlier parked polls at 15:02/15:54/16:02 did answer, possibly because the car was recently
active or plugged in (charge_state CONNECTED). Consequences: never assume parked polls succeed; the app now
shows the car's own data age and a red "no fresh data" banner; stale snapshots are not stored. First real
trip results: driving detection correct (gear ON while moving, OFF when parked), range 341 km
(242-440, trust 28%, 15 km window, 4% SOC) vs BYD 362-374; ours 16.1 vs BYD 14.4 kWh/100 km (noisy at 4%).
Longer term: MQTT push (pyBYD supports it) would detect trip start without polling; see section 2.

## 6h. Range swing fix: tick-anchored estimator (2026-11-08)

Owner feedback after the first real drive: "swinging a lot". Cause: with whole-percent SOC the plain estimate
creeps UP between battery ticks (km grow, % does not) and drops a whole percent at each tick (sawtooth). Real
replay: 296 -> 318 -> 341 km with no tick. Fix (`range_engine._estimate_transitions`): end the window at the
LATEST tick (true SOC there = new level + 0.5, known exactly if the display rounds to nearest), start it at the
best earlier endpoint (tick or reading, chosen by lowest estimated error), carry the odometer gap as position
uncertainty, and track the fractional SOC since the tick from distance driven, clipped to what the display shows.
Falls back to the plain method when the current run has no tick-anchored window yet (e.g. right after charging).
Measured: simulated poll-to-poll change 1.70% -> 0.20% (worst 13.5% -> 3.9%); real replay 326 -> 325 -> 324 km;
band width unchanged (~36%); simulated coverage 85% (nominal 80%). 78 tests.
**Assumption to verify:** BYD rounds SOC to nearest (not floor). If it floors, the tick level is +1.0 not +0.5 and
every estimate is biased ~0.5% SOC high; check by comparing the SOC at ticks against BYD range per percent or by
logging SOC against a trusted source. Open: window start uncertainty is large when the start is a long gap.

## 6i. Battery size is a setting; the owner's car is likely the 49.92 kWh pack (2026-11-08)

The default was 60.48 kWh (Extended Range), which made the provisional estimate and kWh/100 km too optimistic
(~390-400 km at 100%). The owner pointed out BYD's 14 kWh/100 km x a 49.92 kWh pack is only ~356 km. Data agrees:
measured 3.4-3.75 km per % (340-375 km at 100%, noisy, 12-15 km of data); BYD's displayed range implies 398-414 km
at 100% = roughly 17% optimistic. Now `cloud/settings.json` (battery_kwh, usable_fraction; shared by poller and
server; invalid values ignored; 6 tests). Owner's file set to 49.92. **CONFIRMED by the owner: 49.92 kWh (Standard Range).**
The SOH estimator (usable_kWh ~= BYD kWh/100km x our km per %) will also expose a wrong battery size.

## 6j. Use case and roadmap: long journeys (decided 2026-11-08)

Primary use: long drives, because BYD's own dashboard range is optimistic (about 17% on the owner's data).
Journey mode (priority order): reliable always-on hosting first (a sleeping PC or dropped tunnel kills the app
mid-trip); arrival forecast from a typed distance with a verdict based on the LOW end of the band; pace-aware window
(auto 10/25 km at highway speed); live "BYD is X% optimistic"; charging-stop helper (SOC rate, estimated kW,
time to target, leave-with % for the next leg; log BYD time-to-full fields); optional alerts (off by default,
no interaction while moving); trip summaries split by speed to learn highway consumption. Known weakness: the first
~25 km at highway speed is optimistic because history is city driving; wind, hills, temperature, A/C, load are not
modelled. Needs the owner's answers on journey length, hosting (Pi/VPS/laptop) and audible alerts.

**Head unit: out of scope for now.** Android Auto is the likely later route (owner's suggestion). From general
knowledge, unverified: it only shows template-based screens from approved app categories (not web pages), so it
would be a native Android app and the category fit must be checked; head-unit and region support is a separate
question. Design consequence: keep data and UI separate, keep the JSON API stable and versioned, and add a minimal
`/api/glance` (about three short text lines: range + band, arrival forecast + verdict) usable by any small or
templated display (Android Auto pane, widget, watch, Home Assistant).

## 6k. Trips with maps and consumption (built 2026-11-08)

`cloud/trips.py` (detection, per-trip consumption, totals, detail), `/api/trips` and `/api/trips/<id>`, Trips tab
(`web/trips.js`): list with totals, trip page with OpenStreetMap route coloured by speed, consumption with +/-,
speed chart. Consumption is tick-to-tick when a trip has 2+ battery ticks, else start/end, and is withheld
("too short to measure") below 2% battery / 3 km. Totals include short trips because rounding errors average out
(tested: 30 trips of ~4 km, each too short, total within 1.5 kWh/100 of truth). Car silent + odometer jump = an
"unrecorded drive" with no route. Switches: `--no-map-tiles` (no third party), `--no-location` (no coordinates);
CSP allows `tile.openstreetmap.org` images only when tiles are on. Trip browsing does not touch the viewer flag.
**Bugs found by real data/screenshots and fixed:** (1) BYD returned an all-zero empty snapshot (SOC 0, odometer 0,
no timestamp) which the poller stored, and which would have shown "battery 0%" and a 19,723 km trip: now never
stored, and ignored by the engine, status, chart, db reads and trips (plus a 300 km/h plausibility cap on odometer
jumps); (2) map tiles shared a CSS class with stat cards (dark gutters): renamed `maptile` with a regression test.
123 tests. Open: charging sessions, regen-setting tag, per-trip weather/temperature, trip delete/export, CSV export.

## 6l. Finding: BYD range assumes a ~57 kWh battery (2026-11-08)

Owner's hypothesis: BYD's kWh/100 km is correct (measured energy), but its remaining-range estimate uses a hardcoded
~60 kWh battery. Checked on the owner's data: BYD range x BYD kWh/100 / SOC gives 56.1-59.2 kWh across 8 distinct
readings (median 57.3), versus a 49.92 kWh pack (about 47.4 usable at 95%): BYD range is about 21% too high
(15% vs nominal). Quick correction: true range ~ BYD range x usable/57.3 (about x0.83). The provisional path already
uses BYD's rate with the owner's pack (91% SOC: 300 km, 218-381, trust 32%) whenever our own window is
insufficient; with a thin own window (12 km / 4%) the own-driving estimate (324 km) takes priority and the two differ 8%.
Proposed, not built: inverse-variance fusion of (a) BYD-rate x usable capacity (about +/-7%, capacity-limited) and
(b) own-driving km per % (capacity-independent, improves with distance), which also yields learned usable capacity
(usable ~ BYD kWh/100 x km per %). Would change the headline number, so needs the owner's go-ahead.

## 6m. Combined estimate: built, with an honest result (2026-11-08)

Owner approved using BYD's measured consumption (right) with the owner's own battery size (BYD's range assumes about
57 kWh, wrong) together with own-driving. Built: `range_engine.fuse` / `best_estimate` (inverse-variance in log space;
the road-ahead variability cv is shared and added once; a disagreement between the inputs widens the band by half the
log gap), `settings.json` `range_mode` ("combined" default, or "own"), the page shows both inputs and their weights.
Owner's data now: headline 308 km (226-390, trust 33%) = own driving 324 (weight 35%) + BYD consumption on 47.4 kWh
usable 300 km, disagree 8%; BYD's own range is 374, so the headline is 18% below it. Own-only mode gives 324.

**The simulator did NOT show an accuracy gain** (tools/fusion_experiment.py, centre error vs truth, mean of 150-200 drives):
dense polling 3.6% combined vs 3.7% own at 20 km (equal beyond); sparse start (first 12 km unreported) combined is
slightly WORSE at the very start (3.8% vs 2.9% at 2 km; 3.9 vs 3.6 at 5 km), equal from ~20 km observed; with the battery
size 7% wrong the combined error is 8.2% vs 2.9% at 2 km, 5.4-6.1% vs 3.6% at 5 km, converging after ~40 km. Band coverage is
unchanged (86-98% vs a 80% nominal). Reasons: tick-anchoring already makes the own estimate accurate in simulation; the BYD
side is limited by the unknown usable fraction (an assumption: 0.95). So the justification is robustness, not measured
accuracy: BYD's battery % is not linear with energy (Lingbox lesson), which the simulator does not model, and the disagreement
widening makes a wrong battery size visible. Revisit with real long drives; if own-only stays as good, switch the default.
**Next, and the real prize:** learn usable capacity (usable ~ BYD kWh/100 x km per %), which turns the BYD side from +/-7% into a
few % and makes the combination worth it. Owner's own numbers suggest usable could be near 100% of 49.92, not 95%
(own km per % x BYD kWh/100 is about 51 kWh, +/-10% on 12 km of data): unproven.

## 6n. THE RANGE POLICY (owner's rule, 2026-11-08; supersedes blending as the default)

1. **Once the app has WATCHED 50 km of driving, it uses only the owner's own data.**
2. **Before that, or whenever own data is missing for any reason** (silent car, no tick-anchored window yet, after a
   charge), it uses **BYD's measured last-50-km consumption (kWh/100 km) on the owner's correct battery size**.
3. **BYD's range estimate is never used**, except on a brand-new car (odometer under 50 km) that has no consumption
   figure at all; it is then shown wide and low-trust and labelled "new car".
Built as `range_engine.policy_estimate` (default `range_mode: "policy"`; "own" and the experimental "combined" blend remain
as options). "Watched" means odometer gains between readings at most 10 min apart (`db.observed_km`): unreported stretches,
stale duplicates, empty snapshots and impossible jumps do not count; the owner's first real day counts 3 km (the 12 km
the car did not report do not). `parts.policy` records which rule produced the number (own / immature / missing-own /
new-car / own-no-byd / none) and the page says why, with "X of 50 km recorded". BYD's displayed range is still shown for
comparison only. **Bug found on real data and fixed:** the first `observed_km` measured gaps on our POLL clock; during the car's silent stretch BYD returned the same old snapshot while our clock ticked, so the unreported 12 km looked like 12 km in 3 minutes (228 km/h, under the 300 km/h cap) and the page said 15 km recorded instead of 3. It now uses the car's own clock (`car_ts`), falling back to poll time only when there is none; regression test replays the real day. Owner's data now: 3 km watched, so the headline is BYD consumption 14.4 x 47.4 kWh usable at 91% =
300 km (218-381, trust 32%). Tests: policy matrix incl. "never BYD range on a car that is not new", exact 50 km threshold,
observed-km edge cases. Caveat from the simulator (6m): own-driving was already about as accurate as anything at 20 km, so the
50 km gate is conservative; revisit once real long drives exist. Open: learn usable capacity (SOH) to firm up rule 2.

## 6o. Learned usable capacity / SOH (built 2026-11-08)

`cloud/capacity.py`, `/api/battery`, Battery tab. usable kWh = BYD kWh/100 km x km / battery % over 30-50 km windows that
start AND end on a battery tick (so the percent difference is an exact integer and independent of whether BYD rounds or
floors its display), kept inside 15-92% battery, non-overlapping, closest to BYD's own 50 km window. Combined with the
configured battery (prior, sd 8%) in log space by inverse variance; per-window error = odometer rounding + tick position +
BYD's 0.1 resolution + window mismatch. Result: usable kWh +/- sd, % of nominal, confidence %, the windows used.
Feeds the BYD-consumption range (rule 2 in 6n) and trip consumption: usable capacity and its uncertainty replace the fixed
setting once windows exist; with none, the setting is used unchanged. Cached per DB state.
**Simulator (known true capacity):** 0.3% mean error after 150 km (3 windows), worst 0.7%; one window sd 0.9%; its stated
uncertainty is conservative (100% coverage at 1 sd). **That is idealised**: BYD's rate equals the true energy over exactly
50 km and the percent is perfectly linear. Real systematic errors (BYD's energy accuracy, percent non-linearity) are NOT
modelled and NOT validated, so a 3% systematic floor caps confidence near 60%; the page says so. The percentage compares
usable energy with the nominal pack; a new pack may deliver less than nominal, so degradation shows only as a TREND over
months. Estimates above 105% or below 80% of nominal carry a warning (check battery_kwh / BYD's figure). Owner's data: only
3 km watched so far, so it is still "not measured yet" (the setting, 47.4 kWh). Race fixed on the way: the server closed
its DB handle after replying, so a fast client could delete the file first on Windows; handle now closes before the reply.
182 tests. Open: recency weighting / trend chart over months; validate against real long drives and, if possible, a
charger-metered kWh session (an independent check of BYD's energy figure).

## 6p. Correction: I had invented a 95% usable fraction (2026-11-08)

The owner told me the pack is 49.92 kWh. I added `usable_fraction: 0.95` (giving 47.4 kWh) as my own assumption, put it in
the owner's `settings.json`, and the Battery tab called it "your setting". It was never the owner's. Removed: the default is now
1.0 (settings.py, server.py, settings.json, settings.example.json), so the app starts from 49.92 kWh and the Battery tab measures
the real usable energy. Every mention of 47.4 kWh / 95% above (6i, 6k, 6l, 6m, 6o and the owner's numbers: 300 km, 308 km) is
superseded: with 49.92 the BYD-consumption estimate at 91% and 14.4 kWh/100 km is about 315 km, not 300. The owner's first
own-driving reading (51.6 kWh from c x k) was already pointing at >= nominal, which a 95% usable share contradicted.
Lesson: do not put an assumption in the owner's config file; show defaults as defaults.

## 6q. Band upside capped by measured evidence (2026-11-08)

Owner: the top of the band (403 km) was above BYD's own number (374), which already reads high on this car. Cause: the band is
symmetric around the centre, so it allowed the road ahead to be 20% BETTER than the last 50 km as readily as 20% worse; that 20%
is my placeholder (86% of the width) and the upside half had no evidence behind it. Fix: in the BYD-consumption estimate the high
end can no longer exceed the range at the best last-50-km consumption ever recorded on this car (`db.best_r50`, floor 6 kWh/100 km
to reject sentinels) plus the battery-size uncertainty. Measured consumption only; BYD's range estimate is still never used. The
downside (highway, A/C, wind) is untouched. Owner's data: 228-403 (trust 31%) became 228-358 (trust 49%), top 16 km below BYD's
displayed 374. Trust follows the width actually shown. 20 new tests (cap never above the uncapped band or below the centre, better
best = higher cap, sentinels ignored, downside identical, policy passes it, never uses BYD range).
**Honest limits:** the car's history is one day (14.0-14.4 kWh/100 km), so "best recorded" is thin evidence now and gets credible
with more driving; the cap can make the top-end coverage lower than the nominal 80%; the downside placeholder (20%) is unchanged
and still dominates the low end; the own-driving (mature) path does not have this cap yet. BYD's lifetime average (15.4) is above
the recent 14.4: a long, mixed trip is more likely to be worse than better than the last 50 km.

## 6r. Charging log built; app text moved into tooltips; journey planner shelved (2026-11-09)

**Owner decisions:** journey planners are out of scope for now (supersedes the journey-mode list in 6j; the glance API and
Android Auto notes stay as future groundwork). The word "likely" is removed from the app. Long explanatory text must not be the
first thing on a page: it lives in (i) tooltips.

**UI:** `web/ui.js` gives every page an (i) button; tap toggles (phones have no hover), hover also shows it on a pointer device,
Escape closes, `aria-expanded` is kept. Tooltips FLOAT (position absolute) so opening one never pushes the key stats down. Main
page now: status, range, band, trust + (i), four tiles, 24 h chart, "Estimate only (i)". The old calculation paragraph, the
footer disclaimer, the trips/battery/charging notes are all tooltips. `?tips=open` shows them all (used for screenshots). Tests pin
this (no visible `.note` paragraph on the main page, `#basis` hidden by default, `.tip` floating, no "likely", no HTML injection).

**Charging log** (`charging.py`, `/api/charging`, Charging tab): a session is a run of readings where the car is NOT moving
(odometer unchanged) and the battery percent RISES; BYD's CHARGING state only helps continue a session. Trailing flat readings
(full, still plugged in) are trimmed. A rise across a silence is reported as "not watched" (gain and energy only). Energy into the
BATTERY = gain % x usable capacity (+/- 0.41% of the pack, about 0.2 kWh; excludes charger losses). Average power uses the moments
the percent ticked up (first to last tick, +/- from the polling interval); AC vs DC is a labelled guess (25 kW). Cost needs the owner's
`tariff_per_kwh` (and optional `charging_loss_pct`, default 0); nothing is assumed. While charging, the poller polls every 5 min and
holds that through a 20 min plateau (battery rise while parked counts as charging evidence even without BYD's label).
**Validation, honestly:** the owner's database has NO charge in it (SOC never rose, state never CHARGING, no positive parked power),
so this is simulator-tested only (energy within +/-1 sd 66% and +/-2 sd 96% of 600 charges: well calibrated; power mean error 1.4%;
a 60 kW DC charge polled every 15 min honestly reads 72 +/- 30 kW). BYD's CHARGING enum value and the sign/units of its instantaneous
power while charging are unverified. The first real session is the first real test.
Also found: an over-clipped simulator (charges past 100%) and a partial-last-step bug made my first accuracy numbers look worse than
the module is; both were test-harness bugs, fixed. 242 tests. Open: charging location/home-vs-public, session delete/export,
a charger-metered kWh check (best independent validation of BYD's energy figure), regen tag.

## 6s. Regen vs charging, and regen on the trip page (2026-11-09)

**Gap found and fixed:** the charging detector judged "stopped" by the odometer, but the odometer is whole km, so many consecutive
DRIVING readings share one value; two regen battery ticks on a long descent could have been read as a charge. A session now also
requires speed zero in both readings (speed, not the gear: a car can sit plugged in with the gear ON). The poller's charging
cadence uses the same rule. A battery rise inside a trip is classed "regen" (net gain on a descent), not "charged"; only a rise
across an unreported gap is "charged".

**Regen on the trip page** (`trips.regen_stats`, `power_series` in `/api/trips/<id>`, "Regen" section): energy recovered,
energy drawn, regen share, a power chart (up = into the battery) and a cross-check against the battery %. Source: BYD's
instantaneous battery power (`gl`), a spot reading about every 30 s, sample-and-hold weighted (cap 90 s), needs 8+ samples.
**Sign:** positive = into the battery. Evidence: the car on and standing still read -0.72 kW (a standing car cannot regenerate,
so negative = drawn). Honest correction: I first said the owner's first trip "confirmed" the sign because the samples netted
0.54 kWh against 0.50 kWh from the battery %; that is WEAK (one -60 kW sample was already 0.50 kWh, so even a flipped sign
passed). The check now only judges when it can fail (allowed gap smaller than the energy): on 45 min simulated trips it accepts a
correct sign 90%+ of the time and catches a flipped one 95%+; on short trips it stays silent. **Accuracy (simulation, 30 s
samples):** drawn energy about 8% error, net about right; recovered regen is inherently rough, about 35% error per trip, because
regen is short bursts that fall between samples; its stated +/- is inflated 1.25x (coverage 93% -> 96% at 2 sd once a burst is
caught); a trip where no sample caught a burst says "none seen" (never "0.00 +/- 0.00"). The regen figures never use location.
**Not available:** the regen SETTING (standard/high) and the drive mode are not in BYD's data; a manual tag is still an option.
266 tests. Open: manual regen-setting tag; verify the sign again on the first real charge (BYD power should read positive).

## 6t. First real charge: BYD's own state and power now drive the charging log (2026-11-09)

**What the first real charge (03:44Z, ~6.3 kW AC) showed:** BYD's `charge_state` reads **CHARGING** (the label I had guessed is
correct; sequence CONNECTED -> NOT_CHARGING -> CHARGING), and `gl` (live battery power) reads a steady **+6.3 kW**: positive
= into the battery (the sign is now confirmed a second way, besides the -0.7 kW draw while standing). Why the Charging tab was
empty: my detector required a 2% battery gain and only 1% had happened; I had built it assuming BYD's label could not be trusted.
Now: two stopped readings saying CHARGING make a session (listed within a minute); a battery-only session still needs 2%.
**Energy** is integrated from BYD's power (trapezoid, plus half a poll interval at each unseen edge); the battery % is the
cross-check ("check") on sessions of 5%+ and the fallback without power. **Simulation** at the cadences the app uses (30 s-5 min):
AC mean error 1.2%, tapering fast charge 3.5% (+2.9% bias from taper curvature), stated uncertainty conservative (99-100% inside 2 sd);
a meter biased by 50% is caught 95%+ of the time. The displayed +/- never goes below 0.1 because BYD's power reading is not yet
verified to that precision. On the owner's live charge: 14 min, 87->90%, 1.6 kWh by power vs 1.5 kWh by battery %, avg 6.4 kW (AC guess).
A 4-minute 3 kW CHARGING blip earlier (03:02Z, 0% gain) is listed too: it is what BYD reported.

**Phone refresh.** The owner's observation is right and is the likely explanation: a web page on a phone does not run with the
screen off; it catches up when woken. Changes: every fetch has a 10 s timeout (a hung request on a changing mobile network could
silently stop the refresh loop forever: real bug), the next refresh is always scheduled (`finally`), failures retry in 5 s, a 60 s
watchdog, immediate refresh on screen-on / pageshow / focus / network-back, a pulse on the status dot at every refresh, and "car data
X ago" now counts every second. Optional **Keep screen on** switch (Screen Wake Lock; HTTPS or localhost only; remembered per phone;
hidden where unsupported). The Charging and Trips pages now refresh themselves while a charge or trip is in progress (20-30 s);
while a charge is in progress the page asks the poller to keep polling (the car is awake charging anyway). The running server had
the old charging code in memory until restarted: restart after Python changes.
283 tests. Open: charger-metered kWh to check BYD's power scale independently; DC fast-charge session to test the taper handling on
real data; brief-plug sessions (a few minutes, 0% gain) may deserve a "brief" label.

## 6aa. Shared server: people sign in with their own BYD login (2026-11-09)

**Owner:** "Not everyone can self host. Some testers will use a shared server to track their own vehicles while accepting some risks", and "some are willing to
test this but they are not developers". This reverses the earlier rule that a BYD password is accepted only on the host's own computer, **for an explicit,
opt-in `--multi-user` mode only**; the default single-owner mode is unchanged. **Built:** `users.py` (registry: opaque keyed-hash ids, hashed session tokens, 90-day
sliding sessions, a per-server salt), `multirun.py` (a supervisor: one poller process per person with `BYD_DATA_DIR=<data>/users/<id>`, restart with backoff, wipe on
delete), `db.use_dir` (a per-thread data folder so each web request works on one person's folder only; `credstore`, `pollstatus`, `settings`, viewer flag, capacity cache
all follow it), server `--multi-user/--max-users/--assume-https` and `BYD_APP_INVITE`, login in shared mode (BYD validates, the login is stored encrypted in the person's
own folder, a session cookie is issued), per-person battery size, `Sign out on this device` vs `Delete my data`, a front-door sign-in page in plain language with a required
"the host can see my login and data" confirmation, `docs/TESTERS.md` for non-developers, `deploy/byd-range-shared.service`. **Protections:** passwords only over HTTPS (or the
machine itself); 5 attempts per BYD account / 20 per address / 120 per server per 10 min (wrong logins can lock a real account); user cap and optional invite code
(checked before BYD is contacted); every data endpoint needs a session; a wrong BYD login creates no user and no folder. **Verified:** 25 new tests (isolation of two people
including the viewer flag and battery size, caps, invite, rate limits, sign-out vs forget, expiry, supervisor, ids), the full suite (430) on Windows and on real Linux,
and `deploy/e2e/e2e_multiuser.sh` (46 checks, real server and real per-person poller processes, fake pyBYD, restart persistence, delete wipes the folder), and the sign-in
page driven in headless Edge as a first-time visitor. **Not verified / open:** real non-developer testers; more than a few users; days of running; a real BYD account through
shared mode; an independent security review of the isolation; iPhone; a guest/view-only role is not needed in shared mode but still does not exist for single-owner mode.
**Known limits:** the host can read everyone's data (said on the page); a tunnel provider ends TLS and so sees passwords; spamming an account name can block its sign-in for 10 minutes.

## 6ac. Bug: the low end of the band collapsed to a few km (2026-11-09)

**Report (owner, testing on the Azure server):** "the lower range was too low a couple of times". **Replay of the stored drive** (the same one as 6z): at 77% the band was
9-446 km around 227 km; at 84-86% it was 62-64 km around 290. **Cause 1 (the main one):** a slow AC charge raises the battery percent about 1% per reading, so it never
reaches the 2% rise that `split_runs` treats as a charge. A drive before and a drive after the charge were one run, and the chunk spanning the charge read "53 km for 3%"
(17.7 km per percent, normal is about 3.5). The driving-variability figure (`cv`, the spread of km per percent between chunks) came out at 1.03, shrank to 0.75, and made the band
half-width 0.96. The same splitter feeds the learned battery capacity, which was exposed too. **Fixes:** (1) a rise in battery percent while the odometer has not moved now starts a new
run (charging, however slow); (2) a jump of more than 4 km between two readings (an unwatched stretch) cannot sit inside a chunk; (3) from 4 chunks the variability is measured
by median absolute deviation, so one odd chunk (a hill, a long stop with the A/C on, a glitch) cannot dominate; (4) the variability is capped at 0.45, so whatever the data
the band's low end stays above about 40% of the estimate. **Replay after:** 77% is 145-309 around 227; 80% is 192-303; 91% is 311-487; variability 0.17-0.28 from up to 8
chunks. 9 new tests. **Open:** the band is symmetric around the estimate by construction; a lower bound from the worst measured consumption (highway, cold) would be tighter and
more honest on the low side.

## 6ab. Independent security review of shared mode, and what it changed (2026-11-09)

A separate reviewer (a fresh agent given only the code and the threat model, read-only) traced the new mode adversarially. **Found no** cross-user leak, no anonymous data route, no
path traversal, no cache or thread-local bleed, no session or CSRF flaw, no front-end injection. **Real findings, all fixed with tests (8 new):** (1) *membership oracle*: with an
invite code set, a 403 "invite" vs 401/409 told a stranger whether an account name was a member; the invite is now required of everyone and checked first, so the reply is the same
(2) *unauthenticated lock-out and password relay*: junk POSTs consumed a victim's sign-in budget and, for members, forwarded wrong passwords to BYD; now only logins that reached BYD and
were rejected count against an account and the server, an address is charged for every attempt, and the invite check comes first (3) *a non-ASCII invite code or token crashed the handler*
(`hmac.compare_digest` raises on non-ASCII text): all comparisons now go through `safe_eq`. **Suspicions also closed:** the user id now hashes the exact account name (case folding was a
guess about BYD that could have mapped two people to one folder); the throttle table drops its oldest entries instead of clearing everyone's counters; a wipe that fails keeps the person
"removing" and is retried; the server says to start it with `run.py`. **Left as documented limits:** a member can be blocked for 10 minutes by someone guessing their password through the
server; without an invite code the server relays login guesses to BYD within the limits; the proxy must append to `X-Forwarded-For`.

## 6z. Bug: range far above anything the car has done (2026-11-09)

**Report (owner):** during a drive the app showed ~400 km near 80% battery. **Replay of the stored drive** (08:25-11:09, 260 readings, current code):
399 km at 91% at the start (a 35 km window with 8% drop = 11.4 kWh/100 km, while BYD's own measured last-50-km figure has never been below 13.1), then
~250 km at 80%, but with a band of 22-477 km. **Cause found:** the "upside cap" by the best measured consumption existed only on the BYD-consumption
path; once own data took over (after 50 km) nothing bounded it. Likely reason the own window was optimistic: whole-percent battery data over a short window, and
the displayed percentage appearing to fall more slowly near the top (later windows implied ~43 kWh usable, this one ~59). **Fix:** `apply_ceiling` in
range_engine: on the own path the range cannot exceed remaining energy / best BYD-measured consumption, and the band top may exceed it only by the battery-size
uncertainty. Measured data only; BYD's range estimate is still never used; a low estimate is never raised. Replay after: 348 km at 91%, band tops 353-357.
**Not fixed, open:** the band's LOW end is still often absurd (9-65 km) when the window grows; and whether the owner saw the headline number or the band top
is unconfirmed (the replay never showed a 400 headline near 80%). 6 new tests.

## 6y. A shared community server (2026-11-09)

**Owner:** relatives and friends will use the owner's server, and the project will be shared in a community that adds PRs, features and tests. Consequences found:
(1) the "sign in only on the computer that runs the app" message assumed a single self-hoster; it now explains why (a password typed over the network passes through
the host) and what a guest can do. (2) **The build is single-tenant**: one BYD account, one poller, one database, one access token. Friends who get the link and token
see the OWNER's car, and, since sign-out and battery size were opened to any holder of the cookie, can also sign the owner out or change the battery size. A real shared
server needs: per-user accounts, per-user data segregation (HOSTING_SECURITY.md options A/B), a view-only guest token, and a decision on where a friend's BYD
password is typed (a native client that hands over tokens only, so the host never sees it). **Not built; open decision recorded for the owner.**

## 6x. Pakistan defaults and sign-out from the phone (2026-11-09)

**Owner:** pre-fill Pakistan and its server URL; "I need to be able to sign out". The sign-in form now defaults to Pakistan (country PK, region sg locked
to the confirmed server, address `https://dilinkappoversea-sg.byd.auto` shown); "Other country..." reveals the code box and the region choice. Sign-out was
disabled outside the server computer: it is now allowed from any device holding the login cookie, still behind the CSRF header, Origin check and JSON
type. Reasoning: signing out only stops the app reading the car and deletes a stored login, while signing in (a password) stays local-only. Trade-off: anyone with
the access token can sign the app out, but they could already read everything. Tests cover both directions.

## 6w. secrets.env removed (2026-11-09)

**Owner:** "I do not want to see secrets.env on the server info or have that file anymore." Done: the owner's login was migrated into the encrypted
store (read back and compared field by field before anything was deleted), then `secrets.env` (and an empty stray copy in the project root) and
`secrets.env.example` were deleted. The code no longer has any plain-text credentials path (`byd_config` reads only `credstore`), the Account page
no longer mentions the file, `/api/logout` no longer reports where else a login might live, the READMEs, `find_region.py` and `probe_login.py` point to
the Account page instead, and a test proves the file is ignored. The older sections below that mention secrets.env describe history. `.gitignore`
still ignores `*.env`. **Consequence:** the only way to sign in is the Account page on the machine that runs the app (or the SSH tunnel).

## 6v. Battery size is the owner's choice; installable web app (2026-11-09)

**Owner:** "In our community this is not the size, my size is the standard here in Pakistan. Also can this be a PWA?" The default of 60.48 kWh
was wrong for the community. pyBYD's `Vehicle` has no battery or variant field, so the size cannot be read from the car: the owner chooses.
**Built:** default is now 49.92 kWh (Standard Range) but never silent: `battery_set` is false until a valid size is saved, and the Now page
shows "Battery size not set: assuming 49.92 kWh" with a link. The Account page has a selector (Standard 49.92, Extended 60.48, Other 20-150);
in the Asia/Karachi time zone Standard is preselected and labelled as owner-reported, otherwise the owner must choose. `POST /api/settings`
(login cookie + CSRF header + Origin check, validated, saved atomically to `<data dir>/settings.json`, other keys kept; takes effect at
once). Settings moved from the code folder to the data directory (an old file next to the code is still read). **PWA:** manifest, four generated icons
(`tools/make_icons.py`, standard library only; one maskable), `sw.js` (versioned shell cache, **never** caches `/api/*`, cache name = hash
of the shell files, injected by the server), `pwa.js` (registers only in a secure context; Install button on `beforeinstallprompt`),
CSP gains `manifest-src 'self'; worker-src 'self'`. Token mode: the browser fetches the manifest, icons and worker WITHOUT the cookie, so only
those plus `app.css` are public (no data, no secret; same files as the open repository); everything else stays 401. An unauthorised `/` now shows a
token form (needed because an iPhone home-screen app has its own cookie jar), served with `form-action 'self'` only for that page.
The login cookie now lasts a year. **Bug found while testing:** my first CSP edit dropped the quotes around `'none'`/`'self'` in `form-action`
(a keyword without quotes is a host name); caught by the new tests. **Verified:** new tests (settings, endpoint, CSRF, token mode, public vs private
assets, cache never covers the API, every cached file exists), a headless Edge run (selector, banner, service worker registers on localhost).
**Phone check (owner, Chrome on Android): installed and showing live numbers.** Not yet checked: iPhone; HTTPS is required for installability (Tailscale serve gives it); the service worker's offline
behaviour was reviewed, not exercised on a device.

## 6u. Sign-in in the app; the deployment target is Linux (2026-11-09)

**Owner:** the server will run on Linux, not Windows. Sign-in must never send credentials to a server of ours, must warn against the
primary login, and must persist. Built: `credstore.py` (AES-256-GCM, key outside the data folder: `$BYD_APP_KEY_FILE`, systemd credential
`byd_credential_key`, or `~/.config/byd-range/credential.key`; DPAPI on Windows for development; no plain-text store), `byd_config`
(stored sign-in first, `secrets.env` still works, a host that is not `dilinkappoversea-<xx>.byd.auto` is never sent a password),
`pollstatus.py`, the poller's session logic (waits for a sign-in instead of exiting; notices a changed or removed sign-in within
seconds; **never retries a rejected login**, only network errors), `/api/account`, `/api/login`, `/api/logout`, the Account page
(boxed always-visible warning, required sub-account confirmation, masked user, poller state, sign out), `BYD_DATA_DIR`, a hardened
systemd unit and `deploy/README.md`. Sign-in is local-only (loopback address + localhost Host + no proxy headers; SSH tunnel works,
ngrok/Cloudflare/nginx/Tailscale do not), CSRF-protected, rate-limited (5 per 10 min), never echoes the password.
**Why not in the phone's browser:** a web page cannot log in to BYD (CORS + crypto only in pyBYD): remote sign-in needs a native client.
**Verification:** 370+ Windows tests; the Linux-relevant tests on real Ubuntu 22.04 (Python 3.10, cryptography 3.4.8); a 39-check
end-to-end run of the real run.py with a fake pyBYD (wrong password rejected and nothing stored; right password stored encrypted, key
mode 600 outside data; poller picks it up; full restart without signing in again; wrong or world-readable key reported; sign-out;
password absent from log and files) kept in `deploy/e2e/`; the hash-locked requirements checked to download with matching hashes for
Python 3.12 on Linux x86-64 and ARM64 (19 of 19). **Not verified:** a real BYD sign-in through this page (no credentials used); the
POSIX permission tests ran only under WSL; the systemd unit passed `systemd-analyze verify` apart from the install path not existing
there, but was not started as a real service.
**Bugs found by running on Linux and fixed:** (1) `_last_energy_fetch = 0.0` compared with `time.monotonic()`, which counts from boot on
Linux, so for the first 5 minutes after a server boots the energy reading was silently skipped (same pattern in `server._last_touch`);
now `-inf`, with regression tests that simulate a freshly booted machine and a proof they fail on the old code; (2) a test assumed
`str(IntEnum)` is a number, true only on Python 3.11+ (the app always used `.name`); (3) the server replied to a rejected POST without
reading its body, so the OS could reset the connection and an SSH-tunnelled client could miss the explanation: bodies are now read first.
Open: a native client for remote sign-in and for hosted multi-user token hand-off; choose the battery size on the sign-in page (the
default of 60.48 kWh is wrong for Standard Range cars); several BYD accounts; BYD's real session lifetime is still unknown (the poller
re-signs-in by itself when it expires).

## 6d. Requirement: range shown as a band + trust %, narrowing with usage (recorded 2026-11-08)

Show e.g. "Range 262 km (235-290), trust 75%" instead of a single number; the band narrows as data accrues.

- **Capacity cancels out of range:** `range_km = soc_now / SOC_used_in_window x km_in_window`. The range band
  does not depend on SOH/usable kWh (only kWh/100 km does).
- **Measurement error shrinks with window size:** for contiguous segments the SOC drops telescope to
  `soc_start - soc_end`, so whole-percent rounding contributes only ~+/-1% total (not per segment). Relative
  error ~ 1 / total SOC drop (5% drop -> ~20%, 30% drop -> ~3%).
- **Driving variability is a floor:** spread of per-segment consumption (speed, temperature, A/C) sets the
  minimum band width; more data estimates it better but cannot remove it.
- **Low-SOC widening:** rounding on today's SOC is a larger fraction at low SOC (10% -> ~+/-5%).
- Band = 80% prediction interval; trust % derived from relative band width, window km, segment count.
- Below a minimum data threshold show "not enough data yet", never a fabricated number.
- Tasks: [ ] interval maths in `range_engine.py`; [ ] tests that the band narrows as simulated drives are
  added and respects the variability floor; [ ] UI wording that says "estimate".

## 6c. Requirement: battery SOH estimate with confidence (recorded 2026-11-08)

Estimate battery state of health (usable capacity now vs nominal, e.g. 60.48 kWh) and show it with a
**confidence %**. It also feeds the range maths (learned usable capacity replaces the fixed constant; the
Lingbox car delivered ~93% of nominal, so the constant is not trustworthy).

**Core difficulty:** SOC and odometer alone cannot reveal capacity. Consumption is computed as
`ΔSOC x capacity / distance`, so capacity cancels out of any drive-only estimate. We need an
**independent energy measurement**: kWh per ΔSOC.

**Best candidate (user confirmed 2026-11-08: BYD app shows last-50 km kWh consumption):**
`recent_50km_energy` (and `recent_50km_energy_ev` + unit) from realtime. It is measured energy,
independent of SOC. With our SOC-derived `km_per_pct = k` and BYD's `c` kWh/100 km over the same recent
driving: `usable_kWh ~= c x k`, `SOH ~= usable / nominal`. Average many windows; confidence from the
estimator spread. Still to verify from real output: unit and resolution (decimals?), whether it is truly a
rolling 50 km window, how often it updates, whether it includes parked/aux drain, and how it behaves right
after a charge. Poller currently stores it as raw text (`recent_50km`); parse into number + unit.

**Field semantics (from pyBYD 0.0.79 source docs, not yet seen on a real car):** `recent_50km_energy` is a
*rate* (kWh/100 km, `"--"` when unavailable), not absolute kWh. Absolute kWh over a rolling last 50 km is
`get_energy_consumption().nearest_energy_consumption.ev_consumption`, which also carries
`avg_ev_consumption` and `air_distribution` (% of the 50 km with HVAC on: explains range loss from A/C).
**DONE 2026-11-08:** poller now logs `r50_rate`, `r50_kwh`, `r50_air_pct` as numeric columns every poll (energy
endpoint rate-limited to once per 5 min; failures non-fatal); DB auto-migrates older files; 7 tests added.
The SOH estimator itself is not built yet.

Other candidate energy sources (all **unverified** until `probe_login.py` shows real fields):
1. BYD cumulative energy fields (`total_energy`, `total_consumption`, `get_energy_consumption`
   `cumulative_energy_consumption`): kWh used over a SOC drop gives `capacity = kWh / (ΔSOC/100)`.
2. Metered charging: kWh delivered (wallbox/smart-meter) over a SOC gain, corrected for charge efficiency.
   Needs an external source, so optional and self-host only.
3. Cross-check only: BYD `endurance_mileage` implied range per SOC%.

**Confidence score (0-100%)** should reflect: number of qualifying samples, SOC span covered (wider is better;
>=20-30% per sample), spread/variance of per-sample estimates, agreement between independent sources,
temperature (cold depresses usable capacity), and recency. Show a range, e.g. "SOH 93% +/- 3 (confidence 70%)",
and "insufficient data" below a threshold rather than a false number.

**Caveats to state in the UI/docs:** BMS-reported SOC is not true SOC and is non-linear near the ends (use the
20-90% band); this is an estimate, not a diagnostic, and does not replace BYD's own battery check.

Tasks: [ ] confirm energy fields from real output; [ ] sample-quality filters; [ ] capacity estimator + confidence
score with tests; [ ] persist capacity history per vehicle (needs `user_id` + `vin` columns, see Phase 2b).

## 6a. Design rule: READ-ONLY (decided 2026-11-08)

This project **never sends control commands** (lock/unlock, climate, windows, trunk, charging start/stop,
schedules, rename). Any control is done in BYD's own app or the car. We only read: realtime, GPS, charging
status, energy, HVAC status.

- Enforce in code, not just policy: wrap pyBYD in a thin **read-only facade** that exposes only `get_*`
  calls; the rest of the codebase must not import `BydClient` directly.
- CI check: fail the build if any control method (`lock`, `unlock`, `start_*`, `stop_*`, `open_*`, `set_*`,
  `save_*`, `toggle_*`, `schedule_*`, `rename_*`, `find_car`, `flash_lights`) is referenced outside tests.
- Limit of this guarantee: BYD tokens are not scoped read-only. A stolen token could still control the car
  via BYD's API, so token custody (encryption, sub-accounts, short retention) still matters.
- Shared/multi-tenant servers therefore hold no more risk than needed, and consent copy can truthfully say
  "this software only reads your data".

## 6b. Parked: solar-aware charge current (decide later)

_Conflicts with the read-only rule: would need to live outside this project (e.g. evcc / Home Assistant) or be
a separately-reviewed opt-in add-on._

- Goal: charge at 32 A when sunny, less otherwise, to maximise solar use.
- The head unit has a charge-current setting (8 / 16 / 32 A). pyBYD 0.0.79 does **not** expose it; its only
  charge controls are start/stop, schedule and smart-charge toggle. Not exhaustively verified (e.g.
  `vehicle_settings.py` not read line by line).
- Options when revisited: (a) wallbox-side current control (OCPP/Modbus/API, or evcc/Home Assistant),
  (b) find whether BYD's cloud has a charge-current endpoint (e.g. via traffic capture), (c) head-unit route
  (parked, see section 2).
- Must be an optional, **self-host-only** module; never in a shared server build (wallbox/car control).

## 7. Repository layout

| Path | What | Committed? |
|---|---|---|
| `cloud/` | Phase-1 Python (reference + capture) | yes (minus `secrets.env`, `.venv`, `data/`) |
| `docs/` | Plan, pyBYD audit, hosting/security, head-unit notes | yes |
| `adb_unlock/` | Unlock-code generator (parked approach) | yes (source only) |
| `probe/`, `recon/`, `scripts/` | Head-unit probe/recon tooling (parked) | yes |
| `decompiled/` | Decompiled third-party APK | **no** (gitignored — derived third-party work) |
| `SecretApk/` | Third-party APK binary | **no** (gitignored) |
| `build/` | APK build artifacts | **no** (gitignored) |
