# ARCHITECTURE — intelligence, storage, backtesting

Companion to the top-level `ARCHITECTURE.md`. Covers the three areas owned
by the intelligence-builder: `intelligence/`, `storage/`, `backtesting/`.

Design principles these areas obey (from `TARGET_ARCHITECTURE.md` §0):
deterministic safety below the model layer; local safety survives cloud
outage; dry-run default; no hosted LLM anywhere; the mobile app is not a
dependency; never invent market data or calendar events.

---

## 1. intelligence/ — deterministic market intelligence, no LLM

`intelligence/` answers "what do we know?" without ever calling a model,
a broker, or the cloud. Every subpackage is importable on bare Linux with
zero heavy dependencies.

```
intelligence/
├── correlation/        # sessions + correlation exposure (port of knowledge.js)
├── economic_calendar/  # provider INTERFACE only — no fabricated events
├── experience/         # local experience memory (trades, setups, regimes, …)
├── reflections/        # post-trade outcome math + reflection records
└── ml/                 # OPTIONAL: feature schema + trainer + parked serving
```

### correlation/

Canonical Python port of the Worker's `src/agent/knowledge.js`. Static
reference data, honestly labeled as such:

- `SESSIONS_UTC` — fixed UTC windows (tokyo 0–9, london 8–17, new_york
  13–22); `current_session_info()` derives active sessions, overlaps, and a
  liquidity note.
- `CORRELATION_REFERENCE` — commonly-cited long-run correlation directions
  (`positive`/`negative`) between majors, indices, AAPL, XAUUSD. A heuristic,
  not a computed statistic. A live rolling-correlation upgrade would be
  computed from candle data, never by pretending this table is one.
- `check_correlated_exposure(new_signal, open_positions)` — the
  deterministic stacking rule: for each open position on a *different*
  symbol, stacked = (positive ∧ same direction) ∨ (negative ∧ opposite
  direction); emits the original flag text verbatim.

Double-counting fix: the original `memory.js#getActiveExposure` treated
every `executed` signal as open forever (no live MT5 feed). Here the caller
must pass the broker-reported open list (`BrokerAdapter.positions()`); the
function additionally skips anything explicitly marked non-open.

Consumers: `core/risk` (correlation exposure limits), `agent/tools`
(`get_correlation_exposure` / `get_session_info` capabilities).

### economic_calendar/

Provider **interface** only. `CalendarProvider.fetch(from_, to_) ->
list[CalendarEvent]`; `get_calendar(symbol, hours_ahead, provider)` returns
`{"available": False, "reason": "no provider configured"}` until a real
source is wired, and degrades to `available: False` (never invented events)
if a provider fails. Wiring a real source later means implementing the
interface against the documented contract (`GET {NEWS_CALENDAR_URL}?symbol=
&hours_ahead=`, optional bearer key) — no changes to consumers.

### experience/ + reflections/

Local experience memory backed by the storage journal:

- `ExperienceStore` — `record(kind, …)` / `query(...)` over the closed kind
  set `{trade, setup, regime, strategy, symbol, failure, pattern,
  reflection}`. Unknown kinds raise; they are added deliberately.
- `reflections/outcome.py` — `compute_outcome()`, the pure port of
  `reflect.js#computeOutcome` (win/loss/breakeven from entry vs close).
- `reflections/reflect.py` — `record_reflection()` runs the deterministic
  pipeline **outcome → performance → reflection → experience**:
  `build_performance()` computes price diff, R-multiple, planned R:R from
  entry/SL/TP/close (math only, no judgement), then the record is stored via
  the experience store. Reflection *text generation* was the LLM part of the
  original and is deliberately not ported: the external agent writes its own
  review into `reflection_text`; until then the record honestly says the
  review is pending. The subsystem never invents one.

**Risk boundary (non-negotiable):** experience and reflections are advisory
data for the external agent. They cannot change risk parameters, engage the
kill switch, or approve/block trades. `core/risk` and `core/execution` never
read the journal — there is no code path from reflection to control.

This is retrieval-augmented experience, not learning: future agent reasoning
can be handed past reflections, which is bounded but real value — framed
honestly, exactly like the original.

### ml/ — OPTIONAL

- `features.py` — the SINGLE canonical 8-feature schema (`FEATURE_COLUMNS`).
  The `ml_serving/` byte-duplicate was dropped. Training-time and
  inference-time features must match this list exactly; `backtesting`
  asserts it.
- `train.py` — port of `train_model.py`: time-based split (no lookahead),
  timeout rows dropped, `<200 rows` and `AUC≈0.5` warnings, empty-frame
  guard. Heavy deps (numpy/pandas/xgboost/sklearn) are guarded — the module
  imports cleanly without them and raises `MLDependencyError` with install
  instructions when training is attempted.
- `predict.py` — `get_ml_prediction()` honestly returns `{"available":
  False, "reason": "no trained model deployed"}`. **Serving is PARKED**: the
  original FastAPI `/predict` was not ported because nothing ever called it
  and no model exists. Un-parking requires, in order: labeled CSV →
  trained `model.json` → a real serving process computing these exact
  features → a backend here. The subsystem works fully with ML disabled,
  and no consumer may treat a missing prediction as a negative signal.

---

## 2. storage/ — local persistence

`storage/` is the LOCAL store (`TARGET_ARCHITECTURE.md` §5). It is **not**
the Worker D1 schema — that lives in `worker/` (cloud layer). Local holds:

| Table | Holds | Why local |
|---|---|---|
| `risk_state` (kv) | daily-loss baseline, loss-streak, … | fixes the restart hole; risk works with no network |
| `kill_switch` | engaged/source/ts latch | fails closed on Worker outage |
| `event_queue` | internal events (FIFO) | local bus buffer; agent/CLI poll it |
| `audit_log` | append-only audit records | every gateway decision is traceable offline |
| `journal` | experience + reflection entries | local memory, queryable by kind/symbol/outcome |

DDL is the single source of truth: `storage/schemas/schema.sql`, executed
by `Store.__init__`. Contract:

- `get_risk_state()/set_risk_state(dict)` — JSON-encoded kv upsert.
- `get_kill_switch()/set_kill_switch(engaged, source)` → `{"engaged",
  "source", "ts"}`.
- `enqueue_event(dict)/dequeue_events(limit=100)` — FIFO pop; returned
  dicts carry `_queue_id`, `_ts`.
- `audit(dict)/query_audit(limit=100, since=None)` — newest-first.
- `journal_add(dict)/journal_query(**filters)` — filters: `kind, symbol,
  direction, outcome, since, until`; unknown filters raise `ValueError`.

Thread-safe (`RLock`, `check_same_thread=False`) for daemon threads.
Default path `<install_dir>/storage/local.db`, overridable via
`FOREX_AGENT_STORAGE`. `storage/` imports nothing but the stdlib; every
other area imports from it, never the reverse.

---

## 3. backtesting/ — fully separated from live execution

Walk-forward engine ported from `forex-bot-bridge/backtest.py`, running the
same strategy semantics as live against historical candles.

```
backtesting/
├── labeler.py   # label_outcome: SL/TP-first walk, conservative tie-break
├── data.py      # fetch_historical_candles via BrokerAdapter ONLY
├── engine.py    # run_backtest + _ReadOnlyAdapter + CLI
└── errors.py    # BacktestError with structured codes
```

**Separation (structural):** `run_backtest` wraps any adapter in
`_ReadOnlyAdapter` before use — `submit_order`/`modify_order`/`close_position`
raise `BacktestError(ORDER_BLOCKED)`. The module never imports MetaTrader5,
never touches the execution gateway. A backtest run cannot place a live
order even if handed the live `MT5Adapter`. Data flows one way:
adapter → strategy → labeler → rows.

**Closed-candle fix:** the walk passes `window = candles[:i+1]` to
`strategy.evaluate()` — the same shape the live path hands the strategy.
Per the `Strategy` contract the last element is the forming candle and is
excluded from signal logic, so evaluation is on closed candles only,
identical to live. The backtest deliberately does *not* pre-truncate: that
would evaluate one candle deeper than production and silently diverge from
it. The signal's `candle_time` (the closed candle it fired on) anchors
outcome labeling; a signal timestamped outside its window raises
`SIGNAL_CANDLE_NOT_FOUND` rather than labeling against the wrong candle.

**Data:** exclusively `BrokerAdapter.candles()` (closed-only, oldest→newest).
The interface is count-based, so ranges are served by fetching up to
`max_candles` and filtering to `[start, end]`; a start older than available
history is an honest `INSUFFICIENT_HISTORY`, not silent truncation.
`DisconnectedAdapter` surfaces as `BrokerError(BROKER_UNAVAILABLE)`.

**Labeling** (`labeler.py`): from the entry candle forward (entry candle
excluded — no lookahead), each candle's high/low checked against SL/TP;
both-hit resolves conservatively to loss; neither within `max_lookahead`
(200) → `timeout`. Rows carry the canonical 8 features
(`extract_features` asserts schema parity with `intelligence.ml`).

**Strategy injection:** `run_backtest(adapter, strategy, symbols, start,
end, …)` takes any `strategy.evaluate(symbol, candles)` (real:
`core.strategies.EmaRsiStrategy`) — the engine owns windowing and labeling,
never strategy internals.

---

## Honest gaps (carried, not hidden)

1. **Economic calendar**: no provider — interface only, `available: false`.
2. **ML prediction**: no dataset, no model, serving parked — `available: false`.
3. **Correlation table**: static heuristic, not a rolling statistic.
4. **Experience**: retrieval-augmented memory, not learning; reflection text
   needs the external agent.
