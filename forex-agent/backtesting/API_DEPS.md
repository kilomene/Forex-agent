# API_DEPS — backtesting/

Cross-area imports made by `backtesting/` (owner: intelligence-builder).
`import MetaTrader5` appears nowhere here — data arrives only via the adapter.

| Import | From | Expected signature / contract |
|---|---|---|
| `from broker import BrokerAdapter, BrokerError` | `backtesting/data.py` | `BrokerAdapter.candles(symbol: str, timeframe: str, count: int = 200) -> list[Candle]` — CLOSED candles only, oldest→newest (TARGET §2). `BrokerError(code, message, detail)` with structured codes incl. `BROKER_UNAVAILABLE`, `INVALID_SYMBOL`, `CONFIG_INVALID`. Owner: broker-builder. |
| strategy object | `backtesting/engine.py::run_backtest(adapter, strategy, ...)` (injected by caller) | `strategy.evaluate(symbol: str, candles) -> Signal \| None`. Contract (core/strategies.Strategy): `candles` oldest→newest; the LAST element is treated as forming and excluded from signal logic. Real impl: `core.strategies.EmaRsiStrategy` (owner: core-builder). Any object with `.evaluate(symbol, candles)` — or a plain callable `(symbol, candles)` — is accepted (duck-typed, no isinstance gate). |
| signal (duck-typed) | returned by the strategy | attributes: `direction` ("BUY"/"SELL"), `entry_price`, `stop_loss`, `take_profit`, `candle_time` (must match a candle in the evaluation window), `ema_fast`, `ema_slow`, `rsi_value`, `atr_value`, `smc_summary` (dict with `available` + `market_structure.trend`), `strategy` (name). Matches `core.signals.Signal`. |
| `from intelligence.ml import FEATURE_COLUMNS` | `backtesting/engine.py::extract_features` (function-local import) | the canonical 8-feature schema list; `extract_features` asserts its output keys match it exactly (schema-drift tripwire). |

Deliberately NOT imported: `core.execution`, `core.risk`, `broker.mt5`,
`MetaTrader5`, `config` (only lazily inside `engine.main()` CLI for
`EmaRsiStrategyConfig`, with documented-default fallback).

Structural note: `run_backtest` wraps the adapter in `_ReadOnlyAdapter`
before any use; `submit_order`/`modify_order`/`close_position` raise
`BacktestError("ORDER_BLOCKED")`. Backtesting never calls them by
construction (only `candles()` is used for data).
