# agent/ API_DEPS — cross-area dependencies

Every import that `agent/` (event bus + tools) makes outside its own
package lives behind `agent/tools/backend.py` (lazy, override-injectable
for tests). This file records the exact surface the interface-builder
depends on, per sibling area. If you change one of these signatures,
this file (and `tests/test_iface_*.py`) must be updated in the same
commit.

Conventions enforced by tests:
- `MetaTrader5` is never imported anywhere under `agent/` (AST test).
- `agent/tools/trading.py` never imports the `broker` package at all
  (AST test) — trade-affecting calls go through `core.execution` only.
- Read-only tools may use the `BrokerAdapter`; nothing else in `agent/`
  touches it.

## storage (storage/__init__ -> Store)

Local SQLite. Used by the event bus (dual-write) and by tools for
kill-switch latch / risk state / experience journal.

- `Store(path=None)` — `path` defaults to `FOREX_AGENT_STORAGE` env or the
  standard data dir.
- `enqueue_event(event: dict) -> int` — durable FIFO queue. The bus
  `publish()` writes every validated event here. Drained by the
  health_monitor daemon via `dequeue_events(limit)` for Worker sync.
- `dequeue_events(limit=100) -> list[dict]` — destructive read (sync side).
- `journal_add(entry: dict) -> int` — the bus writes
  `{"kind": "event", "payload": <event dict>}` here; `ExperienceStore`
  writes `{"kind": <experience kind>, "symbol", "direction", "outcome",
  "tags", "detail"}`.
- `journal_query(limit=100, kind=None, symbol=None, direction=None,
  outcome=None, since=None) -> list[dict]` — newest-first; each row gains
  `_journal_id` and `_ts` (ISO-8601). The bus `poll()` reads
  `kind="event"`; `ExperienceStore.query` passes kind/symbol through.
- `get_kill_switch() -> dict` — `{"engaged": bool, "source": str|None,
  "ts": str|None}`. Source of truth for the latch.
- `set_kill_switch(engaged: bool, source=None)`.
- `get_risk_state() -> dict` / `set_risk_state(patch: dict)` — persisted
  risk-engine state (daily-loss baseline, loss streak).
- `audit(record: dict) -> int|None` / `query_audit(limit=100, since=None)` —
  used by the gateway for trade decisions (not called directly by agent/).

## broker (broker/__init__)

- `BrokerAdapter` (ABC). Read-only tools use: `candles(symbol, timeframe,
  count) -> list[Candle]` (CLOSED candles only), `positions() ->
  list[Position]`, `account_info() -> AccountInfo`, `symbols(names=None)
  -> list[SymbolSpec]`, `health() -> BrokerHealth`. Constructed via
  `config.broker.provider`: `"mt5"` -> `broker.mt5.adapter.MT5Adapter`
  (Linux-safe; raises `BrokerError` without a terminal), anything else ->
  `broker.disconnected.DisconnectedAdapter`.
- Dataclasses: `AccountInfo(balance, equity, currency, margin,
  free_margin, leverage, login, server)`, `SymbolSpec(name, volume_min,
  volume_max, volume_step, tick_value, tick_size, contract_size, digits,
  point)`, `Position(ticket, symbol, side, volume, entry_price,
  current_price, profit, swap, commission, magic, comment, open_time)`
  with `.is_own` (magic-number filter) and `.signal_id` (parsed from
  comment), `BrokerHealth(connected, adapter, server_time, ...)`.
- `BrokerError` carries `.code` (e.g. `BROKER_UNAVAILABLE`,
  `INVALID_SYMBOL`, `MARKET_CLOSED`) and `.message`.

## config (config.config)

- `load_config() -> AppConfig`. Used fields: `.mode` (`"dry_run"` default),
  `.broker.provider/.login/.password/.server/.terminal_path`,
  `.trading.symbols/.timeframes/.ema_rsi`, `.risk` (RiskConfig),
  `.worker.base_url/.api_key/.enabled`.

## core.execution (core.execution.gateway / .kill_switch)

THE only path for trade-affecting calls. `backend.gateway()` builds one
`ExecutionGateway(config, adapter, risk_manager, kill_switch, store)`.

- `TradeRequest(signal_id, symbol, direction, stop_loss, take_profit,
  entry_price=None, volume=None, idempotency_key=None, source="agent",
  requested_at=None)` — `signal_id`, `symbol`, `direction`, `stop_loss`,
  `take_profit` required; `idempotency_key`/`requested_at` auto-filled.
- `ExecutionGateway.request_trade(req: TradeRequest) -> GatewayDecision`.
- `GatewayDecision(approved, reason, reason_code, idempotency_key,
  volume=0.0, ticket=None, price=None, audit_id=None,
  audit_pending=False, notes=[])`.
- `KillSwitch(store, adapter)`: `engage(source="agent") /
  disengage(source="agent") -> {"engaged","source","ts"}`;
  `is_engaged() -> bool` (fails CLOSED); `state()`; `close_all()`.
  `engage()` emits `kill_switch.activated` via `core.events.emit`
  (forwarded to the agent bus); disengage is silent — the agent tool
  emits `kill_switch.cleared`.
- **MISSING (execution builder)**: the gateway exposes NO `close_position`
  / `modify_position` API. `forex.close_position` and
  `forex.modify_position` therefore return `DEPENDENCY_UNAVAILABLE`
  rather than bypass the gateway. When the execution builder adds these,
  wire them in `agent/tools/trading.py`.

## core.risk (core.risk.engine)

- `RiskManager(config: RiskConfig, store=None)` — constructed inside
  `backend.gateway()`; agent tools read standing state via
  `store.get_risk_state()`, not via the manager.
- Constants `DRY_RUN_BLOCKED`, `KILL_SWITCH_ENGAGED` (reason codes).

## core.indicators / core.market / core.smc / core.strategies / core.signals

- `core.indicators`: `ema(values, period)`, `rsi(values, period=14)`,
  `atr(highs, lows, closes, period=14)` — each returns a list aligned to
  the input (leading `None`s); callers use the last element.
- `core.market.Candle` dataclass `(time, open, high, low, close, volume)`
  with `.to_dict()/.from_dict()`.
- `core.smc` (duck-typed candles): `find_swing_points`,
  `analyze_market_structure`, `detect_structure_breaks`,
  `detect_trend_lines`, `detect_support_resistance_zones`,
  `detect_liquidity_zones`, `detect_fair_value_gaps`, `detect_order_blocks`.
- `core.strategies.EmaRsiStrategy(cfg.trading.ema_rsi, timeframe).evaluate(
  symbol, candles) -> Signal | None`.
- `core.signals.Signal` dataclass (converted to plain dicts via
  `dataclasses.asdict` before leaving the tool layer).

## core.performance

- `compute_performance(adapter, lookback_days=30) -> PerformanceSnapshot`
  (honest stats from real closed-deal history; `win_rate_pct=None` when
  there are no closed trades).
- `snapshot_to_dict(snapshot) -> dict`.

## core.events

- `emit(event: dict)` — validates and forwards to
  `agent.events.bus.publish`; falls back to an in-process queue when the
  bus is unavailable. This is how gateway/kill-switch events land in the
  agent journal.

## intelligence.*

- `intelligence.correlation.knowledge`: `current_session_info(now=None)
  -> {"active_sessions": [...], "liquidity_advisory": str}`,
  `correlated_pairs(symbol) -> list`, `check_correlated_exposure(
  new_signal: {"symbol","direction"}, open_positions: list[dict]) -> list`.
  (The real port of the old `knowledge.js`; the duplicate
  `agent/tools/knowledge.py` was removed.)
- `intelligence.experience.store.ExperienceStore(store)`: `record(kind,
  *, symbol, direction, outcome, payload, tags)`, `query(kind, symbol,
  direction, outcome, since, limit)`. Kinds include `"reflection"`.
- `intelligence.economic_calendar.provider.get_calendar(symbol=None,
  hours_ahead=24, provider=None) -> {"available": bool, "events": [...] |
  "reason": str}`. `CalendarProvider` ABC (`fetch(from_, to) ->
  [CalendarEvent]`). No provider is configured -> honest
  `available:false`; a provider can be injected in tests via
  `backend.set_override("calendar_provider", provider)`.
- `intelligence.ml.predict.get_ml_prediction(features=None, model=None)
  -> {"available": False, "reason": "no trained model deployed"}` —
  serving deliberately PARKED (no dataset/model); the agent passes it
  through and never treats absence as a negative signal.

## Areas agent/ deliberately does NOT touch

- `broker/mt5/` internals (only `broker_adapter()` constructs the adapter).
- `daemon/` processes (they are separate OS processes; the agent reads
  their PID files only for `forex.get_health`).
- Worker cloud API (best-effort reachability check in `get_health` only;
  sync is the daemon's job; Worker outage never affects local safety).
