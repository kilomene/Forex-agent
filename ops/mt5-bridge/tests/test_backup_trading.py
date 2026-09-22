"""Hermetic unit tests for backup_trading.py + state persistence regressions.

tmp_path only: no MT5, no network, no cron. Covers:
  - reconcile_closes(): strict broker linkage (DEAL_ENTRY_OUT /
    DEAL_POSITION_ID / DEAL_MAGIC), never using a deal/order id as the
    position ticket, P&L honesty (unknown unless components present),
    idempotency, and loud fail-closed skips.
  - do_backup(): the learning DB is snapshotted (via the SQLite online
    backup API) with a SHA-256 manifest entry.
  - refuse_git_paths(): .db/.sqlite/.so/.env never reach a git-bound list.
  - save->reload persistence: dedup keys and state survive JSON round-trips,
    including legacy list-form keys (the trades_seen crash-loop class).
"""
import json
import os
import sqlite3
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import backup_trading as bt  # noqa: E402
import trade_executor as te  # noqa: E402


# ---------------------------------------------------------------- fixtures

def write_journal(path, events):
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def write_trades(path, events):
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def opened(ticket, **kw):
    ev = {"type": "trade.opened", "ticket": ticket, "symbol": "XAGUSD",
          "direction": "SELL", "volume": 65.45, "entry_price": 66.015,
          "time": "2026.09.22 06:45:03"}
    ev.update(kw)
    return ev


def ea_close(ticket, **kw):
    """Legacy EA close shape: ticket + reason, no linkage fields."""
    ev = {"type": "trade.closed", "ticket": ticket, "exit_price": 65.378,
          "profit": 20845.83, "reason": "broker", "time": "2026.09.22 08:41:28",
          "symbol": "XAGUSD", "direction": "SELL", "volume": 65.45}
    ev.update(kw)
    return ev


def linked_close(position_id, **kw):
    """New explicit format with the full broker linkage triple."""
    ev = {"type": "trade.closed", "ticket": position_id,
          "position_id": position_id, "deal_id": 10338637554,
          "order_id": 10609986090, "deal_entry": "OUT",
          "magic": bt.IN_MAGIC, "exit_price": 65.378,
          "reason": "broker", "time": "2026.09.22 08:41:28",
          "symbol": "XAGUSD", "direction": "SELL", "volume": 65.45,
          "gross_profit": 20850.0, "commission": -4.17, "swap": 0.0}
    ev.update(kw)
    return ev


def journal_closed_tickets(path):
    out = []
    with open(path) as f:
        for line in f:
            ev = json.loads(line)
            if ev.get("type") == "trade.closed":
                out.append(ev)
    return out


# ------------------------------------------------- reconcile: happy paths

def test_reconcile_backfills_explicit_broker_confirmed_close(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    write_trades(str(t), [linked_close(10608752852)])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert (added, skipped) == (1, 0)
    recs = journal_closed_tickets(str(j))
    assert len(recs) == 1
    rec = recs[0]
    assert rec["ticket"] == 10608752852
    assert rec["position_id"] == 10608752852
    assert rec["deal_id"] == 10338637554
    assert rec["order_id"] == 10609986090
    assert rec["confirmation_status"] == "broker-confirmed"
    assert rec["profit"] == pytest.approx(20850.0 - 4.17 + 0.0)
    assert rec["profit_unknown"] is False
    assert rec["reconciled"] is True
    assert rec["linkage"]["strength"] == "explicit"


def test_reconcile_provisional_when_components_missing(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    ev = linked_close(10608752852)
    del ev["gross_profit"], ev["commission"], ev["swap"]
    ev["profit"] = 20845.83  # source's own figure: audit only, not a claim
    write_trades(str(t), [ev])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert (added, skipped) == (1, 0)
    rec = journal_closed_tickets(str(j))[0]
    assert rec["confirmation_status"] == "provisional"
    assert rec["profit"] is None
    assert rec["profit_unknown"] is True
    assert rec["profit_reported_by_source"] == 20845.83


def test_reconcile_legacy_ea_close_is_provisional_unknown(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    write_trades(str(t), [ea_close(10608752852)])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert (added, skipped) == (1, 0)
    rec = journal_closed_tickets(str(j))[0]
    assert rec["confirmation_status"] == "provisional"
    assert rec["profit"] is None
    assert rec["profit_unknown"] is True
    assert rec["profit_reported_by_source"] == 20845.83
    assert rec["linkage"]["strength"] == "ea"


def test_reconcile_accepts_net_profit_component(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    ev = linked_close(10608752852)
    del ev["gross_profit"], ev["commission"], ev["swap"]
    ev["net_profit"] = 10845.73
    write_trades(str(t), [ev])
    added, _ = bt.reconcile_closes(str(j), str(t))
    assert added == 1
    rec = journal_closed_tickets(str(j))[0]
    assert rec["confirmation_status"] == "broker-confirmed"
    assert rec["profit"] == pytest.approx(10845.73)


# ------------------------------------------------- reconcile: fail closed

def test_reconcile_refuses_deal_id_as_ticket(tmp_path):
    """The old bug class: a close keyed on a deal/order id must never be
    journaled as a position close."""
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    ev = ea_close(10338637554)  # close DEAL id used as the ticket
    ev["deal_id"] = 10338637554
    write_trades(str(t), [ev])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert added == 0 and skipped == 1
    assert journal_closed_tickets(str(j)) == []


def test_reconcile_refuses_missing_position_id(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    ev = linked_close(10608752852)
    del ev["position_id"]
    del ev["ticket"]
    write_trades(str(t), [ev])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert added == 0 and skipped == 1


def test_reconcile_refuses_ticket_position_id_mismatch(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    ev = linked_close(10608752852, ticket=10338637554)  # deal id as ticket
    write_trades(str(t), [ev])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert added == 0 and skipped == 1


def test_reconcile_refuses_wrong_magic(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    write_trades(str(t), [linked_close(10608752852, magic=999)])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert added == 0 and skipped == 1


def test_reconcile_refuses_deal_entry_not_out(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    write_trades(str(t), [linked_close(10608752852, deal_entry="IN")])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert added == 0 and skipped == 1


def test_reconcile_refuses_untrusted_reason(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    # terminal-log / DB-derived estimates need broker history, not backfill
    write_trades(str(t), [ea_close(10608752852, reason="broker-sl-reconciled")])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert added == 0 and skipped == 1


def test_reconcile_refuses_unknown_position(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    write_trades(str(t), [linked_close(99999999999)])
    added, skipped = bt.reconcile_closes(str(j), str(t))
    assert added == 0 and skipped == 1


def test_reconcile_idempotent(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852)])
    write_trades(str(t), [linked_close(10608752852)])
    assert bt.reconcile_closes(str(j), str(t)) == (1, 0)
    assert bt.reconcile_closes(str(j), str(t)) == (0, 0)
    assert len(journal_closed_tickets(str(j))) == 1


def test_reconcile_skips_already_journaled(tmp_path):
    j = tmp_path / "journal.jsonl"
    t = tmp_path / "trades.jsonl"
    write_journal(str(j), [opened(10608752852),
                           dict(ea_close(10608752852), **{"reconciled": False})])
    write_trades(str(t), [linked_close(10608752852)])
    assert bt.reconcile_closes(str(j), str(t)) == (0, 0)
    assert len(journal_closed_tickets(str(j))) == 1


def test_reconcile_missing_trades_file(tmp_path):
    j = tmp_path / "journal.jsonl"
    write_journal(str(j), [opened(10608752852)])
    assert bt.reconcile_closes(str(j), str(tmp_path / "nope.jsonl")) == (0, 0)


# ------------------------------------------------- backup: learning DB

def _make_db(path, rows=(("sig1", "ok"),)):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE lessons (id INTEGER PRIMARY KEY, name TEXT, v TEXT)")
    con.executemany("INSERT INTO lessons (name, v) VALUES (?, ?)", rows)
    con.commit()
    con.close()


def test_backup_includes_learning_db(tmp_path):
    run_d = tmp_path / "run"
    data_d = tmp_path / "data"
    run_d.mkdir()
    data_d.mkdir()
    (run_d / "a.json").write_text("{}\n")
    _make_db(str(data_d / "trade_learning.db"))
    dest = bt.do_backup(
        backup_root=str(tmp_path / "backups"),
        snapshot_files=[(str(run_d), "a.json"),
                        (str(data_d), "trade_learning.db"),
                        (str(run_d), "missing.json")])
    snap_db = os.path.join(dest, "trade_learning.db")
    assert os.path.isfile(snap_db)
    # snapshot is a valid, complete SQLite DB with the same rows
    con = sqlite3.connect(snap_db)
    assert con.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1
    con.close()
    with open(os.path.join(dest, "manifest.json")) as f:
        manifest = json.load(f)
    entry = manifest["files"]["trade_learning.db"]
    assert entry["method"] == "sqlite-backup"
    assert len(entry["sha256"]) == 64
    assert entry["bytes"] > 0
    assert "missing.json" in manifest["missing"]


def test_backup_db_consistent_under_write(tmp_path):
    """Snapshot via the online backup API while a writer holds the DB."""
    data_d = tmp_path / "data"
    data_d.mkdir()
    db_path = str(data_d / "trade_learning.db")
    _make_db(db_path)
    writer = sqlite3.connect(db_path)
    writer.execute("INSERT INTO lessons (name, v) VALUES ('w', '1')")
    # NOTE: uncommitted on purpose -- the snapshot must still be readable.
    try:
        dest = bt.do_backup(
            backup_root=str(tmp_path / "backups"),
            snapshot_files=[(str(data_d), "trade_learning.db")])
        snap_db = os.path.join(dest, "trade_learning.db")
        con = sqlite3.connect(snap_db)
        con.execute("SELECT COUNT(*) FROM lessons").fetchone()
        con.close()
    finally:
        writer.close()


# ------------------------------------------------- git guard

@pytest.mark.parametrize("bad", [
    "trade_learning.db",
    "data/trade_learning.db",
    "/home/hatch/workspace/mt5/data/trade_learning.db",
    "x.sqlite", "x.sqlite3", "lib/native.so", "config.env",
])
def test_refuse_git_paths_rejects_state_secrets_binaries(bad):
    with pytest.raises(ValueError):
        bt.refuse_git_paths(["trade_executor.py", bad], where="test")


def test_refuse_git_paths_allows_code_and_docs():
    assert bt.refuse_git_paths(
        ["trade_executor.py", "tests/test_trader.py", "README.md",
         "trades/index.md", "chart.png"], where="test") is True


# ------------------------------------------------- persistence regressions

def test_normalize_seen_keys_strings_survive():
    keys = ["trade.closed||10608752852|", "trade.opened|cmd-1|10608752853|"]
    assert te.normalize_seen_keys(keys) == set(keys)


def test_normalize_seen_keys_migrates_legacy_lists():
    raw = [["trade.closed", None, 10608752852, None],
           ["trade.opened", "cmd-1", 10608752853, None]]
    seen = te.normalize_seen_keys(raw)  # must not raise "unhashable type"
    assert seen == {"trade.closed||10608752852|",
                    "trade.opened|cmd-1|10608752853|"}


def test_normalize_seen_keys_migrates_tuples():
    seen = te.normalize_seen_keys([("trade.closed", None, 1, None)])
    assert seen == {"trade.closed||1|"}


def test_state_save_reload_cycle_twice_with_legacy_keys(tmp_path):
    """The exact crash-loop class: tuple keys -> JSON lists -> reload."""
    p = str(tmp_path / "state.json")
    legacy = [["trade.closed", None, 10608752852, None],
              "trade.opened|cmd-1|10608752853|"]
    te.save_state(p, {"offset": 10, "seen": [], "trades_offset": 5,
                      "trades_seen": legacy})
    once = te.load_state(p)
    seen = te.normalize_seen_keys(once["trades_seen"])  # no crash
    assert len(seen) == 2
    # write back the normalized form, reload again: stable, still no crash
    once["trades_seen"] = sorted(seen)
    te.save_state(p, once)
    twice = te.load_state(p)
    seen2 = te.normalize_seen_keys(twice["trades_seen"])
    assert seen2 == seen
    assert all(isinstance(k, str) for k in twice["trades_seen"])


def test_json_cannot_serialize_set_documents_why_lists():
    """State must persist dedup keys as JSON lists of strings, never sets:
    json.dump(set(...)) raises TypeError -- the original crash-loop class."""
    with pytest.raises(TypeError):
        json.dumps({"trades_seen": {("a", "b")}})
