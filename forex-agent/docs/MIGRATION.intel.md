# MIGRATION notes — intelligence, storage, backtesting

Where each original module went, what was kept, what was parked, and what
was deliberately not ported. Originals live read-only in
`~/workspace/forex-migration/original/` (bridge) and `.../forex-signal-worker/`.

## knowledge.js → intelligence/correlation/knowledge.py — KEPT (ported)

`src/agent/knowledge.js` → `intelligence/correlation/knowledge.py`.
Static session windows, the correlation reference table, and
`checkCorrelatedExposure` ported to Python as the CANONICAL implementation
(the Worker's copy is being removed). Names mapped to Python conventions:
`currentSessionInfo` → `current_session_info`,
`correlatedPairs` → `correlated_pairs`,
`checkCorrelatedExposure` → `check_correlated_exposure`.
The flag text is byte-identical to the original. Improvement over the
original: the caller must pass broker-reported open positions (fixing the
`memory.js` "executed assumed open" double-counting weakness); positions
explicitly marked non-open are defensively skipped.

## reflect.js → intelligence/reflections/ — REFACTORED (math kept, LLM dropped)

- `computeOutcome` (pure win/loss/breakeven math) →
  `intelligence/reflections/outcome.py::compute_outcome`. Kept verbatim in
  behavior.
- The "store a structured reflection on every close" discipline →
  `intelligence/reflections/reflect.py::record_reflection`, running the
  deterministic pipeline outcome → performance → reflection → experience.
- **Not ported:** `reflectPrompt.js` and the `askProvider` LLM call.
  Reflection *text generation* is external-agent reasoning in the new
  architecture; the subsystem stores deterministic facts and an optional
  agent-supplied review, never an invented one.

## memory.js → intelligence/experience/ (+ D1 stays cloud) — REFACTORED

`getRecentHistory`/`summarizeHistory`/`getActiveExposure` were D1 queries
against the Worker's `signals` table. Split by the storage division
(TARGET §5): historical signal ledger stays in D1 (cloud,
`worker/`); the subsystem's own retrievable memory is now
`intelligence/experience/store.py::ExperienceStore`, backed by the local
journal in `storage/`. Kinds: trade, setup, regime, strategy, symbol,
failure, pattern, reflection. The "executed assumed open" approximation is
gone — exposure is computed from `BrokerAdapter.positions()`.

## context.js — NOT ported (superseded)

`buildContext` ("gather the facts" bundling for the LLM prompt) has no
subsystem home in the agent-native design: the external agent assembles its
own briefings from the capability tools (`agent/tools/`), which call into
`intelligence/` for session/correlation/calendar/ML/past-reflection facts.
No logic was lost — the deterministic pieces (session info, correlation
flags) live in `intelligence/correlation/`; the prompt assembly died with
the hosted-LLM path (REMOVE per classification).

## ml_features.py → intelligence/ml/features.py — KEPT (single copy)

The shared 8-feature schema kept as the ONE canonical copy. The
`ml_serving/ml_features.py` byte-duplicate was dropped (drift hazard). The
train/inference contract is documented on the module: all sides change
together or not at all.

## train_model.py → intelligence/ml/train.py — REFACTORED (optional)

Kept as the optional offline trainer: time-based split, timeout rows
dropped, `<200 rows` / `AUC≈0.5` warnings. Changes: heavy deps
(numpy/pandas/xgboost/sklearn) are guarded so the module imports on bare
Linux (`MLDependencyError` with install instructions if training is
attempted without them); the empty-frame edge case raises `ValueError`
instead of training on nothing; empty train/test splits raise too.

## ml_serving/serve_model.py — PARKED (not ported)

Working FastAPI `/predict`, but nothing in the live flow ever called it and
no `model.json` exists. Porting it would be theater. The un-parking path is
documented in `intelligence/ml/predict.py`: labeled CSV → `model.json` → a
real serving process on the exact `FEATURE_COLUMNS` → backend here. Until
then `get_ml_prediction()` honestly returns `available: false`.

## backtest.py → backtesting/ — REFACTORED

- `label_outcome` → `backtesting/labeler.py`, behavior unchanged
  (conservative tie-break, no lookahead on the entry candle).
- Data fetch → `backtesting/data.py::fetch_historical_candles`, exclusively
  through `BrokerAdapter.candles()` — the module-level `import MetaTrader5`
  is gone; the "Windows-only for data fetch" limitation with it.
- Walk-forward → `backtesting/engine.py::run_backtest`, strategy injected
  (`strategy.evaluate(symbol, window)`), rows keep the original shape plus a
  `strategy` field.
- **Closed-candle fix:** the original evaluated the forming candle (repaint).
  Now the walk hands the strategy the same window shape as live, and the
  `Strategy` contract excludes the final (forming) element — identical
  closed-candle semantics in backtest and production. The backtest does not
  pre-truncate (that would diverge from live by one candle).
- **Separation:** `_ReadOnlyAdapter` makes live orders structurally
  impossible; `BacktestError(ORDER_BLOCKED)` on any order attempt.
- `extract_features` ported (duck-typed) with a schema-parity assert
  against `intelligence.ml.FEATURE_COLUMNS`.

## test_backtest.py → tests/test_intel_backtest.py — REFACTORED

The original re-implemented `label_outcome` locally because `backtest.py`
imported MetaTrader5 at load time. That reason is gone (no MT5 import
anywhere in `backtesting/`), so the tests import the REAL
`backtesting.labeler` module. All six original scenarios kept, plus new
coverage: the closed-candle forming-spike test, the read-only separation
test, adapter-driven fetch tests, and the feature-schema test.

## What was NOT carried over (intentionally)

- `src/ai/*`, agent loops, prompts (`reasoning.js`, `agentPrompt.js`,
  `parse.js`, `reflectPrompt.js`, `dynamicSettings.js`) — hosted-LLM path,
  REMOVE per classification. The deterministic correlation-flag logic from
  `reasoning.js` survives in `intelligence/correlation/`.
- `get_economic_calendar` / `get_ml_prediction` Worker tool backends —
  replaced by the honest interfaces in `intelligence/` (both still
  `available: false`, as before, but now with a defined provider interface
  for future wiring).
