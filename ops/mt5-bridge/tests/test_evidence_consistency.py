"""Repair round 2 (2026-09-24): published evidence pages must never
contradict the classifier.

Generates pages from SYNTHETIC journal data (no live run state, no
pre-generated evidence_out dir) and asserts each page's Status line equals
classify_outcome() for that ticket. A stale page (e.g. "OUTCOME UNKNOWN"
after the close was broker-confirmed) fails until the pages are
regenerated. Hermetic by design: the earlier live-data version passed on
the trading machine and failed on every fresh checkout (CI), which is
exactly what killed the build on commit 23fad237.
"""
import glob
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_report as tr  # noqa: E402

STATUS_TO_CLASS = {v: k for k, v in tr.STATUS_TEXT.items()}

XAG_TICKET = "10608752852"
XAG_NET = 208458.25  # 65.45 x 5000 x (66.015 - 65.378)


def _open(ticket, symbol="EURUSD", direction="BUY", entry=1.1000,
          day="2026-09-22"):
    return {"type": "trade.opened", "ticket": int(ticket), "symbol": symbol,
            "direction": direction, "volume": 1.0, "entry_price": entry,
            "time": f"{day} 10:00:00".replace("-", "."),
            "signal_id": "SIG_CONSISTENCY", "command_id": "cmd-consistency"}


def _broker_close(ticket, net, exit_price=1.1050, day="2026-09-22"):
    return {"type": "trade.closed", "ticket": int(ticket), "reason": "broker",
            "time": f"{day} 11:00:00".replace("-", "."),
            "exit_price": exit_price, "profit": net, "net_profit": net,
            "commission": 0.0, "swap": 0.0}


def _synthetic_journal():
    return [
        _open(XAG_TICKET, symbol="XAGUSD", entry=65.378),
        dict(_broker_close(XAG_TICKET, XAG_NET, exit_price=66.015),
             symbol="XAGUSD"),
        _open("10608752853"),
        _broker_close("10608752853", -120.50),
        _open("10608752854"),  # no close -> unknown
    ]


def _generate(tmp_path):
    out = str(tmp_path / "out")
    journal = _synthetic_journal()
    result = tr.generate(out, journal, [], [], {})
    assert not result["violations"], "privacy gate tripped on synthetic data"
    return out, journal


def _published_status(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("**Status:**"):
                return line[len("**Status:**"):].strip()
    return None


def test_all_published_pages_agree_with_classifier(tmp_path):
    out, journal = _generate(tmp_path)
    trades, _overrides, _pos, _snap = tr.build_trades(journal, [], [], {})
    by_ticket = {str(t["ticket"]): t for t in trades}

    pages = sorted(glob.glob(os.path.join(out, "*", "[0-9]*.md")))
    assert pages, "no evidence pages generated"
    assert len(pages) == len(trades)
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


def test_xagusd_208k_page_is_broker_confirmed(tmp_path):
    # The +$208,458.25 XAGUSD close shape (65.45 x 5000 x (66.015-65.378))
    # is broker-confirmed net in the journal; its page must say so, not
    # "unknown", and must carry the figure.
    out, _journal = _generate(tmp_path)
    pages = glob.glob(os.path.join(out, "*", f"{XAG_TICKET}.md"))
    assert pages, "XAGUSD page was not generated"
    assert _published_status(pages[0]) == tr.STATUS_TEXT["confirmed_profit"]
    body = open(pages[0], encoding="utf-8").read()
    assert "208,458.25" in body, "page must carry the broker net figure"


def test_unknown_close_page_stays_unknown(tmp_path):
    # The ticket with no close record must render OUTCOME UNKNOWN and
    # claim no P&L -- the page and the classifier must agree on that too.
    out, _journal = _generate(tmp_path)
    pages = glob.glob(os.path.join(out, "*", "10608752854.md"))
    assert pages, "unknown ticket page was not generated"
    assert _published_status(pages[0]) == tr.STATUS_TEXT["unknown"]
