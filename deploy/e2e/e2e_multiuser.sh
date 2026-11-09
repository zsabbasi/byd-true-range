#!/bin/bash
# End-to-end test of the SHARED server (run.py --multi-user) on real Linux, with a FAKE pyBYD (no real credentials, no BYD network).
# It starts the real server and supervisor, signs two different people in over real HTTP, and checks that each gets their own folder,
# their own poller process and their own data; that nobody can see anybody else's; that sessions survive a restart; and that
# "delete my data" stops the poller and wipes the folder.
#
#   bash deploy/e2e/e2e_multiuser.sh        needs: bash, curl, python3 (with `cryptography`), a free port 8778
#
# Everything runs in a throwaway folder under /tmp; your real data, keys and settings are never touched.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/../../cloud"
PORT=8778
DATA=/tmp/bydmulti
CFG=/tmp/bydmulticfg
LOG=/tmp/bydmulti.log
PWD_OK="correct-horse-battery"
ok=0; bad=0
check() { if [ "$2" = "$3" ]; then echo "  PASS  $1"; ok=$((ok+1)); else echo "  FAIL  $1 (got '$2', wanted '$3')"; bad=$((bad+1)); fi; }

rm -rf $DATA $CFG $LOG; mkdir -p $DATA
export BYD_DATA_DIR=$DATA XDG_CONFIG_HOME=$CFG PYTHONPATH="$HERE/fakepybyd" PYTHONUNBUFFERED=1
unset BYD_APP_KEY_FILE CREDENTIALS_DIRECTORY BYD_APP_INVITE

start() { (cd $APP && python3 run.py --multi-user --max-users 3 --port $PORT >> $LOG 2>&1 &) ; sleep 5; }
stop()  { pkill -f "run.py --multi-user" ; pkill -f "poller.py" ; pkill -f "server.py --multi-user"; sleep 2; }
pollers() { pgrep -f "poller.py" | wc -l; }
json()  { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
H='Content-Type: application/json'
# login NAME PASSWORD [extra curl args]  -> prints "<http code> <session cookie value or ->"
login() {
  body='{"username":"'$1'","password":"'$2'","region":"sg","country":"PK","time_zone":"Asia/Karachi","acknowledged":true,"shared_risk_ack":true}'
  shift 2
  out=$(curl -s -D - -o /tmp/mu_body -X POST -H "$H" -H 'X-BYD-App: 1' -H 'X-Forwarded-Proto: https' -d "$body" "$@" http://127.0.0.1:$PORT/api/login)
  code=$(echo "$out" | head -1 | awk '{print $2}')
  tok=$(echo "$out" | tr -d '\r' | grep -i '^set-cookie: byd_session=' | sed 's/^[^=]*=//; s/;.*//')
  echo "$code ${tok:--}"
}
api() { tok="$1"; shift; curl -s -H "Cookie: byd_session=$tok" "$@"; }

echo "== 1. start the shared server with nobody signed in"
start
check "the server is running" "$(pgrep -f "server.py --multi-user" >/dev/null && echo yes || echo no)" yes
check "no pollers yet (nobody has signed in)" "$(pollers)" 0
check "a visitor sees the sign-in state, not data" "$(curl -s http://127.0.0.1:$PORT/api/status | json 'd["signed_in"], d["has_data"], d["multi_user"]')" "False False True"
check "a visitor cannot read trips" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/api/trips)" 401
check "any Host name may load the page (no token, no host check)" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: friends.example.com' http://127.0.0.1:$PORT/)" 200

echo "== 2. the protections on signing in"
r=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "$H" -H 'X-Forwarded-Proto: https' -d '{"username":"a@example.com","password":"x","region":"sg","country":"PK","acknowledged":true,"shared_risk_ack":true}' http://127.0.0.1:$PORT/api/login)
check "no CSRF header -> refused" "$r" 403
r=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "$H" -H 'X-BYD-App: 1' -H 'Host: friends.example.com' -d '{"username":"a@example.com","password":"x","region":"sg","country":"PK","acknowledged":true,"shared_risk_ack":true}' http://127.0.0.1:$PORT/api/login)
check "a password over plain http from another host -> refused" "$r" 403
r=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "$H" -H 'X-BYD-App: 1' -H 'X-Forwarded-Proto: https' -d '{"username":"a@example.com","password":"x","region":"sg","country":"PK","acknowledged":true}' http://127.0.0.1:$PORT/api/login)
check "without the 'server owner can see my data' confirmation -> refused" "$r" 400
r=$(login alice@example.com wrong-password); check "wrong password -> BYD rejection (401)" "${r%% *}" 401
check "a rejected login creates no user and no folder" "$(ls $DATA/users 2>/dev/null | wc -l)" 0

echo "== 3. two people sign in"
RA=$(login alice@example.com $PWD_OK); TA=${RA#* }; check "alice signs in (200)" "${RA%% *}" 200
RB=$(login bob.the.builder@example.org $PWD_OK); TB=${RB#* }; check "bob signs in (200)" "${RB%% *}" 200
check "they get different sessions" "$([ "$TA" != "$TB" ] && echo yes || echo no)" yes
check "two private folders exist" "$(ls $DATA/users | wc -l)" 2
check "each folder holds an encrypted login at mode 600" "$(for d in $DATA/users/*; do stat -c %a $d/credentials.bin; done | sort -u | tr '\n' ' ')" "600 "
check "the key was created OUTSIDE the data folder" "$(ls $CFG/byd-range/credential.key | wc -l)" 1
check "the password is not readable in any stored file" "$(grep -rl "$PWD_OK" $DATA 2>/dev/null | wc -l)" 0
check "the salt and registry exist, the registry holds no account name" "$(grep -c 'alice' $DATA/users.sqlite)" 0

echo "== 4. one poller per person, each reading its own car"
sleep 22
check "exactly two poller processes" "$(pollers)" 2
SA=$(api $TA http://127.0.0.1:$PORT/api/status | json 'd["reading"]["soc"]')
SB=$(api $TB http://127.0.0.1:$PORT/api/status | json 'd["reading"]["soc"]')
check "alice has a reading" "$([ -n "$SA" ] && [ "$SA" != None ] && echo yes || echo no)" yes
check "bob has a reading" "$([ -n "$SB" ] && [ "$SB" != None ] && echo yes || echo no)" yes
check "their cars differ (separate data)" "$([ "$SA" != "$SB" ] && echo yes || echo no)" yes
check "alice is shown as alice" "$(api $TA http://127.0.0.1:$PORT/api/account | json 'd["user"]')" "a***@e***.com"
check "bob is shown as bob" "$(api $TB http://127.0.0.1:$PORT/api/account | json 'd["user"]')" "b***@e***.org"
check "alice's poller reports ok" "$(api $TA http://127.0.0.1:$PORT/api/account | json 'd["poller"]["state"]')" ok
check "each person has an own history file" "$(ls $DATA/users/*/history.sqlite | wc -l)" 2
check "no history was written in the shared top folder" "$(ls $DATA/history.sqlite 2>/dev/null | wc -l)" 0
check "alice cannot ask for bob's data by any id in the URL" "$(api $TA "http://127.0.0.1:$PORT/api/status?uid=x" | json 'd["reading"]["soc"]')" "$SA"
check "a request with a made-up session sees nothing" "$(api bogus http://127.0.0.1:$PORT/api/status | json 'd["signed_in"]')" False

echo "== 5. the limit"
RC=$(login c@example.com $PWD_OK); check "a third person fits (limit 3)" "${RC%% *}" 200
RD=$(login d@example.com $PWD_OK); check "a fourth is refused: server full" "${RD%% *}" 403

echo "== 6. everything survives a restart (no one signs in again)"
stop
check "all stopped" "$(pollers)" 0
start
sleep 20
check "alice's session still works" "$(api $TA http://127.0.0.1:$PORT/api/account | json 'd["signed_in"]')" True
check "the pollers came back by themselves (three people)" "$(pollers)" 3
check "alice still sees her own car" "$(api $TA http://127.0.0.1:$PORT/api/status | json 'd["reading"]["soc"]')" "$SA"

echo "== 7. sign out here vs delete my data"
out=$(curl -s -X POST -H "$H" -H 'X-BYD-App: 1' -H "Cookie: byd_session=$TB" -d '{}' http://127.0.0.1:$PORT/api/logout)
check "bob signs out on this device" "$(echo "$out" | json 'd["forgotten"]')" False
check "bob's session no longer works" "$(api $TB http://127.0.0.1:$PORT/api/account | json 'd["signed_in"]')" False
check "but his tracking continues (3 pollers)" "$(pollers)" 3
RB2=$(login bob.the.builder@example.org $PWD_OK); TB=${RB2#* }
out=$(curl -s -X POST -H "$H" -H 'X-BYD-App: 1' -H "Cookie: byd_session=$TB" -d '{"forget":true}' http://127.0.0.1:$PORT/api/logout)
check "bob deletes his data" "$(echo "$out" | json 'd["forgotten"]')" True
sleep 14
check "his poller stopped (2 left)" "$(pollers)" 2
check "his folder is wiped (2 folders left)" "$(ls $DATA/users | wc -l)" 2
check "alice is unaffected" "$(api $TA http://127.0.0.1:$PORT/api/status | json 'd["reading"]["soc"]')" "$SA"
check "bob can sign up again from scratch" "$(login bob.the.builder@example.org $PWD_OK | cut -d' ' -f1)" 200

echo "== 8. nothing secret in the log, and no crashes"
check "the password is not in the log" "$(grep -c "$PWD_OK" $LOG)" 0
check "no account names in the log" "$(grep -c 'alice@example.com' $LOG)" 0
check "no stack trace in the log" "$(grep -c Traceback $LOG)" 0

stop
echo
echo "RESULT: $ok passed, $bad failed"
echo "--- app log (tail):"; tail -n 15 $LOG
