"""Regression tests: weekend reflection + final report must dedup closes by ticket.

Forensic finding (2026-10-17): the raw journal holds ~10x duplicated
trade.closed events (mirrored executor events + reconciled broker-history
backfills), so the Saturday reflection reported 2,225 closed trades and
+$4,861,822.21 instead of the real 219 unique trades and +$253,612.46.
The daily summary was already fixed (repair round 2, 2026-09-24) to
dedup per ticket with trade.close_corrected winning and net_profit
preferred -- these two scripts must follow the same rule.
"""
import json
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import weekend_reflection as wr  # noqa: E402
import final_report as fr  # noqa: E402


def C(ticket, profit, **kw):
    e = {"type": "trade.closed", "ticket": ticket, "profit": profit,
         "symbol": "EURUSD", "direction": "BUY",
         "time": "2026-10-10 10:00:00"}
    e.update(kw)
    return e


def write_journal(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_dedup_collapses_mirrored_duplicates():
    journal = [
        C(111, -100.0),                      # provisional
        C(111, -100.0, reason="broker"),     # mirrored duplicate
        C(111, -100.0, reconciled=True),     # reconciled backfill
        C(222, 250.0),
        C(222, 250.0, reason="broker"),
    ]
    uniq, raw = wr.dedup_closed(journal)
    assert raw == 5
    assert len(uniq) == 2
    assert sorted(e["ticket"] for e in uniq) == [111, 222]


def test_close_corrected_wins_regardless_of_order():
    # correction first, provisional LATER -- correction must still win
    journal = [
        {"type": "trade.close_corrected", "ticket": 111,
         "profit": -100.0, "net_profit": -160.74,
         "time": "2026-10-10 10:00:00", "symbol": "EURUSD",
         "direction": "BUY"},
        C(111, -100.0),  # late provisional must not clobber the correction
    ]
    uniq, _raw = wr.dedup_closed(journal)
    assert len(uniq) == 1
    assert uniq[0]["type"] == "trade.close_corrected"
    # and the reverse order too
    uniq2, _ = wr.dedup_closed(list(reversed(journal)))
    assert len(uniq2) == 1
    assert uniq2[0]["type"] == "trade.close_corrected"


def test_ticketless_closes_each_count():
    journal = [C(None, -10.0), C(None, -20.0), C("", -30.0)]
    uniq, raw = wr.dedup_closed(journal)
    assert raw == 3
    assert len(uniq) == 3  # never merged into one bucket


def test_final_report_uses_same_dedup():
    journal = [C(111, -100.0), C(111, -100.0), C(222, 250.0)]
    uniq, raw = fr.dedup_closed(journal)
    assert (raw, len(uniq)) == (3, 2)


def test_reflection_end_to_end_deduped(tmp_path, monkeypatch):
    journal_path = tmp_path / "nova_journal.jsonl"
    events = []
    for t, p in [(111, -100.0), (222, 250.0), (333, -50.0)]:
        events.append(C(t, p))
        events.append(C(t, p, reason="broker"))  # duplicate each
    write_journal(str(journal_path), events)
    monkeypatch.setattr(wr, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(wr, "JOURNAL_PATH", str(journal_path))
    assert wr.main() == 0
    md_files = list(tmp_path.glob("reflection_*.md"))
    assert len(md_files) == 1
    md = md_files[0].read_text()
    assert "Closed trades so far: **3**" in md
    assert "2225" not in md
    # a reflection record was appended to the journal
    lines = journal_path.read_text().strip().split("\n")
    assert json.loads(lines[-1])["type"] == "reflection"
    assert json.loads(lines[-1])["overall"]["trades"] == 3


def test_final_report_end_to_end_deduped(tmp_path, monkeypatch):
    journal_path = tmp_path / "nova_journal.jsonl"
    events = []
    for t, p in [(111, -100.0), (222, 250.0)]:
        events.append(C(t, p))
        events.append(C(t, p, reason="broker"))
    write_journal(str(journal_path), events)
    monkeypatch.setattr(fr, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(fr, "JOURNAL_PATH", str(journal_path))
    assert fr.main() == 0
    md = (tmp_path / "FINAL_REPORT.md").read_text()
    assert "| Trades closed | 2 |" in md
    assert "Journaled close events (incl. duplicates) | 4 |" in md
    assert "Total P&L | +150.00 |" in md
    # one row per ticket in the trade table
    table_rows = [l for l in md.split("\n")
                  if l.startswith("| ") and ("| 111 |" in l or "| 222 |" in l)]
    assert len(table_rows) == 2
