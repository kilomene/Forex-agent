#!/usr/bin/env bash
# install.sh — Linux-first, idempotent installer for the forex-agent subsystem.
#
#   installer/install.sh [--prefix DIR] [--system] [--non-interactive]
#                        [--skip-daemons] [--source DIR] [--agent|--json]
#
# Agent-friendly: every knob is a flag or an env var; with --non-interactive
# it never prompts. Safe to re-run: every step detects existing state and
# repairs instead of duplicating. Never overwrites an existing secrets file.
#
# Result is recorded in $FOREX_AGENT_HOME/install-state.json:
#   {"state": "ready"}              everything installed and verified
#   {"state": "needs_credentials"}  installed; broker credentials missing
#   {"state": "error", "error": {"code","step","message"}}
#
# With --agent (alias --json) the installer additionally emits a
# machine-readable install result on stdout and always writes
# $FOREX_AGENT_HOME/install-result.json. The result's "status" is derived
# from REAL probes (broker_status(), config mode, kill switch, daemon
# supervisor) — never invented:
#   installed            verified install, disconnected analysis mode
#   configured           verified, broker credentials present, daemons not started
#   operational          daemons running (subsystem operating; see broker field)
#   broker_disconnected  broker expected (provider != disconnected) but probe failed
#   broker_connected     broker probe: logged in, trading not available
#   trading_disabled     broker up but dry-run default or kill switch engaged
#   trading_ready        broker up + trading_available + mode=live + kill switch clear
#   needs_credentials    provider != disconnected and MT5_* keys missing/invalid
#   error                a step failed (error.code/step/message)
# Missing credentials are reported as {"status":"needs_credentials",
# "required":[...]}; empty or whitespace-only values count as missing.
#
# Secrets (MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, WORKER_API_KEY) come from
# the environment or an interactive prompt, and are written to
# $FOREX_AGENT_HOME/secrets.env with mode 0600. They are never printed.
set -u

# -- options ---------------------------------------------------------------
PREFIX="${FOREX_AGENT_HOME:-$HOME/.forex-agent}"
SYSTEM=0
NON_INTERACTIVE=0
SKIP_DAEMONS=0
SOURCE=""
AGENT_OUT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)          PREFIX="$2"; shift 2 ;;
        --system)          SYSTEM=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --skip-daemons)    SKIP_DAEMONS=1; shift ;;
        --source)          SOURCE="$2"; shift 2 ;;
        --agent|--json)    AGENT_OUT=1; shift ;;
        -h|--help)         sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$SOURCE" ]; then
    # The forex-agent tree containing this installer.
    SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
fi

STATE_FILE="$PREFIX/install-state.json"
SECRETS_FILE="$PREFIX/secrets.env"
ENV_FILE="$PREFIX/agent.env"
SECRET_KEYS="MT5_LOGIN MT5_PASSWORD MT5_SERVER MT5_GATEWAY_TOKEN WORKER_API_KEY NOTIFY_WEBHOOK_URL"

log()  { echo "[install] $*"; }
jsonq() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }
write_state() { # write_state <state> [extra_json]
    local state="$1" extra="${2:-}"
    if [ -n "$extra" ]; then
        printf '{"state":%s,"updated_at":%s,%s}\n' \
            "$(jsonq "$state")" "$(jsonq "$(date -u +%FT%TZ)")" "$extra" > "$STATE_FILE"
    else
        printf '{"state":%s,"updated_at":%s}\n' \
            "$(jsonq "$state")" "$(jsonq "$(date -u +%FT%TZ)")" > "$STATE_FILE"
    fi
}

# -- agent result ------------------------------------------------------------
# agent_probe: run the REAL probes (config mode, kill switch, broker_status)
# in the source tree. Prints one JSON line on stdout; never fails the caller.
agent_probe() {
    ( cd "$SOURCE" 2>/dev/null || exit 1
      FOREX_AGENT_HOME="$PREFIX" FOREX_AGENT_STORAGE="$PREFIX/storage/local.db" \
      timeout 90 python3 - 2>/dev/null <<'PYEOF' | grep '^{' | tail -n 1
import json
out = {"probe_ok": False}
try:
    from agent.tools import backend
    cfg = backend.app_config()
    try:
        kill_engaged = bool(backend.store().get_kill_switch().get("engaged"))
    except Exception:
        kill_engaged = None
    try:
        broker = backend.broker_adapter().broker_status().get("broker", {})
        broker_probe_ok = True
    except Exception as exc:  # broker_status() itself never raises; the lookup might
        broker = {}
        broker_probe_ok = False
        out["error"] = "broker probe failed: %s" % str(exc)[:200]
    out.update({"probe_ok": True, "mode": cfg.mode,
                "kill_engaged": kill_engaged,
                "broker": broker, "broker_probe_ok": broker_probe_ok})
except Exception as exc:
    out = {"probe_ok": False, "error": str(exc)[:200]}
print(json.dumps(out))
PYEOF
    )
    return 0
}

# emit_agent_result [status] [error_json]: derive the machine-readable
# install result from real probes, write $PREFIX/install-result.json, and
# print it on stdout when --agent/--json was passed. With no status the
# status is derived; pass "error" explicitly from fail().
emit_agent_result() {
    local status="${1:-}" err="${2:-null}"
    local probe daemons_out
    probe="$(agent_probe)" || probe=""
    [ -n "$probe" ] || probe='{"probe_ok":false}'
    daemons_out="$(FOREX_AGENT_HOME="$PREFIX" timeout 30 ./scripts/forex-daemons status 2>/dev/null)" \
        || daemons_out=""
    AGENT_STATUS="$status" AGENT_ERR="$err" AGENT_PROBE="$probe" \
    AGENT_DAEMONS="$daemons_out" AGENT_PREFIX="$PREFIX" AGENT_SOURCE="$SOURCE" \
    AGENT_OUT="$AGENT_OUT" \
    python3 - <<'PYEOF'
import json, os

status = os.environ.get("AGENT_STATUS", "")
err = json.loads(os.environ.get("AGENT_ERR") or "null")
probe = json.loads(os.environ.get("AGENT_PROBE") or '{"probe_ok": false}')
dstat = os.environ.get("AGENT_DAEMONS", "")
prefix = os.environ["AGENT_PREFIX"]
source = os.environ["AGENT_SOURCE"]
show = os.environ.get("AGENT_OUT") == "1"

SECRET_KEYS = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
provider = (os.environ.get("BROKER_PROVIDER") or "disconnected").strip().lower()
# Empty or whitespace-only counts as missing/invalid.
missing = [k for k in SECRET_KEYS if not (os.environ.get(k) or "").strip()]

DAEMON_NAMES = ("market_monitor", "signal_monitor",
                "position_monitor", "health_monitor")
daemons_running = bool(dstat) and all(
    ("%s: running" % n) in dstat for n in DAEMON_NAMES)

probe_ok = bool(probe.get("probe_ok")) and bool(probe.get("broker_probe_ok"))
broker = probe.get("broker") if isinstance(probe.get("broker"), dict) else {}

if not status:
    if err is not None:
        status = "error"
    elif provider != "disconnected" and missing:
        status = "needs_credentials"
    elif probe_ok:
        connected = bool(broker.get("connected"))
        trading_avail = bool(broker.get("trading_available"))
        mode = probe.get("mode")
        kill = probe.get("kill_engaged")
        if connected and trading_avail and mode == "live" and kill is False:
            status = "trading_ready"
        elif connected and trading_avail:
            status = "trading_disabled"   # dry-run default or kill switch engaged
        elif connected:
            status = "broker_connected"
        elif daemons_running:
            status = "broker_disconnected"
        elif provider != "disconnected":
            status = "configured"        # creds present, daemons not started
        else:
            status = "installed"          # disconnected analysis mode, verified
    else:
        status = "broker_disconnected" if daemons_running else (
            "configured" if (provider != "disconnected" and not missing)
            else "installed")

result = {
    "schema": "forex-agent.install-result/1",
    "status": status,
    "prefix": prefix,
    "mode": probe.get("mode") if probe.get("probe_ok") else None,
    "kill_switch_engaged": (probe.get("kill_engaged")
                            if probe.get("probe_ok") else None),
    "signals": "unavailable" if status == "error" else "available",
    "events": "running" if daemons_running else "stopped",
    "mcp": "unavailable" if status == "error" else "available",
    "daemons": daemons_running,
    "manifest": os.path.join(source, "agent", "capabilities.json"),
}
if probe_ok:
    result["broker"] = "connected" if broker.get("connected") else "disconnected"
    detail = broker.get("detail")
    result["broker_detail"] = (detail.get("reason")
                               if isinstance(detail, dict) else None)
    result["broker_provider"] = broker.get("provider")
    result["trading"] = "ready" if status == "trading_ready" else "disabled"
else:
    result["broker"] = "unavailable"
    result["broker_detail"] = probe.get("error") or "broker probe failed"
    result["broker_provider"] = provider or None
    result["trading"] = "unavailable"
if status == "needs_credentials":
    result["required"] = missing
if status == "error" and err is not None:
    result["error"] = err

os.makedirs(prefix, exist_ok=True)
with open(os.path.join(prefix, "install-result.json"), "w") as fh:
    json.dump(result, fh, indent=2)
    fh.write("\n")
if show:
    print(json.dumps(result))
PYEOF
}

fail() { # fail <code> <step> <message>
    local code="$1" step="$2" msg="$3"
    log "ERROR [$code] at $step: $msg"
    mkdir -p "$(dirname "$STATE_FILE")"
    printf '{"state":"error","error":{"code":%s,"step":%s,"message":%s},"updated_at":%s}\n' \
        "$(jsonq "$code")" "$(jsonq "$step")" "$(jsonq "$msg")" "$(jsonq "$(date -u +%FT%TZ)")" \
        > "$STATE_FILE"
    if command -v python3 >/dev/null 2>&1; then
        emit_agent_result "error" \
            "$(python3 -c 'import json,sys; print(json.dumps({"code":sys.argv[1],"step":sys.argv[2],"message":sys.argv[3]}))' \
                "$code" "$step" "$msg")" || true
    fi
    exit 1
}

# -- step: python ----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || fail "NO_PYTHON" "python" "python3 not found on PATH"
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PYVER" in
    3.12|3.13|3.14|3.15) log "python $PYVER ok" ;;
    *) fail "BAD_PYTHON" "python" "need python >= 3.12, found $PYVER" ;;
esac

# -- step: directories (idempotent) ----------------------------------------
for d in "$PREFIX" "$PREFIX/run" "$PREFIX/log" "$PREFIX/storage"; do
    if [ ! -d "$d" ]; then mkdir -p "$d" && log "created $d"; fi
    # The home tree holds secrets.env (0600) and the local DB: keep
    # directory listings private too, not just the secret file itself.
    chmod 700 "$d"
done

# -- step: secrets (never overwrite, always 0600) ---------------------------
if [ ! -f "$SECRETS_FILE" ]; then
    : > "$SECRETS_FILE"
    log "created $SECRETS_FILE"
fi
chmod 600 "$SECRETS_FILE"

# Add any missing keys from the environment (or prompt interactively).
for key in $SECRET_KEYS; do
    if grep -q "^${key}=" "$SECRETS_FILE" 2>/dev/null; then continue; fi
    val="${!key:-}"
    if [ -z "$val" ] && [ "$NON_INTERACTIVE" -eq 0 ] && [ -t 0 ]; then
        if [ "$key" = "MT5_PASSWORD" ] || [ "$key" = "WORKER_API_KEY" ]; then
            read -r -s -p "[install] $key (empty to skip): " val; echo
        else
            read -r -p "[install] $key (empty to skip): " val
        fi
    fi
    if [ -n "$val" ]; then
        # Shell-quote robustly via python (single-quote style).
        qval="$(python3 -c 'import sys; print("\x27" + sys.argv[1].replace("\x27", "\x27\\\x27\x27") + "\x27")' "$val")"
        printf '%s=%s\n' "$key" "$qval" >> "$SECRETS_FILE"
        log "stored $key in secrets file"
    fi
done
chmod 600 "$SECRETS_FILE"

# -- step: non-secret env file ---------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# forex-agent runtime environment (non-secret). Secrets live in secrets.env (0600).
FOREX_AGENT_HOME=$PREFIX
FOREX_AGENT_STORAGE=$PREFIX/storage/local.db
BROKER_PROVIDER=\${BROKER_PROVIDER:-disconnected}
SYMBOLS=\${SYMBOLS:-EURUSD,GBPUSD,USDJPY,AUDUSD}
TIMEFRAME=\${TIMEFRAME:-H1}
WORKER_ENABLED=\${WORKER_ENABLED:-false}
WORKER_BASE_URL=\${WORKER_BASE_URL:-}
DRY_RUN=\${DRY_RUN:-true}
EOF
    log "wrote $ENV_FILE"
fi

# -- step: source tree sanity ----------------------------------------------
[ -x "$SOURCE/scripts/forex" ] || fail "BAD_SOURCE" "source" "no executable scripts/forex in $SOURCE"
[ -f "$SOURCE/agent/capabilities.json" ] || fail "BAD_SOURCE" "source" "agent/capabilities.json missing in $SOURCE"

# -- step: smoke tests ------------------------------------------------------
export FOREX_AGENT_HOME="$PREFIX"
export FOREX_AGENT_STORAGE="$PREFIX/storage/local.db"
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; . "$SECRETS_FILE"; set +a

cd "$SOURCE" || fail "BAD_SOURCE" "smoke" "cannot cd to $SOURCE"
python3 scripts/gen_manifest.py --check \
    || fail "SMOKE_MANIFEST" "smoke" "capabilities.json is stale"
# MCP ping over stdio.
if ! printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"ping"}' \
    | timeout 30 python3 -m agent.mcp.server 2>/dev/null \
    | grep -q '"result"'; then
    fail "SMOKE_MCP" "smoke" "MCP server ping failed"
fi
# CLI config renders without secrets.
if ! timeout 30 ./scripts/forex config --json 2>/dev/null | grep -q '"ok"'; then
    fail "SMOKE_CLI" "smoke" "CLI config command failed"
fi
log "smoke tests passed"

# -- step: daemons (optional) ------------------------------------------------
if [ "$SKIP_DAEMONS" -eq 0 ]; then
    if ! timeout 120 ./scripts/forex-daemons start; then
        fail "DAEMONS" "daemons" "could not start all daemons (see $PREFIX/log/)"
    fi
    log "daemons started"
else
    log "skipping daemon start (--skip-daemons)"
fi

# -- step: systemd (opt-in, needs root) -------------------------------------
if [ "$SYSTEM" -eq 1 ]; then
    [ "$(id -u)" -eq 0 ] || fail "NOT_ROOT" "systemd" "--system requires root"
    command -v systemctl >/dev/null 2>&1 || fail "NO_SYSTEMD" "systemd" "systemctl not found"
    if ! id forex >/dev/null 2>&1; then
        useradd -r -s /usr/sbin/nologin -d /var/lib/forex-agent forex \
            || fail "USERADD" "systemd" "could not create forex user"
        log "created forex user"
    fi
    mkdir -p /var/lib/forex-agent /opt/forex-agent
    chown forex:forex /var/lib/forex-agent
    # Install units with the real source path baked in.
    for unit in "$SOURCE"/daemon/systemd/forex-*.service "$SOURCE"/daemon/systemd/forex-agent.target; do
        base="$(basename "$unit")"
        sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$SOURCE|" \
            -e "s|^Environment=FOREX_AGENT_HOME=.*|Environment=FOREX_AGENT_HOME=/var/lib/forex-agent|" \
            -e "s|^Environment=FOREX_AGENT_STORAGE=.*|Environment=FOREX_AGENT_STORAGE=/var/lib/forex-agent/storage/local.db|" \
            "$unit" > "/etc/systemd/system/$base"
    done
    # Secrets for the system install live in /var/lib/forex-agent/secrets.env.
    if [ ! -f /var/lib/forex-agent/secrets.env ] && [ -f "$SECRETS_FILE" ]; then
        cp "$SECRETS_FILE" /var/lib/forex-agent/secrets.env
        chmod 600 /var/lib/forex-agent/secrets.env
        chown forex:forex /var/lib/forex-agent/secrets.env
    fi
    systemctl daemon-reload
    systemctl enable --now forex-agent.target \
        || fail "SYSTEMD" "systemd" "could not enable forex-agent.target"
    log "systemd units installed and enabled"
fi

# -- step: final state -------------------------------------------------------
# Ready means: installed AND (analysis-only disconnected mode OR broker
# credentials present). Otherwise the install is complete but waiting on
# the human for credentials.
set +u
BROKER_PROVIDER="${BROKER_PROVIDER:-disconnected}"
HAVE_CREDS=1
for key in MT5_LOGIN MT5_PASSWORD MT5_SERVER; do
    eval "v=\${$key:-}"
    [ -n "$v" ] || HAVE_CREDS=0
done
set -u

if [ "$BROKER_PROVIDER" = "disconnected" ] || [ "$HAVE_CREDS" -eq 1 ]; then
    write_state "ready" '"mode":"dry_run_default","daemons":true'
    log "install complete: state=ready"
else
    write_state "needs_credentials" '"missing":["MT5_LOGIN","MT5_PASSWORD","MT5_SERVER"]'
    log "install complete: state=needs_credentials (broker credentials missing)"
fi
emit_agent_result
exit 0
