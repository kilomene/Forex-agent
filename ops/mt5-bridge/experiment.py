#!/usr/bin/env python3
"""
Durable 30-day experiment lifecycle record.

Worker C (2026-09-23): part of the Forex trading system upgrade.

The experiment record lives at run/experiment.json and is recomputed from
the append-only journal (nova_journal.jsonl) plus nova_trades.jsonl --
DEMO scope only; entries tagged with a non-DEMO mode are never mixed in
(there are none yet, the filter is forward-looking).

Completion semantics (owner requirement):
  * When now >= end_time and status == "RUNNING", update_experiment_record()
    flips status to "DEMO_EXPERIMENT_COMPLETE", journals
    {"type": "experiment.completed", "status": "DEMO_EXPERIMENT_COMPLETE"},
    and raises the trade halt: run/experiment_halt is created and the
    record's trade_halt flag is set. While the halt is present the trade
    executor refuses ALL new entries ("skipped:experiment_complete").
  * Clearing the halt is an EXPLICIT OPERATOR ACTION ONLY:
    `python3 experiment.py --resume`. There is NO automatic resume and
    there is NO code path anywhere in this system that auto-switches to
    LIVE. Going live requires the operator to switch trading mode through
    the mode system (Worker A's trading_mode.py), never this module.

Usage:
    python3 experiment.py --init     create the record if missing
    python3 experiment.py --update   recompute metrics (+ complete if due)
    python3 experiment.py --status   print the record
    python3 experiment.py --resume   explicit operator action: clear halt
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RUN_DIR = os.path.join(BASE, "run")

EXPERIMENT_ID = "forex-30day-20260921"
START_TIME = "2026-09-21T00:00:00+00:00"
END_TIME = "2026-10-21T23:59:59+00:00"
DEMO_ACCOUNT = 112975129
BROKER = "MetaQuotes"
SERVER = "MetaQuotes-Demo"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETE = "DEMO_EXPERIMENT_COMPLETE"

# Journal types counted as execution errors (documented mapping).
EXECUTION_ERROR_TYPES = {"trade.rejected", "command.failed", "ea.rejected"}
RECONCILE_ERROR_TYPES = {"reconcile.error"}
BROKER_DISCONNECT_TYPES = {"broker.disconnect"}
AI_ERROR_TYPES = {"ai.error"}


def record_path(run_dir=None):
    return os.path.join(run_dir or DEFAULT_RUN_DIR, "experiment.json")


def halt_path(run_dir=None):
    return os.path.join(run_dir or DEFAULT_RUN_DIR, "experiment_halt")


def _atomic_write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".experiment_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def blank_record():
    return {
        "experiment_id": EXPERIMENT_ID,
        "start_time": START_TIME,
        "end_time": END_TIME,
        "demo_account": DEMO_ACCOUNT,
        "broker": BROKER,
        "server": SERVER,
        "strategy_version": "NovaSignals v1.01 / NovaTrader",
        "ai_version": "ai-engine-0.1-advisory",
        "risk_version": "risk-engine-c",
        # computed below; 0/None until the first --update
        "number_of_trades": 0,
        "wins": 0,
        "losses": 0,
        "net_profit": 0.0,
        "drawdown": 0.0,
        "profit_factor": None,
        "average_R": None,
        "maximum_losing_streak": 0,
        "execution_errors": 0,
        "reconciliation_errors": 0,
        "broker_disconnects": 0,
        "ai_errors": 0,
        "kill_switch_events": 0,
        "status": STATUS_RUNNING,
        "trade_halt": False,
        "completed_at": None,
        "last_updated": None,
    }


def init_record(run_dir=None):
    """Create the record if missing; never overwrite an existing one."""
    path = record_path(run_dir)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    rec = blank_record()
    rec["last_updated"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(path, rec)
    return rec


def _iter_jsonl(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _is_demo_scope(ev):
    """DEMO scope only: skip anything explicitly tagged with another mode."""
    mode = ev.get("mode")
    return mode is None or str(mode).upper() == "DEMO"


def _eff_profit(ev):
    for k in ("net_profit", "profit"):
        try:
            v = float(ev.get(k))
        except (TypeError, ValueError):
            continue
        if v == v:  # not NaN
            return v
    return 0.0


def compute_metrics(journal_path, trades_path=None):
    """Recompute experiment metrics from the journal (+ trades file).

    DEMO scope only. No journal rewrites -- pure read.
    """
    closes = {}  # ticket -> profit (dedupe; close_corrected wins)
    decisions = 0
    signals = 0
    execution_errors = 0
    reconciliation_errors = 0
    broker_disconnects = 0
    ai_errors = 0
    kill_switch_events = 0
    for ev in _iter_jsonl(journal_path):
        if not isinstance(ev, dict) or not _is_demo_scope(ev):
            continue
        t = ev.get("type")
        if t == "signal.received":
            signals += 1
        elif t == "trade.decision":
            decisions += 1
        elif t in EXECUTION_ERROR_TYPES:
            execution_errors += 1
        elif t in RECONCILE_ERROR_TYPES:
            reconciliation_errors += 1
        elif t in BROKER_DISCONNECT_TYPES:
            broker_disconnects += 1
        elif t in AI_ERROR_TYPES:
            ai_errors += 1
        elif t == "ai.decision":
            if not ev.get("valid", True):
                ai_errors += 1
        elif t == "kill_switch.tripped":
            kill_switch_events += 1
        elif t == "trade.closed":
            ticket = str(ev.get("ticket"))
            closes[ticket] = _eff_profit(ev)
        elif t == "trade.close_corrected":
            ticket = str(ev.get("ticket"))
            closes[ticket] = _eff_profit(ev)
    # The trades file is authoritative for fills the journal may have
    # missed; it only ADDS tickets, never overrides journal P&L.
    if trades_path:
        for ev in _iter_jsonl(trades_path):
            if not isinstance(ev, dict) or not _is_demo_scope(ev):
                continue
            if ev.get("type") == "trade.closed":
                ticket = str(ev.get("ticket"))
                closes.setdefault(ticket, _eff_profit(ev))
    profits = list(closes.values())
    n = len(profits)
    wins = sum(1 for p in profits if p > 0)
    losses = sum(1 for p in profits if p < 0)
    net = sum(profits)
    gross_win = sum(p for p in profits if p > 0)
    gross_loss = -sum(p for p in profits if p < 0)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        None if gross_win == 0 else float("inf"))
    # Max peak-to-trough drawdown on the cumulative P&L curve.
    peak, dd = 0.0, 0.0
    cum = 0.0
    for p in profits:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    # Longest run of consecutive losing closes (journal order).
    max_streak, cur = 0, 0
    for p in profits:
        cur = cur + 1 if p < 0 else 0
        max_streak = max(max_streak, cur)
    average_R = (net / (n * 100.0)) if n else None  # $100 planned/trade
    return {
        "number_of_trades": n,
        "wins": wins,
        "losses": losses,
        "net_profit": round(net, 2),
        "drawdown": round(dd, 2),
        "profit_factor": (round(profit_factor, 3)
                          if profit_factor not in (None, float("inf"))
                          else profit_factor),
        "average_R": round(average_R, 3) if average_R is not None else None,
        "maximum_losing_streak": max_streak,
        "execution_errors": execution_errors,
        "reconciliation_errors": reconciliation_errors,
        "broker_disconnects": broker_disconnects,
        "ai_errors": ai_errors,
        "kill_switch_events": kill_switch_events,
        "signals_received": signals,
        "decisions_made": decisions,
    }


def _journal_append(journal_path, entry):
    entry = dict(entry)
    entry.setdefault(
        "time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    d = os.path.dirname(os.path.abspath(journal_path))
    os.makedirs(d, exist_ok=True)
    with open(journal_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _parse_end(end_time):
    try:
        dt = datetime.fromisoformat(end_time)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def update_experiment_record(run_dir=None, journal_path=None,
                             trades_path=None, now=None):
    """Recompute the record; complete the experiment when past end_time.

    Returns the record dict. On completion: status becomes
    DEMO_EXPERIMENT_COMPLETE, the trade halt is raised (record flag +
    run/experiment_halt sentinel so the executor refuses new entries),
    and experiment.completed is journaled. NEVER auto-switches to LIVE.
    """
    run_dir = run_dir or DEFAULT_RUN_DIR
    journal_path = journal_path or os.path.join(run_dir, "nova_journal.jsonl")
    rec = init_record(run_dir)
    if rec.get("status") != STATUS_RUNNING:
        # Already complete: still refresh metrics, never re-transition.
        metrics = compute_metrics(journal_path, trades_path)
        rec.update(metrics)
        rec["last_updated"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(record_path(run_dir), rec)
        return rec
    metrics = compute_metrics(journal_path, trades_path)
    rec.update(metrics)
    end = _parse_end(rec.get("end_time") or END_TIME)
    now = now or datetime.now(timezone.utc)
    if end is not None and now >= end:
        rec["status"] = STATUS_COMPLETE
        rec["trade_halt"] = True
        rec["completed_at"] = now.isoformat()
        # Raise the halt the executor enforces: sentinel file + record flag.
        hp = halt_path(run_dir)
        with open(hp, "w", encoding="utf-8") as f:
            f.write("1\n")
        _journal_append(journal_path, {
            "type": "experiment.completed",
            "status": STATUS_COMPLETE,
            "experiment_id": rec.get("experiment_id"),
            "number_of_trades": rec.get("number_of_trades"),
            "net_profit": rec.get("net_profit"),
            "note": ("30-day demo experiment complete. New entries halted "
                     "until explicit operator action (experiment.py "
                     "--resume). NEVER auto-switches to LIVE."),
        })
    rec["last_updated"] = now.isoformat()
    _atomic_write_json(record_path(run_dir), rec)
    return rec


def resume_trading(run_dir=None):
    """EXPLICIT OPERATOR ACTION: clear the post-experiment trade halt.

    This only re-arms DEMO entries. It does NOT switch to LIVE -- there is
    no auto-switch path anywhere in this system.
    """
    run_dir = run_dir or DEFAULT_RUN_DIR
    rec = init_record(run_dir)
    rec["trade_halt"] = False
    rec["last_updated"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(record_path(run_dir), rec)
    try:
        os.unlink(halt_path(run_dir))
    except OSError:
        pass
    return rec


def main(argv):
    run_dir = DEFAULT_RUN_DIR
    args = []
    it = iter(argv[1:])
    for a in it:
        if a == "--run-dir":
            try:
                run_dir = next(it)
            except StopIteration:
                print("--run-dir needs a path", file=sys.stderr)
                return 2
        elif a.startswith("--run-dir="):
            run_dir = a.split("=", 1)[1]
        else:
            args.append(a)
    if "--update" in args:
        rec = update_experiment_record(run_dir)
        print(json.dumps(rec, indent=2))
    elif "--init" in args:
        print(json.dumps(init_record(run_dir), indent=2))
    elif "--status" in args:
        print(json.dumps(init_record(run_dir), indent=2))
    elif "--resume" in args:
        rec = resume_trading(run_dir)
        print("trade halt cleared (explicit operator action). "
              "Mode unchanged; no LIVE switch performed.")
        print(json.dumps({k: rec.get(k) for k in
                          ("status", "trade_halt")}, indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
