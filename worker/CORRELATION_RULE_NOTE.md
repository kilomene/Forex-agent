# Correlation-Flag Rule — moved to the subsystem core

**Date:** 2026-09-21 · **Source:** original
`forex-signal-worker/src/agent/reasoning.js` + `context.js` + `knowledge.js`
(read-only originals under `~/workspace/forex-migration/original/`).

## Why this note exists

The old Worker ran a deterministic correlation check on every new signal
*before* any model was consulted, and appended the resulting flags to
`risk_notes` as **ground truth, not opinion**. When the AI pipeline was
stripped from the Worker, this rule did NOT die with it — per TARGET
ARCHITECTURE it now lives in the subsystem core
(`intelligence/correlation/` + `core/risk/`). This note is the exact spec
so the port loses nothing.

## The rule (verbatim semantics)

For a **new signal** versus **each open position on a different symbol**:

1. `relation = CORRELATION_REFERENCE[newSignal.symbol][position.symbol]`
   — one of `'positive'` | `'negative'`; skip the position if the pair is
   not in the table.
2. `sameDirection = (position.direction === newSignal.direction)`
3. `stacked = (relation === 'positive' && sameDirection) || (relation === 'negative' && !sameDirection)`
4. If `stacked`, append this flag text to `risk_notes`:
   > `Correlated exposure: open {position.direction} {position.symbol} position is {relation}ly correlated with this {newSignal.direction} {newSignal.symbol} signal — this may be effectively doubling the same directional bet rather than diversifying.`

Notes on the original semantics worth preserving:

- Same-symbol positions are skipped (`pos.symbol === newSignal.symbol → continue`).
- "Open positions" in the Worker meant D1 signals with status in
  `('executed', 'approved')` (see original `memory.js getActiveExposure`,
  max 20 most recent). The subsystem core should use **real** open
  positions from the broker adapter where available — strictly better
  than the Worker's approximation, which could double-count closes the
  Worker never heard about.
- The flag is appended to whatever `risk_notes` already exist (space-joined);
  it never overwrites.

## Static correlation reference table (verbatim)

"Positive" = pairs tend to move the same direction; "negative" = opposite.
Commonly-cited long-run approximations — a reference heuristic, NOT a
computed statistic. A future upgrade is a rolling correlation computed
off the subsystem's own candle feed.

```js
const CORRELATION_REFERENCE = {
  EURUSD: { GBPUSD: 'positive', USDJPY: 'negative', AUDUSD: 'positive' },
  GBPUSD: { EURUSD: 'positive', USDJPY: 'negative', AUDUSD: 'positive' },
  USDJPY: { EURUSD: 'negative', GBPUSD: 'negative', AUDUSD: 'negative' },
  AUDUSD: { EURUSD: 'positive', GBPUSD: 'positive', USDJPY: 'negative' },

  // Indices: broadly move together (risk-on/risk-off)
  US30:  { US100: 'positive', US500: 'positive' },
  US100: { US30: 'positive', US500: 'positive', AAPL: 'positive' },
  US500: { US30: 'positive', US100: 'positive' },

  // A stock correlates with an index it's a major component of
  AAPL: { US100: 'positive' },

  // Gold commonly moves inversely to USD strength
  XAUUSD: { EURUSD: 'positive', USDJPY: 'negative' },
};
```

Also from the original `knowledge.js`: session UTC windows
(tokyo 0–9, london 8–17, new_york 13–22) — moved with the same module to
`intelligence/correlation/`.

## Worked example

New signal: BUY EURUSD. Open positions: BUY GBPUSD, SELL USDJPY.

- vs BUY GBPUSD: relation positive, same direction → **stacked** → flag:
  "Correlated exposure: open BUY GBPUSD position is positively correlated
  with this BUY EURUSD signal — this may be effectively doubling the same
  directional bet rather than diversifying."
- vs SELL USDJPY: relation negative, opposite direction → **stacked** →
  flag (negatively correlated).

New signal: SELL EURUSD vs BUY GBPUSD → positive relation, different
direction → not stacked → no flag.
