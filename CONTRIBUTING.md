# Contributing

Thanks for helping. This project reads a BYD car's data from BYD's cloud and shows an honest range, trips, charging and battery capacity.
Please read `docs/PROJECT_PLAN.md` (decisions and why) first.

## Ground rules (a PR that breaks one of these will not be merged)
* **Read-only.** Never send a control command to the car.
* **Credentials only go to BYD.** No telemetry, no analytics, no third-party requests (the one exception, map tiles, is switchable). The sign-in
  stays local-only; see `docs/HOSTING_SECURITY.md`.
* **Dependencies are hash-locked** (`cloud/requirements.txt`). Do not add one casually; explain why in the PR.
* **No `innerHTML`** in the web app (text goes in with `textContent`); the page runs under a strict CSP.
* **Honesty over looks.** Say what a number is based on, and what is not verified. Do not invent defaults (the battery size is asked, not assumed).

## Running the tests
```
cd cloud
python -m unittest discover -s tests          # about 400 tests, no BYD account or network needed
bash ../deploy/e2e/e2e_linux.sh               # Linux end-to-end with a fake pyBYD
```
Add a test with every change. Tests must never read your real data or credentials (they use temporary folders).

## Testing with your own car
Run your own copy (`deploy/README.md`) with a dedicated BYD sub-account. Reports of regions, models and charger behaviour that worked or did not are very
welcome as issues. Never paste your password, access token, VIN or exact location.

## Licence
Apache License 2.0 (see `LICENSE`). By contributing you agree your contribution is licensed under it. Security problems: report them privately to the
maintainer (GitHub's private vulnerability reporting), not in a public issue.
