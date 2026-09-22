# ARCHITECTURE — forex-agent subsystem

The agent provides the brain. The Forex subsystem provides the capability.
This document is the system map. Detailed chapters live in the companion
partials:

- `docs/ARCHITECTURE.core.md` — `core/`, `broker/`, `config/`
- `docs/ARCHITECTURE.intel.md` — `intelligence/`, `storage/`, `backtesting/`

This file adds: the agent interface layer, daemons, the Cloudflare Worker
(cloud/sync), and the installer — plus the principles that govern them all.

## 0. Non-negotiable principles (from the approved audit)

1. **The agent provides the brain; the subsystem provides the capability.**
   No LLM is built or hosted anywhere in this tree. All reasoning is the
   external agent's; the subsystem is deterministic capability.
2. **Deterministic safety below the agent layer.** Every trade-affecting
   operation passes through the execution gateway and its safety stack.
   Natural language, agent instructions, Worker outage, or model output
   can never bypass the kill switch or risk controls.
3. **Local safety survives cloud outage.** The kill-switch latch and risk
   state live in local SQLite; a dead Worker or network changes nothing.
4. **Dry-run/demo is the default.** Live trading requires an explicit
   config change (`mode: live`) plus valid credentials.
5. **Never invent data.** No fabricated market data, calendar events,
   execution results, or ML predictions. Unavailable sources report
   `available: false` honestly.
6. **Signal ≠ trade request ≠ approved trade ≠ confirmed execution.**
   Distinct states; execution is only reported after broker confirmation.
7. **Linux-first, agent-agnostic.** Never hard-code a host agent name.
8. **MT5 is quarantined.** `import MetaTrader5` occurs only under
   `broker/mt5/`; everything else talks to `BrokerAdapter`.
9. **Backtesting is structurally separated from live execution.**
10. **The mobile app is legacy reference only** (`docs/reference/`) —
    never a dependency.

## 1. System map

```
                        ┌─────────────────────────────┐
                        │      EXTERNAL AGENT          │  any host: Grok-class
                        │   (the brain — not ours)     │  bots, local models…
                        └──────────────┬──────────────┘
        interfaces (all equal citizens, none privileged):
          MCP (stdio) │ CLI │ localhost API │ events │ health
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│ agent/ — capability registry (18 forex.* tools)                  │
│  tools route ONLY through backend singletons: gateway, broker,  │
│  store. Trading tools never touch the adapter directly.         │
└────────────────────────┬────────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ core/execution/gateway — THE choke point                         │
│  idempotency → kill switch (fail-closed) → symbol whitelist →   │
│  risk engine → spread/tick/account/broker-grid checks →         │
│  dry-run block → broker submit → confirmed fills only           │
│  + close_position / modify_position (broker-confirmed, audited) │
└────────────────────────┬────────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ broker.BrokerAdapter — MT5 | paper | disconnected (same iface)   │
│ broker/mt5/ — the ONLY directory that may import MetaTrader5    │
└─────────────────────────────────────────────────────────────────┘
        ┌───────────────┴───────────────┐
        ▼                               ▼
 intelligence/ (deterministic)   storage/ (local SQLite)
  correlation · calendar          risk_state · kill_switch latch
  experience · reflections        event_queue · audit_log · journal
  ml (optional/parked)
        ▲                               ▲
 backtesting/ — read-only wrapper;      │ journal of experience
 never imports the gateway              │
                                        ▼
 daemon/ — 4 monitors (market, signal, position, health)
        │  + systemd units / PID fallback supervision
        ▼  (defensive client; dead-letter on failure)
 worker/ — Cloudflare Worker: cloud API / control / sync /
           persistence (D1) ONLY. Never the brain, never a
           safety authority. Never auto-approves signals.
```

## 2. Agent interfaces (`agent/`, `scripts/`, `agent/mcp/`)

One capability registry, three front-ends, identical semantics:

- **`agent/tools/`** — 18 `forex.*` capabilities as local Python
  functions: market data, analyze, signals, positions, account, risk,
  performance, trade history, SMC, session, calendar, experience,
  health, `request_trade`, `close_position`, `modify_position`,
  `kill_switch`. Structured `{ok, error_code, …}` dicts everywhere;
  documented JSON schemas in `agent/capabilities.json` and
  `agent/tools/registry.py`.
- **`agent/mcp/server.py`** — hand-written MCP JSON-RPC over stdio
  (no SDK dependency); tools + resources + ping.
- **`scripts/forex`** — CLI with the same capability surface,
  machine-readable JSON output.
- **`scripts/local_api.py`** — localhost-only HTTP API binding
  127.0.0.1 (the loopback bind IS the access control); tool failure →
  HTTP 422 with the same structured error body.
- **`agent/events/`** — strict structured event bus (schema-validated,
  persisted to the store's event queue, subscribers; FIFO pollable by
  agents/CLI). `core/events.py` is a shim that validates then publishes
  here.
- **`agent/capability/`** — local capability registry: agents discover
  what the subsystem can do without importing internals.

Routing invariant: trading tools (`request_trade`, `close_position`,
`modify_position`) call the **execution gateway**, never the adapter.
`forex.kill_switch` engages the persistent latch; closing is never
blocked by an engaged latch (closing reduces risk).

## 3. Daemons (`daemon/`)

Four monitors, each a single-responsibility loop with jittered
intervals, supervised by systemd units (`daemon/systemd/`, enabled via
`forex-agent.target`) or the fallback PID-based supervisor
(`scripts/forex-daemons`):

| Daemon | Owns |
|---|---|
| `market_monitor` | candle refresh via adapter; closed-candle signals |
| `signal_monitor` | pending-signal lifecycle vs. the Worker queue |
| `position_monitor` | exit pass (`core/positions`), reconciliation |
| `health_monitor` | broker health, store health, daemon heartbeats |

Daemons act through the same gateway/capability path as the agent —
no privileged back door. The Worker client (`daemon/worker_client.py`)
is defensive: timeouts, retries with backoff for idempotent reads,
dead-letter queue for failed syncs. A dead Worker degrades to
local-only operation; safety is unaffected.

## 4. Cloud layer (`worker/`)

The Cloudflare Worker is **cloud API / control / sync / persistence
(D1) only**:

- Signal ingestion stores `pending` signals; **it never auto-approves**
  (the old autonomous approve was removed) and runs no LLM.
- Lifecycle routes: approve / reject / executed / failed (new) /
  closed; kill-switch mirror; trading-mode settings; chart data;
  devices + FCM push; performance snapshots; reflection records.
- Per-client hashed credentials (revocable/rotatable) + per-isolate
  rate limiting; production needs a Cloudflare dashboard rule for a
  hard global cap.
- Non-destructive numbered migrations (`migrations/`); fresh-deploy
  `schema.sql`; `database_id` is a deploy-time placeholder.
- Full contract: `worker/CONTRACT.md`; fake-D1 contract suite green
  (20/20).

The Worker is never the brain and never a safety authority: the local
kill-switch latch and risk engine do not consult it.

## 5. Storage division (`storage/` vs `worker/` D1)

| Local (`storage/local.db`) | Cloud (Worker D1) |
|---|---|
| risk state (daily-loss baseline, streaks) | historical signal ledger |
| kill-switch latch (fail-closed) | devices, FCM tokens |
| event queue (FIFO) | performance snapshots |
| audit log (append-only) | reflection records |
| experience journal | chart data |

Local safety state never leaves the machine; cloud state is sync and
history. Details: `docs/ARCHITECTURE.intel.md` §2.

## 6. Configuration (`config/`)

One surface: `config/defaults.yaml` defaults, overlaid by `FOREX_*`
environment variables and an optional `0600` secrets file. `mode:
dry_run` default; `mode: live` + `provider: mt5` fails fast without
`MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER`. `AppConfig.redacted()` is the
only printable form. Full reference: `docs/CONFIGURATION.md`.

## 7. Install and operations

- Install: `docs/INSTALL.md` — Linux-first idempotent installer
  (`installer/install.sh`), machine-readable `install-state.json`
  (`needs_credentials` / `ready` / structured error), interactive or
  env-driven credentials, `0600` secrets, manifest freshness check,
  optional daemon start.
- Operations: `docs/OPERATIONS.md` — start/stop/status/restart,
  health, logs, upgrades, outage behavior (local safety continues),
  backup of `local.db`.
- Security: `docs/SECURITY.md` — threat model, credential handling,
  loopback-only API, fail-closed controls, what is never logged.

## 8. What is honestly unavailable (not hidden)

- **Economic calendar**: no provider wired — `available: false`.
- **ML prediction**: no trained model — `available: false` (serving
  parked; trainer optional).
- **MT5 connectivity**: untestable on this Linux box without a real
  terminal/gateway + credentials (see §9); the adapter degrades
  honestly to `BROKER_UNAVAILABLE`. Real MT5 trading has not been
  validated in this environment.
- **Worker deploy**: needs a real D1 `database_id` + deploy credentials.
- **Mobile app**: legacy reference only (`docs/reference/`).

## 9. MT5 on Linux: where the terminal actually runs

There is no fake MT5 anywhere in this tree, and there never will be.
The rule is stated once, in `broker/mt5/RUNTIME.md`, and everything
else follows from it:

**`pip install MetaTrader5` on Linux does NOT create a working MT5.**
The `MetaTrader5` Python package ships Windows-only wheels; on Linux the
import fails outright — and even where the import succeeds there is no
terminal behind it unless you provide one. Without a real terminal,
every broker operation raises `BrokerError(BROKER_UNAVAILABLE)` with an
explanatory message: never fake data, never a silent simulation.

So where does MT5 actually run? Exactly one of:

1. **A Windows host** with the official terminal (`terminal64.exe`),
   logged in. `MT5Adapter` talks to it directly — the only config where
   `import MetaTrader5` happens locally (`broker/mt5/adapter.py`, and
   only under `broker/mt5/` per principle 8).
2. **A Wine-compatible runtime** that can run `terminal64.exe` —
   same code path as (1), the terminal is just emulated.
3. **A remote gateway host** (Windows, or anything that fronts a real
   terminal) running the MT5 Gateway: a small HTTP service whose
   transport contract is specified byte-for-byte in
   `broker/mt5/gateway_contract.md` — **12 operations only**, under
   `/api/v1/...`: `ping, terminal, account, symbols, market_data, quote,
   positions, orders, submit, modify, close, deal_history`. Bearer auth
   is mandatory (`MT5_GATEWAY_TOKEN` must be set — the transport refuses
   to construct without it) and the allowlist is total: any operation
   name not in `OPERATIONS` raises `GATEWAY_REJECTED` before any network
   I/O. There is no "run arbitrary command" op.

In code: `broker/mt5/gateway.py` defines the `MT5Transport` ABC and the
`RemoteMT5GatewayTransport` implementation (env: `MT5_GATEWAY_URL`,
`MT5_GATEWAY_TOKEN`). The `BrokerAdapter` for MT5 is selected by
`BROKER_PROVIDER=mt5`; it degrades honestly through the structured
`broker_status()` flags (`configured ≠ reachable ≠ connected ≠
trading_available`) — see §10 and `scripts/forex broker-status`.

## 10. Execution gateway: lifecycle + broker guard

`core/execution/gateway.py` is the only path from "the agent wants a
trade" to "an order leaves this machine". `core/execution/broker_guard.py`
wraps the adapter in a `GatewayOnlyAdapter` that raises
`BrokerError(GATEWAY_BYPASS_ATTEMPTED)` on any write attempt made
**outside** `execution_scope()` — a scope only the gateway enters. A
tool, daemon, or agent can never touch the broker directly for trades,
whatever the natural-language instruction said.

Every trade moves through an explicit lifecycle, persisted per
`request_id`:

```
requested → validated → risk_checked → approved → submitted
        → broker_acknowledged → executed
```

Terminal states: `rejected`, `failed`, `dry_run_simulated`,
`duplicate_suppressed`. The state `executed` is reported **only after
broker confirmation** (principle 6); in dry-run mode the terminal state
is `dry_run_simulated` — never `executed`.

- **Idempotency:** `request_id` is persisted in the SQLite
  `execution_idempotency` table (survives restarts). A repeated
  `request_id` returns the *original* `GatewayDecision` with state
  `duplicate_suppressed` — never a second execution. One fill per
  `signal_id`, ever.
- **Kill switch:** engaged → `request_trade` and `modify_position` are
  rejected; `close_position` is still allowed — closing reduces risk,
  and blocking a close would trap the agent in a losing position. The
  latch persists across restarts and is fail-closed (unreadable latch =
  engaged).
- **Modify validation:** direction-aware SL tightening only — a BUY
  stop may move up, a SELL stop may move down, never loosened.

## 11. Event delivery (SSE) + notification channels

The agent is pushed structured events over a localhost SSE stream; it
never polls "do we have a signal?":

- `GET http://127.0.0.1:8765/events` with
  `Accept: text/event-stream` (port overridable via `FOREX_API_PORT`;
  bind is **127.0.0.1 only**). Frames: `id:` / `event:` / `data:` —
  `data:` is one JSON line with `event_id`, `severity`, `timestamp`,
  `payload`. `: ping` keep-alive every 15 s.
- `?resume_from=<event_id>` replays everything journaled after it,
  oldest first, then streams live. Unknown ids → HTTP 400. Combine
  with `?ack=<event_id>` for cumulative delivery marking (delivery
  marker only — replays may still return acked events; dedup on
  `event_id`).
- Poll fallback: `GET /events/latest?since=<event_id>&limit=N`; CLI:
  `scripts/forex events --since-id <id>` / `--follow`.
- Backed by the persistent SQLite `event_journal` (durable FIFO, dedup
  on `INSERT OR IGNORE`); daemons publish through it cross-process.
- Severities `INFO/NOTICE/WARNING/CRITICAL` with a per-event-type
  mapping (`broker.disconnected` and kill-switch activation are
  CRITICAL). Full spec: `docs/EVENTS.md`.

Notifications (`agent/notifications/`) fan events out to optional sinks
without ever touching the trading path. `NotificationChannel` ABC;
`AgentChannel` is primary (always on — it *is* the journal/SSE);
`WorkerChannel`, `FCMChannel` (optional/legacy, unconfigured by
default), `WebhookChannel` are optional and severity-routed. Dispatcher:
`scripts/forex notify --run` / `--status` / `--once`. One channel
failing never blocks the others. Spec: `docs/NOTIFICATIONS.md`.

## 12. Capability contract

`agent/capabilities.json` is the machine-readable contract of
everything above — interfaces, lifecycle commands, installer status
vocabulary, broker-status fields, modes. It is **generated** from the
live tool registry by `scripts/gen_manifest.py` and verified with
`scripts/gen_manifest.py --check` (the generator is the source of
truth; never hand-edit the JSON). The installer speaks it too:
`installer/install.sh --agent` (alias `--json`) prints and stores a
structured `install-result.json` whose `status` (`installed`,
`configured`, `operational`, `broker_disconnected`, `broker_connected`,
`trading_disabled`, `trading_ready`, `needs_credentials`, `error`) is
derived from real probes — never invented. Full contract overview:
`docs/CAPABILITY.md`.
