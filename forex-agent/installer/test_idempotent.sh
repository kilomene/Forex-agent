#!/usr/bin/env bash
# test_idempotent.sh — proves installer/install.sh is idempotent.
#
#   installer/test_idempotent.sh
#
# Runs the installer twice against a throwaway prefix and asserts:
#   1. both runs exit 0;
#   2. the reported state is identical after each run;
#   3. a pre-existing secrets file is never overwritten (content + 0600);
#   4. needs_credentials is reported when the broker is live-mode without creds;
#   5. error state is structured when a step fails (bad source tree).
set -u

INSTALLER="$(cd "$(dirname "$0")" && pwd)/install.sh"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "ok   - $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL - $1"; }

# -- run 1 + 2: disconnected mode, pre-seeded secrets -----------------------
HOME1="$(mktemp -d)"
echo "MT5_LOGIN='sentinel-login'" > "$HOME1/secrets.env"
chmod 600 "$HOME1/secrets.env"

"$INSTALLER" --prefix "$HOME1" --non-interactive --skip-daemons >/tmp/inst1.log 2>&1
[ $? -eq 0 ] && ok "run 1 exits 0" || bad "run 1 exits 0"
STATE1="$(python3 -c 'import json; print(json.load(open("'"$HOME1"'/install-state.json"))["state"])')"
[ "$STATE1" = "ready" ] && ok "run 1 state=ready (disconnected)" || bad "run 1 state=ready (got $STATE1)"
[ "$(stat -c %a "$HOME1/secrets.env")" = "600" ] && ok "secrets file is 0600" || bad "secrets file mode"
grep -q "sentinel-login" "$HOME1/secrets.env" && ok "sentinel secret preserved" || bad "sentinel secret preserved"
SUM1="$(sha256sum "$HOME1/secrets.env" | cut -d' ' -f1)"

"$INSTALLER" --prefix "$HOME1" --non-interactive --skip-daemons >/tmp/inst2.log 2>&1
[ $? -eq 0 ] && ok "run 2 exits 0" || bad "run 2 exits 0"
STATE2="$(python3 -c 'import json; print(json.load(open("'"$HOME1"'/install-state.json"))["state"])')"
[ "$STATE2" = "$STATE1" ] && ok "run 2 state unchanged ($STATE2)" || bad "run 2 state unchanged"
SUM2="$(sha256sum "$HOME1/secrets.env" | cut -d' ' -f1)"
[ "$SUM2" = "$SUM1" ] && ok "secrets file untouched by run 2" || bad "secrets file untouched by run 2"

# -- needs_credentials: live broker, no creds --------------------------------
HOME2="$(mktemp -d)"
BROKER_PROVIDER=mt5 "$INSTALLER" --prefix "$HOME2" --non-interactive --skip-daemons >/tmp/inst3.log 2>&1
[ $? -eq 0 ] && ok "live-mode run exits 0" || bad "live-mode run exits 0"
STATE3="$(python3 -c 'import json; print(json.load(open("'"$HOME2"'/install-state.json"))["state"])')"
[ "$STATE3" = "needs_credentials" ] && ok "live without creds -> needs_credentials" \
    || bad "live without creds -> needs_credentials (got $STATE3)"

# -- error: bad source tree ----------------------------------------------------
HOME3="$(mktemp -d)"
"$INSTALLER" --prefix "$HOME3" --non-interactive --skip-daemons --source /nonexistent >/tmp/inst4.log 2>&1
[ $? -ne 0 ] && ok "bad source fails non-zero" || bad "bad source fails non-zero"
ERR="$(python3 -c 'import json; d=json.load(open("'"$HOME3"'/install-state.json")); print(d["state"], d["error"]["code"], d["error"]["step"])' 2>/dev/null)"
[ "${ERR%% *}" = "error" ] && ok "structured error state ($ERR)" || bad "structured error state"

rm -rf "$HOME1" "$HOME2" "$HOME3"
echo "---"
echo "pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
