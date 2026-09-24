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
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))
MEMORY_DIR = os.environ.get("MEMORY_DIR", os.path.expanduser("~/memory"))
JOURNAL_PATH = os.path.join(STATE_DIR, "nova_journal.jsonl")


def load_journal(journal_path=JOURNAL_PATH):
    out = []
    try:
        with open(journal_path, encoding="utf-8", errors="replace") as f:
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


def _eff_profit(ev):
    """Authoritative P&L for one close event.

    net_profit (gross profit + commission + swap) is preferred over the
    bare profit field. Mirrors trade_executor.eff_profit() (gate
    accounting) and experiment._eff_profit() (experiment record) so the
    daily summary agrees with both.
    """
    for k in ("net_profit", "profit"):
        try:
            v = float(ev.get(k))
        except (TypeError, ValueError):
            continue
        if v == v:  # not NaN
            return v
    return 0.0


def _close_date(ev):
    """Day attribution for a close event.

    A trade.close_corrected revision carries the authoritative broker
    exit time; otherwise the event's own time. Dotted MT5 timestamps and
    dashed ISO both normalize to YYYY-MM-DD.
    """
    t = ev.get("exit_time_broker") or ev.get("time")
    return str(t or "")[:10].replace(".", "-")


def summarize_day(journal, today):
    """Per-ticket effective P&L for one day.

    A trade.close_corrected revision supersedes that ticket's provisional
    trade.closed (its swap/commission-corrected net wins) REGARDLESS of
    journal order: a late provisional close can never clobber an earlier
    correction (repair round 2, 2026-09-24). Each ticket counts exactly
    once, dated by its authoritative close time. Closes with no ticket
    are quarantined individually -- they are never merged into one
    bucket (old code keyed them all as str(None)).
    Returns (day_pnl, profits).
    """
    closes = {}  # ticket -> [date, pnl, is_correction]
    ticketless = []  # [(date, pnl)] -- each counted on its own
    for e in journal:
        t = e.get("type")
        if t not in ("trade.closed", "trade.close_corrected"):
            continue
        is_correction = (t == "trade.close_corrected")
        ticket = e.get("ticket")
        date, pnl = _close_date(e), _eff_profit(e)
        if ticket is None or ticket == "":
            ticketless.append((date, pnl))
            continue
        key = str(ticket)
        prev = closes.get(key)
        if prev is None or is_correction or not prev[2]:
            # A correction always wins (any order); a provisional close
            # only replaces another provisional (last-wins, as before).
            closes[key] = (date, pnl, is_correction)
    profits = [pnl for (d, pnl, _c) in closes.values() if d == today]
    profits += [pnl for (d, pnl) in ticketless if d == today]
    return sum(profits), profits


def main(journal_path=JOURNAL_PATH, today=None, memory_dir=MEMORY_DIR):
    today = today or datetime.now().date().isoformat()
    journal = load_journal(journal_path)

    decisions = [e for e in journal
                 if e.get("type") == "trade.decision" and
                 event_date(e) == today]
    signals = [e for e in journal
               if e.get("type") == "signal.received" and
               event_date(e) == today]
    trips = [e for e in journal
             if e.get("type") == "kill_switch.tripped" and
             event_date(e) == today]

    # Day P&L from per-ticket effective (net, correction-aware) P&L, not
    # the bare profit field: a trade.close_corrected revision (authoritative
    # broker net incl. swap/commission) supersedes the provisional close.
    day_pnl, profits = summarize_day(journal, today)
    wins = sum(1 for p in profits if p > 0)
    expectancy = (day_pnl / len(profits)) if profits else 0.0
    win_rate = (wins / len(profits) * 100) if profits else 0.0

    summary = {
        "type": "daily_summary",
        "date": today,
        "signals": len(signals),
        "decisions": len(decisions),
        "trades_closed": len(profits),
        "wins": wins,
        "losses": len(profits) - wins,
        "day_pnl": round(day_pnl, 2),
        "win_rate_pct": round(win_rate, 1),
        "expectancy": round(expectancy, 2),
        "kill_switch_trips": len(trips),
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(journal_path, "a") as f:
        f.write(json.dumps(summary) + "\n")

    line = (f"- 23:55 PT: 30-day experiment day summary {today}: "
            f"{len(signals)} signals, {len(profits)} trades closed, "
            f"P&L {day_pnl:+.2f}, win rate {win_rate:.1f}%, "
            f"expectancy {expectancy:+.2f}/trade, "
            f"kill-switch trips {len(trips)}.")
    os.makedirs(memory_dir, exist_ok=True)
    mem_path = os.path.join(memory_dir, f"{today}.md")
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
