# INSTALL — forex-agent subsystem

Linux-first. Python 3.12+, no new pip dependencies (stdlib only).

## Quick install (recommended)

```bash
cd forex-agent
./installer/install.sh --agent --non-interactive --prefix ~/.forex-agent
cat ~/.forex-agent/install-result.json
```

`--agent` (alias `--json`) prints the structured install result on
stdout and stores it in `$FOREX_AGENT_HOME/install-result.json` — the
machine-readable contract a host agent consumes (see "Install states").

Then start the background daemons:

```bash
./scripts/forex-daemons start
./scripts/forex-daemons status
```

## What the installer does

1. Verifies `python3 >= 3.12`.
2. Creates the agent home (`~/.forex-agent` by default): `run/`, `log/`,
   `storage/`.
3. Creates `secrets.env` (**mode 0600**, never overwritten on re-runs) and
   fills in any secrets found in the environment:
   `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_GATEWAY_URL`,
   `MT5_GATEWAY_TOKEN`, `WORKER_API_KEY`.
   Without `--non-interactive` and with a TTY, it prompts for missing ones
   (passwords silently).
4. Writes `agent.env` (non-secret defaults: `BROKER_PROVIDER=disconnected`,
   symbols, timeframe, dry-run).
5. Smoke-tests: manifest freshness check (`scripts/gen_manifest.py
   --check`), MCP ping, CLI config render.
6. Optionally starts the six managed services (four monitors plus the
   `local_api` SSE/API server and the `agent_bridge` notification
   bridge; `--skip-daemons` to skip).
7. Probes the real state (`broker_status()`, config mode, kill-switch
   latch, daemon supervisor, plus a real TCP/`/health`/SSE-handshake
   probe of the local API) and writes the machine-readable result to
   `$FOREX_AGENT_HOME/install-result.json` (printed on stdout with
   `--agent`/`--json`).
8. With `--system` (as root): creates the `forex` user, installs systemd
   units from `daemon/systemd/` into `/etc/systemd/system`, enables
   `forex-agent.target`.

## Install states

Two files, two vocabularies — both always written:

- `$FOREX_AGENT_HOME/install-state.json` — the legacy simple state:

  | state | meaning | next step |
  |---|---|---|
  | `ready` | installed + verified | start daemons, operate |
  | `needs_credentials` | installed, but live broker mode lacks `MT5_*` | add credentials to `secrets.env`, re-run |
  | `error` | `{"code","step","message"}` | fix the named step, re-run |

- `$FOREX_AGENT_HOME/install-result.json` — the contract a host agent
  reads (`schema: "forex-agent.install-result/1"`). `status` is derived
  from real probes, never invented:

  | status | meaning | next step |
  |---|---|---|
  | `installed` | verified install, disconnected analysis mode | operate; broker calls return honest `BROKER_UNAVAILABLE` |
  | `configured` | broker credentials present, daemons not started | `scripts/forex-daemons start` |
  | `operational` | daemons running | normal operation (see the `broker`/`trading` fields) |
  | `broker_disconnected` | broker expected (`provider != disconnected`) but the probe failed | **honest status, not an error** — fix the terminal/gateway, re-run |
  | `broker_connected` | broker probe: logged in, trading not available | check terminal trade permissions |
  | `trading_disabled` | broker up but dry-run default or kill switch engaged | set `MODE=live` / disengage kill switch deliberately |
  | `trading_ready` | broker up + `trading_available` + `MODE=live` + kill switch clear | live trading is possible |
  | `needs_credentials` | broker mode requested but `MT5_*` missing/blank | add them to `secrets.env` (0600), re-run — result carries a `required` list |
  | `error` | a step failed (`error.code`/`step`/`message`) | fix the named step, re-run |

  Example (fresh install, no broker, `--skip-daemons`):

  ```json
  {
    "schema": "forex-agent.install-result/1",
    "status": "installed",
    "mode": "dry_run",
    "kill_switch_engaged": false,
    "broker": "disconnected",
    "broker_provider": "disconnected",
    "broker_detail": "No broker configured (provider=disconnected).",
    "trading": "disabled",
    "signals": "available",
    "events": "unavailable",
    "agent_notification": "unavailable",
    "mcp": "available",
    "daemons": false,
    "manifest": "/opt/forex-agent/agent/capabilities.json"
  }
  ```

  `events` is `"running"` only when the installer **really probed**
  `127.0.0.1:$FOREX_API_PORT` — TCP accept, `GET /health` → `ok:true`,
  and an actual SSE handshake on `GET /events` — otherwise
  `"unavailable"`. Daemon liveness alone never earns `"running"`.
  `agent_notification` is `"configured"` (bridge running + a
  `FOREX_AGENT_NOTIFICATION_COMMAND` sink is set), `"unconfigured"`
  (bridge running, no sink — cursor tracked, nothing proactively
  delivered), or `"unavailable"` (bridge not running).

The installer is **idempotent**: re-running repairs and verifies instead of
duplicating. The double-run proof lives in
`installer/test_idempotent.sh` (11 assertions, run it any time).

## Modes

- **Analysis-only (default):** `BROKER_PROVIDER=disconnected`.
  Broker-dependent calls return honest `BROKER_UNAVAILABLE`; the CLI,
  MCP, API, events, and config all work.
- **Live MT5:** set `BROKER_PROVIDER=mt5` plus `MT5_LOGIN` /
  `MT5_PASSWORD` / `MT5_SERVER` in `secrets.env`, then re-run the
  installer. Where MT5 actually runs on Linux is specified in
  `broker/mt5/RUNTIME.md` and `docs/ARCHITECTURE.md` §9: either a
  Windows terminal (local or Wine-emulated) — or the **remote MT5
  gateway**, configured with `MT5_GATEWAY_URL` + `MT5_GATEWAY_TOKEN`
  (bearer auth mandatory; 12-operation allowlist only — see
  `broker/mt5/gateway_contract.md`). There is no fake MT5: without a
  real terminal/gateway, the adapter reports `BROKER_UNAVAILABLE`.
- **Dry-run (default):** even with a broker connected, orders are
  simulated unless you deliberately set `MODE=live` (env or
  `agent.env`). There is no casual path to live trading.

## Cloud sync (optional)

```bash
export WORKER_ENABLED=true
export WORKER_BASE_URL='https://forex-control-plane.ayiijumo.workers.dev'
export WORKER_API_KEY='...'
./installer/install.sh --non-interactive --prefix ~/.forex-agent
```

The Worker is cloud/sync only: signal ledger, performance snapshots,
kill-switch mirror for the mobile app. It is never on the local safety
path — an outage is logged as `WORKER_UNAVAILABLE` and changes nothing
locally.

## Uninstall

```bash
./scripts/forex-daemons stop
# per-user install:
rm -rf ~/.forex-agent
# system install (as root):
systemctl disable --now forex-agent.target
rm -f /etc/systemd/system/forex-*.service /etc/systemd/system/forex-agent.target
userdel forex
rm -rf /var/lib/forex-agent /opt/forex-agent
```

## Health checks after install

```bash
scripts/forex health --json        # broker flags, kill-switch latch, daemons, worker
scripts/forex broker-status --json # structured broker_status(): configured/reachable/
                                   # connected/account_available/market_data_available/
                                   # trading_available + detail
scripts/forex-daemons health --json
```

`broker_disconnected` in the install result or all-`false` flags in
`broker-status` is **honest status, not an error** — it means the broker
runtime isn't there. Analysis subsystems still work; trading calls
report `BROKER_UNAVAILABLE`.

## Troubleshooting

| Symptom | Action |
|---|---|
| `needs_credentials` | `BROKER_PROVIDER=mt5` but `MT5_*` missing/blank — add them to `secrets.env` (0600) and re-run. |
| `broker_disconnected` | Broker expected but unreachable: check the terminal/gateway host, `MT5_GATEWAY_URL`/`MT5_GATEWAY_TOKEN` (gateway bearer auth is mandatory), or fall back to `BROKER_PROVIDER=disconnected` analysis mode. |
| Install status `error` | Read `install-result.json` → `error.code`/`step`/`message`; fix; re-run (idempotent). |
| `scripts/forex-daemons status` shows stopped | Start them: `scripts/forex-daemons start`; logs in `$FOREX_AGENT_HOME/log/`. |
| Smoke-test failure on manifest | `python3 scripts/gen_manifest.py` regenerates `agent/capabilities.json`; `--check` verifies freshness. |

## Verifying an install

```bash
./installer/test_idempotent.sh   # installer double-run proof (11 assertions)
python3 -m pytest tests          # full suite (368 tests)
python3 scripts/gen_manifest.py --check  # manifest freshness
```
