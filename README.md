# BYD Atto 3: true range

Goal: a "true range" tool like the Lingbox one in `D:\Research\ev-range-headunit`, showing real range from
recent driving plus a driving/GPS history BYD doesn't keep.

## Approach: BYD cloud API (current direction)
Instead of hooking the head unit, we read vehicle data from **BYD's cloud** (the backend the BYD phone app
uses) via the audited open-source `pyBYD` library — SOC, odometer, speed, BYD's own range, GPS, charging.
Plan: **Phase 1** validate + capture + compute range in Python (`cloud\`); **Phase 2** a phone app reusing
that logic (adaptive polling: fast while driving, hourly while parked for history).

- `cloud\` — Phase 1 Python reference + data capture. See `cloud\README.md`.
- `docs\PYBYD_AUDIT.md` — supply-chain audit of pyBYD (verdict: clean, talks only to BYD).

### Next action
`cd cloud`, follow `cloud\README.md`: create the venv with the hash-locked requirements, run `run.py`, open the app on that computer and sign in on the Account page
(no credentials file exists), then the app starts recording.

> Note on "realtime": cloud polling wakes the car's TBOX (~5-15 s/poll) and draws the 12V battery, so live
> data refreshes every ~30 s while driving, not every 2 s like the CAN-bus Lingbox project.

---
