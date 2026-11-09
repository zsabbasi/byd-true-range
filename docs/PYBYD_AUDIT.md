# pyBYD supply-chain audit (2026-11-08)

Source audit of [`jkaberg/pyBYD`](https://github.com/jkaberg/pyBYD) (main branch) before using it to log
in to BYD's cloud with a real account. Done by reading the source on GitHub, not by running it.

## Verdict: clean. It only talks to BYD.

Every network destination found in the code:

| Destination | Set by | Notes |
|---|---|---|
| `https://dilinkappoversea-eu.byd.auto` | `config.base_url` default | All HTTPS API traffic incl. login. Overridable via `BYD_BASE_URL`. |
| MQTT broker (TLS :8883) | **Returned by BYD's server** at `/app/emqAuth/getEmqBrokerIp` | Not hardcoded; BYD tells the client its own push broker. TLS cert validated vs system CAs. |

- No non-BYD hostnames, no IP literals, no telemetry/analytics/Sentry/crash-reporter code anywhere.
- One network library (`aiohttp`) via a single transport file (`_transport.py`); all endpoints are
  relative paths joined to `base_url`. No stray `requests`/`urllib`/`httpx`/`socket` usage.
- All 16 `_api/*.py` files, `session.py`, `_transport.py`, `_crypto/*`, `_mqtt.py`, `config.py`,
  `_redact.py` were read.

## Password flow
- Used as an AES key (`MD5(MD5(password))`) and signing input, and also sent as a plaintext `signKey`
  field inside the request body. The body is wrapped in a "Bangcle" white-box AES envelope (tables shipped
  in `data/bangcle_tables.bin`) — obfuscation, not real secrecy. HTTPS protects it in transit; BYD's
  server receives your actual password (this is how BYD's own app works).
- Password is **never written to disk**, and is **redacted from logs** (`_redact.py` scrubs `password`,
  all tokens, cookies). Session holds only `user_id` + tokens, not the password.

## Runtime dependencies (all blue-chip)
`cryptography>=42.0` (PyCA), `aiohttp>=3.9,<4` (aio-libs), `pydantic>=2.6,<3`, `paho-mqtt>=2.1,<3` (Eclipse).

## Things to be aware of (not deal-breakers)
1. `BYD_BASE_URL` has no allow-list validation — a malicious value would send the password elsewhere.
   **Mitigation:** our `byd_config.py` passes `base_url` explicitly from the stored sign-in, not the shell env.
2. DEBUG logging would write VIN/userId/GPS (not password/tokens) to a log file. **Mitigation:** keep
   logging at INFO.
3. The MQTT broker host is server-supplied (can't be verified from source). Safe given you already trust
   BYD and TLS is validated; we also run with MQTT **off** in Phase 1.

## Residual to close later (optional)
Run once behind a local proxy (mitmproxy) to observe actual hosts contacted, if you want runtime
confirmation beyond the static read.
