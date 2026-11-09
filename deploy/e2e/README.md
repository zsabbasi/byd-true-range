# End-to-end test (Linux)

`bash deploy/e2e/e2e_linux.sh` starts the real app with a **fake pyBYD** (`fakepybyd/`, which talks to nobody) and checks, over
real HTTP: refusal of sign-in from a tunnel or a non-local Host, a wrong password (rejected, nothing stored), the right password
(stored encrypted with AES-256-GCM, key outside the data folder, modes 600/700), the poller picking it up by itself, **a full
restart without signing in again**, a wrong or world-readable key (reported, not a crash), sign-out, and that the password never
reaches the log or any file. It needs bash, curl and python3 with `cryptography`.

Last result: 39 passed, 0 failed (Ubuntu 22.04, Python 3.10, cryptography 3.4.8).
