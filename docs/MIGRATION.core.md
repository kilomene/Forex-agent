# MIGRATION notes — core, broker, config

Where each original bridge module went, what was kept, what was fixed,
and what was deliberately not ported. Originals live read-only in
`~/workspace/forex-migration/original/forex-bot-bridge/`.

## config.py → config/ — REFACTORED (surface kept, secrets hardened)

`config/defaults.yaml` + `config/config.py`. Every original key kept
(strategy, risk, exits, worker, notifications, charts); new keys:
`mode: dry_run` (default — the original had no dry-run concept),
`kill_switch.*`, `events.*`, `performance.*`. Secrets (MT5 creds, worker
URL/token) moved out of the yaml into environment variables or a
`0600` secrets file; `AppConfig.redacted()` for logging. Live MT5 mode
fails fast without credentials (`CREDENTIALS_INVALID`).

## indicators.py → core/indicators/ — KEPT (verbatim)

EMA/RSI/ATR ported verbatim, pure functions, tested against the
original's expectations.

## smc_analysis.py → core/smc/ — KEPT (verbatim)

Deterministic SMC ported verbatim.

## signal_engine.py → core/strategies/ + core/signals/ — REFACTORED (repaint fixed)

Strategy logic ported; **the repaint bug is fixed**: `evaluate()`
excludes the final (forming) candle, so a violent intra-candle spike
can no longer trigger a signal the closed candle wouldn't confirm. A
regression test proves the old behavior would have fired and the new
one doesn't. Signal lifecycle + dedup-by-`(symbol, candle_time)` from
`main.py` lives in `core/signals/`.

## chart_data.py → core/market/ — REFACTORED (closed-candle contract)

`Candle` dataclass + chart payload packaging; only closed candles feed
the strategy.

## mt5_client.py + scattered direct MT5 calls → broker/mt5/adapter.py — REFACTORED (quarantined)

The `MT5Client` class AND the direct `mt5.*` calls that used to live in
`exit_manager.py`, `reconciliation.py`, `performance_review.py`, and
`cost_tracking.py` are all absorbed into `MT5Adapter` behind the
`BrokerAdapter` interface. `import MetaTrader5` exists exactly once in
the new tree (`broker/mt5/adapter.py`, optional/degraded). Nothing else
in core may touch MT5.

## mt5_shared.py → broker/__init__.py + broker/mt5/shared.py — KEPT (split)

`MAGIC_NUMBER = 20260817`, the `approved:<signal_id>` comment
convention, and `extract_signal_id_from_comment` / `is_own_position`
are now first-class in the `broker` package so every consumer (risk,
positions, performance, reconciliation) filters identically.

## risk.py → core/risk/engine.py — REFACTORED (three holes fixed)

1. Daily loss is computed on **equity** (including floating), not balance.
2. Volume is floored/clamped to the broker's `volume_min/max/step` grid.
3. Position counts and exposure include **only own-magic** positions.
Plus: persisted day/equity-baseline/outcome-streak via the store
contract (consecutive-loss breaker survives restarts), deterministic
correlation limits via `intelligence.correlation` (canonical port,
owned by the intelligence-builder).

## kill_switch.py → core/execution/kill_switch.py — REFACTORED (fail-closed latch)

Persistent idempotent latch (original ts/source kept on re-engage),
fail-closed reads, bot-magic-filtered `close_all()` with per-ticket
results, `kill_switch.activated` event. Now sits **below** the execution
gateway so no agent path can bypass it.

## (new) core/execution/gateway.py — NEW (the choke point)

Did not exist as a unit in the original: order placement was inline in
`main.py`. Now every trade flows agent → `TradeRequest` → gateway →
risk/safety → broker, with idempotency keys, duplicate-signal
prevention, dry-run blocking, and a full audit trail. There is no other
path to `submit_order()`.

## exit_manager.py → core/positions/ — REFACTORED (adapter-driven)

Time exit → trailing → breakeven precedence kept verbatim in behavior;
the "SL only tightens, never loosens" rule is now enforced in one
`_tighten_sl()` helper with tests proving a worse stop is never sent.
All MT5 calls go through `BrokerAdapter` (`positions`,
`modify_order`, `close_position`, `candles` for ATR). Close reporting
goes to an `on_close(signal_id, reason, price, costs)` callback +
`position.closed` event instead of the Worker client directly.

## performance_review.py + cost_tracking.py → core/performance/ — REFACTORED (adapter-driven)

`PerformanceSnapshot` fields unchanged; stats still come from real deal
history filtered by magic number; commission still summed over entry
AND exit deals. Now via `adapter.deal_history()`; `get_position_costs`
takes the adapter instead of calling MT5 directly.

## reconciliation.py → core/reconciliation/ — REFACTORED (adapter-driven)

Same gap closed (externally-closed positions reported via the closing
deal matched on `approved:<signal_id>`); now via `adapter.positions()`
and `adapter.deal_history()`. Takes the believed-open signal set as an
argument (the daemon reads it from the store journal) and reports via
`on_close` + `position.external_close` instead of the Worker client.

## worker_client.py → NOT ported (superseded)

The Worker is the cloud/sync layer, owned by the worker-builder. Its
two uses from core code — `report_closed` and `get_executed_signals` —
are replaced by the `on_close` callback + event emission (positions,
reconciliation) and the store journal. No core module imports anything
worker-related.

## ml_features.py / train_model.py → NOT ported (intelligence-builder owns ML)

Feature schema, training, and serving live in `intelligence/ml/`.

## backtest.py / test_backtest.py → NOT ported (intelligence-builder owns backtesting)

Backtesting lives in `backtesting/`. The closed-candle contract in
`core/market` is what the backtester consumes.

## main.py → NOT ported as a unit (daemon-builder owns the loop)

`main.py`'s loop responsibilities are split by owner: signal dedup →
`core/signals`, risk → `core/risk`, order placement → the new
`core/execution` gateway, exit pass → `core/positions`, reconciliation
→ `core/reconciliation`, event fan-out → `agent.events.bus`
(interface-builder). The daemon-builder wires these together.

## Deliberately dropped

- Mobile-app push paths (the mobile app is not a dependency).
- Any hosted-LLM call (there is no hosted LLM in the new system).
- `test_indicators.py` / `test_smc_analysis.py` originals → replaced by
  `tests/test_core_indicators.py` / `tests/test_core_smc.py`.
