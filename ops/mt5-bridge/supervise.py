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
    return 0


if __name__ == "__main__":
    sys.exit(main())
