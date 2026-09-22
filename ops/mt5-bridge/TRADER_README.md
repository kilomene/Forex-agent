# Nova Autonomous Trader — Demo Stack

Observation stays observation. Trading lives in a **separate** stack that is
**OFF by default** and can only be armed by the user.

## Architecture

```
NovaSignals.mq5 (observation ONLY, never trades)
   │ writes nova_signals.jsonl  (signal.detected)
   ▼
┌────────────────┐      ┌──────────────────┐      ┌──────────────┐
│ signal_bridge  │      │ trade_executor   │      │ NovaTrader   │
│ (Telegram bot  │      │ (Python daemon)  │      │ .mq5 (EA)    │
│  notifications)│      │ tails signals ──▶│      │ polls        │
└────────────────┘      │ risk gates       │      │ nova_commands│
                        │ sizing           │─────▶│ .jsonl ──▶   │
┌────────────────┐      │ journal          │      │ market orders│
│ tg_commands.py │◀────▶│ run/trading_     │      │ (magic       │
│ (Telegram      │      │   enabled (0/1)  │      │  20260921)   │
│  /stop /start  │      └──────────────────┘      └──────┬───────┘
│  /status /pnl) │              │                        │ writes
└────────────────┘              ▼                        ▼
                        risk_config.json         nova_trades.jsonl
                        (trading_enabled=false,  (trade.opened/
                         dry_run=true)            trade.closed/
                                                  trade.rejected)
```

Supporting files (all in `MQL5/Files/` unless noted):

| File | Writer | Purpose |
|---|---|---|
| `nova_signals.jsonl` | NovaSignals | signal.detected events |
| `nova_feed.json` | NovaSignals | 10 s heartbeat, proves feed live |
| `nova_commands.jsonl` | trade_executor | trade.open / trade.close commands |
| `nova_trades.jsonl` | NovaTrader | trade.opened / trade.closed / trade.rejected |
| `nova_symbol_specs.json` | NovaTrader (60 s) | tick value/size, volume min/max/step, stops level, spread + account{equity,balance,currency,server,time} |
| `nova_journal.jsonl` | trade_executor | every decision: signal_id → command_id → ticket → close |
| `run/trading_enabled` | tg_commands / executor | kill switch: "1" open new trades, "0" don't |
| `risk_config.json` | operator | risk parameters (see below) |
| `run/trade_executor.state.json` | trade_executor | JSONL cursor + dedup |

## Safety layers (all must pass for any order)

1. `risk_config.json`: `trading_enabled` must be `true` **and** `dry_run` false.
2. Kill switch `run/trading_enabled` must contain `1` (checked every 5 s).
3. Demo-server check: server string must contain "demo" (executor + EA refuse otherwise).
4. Fresh specs: `nova_symbol_specs.json` older than 5 min → no sizing, skip with reason.
5. Max concurrent trades (default 3), one position per symbol.
6. Max spread filter (default 50 points), stops-level validation, volume normalized to broker min/max/step.
7. Daily loss limit (default 2% of equity): on breach the executor writes `"0"` to the kill switch itself and logs loudly.
8. EA-level: one retry on requote, then reject; never throws.

## Config reference (`risk_config.json`)

```json
{
  "trading_enabled": false,
  "dry_run": true,
  "risk_per_trade_pct": 0.5,
  "max_concurrent_trades": 3,
  "max_daily_loss_pct": 2.0,
  "max_spread_points": 50
}
```

Position sizing: `risk_amount = equity × risk_per_trade_pct / 100`;
`volume = risk_amount / (sl_ticks × tick_value)`, rounded **down** to the
broker's volume step, clamped to [min, max]. `sl_ticks = |entry − sl| / tick_size`.

## Activation procedure (requires explicit user approval — never automatic)

1. Verify the terminal is on the **demo** server (watchdog + executor refuse otherwise).
2. Confirm `NovaTrader` is attached to a chart (any symbol) and `nova_symbol_specs.json` is fresh (check its `time` field; < 60 s old).
3. User says the word: set `"trading_enabled": true, "dry_run": false` in `risk_config.json` and write `1` to `run/trading_enabled`.
4. Launch the executor persistently: `nohup python3 trade_executor.py > run/trade_executor.log 2>&1 &`
5. Watch `nova_journal.jsonl` for the first `commanded` decision; verify the fill in `nova_trades.jsonl`.

## Kill switch

- Telegram: `/stop` (writes `0` to `run/trading_enabled`) — instant, no new trades.
- Shell: `echo 0 > ~/workspace/mt5/bridge/run/trading_enabled`
- Resume: `/start` or `echo 1 > …` (config gates still apply).
- Nuclear: detach NovaTrader from the chart / disable Algo Trading in MT5.

## Learning loop

`review_trades.py` reads `nova_journal.jsonl` → per-symbol win rate, profit
factor, avg R, expectancy + per-session stats → writes
`run/review_proposal_<date>.md`. A weekly cron (Mon 08:00 America/Los_Angeles)
delivers the proposal for user approval, then `push_evidence.py --weekly`
pushes the week's journal slice + proposal to the evidence repo
`kilomene/forex-30day-trading-experiment`. **Proposals never auto-apply.**

## 30-day experiment (2026-09-21 → 2026-10-21)

The full experiment spec is [`EXPERIMENT_CHARTER.md`](EXPERIMENT_CHARTER.md).
Supporting tooling:

| Script | Schedule | Purpose |
|---|---|---|
| `daily_summary.py` | daily 23:55 PT (`experiment-daily-summary`) | day P&L, trades, win rate, expectancy → journal `daily_summary` + one line in `~/memory/YYYY-MM-DD.md` |
| `weekend_reflection.py` | Sat & Sun 10:00 PT (`experiment-saturday-reflection`, `experiment-sunday-reflection`) | stats by symbol/direction/session/weekday, streaks → journal `reflection` + `run/reflection_<date>.md`; proposals for approval only |
| `final_report.py` | once, 2026-10-21 09:00 PT (`experiment-final-report`) | compiles `run/FINAL_REPORT.md`: every trade, full stats, reflections, config history, honest accounting |
| `push_evidence.py` | `--init` / `--weekly` / `--final` | GitHub REST API push to `kilomene/forex-30day-trading-experiment` via the stored custom.github credential (surrogate helpers; raw token never read) |

Journal event taxonomy: `signal.received`, `trade.decision`, `command.sent`,
`trade.opened`, `trade.closed`, `trade.rejected`, `kill_switch.tripped`,
`config.changed`, `daily_summary`, `reflection`.

## Dry-run mode

With `dry_run: true` (the default) the executor tails signals, runs every
gate, computes the exact volume it *would* trade, journals `intended`
decisions — and writes **zero** commands. Verify with:
`python3 -m pytest tests/test_trader.py -q`.

## What this stack never does

- NovaSignals never trades (observation-only by design, forever).
- No real-money server: anything not matching "demo" is refused loudly.
- No credentials in files: Telegram token/chat id live only in process env.
- No auto-activation: `trading_enabled=false`, `dry_run=true` ship as defaults.
