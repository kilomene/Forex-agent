"""Issue 1 regression tests: ticket_tracker.py read/verify behavior.

The seen-ticket file is EA-owned; the Python tracker never writes it.
Tests cover whitespace-tolerant parsing, corrupt-file recovery, and
cross-checking against durable open/close history. Hermetic (tmp_path).
"""
import json
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import ticket_tracker as tt  # noqa: E402


def write(path, text):
    with open(path, "w") as f:
        f.write(text)
    return path


def test_parse_compact_json():
    doc = {"tickets": [
        {"ticket": 10641277984, "symbol": "GBPNZD", "type": "BUY",
         "volume": 1.04, "tracked_at": 1790000000, "missing_since": 0},
        {"ticket": 10640513739, "symbol": "CHFJPY", "type": "SELL",
         "volume": 1.13, "tracked_at": 1790000001, "missing_since": 12},
    ]}
    p = write("/tmp/tt_compact.json", json.dumps(doc))
    r = tt.parse_seen_file(p)
    assert r["tickets"] == [10641277984, 10640513739]
    assert r["corrupt"] == []
    assert r["entries"][1]["missing_since"] == 12
    assert r["entries"][0]["direction"] == "BUY"


def test_parse_whitespace_variants(tmp_path):
    # Invalid JSON (missing comma) but whitespace-variant entries:
    # forces the tolerant scan path, mirroring the EA's fixed loop.
    txt = ('{"tickets":[{ "ticket" : 111 , "symbol" : "AAA" }\n'
           '{ "ticket":222,\n"symbol":"BBB" }\n'
           '{\t"ticket"\t:\t333\t}]}')
    p = write(str(tmp_path / "seen.json"), txt)
    r = tt.parse_seen_file(p)
    assert r["tickets"] == [111, 222, 333]
    assert r["corrupt"] == []
    assert r["entries"][2]["symbol"] is None  # field absent -> None


def test_parse_corrupt_file_recovers(tmp_path):
    p = write(str(tmp_path / "seen.json"), "not json at all {{{")
    r = tt.parse_seen_file(p)
    assert r["entries"] == [] and r["tickets"] == []
    assert len(r["corrupt"]) == 1


def test_parse_missing_file_no_raise(tmp_path):
    r = tt.parse_seen_file(str(tmp_path / "nope.json"))
    assert r["entries"] == [] and len(r["corrupt"]) == 1


def test_parse_empty_file(tmp_path):
    p = write(str(tmp_path / "seen.json"), "  \n")
    r = tt.parse_seen_file(p)
    assert r == {"entries": [], "corrupt": [], "tickets": []}


def test_parse_schema_drift_fails_loud(tmp_path):
    # Repair round 2 (2026-09-24): if the EA ever changes the seen-file
    # schema (e.g. {"seen":[...]} instead of {"tickets":[...]}), the old
    # fast path silently returned ZERO tickets -- a schema drift that
    # looks exactly like "no positions tracked". It must fail loudly.
    p = write(str(tmp_path / "seen.json"),
              json.dumps({"seen": [{"ticket": 111}]}))
    r = tt.parse_seen_file(p)
    assert r["tickets"] == []
    assert len(r["corrupt"]) == 1
    assert "schema" in r["corrupt"][0]


def test_parse_non_dict_json_fails_loud(tmp_path):
    p = write(str(tmp_path / "seen.json"), json.dumps([{"ticket": 111}]))
    r = tt.parse_seen_file(p)
    assert r["tickets"] == []
    assert len(r["corrupt"]) == 1


def test_audit_marks_schema_drift_not_ok(tmp_path):
    # audit_seen_file must surface schema drift as ok=False, never a
    # clean bill of health.
    seen = write(str(tmp_path / "seen.json"),
                 json.dumps({"seen": [{"ticket": 111}]}))
    trades = write(str(tmp_path / "trades.jsonl"), "")
    journal = write(str(tmp_path / "journal.jsonl"), "")
    rep = tt.audit_seen_file(seen, trades, journal)
    assert rep["verify"]["ok"] is False
    assert rep["verify"]["corrupt_spans"] == 1


def test_verify_ok():
    r = tt.verify_seen_against_history([1, 2], [1, 2], [3])
    assert r["ok"] is True
    assert r["untracked_open"] == [] and r["tracked_but_closed"] == []


def test_verify_untracked_open():
    r = tt.verify_seen_against_history([1], [1, 2], [])
    assert r["ok"] is False
    assert r["untracked_open"] == [2]


def test_verify_tracked_but_closed():
    r = tt.verify_seen_against_history([1, 2], [1, 2], [2])
    assert r["ok"] is False
    assert r["tracked_but_closed"] == [2]
    assert r["untracked_open"] == []  # closed tickets are not "open"


def test_verify_duplicate_tickets():
    r = tt.verify_seen_against_history([5, 5, 6], [5, 6], [])
    assert r["ok"] is False
    assert r["duplicate_tickets"] == [5]


def test_verify_string_tickets_normalized():
    r = tt.verify_seen_against_history(["10641277984"], ["10641277984"], [])
    assert r["ok"] is True
