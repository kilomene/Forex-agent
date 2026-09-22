"""Hermetic unit tests for trade_learning.py (tmp_path only, no MT5, no network)."""
import json
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_learning as tl  # noqa: E402


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def db(tmp_path):
    conn = tl.connect(str(tmp_path / "test.db"))
    tl.init_schema(conn)
    yield conn
    conn.close()


def _journal_lines():
    return [
        {"type": "signal.received", "signal_id": "S1", "symbol": "EURUSD",
         "timeframe": "M15", "direction": "BUY", "entry_price": 1.1,
         "stop_loss": 1.09, "take_profit": 1.12, "strategy": "ema_rsi",
         "trigger": "EMA20 crossed above EMA50, RSI=60.0",
         "rsi": 60.0, "atr": 0.001, "time": "2026-09-22 10:00:00"},
        {"type": "trade.decision", "signal_id": "S1", "symbol": "EURUSD",
         "direction": "BUY", "decision": "commanded", "volume": 2.0,
         "risk_amount": 2000.0, "command_id": "cmd-S1",
         "context": {"capital_basis": 200000, "spread_points": 5},
         "time": "2026-09-22 10:00:05"},
        {"type": "trade.decision", "signal_id": "S2", "symbol": "GBPUSD",
         "direction": "SELL", "decision": "skipped:spread_too_wide",
         "volume": None, "risk_amount": None,
         "context": {"spread_points": 99}, "time": "2026-09-22 10:05:00"},
        {"type": "command.sent",
         "command": {"type": "trade.open", "id": "cmd-S1", "symbol": "EURUSD",
                     "direction": "BUY", "volume": 2.0, "sl": 1.09, "tp": 1.12,
                     "signal_id": "S1"},
         "time": "2026-09-22 10:00:06"},
        {"type": "trade.update", "command_id": "cmd-S1", "ticket": 111,
         "deal": 9001, "fill_price": 1.1001, "signal_id": "S1",
         "time": "2026-09-22 10:00:10"},
        {"type": "trade.opened", "ticket": 111, "deal": 9001, "symbol": "EURUSD",
         "direction": "BUY", "volume": 2.0, "entry_price": 1.1001,
         "time": "2026.09.22 10:00:10", "signal_id": "S1",
         "command_id": "cmd-S1"},
        # ticket 222 opens but never closes anywhere and is absent from the
        # broker snapshot -> outcome must stay unknown, P&L never invented
        {"type": "trade.opened", "ticket": 222, "deal": 9002, "symbol": "USDJPY",
         "direction": "SELL", "volume": 1.0, "entry_price": 150.0,
         "time": "2026.09.22 11:00:00", "signal_id": "S9",
         "command_id": "cmd-S9"},
        {"type": "signal.outcome", "signal_id": "S1", "symbol": "EURUSD",
         "direction": "BUY", "entry": 1.1, "sl": 1.09, "tp": 1.12,
         "outcome": "TP", "hit_at": "2026-09-22 12:00:00", "snapshots": 3,
         "time": "2026-09-22 12:00:00"},
    ]


def _trades_lines():
    return [
        {"type": "trade.opened", "command_id": "cmd-S2", "ticket": 333,
         "deal": 9003, "fill_price": 1.27, "time": "2026.09.22 13:00:00",
         "signal_id": "S2", "symbol": "GBPUSD", "direction": "SELL",
         "volume": 1.5},
        # close present in broker history but missing from journal ->
        # reconciliation must mark it closed + broker_confirmed
        {"type": "trade.closed", "ticket": 333, "exit_price": 1.26,
         "profit": 100.0, "commission": -2.0, "swap": 0.0, "reason": "tp",
         "time": "2026.09.22 14:00:00", "symbol": "GBPUSD",
         "direction": "SELL", "volume": 1.5},
        {"type": "trade.rejected", "command_id": "cmd-S3",
         "reason": "send_failed:4756", "signal_id": "S3"},
        {"type": "trade.closed", "ticket": 111, "exit_price": 1.099,
         "profit": -50.0, "reason": "broker", "time": "2026.09.22 15:00:00",
         "symbol": "EURUSD", "direction": "BUY", "volume": 2.0},
    ]


def _signals_lines():
    return [
        {"id": "S1", "type": "signal.detected", "symbol": "EURUSD",
         "timeframe": "M15", "direction": "BUY", "entry_price": 1.1,
         "stop_loss": 1.09, "take_profit": 1.12, "ema_fast": 1.099,
         "ema_slow": 1.098, "rsi_value": 60.0, "atr_value": 0.001,
         "candle_time": "2026.09.22 09:45:00", "strategy": "ema_rsi",
         "trigger": "EMA20 crossed above EMA50, RSI=60.0",
         "server_time": "2026.09.22 10:00:00"},
    ]


@pytest.fixture
def sources(tmp_path):
    files_dir = tmp_path / "files"
    state_dir = tmp_path / "state"
    files_dir.mkdir()
    state_dir.mkdir()
    (state_dir / "nova_journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _journal_lines()) + "\n")
    (files_dir / "nova_trades.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _trades_lines()) + "\n")
    (files_dir / "nova_signals.jsonl").write_text(
        "\n".join(json.dumps(e) for e in _signals_lines()) + "\n")
    # broker snapshot: ticket 444 open only; ticket 222 absent -> unknown
    (files_dir / "nova_positions.json").write_text(json.dumps({
        "time": 1790066900, "server_time": "2026.09.22 16:00:00",
        "account": 99999999,  # must NEVER be persisted
        "positions": [{"ticket": 444, "symbol": "AUDUSD", "volume": 3.0,
                       "type": "BUY", "open_price": 0.65,
                       "current_price": 0.66, "profit": 300.0}],
    }))
    return {"files_dir": str(files_dir), "state_dir": str(state_dir)}


# ---------------------------------------------------------------- tests

def test_schema_creates_all_tables(db):
    tables = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ["signals", "decisions", "commands", "trades",
              "signal_outcomes", "lessons", "notes", "outages",
              "ingest_state", "meta"]:
        assert t in tables, t
    assert db.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "1"


def test_ingest_twice_identical_counts(db, sources):
    tl.ingest_all(db, files_dir=sources["files_dir"],
                  state_dir=sources["state_dir"])
    first = tl.table_counts(db)
    assert first["signals"] >= 2      # S1 + S2 (decision-only) ; S9 via open
    assert first["decisions"] == 2
    assert first["commands"] >= 2     # cmd-S1 filled, cmd-S3 rejected
    assert first["trades"] == 4      # 111, 222, 333, 444

    tl.ingest_all(db, files_dir=sources["files_dir"],
                  state_dir=sources["state_dir"])
    second = tl.table_counts(db)
    assert first == second, "re-ingest must not duplicate rows"


def test_reconcile_marks_broker_close(db, sources):
    tl.ingest_all(db, files_dir=sources["files_dir"],
                  state_dir=sources["state_dir"])
    t333 = db.execute("SELECT * FROM trades WHERE ticket=333").fetchone()
    assert t333["status"] == "closed"
    assert t333["broker_confirmed"] == 1
    assert t333["profit"] == 100.0
    assert t333["commission"] == -2.0
    # journal-missing close for 111 came from the broker file too
    t111 = db.execute("SELECT * FROM trades WHERE ticket=111").fetchone()
    assert t111["status"] == "closed"
    assert t111["broker_confirmed"] == 1
    assert t111["profit"] == -50.0


def test_unknown_outcome_never_invents_pnl(db, sources):
    tl.ingest_all(db, files_dir=sources["files_dir"],
                  state_dir=sources["state_dir"])
    t222 = db.execute("SELECT * FROM trades WHERE ticket=222").fetchone()
    assert t222["status"] == "unknown"
    assert t222["provisional"] == 1
    assert t222["profit"] is None, "P&L must never be invented"
    assert "UNKNOWN" in (t222["outcome_note"] or "")
    # still-open snapshot ticket stays open, not unknown
    t444 = db.execute("SELECT * FROM trades WHERE ticket=444").fetchone()
    assert t444["status"] == "open"


def test_known_vanished_ticket_gets_honesty_note(db, tmp_path):
    state = tmp_path / "st"
    files = tmp_path / "f"
    state.mkdir()
    files.mkdir()
    (state / "nova_journal.jsonl").write_text(json.dumps({
        "type": "trade.opened", "ticket": 10607517845, "deal": 1,
        "symbol": "GBPJPY", "direction": "BUY", "volume": 136.84,
        "entry_price": 210.48, "time": "2026.09.22 04:45:05",
        "signal_id": "G1", "command_id": "cmd-G1"}) + "\n")
    (files / "nova_positions.json").write_text(
        json.dumps({"positions": []}))  # broker shows nothing open
    tl.ingest_all(db, files_dir=str(files), state_dir=str(state))
    row = db.execute(
        "SELECT * FROM trades WHERE ticket=10607517845").fetchone()
    assert row["status"] == "unknown"
    assert row["profit"] is None
    note = row["outcome_note"] or ""
    assert "UNKNOWN" in note
    assert "NEVER" in note and "banked" in note


def test_command_rejection_recorded(db, sources):
    tl.ingest_all(db, files_dir=sources["files_dir"],
                  state_dir=sources["state_dir"])
    cmd = db.execute(
        "SELECT * FROM commands WHERE command_id='cmd-S3'").fetchone()
    assert cmd["ack_status"] == "rejected"
    assert cmd["reject_reason"] == "send_failed:4756"
    cmd1 = db.execute(
        "SELECT * FROM commands WHERE command_id='cmd-S1'").fetchone()
    assert cmd1["ack_status"] == "filled"
    assert cmd1["fill_ticket"] == 111


def test_lesson_roundtrip_and_review_dossier(db, sources):
    tl.ingest_all(db, files_dir=sources["files_dir"],
                  state_dir=sources["state_dir"])
    tl.add_lesson(db, 111, why_entered="EMA cross + RSI",
                  why_exited="SL hit", what_worked="entry timing",
                  what_failed="late session, thin liquidity",
                  what_to_change="avoid late-session entries",
                  author="agent")
    lesson = tl.get_lesson(db, 111)
    assert lesson["why_exited"] == "SL hit"
    dossier = tl.get_trade_review(db, 111)
    assert dossier["trade"]["ticket"] == 111
    assert dossier["signal"]["signal_id"] == "S1"
    assert dossier["decision"]["decision"] == "commanded"
    assert dossier["command"]["command_id"] == "cmd-S1"
    assert dossier["lesson"]["what_failed"] == "late session, thin liquidity"
    assert dossier["signal_outcome"]["outcome"] == "TP"
    assert tl.get_trade_review(db, 999999) is None


def test_daily_aggregate(db, sources):
    tl.ingest_all(db, files_dir=sources["files_dir"],
                  state_dir=sources["state_dir"])
    d = tl.daily_aggregate(db, "2026-09-22")
    assert d["signals"] >= 1
    assert d["commanded"] == 1
    assert d["decisions"]["skipped:spread_too_wide"] == 1
    # closes: 111 (-50) and 333 (+100) both exit 2026-09-22
    assert d["closed_trades"] == 2
    assert d["wins"] == 1 and d["losses"] == 1
    assert d["total_pnl"] == 50.0
    w = tl.weekly_aggregate(db, "2026-09-21")
    assert w["closed_trades"] == 2
    assert w["total_pnl"] == 50.0
    assert w["days"]["2026-09-22"]["total_pnl"] == 50.0


def test_no_account_numbers_persisted(db, sources):
    tl.ingest_all(db, files_dir=sources["files_dir"],
                  state_dir=sources["state_dir"])
    blob = json.dumps(tl.export_sanitized(db))
    assert "99999999" not in blob
    # raw account key must not appear in any stored payload
    for r in db.execute("SELECT raw_json FROM trades"):
        if r["raw_json"]:
            assert '"account"' not in r["raw_json"]


def test_outage_gap_detection(db, tmp_path):
    state = tmp_path / "st2"
    state.mkdir()
    lines = [
        {"type": "signal.received", "signal_id": "A",
         "time": "2026-09-22 10:00:00"},
        {"type": "signal.received", "signal_id": "B",
         "time": "2026-09-22 12:00:00"},  # 2h gap
    ]
    (state / "nova_journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in lines) + "\n")
    tl.init_schema(db)
    n1 = tl.detect_gaps(db, str(state / "nova_journal.jsonl"), threshold_min=45)
    assert n1 == 1
    n2 = tl.detect_gaps(db, str(state / "nova_journal.jsonl"), threshold_min=45)
    assert n2 == 0, "gap detection must be idempotent"
    row = db.execute("SELECT * FROM outages").fetchone()
    assert row["kind"] == "detected_gap"
    assert "not confirmed" in row["note"]
