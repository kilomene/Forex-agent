"""Repair round 2 (2026-09-24): published evidence pages must never
contradict the classifier.

Rebuilds every trade from the current durable logs and asserts each
published per-trade page's Status line equals classify_outcome() for that
ticket. A stale page (e.g. "OUTCOME UNKNOWN" after the close was
broker-confirmed) fails until the pages are regenerated.
"""
import glob
import json
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_report as tr  # noqa: E402

EVIDENCE = os.path.join(BRIDGE, "evidence_out")
STATUS_TO_CLASS = {v: k for k, v in tr.STATUS_TEXT.items()}


def _published_status(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("**Status:**"):
                return line[len("**Status:**"):].strip()
    return None


def test_all_published_pages_agree_with_classifier():
    journal = tr.load_jsonl(tr.JOURNAL_PATH)
    broker_trades = tr.load_jsonl(tr.TRADES_PATH)
    signals = tr.load_jsonl(tr.SIGNALS_PATH)
    try:
        with open(tr.POSITIONS_PATH, encoding="utf-8",
                  errors="replace") as f:
            positions = json.load(f)
    except (OSError, ValueError):
        positions = {}
    trades, _overrides, _pos, _snap = tr.build_trades(
        journal, broker_trades, signals, positions)
    by_ticket = {str(t["ticket"]): t for t in trades}

    pages = sorted(glob.glob(os.path.join(EVIDENCE, "*", "[0-9]*.md")))
    assert pages, "no published evidence pages found"
    mismatches = []
    for p in pages:
        ticket = os.path.splitext(os.path.basename(p))[0]
        status = _published_status(p)
        assert status is not None, f"{p}: no Status line"
        cls = STATUS_TO_CLASS.get(status)
        assert cls is not None, f"{p}: unrecognized status {status!r}"
        live = by_ticket.get(ticket)
        assert live is not None, f"{p}: ticket not in rebuilt trades"
        expected = tr.classify_outcome(live)
        if cls != expected:
            mismatches.append(f"{ticket}: page={cls} classifier={expected}")
    assert not mismatches, (
        "stale evidence pages (regenerate with trade_report.py):\n"
        + "\n".join(mismatches))


def test_xagusd_208k_page_is_broker_confirmed():
    # The +$208,458.25 XAGUSD close (ticket 10608752852) is broker-confirmed
    # net in the journal; its page must say so, not "unknown".
    p = os.path.join(EVIDENCE, "2026-09-22", "10608752852.md")
    assert os.path.exists(p)
    assert _published_status(p) == tr.STATUS_TEXT["confirmed_profit"]
