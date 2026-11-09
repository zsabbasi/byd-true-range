# Hosting & security analysis

How to offer this to other users without taking on their BYD passwords, and what is / isn't possible.

## Two facts that shape everything

1. **The password is needed only at login.** After login, pyBYD uses tokens (`signToken`, `encryToken`,
   `userId`) for all polling; the MQTT credential derives from `signToken`, not the password. So a server
   can run on **tokens alone** — the password never has to reach it.
2. **A polling server cannot be blind to the data.** Whoever calls BYD and decrypts the responses holds
   the plaintext (location, SOC) in memory. Encryption-at-rest protects the database from a breach; it
   cannot blind the live process. There is no way to have a server do the polling yet not see the data.

Corollary: there is **no token-only refresh** in pyBYD. When tokens expire, a new `login()` (password)
is required. Client-side TTL defaults to 12 h; the real server-side token lifetime is unknown and must be
measured.

## Why "server does MQTT only, data encrypted" doesn't work as hoped

- MQTT needs the tokens (credential derivation + message decryption with `MD5(encryToken)`). Either the
  server holds tokens (and can read everything) or it holds ciphertext it can't use offline.
- MQTT is a **passive push** channel — it catches what the car volunteers (driving, charging). Forcing an
  hourly **GPS** sample needs HTTP trigger calls (`getGpsInfo`), which MQTT doesn't provide.
- So an MQTT-only server can't deliver reliable hourly history, which was the reason to have a server.

## Architecture options

| | Password on your server | Data readable by your server | 24/7 background history | Your liability |
|---|---|---|---|---|
| **A. Token handoff** | Never | Yes (encrypt at rest) | Yes, until tokens expire → phone re-hands | Medium (data processor) |
| **B. Zero-knowledge sync** | Never | Never | No (only when phone awake) | Low |
| **C. Self-hosted poller** | Not on your infra | Not on your infra | Yes | Lowest (software vendor) |

- **A — Token handoff.** Phone holds password, logs in locally, sends only tokens to the server; server
  polls + maintains MQTT and stores history. Honest user promise: "your BYD password never leaves your
  phone." You still custody tokens (car-control-capable) + location → encrypt per-user at rest, minimise
  retention, allow delete.
- **B — Zero-knowledge sync.** Phone does all BYD comms and encrypts each reading with a key only it holds;
  server stores opaque blobs and syncs across the user's devices. Server can read nothing. Cost: no
  server-side history; depends on phone background limits.
- **C — Self-hosted poller.** Ship the `cloud/` poller as software; each user runs it on their own
  always-on box (PC / Pi / their own VPS); the phone app is a UI over the user's own instance. Tokens and
  data never touch your infrastructure. Matches the Lingbox self-host ethos.

## Recommendation

- If reliable background history is the point → **A**, with a **dedicated BYD sub-account per user**
  (revocable, not their main account), per-user encryption at rest, minimal retention, user-delete.
- For the **lowest liability** → **C**: you host nothing sensitive and are a software vendor, not a data
  processor.
- **B** is the fallback if neither fits.

## Hardening checklist (A/B/C)

- [ ] Dedicated/authorized BYD sub-account per user (avoids kicking the phone app; revocable).
- [ ] Measure real token lifetime before committing to A.
- [ ] Per-user encryption at rest for tokens + data (A/B).
- [ ] Minimal retention + user-initiated delete.
- [ ] Never log DEBUG in production (VIN/userId/GPS aren't redacted by pyBYD; password/tokens are).
- [ ] `base_url` set explicitly in code; never inherited from shell env.
- [ ] Consent copy covering location + car-control token custody (A/B).

## Legal note

Under A and B you store real-time **location** and tokens that can **unlock/control a car** — sensitive,
safety-relevant PII with consent and breach-notification duties (GDPR-type regimes). C avoids this by
keeping all custody with the user.

## Implemented: local sign-in (2026-11-09)

For the self-hosted deployment (Option C, now on Linux) the BYD password is typed on the computer that runs the app and goes only to
BYD. There is **no server of ours** in that path. What was built, and its limits:

* **Why not in the phone's browser:** a web page cannot sign in to BYD (cross-origin calls are refused; the login's white-box crypto
  exists only in pyBYD). Remote or phone-side sign-in therefore needs a native client; it is not built. A *hosted* multi-user service
  (Options A/B) would need that native client to do the sign-in and hand over only tokens: the password could still never reach the
  host, but this code does not implement that hand-off.
* **Local only:** the sign-in endpoints accept a request only if its address is loopback, its Host header is localhost, and it
  carries none of the headers a tunnel or proxy adds. SSH port-forwarding to localhost qualifies; ngrok, Cloudflare, nginx and
  Tailscale do not (which is the point). A proxy deliberately configured to strip those headers and fake the Host would defeat this
  check: that is unsupported and documented as such. Even a valid token does not unlock remote sign-in.
* **Login CSRF:** a custom header, a JSON content type and an origin check, so a web page in the owner's browser cannot sign the app in to an attacker's account.
* **5 attempts per 10 minutes**, and a rejected stored login is never retried by the poller.
* **Encrypted at rest:** AES-256-GCM with the key outside the data folder (systemd `LoadCredential=` from a root-owned file, or a 0600
  file in the config folder); DPAPI on Windows. Protects a copied or backed-up data folder; not root, and not a compromise of the app.
* **A required confirmation** that the account is a dedicated sub-account, enforced by the server as well as the page. The warning
  about the primary login is always visible, not a tooltip.
* **Not covered:** BYD tokens are not scoped read-only, so a stolen session could still control the car through BYD's API; that is why
  a dedicated, revocable sub-account is the recommendation.


## Implemented: shared server mode (2026-11-09)

`run.py --multi-user` is a minimal version of hosting option B for a small group of testers: each person signs in with their own BYD account **on the host's page**
(over HTTPS), and gets their own folder, encrypted login, database, settings and poller process. It deliberately accepts the trade-off the earlier sections tried to
avoid: the host's machine sees each password in memory and holds each login encrypted at rest, so **the host must be trusted by the testers**, and the page says so and
requires confirmation. A native client that hands over tokens only (so the host never sees a password) is still not built. See `docs/SAFETY.md` and `docs/PROJECT_PLAN.md`
section 6aa for the protections, the tests and the known limits.
