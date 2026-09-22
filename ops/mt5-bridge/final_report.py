#!/usr/bin/env python3
"""
Nova day-30 final report compiler for the 30-day experiment.

Reads run/nova_journal.jsonl and compiles run/FINAL_REPORT.md:
every trade, full statistics, all reflections, config history, kill-switch
trips, daily summaries — wins AND losses, exactly as journaled.
Run push_evidence.py --final afterwards to publish.

Env:
  TRADER_STATE_DIR  state dir holding nova_journal.jsonl (default: <bridge dir>/run)
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))
JOURNAL_PATH = os.path.join(STATE_DIR, "nova_journal.jsonl")


def load_journal():
    out = []
    try:
        with open(JOURNAL_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def stats(profits):
    n = len(profits)
    wins = sum(1 for p in profits if p > 0)
    gross_w = sum(p for p in profits if p > 0)
    gross_l = -sum(p for p in profits if p < 0)
    return {
        "trades": n, "wins": wins, "losses": n - wins,
        "win_rate": wins / n * 100 if n else 0.0,
        "avg_win": gross_w / wins if wins else 0.0,
        "avg_loss": -(gross_l / (n - wins)) if n - wins else 0.0,
        "expectancy": sum(profits) / n if n else 0.0,
        "total": sum(profits),
        "profit_factor": gross_w / gross_l if gross_l > 0 else float("inf"),
        "best": max(profits) if profits else 0.0,
        "worst": min(profits) if profits else 0.0,
    }


def m(x):
    return f"{x:+.2f}"


def main():
    journal = load_journal()
    today = datetime.now().date().isoformat()

    signals = [e for e in journal if e.get("type") == "signal.received"]
    decisions = [e for e in journal if e.get("type") == "trade.decision"]
    opened = [e for e in journal if e.get("type") == "trade.opened"]
    closed = [e for e in journal if e.get("type") == "trade.closed"]
    trips = [e for e in journal if e.get("type") == "kill_switch.tripped"]
    cfg_changes = [e for e in journal if e.get("type") == "config.changed"]
    reflections = [e for e in journal if e.get("type") == "reflection"]
    dailies = [e for e in journal if e.get("type") == "daily_summary"]

    profits = []
    for e in closed:
        try:
            profits.append(float(e.get("profit") or 0))
        except (TypeError, ValueError):
            pass
    o = stats(profits)

    by_symbol = defaultdict(list)
    by_dir = defaultdict(list)
    for e in closed:
        try:
            p = float(e.get("profit") or 0)
        except (TypeError, ValueError):
            continue
        by_symbol[e.get("symbol") or "?"].append(p)
        by_dir[e.get("direction") or "?"].append(p)

    dec_counts = defaultdict(int)
    for e in decisions:
        dec_counts[e.get("decision") or "?"] += 1

    L = []
    L.append("# FINAL REPORT — 30-Day Autonomous Demo Trading Experiment")
    L.append("")
    L.append(f"Compiled: {today} | Experiment: 2026-09-21 → 2026-10-21")
    L.append("Account: MetaQuotes-Demo (demo, hedging). No real money was "
             "ever at risk.")
    L.append("")
    L.append("## The goal, and what actually happened")
    L.append("")
    L.append("The goal was for wins to be much greater than losses. "
             "This report shows exactly what happened — every trade below, "
             "wins and losses alike.")
    L.append("")
    L.append("## Headline statistics")
    L.append("")
    L.append("| Metric | Value |")
    L.append("|---|---|")
    L.append(f"| Signals received | {len(signals)} |")
    L.append(f"| Decisions journaled | {len(decisions)} |")
    L.append(f"| Trades opened | {len(opened)} |")
    L.append(f"| Trades closed | {len(closed)} |")
    L.append(f"| Wins / Losses | {o['wins']} / {o['losses']} |")
    L.append(f"| Win rate | {o['win_rate']:.1f}% |")
    L.append(f"| Total P&L | {m(o['total'])} |")
    L.append(f"| Avg win | {m(o['avg_win'])} |")
    L.append(f"| Avg loss | {m(o['avg_loss'])} |")
    L.append(f"| Expectancy per trade | {m(o['expectancy'])} |")
    pf = "inf" if o["profit_factor"] == float("inf") else \
        f"{o['profit_factor']:.2f}"
    L.append(f"| Profit factor | {pf} |")
    L.append(f"| Best trade | {m(o['best'])} |")
    L.append(f"| Worst trade | {m(o['worst'])} |")
    L.append(f"| Kill-switch trips | {len(trips)} |")
    L.append("")
    L.append("## Decision breakdown")
    L.append("")
    for dec, n in sorted(dec_counts.items()):
        L.append(f"- {dec}: {n}")
    L.append("")
    L.append("## Per-symbol results")
    L.append("")
    L.append("| Symbol | Trades | Win rate | Total P&L |")
    L.append("|---|---|---|---|")
    for sym, ps in sorted(by_symbol.items(), key=lambda kv: sum(kv[1])):
        s = stats(ps)
        L.append(f"| {sym} | {s['trades']} | {s['win_rate']:.1f}% | "
                 f"{m(s['total'])} |")
    L.append("")
    L.append("## Per-direction results")
    L.append("")
    for d, ps in sorted(by_dir.items()):
        s = stats(ps)
        L.append(f"- {d}: {s['trades']} trades, {s['win_rate']:.1f}% win, "
                 f"{m(s['total'])}")
    L.append("")
    L.append("## Every closed trade")
    L.append("")
    L.append("| # | Time | Symbol | Dir | Vol | Entry | Exit | P&L | "
             "Hold | Reason |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for i, e in enumerate(sorted(closed, key=lambda x: str(x.get("time"))),
                                 1):
        hold = e.get("hold_seconds")
        hold_s = f"{hold // 60}m" if isinstance(hold, int) else "?"
        try:
            p = float(e.get("profit") or 0)
        except (TypeError, ValueError):
            p = 0.0
        L.append(f"| {i} | {e.get('time', '?')} | {e.get('symbol', '?')} | "
                 f"{e.get('direction', '?')} | {e.get('volume', '?')} | "
                 f"{e.get('entry_price', '?')} | {e.get('exit_price', '?')} | "
                 f"{m(p)} | {hold_s} | {e.get('reason', '?')} |")
    L.append("")
    L.append("## Config history")
    L.append("")
    if cfg_changes:
        for e in cfg_changes:
            L.append(f"- {e.get('time', '?')}: {e.get('old')} → {e.get('new')}")
    else:
        L.append("No config changes during the experiment.")
    L.append("")
    L.append("## Kill-switch trips")
    L.append("")
    if trips:
        for e in trips:
            L.append(f"- {e.get('time', '?')}: {e.get('reason')} "
                     f"(today's P&L at trip: {e.get('todays_profit')})")
    else:
        L.append("None.")
    L.append("")
    L.append("## Daily summaries")
    L.append("")
    for e in sorted(dailies, key=lambda x: str(x.get("date"))):
        L.append(f"- {e.get('date')}: {e.get('trades_closed', 0)} closed, "
                 f"P&L {m(e.get('day_pnl', 0))}, win rate "
                 f"{e.get('win_rate_pct', 0)}%")
    L.append("")
    L.append("## Reflections captured")
    L.append("")
    L.append(f"{len(reflections)} reflection records journaled; "
             f"proposal files live under evidence/.")
    L.append("")
    L.append("## Honest accounting")
    L.append("")
    if o["total"] > 0:
        L.append(f"The experiment ended **profitable**: {m(o['total'])} "
                 f"over {o['trades']} closed trades.")
    elif o["total"] < 0:
        L.append(f"The experiment ended **at a loss**: {m(o['total'])} "
                 f"over {o['trades']} closed trades. The goal — wins much "
                 f"greater than losses — was not met.")
    else:
        L.append("The experiment ended flat: no net P&L.")
    L.append("")
    L.append("Nothing here is cherry-picked: the full journal "
             "(`evidence/nova_journal_full.jsonl`) contains every signal, "
             "every decision, and every fill.")

    out_path = os.path.join(STATE_DIR, "FINAL_REPORT.md")
    with open(out_path, "w") as f:
        f.write("\n".join(L) + "\n")
    print(f"wrote {out_path} ({len(closed)} closed trades, "
          f"P&L {m(o['total'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
