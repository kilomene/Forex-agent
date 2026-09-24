"""Regression tests for the NovaTrader EA redeploy (2026-09-23).

Covers the three reconstructed features from the Python side:
 1. broker-position publishing: the EA's exact nova_positions.json schema
    (ticket, symbol, type, volume, open_price, current_price, open_time,
    sl, tp, profit + server_time) must parse through load_broker_positions.
 2. exit reconciliation: a reconciled trade.closed (reason "broker") with
    real P&L must mirror into the journal and count in P&L; one with
    honest-unknown P&L (profit null) must not crash anything, must not
    fabricate a number, and must not count as a loss.
 3. the EA close-record contract: documents the exact JSON shapes the MQL5
    emits so future EA edits stay compatible.

MQL5 logic itself (breakeven modify, deal-history scan) cannot be
unit-tested here; manual verification steps are documented in
ops/mt5-bridge/docs/ea-redeploy-verification.md.
"""
import json
import os
import time

import trade_executor as te


def _ea_positions_payload():
    # Mirrors the MQL5 StringFormat in PublishPositions() exactly.
    return {
        "time": 1729675200,
        "server_time": "2026.09.23 12:00:00",
        "account": 112975129,
        "positions": [
            {"ticket": 10623564489, "symbol": "XAUEUR", "volume": 0.11,
             "type": "BUY", "magic": 20260921,
             "open_price": 4521.10, "current_price": 4525.40,
             "profit": 12.55, "sl": 4510.00, "tp": 4540.00,
             "open_time": 1729673702},
            {"ticket": 10628887260, "symbol": "CHFJPY", "volume": 1.13,
             "type": "SELL", "magic": 20260921,
             "open_price": 191.856, "current_price": 191.900,
             "profit": -45.20, "sl": 192.100, "tp": 191.400,
             "open_time": 1729673705},
        ],
    }


def test_positions_feed_full_ea_schema(tmp_path):
    fd = str(tmp_path)
    p = os.path.join(fd, "nova_positions.json")
    with open(p, "w") as f:
        json.dump(_ea_positions_payload(), f)
    os.utime(p, (time.time(), time.time()))
    positions, fresh = te.load_broker_positions(fd)
    assert fresh is True
    assert len(positions) == 2
    required = {"ticket", "symbol", "type", "volume", "open_price",
                "current_price", "open_time", "sl", "tp", "profit"}
    for pos in positions:
        assert required <= set(pos), f"missing fields: {required - set(pos)}"
        assert pos["type"] in ("BUY", "SELL")
        assert isinstance(pos["open_time"], int)
    data = json.load(open(p))
    assert isinstance(data["server_time"], str) and data["server_time"]


def _engine(tmp_path):
    fd = os.path.join(str(tmp_path), "files")
    sd = os.path.join(str(tmp_path), "state")
    os.makedirs(fd)
    os.makedirs(sd)
    open(os.path.join(fd, "nova_trades.jsonl"), "w").close()
    return te.TraderEngine(files_dir=fd, state_dir=sd), fd, sd


def _journal_entries(sd):
    jp = os.path.join(sd, "nova_journal.jsonl")
    if not os.path.exists(jp):
        return []
    return [json.loads(l) for l in open(jp) if l.strip()]


def test_reconciled_close_real_pnl_mirrored(tmp_path):
    eng, fd, sd = _engine(tmp_path)
    tp = os.path.join(fd, "nova_trades.jsonl")
    with open(tp, "a") as f:
        f.write(json.dumps({"type": "trade.opened", "ticket": 10623564384,
                            "symbol": "NZDUSD", "direction": "SELL",
                            "volume": 2.27}) + "\n")
        # Exact shape the EA's ReconcilePositions() emits on a found deal.
        f.write(json.dumps({"type": "trade.closed", "ticket": 10623564384,
                            "exit_price": 0.57010, "profit": -54.48,
                            "reason": "broker", "time": "2026.09.23 02:41:00",
                            "symbol": "NZDUSD", "direction": "SELL",
                            "volume": 2.27,
                            "note": "broker-side exit detected by EA "
                                    "position watch"}) + "\n")
    assert eng.tail_trades() == 2
    closes = [e for e in _journal_entries(sd) if e.get("type") == "trade.closed"]
    assert len(closes) == 1
    assert closes[0]["profit"] == -54.48
    assert closes[0]["reason"] == "broker"
    jp = os.path.join(sd, "nova_journal.jsonl")
    # The fixture close is dated 2026.09.23: derive "today" from the
    # fixture, not the wall clock (repair round 2, 2026-09-24 -- the old
    # time.strftime("%Y-%m-%d") made this test a date bomb that fails on
    # any day other than 2026-09-23).
    today = "2026-09-23"
    _, pnl, losses = te.load_trade_state(tp, today, jp)
    assert pnl == -54.48
    assert losses == 1


def test_reconciled_close_unknown_pnl_is_safe(tmp_path):
    eng, fd, sd = _engine(tmp_path)
    tp = os.path.join(fd, "nova_trades.jsonl")
    with open(tp, "a") as f:
        f.write(json.dumps({"type": "trade.opened", "ticket": 10623888393,
                            "symbol": "XAGUSD", "direction": "BUY",
                            "volume": 0.9}) + "\n")
        # Honest-unknown shape: profit null, never a fabricated number.
        f.write(json.dumps({"type": "trade.closed", "ticket": 10623888393,
                            "exit_price": None, "profit": None,
                            "profit_status": "unknown", "reason": "broker",
                            "time": "2026.09.23 09:40:00",
                            "symbol": "XAGUSD", "direction": "BUY",
                            "volume": 0.9,
                            "note": "broker-side exit; closing deal not "
                                    "found in 2-day history after 24h "
                                    "missing; P&L under reconciliation, "
                                    "not invented"}) + "\n")
    assert eng.tail_trades() == 2  # must not raise
    closes = [e for e in _journal_entries(sd) if e.get("type") == "trade.closed"]
    assert len(closes) == 1
    assert closes[0]["profit"] is None
    assert closes[0]["profit_status"] == "unknown"
    jp = os.path.join(sd, "nova_journal.jsonl")
    # Fixture close is dated 2026.09.23 -- see the date-bomb note above.
    today = "2026-09-23"
    open_pos, pnl, losses = te.load_trade_state(tp, today, jp)
    assert pnl == 0.0  # unknown contributes nothing, fabricates nothing
    assert losses == 0  # unknown must not count as a loss
    assert "10623888393" not in open_pos and 10623888393 not in open_pos


def test_ea_close_record_contract():
    # Documents the two JSON shapes NovaTrader.mq5 ReconcilePositions()
    # may emit. If the EA changes these, this test (and the pipeline)
    # must be updated together.
    real = {"type": "trade.closed", "ticket": 1, "exit_price": 1.2345,
            "profit": -12.34, "reason": "broker", "time": "2026.09.23 02:41:00",
            "symbol": "EURUSD", "direction": "BUY", "volume": 1.0,
            "note": "broker-side exit detected by EA position watch"}
    unknown = {"type": "trade.closed", "ticket": 2, "exit_price": None,
               "profit": None, "profit_status": "unknown", "reason": "broker",
               "time": "2026.09.23 09:40:00", "symbol": "EURUSD",
               "direction": "SELL", "volume": 1.0,
               "note": "broker-side exit; closing deal not found in 2-day "
                       "history after 24h missing; P&L under reconciliation, "
                       "not invented"}
    for rec in (real, unknown):
        assert rec["type"] == "trade.closed"
        assert rec["reason"] == "broker"
        assert isinstance(rec["ticket"], int)
        p = rec["profit"]
        assert p is None or isinstance(p, (int, float)), \
            "profit must be a real number or null -- never a fabricated string"
    assert unknown["profit_status"] == "unknown"
    assert "not invented" in unknown["note"]


def test_trades_file_one_json_per_line(tmp_path):
    # The EA's AlreadyClosedInTradesFile guard matches a ticket within the
    # single JSON line holding each trade.closed. If a line ever held two
    # records, the guard could misfire (2026-09-23: a 600-char window bled
    # into the next line and falsely reported "already closed", silently
    # dropping real GBPCAD/NZDUSD closes). Every writer must keep
    # one-JSON-per-line.
    p = tmp_path / "nova_trades.jsonl"
    p.write_text(
        '{"type":"trade.closed","ticket":111,"profit":-1.0}\n'
        '{"type":"trade.opened","ticket":222}\n'
        '{"type":"trade.closed","ticket":333,"profit":2.0}\n')
    lines = p.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        rec = json.loads(line)
        assert rec["type"] in ("trade.opened", "trade.closed",
                               "trade.rejected")
    # the guard's contract: ticket 222 must NOT be found on a trade.closed
    # line even though it follows one directly
    for line in lines:
        if '"trade.closed"' in line:
            assert '"ticket":222' not in line
