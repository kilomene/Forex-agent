# CONFIGURATION — the full config surface

**Scope:** every setting the forex-agent subsystem reads. Field names are
coordinated with `TARGET_ARCHITECTURE.md` §7 and with the actual loader in
`config/` (`defaults.yaml` + `config.py`).

**Precedence:** environment variable > `config/defaults.yaml` > built-in
default.

**Secrets rule (non-negotiable):** secrets live in environment variables or
in the file pointed to by `FOREX_SECRETS_FILE` (must be mode `0600`) —
never in `defaults.yaml`, never in code, never in logs. Secret env vars:
`MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `WORKER_API_KEY`
(and, when wired: `NEWS_CALENDAR_API_KEY`, `ML_MODEL_API_KEY`).

---

## mode — dry-run default

| Key | Env | Default | Notes |
|---|---|---|---|
| `mode` | `MODE` / `DRY_RUN` | `dry_run` | `dry_run` \| `live`. `DRY_RUN=true/1/yes` ⇒ dry_run. |

`dry_run` is the default and blocks ALL live orders at the execution
gateway. Switching to `live` is a deliberate act: edit `mode` (or set
`MODE=live` / `DRY_RUN=false`) **and** provide broker credentials. There is
no casual path to live trading.

## broker

| Key | Env | Default | Notes |
|---|---|---|---|
| `broker.provider` | `BROKER_PROVIDER` | `mt5` | `mt5` \| `disconnected` |
| — | `MT5_LOGIN` * | — | required in live mode |
| — | `MT5_PASSWORD` * | — | required in live mode |
| — | `MT5_SERVER` * | — | e.g. `Exness-MT5Trial8`; required in live mode |
| — | `MT5_TERMINAL_PATH` | — | optional path to `terminal64.exe` |
| — | `MT5_GATEWAY_URL` | — | remote MT5 gateway host, e.g. `https://mt5-gw.example.com`; when set, `MT5Adapter` routes all operations through the remote gateway transport (see `broker/mt5/RUNTIME.md`, `broker/mt5/gateway_contract.md`) |
| — | `MT5_GATEWAY_TOKEN` | — | bearer token for the remote gateway (**required** when `MT5_GATEWAY_URL` is set; unauthenticated remote use is refused) |
| — | `MT5_ALLOW_NONWINDOWS_RUNTIME` | — | set to `1` to silence the platform warning when running under a Wine-compatible runtime (best effort, unsupported) |

`disconnected` lets the whole subsystem import and run on Linux with no
broker (analysis works; trading calls raise `BROKER_UNAVAILABLE`).

`scripts/forex broker-status` (and `GET /status` on the local API) reports
the structured broker runtime status: `configured` / `reachable` /
`connected` / `account_available` / `market_data_available` /
`trading_available` — field reference in `broker/mt5/RUNTIME.md`.

## account

No separate account section — the account snapshot (balance **and** equity,
currency, margin) comes from `BrokerAdapter.account_info()`. Daily-loss
accounting is done on **equity**, not balance (one of the original `risk.py`
holes, fixed).

## symbols / timeframes

| Key | Env | Default |
|---|---|---|
| `symbols` | `SYMBOLS` (comma-separated) | `[EURUSD, GBPUSD, USDJPY, AUDUSD]` |
| `timeframes` | `TIMEFRAME` | `[H1]` |

## strategies

`strategies.ema_rsi.*` (env override in caps):

| Key | Env | Default |
|---|---|---|
| `strategies.ema_rsi.enabled` | `STRATEGY_EMA_RSI_ENABLED` | `true` |
| `strategies.ema_rsi.ema_fast` | `EMA_FAST` | `20` |
| `strategies.ema_rsi.ema_slow` | `EMA_SLOW` | `50` |
| `strategies.ema_rsi.rsi_period` | `RSI_PERIOD` | `14` |
| `strategies.ema_rsi.atr_period` | `ATR_PERIOD` | `14` |
| `strategies.ema_rsi.rsi_overbought` | `RSI_OVERBOUGHT` | `70.0` |
| `strategies.ema_rsi.rsi_oversold` | `RSI_OVERSOLD` | `30.0` |
| `strategies.ema_rsi.atr_sl_multiplier` | `ATR_SL_MULTIPLIER` | `1.5` |
| `strategies.ema_rsi.atr_tp_multiplier` | `ATR_TP_MULTIPLIER` | `3.0` |

## risk

Fractions are of **equity** (`0.01` = 1%). Position counts cover the bot's
OWN positions only (magic-number filtered — another original hole, fixed).

| Key | Env | Default |
|---|---|---|
| `risk.fixed_lot_size` | `FIXED_LOT_SIZE` | `0.01` |
| `risk.use_percent_risk_sizing` | `USE_PERCENT_RISK_SIZING` | `false` |
| `risk.max_risk_per_trade` | `MAX_RISK_PER_TRADE` (legacy pct: `MAX_RISK_PER_TRADE_PCT`) | `0.01` |
| `risk.max_daily_loss` | `MAX_DAILY_LOSS` (legacy pct: `MAX_DAILY_LOSS_PCT`) | `0.03` |
| `risk.max_open_positions` | `MAX_OPEN_POSITIONS` | `3` |
| `risk.max_total_exposure_lots` | `MAX_TOTAL_EXPOSURE_LOTS` | `1.0` |
| `risk.max_consecutive_losses` | `MAX_CONSECUTIVE_LOSSES` | `4` |
| `risk.max_correlated_positions` | `MAX_CORRELATED_POSITIONS` | `1` |
| `risk.require_stop_loss` | `REQUIRE_SL` | `true` |
| `risk.min_account_equity` | `MIN_ACCOUNT_EQUITY` | `0.0` (disabled) |

Risk state (daily-loss baseline, loss-streak) persists in the local store
(`storage/`), surviving restarts — the original in-memory hole, fixed.

## limits

| Key | Env | Default | Notes |
|---|---|---|---|
| `risk.max_spread_points` | `MAX_SPREAD_POINTS` | `0.0` | `0` = disabled; spread gate in price points |
| (broker-driven) | — | — | order volume is clamped to the broker's `volume_min/max/step` from `BrokerAdapter.symbols()` |

## autonomy

| Key | Env | Default | Notes |
|---|---|---|---|
| `autonomy.mode` | `AUTONOMY_MODE` | `assisted` | `signals_only` \| `assisted` \| `autonomous` |

- `signals_only`: detect + report signals, never request trades.
- `assisted`: request trades; agent/human approves at the gateway input.
- `autonomous`: approved signals flow to the gateway without a human tap.
- The gateway's risk checks apply identically in every mode.

## worker (cloud/sync layer)

| Key | Env | Default |
|---|---|---|
| `worker.enabled` | `WORKER_ENABLED` | `false` |
| `worker.url` | `WORKER_BASE_URL` | `""` |
| `worker.poll_interval_seconds` | `POLL_INTERVAL_SECONDS` | `30` |
| — | `WORKER_API_KEY` * | — |

Worker outage never weakens local safety: risk state, kill-switch latch,
and the event queue live in `storage/` locally; cloud sync is best-effort.

Future provider wiring (env only, no yaml): `NEWS_CALENDAR_URL` (+
`NEWS_CALENDAR_API_KEY`) for the economic-calendar provider interface;
`ML_MODEL_URL` (+ `ML_MODEL_API_KEY`) if ML serving is ever un-parked.

## events

| Key | Env | Default |
|---|---|---|
| `events.enabled` | `EVENTS_ENABLED` | `true` |
| `events.queue_max` | `EVENTS_QUEUE_MAX` | `1000` |

Local event queue lives in `storage/` (`enqueue_event`/`dequeue_events`).
Event schema: `{"event": "signal.detected", "ts", "symbol", "timeframe",
"direction", "confidence", "strategy"}` etc. (see `TARGET_ARCHITECTURE.md`
§4).

## notifications

| Key | Env | Default |
|---|---|---|
| `notifications.enabled` | `NOTIFICATIONS_ENABLED` | `false` |

Delivery is via the Worker (FCM) when `worker.enabled` is true; a local
fallback may deliver without it. The mobile app is never required.

## exits

| Key | Env | Default |
|---|---|---|
| `exits.enabled` | `EXIT_MANAGER_ENABLED` | `true` |
| `exits.max_hold_hours` | `MAX_HOLD_HOURS` | `48.0` |
| `exits.breakeven_trigger_atr` | `BREAKEVEN_TRIGGER_ATR` | `1.0` |
| `exits.trailing_activation_atr` | `TRAILING_ACTIVATION_ATR` | `2.0` |
| `exits.trailing_atr_multiplier` | `TRAILING_ATR_MULTIPLIER` | `1.5` |
| `exits.check_interval_seconds` | `EXIT_CHECK_INTERVAL_SECONDS` | `60` |

## performance

| Key | Env | Default |
|---|---|---|
| `performance.enabled` | `PERFORMANCE_REVIEW_ENABLED` | `true` |
| `performance.interval_hours` | `PERFORMANCE_REVIEW_INTERVAL_HOURS` | `6.0` |
| `performance.lookback_days` | `PERFORMANCE_LOOKBACK_DAYS` | `30` |

## kill_switch

| Key | Env | Default |
|---|---|---|
| `kill_switch.check_interval_seconds` | `KILL_SWITCH_CHECK_INTERVAL_SECONDS` | `10` |

The kill-switch latch persists in `storage/` (`get/set_kill_switch`) and is
enforced below the agent layer; the local copy fails closed.

## charts

| Key | Env | Default |
|---|---|---|
| `charts.symbols` | `CHART_SYMBOLS` | `[]` (empty = trading symbols) |
| `charts.timeframes` | `CHART_TIMEFRAMES` | `[M15, H1, H4, D1]` |
| `charts.candle_count` | `CHART_CANDLE_COUNT` | `150` |
| `charts.update_interval_seconds` | `CHART_UPDATE_INTERVAL_SECONDS` | `120` |

## daemon intervals

| Key | Env | Default |
|---|---|---|
| `scan_interval_seconds` | `SCAN_INTERVAL_SECONDS` | `60` |
| `reconciliation_interval_seconds` | `RECONCILIATION_INTERVAL_SECONDS` | `300` |

## ML (optional)

| Key | Env / flag | Default | Notes |
|---|---|---|---|
| model input | `intelligence.ml.train --input` | `backtest_dataset.csv` | labeled CSV from backtesting |
| model output | `intelligence.ml.train --output` | `model.json` | XGBoost model + `*_metrics.json` |
| — | `MODEL_PATH` | — | where a future serving endpoint would load `model.json` |
| — | `MODEL_VERSION` | — | model version tag |

ML is optional and serving is PARKED: `get_ml_prediction()` honestly
returns `{"available": False, "reason": "no trained model deployed"}`. The
subsystem works fully with ML disabled; nothing treats a missing prediction
as a negative signal. Training needs the optional stack
(`xgboost scikit-learn pandas numpy`).

## backtesting

No yaml section — backtesting is an offline tool, driven by CLI flags and
the Python API:

```
python -m backtesting.engine --symbols EURUSD,GBPUSD --timeframe H1 \
    --start 2024-01-01 --end 2024-06-01 --output backtest_dataset.csv \
    [--min-history 60] [--max-lookahead 200] [--adapter mt5]
```

`run_backtest(adapter, strategy, symbols, start, end, *, timeframe="H1",
min_history=60, max_lookahead=200, max_candles=100000)`. Data comes only
from `BrokerAdapter.candles()` (closed candles); the run is structurally
incapable of live orders (`_ReadOnlyAdapter`).

## logging

| Key | Env | Default |
|---|---|---|
| `logging.level` | `LOG_LEVEL` | `INFO` |
| `logging.format` | `LOG_FORMAT` | `json` (`json` \| `text`) |

## storage (local)

| Key | Env | Default |
|---|---|---|
| — | `FOREX_AGENT_STORAGE` | `<install_dir>/storage/local.db` |

The local SQLite store (risk state, kill-switch, event queue, audit log,
journal). Nothing safety-critical may live only in the cloud.
