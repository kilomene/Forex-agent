# 30-Day Autonomous Demo Trading Experiment — Charter

**Experiment ID:** `forex-30day-001`
**Start:** 2026-09-21 (Day 1) — **End:** 2026-10-21 (Day 30), 09:00 America/Los_Angeles
**Account:** MetaQuotes-Demo demo account, hedging mode. **No real money, ever.**
**Strategy source:** NovaSignals EA (EMA20/EMA50 cross + RSI, M15, 77-symbol watchlist).
**Executor:** `trade_executor.py` + `NovaTrader.mq5` (command consumer).
**Status:** ACTIVE (activated 2026-09-21 by Zenas Ayansipe — "go").

## 1. The goal (honestly stated)

Zenas's rule: *"Not that there won't be losses, but the profit — wins — must be very
greater than the losses."*

That is the **goal**, not a promise. Trading loses sometimes; the experiment's job is
to find out whether this signal source, with disciplined risk, produces wins that
outweigh losses over 30 days. Whatever happens — profit or loss — the final report
documents it exactly. There is no cherry-picking: every signal, every decision, every
fill, every close is journaled.

## 2. What gets logged (everything)

`run/nova_journal.jsonl` is the single audit log. Every event is one JSON line with a
timestamp:

| Event type | When |
|---|---|
| `signal.received` | every new `signal.detected` the executor tails |
| `trade.decision` | every gate outcome (`commanded`, `intended`, `skipped:*`) incl. market context |
| `command.sent` | a `trade.open` command is written to `nova_commands.jsonl` |
| `trade.opened` | NovaTrader EA publishes a fill (ticket, entry, volume, symbol, direction) |
| `trade.closed` | position closed (entry, exit, P&L, hold time, reason: tp/sl/command) |
| `kill_switch.tripped` | 2% daily-loss limit hit (kill switch → `"0"`, trading halted) |
| `config.changed` | risk config values changed or clamped to bounds |
| `reflection` | weekend reflection records (stats + proposed tweaks) |
| `daily_summary` | end-of-day stats (23:55 PT cron) |

All times in the journal are local (PT); server times from MT5 are GMT+3 (EEST) for
the whole experiment period.

## 3. Risk rules (the min and the max)

| Parameter | Default | Min | Max | Note |
|---|---|---|---|---|
| `risk_per_trade_pct` | 0.5 | 0.25 | 1.0 | clamped on load; clamping is journaled |
| `max_concurrent_trades` | 100 (no cap) | 1 | none — cap lifted 2026-09-21 per user instruction ("trade as much as you can, no limit"); one position per symbol remains the natural limit |
| `max_daily_loss_pct` | 2.0 | — | — | breach → kill switch `"0"` + journal |
| `max_spread_points` | 50 | — | — | signals wider than this are skipped |

- Volume is clamped to the broker's `volume_min`/`volume_max` and floored to
  `volume_step`; `tick_value<=0` → `skipped:bad_specs`.
- One position per symbol at a time.
- **Both directions** are traded (BUY and SELL) — as many signals as the risk
  gates allow.
- Gate order: `trading_disabled → stale_specs/sizing → dry_run → market_closed →
  non_demo_server → max_concurrent → symbol_already_open → spread_too_wide →
  daily_loss_limit → commanded`.

## 4. Market hours

New positions open only when the FX market is open. Closed window (server time,
GMT+3): **Saturday all day, and Sunday before 01:00 server time**
(Friday 22:00 GMT close → Saturday 01:00 server; Sunday 22:00 GMT open →
Monday 01:00 server).

- Signal arrives while closed → `skipped:market_closed`. No new position.
- Positions already open are **held**, never force-closed by the gate.
- Each decision records market context: server hour, weekday, session bucket
  (`asia` 00–07, `london` 08–12, `newyork` 13–20, `late` 21–23), spread, market_open.

## 5. Kill-switch discipline

- The 2% daily-loss auto-trip stays on for the whole experiment. On trip:
  `run/trading_enabled` is written `"0"`, trading halts immediately, and a
  `kill_switch.tripped` event is journaled.
- **Only Zenas re-enables trading after a trip.** No cron, no script, no agent
  turns the kill switch back to `"1"` on its own.
- Telegram `/stop` and `/start` exist as code only (`tg_commands.py` is never
  launched without Zenas's explicit approval).

## 6. Learning loop

- **Weekend reflections** (Sat + Sun 10:00 PT): read the journal, compute win
  rate by symbol / direction / session / weekday, avg win, avg loss,
  expectancy; write `reflection` records to the journal and a proposal file
  under `run/`. Proposals are **for Zenas's approval only — never auto-applied**.
- **Weekly review** (Mon 08:00 PT): `review_trades.py` proposal, same rule.
- Market-closed time is study time: the reflection pass also notes streaks,
  worst symbols, worst sessions, and streaks of consecutive losers.
- **Strategy parameter changes require Zenas's approval.** The system may
  propose; it may never apply.

## 7. Daily rhythm

- 23:55 PT daily: `daily_summary` event → journal + one line in the daily log
  `~/memory/YYYY-MM-DD.md` (day P&L, trades, win rate, expectancy).

## 8. Evidence & the final report

- Public evidence repo: `kilomene/forex-30day-trading-experiment`.
  Initial push: trader code + this charter + README. Weekly: journal slice +
  review proposal pushed every Monday. 
- Day 30 (2026-10-21 09:00 PT): `final_report.py` compiles `FINAL_REPORT.md`
  from the journal — every trade, full statistics, all reflections, config
  history — wins AND losses, then pushes journal + report.
- The final report shows exactly what happened either way. No fabrication of
  signals, trades, or P&L — ever.

## 9. Hard constraints

- Demo servers only (server name must contain "demo"). Any non-demo server is
  refused loudly.
- Never touch `NovaSignals.mq5` (observation-only), `signal_bridge.py` signal
  logic, or `chat_notify.py` behavior.
- No secrets in files, logs, or memory. Raw tokens live only in process env or
  the vault connector.
- No live money, ever. No strategy change without Zenas's approval.

## 10. Experiment pre-registration protocol (Hummingbot-style honest lab book)

No tweak — however small, however "obviously good" — ships on a hunch. Every
idea gets its own pre-registered test window, and the window's results are
published win or lose.

**The workflow:**

1. **Pre-register BEFORE the window opens.** Fill in
   `experiment_preregistration_template.md` and save it as
   `run/prereg_<name>.md`. Exactly one idea per window; fixed window dates;
   fixed risk ($100/trade, 10 concurrent — unchanged); fixed symbols; a
   config-snapshot reference (path + sha256 + date). Stop conditions are
   defined upfront (drawdown halt level, minimum trades for a verdict) —
   never invented mid-window.
2. **The file is frozen at window start.** Any edit after the window opens is
   a protocol deviation and must be journaled as such; the window's results
   cannot be silently kept.
3. **Trades are tagged by variant.** Journaled close events (and their linked
   `trade.decision` events) carry a `variant` tag naming the pre-registered
   variant they belong to. The first variant is always the control — current
   behavior, unchanged.
4. **weekend_reflection.py publishes per-variant P&L.** For every reflection
   date inside a prereg window, the reflection `.md` gains a "Pre-registered
   experiments" section and the journaled `reflection` event gains a
   `preregistration` block: per-variant trades, total P&L, expectancy,
   Sharpe, profit factor, max drawdown — for EVERY variant, including losers.
   A losing variant is a result, not a failure; hiding one voids the window.
   Variants with no journaled trades yet are reported honestly ("no variant
   data journaled yet") — numbers are never invented, never borrowed.
   With no active prereg file, the section states "no pre-registered
   experiment for this window" and nothing else changes.
5. **Metrics that decide** (per variant, from `performance.py`): Sharpe
   ratio, max drawdown, profit factor, expectancy vs the pre-window baseline.
   **WIN RATE ALONE DECIDES NOTHING.** A variant graduates only by moving
   these metrics in the right direction versus baseline and control, with at
   least the pre-registered minimum trades behind it. Fewer trades than the
   minimum → **inconclusive**, never "promising".

**Real-money standard (Zenas's standing principle, 2026-10-18):** every
variant must be one we would run with actual capital — the same risk plan,
the same sizing, the same stops. No widened stops, no experimental leverage,
no "let's see what happens" sizing justified by "it's only demo". The demo
is a strict laboratory, not a permissive one: it exists to reject ideas
cheaply, not to excuse sloppy ones. Treat every demo dollar as real money.

**The shipping rule:** no tweak ships without moving the Phase-1 metrics on
demo first — inside a pre-registered window, against the baseline, published
win or lose. Proposals remain for Zenas's approval only; the protocol
changes how ideas are *tested*, never who *approves* them (§6, §9 stand).

---

## Continuous evolution loop (2026-10-18)

Per Zenas's standing order ("you're the builder", "everything would be
done by you tho auto update and upgrading", every version end-to-end
tested before install), the trading stack now evolves through a gated
pipeline instead of ad-hoc edits. Full design: `EVOLUTION_LOOP.md`.

- **Weekly pass** (`evolve.py --weekly`, cron `project-evolution-weekly`):
  studies the journal + latest reflection + borrow-list, maintains tested
  proposals under `proposals/<id>/` (proposal.md + patch.diff + tests).
- **Install pipeline** (`pipeline.py`): fixed gate order, no skipping —
  full test suite green -> EA recompile (when .mq5 changes) -> staging
  replay of real signals through the real gates (refactors must be
  decision-identical; behavior changes must win on Sharpe / max drawdown
  / profit factor with the real-money standard: "doesn't lose much"
  fails) -> 48h/200-signal shadow canary with auto-rollback on any guard
  trip -> install -> post-install assertions (kill switch still "0",
  risk 0.05%/10 concurrent, blocked_symbols unchanged, daemon healthy,
  supervisor healthy).
- **Approval tier** (corrected 2026-10-18 per Zenas's governing
  "auto update and upgrading" order): everything except the two
  hard-blocked classes flows automatically through the full gate chain
  — execution refactors, protection-plugin changes
  (disabled-by-default at build; enablement only after the calm canary
  via `plugin-observe` / `plugin-enforce`), report upgrades,
  journaling/tooling/docs, EA updates (with MQL5 recompile). The ONLY
  hard-blocked classes — never installed by the pipeline, Zenas's own
  actions — are (a) anything that would resume trading
  (`run/trading_enabled` -> `"1"`; auto-updates NEVER resume trading)
  and (b) any change to `blocked_symbols` (the GBPZAR exclusion is
  Zenas's pending decision). Risk config (0.05% / 10 concurrent) is a
  pipeline-asserted invariant, verified post-install with rollback on
  drift — not an approval item.
- **Rollback**: `python3 rollback.py --previous` restores the last good
  version, restarts the daemon, verifies health, journals the event.
- **Manifests**: `versions/<id>/` holds version.json, file snapshots,
  pytest/staging/canary evidence; `run/evolution.log` records every run.

This section documents the pipeline's existence and guarantees. It does
not change any charter rule above: §§ on risk, halt authority, and
Zenas's approval rights stand.
