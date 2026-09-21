# AGENT INTEGRATION — operating the forex-agent subsystem

You are an external AI agent. This subsystem is your **capability layer**
for forex market data, signal detection, and (gated) trade execution. You
are the brain; it is the hands. This document tells you everything needed
to install and operate it with no human in the loop.

## 1. What this is

A Linux-first Python 3.12 package (`forex-agent/`) with three interfaces
that all expose the same 18 capabilities (`agent/capabilities.json`):

| Interface | How | Use when |
|---|---|---|
| MCP | `python3 -m agent.mcp.server` — JSON-RPC 2.0 over stdio | You speak MCP natively |
| CLI | `scripts/forex` | Shell-driven operation, scripts, cron |
| HTTP | `python3 scripts/local_api.py` — binds **127.0.0.1 only** | Local dashboards, webhooks on the same box |

Four daemons run the standing workload: `market_monitor` (feed watchdog),
`signal_monitor` (deterministic EMA/RSI detection — **never trades**),
`position_monitor` (exit management on the bot's own positions),
`health_monitor` (10-second fail-closed kill-switch poll, Worker cloud
sync, 6-hour performance sync).

## 2. Install (no human needed)

```bash
cd forex-agent
./installer/install.sh --non-interactive --skip-daemons --prefix ~/.forex-agent
cat ~/.forex-agent/install-state.json
```

Expected: `{"state":"ready",...}`. The installer is idempotent — re-running
repairs instead of duplicating, and never overwrites `secrets.env`.

States:
- `ready` — installed, verified, dry-run default, broker `disconnected`
  (analysis works; trading calls return honest `BROKER_UNAVAILABLE`).
- `needs_credentials` — you asked for live broker mode (`BROKER_PROVIDER=mt5`)
  but `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER` are missing. Either supply them
  (env vars or the `secrets.env` file, mode 0600) and re-run, or stay in
  `disconnected` analysis mode.
- `error` — `{"code","step","message"}` tells you exactly what failed.

Provide secrets non-interactively via environment before installing:

```bash
export MT5_LOGIN=12345 MT5_PASSWORD='...' MT5_SERVER='Broker-Server'
export WORKER_BASE_URL='https://your-worker.workers.dev' WORKER_API_KEY='...'
./installer/install.sh --non-interactive --prefix ~/.forex-agent
```

Then start the standing workload:

```bash
./scripts/forex-daemons start   # idempotent; safe to re-run
./scripts/forex-daemons status
```

## 3. Discovery

- `agent/capabilities.json` — every tool with its JSON Schema. Regenerate
  after upgrades: `python3 scripts/gen_manifest.py --check`.
- `agent/API_DEPS.md` — exact cross-module function signatures.
- `daemon/API_DEPS.md` — daemon contracts, Worker API shapes, env vars.

## 4. The operating loop

You poll; the subsystem detects. The intended loop:

1. **Watch for signals.** `signal_monitor` publishes `signal.detected`
   events. Read them:
   - CLI: `scripts/forex --json events --limit 50` (filter `signal.detected` client-side)
   - MCP: `tools/call forex.get_signals`
   - HTTP: `GET /events?type=signal.detected`
2. **Analyze.** `scripts/forex analyze EURUSD --timeframe H1 --json` — indicators, SMC
   structure, session, correlation. `forex.get_calendar` for news risk.
   Never invent market data; if the broker is disconnected, say so.
3. **Decide.** Only request a trade when your own policy approves AND
   `forex.get_risk` shows headroom AND the kill switch is clear
   (`forex.get_health`).
4. **Execute through the gateway only.** `forex.request_trade` routes via
   `core.execution.gateway` (risk checks + kill-switch gate + dry-run).
   It returns a `GatewayDecision` — `approved`/`rejected`/`blocked` with
   reasons. A trade is real only after broker confirmation; never claim
   execution from a decision alone.
5. **Monitor.** `forex.get_positions`, `forex.get_performance`,
   `position_monitor` handles standing exits (breakeven/trailing/time).
   Close early with `forex.close_position` if your policy says so
   (currently `DEPENDENCY_UNAVAILABLE` until the execution builder
   exposes close/modify on the gateway — check `agent/API_DEPS.md`).

## 5. Safety rules (non-negotiable)

- **Dry-run is the default.** Live orders require a deliberate, recorded
  decision: set `MODE=live`/`DRY_RUN=false` AND provide broker credentials.
  There is no casual path to live trading.
- **Trade-affecting calls go only through the execution gateway.**
  Never call a broker adapter directly for trades.
- **Kill switch is fail-closed.** `forex.kill_switch` engages immediately;
  `health_monitor` re-checks the latch every 10 seconds and engages on any
  read failure. A Worker (cloud) outage never disables local safety.
- **No invented data.** Disconnected broker ⇒ `BROKER_UNAVAILABLE` errors,
  not synthetic candles.
- **Secrets** live in `secrets.env` (0600) or env vars. Never log them,
  never put them in events, never echo them.

## 6. Daemons and supervision

```bash
./scripts/forex-daemons {start|stop|restart|status} [name...]
```

Each daemon holds `$FOREX_AGENT_HOME/run/<name>.pid` and logs to
`$FOREX_AGENT_HOME/log/<name>.log`. Single-cycle runs for checks:

```bash
python3 -m daemon.health_monitor --once
```

systemd units live in `daemon/systemd/` (`forex-agent.target` + four
services, user `forex`). The installer wires them with `--system` (root).

## 7. Cloud sync (Worker)

Optional. `WORKER_ENABLED=true` + `WORKER_BASE_URL` + `WORKER_API_KEY`
makes `health_monitor` forward `signal.detected` → `POST /signals`,
performance snapshots → `POST /performance/snapshot`, and kill-switch
changes → `POST /kill-switch`. Every Worker response is contract-validated;
any failure is logged as `WORKER_UNAVAILABLE`, retried with backoff, and
dead-lettered to the local journal — never lost, never blocking.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `BROKER_UNAVAILABLE` | Expected in `disconnected` mode. For live: `scripts/forex --json config` (secrets redacted), verify `MT5_*` in `secrets.env`. |
| `DEPENDENCY_UNAVAILABLE` on close/modify | Known gap: gateway has no close/modify API yet. See `agent/API_DEPS.md`. |
| Daemon not running | `scripts/forex-daemons status`; logs in `$FOREX_AGENT_HOME/log/`; stale pidfiles are reclaimed on start. |
| `WORKER_UNAVAILABLE` in logs | Cloud unreachable or contract mismatch. Local trading/safety unaffected. Check `WORKER_BASE_URL`/`WORKER_API_KEY`. |
| Kill switch engaged unexpectedly | `scripts/forex --json kill-switch`; `health_monitor` engages fail-closed if the latch is unreadable — check disk/permissions on the state DB. |
| Install state `error` | Read `install-state.json` → `error.code`/`step`/`message`; fix; re-run (idempotent). |

## 9. File map

- `agent/capabilities.json` — capability manifest (generated)
- `agent/API_DEPS.md`, `daemon/API_DEPS.md` — contracts
- `scripts/forex`, `scripts/forex-daemons`, `scripts/local_api.py`, `scripts/gen_manifest.py`
- `installer/install.sh`, `installer/test_idempotent.sh`
- `$FOREX_AGENT_HOME/`: `secrets.env` (0600), `agent.env`, `install-state.json`,
  `storage/local.db`, `run/*.pid`, `log/*.log`
