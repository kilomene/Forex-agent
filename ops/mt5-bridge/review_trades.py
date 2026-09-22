#!/usr/bin/env python3
"""
Nova trade review.

Reads nova_journal.jsonl (+ nova_trades.jsonl for fills, nova_signals.jsonl
for candle times) and writes a markdown review proposal to
run/review_proposal_<YYYY-MM-DD>.md with per-symbol stats (trades, wins,
win rate, profit factor, avg R, expectancy) and per broker-hour session
stats. Proposals NEVER auto-apply -- this script only writes the file.

If there are no closed trades yet: prints "no trades yet -- nothing to
review" and writes exactly that to the proposal file. Exit 0 always.

Env:
  MT5_FILES_DIR    dir with nova_signals.jsonl / nova_trades.jsonl
  TRADER_STATE_DIR state dir holding nova_journal.jsonl (default: <bridge dir>/run)
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.environ.get(
    "MT5_FILES_DIR",
    os.path.expanduser(
        "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files"
    ),
)
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))

SIGNALS_PATH = os.path.join(FILES_DIR, "nova_signals.jsonl")
TRADES_PATH = os.path.join(FILES_DIR, "nova_trades.jsonl")
JOURNAL_PATH = os.path.join(STATE_DIR, "nova_journal.jsonl")


def parse_mt5_time(s):
    if not s:
        return None
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def load_signals():
    """signal_id -> candle_time string."""
    out = {}
    try:
        with open(SIGNALS_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    sig = json.loads(line)
                except ValueError:
                    continue
                if sig.get("id"):
                    out[sig["id"]] = sig.get("candle_time")
    except OSError:
        pass
    return out


def load_journal():
    """signal_id -> decision dict (last decision wins)."""
    out = {}
    try:
        with open(JOURNAL_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "trade.decision" and e.get("signal_id"):
                    out[e["signal_id"]] = e
    except OSError:
        pass
    return out


def load_trades():
    """(opened: ticket -> info, closed: list of {ticket, profit, ...})."""
    opened, closed = {}, []
    try:
        with open(TRADES_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "trade.opened" and ev.get("ticket") is not None:
                    opened[ev["ticket"]] = ev
                elif ev.get("type") == "trade.closed":
                    closed.append(ev)
    except OSError:
        pass
    return opened, closed


def stats_for(profits):
    n = len(profits)
    wins = sum(1 for p in profits if p > 0)
    gross_profit = sum(p for p in profits if p > 0)
    gross_loss = -sum(p for p in profits if p < 0)
    return {
        "trades": n,
        "wins": wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "total": sum(profits),
        "expectancy": (sum(profits) / n) if n else 0.0,
    }


def build_review():
    decisions = load_journal()
    opened, closed = load_trades()
    candle_of = load_signals()

    trades = []  # closed trades enriched with symbol/risk/candle hour
    for ev in closed:
        ticket = ev.get("ticket")
        op = opened.get(ticket, {})
        sig_id = op.get("signal_id")
        dec = decisions.get(sig_id, {}) if sig_id else {}
        symbol = op.get("symbol") or dec.get("symbol") or "?"
        risk = dec.get("risk_amount") or 0
        try:
            profit = float(ev.get("profit") or 0)
        except (TypeError, ValueError):
            profit = 0.0
        r = (profit / risk) if risk else 0.0
        dt = parse_mt5_time(candle_of.get(sig_id)) if sig_id else None
        trades.append({
            "symbol": symbol, "profit": profit, "r": r,
            "hour": dt.hour if dt else None,
        })

    if not trades:
        return None

    by_symbol = defaultdict(list)
    by_hour = defaultdict(list)
    for t in trades:
        by_symbol[t["symbol"]].append(t["profit"])
        if t["hour"] is not None:
            by_hour[t["hour"]].append(t["profit"])
    r_by_symbol = defaultdict(list)
    for t in trades:
        r_by_symbol[t["symbol"]].append(t["r"])

    sym_rows = []
    for sym, profits in sorted(by_symbol.items()):
        s = stats_for(profits)
        rs = r_by_symbol[sym]
        s["avg_r"] = sum(rs) / len(rs) if rs else 0.0
        s["symbol"] = sym
        sym_rows.append(s)

    hour_rows = []
    for h, profits in sorted(by_hour.items()):
        s = stats_for(profits)
        s["hour"] = h
        hour_rows.append(s)

    overall = stats_for([t["profit"] for t in trades])
    all_r = [t["r"] for t in trades]
    overall["avg_r"] = sum(all_r) / len(all_r) if all_r else 0.0

    return {"overall": overall, "symbols": sym_rows, "hours": hour_rows,
            "n": len(trades)}


def fmt_money(x):
    return f"{x:+.2f}"


def render_md(review):
    o = review["overall"]
    lines = []
    lines.append(f"# Trade review proposal — {datetime.now().date().isoformat()}")
    lines.append("")
    lines.append(f"Closed trades reviewed: **{review['n']}**")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Trades | {o['trades']} |")
    lines.append(f"| Win rate | {o['win_rate']:.1f}% |")
    pf = "inf" if o["profit_factor"] == float("inf") else f"{o['profit_factor']:.2f}"
    lines.append(f"| Profit factor | {pf} |")
    lines.append(f"| Avg R | {o['avg_r']:.2f} |")
    lines.append(f"| Expectancy (per trade) | {fmt_money(o['expectancy'])} |")
    lines.append(f"| Total profit | {fmt_money(o['total'])} |")
    lines.append("")
    lines.append("## Per-symbol stats")
    lines.append("")
    lines.append("| Symbol | Trades | Wins | Win rate | Profit factor | Avg R | "
                 "Expectancy | Total |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in sorted(review["symbols"], key=lambda r: r["total"]):
        pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        lines.append(
            f"| {s['symbol']} | {s['trades']} | {s['wins']} | "
            f"{s['win_rate']:.1f}% | {pf} | {s['avg_r']:.2f} | "
            f"{fmt_money(s['expectancy'])} | {fmt_money(s['total'])} |"
        )
    top = max(review["symbols"], key=lambda r: r["total"])
    worst = min(review["symbols"], key=lambda r: r["total"])
    lines.append("")
    lines.append(f"Top symbol: **{top['symbol']}** ({fmt_money(top['total'])}, "
                 f"{top['win_rate']:.1f}% win rate).")
    lines.append(f"Worst symbol: **{worst['symbol']}** ({fmt_money(worst['total'])}, "
                 f"{worst['win_rate']:.1f}% win rate).")
    lines.append("")
    lines.append("## Session notes (broker hour of signal candle)")
    lines.append("")
    lines.append("| Hour (broker) | Trades | Win rate | Total |")
    lines.append("|---|---|---|---|")
    for h in review["hours"]:
        lines.append(f"| {h['hour']:02d}:00 | {h['trades']} | "
                     f"{h['win_rate']:.1f}% | {fmt_money(h['total'])} |")
    neg_hours = [h for h in review["hours"]
                 if h["total"] < 0 and h["trades"] >= 5]
    if neg_hours:
        lines.append("")
        lines.append("Negative-expectancy sessions (>=5 trades): " +
                     ", ".join(f"{h['hour']:02d}:00 ({fmt_money(h['total'])})"
                               for h in neg_hours) + ".")
    lines.append("")
    lines.append("## Proposed changes")
    lines.append("")
    lines.append("These are proposals only. Nothing is applied automatically.")
    lines.append("")
    n = 1
    weak = [s for s in review["symbols"]
            if s["trades"] >= 20 and s["win_rate"] < 35]
    for s in weak:
        lines.append(f"{n}. Exclude {s['symbol']}: {s['win_rate']:.1f}% win rate "
                     f"over {s['trades']} trades is below the 35% cutoff.")
        n += 1
    thin = [s for s in review["symbols"]
            if s["trades"] < 20 and s["total"] < 0]
    for s in thin:
        lines.append(f"{n}. Watch {s['symbol']}: negative total "
                     f"({fmt_money(s['total'])}) on only {s['trades']} trades -- "
                     f"collect more data before excluding.")
        n += 1
    for h in neg_hours:
        lines.append(f"{n}. Consider skipping the {h['hour']:02d}:00 broker-hour "
                     f"session: {fmt_money(h['total'])} over {h['trades']} trades.")
        n += 1
    if o["profit_factor"] != float("inf") and o["profit_factor"] < 1.0:
        lines.append(f"{n}. Overall profit factor {o['profit_factor']:.2f} < 1.0: "
                     f"tighten entries (e.g. require RSI confirmation) or widen "
                     f"take-profits before adding symbols.")
        n += 1
    low_r = [s for s in review["symbols"]
             if s["trades"] >= 20 and s["avg_r"] < 0.5]
    for s in low_r:
        lines.append(f"{n}. {s['symbol']} avg R is {s['avg_r']:.2f}: review its "
                     f"SL/TP placement -- stops may be too wide for the targets.")
        n += 1
    if n == 1:
        lines.append("1. No changes proposed: all symbols and sessions are "
                     "within tolerance so far.")
    lines.append("")
    return "\n".join(lines)


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    day = datetime.now().date().isoformat()
    out_path = os.path.join(STATE_DIR, f"review_proposal_{day}.md")
    review = build_review()
    if review is None:
        msg = "no trades yet -- nothing to review"
        print(msg)
        with open(out_path, "w") as f:
            f.write(msg + "\n")
        return 0
    md = render_md(review)
    with open(out_path, "w") as f:
        f.write(md)
    print(f"wrote {out_path} ({review['n']} closed trades reviewed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
