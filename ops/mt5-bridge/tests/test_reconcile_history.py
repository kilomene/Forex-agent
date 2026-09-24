"""Issue 3 regression tests: rebuild_open_close_sets() + verify_close().

Hermetic (tmp_path only). Derives authoritative open/close sets purely
from the two durable logs and verifies every close's P&L components.
"""
import json
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

from reconcile import rebuild_open_close_sets, verify_close  # noqa: E402


def _write(path, events):
    with open(path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return path


def _opened(ticket, src="ea"):
    return {"type": "trade.opened", "ticket": ticket, "symbol": "EURUSD",
            "direction": "BUY", "volume": 1.0, "deal": 9000 + ticket % 1000,
            "fill_price": 1.10, "time": "2026.09.22 10:00:00",
            "signal_id": "s", "command_id": "cmd-%d" % ticket}


def _closed_full(ticket):
    return {"type": "trade.closed", "ticket": ticket, "symbol": "EURUSD",
            "direction": "BUY", "volume": 1.0, "entry_price": 1.10,
            "exit_price": 1.12, "profit": 195.0, "gross_profit": 200.0,
            "swap": 0.0, "commission": -5.0, "net_profit": 195.0,
            "deal_id": 777001, "order_id": 555001, "position_id": ticket,
            "reason": "command", "time": "2026-09-22 11:00:00",
            "signal_id": "s", "command_id": "cmd-%d" % ticket}


def test_sets_from_both_logs(tmp_path):
    trades = _write(str(tmp_path / "t.jsonl"),
                    [_opened(1), _opened(2), _closed_full(1)])
    journal = _write(str(tmp_path / "j.jsonl"),
                     [_opened(1), _opened(2), _closed_full(1),
                      {"type": "positions.reconciled", "time": "t",
                       "tickets_closed": [{"ticket": "2",
                                           "symbol": "EURUSD"}]}])
    r = rebuild_open_close_sets(trades, journal)
    assert r["opened_tickets_ea"] == [1, 2]
    assert r["opened_tickets_journal"] == [1, 2]
    assert r["reconciled_tickets"] == [2]
    assert r["open_tickets"] == []
    assert r["closed_tickets"] == [1, 2]
    assert r["phantoms"] == []
    assert r["mirror_gaps"] == []
    assert r["journal_extra_opens"] == []


def test_phantom_close_flagged(tmp_path):
    trades = _write(str(tmp_path / "t.jsonl"), [_closed_full(9)])
    journal = _write(str(tmp_path / "j.jsonl"), [_closed_full(9)])
    r = rebuild_open_close_sets(trades, journal)
    assert r["phantoms"] == [9]
    assert r["open_tickets"] == []


def test_mirror_gap_and_journal_extra_open(tmp_path):
    trades = _write(str(tmp_path / "t.jsonl"), [_opened(1), _opened(2)])
    journal = _write(str(tmp_path / "j.jsonl"), [_opened(1), _opened(3)])
    r = rebuild_open_close_sets(trades, journal)
    assert r["mirror_gaps"] == [2]
    assert r["journal_extra_opens"] == [3]
    assert r["open_tickets"] == [1, 2, 3]


def test_close_corrected_counts_as_close_evidence(tmp_path):
    # Repair round 2 (2026-09-24): a ticket whose ONLY close record is a
    # trade.close_corrected revision (provisional trade.closed never
    # journaled, e.g. lost across a restart) must still count as closed --
    # the old journal loop ignored trade.close_corrected, so the ticket
    # looked open forever.
    corrected = dict(_closed_full(5), type="trade.close_corrected",
                     note="broker-history reconciliation")
    trades = _write(str(tmp_path / "t.jsonl"), [_opened(5)])
    journal = _write(str(tmp_path / "j.jsonl"),
                     [_opened(5), corrected])
    r = rebuild_open_close_sets(trades, journal)
    assert 5 in r["closed_tickets"]
    assert 5 not in r["open_tickets"]
    assert 5 not in r["phantoms"]


def test_close_corrected_in_ea_log_counts_as_close_evidence(tmp_path):
    corrected = dict(_closed_full(6), type="trade.close_corrected")
    trades = _write(str(tmp_path / "t.jsonl"), [_opened(6), corrected])
    journal = _write(str(tmp_path / "j.jsonl"), [_opened(6)])
    r = rebuild_open_close_sets(trades, journal)
    assert 6 in r["closed_tickets"]
    assert 6 not in r["open_tickets"]


def test_bad_lines_counted_not_fatal(tmp_path):
    tp = str(tmp_path / "t.jsonl")
    with open(tp, "w") as f:
        f.write('{"type":"trade.opened","ticket":1}\n')
        f.write('this is not json\n')
        f.write('\n')
    jp = _write(str(tmp_path / "j.jsonl"), [])
    r = rebuild_open_close_sets(tp, jp)
    assert r["bad_lines"] == {"trades": 1, "journal": 0}
    assert r["mirror_gaps"] == [1]


def test_verify_close_pass():
    v = verify_close(_closed_full(1))
    assert v["status"] == "pass"
    assert v["deal_ids"]["deal_id"] == 777001
    assert v["missing_fields"] == []
    assert v["math"]["diff"] == 0.0


def test_verify_close_missing_ids_and_components():
    ev = {"type": "trade.closed", "ticket": 5, "profit": -114.66,
          "reason": "broker"}  # EA-mirror shape: profit only
    v = verify_close(ev)
    assert v["status"] == "incomplete"
    assert "deal/order ids" in v["missing_fields"]
    assert "gross_profit" in v["missing_fields"]
    assert v["math"] is None


def test_verify_close_math_mismatch():
    ev = _closed_full(1)
    ev["net_profit"] = 999.99  # tampered
    v = verify_close(ev)
    assert v["status"] == "mismatch"
    assert "diff" in v["math"] and abs(v["math"]["diff"]) > 0.05


def test_verify_close_unknown_honest():
    ev = {"type": "trade.closed", "ticket": 7, "exit_price": None,
          "profit": None, "profit_status": "unknown", "reason": "broker"}
    v = verify_close(ev)
    assert v["status"] == "incomplete"
    assert "never invented" in v["note"]


def test_verify_close_rounding_tolerance():
    ev = _closed_full(1)
    ev["net_profit"] = 195.04  # within 0.05 tolerance
    assert verify_close(ev)["status"] == "pass"
    ev["net_profit"] = 195.06  # outside tolerance
    assert verify_close(ev)["status"] == "mismatch"


def test_xagusd_close_shape_flagged_honestly():
    # Real 2026-09-22 XAGUSD ticket 10608752852 close shape: broker
    # deal ids + swap/commission/net_profit present, but NO gross_profit
    # field. The verifier must flag it incomplete -- never invent gross.
    ev = {"type": "trade.closed", "ticket": 10608752852, "symbol": "XAGUSD",
          "direction": "SELL", "volume": 65.45, "entry_price": 66.015,
          "exit_price": 65.378, "profit": 208458.25, "commission": 0.0,
          "swap": 0.0, "net_profit": 208458.25,
          "confirmation_status": "broker-confirmed", "reason": "broker-tp",
          "deal_in": 10337385568, "deal_out": 10338637554,
          "reconciled": True}
    v = verify_close(ev)
    assert v["status"] == "incomplete"
    assert v["missing_fields"] == ["gross_profit"]
    assert v["deal_ids"] == {"deal_in": 10337385568,
                             "deal_out": 10338637554}
    assert v["math"] is None  # no fabrication of the missing component
