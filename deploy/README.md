# Running the BYD range app on Linux

The app is a poller (reads your car from BYD's cloud) plus a small web app, started together by `run.py`. This is the
recommended way to run it: a small always-on Linux box (a Raspberry Pi, a home server or a cheap VPS).

Needs **Python 3.11 or newer** (pyBYD requires it; use 3.12) and a 64-bit Linux with glibc 2.28 or newer (Ubuntu 20.04+,
Debian 10+, Raspberry Pi OS Bullseye+). The hash-locked dependencies were checked to exist, with matching hashes, for Python 3.12
on Linux x86-64 **and** ARM64 (a 64-bit Raspberry Pi). The test suite and the end-to-end test pass on Ubuntu 22.04.

## 1. Install

```bash
sudo useradd --system --home /var/lib/byd-range --shell /usr/sbin/nologin byd-range
sudo mkdir -p /opt/byd-range && sudo chown "$USER" /opt/byd-range
git clone <your copy of this repository> /opt/byd-range      # or copy the folder
cd /opt/byd-range/cloud
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt   # the hash-locked dependencies: never a plain `pip install pybyd`
.venv/bin/python -m unittest discover -s tests               # optional: the test suite should say OK
sudo chown -R root:root /opt/byd-range                       # the service user can read the code but not change it
```

Your battery size: until you choose one, the app assumes **49.92 kWh (Standard Range, the usual one in Pakistan)** and says so in
a banner. Choose yours on the **Account** page (Standard 49.92, Extended 60.48, or any other size). It is saved in
`$BYD_DATA_DIR/settings.json`, so it survives updates of the code folder and needs no file editing.

## 2. The key that encrypts your BYD sign-in

The sign-in is stored with AES-256-GCM. The key must live **outside** the data folder, so a copy or backup of the data never
contains it. Create it once, readable by root only:

```bash
sudo mkdir -p /etc/byd-range
head -c 32 /dev/urandom | xxd -p -c 64 | sudo tee /etc/byd-range/credential.key >/dev/null
sudo chmod 600 /etc/byd-range/credential.key
```

The systemd unit hands this to the service as a private credential (`LoadCredential=`). Do not back this file up together with
`/var/lib/byd-range`. If it is lost, nothing is harmed: you just sign in again.

What this protects: a copied or backed-up data folder, a database dump. What it cannot protect against: root, or someone who has
taken over the app itself (it has to decrypt the sign-in to use it).

## 3. Run it as a service

```bash
sudo cp /opt/byd-range/deploy/byd-range.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now byd-range
sudo journalctl -u byd-range -f          # "Not signed in ... Waiting" is expected until you sign in (next step)
```

`--require-token` is on in the unit: every request needs a secret token. Read it with:

```bash
sudo cat /var/lib/byd-range/access_token.txt
```

Open the app as `http://<address>:8765/?token=<that token>` (the app listens on 127.0.0.1 only; see step 5 to reach it).

## 4. Sign in to BYD (once)

Your BYD password is only ever accepted on the server's own localhost, never through a tunnel, a proxy or the network.
On a server with no screen, use an SSH tunnel from your own computer:

```bash
ssh -L 8765:localhost:8765 you@your-server
```

Then, on your own computer, open `http://localhost:8765/?token=<token>` and go to **Account** (bottom of the page).

* **Use a dedicated BYD sub-account, never your primary login.** Share your car with a second BYD account in the BYD app and
  sign in with that one. The page makes you confirm this before it lets you sign in.
* Pick your region. BYD runs one server per region and an account exists only on its own (Pakistan is confirmed as
  Singapore / Asia-Pacific). Wrong region with correct details looks like "Wrong email or password".
* The app signs itself back in when BYD's session expires, so you do not sign in again and again. **If BYD ever rejects the
  stored login, the app stops and waits for you** instead of retrying (repeated wrong logins can lock an account).
* **Sign out** on the same page deletes the stored login. Unlike signing in, it also works from your phone (it only stops the app reading your car).

## 5. Reaching the app from your phone

The app listens on `127.0.0.1` only. Pick one way to reach it:

* **Tailscale (recommended)**: install it on the server and the phone, then `tailscale serve --bg 8765`. Private, encrypted, not on
  the internet. You still sign in over the SSH tunnel above (the app refuses sign-in from a tunnel or proxy).
* **A tunnel such as ngrok or Cloudflare**: works for viewing, but the provider can see the traffic and the token is then the only
  protection. Prefer Tailscale. Add `--no-location` to the `ExecStart` line to keep your routes off the page entirely.
* **Do not** port-forward 8765 to the internet.

If you put a reverse proxy (nginx, Caddy) in front, it must add `X-Forwarded-For` (the defaults do). That is how the app knows
a request did not come from the server itself, and refuses the sign-in.

## 5b. Installing it on the phone (PWA)

The app is an installable web app (a manifest, icons and a small service worker that only caches the page itself, never your data).
Browsers only offer to install, and only run the service worker, on **HTTPS or localhost**:

* **Tailscale**: `tailscale serve --bg 8765` gives you an `https://<machine>.<tailnet>.ts.net` address: open it on the phone, then
  Chrome menu > *Install app* (or the **Install app** button in the page footer); on iPhone, Safari Share > *Add to Home Screen*.
* A plain `http://<LAN address>` works as a normal web page, but cannot be installed.
* Open the `?token=` link once. The login cookie lasts a year. An iPhone home-screen app has its **own** cookie jar: open the
  `?token=` link once inside it, or paste the token into the form it shows.

## 5c. Running a SHARED server (several people, each with their own BYD account)

By default the app serves **one** car, yours. If friends or testers who cannot self-host should use your server for **their own** cars, run it in shared mode.
Read the risk first: **you will be holding other people's BYD logins** (encrypted on your disk, but your machine decrypts them to poll). Run it only for people who
know that, and tell them (`docs/TESTERS.md` is written for them).

```bash
sudo cp /opt/byd-range/deploy/byd-range-shared.service /etc/systemd/system/byd-range.service   # instead of the single-owner unit
echo 'BYD_APP_INVITE=choose-a-code-to-hand-out' | sudo tee /etc/byd-range/shared.env && sudo chmod 600 /etc/byd-range/shared.env   # optional
sudo systemctl daemon-reload && sudo systemctl restart byd-range
```

What it does differently: no access token. Each person signs in on the page with their own BYD account, gets a private folder under `/var/lib/byd-range/users/`,
their own poller process (started and stopped automatically) and their own battery setting. Nobody can see anyone else's data. `--max-users` (default 10) caps
sign-ups, `BYD_APP_INVITE` makes new people enter a code (people already signed up are never locked out by either).

* **HTTPS is required.** The server refuses to accept a password except over HTTPS (it trusts the proxy's `X-Forwarded-Proto: https`; add `--assume-https` if your
  proxy terminates TLS but does not send it) or from the machine itself. Put Caddy, nginx or a tunnel in front. Be aware that a tunnel provider (ngrok, Cloudflare) ends
  the encryption on its own servers, so **it can see the passwords that testers type**. Caddy or nginx on your own server with your own domain does not have that problem.
* **Resources:** roughly 40 to 60 MB of memory per signed-in person (one Python poller each). A small VPS handles ten comfortably.
* **Wrong logins can lock a BYD account**, so each BYD account gets 5 sign-in attempts per 10 minutes, each address 20, the whole server 120.
* **A person deleting their data** from the Account page ends their sessions at once; within about 10 seconds the supervisor stops their poller and wipes their folder.
  You can also remove someone by stopping the service, deleting `/var/lib/byd-range/users/<id>` (ids are opaque, derived from the account), and starting it again.
* **Backups:** back up `/var/lib/byd-range` (no passwords in plain text; the key in `/etc/byd-range/credential.key` stays out of it). Tell people their history is in your backups.
* **Check it** with `bash deploy/e2e/e2e_multiuser.sh` (fake pyBYD, throwaway folder, nothing real is touched).

## 6. Updating, backups and checking it

To check a deployment end to end without any BYD account, run `bash /opt/byd-range/deploy/e2e/e2e_linux.sh` (and `e2e_multiuser.sh` for shared mode) (it uses a fake
pyBYD and a throwaway data folder; see `deploy/e2e/README.md`).

## 6b. Updating and backups

* Update: copy the new code over `/opt/byd-range`, re-run the `pip install --require-hashes ...` line if `requirements.txt`
  changed, then `sudo systemctl restart byd-range`. Python changes only take effect after a restart.
* Back up `/var/lib/byd-range/history.sqlite` (your history). It is safe to back up: it contains no password. Keep the key
  file (`/etc/byd-range/credential.key`) out of that backup.
* Check it is healthy: `systemctl status byd-range`, or open the **Account** page: it shows what the poller is doing.

## Environment variables (all optional)

| Variable | Meaning |
|---|---|
| `BYD_DATA_DIR` | Where history, the encrypted sign-in and the token live (default `cloud/data`). |
| `BYD_APP_INVITE` | Shared server only: a code new people must enter to sign up. Set it in `/etc/byd-range/shared.env`, never on the command line. |
| `BYD_APP_KEY_FILE` | Path of the key file, if you do not use the systemd credential. Must be readable only by the service user. |
| `XDG_CONFIG_HOME` | Without either of the above, a key is created at `$XDG_CONFIG_HOME/byd-range/credential.key` (mode 0600). |
