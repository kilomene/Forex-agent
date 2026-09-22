#!/usr/bin/env python3
"""
Unified self-healing supervisor for the Nova MT5 trading stack.

One periodic healer owns the whole stack in dependency order:

  1. wine     - reinstall from the local apt archive cache if a platform
                container restart wiped the system layer (no network needed)
  2. Xvfb     - :99 display server the terminal needs for its GUI
  3. terminal - MT5 terminal64.exe via scripts/run-mt5-tunnel.sh, which sets
                the LD_PRELOAD CONNECT shim (mandatory: without it the
                terminal never authorizes through the egress proxy)
  4. feed     - nova_feed.json fresh (<180 s) and connected:true, i.e. the
                NovaSignals EA is attached and publishing
  5. daemons  - trade_executor.py, signal_bridge.py, tg_subscribers.py

Healing rule: ONLY start what is missing. Nothing running is ever killed,
restarted, or reconfigured -- in particular the live trade_executor.py is
never touched while it is alive.

Exit codes: 0 = healthy or healed, 2 = hard failure (needs human attention).
Logs to bridge/run/supervise_stack.log.

Usage:
  python3 supervise_stack.py            # heal pass, exit 0/2
  python3 supervise_stack.py --health   # print concise health summary, exit 0

Concurrency: a non-blocking flock on /tmp/supervise_stack.lock makes
overlapping runs a no-op, so a second healer can never launch a duplicate
terminal or daemon.
"""

import codecs
import fcntl
import glob
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
MT5 = os.path.join(HOME, "workspace", "mt5")
BRIDGE = os.path.join(MT5, "bridge")
RUN_DIR = os.path.join(BRIDGE, "run")
LOG_PATH = os.path.join(RUN_DIR, "supervise_stack.log")
LAUNCHER = os.path.join(MT5, "scripts", "run-mt5-tunnel.sh")
LOCK_PATH = "/tmp/supervise_stack.lock"

os.makedirs(RUN_DIR, exist_ok=True)
sys.path.insert(0, BRIDGE)
import watch_mt5  # noqa: E402  (reuses ensure_wine, terminal/feed checks, relaunch)

DAEMONS = ("trade_executor.py", "signal_bridge.py", "tg_subscribers.py")
EA_NAMES = ("NovaSignals", "NovaTrader")


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- processes
# /proc-based liveness: robust against the classic pgrep -f traps (admin
# shells that merely mention a script name, or daemons launched with a
# relative vs absolute path). A process counts as a daemon only when its
# argv[0] is a python interpreter and a later argv element names the script
# file. Our own PID and zombies are excluded.

def _cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().split(b"\0")
    except OSError:
        return []


def _is_zombie(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().split()[2] == "Z"
    except OSError:
        return True


def _iter_pids():
    me = os.getpid()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me or _is_zombie(pid):
            continue
        yield pid


def daemon_pids(script):
    """PIDs running `script` as a python program, in any cmdline form."""
    hits = []
    for pid in _iter_pids():
        argv = [a.decode(errors="replace") for a in _cmdline(pid)]
        if len(argv) < 2:
            continue
        prog = os.path.basename(argv[0])
        if prog not in ("python3", "python", "python3.12", "python3.11",
                        "python3.10"):
            continue
        if any(os.path.basename(a) == script for a in argv[1:]):
            hits.append(pid)
    return hits


def xvfb_pids():
    hits = []
    for pid in _iter_pids():
        argv = [a.decode(errors="replace") for a in _cmdline(pid)]
        if argv and os.path.basename(argv[0]) == "Xvfb" and ":99" in argv:
            hits.append(pid)
    return hits


def terminal_pids():
    hits = []
    for pid in _iter_pids():
        argv = [a.decode(errors="replace") for a in _cmdline(pid)]
        if any(a.lower().endswith("terminal64.exe") for a in argv):
            hits.append(pid)
    return hits


# ---------------------------------------------------------------- layers

def ensure_xvfb():
    if xvfb_pids():
        return True
    log("Xvfb :99 missing -> starting")
    try:
        with open("/tmp/xvfb99.log", "a") as lf:
            subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x800x24"],
                             stdout=lf, stderr=subprocess.STDOUT,
                             start_new_session=True)
        time.sleep(2)
        ok = bool(xvfb_pids())
        log("Xvfb start %s" % ("OK" if ok else "FAILED"))
        return ok
    except Exception as e:  # noqa: BLE001 - best effort
        log("Xvfb start exception: %s" % e)
        return False


def heal_terminal():
    """Ensure the MT5 terminal is alive; relaunch if not. Returns bool."""
    if terminal_pids():
        return True
    log("terminal missing -> ensuring wine, then relaunching")
    if not watch_mt5.ensure_wine():
        log("HARD FAILURE: wine reinstall failed")
        return False
    if not ensure_xvfb():
        log("HARD FAILURE: Xvfb would not start")
        return False
    if not watch_mt5.relaunch():
        log("HARD FAILURE: launcher failed")
        return False
    time.sleep(60)  # terminal start + broker authorization
    if not terminal_pids():
        log("HARD FAILURE: terminal not alive after relaunch")
        return False
    log("terminal relaunched and alive")
    return True


def heal_feed():
    """Ensure the quote feed is fresh (EA attached). Best-effort re-attach.

    Returns True when the feed is fresh afterwards, False on hard failure.
    """
    fresh, detail = watch_mt5.feed_fresh()
    if fresh:
        return True
    log("feed stale (%s); waiting 45 s for EA startup" % detail)
    time.sleep(45)
    fresh, detail = watch_mt5.feed_fresh()
    if fresh:
        log("feed recovered on its own: %s" % detail)
        return True
    log("feed still stale (%s); attempting GUI EA re-attach" % detail)
    watch_mt5.gui_reattach()
    time.sleep(30)
    fresh, detail = watch_mt5.feed_fresh()
    log("post-reattach feed: %s (%s)" % (fresh, detail))
    return fresh


def heal_daemons():
    """Start any missing daemon. Never touches a running one."""
    started = []
    for script in DAEMONS:
        pids = daemon_pids(script)
        if pids:
            continue
        log("%s not running -> starting" % script)
        try:
            log_path = os.path.join(RUN_DIR, script.replace(".py", ".log"))
            with open(log_path, "a") as lf:
                subprocess.Popen(
                    [sys.executable, os.path.join(BRIDGE, script)],
                    stdout=lf, stderr=subprocess.STDOUT,
                    start_new_session=True)
            started.append(script)
        except Exception as e:  # noqa: BLE001 - best effort
            log("FAILED to start %s: %s" % (script, e))
    # verify the ones we started actually showed up
    time.sleep(3)
    failed = [s for s in started if not daemon_pids(s)]
    for s in failed:
        log("HARD: %s did not stay up after start" % s)
    if started:
        log("started daemons: %s" % ", ".join(started))
    return not failed


# ---------------------------------------------------------------- health report

def wine_version():
    for cand in ("/usr/lib/wine/wine64", "/usr/local/bin/wine64"):
        try:
            r = subprocess.run([cand, "--version"], capture_output=True,
                               text=True, timeout=30)
            if r.returncode == 0 and "wine-" in r.stdout.lower():
                return r.stdout.strip()
        except OSError:
            continue
    return "MISSING"


def newest_terminal_log():
    cands = sorted(
        glob.glob(os.path.join(
            HOME, "workspace", "mt5", "prefix", "drive_c",
            "Program Files", "MetaTrader 5", "logs", "20*.log")))
    return cands[-1] if cands else None


def ea_evidence():
    """Last 'loaded successfully' line per EA from the newest terminal log."""
    out = {}
    path = newest_terminal_log()
    if not path:
        return out
    try:
        with codecs.open(path, encoding="utf-16le",
                         errors="replace") as f:
            lines = f.read().splitlines()
        for ea in EA_NAMES:
            hits = [l.strip() for l in lines
                    if ea in l and "loaded successfully" in l]
            if hits:
                out[ea] = hits[-1][-120:]
    except OSError:
        pass
    return out


def feed_status():
    fresh, detail = watch_mt5.feed_fresh()
    return fresh, detail


def health_summary():
    lines = []
    lines.append("wine: %s" % wine_version())
    xp = xvfb_pids()
    lines.append("xvfb :99: %s" % ("pid %s" % xp[0] if xp else "DOWN"))
    tp = terminal_pids()
    lines.append("terminal64: %s" % ("pid %s" % tp[0] if tp else "DOWN"))
    ev = ea_evidence()
    for ea in EA_NAMES:
        lines.append("ea %s: %s" % (ea, "attached" if ea in ev else "NO EVIDENCE"))
    fresh, detail = feed_status()
    lines.append("feed: %s (%s)" % ("fresh" if fresh else "STALE", detail))
    for d in DAEMONS:
        pids = daemon_pids(d)
        lines.append("daemon %s: %s" % (
            d, "pid %s" % ",".join(map(str, pids)) if pids else "DOWN"))
    return lines


# ---------------------------------------------------------------- main

def main(argv):
    if "--health" in argv:
        for line in health_summary():
            print(line)
        return 0

    # single-flight: a second overlapping healer exits quietly
    try:
        lock = open(LOCK_PATH, "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another supervise_stack instance is running; exiting")
        return 0

    actions = []

    # layer 1: wine
    if not watch_mt5.ensure_wine():
        log("HARD FAILURE: wine unavailable and reinstall failed")
        return 2
    # layer 2: Xvfb
    if not ensure_xvfb():
        log("HARD FAILURE: Xvfb unavailable")
        return 2
    # layer 3: terminal
    if not terminal_pids():
        actions.append("terminal relaunched")
    if not heal_terminal():
        return 2
    # layer 4: feed (EA publishing)
    if not heal_feed():
        log("HARD FAILURE: feed still stale after re-attach attempt")
        return 2
    if actions:
        pass  # healed-terminal path already logged
    # layer 5: daemons (start-only, never kill)
    before = {d: daemon_pids(d) for d in DAEMONS}
    if not heal_daemons():
        log("HARD FAILURE: a daemon would not stay up")
        return 2
    for d in DAEMONS:
        if not before[d] and daemon_pids(d):
            actions.append("%s started" % d)

    if actions:
        log("healed: %s" % "; ".join(actions))
    else:
        fresh, detail = watch_mt5.feed_fresh()
        log("healthy: terminal up; %s; all daemons alive" % detail)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
