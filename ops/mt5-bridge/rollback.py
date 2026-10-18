#!/usr/bin/env python3
"""rollback.py --previous | --version <id>

Restores a previous version snapshot, restarts the executor, verifies
health, and journals the rollback.

STDLIB ONLY. This script must work even when the installed bridge
version is completely broken, so it imports NOTHING from the bridge or
from pipeline/ -- every helper it needs is implemented inline.

Usage:
  python3 rollback.py --previous [--bridge-root DIR] [--state-dir DIR]
  python3 rollback.py --version v20261018... [--bridge-root DIR]
  python3 rollback.py --previous --no-restart   # restore files only
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

SNAPSHOT_SUFFIXES = (".py", ".mq5", ".md")
SNAPSHOT_EXCLUDE_DIRS = {
    "run", "__pycache__", ".pytest_cache", "evidence_out",
    "proposals", "versions", ".git",
}
SNAPSHOT_EXCLUDE_FILES = {
    "trade_learning.db", "trade_learning.db-shm", "trade_learning.db-wal",
}


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, sort_keys=True) + "\n")


def snapshot_files(snapshot_dir):
    out = []
    for dirpath, dirnames, filenames in os.walk(snapshot_dir):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SNAPSHOT_EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if fn in SNAPSHOT_EXCLUDE_FILES:
                continue
            if not fn.endswith(SNAPSHOT_SUFFIXES):
                continue
            ap = os.path.join(dirpath, fn)
            out.append((os.path.relpath(ap, snapshot_dir), ap))
    return out


def is_market_open():
    """Inline copy of the market-hours rule (no bridge imports)."""
    now = datetime.now(timezone.utc) + timedelta(hours=3)  # server GMT+3
    wd, hh = now.weekday(), now.hour + now.minute / 60.0
    if wd == 5:
        return False
    if wd == 6:
        return False
    if wd == 0 and hh < 1:
        return False
    return True


def daemon_pid(script_path):
    """PID whose argv[1] IS the script file (no name-mention matching)."""
    want = os.path.abspath(script_path)
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        parts = [p.decode(errors="replace")
                 for p in raw.split(b"\x00") if p]
        if len(parts) < 2:
            continue
        if os.path.basename(parts[0]) not in (
                "python3", "python", "python3.11", "python3.12",
                "python3.10", "python3.9"):
            continue
        if os.path.abspath(parts[1]) == want:
            return int(pid)
    return None


def main(argv):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--previous", action="store_true")
    g.add_argument("--version", default=None)
    ap.add_argument("--bridge-root", default=None)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--no-restart", action="store_true")
    args = ap.parse_args(argv)

    bridge = os.path.abspath(
        args.bridge_root or os.path.dirname(os.path.abspath(__file__)))
    sdir = os.path.abspath(
        args.state_dir or os.environ.get("TRADER_STATE_DIR")
        or os.path.join(bridge, "run"))

    inst_path = os.path.join(bridge, "versions", "installed.json")
    inst = read_json(inst_path, {}) or {}
    current = inst.get("current")
    if args.previous:
        target = inst.get("previous")
        if not target:
            print("no previous version recorded; nothing to roll back",
                  flush=True)
            return 2
        # The tree that was live before <current> installed is stored as
        # <current>'s previous_snapshot (taken at install time).
        snap_owner = current or target
    else:
        target = args.version
        # versions/<id>/previous_snapshot holds the tree live before
        # <id> itself installed.
        snap_owner = target
    snap = os.path.join(bridge, "versions", snap_owner, "previous_snapshot")
    if not os.path.isdir(snap):
        print(f"snapshot missing: {snap}", flush=True)
        return 2

    files = snapshot_files(snap)
    for rel, apath in files:
        dest = os.path.join(bridge, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(apath, dest)
    print(f"restored {len(files)} files from {target}", flush=True)

    # Remove files the rolled-back version ADDED (they exist in its
    # version manifest but not in the snapshot being restored). Without
    # this, a version that adds a broken test/module would leave it
    # behind, poisoning every later version's gate (a).
    removed = 0
    vjson = read_json(os.path.join(
        bridge, "versions", (current or target), "version.json"), {}) or {}
    snap_rels = {rel for rel, _ in files}
    for rel in vjson.get("files") or []:
        if rel not in snap_rels:
            p = os.path.join(bridge, rel)
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"removed {removed} file(s) added by the rolled-back "
              f"version", flush=True)

    if not args.no_restart:
        relaunch = os.environ.get(
            "EVOLUTION_RELAUNCH_SCRIPT",
            os.path.expanduser(
                "~/workspace/mt5/scripts/relaunch_executor.py"))
        print(f"restarting executor via {relaunch}", flush=True)
        env = dict(os.environ)
        env["TRADER_STATE_DIR"] = sdir
        p = subprocess.run([sys.executable, relaunch],
                           capture_output=True, text=True,
                           timeout=180, env=env)
        print(f"relaunch rc={p.returncode}", flush=True)
        time.sleep(5)

    # Verify: daemon alive via /proc cmdline; specs fresh (market-aware).
    daemon_script = os.environ.get(
        "EVOLUTION_DAEMON_SCRIPT",
        os.path.join(bridge, "trade_executor.py"))
    pid = daemon_pid(daemon_script)
    specs_ok = True
    specs_detail = "market closed; freshness check skipped"
    if is_market_open():
        specs = os.path.expanduser(
            "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/"
            "MQL5/Files/nova_symbol_specs.json")
        try:
            age = time.time() - os.path.getmtime(specs)
            specs_ok = age < 300
            specs_detail = f"specs age {age:.0f}s"
        except OSError:
            specs_ok = False
            specs_detail = "specs file missing"
    healthy = pid is not None and specs_ok
    print(f"daemon pid={pid} specs_ok={specs_ok} ({specs_detail})",
          flush=True)

    append_jsonl(os.path.join(sdir, "nova_journal.jsonl"), {
        "type": "pipeline.rollback",
        "target_version": target,
        "restored_files": len(files),
        "daemon_pid": pid,
        "specs_ok": specs_ok,
        "healthy": healthy,
        "actor": "evolution-loop",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    cur = inst.get("current")
    write_json(inst_path, {"current": target, "previous": cur,
                           "updated_utc": utc_now_iso()})
    append_jsonl(os.path.join(sdir, "evolution.log"), {
        "utc": utc_now_iso(),
        "event": "rollback",
        "target_version": target,
        "healthy": healthy,
    })
    print(f"rollback to {target}: {'HEALTHY' if healthy else 'UNHEALTHY'}",
          flush=True)
    return 0 if healthy else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
