# ARCHITECTURE — core, broker, config

Companion to the top-level `ARCHITECTURE.md`. Covers the three areas
owned by the core-builder: `core/`, `broker/`, `config/`.

Design principles these areas obey (from `TARGET_ARCHITECTURE.md` §0):
deterministic safety below the model layer; local safety survives cloud
outage; dry-run default; no hosted LLM anywhere; the mobile app is not
a dependency; never invent market data or calendar events; never report
a trade before broker confirmation.

---

## 1. The safety stack (read bottom-up)

```
agent / daemon / CLI
        │  TradeRequest (signal_id, symbol, direction, SL, TP, idempotency_key)
        ▼
┌─────────────────────────┐
│  core/execution/gateway │  THE choke point — no other path to submit_order
│  1. idempotency replay  │
│  2. kill switch         │── engaged → block (fail-closed on unreadable latch)
│  3. symbol whitelist    │
│  4. risk engine         │── mandatory SL, equity daily-loss, own-position
│  5. safety: tick-fresh    counts, exposure+correlation, broker volume grid
│     spread cap, acct min│
│  6. dry-run block       │── full pipeline runs, NOTHING live leaves
│  7. broker submit       │── confirmed fills only
└─────────────────────────┘
        │  every decision audit-logged + event emitted
        ▼
┌─────────────────────────┐
│  broker.BrokerAdapter   │  MT5, paper, or disconnected — same interface
└─────────────────────────┘
```

Key invariants:

- **The agent can never call the adapter directly.** `request_trade()`
  is the only route to `submit_order()`.
- **Dry-run is the default** (`mode: dry_run` in `config/defaults.yaml`).
  Live trading requires an explicit config change AND valid credentials.
  In dry-run the entire check pipeline still runs, so the audit log shows
  exactly what *would* have happened.
- **Kill switch sits below every agent layer.** `KillSwitch.is_engaged()`
  fails closed: an unreadable latch reads as ENGAGED. The latch is
  persistent (store-backed) and idempotent; `close_all()` closes only
  bot-magic positions, reporting per-ticket results.
- **A trade is reported only after broker confirmation.**
  `submit_order()` returns `OrderResult` solely on broker confirmation;
  rejections and transport errors become structured `BrokerError`s,
  never silent fills.
- **Secrets never logged.** `AppConfig.redacted()` is the only printable
  form; the secrets file must be `0600` (a warning is emitted otherwise).

## 2. core/ — deterministic trading logic, no I/O to the broker

```
core/
├── events.py          # shim: validate (TARGET §4) → agent.events.bus.publish,
│                      #   else module-level in-process queue (drain_queue)
├── market/            # Candle dataclass + closed-candle contract + chart payloads
├── indicators/        # EMA / RSI / ATR — verbatim port, pure functions
├── smc/               # deterministic SMC — verbatim port
├── signals/           # Signal dataclass + lifecycle + dedup by (symbol, candle_time)
├── strategies/        # EmaRsiStrategy.evaluate(symbol, candles) -> Signal | None
│                      #   EXCLUDES the forming candle (repaint fix)
├── risk/              # RiskManager.check(signal, adapter) -> RiskResult
│                      #   equity (not balance) daily-loss · volume clamped to
│                      #   broker min/max/step · own-magic position counts ·
│                      #   persisted day/baseline/streak · correlation limits
├── execution/         # ExecutionGateway + KillSwitch (the choke point)
├── positions/         # exit manager: time exit → trailing → breakeven;
│                      #   SL only ever tightens, never loosens
├── performance/       # deal-history ground truth: stats, outcomes, costs
└── reconciliation/    # external-close detection via closing-deal lookup
```

Data flow for a signal: `market` candles → `strategies` (indicators +
SMC, forming candle excluded) → `signals` (dedup) → agent builds a
`TradeRequest` → `execution` gateway → `risk` → `broker`.

Position lifecycle after fill: `positions.manage_open_positions`
(daemon loop) applies time exit → trailing → breakeven, reporting
closes via `on_close(signal_id, reason, price, costs)`; `reconciliation`
catches closes the bot didn't trigger (manual closes at the broker) by
matching closing deals on the `approved:<signal_id>` comment convention;
`performance` derives honest stats from deal history for the risk
manager's consecutive-loss breaker and for review.

Events emitted (all schema-conformant with `agent/events/bus.py`):
`trade.requested`, `trade.executed`, `trade.rejected`, `risk.blocked`,
`kill_switch.activated`, `position.closed`, `position.external_close`.
See `core/API_DEPS.md` for the exact field contracts.

## 3. broker/ — the MT5 quarantine

```
broker/
├── __init__.py        # BrokerAdapter interface + dataclasses + error codes
├── disconnected.py    # raises BROKER_UNAVAILABLE for every operation
└── mt5/
    ├── adapter.py     # MT5Adapter — the ONLY file that may import MetaTrader5
    └── shared.py      # magic number, comment convention, shared helpers
```

`import MetaTrader5` appears exactly once, inside `try/except
ImportError` in `broker/mt5/adapter.py`; without the terminal the
adapter imports cleanly and every operation raises
`BROKER_UNAVAILABLE`. `MT5Adapter` absorbs both original dialects: the
`MT5Client` class and the direct module-level MT5 calls that used to
live in exit/reconciliation/performance/cost modules.

Conventions: `MAGIC_NUMBER = 20260817` tags every bot order;
`OrderRequest.order_comment()` → `approved:<signal_id>`;
`is_own_position()` / `Position.is_own` / `Position.signal_id()` keep
manual/foreign positions out of every count, exit pass, and stat.

## 4. config/ — one surface, secrets via environment

`config/defaults.yaml` holds every default (mode, broker, symbols,
strategy, risk, autonomy, worker, exits, performance, kill switch,
events, notifications, logging, charts); `config/config.py` overlays
environment variables (`FOREX_*` / original bridge keys) and an
optional secrets file (`FOREX_SECRETS_FILE`, must be `0600`).
`mode: dry_run` default; `mode: live` + `provider: mt5` requires
`MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER` or it fails fast with
`CREDENTIALS_INVALID`. See `docs/CONFIGURATION.md`.
