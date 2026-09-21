# Daemon dependency contracts

Every daemon is a separate OS process (`python3 -m daemon.<name>`).
Daemons never import each other; they coordinate through the local event
bus and the SQLite store. Nothing here may import `MetaTrader5`
(even transitively) — broker access goes only through the `broker`
adapter interface.

## daemon.common

| Helper | Contract |
|---|---|
| `get_home()` | `$FOREX_AGENT_HOME` or `~/.forex-agent`. PID files, logs. |
| `acquire_pidfile(name)` | Idempotent; stale PIDs reclaimed. `False` = already running. |
| `read_pid(name)` | Live PID or `None`. |
| `load_config()` | `config.config.load_config()` — env/YAML, validated. |
| `get_store()` | `storage.Store()` — path from `$FOREX_AGENT_STORAGE` (default `<home>/storage/local.db`; note: the default is resolved at `storage.store` import time). |
| `get_adapter(config)` | Reuses `agent.tools.backend.broker_adapter()` — the same construction as the agent tools. `DisconnectedAdapter` when no broker is configured; daemons must treat that as "no data", never as an error that stops the loop. |
| `get_daemon_state(store, name)` / `save_daemon_state(store, name, state)` | Durable per-daemon blob in the journal (`kind="daemon_state"`, `symbol=<name>`). Survives restarts; used for last-seen markers and dedupe. |
| `Daemon.run_forever()` | Claims pidfile, publishes `daemon.started`, loops `run_once()` every `interval`, publishes `daemon.stopped` on SIGTERM/SIGINT. A crashing `run_once()` is logged and the loop continues. |

## daemon.worker_client — Worker (cloud/sync only)

Built from `AppConfig.worker`: `base_url`, `api_key` (Bearer), `enabled`.
Contract derived from `forex-signal-worker/src/index.js` + `src/auth.js`.

| Method | Worker endpoint | Validated response shape |
|---|---|---|
| `get_kill_switch()` | `GET /kill-switch` | `{"engaged": bool}` |
| `set_kill_switch(bool)` | `POST /kill-switch` | 2xx JSON |
| `post_signal(signal)` | `POST /signals` | 2xx JSON; skips (no HTTP) when required fields are missing |
| `post_performance_snapshot(snap)` | `POST /performance/snapshot` | `{"ok": bool}` |
| `get_latest_performance()` | `GET /performance/latest` | `{"available": bool}` |
| `sync_events(batch)` | mapped (see below) | `{"sent": n, "skipped": m}` |

Event → endpoint mapping in `sync_events`: `signal.detected` → `POST /signals`;
`performance.snapshot` → `POST /performance/snapshot`; `kill_switch.activated/cleared`
→ `POST /kill-switch`. Every other event type has no cloud endpoint and is
counted as `skipped`, never failed.

Failure semantics: any transport error, non-2xx status, non-JSON body, or
shape mismatch raises `WorkerUnavailable` (`.code == "WORKER_UNAVAILABLE"`)
after up to 3 attempts with exponential backoff. Nothing else ever escapes
a public method. **A Worker outage never changes local state** — callers log
`WORKER_UNAVAILABLE` and continue; the local kill-switch latch is always
authoritative over the cloud mirror.

## Daemon responsibilities

| Daemon | Interval source | Does | Never does |
|---|---|---|---|
| `market_monitor` | 60s | Fetches candles per symbol/timeframe; publishes `market.candle_closed` on new closed candles; publishes `market.data_stale` when a feed stalls (>3× interval). Read-only. | Trade, touch positions. |
| `signal_monitor` | 60s | Polls `market.candle_closed` since last run; runs `EmaRsiStrategy.evaluate` (pure); publishes `signal.detected` with deterministic id `sig:<SYM>:<TF>:<candle_time>`, deduped across restarts. | Trade, approve, or auto-execute. Detection only — the agent decides. |
| `position_monitor` | `exits.check_interval_seconds` (60s) | Runs `core.positions.manage_open_positions`: breakeven/trailing tightening and time-based closes on **own** positions only. Core emits `position.closed`. | Touch foreign positions; loosen stops; add exposure. |
| `health_monitor` | `kill_switch.check_interval_seconds` (10s) | Fail-closed kill-switch latch poll (unreadable ⇒ engage); cloud mirror check (informational); broker/sibling-daemon/disk checks; `health.check` on change + 5-min heartbeat; drains the event queue to the Worker; performance sync every `performance_review.interval_hours` (6h) → journal `kind="performance_snapshot"` + `POST /performance/snapshot`. | Change the latch based on the cloud; drop events (persistent Worker failure ⇒ journal `kind="worker_dead_letter"`). |

## Cross-area signatures used

- `broker.BrokerAdapter.candles(symbol, timeframe, count)` → `List[core.market.Candle]`
- `core.strategies.EmaRsiStrategy(EmaRsiStrategyConfig, timeframe).evaluate(symbol, candles)` → `Optional[core.signals.Signal]`
- `core.positions.manage_open_positions(adapter, ExitManagerConfig, timeframe, on_close)` → stats dict
- `core.performance.compute_performance(adapter, lookback_days)` → snapshot; `snapshot_to_dict(snapshot)` → JSON-safe dict
- `agent.tools.backend.gateway().kill_switch.{is_engaged,engage,disengage}`
- `agent.tools.backend.broker_health()` → `{"connected": bool, ...}`
- `agent.tools.backend.broker_adapter()` → `BrokerAdapter`
- `agent.events.bus.publish(event)` / `bus.poll(since)` / `bus.configure(store)`
- `storage.Store`: `dequeue_events(limit)`, `journal_add(entry)`, `journal_query(kind, symbol, limit)`

## Environment

- `FOREX_AGENT_HOME` — agent home (default `~/.forex-agent`): `run/*.pid`, `log/*.log`.
- `FOREX_AGENT_STORAGE` — SQLite path (default `<home>/storage/local.db`).
- `WORKER_ENABLED`, `WORKER_BASE_URL`, `WORKER_API_KEY` — cloud sync (all optional; everything works offline).

## Supervision

- `scripts/forex-daemons {start|stop|restart|status} [names...]` — idempotent PID-file supervisor.
- `daemon/systemd/forex-*.service` + `forex-agent.target` — systemd units (user `forex`, `/opt/forex-agent`, `/var/lib/forex-agent`). The installer wires these up.
