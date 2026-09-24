#!/usr/bin/env python3
"""
Nova weekend reflection (Sat + Sun 10:00 PT crons) for the 30-day experiment.

Reads run/nova_journal.jsonl, computes win rate by symbol / direction /
session / weekday, avg win, avg loss, expectancy, and streaks. Writes
`reflection` records to the journal and a proposal file
run/reflection_<YYYY-MM-DD>.md. Proposals are FOR APPROVAL ONLY --
this script never changes strategy or config.

Env:
  TRADER_STATE_DIR  state dir holding nova_journal.jsonl (default: <bridge dir>/run)
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

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
        "trades": n, "wins": wins,
        "win_rate": wins / n * 100 if n else 0.0,
        "avg_win": gross_w / wins if wins else 0.0,
        "avg_loss": -(gross_l / (n - wins)) if n - wins else 0.0,
        "expectancy": sum(profits) / n if n else 0.0,
        "total": sum(profits),
        "profit_factor": gross_w / gross_l if gross_l > 0 else float("inf"),
    }


def main():
    today = datetime.now().date().isoformat()
    journal = load_journal()
    closes = [e for e in journal if e.get("type") == "trade.closed"]

    decisions = {}
    for e in journal:
        if e.get("type") == "trade.decision" and e.get("signal_id"):
            decisions[e["signal_id"]] = e

    if not closes:
        print("no closed trades yet -- nothing to reflect on")
        with open(os.path.join(STATE_DIR, f"reflection_{today}.md"), "w") as f:
            f.write(f"# Weekend reflection {today}\n\nno closed trades yet.\n")
        return 0

    # enrich closes with symbol/direction/session/weekday from the decision
    enriched = []
    for e in closes:
        d = decisions.get(e.get("signal_id"), {})
        ctx = d.get("context", {}) or {}
        try:
            profit = float(e.get("profit") or 0)
        except (TypeError, ValueError):
            profit = 0.0
        enriched.append({
            "symbol": e.get("symbol") or d.get("symbol") or "?",
            "direction": e.get("direction") or d.get("direction") or "?",
            "session": ctx.get("session") or "?",
            "weekday": ctx.get("weekday") or "?",
            "profit": profit,
            "time": str(e.get("time", "")),
        })
    enriched.sort(key=lambda r: r["time"])

    by_symbol = defaultdict(list)
    by_dir = defaultdict(list)
    by_session = defaultdict(list)
    by_weekday = defaultdict(list)
    for r in enriched:
        by_symbol[r["symbol"]].append(r["profit"])
        by_dir[r["direction"]].append(r["profit"])
        by_session[r["session"]].append(r["profit"])
        by_weekday[r["weekday"]].append(r["profit"])

    # streaks (chronological)
    best_win = worst_loss = cur = 0
    cw = cl = 0
    for r in enriched:
        if r["profit"] > 0:
            cw += 1
            cl = 0
            best_win = max(best_win, cw)
        elif r["profit"] < 0:
            cl += 1
            cw = 0
            worst_loss = max(worst_loss, cl)

    overall = stats([r["profit"] for r in enriched])
    reflection = {
        "type": "reflection",
        "date": today,
        "overall": overall,
        "by_symbol": {k: stats(v) for k, v in sorted(by_symbol.items())},
        "by_direction": {k: stats(v) for k, v in sorted(by_dir.items())},
        "by_session": {k: stats(v) for k, v in sorted(by_session.items())},
        "by_weekday": {k: stats(v) for k, v in sorted(by_weekday.items())},
        "longest_win_streak": best_win,
        "longest_loss_streak": worst_loss,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(reflection) + "\n")

    # proposal file (approval only)
    lines = [f"# Weekend reflection — {today}", "",
             f"Closed trades so far: **{overall['trades']}**",
             f"Win rate: **{overall['win_rate']:.1f}%** | "
             f"Avg win: {overall['avg_win']:+.2f} | "
             f"Avg loss: {overall['avg_loss']:+.2f} | "
             f"Expectancy: {overall['expectancy']:+.2f}/trade | "
             f"Total: {overall['total']:+.2f}",
             f"Longest win streak: {best_win}, longest loss streak: {worst_loss}",
             ""]
    lines.append("## By symbol")
    for sym, s in sorted(reflection["by_symbol"].items(),
                         key=lambda kv: kv[1]["total"]):
        lines.append(f"- {sym}: {s['trades']} trades, {s['win_rate']:.1f}% win, "
                     f"{s['total']:+.2f}")
    lines.append("")
    lines.append("## By direction")
    for d, s in sorted(reflection["by_direction"].items()):
        lines.append(f"- {d}: {s['trades']} trades, {s['win_rate']:.1f}% win, "
                     f"{s['total']:+.2f}")
    lines.append("")
    lines.append("## By session (server time)")
    for sname, s in sorted(reflection["by_session"].items()):
        lines.append(f"- {sname}: {s['trades']} trades, {s['win_rate']:.1f}% "
                     f"win, {s['total']:+.2f}")
    lines.append("")
    lines.append("## By weekday")
    for wday, s in sorted(reflection["by_weekday"].items()):
        lines.append(f"- {wday}: {s['trades']} trades, {s['win_rate']:.1f}% "
                     f"win, {s['total']:+.2f}")
    lines.append("")
    lines.append("## Proposed tweaks (require Zenas's approval — never auto-applied)")
    n = 1
    weak = [k for k, s in reflection["by_symbol"].items()
            if s["trades"] >= 20 and s["win_rate"] < 35]
    for sym in weak:
        s = reflection["by_symbol"][sym]
        lines.append(f"{n}. Consider excluding {sym}: {s['win_rate']:.1f}% win "
                     f"rate over {s['trades']} trades.")
        n += 1
    bad_sess = [k for k, s in reflection["by_session"].items()
                if s["trades"] >= 10 and s["total"] < 0]
    for sname in bad_sess:
        s = reflection["by_session"][sname]
        lines.append(f"{n}. Consider skipping the {sname} session: "
                     f"{s['total']:+.2f} over {s['trades']} trades.")
        n += 1
    if worst_loss >= 5:
        lines.append(f"{n}. Longest losing streak is {worst_loss}: consider a "
                     f"consecutive-loss circuit breaker (e.g. pause after 4 "
                     f"straight losers).")
        n += 1
    if overall["avg_loss"] != 0 and \
            overall["avg_win"] / abs(overall["avg_loss"]) < 1.0:
        lines.append(f"{n}. Avg win ({overall['avg_win']:+.2f}) is smaller than "
                     f"avg loss ({overall['avg_loss']:+.2f}): review SL/TP "
                     f"placement.")
        n += 1
    if n == 1:
        lines.append("1. No changes proposed: everything within tolerance.")
    lines.append("")
    out_path = os.path.join(STATE_DIR, f"reflection_{today}.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {out_path} ({overall['trades']} closed trades reflected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
