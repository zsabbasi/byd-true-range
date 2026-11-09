# Safety and risk

## What this software can and cannot do

- It **reads** your car's data from BYD's cloud and displays an estimate. It never sends a command to the car: there is no control code in the repository, and the sign-in is made without a control PIN.
- It sends your BYD login **only to BYD's own regional host** (`dilinkappoversea-<xx>.byd.auto`, over HTTPS, by pyBYD). A host of any other form is refused before anything is sent. It has no analytics and no other network calls; the one optional exception is map images from OpenStreetMap (`--no-map-tiles` removes it, `--no-location` removes routes entirely).
- It is **not** a fuel gauge or a safety function. The estimate comes from whole-percent battery data, whole-km odometer readings and your recent driving, and is shown as a band with a trust figure. Keep your own reserve (20 to 30 %) and do not use it as the only basis for a range decision.

## The genuinely risky parts

### 1. Your BYD account
- **Use a dedicated sub-account, never your primary login.** BYD's session tokens are not scoped read-only: a stolen session could control the car through BYD's API. A sub-account can be revoked without touching your main account. BYD may also sign your phone app out when the same account logs in somewhere else.
- **Repeated wrong logins can lock a BYD account.** The poller never retries a rejected login (only network errors), the sign-in form allows 5 attempts per 10 minutes, and `find_region.py` tries each host once. Do not loop them.
- **Where the password goes:** typed on the computer that runs the app, sent to BYD, and stored AES-256-GCM encrypted with a key kept outside the data folder (a systemd credential or a `0600` file; Windows DPAPI for development). It is never logged and no page or API returns it. This protects a copied or backed-up data folder. It does **not** protect against root on the server, or against someone who has taken over the app itself (it has to decrypt the login to use it).
- **Sign-in is accepted only locally:** loopback address, a localhost Host header and no proxy headers. An SSH tunnel to localhost qualifies; ngrok, Cloudflare, nginx and Tailscale do not, on purpose (they can see what passes through). A reverse proxy deliberately configured to forge those headers would defeat the check; that is unsupported.

### 2. Polling can wake the car
Each read can wake the car's telematics unit and draws on the 12 V battery. The poller is adaptive (about 30 s while driving, 15 minutes while parked, faster only while the page is open, 5 minutes while charging), writes nothing when BYD returns an empty or stale snapshot, and never wakes the car on its own to browse history. It is still a cloud poll, not a CAN bus: do not leave the car for weeks on a weak 12 V battery with a fast poll running.

### 3. Who can see your data
- The server listens on `127.0.0.1` only. To reach it from a phone you need a tunnel or Tailscale. A tunnel provider such as ngrok **can see the traffic**; the access token is then the only protection. Prefer Tailscale.
- **The app is single-account.** Everyone you give the link and token to sees **your** car: range, trips, locations and charging. They can also sign the app out or change the battery size, because those need only the login cookie. Do not share the token with people you would not give your phone to. A view-only guest token and per-user data are not built (see `docs/HOSTING_SECURITY.md`).
- Add `--no-location` to keep your routes off the page and out of any tunnel.
- Never port-forward the app to the internet.

## What is verified, and what is not

Verified: the range, trip, charging and capacity logic on recorded data and in simulation; the first real AC charge (BYD's charge state and power sign); a real sign-in through the Account page with the Pakistan account; the encrypted store, restart persistence and sign-out on real Linux (end-to-end test with a fake pyBYD); installing as an app from Chrome on Android; the hash-locked dependencies for Linux x86-64 and ARM64.

**Not verified yet:**
- A real DC fast charge (the energy figure for it is unchecked).
- BYD's real session lifetime (the poller signs itself back in; this has not been watched over days).
- Whether BYD rounds or floors the displayed battery percentage.
- The learned battery capacity against anything except simulation, and any state-of-health trend over months.
- The systemd unit as a real running service (it passed `systemd-analyze verify` only).
- iPhone, other BYD regions, other BYD models, other charger types.
- The audit of pyBYD 0.0.79 against the audited `main` (a diff has not been done yet).

## Rules of thumb

1. Use a sub-account. Sign in on the computer itself, not through a tunnel.
2. Treat the band, not the single number, as the answer, and keep a reserve.
3. Never publish your history database, access token, screenshots or logs: they contain where you drive and where you charge. Remove locations before sharing anything.
4. If BYD rejects the stored login the app stops and waits for you. Fix the cause (password, region) before signing in again.
5. If anything looks wrong, sign out on the Account page (or delete the stored login) and stop the service; nothing else needs undoing, because nothing was ever written to the car.
6. Report security problems privately to the maintainer (GitHub's private vulnerability reporting), not in a public issue.
