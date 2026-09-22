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
| MCP | `python3 -m agent.mcp.server` (from `forex-agent/`) — JSON-RPC 2.0 over stdio | You speak MCP natively |
| CLI | `scripts/forex` (from `forex-agent/`) | Shell-driven operation, scripts, cron |
| HTTP | `python3 scripts/local_api.py` — binds **127.0.0.1 only** | Local dashboards, same-box webhooks |

Plus the **event channel** — a localhost SSE stream that pushes
structured events to you (see §5). Four daemons run the standing
workload: `market_monitor` (feed watchdog), `signal_monitor`
(deterministic EMA/RSI detection — **never trades**),
`position_monitor` (exit management on the bot's own positions),
`health_monitor` (10-second fail-closed kill-switch poll, Worker cloud
sync, 6-hour performance sync).

## 2. Install (no human needed)

```bash
cd forex-agent
./installer/install.sh --agent --non-interactive --skip-daemons --prefix ~/.forex-agent
cat ~/.forex-agent/install-result.json
```

Expected: `"status": "installed"` — verified, dry-run default, broker
`disconnected` (analysis-capable subsystems work; anything needing the
broker returns honest `BROKER_UNAVAILABLE`). The installer is idempotent
— re-running repairs instead of duplicating, and **never overwrites**
`secrets.env`. The double-run proof is `installer/test_idempotent.sh`
(11 assertions).

### Two result files

| File | Shape | Use |
|---|---|---|
| `$FOREX_AGENT_HOME/install-state.json` | `{"state": "ready"/"needs_credentials"/"error", ...}` | legacy simple state, always written |
| `$FOREX_AGENT_HOME/install-result.json` | schema `forex-agent.install-result/1`, rich `status` vocabulary + probe fields | the machine-readable contract; printed on stdout with `--agent` (alias `--json`) |

`install-result.json.status` is derived from real probes
(`broker_status()`, config mode, kill-switch state, daemon supervisor)
— never invented:

- `installed` — verified, disconnected analysis mode.
- `configured` — broker credentials present, daemons not started.
- `operational` — daemons running.
- `broker_disconnected` — a broker was expected but the probe failed.
  **This is honest status, not an error**: fix the broker, re-run.
- `broker_connected` — logged in, trading not available.
- `trading_disabled` — broker up, but dry-run default or kill switch
  engaged.
- `trading_ready` — broker up + `trading_available` + `MODE=live` +
  kill switch clear.
- `needs_credentials` — `BROKER_PROVIDER=mt5` but `MT5_LOGIN` /
  `MT5_PASSWORD` / `MT5_SERVER` missing or blank → with a `required`
  list.
- `error` — `{"code","step","message"}` names the failed step.

### Credentials

Supply non-interactively via environment before installing (written
into `secrets.env`, mode 0600, never printed, never overwritten):

```bash
# Local Windows-terminal MT5:
export MT5_LOGIN=12345 MT5_PASSWORD='...' MT5_SERVER='Broker-Server'
# — or remote MT5 gateway host (see docs/ARCHITECTURE.md §9):
export MT5_GATEWAY_URL='https://mt5-gateway.example.com' MT5_GATEWAY_TOKEN='...'
export BROKER_PROVIDER=mt5
export WORKER_ENABLED=true WORKER_BASE_URL='https://forex-control-plane.ayiijumo.workers.dev' WORKER_API_KEY='...'
./installer/install.sh --agent --non-interactive --prefix ~/.forex-agent
```

Then start the standing workload:

```bash
./scripts/forex-daemons start   # idempotent; safe to re-run
./scripts/forex-daemons status
```

## 3. Discovery

- `agent/capabilities.json` — every tool with its JSON Schema, plus the
  real interface commands, installer status vocabulary, broker-status
  fields, and modes. Regenerate after upgrades:
  `python3 scripts/gen_manifest.py --check` (fails if stale).
- `agent/API_DEPS.md` — exact cross-module function signatures.
- `daemon/API_DEPS.md` — daemon contracts, Worker API shapes, env vars.
- `docs/ARCHITECTURE.md` §9–§12 — runtime model, gateway lifecycle,
  events, capability contract.

## 4. The operating loop

You poll; the subsystem detects. The intended loop:

1. **Watch for signals.** `signal_monitor` publishes `signal.detected`
   events. Receive them — the stream is the primary channel, polling
   is the fallback:
   - SSE (recommended): `GET http://127.0.0.1:8765/events` with
     `Accept: text/event-stream`; filter `signal.detected` on the
     `event:` field client-side. See §5 for resume/ack/reconnect.
   - CLI one-shot: `scripts/forex events --since-id <last_id> --json`
   - CLI tail: `scripts/forex events --follow --json` (JSON lines,
     Ctrl-C to stop)
   - MCP: `tools/call forex.get_signals`
   - HTTP poll: `GET /events/latest?since=<event_id>&limit=50`
2. **Analyze.** `scripts/forex analyze EURUSD --timeframe H1 --json` —
   indicators, SMC structure, session, correlation.
   `forex.get_calendar` reports `available: false` honestly (no provider
   wired) — never invent news risk.
3. **Decide.** Only request a trade when your own policy approves AND
   `scripts/forex risk --json` shows headroom AND the kill switch is
   clear (`scripts/forex health --json`).
4. **Execute through the gateway only.** Two real entry points:
   - MCP: `tools/call forex.request_trade` with
     `{signal_id, symbol, direction, volume, stop_loss, take_profit,
     request_id?}`.
   - HTTP: `POST http://127.0.0.1:8765/trade/request` with the same
     JSON body (the CLI is read-only plus kill-switch; it has no trade
     entry point by design).
   The call routes via `core.execution.gateway` (idempotency → kill
   switch → symbol whitelist → risk engine → dry-run block → broker
   submit). It returns a `GatewayDecision` — `approved`/`rejected`/
   `blocked` with reasons and a lifecycle `state`. **A trade is real
   only after broker confirmation** (`state: executed`); never claim
   execution from a decision alone. In dry-run mode the terminal state
   is `dry_run_simulated` — never `executed`.
5. **Monitor.** `scripts/forex positions --json`,
   `scripts/forex performance --json`; `position_monitor` handles
   standing exits (breakeven/trailing/time). Close early with
   `forex.close_position` (MCP) or `POST /position/close` (HTTP), and
   tighten stops with `forex.modify_position` / `POST /position/modify`
   — stops may only ever tighten (direction-aware), never loosen.

## 5. Receiving events (SSE — the actual mechanism)

The event stream is a plain localhost HTTP endpoint; any SSE client
works. No SDK, no auth — the **127.0.0.1 bind is the access control**.

```bash
# Subscribe (port from FOREX_API_PORT, default 8765):
curl -N -H 'Accept: text/event-stream' \
  'http://127.0.0.1:8765/events?resume_from=evt_<last_seen_id>&ack=evt_<last_seen_id>'
```

Frames arrive as `id:` / `event:` / `data:` — `data:` is one JSON line
with `event_id`, `severity` (`INFO`/`NOTICE`/`WARNING`/`CRITICAL`),
`timestamp`, `payload`. The server sends `: ping` every 15 s while idle.

**The reconnect contract** (this is the whole mechanism — implement it
exactly):

1. Keep the last processed `id:` in memory (and durably, if you
   restart).
2. On any disconnect, reconnect with `?resume_from=<last_id>`. The
   server replays **every event journaled after that id, oldest first**,
   then continues with live events. No `resume_from` → only new events
   from connect time.
3. Unknown `resume_from`/`ack` id → HTTP 400: treat it as a fresh
   journal (your cursor belongs to a rotated DB), log, and resubscribe
   without `resume_from`.
4. `ack=<id>` marks that event and everything before it delivered
   (cumulative). Ack is a delivery marker — replays may still return
   acked events, so **dedup on `event_id`** (unique per event,
   `evt_` + uuid4 hex).
5. At most 1000 events replay per connect; reconnect with the newest id
   to page further.

Event types you will actually see: `signal.detected` (NOTICE),
`trade.requested` (INFO), `trade.executed` (NOTICE),
`trade.rejected` (WARNING), `risk.blocked` (WARNING — CRITICAL on
daily-loss limit), `kill_switch.activated` (CRITICAL),
`broker.disconnected` (CRITICAL), `health.check` (INFO). Full severity
table and the poll fallback: `docs/EVENTS.md`.

## 6. Safety rules (non-negotiable)

- **Dry-run is the default** (`mode: dry_run` in config). Live orders
  require a deliberate, recorded act: `MODE=live` (env or `agent.env`)
  AND valid broker credentials. There is no casual path to live trading.
- **Trade-affecting calls go only through the execution gateway.**
  `core/execution/broker_guard.py` wraps the adapter so that any direct
  write outside the gateway's `execution_scope()` raises
  `GATEWAY_BYPASS_ATTEMPTED`. Never call a broker adapter directly.
- **Kill switch is fail-closed.** `forex.kill_switch` engages
  immediately and the latch persists across restarts; `health_monitor`
  re-checks it every 10 seconds and engages on any read failure. While
  engaged: `request_trade` and `modify_position` are rejected, but
  `close_position` is still allowed (closing reduces risk). A Worker
  (cloud) outage never disables local safety — the local latch is
  always authoritative.
- **No invented data.** Disconnected broker ⇒ `BROKER_UNAVAILABLE`
  errors, not synthetic candles. Real MT5 trading has not been
  validated in this environment (no terminal/credentials here).
- **Secrets** live in `secrets.env` (0600) or env vars. Never log them,
  never put them in events, never echo them. `scripts/forex config
  --json` redacts them; `AppConfig.redacted()` is the only printable
  form.

## 7. Daemons and supervision

```bash
./scripts/forex-daemons {start|stop|restart|status|health|safety|prune|logs} [name...]
```

Each daemon holds `$FOREX_AGENT_HOME/run/<name>.pid` and logs to
`$FOREX_AGENT_HOME/log/<name>.log`. No duplicates ever: a lock
serializes mutating commands and each daemon re-checks its PID file —
a second launcher loses and exits quietly. Single-cycle runs for checks:

```bash
python3 -m daemon.health_monitor --once
```

systemd units live in `daemon/systemd/` (`forex-agent.target` + four
services, user `forex`), plus `forex-journal-prune.timer` for daily
event-journal pruning. The installer wires them with `--system` (root).
Full lifecycle semantics (restart safety sequence, journal pruning,
kill-switch persistence): `docs/OPERATIONS.md`.

## 8. Cloud sync (Worker)

Optional. `WORKER_ENABLED=true` + `WORKER_BASE_URL` + `WORKER_API_KEY`
makes `health_monitor` forward `signal.detected` → `POST /signals`,
performance snapshots → `POST /performance/snapshot`, and kill-switch
changes → `POST /kill-switch`. Every Worker response is contract-validated;
any failure is logged as `WORKER_UNAVAILABLE`, retried with backoff, and
dead-lettered to the local journal — never lost, never blocking. The
Worker never auto-approves signals and is never a safety authority.

## 9. Walkthrough: EURUSD signal → event → analyze → request → confirm

The conceptual flow, with real commands at every step. Assume daemons
running, `MODE` default (dry-run), broker connected or not.

```
# 1. signal_monitor detects an EMA/RSI setup and publishes:
#      event: signal.detected   severity: NOTICE
#      payload: {signal_id, symbol: "EURUSD", direction, entry, ...}
#    Your SSE client receives it (or: scripts/forex events --follow --json).

# 2. Analyze before deciding:
scripts/forex analyze EURUSD --timeframe H1 --json
# → indicators, SMC structure, session, correlation. Broker down?
# → honest BROKER_UNAVAILABLE / unavailable flags — decide with what exists.

# 3. Check headroom + safety:
scripts/forex risk --json     # daily-loss headroom, volume step clamp
scripts/forex health --json   # kill switch clear? broker status?

# 4. Request the trade through the gateway (MCP example):
#    tools/call forex.request_trade
#    { "signal_id": "<from the event>", "symbol": "EURUSD",
#      "direction": "BUY", "volume": 0.10,
#      "stop_loss": 1.0810, "take_profit": 1.0890,
#      "request_id": "<your uuid — retry-safe>" }
#
#    or HTTP:
curl -s -X POST http://127.0.0.1:8765/trade/request \
  -H 'Content-Type: application/json' \
  -d '{"signal_id":"<id>","symbol":"EURUSD","direction":"BUY","volume":0.10,
       "stop_loss":1.0810,"take_profit":1.0890,"request_id":"<uuid>"}'

#    → GatewayDecision: approved/rejected/blocked + lifecycle state.
#    Lifecycle on the way: requested → validated → risk_checked →
#    approved → submitted → broker_acknowledged → executed.
#    In dry-run: terminal state dry_run_simulated (decision carries it).

# 5. Confirmation arrives as an event:
#      event: trade.executed   severity: NOTICE
#      (state == "executed" ⇒ broker confirmed. Nothing else counts.)

# 6. Standing management:
scripts/forex positions --json
#    tighten the stop only (never loosen):
#    tools/call forex.modify_position {"ticket": <n>, "stop_loss": <higher>}
#    exit fully:
#    tools/call forex.close_position {"ticket": <n>}
```

Retry rule: if step 4's response is lost (timeout, crash), **resend
with the same `request_id`** — the gateway returns the original
decision with state `duplicate_suppressed`. Never invent a new id on
retry; one fill per signal, ever.

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| `BROKER_UNAVAILABLE` | Expected in `disconnected` mode. For live: `scripts/forex broker-status --json` shows the structured flags (`configured`/`reachable`/`connected`/`trading_available`); verify `MT5_*` in `secrets.env`. `broker_disconnected` installer status is honest, not an error. |
| `trade.rejected` WARNING event | Read the `GatewayDecision` reasons: kill switch, risk, symbol whitelist, validation. Fix the cause; never bypass the gateway. |
| Kill switch engaged unexpectedly | `scripts/forex kill-switch --json`; `health_monitor` engages fail-closed if the latch is unreadable — check disk/permissions on the state DB. |
| SSE disconnects | Reconnect with `?resume_from=<last_id>` (see §5); unknown id → 400 means a fresh/rotated journal — resubscribe clean. |
| Daemon not running | `scripts/forex-daemons status`; logs in `$FOREX_AGENT_HOME/log/`; stale pidfiles are reclaimed on start. |
| `WORKER_UNAVAILABLE` in logs | Cloud unreachable or contract mismatch. Local trading/safety unaffected. Check `WORKER_BASE_URL`/`WORKER_API_KEY`. |
| Install status `error` | Read `install-result.json` → `error.code`/`step`/`message`; fix; re-run (idempotent). |
| Notifications not arriving | `scripts/forex notify --status` (secret-free); the agent/SSE channel is the journal itself — check `scripts/forex events`. |

## 11. File map

- `agent/capabilities.json` — capability manifest (generated; verify
  with `scripts/gen_manifest.py --check`)
- `agent/API_DEPS.md`, `daemon/API_DEPS.md` — contracts
- `scripts/forex`, `scripts/forex-daemons`, `scripts/local_api.py`,
  `scripts/gen_manifest.py`
- `installer/install.sh`, `installer/test_idempotent.sh`
- `$FOREX_AGENT_HOME/`: `secrets.env` (0600), `agent.env`,
  `install-state.json`, `install-result.json`,
  `storage/local.db`, `run/*.pid`, `run/notify.cursor`, `log/*.log`
