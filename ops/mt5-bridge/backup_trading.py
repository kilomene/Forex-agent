#!/usr/bin/env python3
"""
Trading-data backup + close-event reconciliation (runs from cron every 15 min).

1. Snapshots every trading record to ~/workspace/mt5/backups/<UTC ts>/:
   journal, trades file, positions, signals, specs, shadow state, configs,
   cursors, and the SQLite learning DB (trade_learning.db). The DB is copied
   through SQLite's online backup API so the snapshot is transactionally
   consistent even while another process holds it open for writing. Keeps the
   newest 96 snapshots (~24 h); every snapshot carries manifest.json with
   SHA-256 digests.

   The learning DB is LOCAL-ONLY: it is never staged for git. See
   refuse_git_paths(), and the *.db rule in the Forex-agent repo .gitignore
   (verified with `git check-ignore`).

2. Reconciles trade.closed events from nova_trades.jsonl into the journal.
   A close is backfilled ONLY with verified broker linkage:

       DEAL_ENTRY == DEAL_ENTRY_OUT
       AND DEAL_POSITION_ID == the position ticket
       AND DEAL_MAGIC == InMagic (20260921)

   The journal ticket is ALWAYS the position id. A close deal id or close
   order id is NEVER accepted as the ticket. Closes that cannot be linked
   (missing position id, ticket/position mismatch, unknown position,
   untrusted reason, wrong magic, entry not OUT) are skipped loudly --
   never backfilled, never estimated.

   P&L honesty: a backfilled close says confirmation_status="broker-confirmed"
   only when the broker's net P&L components (gross profit + commission +
   swap, or an explicit net figure) are present. Otherwise the record says
   "provisional" with profit=null and profit_unknown=true. Every backfilled
   close carries its full audit trail: position id, deal id, order id, broker
   close time, reason, linkage evidence, and source.

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
DATA_DIR = os.path.join(HOME, "workspace", "mt5", "data")
BACKUP_ROOT = os.path.join(HOME, "workspace", "mt5", "backups")
JOURNAL = os.path.join(RUN_DIR, "nova_journal.jsonl")
TRADES = os.path.join(FILES_DIR, "nova_trades.jsonl")
LEARNING_DB = os.path.join(DATA_DIR, "trade_learning.db")
KEEP = 96
LOG_PATH = os.path.join(RUN_DIR, "backup_trading.log")

# Magic number NovaTrader stamps on every order it owns (NovaTrader.mq5
# InMagic). A close deal is linked to one of OUR positions only when the
# close deal carries this magic.
IN_MAGIC = 20260921

# Accepted encodings of DEAL_ENTRY_OUT in trade.closed source events.
DEAL_ENTRY_OUT_MARKERS = {"OUT", "DEAL_ENTRY_OUT", 1, "1"}

# Reasons the NovaTrader EA emits trade.closed with -- and ONLY from code
# paths that already verified the DEAL_ENTRY_OUT / DEAL_POSITION_ID /
# DEAL_MAGIC triple against broker deal history (ReconcilePositions) or that
# closed one of our own tracked tickets via our own OrderSend (command path).
# Anything else (terminal-log estimates, DB-derived guesses, manual notes)
# needs real broker-history confirmation and is NOT backfilled here.
TRUSTED_CLOSE_REASONS = {"broker", "command"}

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
    (DATA_DIR, "trade_learning.db"),
]

# --- git guard ------------------------------------------------------------
# Suffixes that must NEVER appear in any git-bound file list. Defense in
# depth on top of .gitignore (the Forex-agent repo's .gitignore carries
# *.db; verified with `git check-ignore`). Every script that builds a file
# list for a git push/upload must pass it through refuse_git_paths() first.
GIT_FORBIDDEN_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db-journal",
                          ".db-wal", ".db-shm", ".so", ".env")


def refuse_git_paths(paths, where="git-bound file list"):
    """Raise ValueError if any path is a state/secret/binary artifact that
    must never be committed. Pass every git-bound file list through this."""
    bad = [str(p) for p in paths
           if str(p).lower().endswith(GIT_FORBIDDEN_SUFFIXES)]
    if bad:
        raise ValueError(
            "refusing to stage %d forbidden path(s) in %s: %s"
            % (len(bad), where, bad[:8]))
    return True


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


def snapshot_file(src, dest):
    """Copy src -> dest, returning the method used.

    SQLite databases go through the online backup API so the snapshot is
    transactionally consistent even while another process holds the DB open
    for writing. Falls back to a plain copy if the file is not a readable
    SQLite database.
    """
    if src.lower().endswith((".db", ".sqlite", ".sqlite3")):
        try:
            import sqlite3
            src_con = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
            try:
                dest_con = sqlite3.connect(dest)
                try:
                    src_con.backup(dest_con)
                finally:
                    dest_con.close()
            finally:
                src_con.close()
            return "sqlite-backup"
        except Exception as e:  # not a readable sqlite db; fall through
            log("sqlite backup failed for %s (%r); using plain copy"
                % (src, e))
    shutil.copy2(src, dest)
    return "copy2"


def do_backup(backup_root=BACKUP_ROOT, snapshot_files=None):
    """Snapshot the trading record set; prune to the newest KEEP snapshots."""
    if snapshot_files is None:
        snapshot_files = SNAPSHOT_FILES
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_root, ts)
    os.makedirs(dest, exist_ok=True)
    manifest = {"taken_utc": ts, "files": {}, "missing": []}
    for src_dir, name in snapshot_files:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            manifest["missing"].append(name)
            continue
        snap = os.path.join(dest, name)
        try:
            method = snapshot_file(src, snap)
            st = os.stat(snap)
            manifest["files"][name] = {
                "bytes": st.st_size,
                "sha256": sha256(snap),
                "mtime_utc": datetime.fromtimestamp(
                    st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "method": method,
            }
        except OSError as e:
            manifest["missing"].append("%s (%s)" % (name, e))
    with open(os.path.join(dest, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    # prune old snapshots
    snaps = sorted(d for d in os.listdir(backup_root)
                   if os.path.isdir(os.path.join(backup_root, d)))
    for old in snaps[:-KEEP]:
        shutil.rmtree(os.path.join(backup_root, old), ignore_errors=True)
    log("backup %s: %d files, %d missing, kept %d snapshots" %
        (ts, len(manifest["files"]), len(manifest["missing"]),
         min(len(snaps), KEEP)))
    return dest


def _norm_ticket(t):
    return None if t is None else str(t)


def journal_position_sets(journal_path=JOURNAL):
    """Return (closed, opened, opened_info).

    closed/opened are sets of normalized position tickets seen as
    trade.closed / trade.opened in the journal; opened_info maps a ticket to
    its trade.opened record for audit context on backfilled closes.
    """
    closed, opened, info = set(), set(), {}
    try:
        with open(journal_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                key = _norm_ticket(ev.get("ticket"))
                if key is None:
                    continue
                if ev.get("type") == "trade.closed":
                    closed.add(key)
                elif ev.get("type") == "trade.opened":
                    opened.add(key)
                    info[key] = ev
    except OSError:
        pass
    return closed, opened, info


def check_linkage(ev):
    """Validate the broker close linkage.

    Returns (strength, detail):
      "explicit" -- the event carries position_id + deal_entry OUT +
                    magic == IN_MAGIC: the full broker triple, verified.
      "ea"       -- legacy EA shape (no linkage fields) but with a reason the
                    EA only emits from its triple-checked close paths
                    (ReconcilePositions / command close). The caller must
                    additionally require the ticket to be a known opened
                    position.
      None       -- unlinked: must never be backfilled.

    The journal ticket is ALWAYS the position id; a close deal id or close
    order id is never accepted as the ticket.
    """
    position_id = ev.get("position_id")
    deal_entry = ev.get("deal_entry")
    magic = ev.get("magic")
    if position_id is not None or deal_entry is not None or magic is not None:
        # Explicit format: require the FULL triple, no partial credit.
        if position_id is None:
            return None, ("position_id missing -- refusing to derive the "
                          "ticket from deal/order id")
        ticket = ev.get("ticket")
        if ticket is not None and _norm_ticket(ticket) != _norm_ticket(position_id):
            return None, "ticket != position_id"
        if deal_entry not in DEAL_ENTRY_OUT_MARKERS:
            return None, "deal_entry not OUT (%r)" % (deal_entry,)
        try:
            magic_ok = int(magic) == IN_MAGIC
        except (TypeError, ValueError):
            magic_ok = False
        if not magic_ok:
            return None, "magic != InMagic (%r)" % (magic,)
        return "explicit", "DEAL_ENTRY_OUT + position_id + magic verified"
    if ev.get("reason") in TRUSTED_CLOSE_REASONS:
        return "ea", "legacy EA close (reason=%s)" % ev.get("reason")
    return None, "untrusted reason %r" % (ev.get("reason"),)


def confirmation_of(ev):
    """Return (status, profit).

    "broker-confirmed" only when the broker's net P&L components are present
    (explicit net_profit, or gross_profit + commission + swap). Otherwise
    ("provisional", None): P&L unknown, never estimated.
    """
    gross = ev.get("gross_profit")
    commission = ev.get("commission")
    swap = ev.get("swap")
    net = ev.get("net_profit")
    if net is None and None not in (gross, commission, swap):
        try:
            net = float(gross) + float(commission) + float(swap)
        except (TypeError, ValueError):
            net = None
    if net is not None:
        return "broker-confirmed", float(net)
    return "provisional", None


def build_backfill_record(ev, opened_info, utc_now):
    """Build the journal record for a verified close, with full audit trail."""
    strength, _detail = check_linkage(ev)  # already validated by caller
    status, profit = confirmation_of(ev)
    position_id = ev.get("position_id", ev.get("ticket"))
    info = opened_info.get(_norm_ticket(position_id), {})
    return {
        "type": "trade.closed",
        "ticket": position_id,
        "position_id": position_id,
        "deal_id": ev.get("deal_id"),
        "order_id": ev.get("order_id"),
        "symbol": ev.get("symbol", info.get("symbol")),
        "direction": ev.get("direction", info.get("direction")),
        "volume": ev.get("volume", info.get("volume")),
        "entry_price": info.get("entry_price"),
        "exit_price": ev.get("exit_price"),
        "profit": profit,
        "profit_unknown": profit is None,
        # The source's own profit figure, kept for audit only -- it is NOT
        # a claim unless confirmation_status is broker-confirmed.
        "profit_reported_by_source": ev.get("profit"),
        "gross_profit": ev.get("gross_profit"),
        "commission": ev.get("commission"),
        "swap": ev.get("swap"),
        "broker_close_time": ev.get("time"),
        "reason": ev.get("reason"),
        "confirmation_status": status,
        "linkage": {
            "strength": strength,
            "deal_entry": ev.get("deal_entry"),
            "magic": ev.get("magic"),
        },
        "source": "nova_trades.jsonl",
        "reconciled": True,
        "reconciled_by": "backup_trading",
        "reconciled_at_utc": utc_now,
    }


def reconcile_closes(journal_path=JOURNAL, trades_path=TRADES):
    """Backfill missing journal trade.closed events from nova_trades.jsonl.

    Strict and fail-closed: only closes with verified broker linkage for
    known opened positions are backfilled. Everything else is skipped with a
    loud log line -- never fabricated, never estimated. Idempotent:
    re-running adds nothing. Returns (added, skipped).
    """
    if not os.path.isfile(trades_path):
        log("reconcile: nova_trades.jsonl not found")
        return 0, 0
    closed, opened, opened_info = journal_position_sets(journal_path)
    try:
        with open(trades_path, encoding="utf-8", errors="replace") as f:
            trades = [json.loads(l) for l in f if l.strip()]
    except OSError as e:
        log("reconcile: cannot read trades file: %s" % e)
        return 0, 0
    added = skipped = 0
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(journal_path, "a", encoding="utf-8") as jf:
        for ev in trades:
            if not isinstance(ev, dict) or ev.get("type") != "trade.closed":
                continue
            position_id = ev.get("position_id", ev.get("ticket"))
            key = _norm_ticket(position_id)
            if key is None:
                skipped += 1
                log("reconcile SKIP: close with no position id "
                    "(deal_id=%s order_id=%s) -- refusing to guess" %
                    (ev.get("deal_id"), ev.get("order_id")))
                continue
            if key in closed:
                continue  # already journaled: idempotent
            strength, detail = check_linkage(ev)
            if not strength:
                skipped += 1
                log("reconcile SKIP: unlinked close position=%s: %s"
                    % (position_id, detail))
                continue
            if key not in opened:
                skipped += 1
                log("reconcile SKIP: close for unknown position %s "
                    "(never opened in journal)" % (position_id,))
                continue
            rec = build_backfill_record(ev, opened_info, utc_now)
            jf.write(json.dumps(rec) + "\n")
            closed.add(key)
            added += 1
            log("reconciled close: position %s deal %s order %s status=%s "
                "profit=%s (%s)" % (
                    position_id, ev.get("deal_id"), ev.get("order_id"),
                    rec["confirmation_status"], rec["profit"], detail))
    log("reconcile: %d backfilled, %d skipped (unlinked/unverifiable)"
        % (added, skipped))
    return added, skipped


def main():
    t0 = time.time()
    try:
        do_backup()
    except Exception as e:
        log("backup FAILED: %r" % e)
    try:
        reconcile_closes()
    except Exception as e:
        log("reconcile FAILED: %r" % e)
    log("done in %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
