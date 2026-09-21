# MIGRATION — three projects → one forex-agent subsystem

How the three original projects became `forex-agent`, what was kept,
what was fixed, what was parked, and what was deliberately removed.
Originals are read-only in `~/workspace/forex-migration/original/`;
the mobile app also lives as legacy reference in
`docs/reference/forex-signal-app/` (not a dependency).

Per-area detail:

- `docs/MIGRATION.core.md` — bridge: config, indicators, SMC, strategies,
  market, broker/MT5 quarantine, risk fixes, kill switch, gateway (new),
  exits, performance, reconciliation
- `docs/MIGRATION.intel.md` — bridge + worker knowledge/reflect/memory/ML:
  correlation, calendar, experience, reflections, ML, backtesting
- `docs/MIGRATION.worker.md` — Cloudflare Worker: AI removal, route
  changes, auth, rate limiting, migrations

## 1. Source map

| Original | Became | Role now |
|---|---|---|
| `forex-bot-bridge` (Python) | `core/`, `broker/`, `config/`, `backtesting/`, `intelligence/` parts | local capability: strategy, safety, execution, data |
| `forex-signal-worker` (JS) | `worker/` + `intelligence/correlation/`, `intelligence/reflections/` | cloud API/sync/persistence only; knowledge ported to Python |
| `forex-signal-app` (Expo) | `docs/reference/forex-signal-app/` | legacy reference only — NOT required, NOT depended on |

## 2. What changed structurally

- **The brain moved out.** Every hosted-LLM call (OpenRouter,
  Anthropic, custom-model, prompts, agent loops, Worker auto-approve,
  LLM reflection text) was removed. The external agent reasons; the
  subsystem computes. No model is built, hosted, or required.
- **A choke point was created.** Order placement used to be inline in
  `main.py`; now `core/execution/gateway.py` is the only path to
  `submit_order()`, with idempotency, kill-switch, whitelist, risk,
  spread/account checks, dry-run block, and broker-confirmed fills —
  plus broker-confirmed `close_position` / `modify_position`.
- **MT5 was quarantined.** Direct `mt5.*` calls scattered across
  exit/reconciliation/performance/cost modules now live behind
  `BrokerAdapter`; `import MetaTrader5` occurs exactly once
  (`broker/mt5/adapter.py`, optional/degraded).
- **Safety went local and persistent.** Kill-switch latch and risk
  state live in local SQLite — they survive Worker/network outages.
- **Signals got an honest lifecycle.** `signal` (candidate) →
  `trade request` → `approved trade` → `broker-confirmed execution`
  are distinct states; the Worker never auto-approves; a new
  `POST /signals/:id/failed` route closes the dangling-`approved` gap.
- **Interfaces multiplied.** MCP was not the only surface: the same
  capability registry is exposed over CLI, localhost HTTP API,
  persisted events, health/status, and configuration.
- **Backtesting separated.** Read-only adapter wrapper makes live
  orders structurally impossible; closed-candle semantics identical to
  live.

## 3. What was kept (functionality preserved first)

Indicators and SMC ported verbatim; strategy logic kept with the
repaint fix; risk semantics kept with three holes fixed (equity-based
daily loss, broker volume grid, own-magic counting); exit precedence
(time → trailing → breakeven) and SL-tightens-only kept; deal-history
stats and commission math kept; session/correlation knowledge ported
canonically; reflection math kept; ML feature schema kept as the single
canonical copy; Worker routes kept except the AI settings screens.

## 4. What was parked honestly (not silently dropped)

- Economic-calendar provider: interface exists, `available: false`.
- ML serving: no model exists; `available: false`. Optional trainer kept.
- Correlation table: static reference heuristic, not a live statistic.
- Reflection text: the external agent's job; the subsystem stores the
  record and the math, never invented prose.
- MT5 live trading: adapter complete but unvalidated without the
  terminal + credentials; degrades to `BROKER_UNAVAILABLE`.
- Worker deployment: needs real D1 `database_id` + deploy credentials.

## 5. What was removed deliberately

- All hosted-AI modules, prompts, agent loops, provider configs
  (both in the Worker and any bridge LLM experiment).
- Autonomous Worker auto-approval of signals.
- Mobile-app push paths as subsystem dependencies (app is reference).
- `ml_serving/` duplicate feature file (drift hazard).
- Tracked `__pycache__`, `.pytest_cache`, runtime DBs (gitignored).

## 6. Verification after migration

- Python: 245/245 pytest green (core, intelligence, interfaces,
  integration) — no MetaTrader5, no mobile app, no network, no LLM.
- Worker: 20/20 fake-D1 contract tests green.
- Installer: idempotency test green (11/11); `needs_credentials` /
  `ready` states verified.
- Closed-candle regression test proves the repaint fix.
- Classification: `audit/CLASSIFICATION.md` (KEEP / REFACTOR / REMOVE /
  OPTIONAL) was followed throughout; `audit/AUDIT.md` records the
  original-state evidence.
