# core/ API_DEPS — cross-area imports owned by CORE-BUILDER

Every import from `core/*` to a module outside `core/`, with the exact
contract core relies on. If you own the other side, this is what you
must not break. Imports run with `PYTHONPATH=forex-agent` and use
top-level package names (`broker`, `config`, `core`, `agent`, …).

## broker/ (owned by CORE-BUILDER — internal, stable)

| Imported by | Names | Contract |
|---|---|---|
| `core/risk/engine.py` | `BrokerAdapter`, `BrokerError`, `INVALID_ORDER`, `MAX_EXPOSURE`, `RISK_LIMIT_EXCEEDED`, `DAILY_LOSS_LIMIT`, `is_own_position` | `BrokerError(code: str, message: str, detail: dict \| None)`; `is_own_position(magic: int \| None) -> bool` (True iff magic == 20260817); adapter methods used: `account_info() -> AccountInfo(balance, equity, currency)`, `positions() -> list[Position]` (`Position`: ticket, position_id, symbol, direction "BUY"/"SELL", volume, price_open, price_current, sl, tp, profit, swap, magic, comment, time_open; `.is_own`, `.signal_id`), `symbols(names) -> list[SymbolSpec]` (volume_min/max/step, tick_value, tick_size) |
| `core/execution/gateway.py` | `BrokerAdapter`, `BrokerError`, `BrokerHealth`, `OrderRequest`, `INVALID_ORDER`, `INVALID_SYMBOL`, `MARKET_CLOSED`, `RISK_LIMIT_EXCEEDED` | `OrderRequest(symbol, direction, volume, stop_loss, take_profit, signal_id=None, comment=None, idempotency_key=None)` with `.order_comment() -> str` (`approved:<signal_id>` convention); `submit_order(req) -> OrderResult` (ticket, symbol, direction, volume, price, retcode) — broker-confirmed only, raises `BrokerError` on reject; `quote(symbol) -> Quote \| None` (`Quote`: bid, ask, time; `.spread`); `modify_order(ticket, sl, tp) -> None`; `close_position(ticket) -> float` (close price) |
| `core/execution/kill_switch.py` | `BrokerAdapter` (constructor arg, may be None) | `positions()`, `close_position(ticket)` only |
| `core/positions/__init__.py` | `BrokerAdapter`, `Position` | `positions()`, `modify_order(ticket, sl, tp)`, `close_position(ticket) -> float`, `candles(symbol, timeframe, count) -> list[Candle]` (closed candles, oldest→newest; `Candle`: time, open, high, low, close) |
| `core/performance/__init__.py` | `BrokerAdapter`, `is_own_position` | `deal_history(from_: datetime, to: datetime, position_id: int \| None = None) -> list[Deal]`; `Deal`: ticket, position_id, symbol, direction, entry "IN"/"OUT", volume, price, profit, commission, swap, magic, comment, time |
| `core/reconciliation/__init__.py` | `BrokerAdapter`, `extract_signal_id_from_comment`, `is_own_position` | `extract_signal_id_from_comment(comment: str) -> str \| None` (parses `approved:<signal_id>`); `positions()`, `deal_history(...)` as above |

## config/ (owned by CORE-BUILDER — internal, stable)

| Imported by | Names | Contract |
|---|---|---|
| `core/execution/gateway.py` | `AppConfig` | `.dry_run: bool` (default True); `.mode: str`; `.trading.symbols: list[str]` (whitelist), `.trading.timeframe: str`; `.risk.max_spread_points: float` (0 disables), `.risk.min_account_equity: float` (0 disables) |
| `core/risk/engine.py` | `RiskConfig` | `.fixed_lot_size`, `.use_percent_risk_sizing`, `.max_risk_per_trade`, `.max_daily_loss`, `.max_open_positions`, `.max_total_exposure_lots`, `.max_consecutive_losses`, `.max_correlated_positions`, `.require_stop_loss`, `.max_spread_points`, `.min_account_equity` |
| `core/strategies/__init__.py` | `EmaRsiStrategyConfig` | `.ema_fast`, `.ema_slow`, `.rsi_period`, `.rsi_oversold`, `.rsi_overbought`, `.atr_period`, `.atr_sl_multiplier`, `.atr_tp_multiplier`, `.confirm USE_SMC` (bool), `.min_candles` |
| `core/positions/__init__.py` | `ExitManagerConfig` | `.max_hold_hours`, `.breakeven_trigger_atr`, `.trailing_activation_atr`, `.trailing_atr_multiplier` |

## agent.events.bus (owned by interface-builder — EXTERNAL)

| Imported by | Names | Contract |
|---|---|---|
| `core/events.py` | `publish` (lazy import inside `_forward()`) | `publish(event: dict) -> dict`; raises `ImportError` when the bus isn't installed, `ValueError` on schema violation, `RuntimeError` when storage is unavailable. **core never lets a bus failure break a producer**: any exception → event appended to the module-level in-process queue (`drain_queue()` for the bus owner to forward later). |

The real schema lives at `agent/events/bus.py::EVENT_SCHEMAS`
(required/optional fields per event name; unknown names rejected).
core emits only schema-conformant events:

| Event | Required fields core sends |
|---|---|
| `trade.requested` | symbol, side, volume, idempotency_key (+ signal_id, stop_loss, take_profit, requested_by) |
| `trade.executed` | symbol, ticket, side, volume (+ idempotency_key, price, signal_id) |
| `trade.rejected` | reason (+ symbol, signal_id, detail{message, volume, idempotency_key, source}) |
| `risk.blocked` | reason (+ symbol, signal_id, detail{message, idempotency_key}) |
| `kill_switch.activated` | source |
| `position.closed` | ticket, symbol, reason (+ close_price, profit) |
| `position.external_close` | ticket (= position_id), symbol (+ note=`reconciled_external_close:<signal_id>`) |

`core/events.py::emit()` also stamps `ts` (ISO-8601 UTC) when missing.

## intelligence.correlation (owned by intelligence-builder — EXTERNAL)

| Imported by | Names | Contract |
|---|---|---|
| `core/risk/correlation.py` | `CORRELATION_REFERENCE`, `check_correlated_exposure`, `correlated_pairs` | Canonical port of the forex-signal-worker correlation knowledge. `check_correlated_exposure(new_signal, open_positions) -> list[str]` — `new_signal` has `.symbol`/`.direction` (or dict equivalents); `open_positions` are `broker.Position`. Returns human-readable flags; the canonical warning wording contains "Correlated exposure: open LONG/BUY … is positively/negatively correlated … effectively doubling the same directional bet rather than diversifying." Falls back to an identical-logic local port if `intelligence.correlation` isn't importable (standalone checkouts). |

## storage.Store (owned by storage-builder — EXTERNAL, duck-typed)

core takes a `store` object; it is never imported. Expected surface:

| Used by | Method | Contract |
|---|---|---|
| `RiskManager` | `get_risk_state() -> dict`, `set_risk_state(patch: dict) -> None` | Persisted keys: `day` (ISO date), `start_of_day_equity`, `recent_outcomes` (list[bool]) |
| `KillSwitch` | `get_kill_switch() -> dict`, `set_kill_switch(engaged: bool, source: str \| None) -> None` | Persisted keys: `engaged`, `source`, `ts` |
| `ExecutionGateway` | `audit(record: dict) -> int \| None` | Returns an audit id; any exception → decision still returned with `audit_pending=True` |

## What core provides to others

- `core.signals.Signal` — dataclass: symbol, timeframe, direction ("BUY"/"SELL"), entry_price, stop_loss, take_profit, ema_fast, ema_slow, rsi_value, atr_value, candle_time (datetime), trigger, strategy, id (optional), confidence, smc_note, chart_payload.
- `core.market.Candle` — time, open, high, low, close, volume.
- `core.indicators` — `ema(values, period)`, `rsi(closes, period)`, `atr(highs, lows, closes, period)` (pure functions, lists in/out).
- `core.strategies.EmaRsiStrategy(cfg).evaluate(symbol, candles) -> Signal | None` (excludes the forming candle).
- `core.risk.RiskManager(cfg, store).check(signal, adapter, kill_switch_engaged=False) -> RiskResult` (`RiskResult`: allowed, reason, reason_code, lot_size, notes).
- `core.execution.ExecutionGateway(config, adapter, risk_manager, kill_switch, store).request_trade(TradeRequest) -> GatewayDecision` — the ONLY path to `adapter.submit_order`.
- `core.execution.KillSwitch(store, adapter=None)` — `engage(source)`, `disengage(source)`, `is_engaged()` (fail-closed), `close_all() -> dict`, `state()`.
- `core.positions.manage_open_positions(adapter, exit_cfg, timeframe, on_close=None) -> dict` stats.
- `core.performance` — `compute_performance(adapter, lookback_days)`, `get_recent_outcomes(adapter, limit, lookback_days)`, `get_position_costs(adapter, position_id)`, `PerformanceSnapshot`, `snapshot_to_dict`.
- `core.reconciliation` — `reconcile(adapter, believed_open_signal_ids, on_close, lookback_days)`, `get_open_signal_ids(adapter)`, `find_closing_deal(adapter, signal_id, lookback_days)`.
- `core.events` — `emit(event)`, `drain_queue(limit)`, `queued_count()`, `validate(event)`.
