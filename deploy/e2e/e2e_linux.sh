#!/bin/bash
# End-to-end test of sign-in, persistence, restart and sign-out on real Linux, with a FAKE pyBYD (no real credentials and no
# network to BYD). It starts the real run.py (poller + web app), talks to it over real HTTP, and checks the real encrypted store.
#
#   bash deploy/e2e/e2e_linux.sh        needs: bash, curl, python3 (with `cryptography`), a free port 8777
#
# It uses a throwaway data folder and key under /tmp and never touches your real data, key or secrets.
set -u
PORT=8777
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/../../cloud"
DATA=/tmp/bydrun
CFG=/tmp/bydcfg
LOG=/tmp/bydrun.log
PW="correct-horse-battery"
ok=0; bad=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1"; ok=$((ok+1)); else echo "  FAIL  $1 (got '$2', wanted '$3')"; bad=$((bad+1)); fi; }

rm -rf $DATA $CFG /tmp/jar $LOG; mkdir -p $DATA
export BYD_DATA_DIR=$DATA XDG_CONFIG_HOME=$CFG PYTHONPATH="$HERE/fakepybyd" PYTHONUNBUFFERED=1
unset BYD_APP_KEY_FILE CREDENTIALS_DIRECTORY

start() { (cd $APP && python3 run.py --require-token --port $PORT >> $LOG 2>&1 &) ; sleep 6; }
stop()  { pkill -f "run.py --require-token --port $PORT" ; pkill -f "poller.py" ; pkill -f "server.py --require-token --port $PORT"; sleep 2; }
api()   { curl -s -b /tmp/jar "$@"; }
post()  { # post PATH JSON [extra curl args]
  p="$1"; body="$2"; shift 2
  curl -s -b /tmp/jar -X POST -H 'Content-Type: application/json' -H 'X-BYD-App: 1' -d "$body" -w ' [%{http_code}]' "$@" http://127.0.0.1:$PORT$p; }
field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1'))"; }
GOOD='{"username":"sub.account@example.com","password":"'$PW'","region":"sg","country":"PK","time_zone":"Asia/Karachi","acknowledged":true}'
BAD='{"username":"sub.account@example.com","password":"wrong","region":"sg","country":"PK","time_zone":"Asia/Karachi","acknowledged":true}'

echo "== 1. start the app with nobody signed in"
start
check "the app is running (did not exit for lack of credentials)" "$(pgrep -f "run.py --require-token --port $PORT" >/dev/null && echo yes || echo no)" yes
TOKEN=$(cat $DATA/access_token.txt)
check "token mode: no token, no access" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/api/account)" 401
check "the token URL sets a session cookie" "$(curl -s -c /tmp/jar -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/?token=$TOKEN")" 302
check "account: not signed in" "$(api http://127.0.0.1:$PORT/api/account | field signed_in)" False
check "poller says it is waiting for a sign-in" "$(api http://127.0.0.1:$PORT/api/account | python3 -c 'import sys,json; print(json.load(sys.stdin)["poller"]["state"])')" waiting-for-signin
check "the Now page is told nobody is signed in" "$(api http://127.0.0.1:$PORT/api/status | field signed_in)" False

echo "== 2. the protections"
check "no CSRF header -> refused" "$(curl -s -b /tmp/jar -X POST -H 'Content-Type: application/json' -d "$GOOD" -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/api/login)" 403
code=$(curl -s -b /tmp/jar -X POST -H 'Content-Type: application/json' -H 'X-BYD-App: 1' -H 'X-Forwarded-For: 1.2.3.4' -d "$GOOD" -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/api/login)
check "a tunnel header (X-Forwarded-For) -> 403" "$code" 403
code=$(curl -s -b /tmp/jar -X POST -H 'Content-Type: application/json' -H 'X-BYD-App: 1' -H 'Host: abc.ngrok-free.app' -H "Cookie: byd_token=$TOKEN" -d "$GOOD" -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/api/login)
check "a non-local Host header (ngrok) with a VALID token -> still 403" "$code" 403
check "nothing was stored by refused attempts" "$(ls $DATA/credentials.bin 2>/dev/null | wc -l)" 0

echo "== 3. wrong password, then the right one"
out=$(post /api/login "$BAD")
echo "$out" | grep -q '\[401\]' && r=401 || r="$out"
check "wrong password -> BYD rejection (401)" "$r" 401
check "the rejection carries BYD's code" "$(echo "$out" | sed 's/ \[401\]//' | field code)" 3008
check "nothing stored after a rejected login" "$(ls $DATA/credentials.bin 2>/dev/null | wc -l)" 0
out=$(post /api/login "$GOOD")
echo "$out" | grep -q '\[200\]' && r=200 || r="$out"
check "right password -> signed in (200)" "$r" 200
check "reply lists the car (no password in it)" "$(echo "$out" | grep -c "$PW")" 0
check "credentials file written" "$(ls $DATA/credentials.bin | wc -l)" 1
check "the password is NOT readable in that file" "$(grep -c "$PW" $DATA/credentials.bin)" 0
check "the username is NOT readable in that file" "$(grep -c "sub.account" $DATA/credentials.bin)" 0
check "the credentials file is mode 600" "$(stat -c %a $DATA/credentials.bin)" 600
check "the key was created OUTSIDE the data folder" "$(ls $CFG/byd-range/credential.key | wc -l)" 1
check "the key file is mode 600" "$(stat -c %a $CFG/byd-range/credential.key)" 600
check "the key folder is mode 700" "$(stat -c %a $CFG/byd-range)" 700

echo "== 4. the poller picks the sign-in up by itself"
sleep 10
check "poller state is ok" "$(api http://127.0.0.1:$PORT/api/account | python3 -c 'import sys,json; print(json.load(sys.stdin)["poller"]["state"])')" ok
check "account: signed in, masked user" "$(api http://127.0.0.1:$PORT/api/account | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["signed_in"], d["user"])')" "True s***@e***.com"
check "a reading was recorded" "$(api http://127.0.0.1:$PORT/api/status | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["has_data"], d["reading"]["soc"])')" "True 80.0"
check "the stored protection is AES-256-GCM" "$(api http://127.0.0.1:$PORT/api/account | python3 -c 'import sys,json; print("AES-256-GCM" in json.load(sys.stdin)["store"]["protection"])')" True

echo "== 5. PERSISTENCE: restart the whole app; nobody signs in again"
stop
check "app stopped" "$(pgrep -f "run.py --require-token --port $PORT" | wc -l)" 0
start
sleep 8
TOKEN=$(cat $DATA/access_token.txt); rm -f /tmp/jar; curl -s -c /tmp/jar -o /dev/null "http://127.0.0.1:$PORT/?token=$TOKEN"
check "still signed in after the restart" "$(api http://127.0.0.1:$PORT/api/account | field signed_in)" True
check "poller is reading the car again without a new sign-in" "$(api http://127.0.0.1:$PORT/api/account | python3 -c 'import sys,json; print(json.load(sys.stdin)["poller"]["state"])')" ok

echo "== 6. a WRONG key (the key file is lost/replaced) is reported, not a crash"
stop
cp $CFG/byd-range/credential.key /tmp/credential.key.bak
echo "a-different-key-0123456789abcdef0123" > $CFG/byd-range/credential.key
start
sleep 5
rm -f /tmp/jar; curl -s -c /tmp/jar -o /dev/null "http://127.0.0.1:$PORT/?token=$TOKEN"
check "signed_in is False and says why" "$(api http://127.0.0.1:$PORT/api/account | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["signed_in"], d["store"]["readable"], bool(d["store"]["reason"]))')" "False False True"
stop
cp /tmp/credential.key.bak $CFG/byd-range/credential.key; chmod 600 $CFG/byd-range/credential.key
echo "== 6b. a key file readable by others is refused"
chmod 644 $CFG/byd-range/credential.key
start
sleep 5
rm -f /tmp/jar; curl -s -c /tmp/jar -o /dev/null "http://127.0.0.1:$PORT/?token=$TOKEN"
check "refused with a chmod hint" "$(api http://127.0.0.1:$PORT/api/account | python3 -c 'import sys,json; print("chmod 600" in json.load(sys.stdin)["store"]["reason"])')" True
stop
chmod 600 $CFG/byd-range/credential.key
start
sleep 8
rm -f /tmp/jar; curl -s -c /tmp/jar -o /dev/null "http://127.0.0.1:$PORT/?token=$TOKEN"

echo "== 7. sign out deletes it and the poller stops"
out=$(post /api/logout '{}')
echo "$out" | grep -q '\[200\]' && r=200 || r="$out"
check "sign out -> 200" "$r" 200
check "credentials file deleted" "$(ls $DATA/credentials.bin 2>/dev/null | wc -l)" 0
sleep 8
check "poller is waiting for a sign-in again" "$(api http://127.0.0.1:$PORT/api/account | python3 -c 'import sys,json; print(json.load(sys.stdin)["poller"]["state"])')" waiting-for-signin
check "account: signed out" "$(api http://127.0.0.1:$PORT/api/account | field signed_in)" False

echo "== 8. nothing secret in the log, the status file or the data folder (except the encrypted file, now deleted)"
check "the password is not in the app log" "$(grep -c "$PW" $LOG)" 0
check "the password is not in any file in the data folder" "$(grep -rl "$PW" $DATA 2>/dev/null | wc -l)" 0
check "the username is not in the app log" "$(grep -c 'sub.account' $LOG)" 0
check "no stack trace in the log" "$(grep -c Traceback $LOG)" 0

stop
echo
echo "RESULT: $ok passed, $bad failed"
echo "--- app log (token hidden):"; sed 's/token=[A-Za-z0-9_-]*/token=<hidden>/' $LOG | tail -25
