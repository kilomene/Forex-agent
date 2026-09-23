#!/usr/bin/env python3
"""
Nova daily summary (23:55 PT cron) for the 30-day experiment.

Reads run/nova_journal.jsonl, computes today's stats (day P&L, trades,
win rate, expectancy), appends a `daily_summary` event to the journal and
one line to ~/memory/YYYY-MM-DD.md.

Env:
  TRADER_STATE_DIR  state dir holding nova_journal.jsonl (default: <bridge dir>/run)
  MEMORY_DIR        daily-log dir (default: ~/memory)
"""

import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))
MEMORY_DIR = os.environ.get("MEMORY_DIR", os.path.expanduser("~/memory"))
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


def event_date(ev):
    """Normalize an event's date to YYYY-MM-DD.

    The EA writes dotted MT5 timestamps ("2026.09.22 02:58:01") while
    executor-side events use dashed ISO ("2026-09-22 06:10:54"); both
    must match the summary day or closes get silently dropped.
    """
    return str(ev.get("time", ""))[:10].replace(".", "-")


def main():
    today = datetime.now().date().isoformat()
    journal = load_journal()

    closes = [e for e in journal
              if e.get("type") == "trade.closed" and
              event_date(e) == today]
    decisions = [e for e in journal
                 if e.get("type") == "trade.decision" and
                 event_date(e) == today]
    signals = [e for e in journal
               if e.get("type") == "signal.received" and
               event_date(e) == today]
    trips = [e for e in journal
             if e.get("type") == "kill_switch.tripped" and
             event_date(e) == today]

    profits = []
    for e in closes:
        try:
            profits.append(float(e.get("profit") or 0))
        except (TypeError, ValueError):
            pass
    wins = sum(1 for p in profits if p > 0)
    day_pnl = sum(profits)
    expectancy = (day_pnl / len(profits)) if profits else 0.0
    win_rate = (wins / len(profits) * 100) if profits else 0.0

    summary = {
        "type": "daily_summary",
        "date": today,
        "signals": len(signals),
        "decisions": len(decisions),
        "trades_closed": len(closes),
        "wins": wins,
        "losses": len(profits) - wins,
        "day_pnl": round(day_pnl, 2),
        "win_rate_pct": round(win_rate, 1),
        "expectancy": round(expectancy, 2),
        "kill_switch_trips": len(trips),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(summary) + "\n")

    line = (f"- 23:55 PT: 30-day experiment day summary {today}: "
            f"{len(signals)} signals, {len(closes)} trades closed, "
            f"P&L {day_pnl:+.2f}, win rate {win_rate:.1f}%, "
            f"expectancy {expectancy:+.2f}/trade, "
            f"kill-switch trips {len(trips)}.")
    os.makedirs(MEMORY_DIR, exist_ok=True)
    mem_path = os.path.join(MEMORY_DIR, f"{today}.md")
    with open(mem_path, "a") as f:
        f.write(line + "\n")
    # Worker C (2026-09-23): refresh the durable experiment record from
    # the journal. Guarded so a failure here can never break the summary.
    try:
        import experiment as _experiment
        _rec = _experiment.update_experiment_record(STATE_DIR)
        if _rec.get("status") == _experiment.STATUS_COMPLETE:
            note = (f"- 23:55 PT: experiment {_rec['experiment_id']} "
                    f"reached DEMO_EXPERIMENT_COMPLETE; new entries halted "
                    f"until explicit operator action.\n")
            with open(mem_path, "a") as f:
                f.write(note)
            print(note.strip())
    except Exception as e:
        print(f"warning: experiment record update failed: {e}")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
