"""Track A repairs (2026-09-22): phantom-position auto-reconcile + fail-closed
risk basis. Hermetic unit tests (tmp_path only, no MT5, no network)."""
import json
import math
import os
import sys
from datetime import datetime

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_executor as te  # noqa: E402


# ---------------------------------------------------------------- helpers

def spec(**kw):
    s = {
        "tick_value": 1.0,
        "tick_size": 0.001,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "stops_level_points": 0,
        "spread_points": 20,
        "digits": 3,
        "point": 0.001,
    }
    s.update(kw)
    return s


def gate_ctx(**over):
    ctx = dict(
        enabled=True,
        dry_run=False,
        specs={"symbols": {"XPDUSD": spec()},
               "account": {"server": "MetaQuotes-Demo", "equity": 1_258_148.44,
                           "balance": 1_258_148.44}},
        specs_fresh=True,
        open_positions={},
        todays_profit=0.0,
        today_str="2026-09-22",
        risk_pct=1.0,
        max_concurrent=10,
        max_spread_points=50,
        max_daily_loss_pct=3.0,
        equity=1_258_148.44,
        balance=1_258_148.44,
        max_total_exposure_lots=0,
        open_lots=0.0,
        max_consecutive_losses=4,
        consecutive_losses=0,
        max_correlated_positions=1,
        require_stop_loss=True,
        risk_basis=200_000.0,
    )
    ctx.update(over)
    return ctx


def sig(**over):
    s = {
        "id": "XPDUSD_M15_BUY_1790082000",
        "type": "signal.detected",
        "symbol": "XPDUSD",
        "timeframe": "M15",
        "direction": "BUY",
        "entry_price": 1310.914,
        "stop_loss": 1306.344,
        "take_profit": 1320.054,
    }
    s.update(over)
    return s


def live_cfg(**over):
    cfg = dict(
        trading_enabled=True,
        dry_run=False,
        risk_per_trade_pct=0.5,   # owner order 2026-09-22: 0.5% of $200,000
        max_concurrent_trades=10,
        max_daily_loss_pct=3.0,
        max_spread_points=50,
        max_correlated_positions=1,
        max_consecutive_losses=4,
        max_total_exposure_lots=0,
        require_stop_loss=True,
        capital_basis=200000,
    )
    cfg.update(over)
    return cfg


def live_specs(equity=1_258_148.44):
    now = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    return {
        "time": now,
        "account": {"equity": equity, "balance": equity,
                    "currency": "USD", "server": "MetaQuotes-Demo",
                    "time": now},
        "symbols": {"XPDUSD": spec()},
    }


# ------------------------------------------------- FIX 2: risk basis -----

VALID_EQUITY = 1_258_148.44

INVALID = [None, 0, -5, -0.5, "abc", "", "12x", float("nan"),
           float("inf"), float("-inf"), [], {}]


@pytest.mark.parametrize("bad", INVALID)
def test_effective_risk_basis_bad_configured_basis(bad):
    assert te.effective_risk_basis({"capital_basis": bad}, VALID_EQUITY) is None


@pytest.mark.parametrize("bad", INVALID)
def test_effective_risk_basis_bad_broker_equity(bad):
    assert te.effective_risk_basis({"capital_basis": 200000}, bad) is None


def test_effective_risk_basis_missing_key():
    assert te.effective_risk_basis({}, VALID_EQUITY) is None
    assert te.effective_risk_basis(None, VALID_EQUITY) is None


def test_effective_risk_basis_numeric_string_accepted():
    assert te.effective_risk_basis({"capital_basis": "200000"},
                                   VALID_EQUITY) == 200000.0


def test_effective_risk_basis_valid():
    assert te.effective_risk_basis({"capital_basis": 200000},
                                   VALID_EQUITY) == 200000.0


def test_effective_risk_basis_clamps_to_equity():
    assert te.effective_risk_basis({"capital_basis": 2_000_000},
                                   500_000.0) == 500_000.0


@pytest.mark.parametrize("bad", INVALID)
def test_gate_risk_basis_unavailable(bad):
    d, flags = te.check_gates(sig(), **gate_ctx(risk_basis=bad))
    assert d == "skipped:risk_basis_unavailable"
    assert flags.get("loud") is True


def test_gate_risk_basis_unavailable_beats_sizing():
    # Even a perfectly sizable signal is refused when the basis is unknown.
    d, _ = te.check_gates(sig(), **gate_ctx(risk_basis=None))
    assert d == "skipped:risk_basis_unavailable"


def test_gate_trading_disabled_still_wins():
    d, _ = te.check_gates(sig(), **gate_ctx(enabled=False, risk_basis=None))
    assert d == "skipped:trading_disabled"


def test_decide_risk_basis_unavailable(tmp_path):
    eng = te.TraderEngine(files_dir=str(tmp_path / "f"),
                          state_dir=str(tmp_path / "s"))
    d, _ = eng.decide(sig(), live_cfg(capital_basis=0), True,
                      live_specs(), True, {}, 0.0, "2026-09-22")
    assert d == "skipped:risk_basis_unavailable"


def test_decide_risk_basis_unavailable_bad_equity(tmp_path):
    eng = te.TraderEngine(files_dir=str(tmp_path / "f"),
                          state_dir=str(tmp_path / "s"))
    d, _ = eng.decide(sig(), live_cfg(), True,
                      live_specs(equity=float("nan")), True,
                      {}, 0.0, "2026-09-22")
    assert d == "skipped:risk_basis_unavailable"


def test_decide_eligible_plans_exactly_1000_risk(tmp_path):
    """0.5% of the $200,000 allocated basis = exactly $1,000 planned risk
    (owner order 2026-09-22)."""
    eng = te.TraderEngine(files_dir=str(tmp_path / "f"),
                          state_dir=str(tmp_path / "s"))
    d, flags = eng.decide(sig(), live_cfg(), True,
                          live_specs(), True, {}, 0.0, "2026-09-22")
    assert d == "commanded"
    assert flags["risk_amount"] == 1000.0


# ------------------------------------------- FIX 1: auto-reconcile --------

SYNC_LINE = ("KK\t0\t10:29:27.559\tNetwork\t'X': terminal synchronized "
             "with MetaQuotes Ltd.: 0 positions, 0 orders, 12373 symbols, "
             "0 spreads")


def write_log(logs_dir, lines):
    name = datetime.now().strftime("%Y%m%d") + ".log"
    p = os.path.join(logs_dir, name)
    with open(p, "w", encoding="utf-16-le") as f:
        f.write("\n".join(lines) + "\n")
    return p


def test_latest_sync_count_zero():
    d = "/tmp/te-reconcile-a"
    os.makedirs(d, exist_ok=True)
    write_log(d, ["noise line",
                  SYNC_LINE.replace("0 positions", "2 positions"),
                  SYNC_LINE])
    count, line = te.latest_terminal_position_count(d)
    assert count == 0
    assert "0 positions, 0 orders" in line


def test_latest_sync_count_takes_last_line():
    d = "/tmp/te-reconcile-b"
    os.makedirs(d, exist_ok=True)
    write_log(d, [SYNC_LINE,
                  SYNC_LINE.replace("0 positions", "3 positions")])
    count, _ = te.latest_terminal_position_count(d)
    assert count == 3


def test_latest_sync_count_missing_log():
    count, line = te.latest_terminal_position_count("/tmp/te-no-such-dir")
    assert count is None and line is None


def test_latest_sync_count_no_sync_lines():
    d = "/tmp/te-reconcile-c"
    os.makedirs(d, exist_ok=True)
    write_log(d, ["just noise", "more noise"])
    count, line = te.latest_terminal_position_count(d)
    assert count is None and line is None


def make_engine(tmp_path):
    files = tmp_path / "files"
    state = tmp_path / "run"
    logs = tmp_path / "logs"
    files.mkdir()
    state.mkdir()
    logs.mkdir()
    return te.TraderEngine(files_dir=str(files), state_dir=str(state)), \
        str(logs), str(state)


def read_journal(state_dir):
    p = os.path.join(state_dir, "nova_journal.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def test_reconcile_clears_phantoms_on_zero(tmp_path):
    eng, logs, state = make_engine(tmp_path)
    write_log(logs, [SYNC_LINE])
    eng.state["open_map"] = {
        "10606155417": {"symbol": "XAUEUR"},
        "10607517845": {"symbol": "GBPJPY"},
    }
    n = eng.reconcile_phantom_positions(logs_dir=logs)
    assert n == 2
    assert eng.state["open_map"] == {}
    # durable: state file on disk also cleared
    saved = json.load(open(os.path.join(state, "trade_executor.state.json")))
    assert saved["open_map"] == {}
    evs = [e for e in read_journal(state)
           if e.get("type") == "positions.reconciled"]
    assert len(evs) == 1
    ev = evs[0]
    assert {t["ticket"] for t in ev["tickets_closed"]} == \
        {"10606155417", "10607517845"}
    assert ev["source"] == SYNC_LINE
    assert "UNKNOWN" in ev["note"]
    assert "No P&L figures created or implied" in ev["note"]
    # no invented P&L anywhere in the event
    assert "profit" not in json.dumps(ev).lower()


def test_reconcile_noop_when_positions_exist(tmp_path):
    eng, logs, state = make_engine(tmp_path)
    write_log(logs, [SYNC_LINE.replace("0 positions", "2 positions")])
    eng.state["open_map"] = {"10606155417": {"symbol": "XAUEUR"}}
    assert eng.reconcile_phantom_positions(logs_dir=logs) == 0
    assert eng.state["open_map"] != {}
    assert not [e for e in read_journal(state)
                if e.get("type") == "positions.reconciled"]


def test_reconcile_noop_when_log_unavailable(tmp_path):
    eng, logs, state = make_engine(tmp_path)
    eng.state["open_map"] = {"10606155417": {"symbol": "XAUEUR"}}
    assert eng.reconcile_phantom_positions(
        logs_dir="/tmp/te-no-such-dir") == 0
    assert eng.state["open_map"] != {}


def test_reconcile_noop_when_open_map_empty(tmp_path):
    eng, logs, state = make_engine(tmp_path)
    write_log(logs, [SYNC_LINE])
    assert eng.reconcile_phantom_positions(logs_dir=logs) == 0
    assert not [e for e in read_journal(state)
                if e.get("type") == "positions.reconciled"]


def test_reconcile_noop_on_stale_sync_line(tmp_path):
    # Regression: a 10:29 sync line must never clear positions opened at
    # 12:00. The sync evidence has to be newer than every open entry.
    eng, logs, state = make_engine(tmp_path)
    write_log(logs, [SYNC_LINE])  # 10:29:27 today, 0 positions
    eng.state["open_map"] = {
        "10618377948": {"symbol": "EURCAD",
                        "time": "2026.09.22 15:00:04",
                        "direction": "BUY"},
    }
    assert eng.reconcile_phantom_positions(logs_dir=logs) == 0
    assert eng.state["open_map"] != {}
    assert not [e for e in read_journal(state)
                if e.get("type") == "positions.reconciled"]


def test_reconcile_clears_when_sync_line_is_fresh(tmp_path):
    # Same zero-position sync line, but stamped after the open entry:
    # genuine phantom, clear proceeds.
    eng, logs, state = make_engine(tmp_path)
    fresh = SYNC_LINE.replace("10:29:27.559", "15:05:00.000")
    write_log(logs, [fresh])
    eng.state["open_map"] = {
        "10618377948": {"symbol": "EURCAD",
                        "time": "2026.09.22 15:00:04",
                        "direction": "BUY"},
    }
    assert eng.reconcile_phantom_positions(logs_dir=logs) == 1
    assert eng.state["open_map"] == {}


# ------------------------------------------------- mirror field preservation
# 2026-09-22: the trades->journal mirror rebuilt trade.closed from a fixed
# field subset, dropping reconciled/confirmation_status/net_profit/deal ids.
# Backfilled closes lost their audit trail (and fired live alerts). The
# mirror must now carry the full source event through.

def _write_trades(files_dir, events):
    p = os.path.join(files_dir, "nova_trades.jsonl")
    with open(p, "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


def test_mirror_preserves_backfill_audit_fields(tmp_path):
    eng, logs, state = make_engine(tmp_path)
    files = os.path.join(str(tmp_path), "files")
    src = {
        "type": "trade.closed",
        "ticket": 10607517845,
        "symbol": "GBPJPY",
        "direction": "BUY",
        "volume": 136.84,
        "entry_price": 210.48,
        "exit_price": 210.712,
        "profit": 20162.64,
        "commission": 0.0,
        "swap": 0.0,
        "net_profit": 20162.64,
        "confirmation_status": "broker-confirmed",
        "reason": "broker-tp",
        "deal_in": 10336151802,
        "deal_out": 10337335595,
        "exit_time_broker": "2026.09.22 06:40:43",
        "time": "2026-09-22 04:06:03",
        "reconciled": True,
        "source": "QueryClose history_query.json",
        "note": "backfilled from broker deal history",
    }
    _write_trades(files, [src])
    assert eng.tail_trades() == 1
    closes = [e for e in read_journal(state)
              if e.get("type") == "trade.closed"]
    assert len(closes) == 1
    rec = closes[0]
    for k in ("reconciled", "confirmation_status", "net_profit",
              "deal_in", "deal_out", "exit_time_broker", "source", "note",
              "symbol", "direction", "volume"):
        assert rec.get(k) == src[k], k


def test_mirror_journals_close_corrected_verbatim(tmp_path):
    eng, logs, state = make_engine(tmp_path)
    files = os.path.join(str(tmp_path), "files")
    corr = {
        "type": "trade.close_corrected",
        "ticket": 10605494223,
        "symbol": "GBPAUD",
        "net_profit": -2016.15,
        "prior_profit_estimate": -1495.22,
        "confirmation_status": "broker-confirmed",
        "reconciled": True,
        "time": "2026-09-22 04:06:03",
    }
    _write_trades(files, [corr])
    eng.tail_trades()
    found = [e for e in read_journal(state)
             if e.get("type") == "trade.close_corrected"]
    assert len(found) == 1
    assert found[0]["net_profit"] == -2016.15
    assert found[0]["reconciled"] is True


def test_mirror_idempotent_across_restart(tmp_path):
    # A kill between mirror and a re-read must not duplicate the journal:
    # the trades_seen dedup key makes a second tail_trades() a no-op.
    eng, logs, state = make_engine(tmp_path)
    files = os.path.join(str(tmp_path), "files")
    src = {"type": "trade.closed", "ticket": 1, "symbol": "EURUSD",
           "profit": 5.0, "time": "2026-09-22 04:00:00"}
    _write_trades(files, [src])
    assert eng.tail_trades() == 1
    # Simulate a restart: a brand-new engine object over the SAME state dir
    # must treat the already-mirrored event as seen (trades_seen dedup).
    eng2 = te.TraderEngine(files_dir=str(tmp_path / "files"),
                           state_dir=str(tmp_path / "run"))
    assert eng2.tail_trades() == 0
    closes = [e for e in read_journal(state)
              if e.get("type") == "trade.closed"]
    assert len(closes) == 1
