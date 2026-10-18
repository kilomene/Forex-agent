# Experiment pre-registration — <EXPERIMENT NAME>

<!-- PREREG
name: <experiment-name, e.g. cooldown-6h>
window_start: <YYYY-MM-DD>
window_end: <YYYY-MM-DD>
variants: <variant-a>, <variant-b>
min_trades: <N, e.g. 20>
stop_drawdown_usd: <X, e.g. 500>
baseline_expectancy: <-12.40, per-trade expectancy over the 30 days before the window>
config_snapshot: <path + sha256 + date, e.g. run/risk_config.json @ 2026-10-25 (sha256:abc123...)>
-->

<!--
  FILL THIS IN *BEFORE* THE WINDOW OPENS. The prereg file lives at
  run/prereg_<name>.md and is FROZEN at window start: any edit after the
  window opens is a protocol deviation and must be journaled as such.
  weekend_reflection.py picks the file up automatically and publishes
  per-variant P&L in every weekend reflection inside the window.

  The <!-- PREREG ... --> block above is machine-read. Keep its keys exact;
  run the validator (prereg.validate) after filling it in.
-->

## 1. Idea under test (EXACTLY ONE)

<State the single idea this window tests, in one or two sentences.
If you have two ideas, you need two windows — never test two ideas in one
window, because then neither result means anything.>

Example: "A 6-hour same-symbol cooldown after any close reduces churn on
M15 noise without giving up the symbols' real moves."

## 2. Fixed window

- **Window start:** <YYYY-MM-DD> (no trades before this date count)
- **Window end:** <YYYY-MM-DD> (no trades after this date count)
- Trades are attributed by broker exit time (the same rule as every other
  reflection stat).

## 3. Fixed risk (UNCHANGED)

- **Risk per trade: $100** — 0.05% of the $200,000 planning basis. Not
  negotiable inside a window.
- **Max concurrent positions: 10.** Not negotiable inside a window.
- Kill switch (2% daily loss) and all executor gates stay exactly as the
  charter defines them.

## 4. Fixed symbols

<List the symbols this window covers — either the full 77-symbol watchlist
or an explicit subset. A subset must be named here BEFORE the window opens;
shrinking the list mid-window because a symbol is losing is
cherry-picking, not science.>

## 5. Fixed config snapshot

- Snapshot reference: <path + sha256 + date — copy from the PREREG block>
- No config, risk, or EA change ships during the window. If something must
  change for safety (kill-switch trip, broker outage), the window is voided
  and the deviation is journaled — the results are not silently kept.

## 6. Stop conditions (defined UPFRONT, before the window opens)

- **Drawdown stop:** halt the window's variants if any variant's window
  drawdown exceeds **$<X>** (copy from the PREREG block). A stopped window
  still publishes everything journaled so far.
- **Inconclusive, not "promising":** if fewer than **<N>** trades fire per
  variant (copy from the PREREG block), the window is **inconclusive** —
  never "promising", never "trending well". Small samples decide nothing.
- **Kill-switch trip:** the charter's kill-switch discipline applies
  unchanged; a trip voids the window.
- No new stop condition may be invented after the window opens.

## 7. Metrics that decide

Computed per variant from the journaled, deduped closes inside the window:

- **Sharpe ratio** (per-trade, from performance.py)
- **Max drawdown** (per-trade, from performance.py)
- **Profit factor** (from performance.py)
- **Expectancy vs the pre-window baseline** (baseline_expectancy in the
  PREREG block, measured over the 30 days before the window)

**WIN RATE ALONE DECIDES NOTHING.** A 60% win rate with a 0.7 profit factor
is a losing system wearing a costume. No variant ships on win rate.

A variant "wins" the window only if it moves these metrics in the right
direction versus baseline AND versus the control variant, with at least
<min_trades> trades behind it.

## 8. Variants

<One bullet per variant, matching the PREREG block's `variants:` list
exactly. The first variant is always the control — current behavior,
unchanged.>

- **<variant-a>** (control): <what it does — current behavior, unchanged>
- **<variant-b>**: <what changes, precisely enough that someone else could
  reimplement it>

**Every variant gets its own P&L published, including losers.** A losing
variant is a result, not a failure. Hiding a losing variant is fabrication
by omission and voids the whole window.

## 9. REAL-MONEY STANDARD (Zenas's standing principle, 2026-10-18)

> "The demo would still feel or be like real money to you."

Every variant in this window must be one we would run with **actual
capital** — the same risk plan, the same sizing, the same stops. No
widened stops, no experimental leverage, no "let's see what happens"
sizing justified by "it's only demo". The demo is a **strict laboratory,
not a permissive one**: it exists to reject ideas cheaply, not to excuse
sloppy ones. Treat every demo dollar as real money — because the habits
built here are the ones that will touch real money later.

Concretely: if a variant's risk profile would make you flinch with real
money on the line, it does not belong in this window. Rewrite it or drop
it.

## 10. Publication

- weekend_reflection.py publishes per-variant P&L (trades, total, expectancy,
  Sharpe, profit factor, max drawdown) in every weekend reflection whose
  date falls inside the window, and journals the same block on the
  reflection event.
- If a variant has no journaled trades yet, the reflection says so honestly
  ("no variant data journaled yet") — it never invents numbers and never
  borrows another variant's trades.
- At window end, the final reflection of the window states the verdict per
  variant against §7, and the verdict is pushed to the evidence repo with
  the journal slice — winners AND losers.
