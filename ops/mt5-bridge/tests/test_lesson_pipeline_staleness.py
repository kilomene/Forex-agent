"""Adversarial regression tests for the lesson-pipeline staleness defect
(Racheal engagement #5, 2026-10-13).

Defect: nothing ran trade_learning.ingest_all between 2026-10-13 10:50 UTC
and 13:06 UTC (it was solely T6's sweep duty) — 21,108 journal lines,
including 3 trade.closed events, went un-ingested and the learning store
went stale. The durable repair is the `learning-ingest` 15-min cron; these
tests pin the code-level contract that makes the cron effective:

1. ingest_all must consume the journal to EOF even after a simulated stall
   (cursor left behind, new closes appended later).
2. backfilled lessons (add_lesson from journal evidence) must satisfy the
   check_lesson_pnl integrity contract.
3. cursors must advance to EOF for every source after ingest_all.

Hermetic: tmp_path only, no MT5, no network, no live DB.
"""
import json
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_learning as tl  # noqa: E402
import check_lesson_pnl as clp  # noqa: E402


@pytest.fixture
def db(tmp_path):
    conn = tl.connect(str(tmp_path / "test.db"))
    tl.init_schema(conn)
    yield conn
    conn.close()


def _write(path, events):
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def live_like(tmp_path):
    """A tmp journal + db pair mimicking the live layout for check_lessons."""
    state = tmp_path / "state"
    state.mkdir()
    journal = state / "nova_journal.jsonl"
    journal.write_text("")
    db_path = str(tmp_path / "live.db")
    return {"journal": str(journal), "db": db_path}


def _close(ticket, profit, t="2026-10-13 12:00:00", symbol="EURUSD"):
    return {"type": "trade.closed", "ticket": ticket, "symbol": symbol,
            "direction": "BUY", "volume": 1.0, "profit": profit,
            "exit_price": 1.1, "reason": "broker", "time": t}


def test_ingest_catches_up_after_stall(db, tmp_path):
    """Simulate the 2026-10-13 stall: ingest, then new closes land in the
    journal with no ingest running, then ingest must pick up everything."""
    state = tmp_path / "st"
    files = tmp_path / "f"
    state.mkdir()
    files.mkdir()
    journal = state / "nova_journal.jsonl"
    _write(journal, [_close(101, 10.0)])
    tl.ingest_all(db, files_dir=str(files), state_dir=str(state))
    assert db.execute(
        "SELECT profit FROM trades WHERE ticket=101").fetchone()["profit"] == 10.0

    # stall window: closes happen, nobody ingests
    _write(journal, [_close(102, -20.0), _close(103, 30.0)])
    assert db.execute(
        "SELECT COUNT(*) n FROM trades WHERE ticket IN (102,103)").fetchone()["n"] == 0

    # cron-equivalent catch-up run
    tl.ingest_all(db, files_dir=str(files), state_dir=str(state))
    rows = {r["ticket"]: r["profit"] for r in db.execute(
        "SELECT ticket, profit FROM trades WHERE ticket IN (101,102,103)")}
    assert rows == {101: 10.0, 102: -20.0, 103: 30.0}
    # cursor pinned at EOF: a further ingest must be a no-op
    before = tl.table_counts(db)
    tl.ingest_all(db, files_dir=str(files), state_dir=str(state))
    assert tl.table_counts(db) == before


def test_cursor_advances_to_eof_for_every_source(db, tmp_path):
    state = tmp_path / "st"
    files = tmp_path / "f"
    state.mkdir()
    files.mkdir()
    journal = state / "nova_journal.jsonl"
    trades_f = files / "nova_trades.jsonl"
    signals_f = files / "nova_signals.jsonl"
    _write(journal, [_close(201, 5.0)])
    _write(trades_f, [_close(202, 6.0)])
    _write(signals_f, [{"id": "SX", "type": "signal.detected",
                        "symbol": "EURUSD", "server_time": "2026.10.13 12:00:00"}])
    tl.ingest_all(db, files_dir=str(files), state_dir=str(state))
    cursors = {r["source"]: r["last_offset"] for r in db.execute(
        "SELECT source, last_offset FROM ingest_state")}
    assert cursors["journal"] == os.path.getsize(journal)
    assert cursors["trades_file"] == os.path.getsize(trades_f)
    assert cursors["signals_file"] == os.path.getsize(signals_f)


def test_backfilled_lessons_satisfy_integrity_contract(live_like):
    """Lessons backfilled from journal evidence via add_lesson (the exact
    repair applied to tickets 10919549804/10924523808/10925975653) must pass
    check_lessons with zero problems."""
    _write(live_like["journal"],
           [_close(301, 194.52, symbol="USDMXN"),
            _close(302, -97.56, symbol="EURAUD"),
            _close(303, 0.0, symbol="XPTUSD")])
    conn = tl.connect(live_like["db"])
    tl.init_schema(conn)
    tl.add_lesson(conn, 301, author="backfill",
                  why_exited="TP hit. Realized +194.52.")
    tl.add_lesson(conn, 302, author="backfill",
                  why_exited="SL hit. Realized -97.56.")
    tl.add_lesson(conn, 303, author="backfill",
                  why_exited="Flat scratch. Realized +0.00.")
    conn.close()
    res = clp.check_lessons(live_like["db"], live_like["journal"])
    assert res["missing_lessons"] == []
    assert res["mismatches"] == []
    assert res["missing_claims"] == []


def test_corrected_transcription_satisfies_integrity_contract(live_like):
    """A lesson whose transcribed figure is corrected to the journal-verified
    figure (the dropped-hundreds-digit class: -7.34 -> -107.34) must pass."""
    _write(live_like["journal"], [_close(401, -107.34, symbol="XAUAUD")])
    conn = tl.connect(live_like["db"])
    tl.init_schema(conn)
    # as first written by the scribe (wrong transcription)
    tl.add_lesson(conn, 401, author="scribe",
                  why_exited="Clean SL hit. Loss -07.34.")
    conn.close()
    res = clp.check_lessons(live_like["db"], live_like["journal"])
    assert res["mismatches"] != [], "sanity: wrong transcription must fail"
    # the correction applied in engagement #5
    conn = tl.connect(live_like["db"])
    row = conn.execute(
        "SELECT why_exited FROM lessons WHERE ref='401'").fetchone()[0]
    conn.execute("UPDATE lessons SET why_exited=? WHERE ref='401'",
                 (row.replace("-07.34", "-107.34"),))
    conn.commit()
    conn.close()
    res = clp.check_lessons(live_like["db"], live_like["journal"])
    assert res["mismatches"] == []
    assert res["missing_claims"] == []
