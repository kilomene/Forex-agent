"""Issue 5 regression tests (2026-09-23): genuine per-trade evidence.

- an evidence .md exists for EVERY closed ticket in the journal
- no evidence file claims P&L for an unresolved trade
  (opened with no verified close -> OUTCOME UNKNOWN, no P&L claimed)
- every generated chart is visibly labeled schematic
- every page carries the required evidence fields:
  ticket, deal_in/deal_out IDs, symbol, direction, volume,
  entry/exit prices + times, gross profit, swap, commission, net_profit,
  close reason, source (journal line refs), lesson ref when one exists
"""
import json
import os
import re

import pytest

import trade_report as rep

FAKE_TICKET = "99000000011"
FAKE_DEAL_IN = "98111111111"
FAKE_DEAL_OUT = "98222222222"


def _synthetic_open(ticket=FAKE_TICKET, **kw):
    e = {"type": "trade.opened", "ticket": int(ticket), "symbol": "EURUSD",
         "direction": "BUY", "volume": 1.0, "entry_price": 1.1000,
         "time": "2026.09.23 10:00:00", "signal_id": "SIG_ISSUE5",
         "command_id": "cmd-issue5"}
    e.update(kw)
    return e


def md_files_under(out_dir):
    found = {}
    for root, _, files in os.walk(out_dir):
        for fn in files:
            if fn.endswith(".md") and fn != "index.md":
                found[fn[:-3]] = os.path.join(root, fn)
    return found


# ---------------------------------------------------------------- coverage

def _write_synthetic_journal(tmp_path):
    j = tmp_path / "journal.jsonl"
    j.write_text("\n".join(json.dumps(e) for e in [
        _synthetic_open(),
        {"type": "trade.closed", "ticket": int(FAKE_TICKET),
         "reason": "broker", "time": "2026.09.23 11:00:00",
         "exit_price": 1.1050, "profit": 100.0,
         "deal_in": int(FAKE_DEAL_IN), "deal_out": int(FAKE_DEAL_OUT),
         "commission": 7.0, "swap": 0.0, "net_profit": 93.0,
         "gross_profit": 100.0},
    ]) + "\n")
    return j


def _patched_paths(monkeypatch, tmp_path, journal_path):
    monkeypatch.setattr(rep, "JOURNAL_PATH", str(journal_path))
    monkeypatch.setattr(rep, "TRADES_PATH", str(tmp_path / "nope.jsonl"))
    monkeypatch.setattr(rep, "SIGNALS_PATH", str(tmp_path / "nope2.jsonl"))
    monkeypatch.setattr(rep, "POSITIONS_PATH", str(tmp_path / "nope3.json"))
    monkeypatch.setattr(rep, "LESSONS_DB", str(tmp_path / "nope.db"))


def test_evidence_file_exists_for_every_closed_ticket(tmp_path, monkeypatch):
    # Contract: every ticket with a trade.closed journal event gets a
    # per-trade evidence page. Hermetic: generates from a synthetic
    # journal into tmp (the earlier live-data version failed on any
    # fresh checkout, which killed CI on commit 23fad237).
    out = tmp_path / "out"
    _patched_paths(monkeypatch, tmp_path, _write_synthetic_journal(tmp_path))
    rc = rep.main(["--out", str(out)])
    assert rc == 0, "privacy gate must pass on synthetic data"
    pages = md_files_under(str(out))
    assert FAKE_TICKET in pages, \
        f"no evidence page for closed ticket {FAKE_TICKET}"
    pngs = set()
    for root, _, files in os.walk(str(out)):
        for fn in files:
            if fn.endswith(".png"):
                pngs.add(fn[:-4])
    assert FAKE_TICKET in pngs, \
        f"no schematic chart for closed ticket {FAKE_TICKET}"


def test_required_evidence_fields_on_pages(tmp_path, monkeypatch):
    # Hermetic: required fields must appear on pages generated from a
    # synthetic journal (the earlier live-data version only passed where
    # the live broker files happened to exist, which killed CI).
    out = tmp_path / "out"
    _patched_paths(monkeypatch, tmp_path, _write_synthetic_journal(tmp_path))
    assert rep.main(["--out", str(out)]) == 0
    pages = md_files_under(str(out))
    assert pages, "no pages generated from synthetic journal"
    required = ["Deal in ID:", "Deal out ID:", "Gross profit:", "Swap:",
                "Commission:", "Net profit:", "Source: nova_journal.jsonl line",
                "- Symbol:", "- Direction:", "- Volume:",
                "- Entry price:", "- Exit price:", "- Entry time:",
                "- Exit time:", "- Exit reason:", "## Lesson"]
    for ticket, path in sorted(pages.items()):
        md = open(path, encoding="utf-8").read()
        for field in required:
            assert field in md, f"{ticket}: missing field {field!r}"
        # the source ref must name at least one journal line
        assert re.search(r"nova_journal\.jsonl line \d+", md), ticket
        # lesson section: either a real lesson ref or the honest absence line
        assert ("- Lesson ref:" in md
                or "No lesson recorded for this ticket" in md), ticket


def test_every_chart_labeled_schematic(tmp_path):
    # Behavioral: inspect each generated figure object for the visible
    # schematic label (title / figure text / axis label). No OCR needed.
    # Hermetic: synthetic trades, no live broker files.
    journal = [
        _synthetic_open(),
        {"type": "trade.closed", "ticket": int(FAKE_TICKET),
         "reason": "broker", "time": "2026.09.23 11:00:00",
         "exit_price": 1.1050, "profit": 100.0, "net_profit": 93.0},
        _synthetic_open(ticket="99000000012"),
    ]
    trades, _, pos_by_ticket, snap_time = rep.build_trades(
        journal, [], [], {})
    assert trades
    for tr in trades:
        tr["_open_pos"] = pos_by_ticket.get(tr["ticket"])
        tr["_snap_time"] = snap_time
        out = str(tmp_path / f"{tr['ticket']}.png")
        fig = rep.draw_schematic(tr, out)
        texts = [t.get_text() for t in fig.texts]
        texts += [ax.get_title(loc="left") for ax in fig.axes]
        texts += [ax.get_xlabel() for ax in fig.axes]
        assert any(rep.SCHEMATIC_LABEL in t for t in texts), tr["ticket"]
        assert os.path.getsize(out) > 0


# ---------------------------------------------------------------- unresolved trades: no invented P&L

def _page_for(journal_events, lessons=None):
    trades, overrides, pos_by_ticket, snap_time = rep.build_trades(
        journal_events, [], [], {})
    assert len(trades) == 1
    tr = trades[0]
    md, _ = rep.trade_page(tr, overrides, pos_by_ticket, snap_time, lessons)
    return md, tr


def test_unresolved_trade_claims_no_pnl():
    md, tr = _page_for([_synthetic_open()])
    assert rep.classify_outcome(tr) == "unknown"
    assert "**Status:** OUTCOME UNKNOWN" in md
    assert "- P&L: unknown" in md
    assert "no profit or loss is claimed" in md
    assert "broker-confirmed" not in md
    # no exact money figure is presented as a result
    assert not re.search(r"P&L: \$", md)


def test_close_without_pnl_figure_is_unknown():
    md, tr = _page_for([
        _synthetic_open(),
        {"type": "trade.closed", "ticket": int(FAKE_TICKET),
         "reason": "broker", "time": "2026.09.23 11:00:00",
         "exit_price": 1.1050, "profit": None},
    ])
    assert rep.classify_outcome(tr) == "unknown"
    assert "OUTCOME UNKNOWN" in md
    assert "CLOSED — PROFIT" not in md
    assert "CLOSED — LOSS" not in md


def test_unresolved_page_shows_not_recorded_not_invented():
    md, _ = _page_for([_synthetic_open()])
    assert "- Deal in ID: not recorded" in md
    assert "- Deal out ID: not recorded" in md
    assert "- Gross profit: not recorded" in md
    assert "- Net profit: not recorded" in md
    assert "- Exit price: not recorded" in md


# ---------------------------------------------------------------- own-deal-id allowlist + honesty

def test_privacy_gate_allows_own_deal_ids(tmp_path):
    out = tmp_path / "out"
    j = tmp_path / "journal.jsonl"
    j.write_text("\n".join(json.dumps(e) for e in [
        _synthetic_open(),
        {"type": "trade.closed", "ticket": int(FAKE_TICKET),
         "reason": "broker", "time": "2026.09.23 11:00:00",
         "exit_price": 1.1050, "profit": 100.0,
         "deal_in": int(FAKE_DEAL_IN), "deal_out": int(FAKE_DEAL_OUT),
         "commission": 7.0, "swap": 0.0, "net_profit": 93.0,
         "gross_profit": 100.0},
    ]) + "\n")
    import unittest.mock as mock
    with mock.patch.object(rep, "JOURNAL_PATH", str(j)), \
         mock.patch.object(rep, "TRADES_PATH", str(tmp_path / "nope.jsonl")), \
         mock.patch.object(rep, "SIGNALS_PATH", str(tmp_path / "nope2.jsonl")), \
         mock.patch.object(rep, "POSITIONS_PATH", str(tmp_path / "nope3.json")), \
         mock.patch.object(rep, "LESSONS_DB", str(tmp_path / "nope.db")):
        rc = rep.main(["--out", str(out)])
    assert rc == 0, "own deal IDs on own page must not trip the privacy gate"
    md = (out / "2026-09-23" / f"{FAKE_TICKET}.md").read_text()
    assert f"- Deal in ID: {FAKE_DEAL_IN}" in md
    assert f"- Deal out ID: {FAKE_DEAL_OUT}" in md
    assert "- Gross profit: $100.00" in md
    assert "- Commission: $7.00" in md
    assert "- Net profit: $93.00" in md


def test_privacy_gate_still_catches_foreign_deal_ids(tmp_path):
    # A deal ID that belongs to NOBODY in this report (e.g. pasted into a
    # note) must still fail the run.
    out = tmp_path / "out"
    j = tmp_path / "journal.jsonl"
    j.write_text("\n".join(json.dumps(e) for e in [
        _synthetic_open(),
        {"type": "trade.closed", "ticket": int(FAKE_TICKET),
         "reason": "manual", "time": "2026.09.23 11:00:00",
         "exit_price": 1.1050, "profit": 50.0,
         "note": "see also deal 98333333333 from the other account"},
    ]) + "\n")
    import unittest.mock as mock
    with mock.patch.object(rep, "JOURNAL_PATH", str(j)), \
         mock.patch.object(rep, "TRADES_PATH", str(tmp_path / "nope.jsonl")), \
         mock.patch.object(rep, "SIGNALS_PATH", str(tmp_path / "nope2.jsonl")), \
         mock.patch.object(rep, "POSITIONS_PATH", str(tmp_path / "nope3.json")), \
         mock.patch.object(rep, "LESSONS_DB", str(tmp_path / "nope.db")):
        rc = rep.main(["--out", str(out)])
    assert rc == 1, "foreign deal ID in a note must still fail the gate"


def test_lesson_section_without_lesson():
    md, _ = _page_for([_synthetic_open()], lessons={})
    assert "## Lesson" in md
    assert "No lesson recorded for this ticket" in md


def test_lesson_section_with_lesson():
    md, _ = _page_for([_synthetic_open()], lessons={
        FAKE_TICKET: {"ref": FAKE_TICKET, "kind": "trade",
                      "why_entered": "EMA cross", "why_exited": "TP hit",
                      "what_worked": "patience", "what_failed": "late entry",
                      "what_to_change": "earlier trigger",
                      "author": "T6", "created_at": "2026-09-23 10:00:00"}})
    assert f"- Lesson ref: {FAKE_TICKET}" in md
    assert "Why entered: EMA cross" in md
    assert "What to change: earlier trigger" in md
