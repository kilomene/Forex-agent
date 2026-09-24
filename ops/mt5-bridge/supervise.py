#!/usr/bin/env python3
"""
Daemon supervisor for the Nova trading stack.

Ensures the three persistent python daemons are alive, relaunching any
that died. Run every 5 minutes via cron (daemon-supervisor cron).

Daemons:
  trade_executor.py   takes signals -> sends trade commands to the EA
  signal_bridge.py    broadcasts signal.detected to Telegram subscribers
  tg_subscribers.py   public bot /start /stop subscription handling

Logs relaunches to run/supervisor.log. Never touches trading config.
"""

import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(BASE, "run")
os.makedirs(RUN_DIR, exist_ok=True)
LOG = os.path.join(RUN_DIR, "supervisor.log")

# EA-owned files live under the MT5 Files dir (same default the executor
# uses). Kept as a local constant so this supervisor never imports the
# executor module -- a broken trade_executor.py must not be able to take
# down daemon supervision.
FILES_DIR = os.path.expanduser(
    "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files")

DAEMONS = ("trade_executor.py", "signal_bridge.py", "tg_subscribers.py")


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def alive(script):
    # Match the script basename anywhere in the command line: daemons are
    # launched with an absolute path (sys.executable + BASE/script), so a
    # "python3 <basename>" pattern never matches. The [x] trick keeps pgrep
    # from matching itself.
    try:
        pat = f"[{script[0]}]{script[1:]}"
        r = subprocess.run(
            ["pgrep", "-f", pat],
            capture_output=True, text=True, timeout=10)
        return bool(r.stdout.strip())
    except Exception:
        return False


def audit_seen_tickets(files_dir=FILES_DIR, run_dir=RUN_DIR):
    """Read-only 5-minute audit of the EA-owned nova_positions_seen.json.

    Calls ticket_tracker.audit_seen_file() and logs SEEN-AUDIT lines.
    Never writes the EA file, the trades file, or the journal -- findings
    are LOUD in the supervisor log; journaling them is deferred to a
    Zenas-authorized restart (see ticket_tracker docstring).
    """
    try:
        import ticket_tracker
    except Exception as e:
        log(f"seen-audit: SKIPPED (import failed: {e})")
        return
    seen = os.path.join(files_dir, "nova_positions_seen.json")
    trades = os.path.join(files_dir, "nova_trades.jsonl")
    journal = os.path.join(run_dir, "nova_journal.jsonl")
    try:
        rep = ticket_tracker.audit_seen_file(seen, trades, journal)
    except Exception as e:
        log(f"seen-audit: ERROR {e}")
        return
    parsed, verify = rep["parsed"], rep["verify"]
    hist = rep["history"]
    if verify["ok"] and not parsed["corrupt"]:
        log(f"seen-audit: OK "
            f"(seen={len(parsed['tickets'])} "
            f"tickets, open={len(hist['open_tickets'])}, "
            f"closed={len(hist['closed_tickets'])})")
        return
    log(f"seen-audit: !!! FINDING ok={verify['ok']} "
        f"corrupt_spans={verify['corrupt_spans']} "
        f"untracked_open={verify.get('untracked_open')} "
        f"tracked_but_closed={verify.get('tracked_but_closed')} "
        f"duplicate_tickets={verify.get('duplicate_tickets')} "
        f"phantoms={hist['phantoms']} mirror_gaps={hist['mirror_gaps']}")


def main():
    relaunched = []
    for script in DAEMONS:
        if alive(script):
            continue
        log(f"{script} not running -> relaunching")
        try:
            subprocess.Popen(
                [sys.executable, os.path.join(BASE, script)],
                stdout=open(os.path.join(RUN_DIR,
                                          script.replace(".py", ".log")), "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True)
            relaunched.append(script)
        except Exception as e:
            log(f"FAILED to relaunch {script}: {e}")
    if relaunched:
        log(f"relaunched: {', '.join(relaunched)}")
    else:
        log("all daemons alive")
    # Repair round 2 (2026-09-24): the seen-file watchdog finally has a
    # production caller. Read-only; findings go to this log.
    audit_seen_tickets()
    return 0


if __name__ == "__main__":
    sys.exit(main())
