# INSTALL — forex-agent subsystem

Linux-first. Python 3.12+, no new pip dependencies (stdlib only).

## Quick install (recommended)

```bash
cd forex-agent
./installer/install.sh --non-interactive --prefix ~/.forex-agent
cat ~/.forex-agent/install-state.json
```

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
   `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `WORKER_API_KEY`.
   Without `--non-interactive` and with a TTY, it prompts for missing ones
   (passwords silently).
4. Writes `agent.env` (non-secret defaults: `BROKER_PROVIDER=disconnected`,
   symbols, timeframe, dry-run).
5. Smoke-tests: manifest freshness check, MCP ping, CLI config render.
6. Optionally starts the four daemons (`--skip-daemons` to skip).
7. With `--system` (as root): creates the `forex` user, installs systemd
   units from `daemon/systemd/` into `/etc/systemd/system`, enables
   `forex-agent.target`.

## Install states

`$FOREX_AGENT_HOME/install-state.json` always tells you where you stand:

| state | meaning | next step |
|---|---|---|
| `ready` | installed + verified | start daemons, operate |
| `needs_credentials` | installed, but live broker mode lacks `MT5_*` | add credentials to `secrets.env`, re-run |
| `error` | `{"code","step","message"}` | fix the named step, re-run |

The installer is **idempotent**: re-running repairs and verifies instead of
duplicating. The double-run proof lives in
`installer/test_idempotent.sh` (11 assertions, run it any time).

## Modes

- **Analysis-only (default):** `BROKER_PROVIDER=disconnected`. Market data
  is unavailable, so signal detection needs a broker — but the CLI, MCP,
  API, events, and config all work. Honest errors, no invented data.
- **Live MT5:** MetaTrader5 is Windows-only; on Linux this normally means
  the MT5 adapter talking to a remote terminal, or a future Linux-native
  adapter. Set `BROKER_PROVIDER=mt5` plus `MT5_LOGIN`/`MT5_PASSWORD`/
  `MT5_SERVER` in `secrets.env`, then re-run the installer.
- **Dry-run (default):** even with a broker connected, orders are simulated
  unless you deliberately set `MODE=live` (or `DRY_RUN=false`) in
  `agent.env`. There is no casual path to live trading.

## Cloud sync (optional)

```bash
export WORKER_ENABLED=true
export WORKER_BASE_URL='https://your-worker.workers.dev'
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

## Verifying an install

```bash
./installer/test_idempotent.sh          # installer double-run proof
python3 -m unittest discover -s tests   # full interface suite (115 tests)
python3 scripts/gen_manifest.py --check  # manifest freshness
```
