"""Tests for trade_report.py honest classifications + privacy gate.

Uses synthetic FAKE identifiers only (99.../98.../555... ranges) -- no real
account numbers, deal/order IDs or credentials anywhere in fixtures.
"""
import json
import os
import re

import pytest

import trade_report as rep

FAKE_TICKET = "99000000001"
FAKE_TICKET2 = "99000000002"
FAKE_DEAL = "98111111111"
FAKE_ACCT = "555444333"


def make_trade(ticket=FAKE_TICKET, close=None, **kw):
    tr = {
        "ticket": ticket,
        "symbol": "EURUSD",
        "direction": "BUY",
        "timeframe": "M15",
        "entry_time": "2026.09.22 10:00:00",
        "entry_price": 1.1000,
        "command_id": "cmd-EURUSD_M15_BUY_1790088000",
        "signal_id": "EURUSD_M15_BUY_1790088000",
        "signal_entry": 1.1000,
        "stop_loss": 1.0950,
        "take_profit": 1.1100,
        "volume": 1.0,
        "signal": {"timeframe": "M15", "entry_price": 1.1000,
                   "stop_loss": 1.0950, "take_profit": 1.1100,
                   "trigger": "EMA20 crossed above EMA50, RSI=58.1",
                   "strategy": "ema-cross"},
        "decision": {"decision": "commanded", "volume": 1.0,
                     "risk_amount": 2000.0, "time": "2026-09-22 10:00:01"},
        "close": close,
        "open_source": "journal",
    }
    tr.update(kw)
    return tr


def page_of(tr):
    md, day = rep.trade_page(tr, [], {}, None)
    return md


# ---------------------------------------------------------------- classification matrix

def test_confirmed_profit_label():
    tr = make_trade(close={"reason": "broker", "profit": 10845.73,
                           "exit_price": 1.1100,
                           "time": "2026.09.22 12:00:00"})
    md = page_of(tr)
    assert "**Status:** CLOSED \u2014 PROFIT (broker-confirmed)" in md
    assert "$10,845.73 (broker-confirmed net)" in md
    assert "## Why profit \u2014 broker-confirmed" in md
    assert "PROVISIONAL" not in md.split("**Status:**")[1].split("\n")[0]


def test_confirmed_loss_label():
    tr = make_trade(close={"reason": "broker", "profit": -250.50,
                           "exit_price": 1.0950,
                           "time": "2026.09.22 12:00:00"})
    md = page_of(tr)
    assert "**Status:** CLOSED \u2014 LOSS (broker-confirmed)" in md
    assert "$-250.50 (broker-confirmed net)" in md
    assert "## Why loss \u2014 broker-confirmed" in md


def test_confirmed_breakeven_requires_exact_zero_net():
    tr = make_trade(close={"reason": "broker", "profit": 0.0,
                           "exit_price": 1.1000,
                           "time": "2026.09.22 12:00:00",
                           "commission": 0.0, "swap": 0.0})
    md = page_of(tr)
    assert "**Status:** CLOSED \u2014 BREAKEVEN (broker-confirmed)" in md
    assert "exactly" in md and "$0.00" in md


def test_provisional_estimate_never_drives_outcome_label():
    tr = make_trade(close={"reason": "broker-sl-reconciled", "profit": -1495.22,
                           "exit_price": 1.0950,
                           "time": "2026.09.22 12:00:00",
                           "note": "profit estimated from specs tick_value, "
                                   "excl. commission/swap"})
    md = page_of(tr)
    assert "**Status:** CLOSED \u2014 PROVISIONAL ESTIMATE (unconfirmed)" in md
    assert "provisional estimate" in md
    assert "NOT broker-confirmed" in md
    assert "CLOSED \u2014 LOSS" not in md
    assert "CLOSED \u2014 PROFIT" not in md
    assert "## Why this is only a provisional estimate" in md


def test_provisional_explicit_flag_wins():
    tr = make_trade(close={"reason": "broker", "profit": 500.0,
                           "exit_price": 1.1100,
                           "time": "2026.09.22 12:00:00",
                           "confirmation": "provisional"})
    md = page_of(tr)
    assert "PROVISIONAL ESTIMATE" in md
    assert "CLOSED \u2014 PROFIT" not in md


def test_explicit_broker_confirmation_overrides_note():
    tr = make_trade(close={"reason": "broker", "profit": 500.0,
                           "exit_price": 1.1100,
                           "time": "2026.09.22 12:00:00",
                           "note": "estimated at first",
                           "confirmation": "broker"})
    assert rep.classify_outcome(tr) == "confirmed_profit"


def test_entry_price_exit_is_not_breakeven():
    tr = make_trade(close={"reason": "broker-sl-reconciled", "profit": 0.0,
                           "exit_price": 1.1000,  # == entry
                           "time": "2026.09.22 12:00:00",
                           "note": "exit at entry, estimated, excl. commission/swap"})
    md = page_of(tr)
    assert "PROVISIONAL ESTIMATE" in md
    assert "BREAKEVEN" not in md.split("**Status:**")[1].split("\n")[0]
    assert "NOT a breakeven" in md


def test_unknown_when_no_close():
    tr = make_trade(close=None)
    md = page_of(tr)
    assert "**Status:** OUTCOME UNKNOWN \u2014 under reconciliation" in md
    assert "- P&L: unknown" in md
    assert "no profit or loss is claimed" in md


def test_unknown_when_close_has_no_pnl():
    tr = make_trade(close={"reason": "broker", "profit": None,
                           "time": "2026.09.22 12:00:00"})
    md = page_of(tr)
    assert "OUTCOME UNKNOWN" in md


def test_confirmation_of_matrix():
    assert rep.confirmation_of(None) is None
    assert rep.confirmation_of({"reason": "broker", "note": ""}) == "broker"
    assert rep.confirmation_of({"reason": "broker",
                                "note": "estimated, excl. commission"}) == "provisional"
    assert rep.confirmation_of({"reason": "broker-sl-reconciled"}) == "provisional"
    assert rep.confirmation_of({"reason": "weird"}) == "provisional"


# ---------------------------------------------------------------- charts

def test_chart_carries_schematic_label(tmp_path):
    tr = make_trade(close={"reason": "broker", "profit": 100.0,
                           "exit_price": 1.1100,
                           "time": "2026.09.22 12:00:00"})
    out = str(tmp_path / "chart.png")
    fig = rep.draw_schematic(tr, out)
    assert os.path.exists(out)
    texts = [t.get_text() for t in fig.texts]
    texts += [ax.get_title(loc="left") for ax in fig.axes]
    texts += [ax.get_xlabel() for ax in fig.axes]
    assert any(rep.SCHEMATIC_LABEL in t for t in texts), texts
    joined = "\n".join(texts).lower()
    assert "historical candle" not in joined


def test_chart_provisional_title(tmp_path):
    tr = make_trade(close={"reason": "broker-sl-reconciled", "profit": 20188.02,
                           "exit_price": 1.1100,
                           "time": "2026.09.22 12:00:00",
                           "note": "estimated from specs, excl. commission/swap"})
    out = str(tmp_path / "chart.png")
    fig = rep.draw_schematic(tr, out)
    title = fig.axes[0].get_title(loc="left")
    assert "PROVISIONAL ESTIMATE" in title
    assert "PROFIT" not in title


def test_chart_unknown_title(tmp_path):
    tr = make_trade(close=None)
    out = str(tmp_path / "chart.png")
    fig = rep.draw_schematic(tr, out)
    assert "OUTCOME UNKNOWN" in fig.axes[0].get_title(loc="left")


# ---------------------------------------------------------------- privacy checker unit tests

def write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_privacy_catches_account_login(tmp_path):
    p = write(tmp_path, "t.md",
              f"# trade\nAccount login: {FAKE_ACCT}\nall good otherwise\n")
    v = rep.scan_evidence_file(p, set())
    assert v, "account login must be caught"
    assert any("9-digit" in x or "labeled" in x for x in v)


def test_privacy_catches_deal_ticket(tmp_path):
    p = write(tmp_path, "t.md",
              f"# trade\nclosed by deal {FAKE_DEAL} at TP\n")
    v = rep.scan_evidence_file(p, set())
    assert v, "deal ticket must be caught"


def test_privacy_catches_hash_ticket(tmp_path):
    p = write(tmp_path, "t.md", "# trade\nsee #98111111112 for details\n")
    v = rep.scan_evidence_file(p, set())
    assert v, "#-prefixed ticket must be caught"


def test_privacy_catches_credentials(tmp_path):
    p = write(tmp_path, "t.md",
              "# trade\npassword = s3cret!\napi_key: ABCD1234\n")
    v = rep.scan_evidence_file(p, set())
    assert len(v) >= 2, v


def test_privacy_catches_bearer_token(tmp_path):
    p = write(tmp_path, "t.md", "# x\nAuthorization: Bearer abcdef123456\n")
    v = rep.scan_evidence_file(p, set())
    assert v


def test_privacy_clean_passes(tmp_path):
    body = (f"# EURUSD BUY -- ticket {FAKE_TICKET}\n\n"
            f"signal EURUSD_M15_BUY_1790088000 decided at 2026.09.22 10:00:01\n"
            f"P&L: $10,845.73 (broker-confirmed net)\n"
            f"planned risk: $2,000.00; RSI=58.1\n")
    p = write(tmp_path, f"{FAKE_TICKET}.md", body)
    v = rep.scan_evidence_file(p, {FAKE_TICKET, "1790088000"})
    assert v == [], v


def test_privacy_png_metadata_caught(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    bad = str(tmp_path / "bad.png")
    info = PngInfo()
    info.add_text("Comment", f"login {FAKE_ACCT}")
    Image.new("RGB", (8, 8)).save(bad, pnginfo=info)
    v = rep.scan_evidence_file(bad, set())
    assert v, "PNG text metadata must be scanned"

    good = str(tmp_path / "good.png")
    Image.new("RGB", (8, 8)).save(good)
    assert rep.scan_evidence_file(good, set()) == []


def test_privacy_png_pixel_bytes_not_flagged(tmp_path):
    # Raw pixel data must not be scanned (compressed bytes contain
    # coincidental digit runs); only text chunks are.
    p = str(tmp_path / f"{FAKE_TICKET}.png")
    tr = make_trade(close={"reason": "broker", "profit": 100.0,
                           "exit_price": 1.1100,
                           "time": "2026.09.22 12:00:00"})
    rep.draw_schematic(tr, p)
    assert rep.scan_evidence_file(p, {FAKE_TICKET}) == []


# ---------------------------------------------------------------- end-to-end generate() + privacy gate

def _journal_lines(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_end_to_end_privacy_fail_on_leaked_deal(tmp_path, monkeypatch):
    out = tmp_path / "out"
    j = tmp_path / "journal.jsonl"
    j.write_text(_journal_lines(
        {"type": "trade.opened", "ticket": int(FAKE_TICKET), "symbol": "EURUSD",
         "direction": "BUY", "volume": 1.0, "entry_price": 1.1,
         "time": "2026.09.22 10:00:00", "signal_id": "SIG1"},
        {"type": "trade.closed", "ticket": int(FAKE_TICKET), "profit": 50.0,
         "reason": "manual", "time": "2026.09.22 11:00:00",
         "exit_price": 1.1050,
         # bare deal number: sanitize_note only redacts #-prefixed ones,
         # so the privacy gate must catch this.
         "note": f"closed by hand, deal {FAKE_DEAL} filled ok"}))
    monkeypatch.setattr(rep, "JOURNAL_PATH", str(j))
    monkeypatch.setattr(rep, "TRADES_PATH", str(tmp_path / "nope.jsonl"))
    monkeypatch.setattr(rep, "SIGNALS_PATH", str(tmp_path / "nope2.jsonl"))
    monkeypatch.setattr(rep, "POSITIONS_PATH", str(tmp_path / "nope3.json"))
    rc = rep.main(["--out", str(out)])
    assert rc == 1, "run must FAIL (non-zero exit) on leaked deal ticket"


def test_end_to_end_clean_mixed_classes(tmp_path, monkeypatch):
    out = tmp_path / "out"
    j = tmp_path / "journal.jsonl"
    j.write_text(_journal_lines(
        {"type": "trade.opened", "ticket": int(FAKE_TICKET), "symbol": "EURUSD",
         "direction": "BUY", "volume": 1.0, "entry_price": 1.1,
         "time": "2026.09.22 10:00:00", "signal_id": "SIG1"},
        {"type": "trade.closed", "ticket": int(FAKE_TICKET), "profit": 100.0,
         "reason": "broker", "time": "2026.09.22 11:00:00",
         "exit_price": 1.1050},
        {"type": "trade.opened", "ticket": int(FAKE_TICKET2), "symbol": "GBPUSD",
         "direction": "SELL", "volume": 2.0, "entry_price": 1.25,
         "time": "2026.09.22 10:05:00", "signal_id": "SIG2"},
        {"type": "trade.closed", "ticket": int(FAKE_TICKET2), "profit": -40.0,
         "reason": "broker-sl-reconciled", "time": "2026.09.22 11:05:00",
         "exit_price": 1.2550,
         "note": "estimated from specs tick_value, excl. commission/swap"}))
    monkeypatch.setattr(rep, "JOURNAL_PATH", str(j))
    monkeypatch.setattr(rep, "TRADES_PATH", str(tmp_path / "nope.jsonl"))
    monkeypatch.setattr(rep, "SIGNALS_PATH", str(tmp_path / "nope2.jsonl"))
    monkeypatch.setattr(rep, "POSITIONS_PATH", str(tmp_path / "nope3.json"))
    rc = rep.main(["--out", str(out)])
    assert rc == 0
    idx = (out / "index.md").read_text()
    # realized total counts ONLY the broker-confirmed close
    assert "**Realized P&L so far: $100.00**" in idx
    assert "1 broker-confirmed close(s)" in idx
    assert "provisional estimate(s) (not counted)" in idx
    prov_page = (out / "2026-09-22" / f"{FAKE_TICKET2}.md").read_text()
    assert "PROVISIONAL ESTIMATE" in prov_page
    assert "CLOSED \u2014 LOSS" not in prov_page


def test_end_to_end_real_data_privacy_clean(tmp_path):
    # Regression: the gate must pass on the real current inputs.
    out = tmp_path / "out"
    rc = rep.main(["--out", str(out)])
    assert rc == 0, "privacy gate must pass on real current data"
    assert (out / "index.md").exists()


def test_generate_empty_inputs_creates_index(tmp_path):
    # Regression (CI 2026-09-22): with zero trades (no journal/broker/signal
    # data, e.g. a fresh checkout in CI) generate() must still create the out
    # dir and write index.md instead of raising FileNotFoundError, and the
    # privacy gate must pass on the empty report.
    out = tmp_path / "out"
    result = rep.generate(str(out), [], [], [], {})
    assert result["violations"] == []
    assert (out / "index.md").exists()


# --------------------------------------- QueryClose reconciliation contracts
# 2026-09-22: the backfill pipeline writes confirmation_status="broker-confirmed"
# (not a bare "confirmation" field) and supersedes provisional closes with
# trade.close_corrected. The reporter must honor both, and prefer net P&L.

import trade_report as tr


def _trade(close):
    return {"ticket": 1, "close": close, "_open_pos": None}


def test_confirmation_status_broker_confirmed_accepted():
    c = {"confirmation_status": "broker-confirmed", "net_profit": 10.0}
    assert tr.confirmation_of(c) == "broker"
    assert tr.classify_outcome(_trade(c)) == "confirmed_profit"


def test_close_net_prefers_net_over_profit():
    c = {"confirmation_status": "broker-confirmed",
         "profit": -1495.41, "net_profit": -2016.15}
    assert tr.close_net(c) == -2016.15
    assert tr.classify_outcome(_trade(c)) == "confirmed_loss"


def test_close_corrected_overlays_provisional_close():
    journal = [
        {"type": "trade.closed", "ticket": 7, "profit": -1495.22,
         "reason": "broker-sl-reconciled",
         "note": "profit estimated from specs tick_value, excl. commission/swap"},
    ]
    feed = [
        {"type": "trade.close_corrected", "ticket": 7,
         "profit": -1495.41, "swap": -520.74, "net_profit": -2016.15,
         "confirmation_status": "broker-confirmed", "reconciled": True,
         "note": "QueryClose: broker shows swap -520.74, net -2016.15"},
    ]
    trades, _, _, _ = tr.build_trades(journal, feed, [], [])
    # the close exists for the ticket even with no open record... (opens empty)
    assert trades == []
    # rebuild with an open record so the trade is listed
    journal2 = journal + [{"type": "trade.opened", "ticket": 7,
                           "symbol": "GBPAUD"}]
    trades, _, _, _ = tr.build_trades(journal2, feed, [], [])
    assert len(trades) == 1
    assert tr.classify_outcome(trades[0]) == "confirmed_loss"
    assert tr.close_net(trades[0]["close"]) == -2016.15
    assert trades[0]["close"]["corrected_from_provisional"] is True
