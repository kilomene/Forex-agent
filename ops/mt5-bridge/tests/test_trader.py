"""Hermetic unit tests for trade_executor.py (tmp_path only, no MT5, no network)."""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_executor as te  # noqa: E402


# ---------------------------------------------------------------- fixtures

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
    """Baseline context where every gate PASSES in live mode."""
    ctx = dict(
        enabled=True,
        dry_run=False,
        specs={"symbols": {"XPDUSD": spec()},
               "account": {"server": "MetaQuotes-Demo", "equity": 1_000_000.0,
                           "balance": 1_000_000.0}},
        specs_fresh=True,
        open_positions={},
        todays_profit=0.0,
        today_str="2026-09-21",
        risk_pct=1.0,
        max_concurrent=3,
        max_spread_points=50,
        max_daily_loss_pct=2.0,
        equity=1_000_000.0,
        balance=1_000_000.0,
        max_total_exposure_lots=1.0,
        open_lots=0.0,
        max_consecutive_losses=2,
        consecutive_losses=0,
        max_correlated_positions=1,
        require_stop_loss=True,
        # fail-closed risk basis (2026-09-22): None/invalid -> skip.
        # Baseline fixtures carry a valid basis so other gates are tested.
        risk_basis=1_000_000.0,
    )
    ctx.update(over)
    return ctx


def sig(**over):
    s = {
        "id": "XPDUSD_M15_BUY_1790028000",
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


FIXTURE_SIGNALS = [
    {
        "id": "XPDUSD_M15_BUY_1790028000", "type": "signal.detected",
        "symbol": "XPDUSD", "timeframe": "M15", "direction": "BUY",
        "entry_price": 1310.914, "stop_loss": 1306.344,
        "take_profit": 1320.054, "candle_time": "2026.09.21 22:00:00",
        "strategy": "ema_rsi",
    },
    {
        "id": "BAC_M15_BUY_1790027100", "type": "signal.detected",
        "symbol": "BAC", "timeframe": "M15", "direction": "BUY",
        "entry_price": 58.23, "stop_loss": 58.04,
        "take_profit": 58.61, "candle_time": "2026.09.21 21:45:00",
        "strategy": "ema_rsi",
    },
    {
        "id": "XPTUSD_M15_BUY_1790028900", "type": "signal.detected",
        "symbol": "XPTUSD", "timeframe": "M15", "direction": "BUY",
        "entry_price": 1807.751, "stop_loss": 1802.491,
        "take_profit": 1818.27, "candle_time": "2026.09.21 22:15:00",
        "strategy": "ema_rsi",
    },
]

SANDBOX_SPECS = {
    "XPDUSD": spec(tick_size=0.001, tick_value=1.0, volume_min=0.01,
                   volume_max=100.0, volume_step=0.01, spread_points=20,
                   digits=3, point=0.001),
    "BAC": spec(tick_size=0.01, tick_value=1.0, volume_min=0.01,
                volume_max=1000.0, volume_step=0.01, spread_points=5,
                digits=2, point=0.01),
    "XPTUSD": spec(tick_size=0.001, tick_value=1.0, volume_min=0.01,
                   volume_max=100.0, volume_step=0.01, spread_points=30,
                   digits=3, point=0.001),
}


def make_sandbox(tmp_path, *, trading_enabled=True, dry_run=True):
    files = tmp_path / "files"
    state = tmp_path / "run"
    files.mkdir()
    state.mkdir()
    now = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    specs = {
        "time": now,
        "account": {"equity": 1_000_000.0, "balance": 1_000_000.0,
                    "currency": "USD", "server": "MetaQuotes-Demo",
                    "time": now},
        "symbols": SANDBOX_SPECS,
    }
    (files / "nova_symbol_specs.json").write_text(json.dumps(specs))
    (files / "nova_signals.jsonl").write_text(
        "\n".join(json.dumps(s) for s in FIXTURE_SIGNALS) + "\n")
    (state / "risk_config.json").write_text(json.dumps({
        "trading_enabled": trading_enabled, "dry_run": dry_run,
        "risk_per_trade_pct": 0.5, "max_concurrent_trades": 3,
        "max_daily_loss_pct": 2.0, "max_spread_points": 50,
        "capital_basis": 200000,
    }))
    (state / "trading_enabled").write_text("1")
    return str(files), str(state)


def read_journal(state_dir):
    p = os.path.join(state_dir, "nova_journal.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


# ---------------------------------------------------------------- sizing

def test_compute_volume_exact():
    v, risk, err = te.compute_volume(
        20_000.0, 0.5, 5.0, 4.0,
        spec(tick_size=0.5, tick_value=2.0, volume_min=0.5,
             volume_max=100.0, volume_step=0.5))
    assert err is None
    assert risk == 100.0
    assert v == 25.0  # 100 / ((1.0/0.5) * 2.0) = 25, step 0.5


def test_compute_volume_floors_to_step():
    v, risk, err = te.compute_volume(
        20_000.0, 0.5, 5.0, 4.0,
        spec(tick_size=0.6, tick_value=2.0, volume_min=0.1,
             volume_max=100.0, volume_step=0.1))
    # raw = 100 / ((1.0/0.6) * 2.0) = 30.0 -> floor to step 0.1 -> 30.0
    assert err is None
    assert v == pytest.approx(30.0)


def test_compute_volume_below_min():
    v, risk, err = te.compute_volume(
        100.0, 0.5, 1310.0, 1300.0,
        spec(tick_size=0.001, tick_value=1.0, volume_min=0.01,
             volume_max=100.0, volume_step=0.01))
    assert err == "skipped:volume_below_min"
    assert v == 0.0


def test_compute_volume_bad_specs_tick_value_zero():
    _, _, err = te.compute_volume(
        10_000.0, 0.5, 1.1, 1.0, spec(tick_value=0.0))
    assert err == "skipped:bad_specs"


def test_compute_volume_bad_specs_tick_size_zero():
    _, _, err = te.compute_volume(
        10_000.0, 0.5, 1.1, 1.0, spec(tick_size=0.0))
    assert err == "skipped:bad_specs"


def test_compute_volume_bad_specs_no_sl_distance():
    _, _, err = te.compute_volume(
        10_000.0, 0.5, 1.1, 1.1, spec())
    assert err == "skipped:bad_specs"


def test_compute_volume_bad_specs_no_step():
    _, _, err = te.compute_volume(
        10_000.0, 0.5, 1.1, 1.0, spec(volume_step=0.0))
    assert err == "skipped:bad_specs"


def test_compute_volume_clamps_to_max():
    v, _, err = te.compute_volume(
        1_000_000.0, 0.5, 10.0, 9.99,
        spec(tick_size=0.01, tick_value=1.0, volume_min=0.01,
             volume_max=10.0, volume_step=0.01))
    assert err is None
    assert v == 10.0


# ---------------------------------------------------------------- gates

def test_gate_trading_disabled():
    d, _ = te.check_gates(sig(), **gate_ctx(enabled=False, dry_run=True))
    assert d == "skipped:trading_disabled"


def test_gate_stale_specs():
    d, _ = te.check_gates(sig(), **gate_ctx(specs_fresh=False))
    assert d == "skipped:stale_specs"


def test_gate_specs_missing():
    d, _ = te.check_gates(sig(), **gate_ctx(specs=None, specs_fresh=False))
    assert d == "skipped:stale_specs"


def test_gate_symbol_not_in_specs():
    d, _ = te.check_gates(sig(symbol="NOPE"),
                          **gate_ctx(specs={"symbols": {"XPDUSD": spec()}}))
    assert d == "skipped:stale_specs"


def test_gate_non_demo_server():
    specs = {"symbols": {"XPDUSD": spec()},
             "account": {"server": "MetaQuotes-Live", "equity": 1e6}}
    d, flags = te.check_gates(sig(), **gate_ctx(specs=specs))
    assert d == "skipped:non_demo_server"
    assert flags.get("loud") is True


def test_gate_demo_server_passes():
    specs = {"symbols": {"XPDUSD": spec()},
             "account": {"server": "MetaQuotes-Demo", "equity": 1e6}}
    d, _ = te.check_gates(sig(), **gate_ctx(specs=specs))
    assert d == "commanded"


def test_gate_max_concurrent():
    open_pos = {i: {"symbol": f"S{i}"} for i in range(3)}
    d, _ = te.check_gates(sig(), **gate_ctx(open_positions=open_pos))
    assert d == "skipped:max_concurrent"


def test_gate_symbol_already_open():
    open_pos = {777: {"symbol": "XPDUSD", "command_id": "cmd-x"}}
    d, _ = te.check_gates(sig(), **gate_ctx(open_positions=open_pos))
    assert d == "skipped:symbol_already_open"


def test_gate_spread_too_wide():
    # tight stop (100 pts): effective cap = max(50, 0.5*100) = 50
    specs = {"symbols": {"XPDUSD": spec(spread_points=51)},
             "account": {"server": "MetaQuotes-Demo", "equity": 1e6}}
    d, _ = te.check_gates(sig(stop_loss=1310.814), **gate_ctx(specs=specs))
    assert d == "skipped:spread_too_wide"


def test_gate_spread_at_limit_passes():
    specs = {"symbols": {"XPDUSD": spec(spread_points=50)},
             "account": {"server": "MetaQuotes-Demo", "equity": 1e6}}
    d, _ = te.check_gates(sig(), **gate_ctx(specs=specs))
    assert d == "commanded"


def test_gate_spread_fixed_cap_blocks_wide_spread():
    # User-approved behavior (restored 2026-09-21): the spread cap is the
    # FIXED max_spread_points (default 50). A 2000-point spread is rejected
    # even though the stop is wide -- the old proportional loosening was
    # never approved and is removed.
    specs = {"symbols": {"XPDUSD": spec(spread_points=2000)},
             "account": {"server": "MetaQuotes-Demo", "equity": 1e6}}
    d, _ = te.check_gates(sig(), **gate_ctx(specs=specs))
    assert d == "skipped:spread_too_wide"


def test_gate_spread_fixed_cap_allows_within_cap():
    # spread 40 <= 50 -> passes the spread gate (other gates decide).
    specs = {"symbols": {"XPDUSD": spec(spread_points=40)},
             "account": {"server": "MetaQuotes-Demo", "equity": 1e6}}
    d, _ = te.check_gates(sig(), **gate_ctx(specs=specs))
    assert d == "commanded"


def test_gate_spread_fixed_cap_blocks():
    # spread 2500 > fixed cap 50 -> skipped.
    specs = {"symbols": {"XPDUSD": spec(spread_points=2500)},
             "account": {"server": "MetaQuotes-Demo", "equity": 1e6}}
    d, _ = te.check_gates(sig(), **gate_ctx(specs=specs))
    assert d == "skipped:spread_too_wide"


def test_gate_exposure_clamps_volume():
    # computed volume for XPDUSD >> 1.0 lot; with 0.3 open, new trade
    # clamps to 0.7 lots and still commands.
    d, flags = te.check_gates(sig(), **gate_ctx(open_lots=0.3))
    assert d == "commanded"
    assert abs(flags["volume"] - 0.7) < 1e-9


def test_gate_exposure_blocks_when_full():
    d, flags = te.check_gates(sig(), **gate_ctx(open_lots=1.0))
    assert d == "skipped:max_total_exposure"
    assert flags.get("loud") is True


def test_gate_consecutive_losses_blocks():
    d, flags = te.check_gates(sig(), **gate_ctx(consecutive_losses=2))
    assert d == "skipped:consecutive_losses"
    assert flags.get("loud") is True


def test_gate_consecutive_losses_below_limit_passes():
    d, _ = te.check_gates(sig(), **gate_ctx(consecutive_losses=1))
    assert d == "commanded"


def test_gate_correlated_position_blocks():
    # EURUSD open; GBPUSD shares USD -> blocked
    pos = {111: {"symbol": "EURUSD", "command_id": "c1", "signal_id": "s1"}}
    s = sig(symbol="GBPUSD")
    ctx = gate_ctx(open_positions=pos)
    ctx["specs"] = {"symbols": {"GBPUSD": spec()},
                    "account": {"server": "MetaQuotes-Demo",
                                "equity": 1_000_000.0, "balance": 1_000_000.0}}
    d, flags = te.check_gates(s, **ctx)
    assert d == "skipped:correlated_position"


def test_gate_uncorrelated_symbol_passes():
    # EURUSD open; AUDJPY shares nothing -> trades
    pos = {111: {"symbol": "EURUSD", "command_id": "c1", "signal_id": "s1"}}
    s = sig(symbol="AUDJPY")
    ctx = gate_ctx(open_positions=pos)
    ctx["specs"] = {"symbols": {"AUDJPY": spec()},
                    "account": {"server": "MetaQuotes-Demo",
                                "equity": 1_000_000.0, "balance": 1_000_000.0}}
    d, _ = te.check_gates(s, **ctx)
    assert d == "commanded"


def test_gate_correlated_opposite_direction_allows():
    # CHFJPY BUY is short-JPY; NZDJPY SELL is long-JPY -> they net out
    # on JPY instead of stacking, so the gate must not block.
    pos = {111: {"symbol": "CHFJPY", "direction": "BUY",
                 "command_id": "c1", "signal_id": "s1"}}
    s = sig(symbol="NZDJPY", direction="SELL", entry_price=90.092,
            stop_loss=90.184, take_profit=89.909)
    ctx = gate_ctx(open_positions=pos)
    ctx["specs"] = {"symbols": {"NZDJPY": spec()},
                    "account": {"server": "MetaQuotes-Demo",
                                "equity": 1_000_000.0, "balance": 1_000_000.0}}
    d, _ = te.check_gates(s, **ctx)
    assert d == "commanded"


def test_gate_correlated_same_direction_blocks():
    # EURUSD BUY + GBPUSD BUY both short USD -> stacking, still blocked.
    pos = {111: {"symbol": "EURUSD", "direction": "BUY",
                 "command_id": "c1", "signal_id": "s1"}}
    s = sig(symbol="GBPUSD", direction="BUY")
    ctx = gate_ctx(open_positions=pos)
    ctx["specs"] = {"symbols": {"GBPUSD": spec()},
                    "account": {"server": "MetaQuotes-Demo",
                                "equity": 1_000_000.0, "balance": 1_000_000.0}}
    d, _ = te.check_gates(s, **ctx)
    assert d == "skipped:correlated_position"


def test_gate_correlated_opposite_usd_allows():
    # EURUSD BUY (short USD) + GBPUSD SELL (long USD) net out -> allowed.
    pos = {111: {"symbol": "EURUSD", "direction": "BUY",
                 "command_id": "c1", "signal_id": "s1"}}
    s = sig(symbol="GBPUSD", direction="SELL")
    ctx = gate_ctx(open_positions=pos)
    ctx["specs"] = {"symbols": {"GBPUSD": spec()},
                    "account": {"server": "MetaQuotes-Demo",
                                "equity": 1_000_000.0, "balance": 1_000_000.0}}
    d, _ = te.check_gates(s, **ctx)
    assert d == "commanded"


def test_gate_correlated_unknown_direction_fails_closed():
    # EURUSD open with unknown direction; GBPUSD BUY -> assumed to stack.
    pos = {111: {"symbol": "EURUSD", "command_id": "c1", "signal_id": "s1"}}
    s = sig(symbol="GBPUSD", direction="BUY")
    ctx = gate_ctx(open_positions=pos)
    ctx["specs"] = {"symbols": {"GBPUSD": spec()},
                    "account": {"server": "MetaQuotes-Demo",
                                "equity": 1_000_000.0, "balance": 1_000_000.0}}
    d, _ = te.check_gates(s, **ctx)
    assert d == "skipped:correlated_position"


def test_gate_missing_sl_refused():
    s = sig(stop_loss=0)
    d, flags = te.check_gates(s, **gate_ctx())
    assert d == "skipped:missing_sl_tp"
    assert flags.get("loud") is True


def test_consecutive_losses_counts_trailing():
    import tempfile, os
    lines = [
        {"type": "trade.closed", "ticket": 1, "profit": 100.0,
         "time": "2026.09.21 10:00:00"},
        {"type": "trade.closed", "ticket": 2, "profit": -50.0,
         "time": "2026.09.21 11:00:00"},
        {"type": "trade.closed", "ticket": 3, "profit": -20.0,
         "time": "2026.09.21 12:00:00"},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                     delete=False) as f:
        for e in lines:
            f.write(json.dumps(e) + "\n")
        path = f.name
    try:
        _, _, consec = te.load_trade_state(path, "2026-09-21")
        assert consec == 2
    finally:
        os.unlink(path)


def test_consecutive_losses_reset_by_win():
    import tempfile, os
    lines = [
        {"type": "trade.closed", "ticket": 1, "profit": -50.0,
         "time": "2026.09.21 11:00:00"},
        {"type": "trade.closed", "ticket": 2, "profit": 30.0,
         "time": "2026.09.21 12:00:00"},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                     delete=False) as f:
        for e in lines:
            f.write(json.dumps(e) + "\n")
        path = f.name
    try:
        _, _, consec = te.load_trade_state(path, "2026-09-21")
        assert consec == 0
    finally:
        os.unlink(path)


def test_gate_floating_drawdown_blocks():
    # balance 1e6, equity 989000 -> floating -11000 = 1.1% -> block new entries
    d, flags = te.check_gates(sig(), **gate_ctx(balance=1_000_000.0,
                                                equity=989_000.0))
    assert d == "skipped:floating_drawdown"
    assert flags.get("loud") is True


def test_gate_floating_drawdown_below_line_passes():
    # floating -9000 = 0.9% < 1.0% -> trades
    d, _ = te.check_gates(sig(), **gate_ctx(balance=1_000_000.0,
                                            equity=991_000.0))
    assert d == "commanded"


def test_gate_floating_drawdown_ignores_profit():
    # equity above balance (floating profit) -> trades
    d, _ = te.check_gates(sig(), **gate_ctx(balance=1_000_000.0,
                                            equity=1_005_000.0))
    assert d == "commanded"


def test_gate_daily_loss_limit():
    # equity 1e6, limit 2% = 20000; today's loss 20001 -> trip
    d, flags = te.check_gates(sig(), **gate_ctx(todays_profit=-20001.0))
    assert d == "skipped:daily_loss_limit"
    assert flags.get("trip") is True
    assert flags.get("loud") is True


def test_gate_daily_loss_just_under_limit_passes():
    d, _ = te.check_gates(sig(), **gate_ctx(todays_profit=-19999.0))
    assert d == "commanded"


def test_gate_dry_run_intended_with_details():
    d, flags = te.check_gates(sig(), **gate_ctx(dry_run=True))
    assert d == "intended"
    assert flags["volume"] > 0
    assert flags["risk_amount"] == pytest.approx(10000.0)


def test_gate_dry_run_disabled_wins():
    d, _ = te.check_gates(sig(), **gate_ctx(enabled=False, dry_run=True))
    assert d == "skipped:trading_disabled"


def test_gate_volume_below_min():
    d, _ = te.check_gates(
        sig(entry_price=1310.0, stop_loss=1300.0),
        **gate_ctx(equity=100.0, risk_basis=100.0,
                   specs={"symbols": {"XPDUSD": spec()}}))
    assert d == "skipped:volume_below_min"


def test_trip_kill_switch_writes_zero(tmp_path):
    p = str(tmp_path / "trading_enabled")
    open(p, "w").write("1")
    te.trip_kill_switch(p)
    assert open(p).read() == "0"


# ---------------------------------------------------------------- command schema

def test_build_command_schema():
    cmd = te.build_command(FIXTURE_SIGNALS[0], 1.09)
    assert set(cmd) == {"type", "id", "symbol", "direction",
                        "volume", "sl", "tp", "signal_id"}
    assert cmd["type"] == "trade.open"
    assert isinstance(cmd["id"], str) and cmd["id"].startswith("cmd-")
    assert isinstance(cmd["symbol"], str)
    assert isinstance(cmd["direction"], str)
    assert isinstance(cmd["volume"], float)
    assert isinstance(cmd["sl"], (int, float))
    assert isinstance(cmd["tp"], (int, float))
    assert isinstance(cmd["signal_id"], str)
    assert cmd["sl"] == FIXTURE_SIGNALS[0]["stop_loss"]
    assert cmd["tp"] == FIXTURE_SIGNALS[0]["take_profit"]


# ---------------------------------------------------------------- config safety

def test_config_created_with_safe_defaults(tmp_path):
    cfg_path = str(tmp_path / "risk_config.json")
    cfg = te.load_config(cfg_path)
    assert os.path.exists(cfg_path)
    assert cfg["trading_enabled"] is False
    assert cfg["dry_run"] is True
    on_disk = json.load(open(cfg_path))
    assert on_disk["trading_enabled"] is False
    assert on_disk["dry_run"] is True


def test_kill_switch_missing_means_off(tmp_path):
    assert te.kill_switch_on(str(tmp_path / "nope")) is False


# ---------------------------------------------------------------- dry-run integration

def test_dry_run_logs_three_intended_writes_zero_commands(tmp_path):
    files, state = make_sandbox(tmp_path, trading_enabled=True, dry_run=True)
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    decisions = eng.run_once()
    assert decisions == ["intended", "intended", "intended"]
    journal = read_journal(state)
    decs = [e for e in journal if e.get("type") == "trade.decision"]
    assert len(decs) == 3
    assert all(e["decision"] == "intended" for e in decs)
    assert {e["signal_id"] for e in decs} == {s["id"] for s in FIXTURE_SIGNALS}
    for e in decs:
        assert e["volume"] and e["volume"] > 0
        assert e["risk_amount"] == pytest.approx(1000.0)  # 0.5% of $200,000
    cmd_p = os.path.join(files, "nova_commands.jsonl")
    assert os.path.getsize(cmd_p) == 0  # exists (startup touch), zero commands written


def test_dry_run_is_idempotent_on_rerun(tmp_path):
    files, state = make_sandbox(tmp_path, trading_enabled=True, dry_run=True)
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    eng.run_once()
    eng.run_once()
    decs = [e for e in read_journal(state)
            if e.get("type") == "trade.decision"]
    assert len(decs) == 3  # no duplicates on second pass


# ---------------------------------------------------------------- journal linkage

def test_journal_linkage_opened_and_closed(tmp_path):
    files = tmp_path / "files"
    state = tmp_path / "run"
    files.mkdir()
    state.mkdir()
    (files / "nova_trades.jsonl").write_text("\n".join([
        json.dumps({"type": "trade.opened", "command_id": "cmd-S1",
                    "ticket": 12345, "deal": 99, "fill_price": 1310.9,
                    "time": "2026.09.21 22:15:00", "signal_id": "S1"}),
        json.dumps({"type": "trade.closed", "ticket": 12345,
                    "exit_price": 1320.0, "profit": 915.0, "reason": "tp",
                    "time": "2026.09.21 23:00:00"}),
    ]) + "\n")
    eng = te.TraderEngine(files_dir=str(files), state_dir=str(state))
    assert eng.tail_trades() == 2
    ups = [e for e in read_journal(str(state))
           if e.get("type") == "trade.update"]
    assert len(ups) == 2
    opened, closed = ups
    assert opened["command_id"] == "cmd-S1"
    assert opened["ticket"] == 12345
    assert opened["fill_price"] == 1310.9
    assert closed["ticket"] == 12345
    assert closed["profit"] == 915.0
    assert closed["reason"] == "tp"


def test_journal_linkage_rejected(tmp_path):
    files = tmp_path / "files"
    state = tmp_path / "run"
    files.mkdir()
    state.mkdir()
    (files / "nova_trades.jsonl").write_text(
        json.dumps({"type": "trade.rejected", "command_id": "cmd-S9",
                    "reason": "invalid volume",
                    "time": "2026.09.21 22:16:00"}) + "\n")
    eng = te.TraderEngine(files_dir=str(files), state_dir=str(state))
    eng.tail_trades()
    ups = [e for e in read_journal(str(state))
           if e.get("type") == "trade.update"]
    assert len(ups) == 1
    assert ups[0]["command_id"] == "cmd-S9"
    assert ups[0]["rejected"] == "invalid volume"


# ---------------------------------------------------------------- live-mode side effects

def test_daily_loss_limit_trips_kill_switch_live(tmp_path):
    files, state = make_sandbox(tmp_path, trading_enabled=True, dry_run=False)
    today = datetime.now().strftime("%Y.%m.%d")
    (os.path.join(files, "nova_trades.jsonl"))
    with open(os.path.join(files, "nova_trades.jsonl"), "w") as f:
        f.write(json.dumps({
            "type": "trade.closed", "ticket": 1, "exit_price": 1.0,
            "profit": -20001.0, "reason": "sl",
            "time": f"{today} 10:00:00"}) + "\n")
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    decisions = eng.run_once()
    assert decisions[0] == "skipped:daily_loss_limit"
    with open(os.path.join(state, "trading_enabled")) as f:
        assert f.read() == "0"
    cmd_p = os.path.join(files, "nova_commands.jsonl")
    assert os.path.getsize(cmd_p) == 0  # exists (startup touch), zero commands written
    decs = [e for e in read_journal(state)
            if e.get("type") == "trade.decision"]
    assert decs[0]["decision"] == "skipped:daily_loss_limit"


def test_live_mode_writes_command_and_journals_commanded(tmp_path):
    files, state = make_sandbox(tmp_path, trading_enabled=True, dry_run=False)
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    decisions = eng.run_once()
    assert decisions == ["commanded", "commanded", "commanded"]
    cmds = [json.loads(l) for l in
            open(os.path.join(files, "nova_commands.jsonl")) if l.strip()]
    assert len(cmds) == 3
    assert all(c["type"] == "trade.open" for c in cmds)
    assert {c["id"] for c in cmds} == {f"cmd-{s['id']}" for s in FIXTURE_SIGNALS}
    decs = [e for e in read_journal(state)
            if e.get("type") == "trade.decision"]
    assert all(e["decision"] == "commanded" for e in decs)
    assert all(e.get("command_id") for e in decs)


def test_load_trade_state_open_minus_closed(tmp_path):
    p = tmp_path / "nova_trades.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "trade.opened", "command_id": "c1",
                    "ticket": 1, "signal_id": "S1", "symbol": "XPDUSD"}),
        json.dumps({"type": "trade.opened", "command_id": "c2",
                    "ticket": 2, "signal_id": "S2", "symbol": "BAC"}),
        json.dumps({"type": "trade.closed", "ticket": 1, "profit": 10.0,
                    "time": "2026.09.21 10:00:00"}),
        json.dumps({"type": "trade.closed", "ticket": 9, "profit": -5.0,
                    "time": "2020.01.01 10:00:00"}),
    ]))
    open_pos, profit, _consec = te.load_trade_state(str(p), "2026-09-21")
    assert set(open_pos) == {2}
    assert open_pos[2]["symbol"] == "BAC"
    assert profit == pytest.approx(10.0)  # only today's close counts


def test_specs_stale_after_300s(tmp_path):
    files, state = make_sandbox(tmp_path, trading_enabled=True, dry_run=True)
    old = (datetime.now() - timedelta(seconds=400)).strftime("%Y.%m.%d %H:%M:%S")
    specs = json.load(open(os.path.join(files, "nova_symbol_specs.json")))
    specs["time"] = old
    json.dump(specs, open(os.path.join(files, "nova_symbol_specs.json"), "w"))
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    decisions = eng.run_once()
    assert decisions == ["skipped:stale_specs"] * 3


# ------------------------------------------------- repo ports (2026-09-21)

def test_broker_positions_fresh(tmp_path):
    import time
    fd = str(tmp_path)
    payload = {"time": 0, "positions": [
        {"ticket": 1, "symbol": "EURUSD", "volume": 11.0, "type": "BUY",
         "magic": 0, "profit": -100.0}]}
    p = os.path.join(fd, "nova_positions.json")
    json.dump(payload, open(p, "w"))
    os.utime(p, (time.time(), time.time()))
    positions, fresh = te.load_broker_positions(fd)
    assert fresh is True
    assert len(positions) == 1
    assert positions[0]["symbol"] == "EURUSD"


def test_broker_positions_stale(tmp_path):
    import time
    fd = str(tmp_path)
    p = os.path.join(fd, "nova_positions.json")
    json.dump({"positions": []}, open(p, "w"))
    old = time.time() - 3600
    os.utime(p, (old, old))
    positions, fresh = te.load_broker_positions(fd)
    assert fresh is False
    assert positions == []


def test_broker_positions_missing(tmp_path):
    positions, fresh = te.load_broker_positions(str(tmp_path))
    assert fresh is False
    assert positions == []


def test_tick_age_fresh():
    feed = {"server_time": "2026.09.22 02:55:00",
            "symbols": {"XPDUSD": {"time": "2026.09.22 02:54:30"}}}
    assert te.feed_tick_age_s(feed, "XPDUSD") == 30.0


def test_tick_age_stale():
    feed = {"server_time": "2026.09.22 02:55:00",
            "symbols": {"XPDUSD": {"time": "2026.09.22 02:50:00"}}}
    assert te.feed_tick_age_s(feed, "XPDUSD") == 300.0


def test_tick_age_unknown_symbol():
    feed = {"server_time": "2026.09.22 02:55:00", "symbols": {}}
    assert te.feed_tick_age_s(feed, "XPDUSD") is None


def test_gate_stale_tick_skips():
    d, flags = te.check_gates(sig(), **gate_ctx(tick_age_s=300.0))
    assert d == "skipped:stale_tick"
    assert flags.get("loud") is True


def test_gate_fresh_tick_passes():
    d, _ = te.check_gates(sig(), **gate_ctx(tick_age_s=5.0))
    assert d == "commanded"


def test_gate_broker_wide_concurrent_counts_manual():
    # 3 manual broker positions (not in the bot journal) hit the
    # max_concurrent=3 cap -- the old journal-only blind spot is gone.
    open_positions = {
        101: {"symbol": "EURUSD", "_broker": True, "manual": True},
        102: {"symbol": "EURUSD", "_broker": True, "manual": True},
        103: {"symbol": "EURUSD", "_broker": True, "manual": True},
    }
    d, _ = te.check_gates(sig(symbol="XPDUSD"), **gate_ctx(
        open_positions=open_positions, max_concurrent=3))
    assert d == "skipped:max_concurrent"


def test_gate_broker_wide_exposure_counts_manual():
    # 55 lots of manual volume exhaust the 1.0-lot exposure cap.
    d, flags = te.check_gates(sig(), **gate_ctx(open_lots=55.0))
    assert d == "skipped:max_total_exposure"
    assert flags.get("loud") is True


def test_gate_broker_wide_correlation_sees_manual():
    # manual EURUSD position correlates (shared USD) with a new GBPUSD
    # signal -- different symbol, so symbol_already_open does not fire.
    open_positions = {
        101: {"symbol": "EURUSD", "_broker": True, "manual": True},
    }
    specs = {"symbols": {"GBPUSD": spec(), "XPDUSD": spec()},
             "account": {"server": "MetaQuotes-Demo", "equity": 1e6}}
    d, _ = te.check_gates(sig(symbol="GBPUSD", entry_price=1.35,
                              stop_loss=1.34, take_profit=1.37),
                          **gate_ctx(specs=specs,
                                     open_positions=open_positions,
                                     max_correlated_positions=1))
    assert d == "skipped:correlated_position"


def test_audit_safety_writes(tmp_path):
    run = str(tmp_path)
    te.audit_safety(run, "kill_switch.tripped", "system", {"reason": "test"})
    lines = open(os.path.join(run, "safety_audit.jsonl")).read().strip().split("\n")
    e = json.loads(lines[0])
    assert e["type"] == "safety.audit"
    assert e["event"] == "kill_switch.tripped"
    assert e["actor"] == "system"
    assert e["details"]["reason"] == "test"
    assert "time" in e


# --------------------------------------------------------------- capital_basis
# Owner order 2026-09-22: $200,000 allocated trading capital. Sizing and the
# daily-loss gate run off the allocated basis instead of broker equity.

def test_effective_risk_basis_uses_config_value():
    assert te.effective_risk_basis({"capital_basis": 200000}, 1_258_148.44) == 200000


def test_effective_risk_basis_clamped_to_account_equity():
    # basis can never exceed what the account actually holds
    assert te.effective_risk_basis({"capital_basis": 5_000_000}, 1_000_000) == 1_000_000


def test_effective_risk_basis_zero_unavailable():
    # FAIL-CLOSED (2026-09-22): zero/missing basis -> None, never equity.
    assert te.effective_risk_basis({"capital_basis": 0}, 1_000_000) is None
    assert te.effective_risk_basis({}, 1_000_000) is None


def test_effective_risk_basis_invalid_unavailable():
    assert te.effective_risk_basis({"capital_basis": -100}, 1_000_000) is None
    assert te.effective_risk_basis({"capital_basis": "bogus"},
                                   1_000_000) is None
    assert te.effective_risk_basis(None, 1_000_000) is None


def test_apply_risk_bounds_capital_basis_validation():
    cfg, notes = te.apply_risk_bounds({"capital_basis": -5})
    assert cfg["capital_basis"] == 0
    assert any("capital_basis" in n for n in notes)
    cfg, notes = te.apply_risk_bounds({"capital_basis": "200000"})
    assert cfg["capital_basis"] == 200000.0
    assert notes == [] or not any("capital_basis" in n for n in notes)


def test_gate_sizing_uses_capital_basis():
    # 0.5% of $200,000 = $1,000 planned risk (owner order 2026-09-22)
    d, flags = te.check_gates(sig(), **gate_ctx(risk_basis=200000.0,
                                               risk_pct=0.5))
    assert d == "commanded"
    assert flags["risk_amount"] == pytest.approx(1000.0)


def test_gate_sizing_zero_basis_fails_closed():
    # FAIL-CLOSED (2026-09-22): zero basis is unavailable, not equity.
    d, flags = te.check_gates(sig(), **gate_ctx(risk_basis=0))
    assert d == "skipped:risk_basis_unavailable"
    assert flags.get("loud") is True


def test_gate_sizing_invalid_basis_fails_closed():
    d, _ = te.check_gates(sig(), **gate_ctx(risk_basis="bogus"))
    assert d == "skipped:risk_basis_unavailable"
    d, _ = te.check_gates(sig(), **gate_ctx(risk_basis=-500))
    assert d == "skipped:risk_basis_unavailable"
    d, _ = te.check_gates(sig(), **gate_ctx(risk_basis=float("nan")))
    assert d == "skipped:risk_basis_unavailable"


def test_gate_daily_loss_limit_uses_capital_basis():
    # live config: 3% of $200,000 = $6,000 daily-loss limit
    d, flags = te.check_gates(
        sig(), **gate_ctx(risk_basis=200000.0, max_daily_loss_pct=3.0,
                          todays_profit=-6001.0))
    assert d == "skipped:daily_loss_limit"
    assert flags.get("trip") is True
    # same loss against a $1,000,000 basis would NOT trip (3% = $30,000)
    d, _ = te.check_gates(
        sig(), **gate_ctx(risk_basis=1_000_000.0, max_daily_loss_pct=3.0,
                          todays_profit=-6001.0))
    assert d == "commanded"
