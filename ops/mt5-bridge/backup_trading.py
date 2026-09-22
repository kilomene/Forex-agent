#!/usr/bin/env python3
"""
Trading-data backup + close-event reconciliation (runs from cron every 15 min).

1. Snapshots every trading record to ~/workspace/mt5/backups/<UTC ts>/:
   journal, trades file, positions, signals, specs, shadow state, configs,
   cursors. Keeps the newest 96 snapshots (~24 h), writes manifest.json.
2. Reconciles trade.closed events: any ticket closed in nova_trades.jsonl
   that has no trade.closed entry in nova_journal.jsonl gets appended to
   the journal with "reconciled": true, so no close ever goes untracked
   (e.g. closes that happened while the terminal was down after a reboot).

Append-only and idempotent. Never modifies existing journal lines.
"""
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
FILES_DIR = os.path.join(
    HOME, "workspace", "mt5", "prefix", "drive_c", "Program Files",
    "MetaTrader 5", "MQL5", "Files")
BRIDGE = os.path.join(HOME, "workspace", "mt5", "bridge")
RUN_DIR = os.path.join(BRIDGE, "run")
BACKUP_ROOT = os.path.join(HOME, "workspace", "mt5", "backups")
JOURNAL = os.path.join(RUN_DIR, "nova_journal.jsonl")
TRADES = os.path.join(FILES_DIR, "nova_trades.jsonl")
KEEP = 96
LOG_PATH = os.path.join(RUN_DIR, "backup_trading.log")

SNAPSHOT_FILES = [
    (RUN_DIR, "nova_journal.jsonl"),
    (RUN_DIR, "safety_audit.jsonl"),
    (RUN_DIR, "shadow_state.json"),
    (RUN_DIR, "risk_config.json"),
    (RUN_DIR, "trade_executor.state.json"),
    (RUN_DIR, "trade_notify.state.json"),
    (RUN_DIR, "chat_notify.state.json"),
    (FILES_DIR, "nova_trades.jsonl"),
    (FILES_DIR, "nova_positions.json"),
    (FILES_DIR, "nova_signals.jsonl"),
    (FILES_DIR, "nova_symbol_specs.json"),
    (FILES_DIR, "nova_symbols.txt"),
    (FILES_DIR, "nova_commands.jsonl"),
]


def log(msg):
    line = "%s %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                      msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def do_backup():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_ROOT, ts)
    os.makedirs(dest, exist_ok=True)
    manifest = {"taken_utc": ts, "files": {}, "missing": []}
    for src_dir, name in SNAPSHOT_FILES:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            manifest["missing"].append(name)
            continue
        try:
            shutil.copy2(src, os.path.join(dest, name))
            st = os.stat(src)
            manifest["files"][name] = {
                "bytes": st.st_size,
                "sha256": sha256(src),
                "mtime_utc": datetime.fromtimestamp(
                    st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            }
        except OSError as e:
            manifest["missing"].append("%s (%s)" % (name, e))
    with open(os.path.join(dest, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    # prune old snapshots
    snaps = sorted(d for d in os.listdir(BACKUP_ROOT)
                   if os.path.isdir(os.path.join(BACKUP_ROOT, d)))
    for old in snaps[:-KEEP]:
        shutil.rmtree(os.path.join(BACKUP_ROOT, old), ignore_errors=True)
    log("backup %s: %d files, %d missing, kept %d snapshots" %
        (ts, len(manifest["files"]), len(manifest["missing"]),
         min(len(snaps), KEEP)))
    return dest


def journal_closed_tickets():
    tickets = set()
    try:
        with open(JOURNAL, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "trade.closed" and ev.get("ticket"):
                    tickets.add(ev["ticket"])
    except OSError:
        pass
    return tickets


def reconcile_closes():
    """Backfill journal trade.closed events missing from nova_trades.jsonl."""
    if not os.path.isfile(TRADES):
        log("reconcile: nova_trades.jsonl not found")
        return 0
    have = journal_closed_tickets()
    added = 0
    try:
        with open(TRADES, encoding="utf-8", errors="replace") as f:
            trades = [json.loads(l) for l in f if l.strip()]
    except OSError:
        return 0
    with open(JOURNAL, "a", encoding="utf-8") as jf:
        for ev in trades:
            if not isinstance(ev, dict) or ev.get("type") != "trade.closed":
                continue
            ticket = ev.get("ticket")
            if not ticket or ticket in have:
                continue
            rec = dict(ev)
            rec["reconciled"] = True
            rec["reconciled_at_utc"] = datetime.now(
                timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            jf.write(json.dumps(rec) + "\n")
            have.add(ticket)
            added += 1
            log("reconciled missing close: ticket %s %s P&L %s" %
                (ticket, ev.get("symbol"), ev.get("profit")))
    if added == 0:
        log("reconcile: no missing closes")
    return added


def main():
    t0 = time.time()
    do_backup()
    reconcile_closes()
    log("done in %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
