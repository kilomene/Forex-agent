#!/usr/bin/env python3
"""Shared helpers for the continuous-evolution install pipeline.

STDLIB ONLY -- this module (and every module in pipeline/) must never
import trade_executor or any other bridge module at top level. The
pipeline has to keep working when the installed version is broken, and
the candidate version under test must be evaluated in an isolated
subprocess, never in this process's module cache.

Conventions
-----------
* BRIDGE_ROOT: the live bridge directory. Defaults to the parent of the
  pipeline/ package; override with --bridge-root or EVOLUTION_BRIDGE_ROOT.
* STATE_DIR: the trader state dir (run/). Defaults to <bridge>/run;
  override with TRADER_STATE_DIR (honored by trade_executor too) or
  --state-dir.
* Versions live in <bridge>/versions/<version-id>/.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

AUTHOR = "evolution-loop"

# The two known Sunday market-closed environmental test failures.
# Documented in EVOLUTION_LOOP.md ("Known environmental failures").
KNOWN_ENV_FAILURES = frozenset({
    "tests/test_trader.py::test_daily_loss_limit_trips_kill_switch_live",
    "tests/test_trader.py::test_live_mode_writes_command_and_journals_commanded",
})

# Code files that belong to a version snapshot. State (run/), caches,
# databases, logs, proposals and versions are NOT part of a version --
# restoring them would clobber live state.
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


def bridge_root(override=None):
    if override:
        return os.path.abspath(override)
    env = os.environ.get("EVOLUTION_BRIDGE_ROOT")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(os.path.join(HERE, os.pardir))


def state_dir(bridge=None, override=None):
    if override:
        return os.path.abspath(override)
    env = os.environ.get("TRADER_STATE_DIR")
    if env:
        return os.path.abspath(env)
    return os.path.join(bridge_root(bridge), "run")


def versions_dir(bridge=None):
    return os.path.join(bridge_root(bridge), "versions")


def version_dir(version_id, bridge=None):
    return os.path.join(versions_dir(bridge), version_id)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_code_files(root):
    """Yield (relpath, abspath) for every file in a version snapshot."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SNAPSHOT_EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if fn in SNAPSHOT_EXCLUDE_FILES:
                continue
            if not fn.endswith(SNAPSHOT_SUFFIXES):
                continue
            ap = os.path.join(dirpath, fn)
            rp = os.path.relpath(ap, root)
            out.append((rp, ap))
    return out


def snapshot_tree(src_root, dest_dir):
    """Copy the version-controlled code tree src_root -> dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    count = 0
    for rel, ap in iter_code_files(src_root):
        dp = os.path.join(dest_dir, rel)
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        shutil.copy2(ap, dp)
        count += 1
    return count


def apply_files(files_dir, dest_root):
    """Overlay candidate changed files (relpath -> content) onto dest."""
    count = 0
    for rel, ap in iter_code_files(files_dir):
        dp = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        shutil.copy2(ap, dp)
        count += 1
    return count


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


def iter_jsonl(path, limit=None):
    n = 0
    try:
        f = open(path)
    except OSError:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue
            n += 1
            if limit and n >= limit:
                return


def append_evolution_log(bridge, entry):
    """Every pipeline run appends here: version, gates, decision."""
    entry = dict(entry)
    entry.setdefault("utc", utc_now_iso())
    append_jsonl(os.path.join(state_dir(bridge), "evolution.log"), entry)


def journal_event(bridge, event):
    """Append an operational event to the live trader journal."""
    journal_event_to(state_dir(bridge), event)


def journal_event_to(sdir, event):
    """Append an operational event to a specific state dir's journal."""
    event = dict(event)
    event.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    append_jsonl(os.path.join(sdir, "nova_journal.jsonl"), event)


def read_kill_switch(bridge):
    p = os.path.join(state_dir(bridge), "trading_enabled")
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return "0"


def read_risk_config(bridge):
    return read_json(os.path.join(state_dir(bridge), "risk_config.json"), {})


def blocked_symbols_fingerprint(bridge):
    cfg = read_risk_config(bridge)
    syms = sorted(str(s).upper() for s in (cfg.get("blocked_symbols") or []))
    return hashlib.sha256(json.dumps(syms).encode()).hexdigest()


def daemon_pid(script_name="trade_executor.py", script_path=None):
    """Return the PID of the daemon whose argv[1] IS the script file.

    Name-mention matching (pgrep -f) is not enough: any shell with the
    name in its command line would match. We require argv[0] to be a
    python interpreter and argv[1] to be exactly the script path (or end
    with /<script_name> when no explicit path is given).
    """
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
        parts = raw.split(b"\x00")
        parts = [p.decode(errors="replace") for p in parts if p]
        if len(parts) < 2:
            continue
        interp = os.path.basename(parts[0])
        if interp not in ("python3", "python", "python3.11", "python3.12",
                          "python3.10", "python3.9"):
            continue
        target = parts[1]
        if script_path:
            if os.path.abspath(target) == os.path.abspath(script_path):
                return int(pid)
        elif target.endswith("/" + script_name) or target == script_name:
            if "pgrep" in target or "relaunch" in target:
                continue
            return int(pid)
    return None


def run(cmd, cwd=None, timeout=600, env=None):
    """Run a subprocess; return (returncode, stdout, stderr)."""
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, env=e)
    return p.returncode, p.stdout, p.stderr


def log_line(msg):
    line = f"{utc_now_iso()} {msg}"
    print(line, flush=True)
    return line


class PipelineError(Exception):
    """A gate rejected the version (expected, journaled)."""


class PipelineHalt(Exception):
    """A hard infrastructure failure (not a gate verdict)."""
