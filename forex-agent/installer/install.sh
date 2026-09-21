#!/usr/bin/env bash
# install.sh — Linux-first, idempotent installer for the forex-agent subsystem.
#
#   installer/install.sh [--prefix DIR] [--system] [--non-interactive]
#                        [--skip-daemons] [--source DIR]
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

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)          PREFIX="$2"; shift 2 ;;
        --system)          SYSTEM=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --skip-daemons)    SKIP_DAEMONS=1; shift ;;
        --source)          SOURCE="$2"; shift 2 ;;
        -h|--help)         sed -n '2,14p' "$0"; exit 0 ;;
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
SECRET_KEYS="MT5_LOGIN MT5_PASSWORD MT5_SERVER WORKER_API_KEY"

log()  { echo "[install] $*"; }
fail() { # fail <code> <step> <message>
    local code="$1" step="$2" msg="$3"
    log "ERROR [$code] at $step: $msg"
    mkdir -p "$(dirname "$STATE_FILE")"
    printf '{"state":"error","error":{"code":%s,"step":%s,"message":%s},"updated_at":%s}\n' \
        "$(jsonq "$code")" "$(jsonq "$step")" "$(jsonq "$msg")" "$(jsonq "$(date -u +%FT%TZ)")" \
        > "$STATE_FILE"
    exit 1
}
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
exit 0
