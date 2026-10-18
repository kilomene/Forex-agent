"""In-flight slot reservation for the max-concurrent gate (2026-10-16).

Regression tests for the same-batch race that tripped the sentinel halt:
six trade.open commands were emitted inside the command->fill latency
window, the gate counted only confirmed positions, and the broker ended up
with 12 open positions against a cap of 10.

The fix: trade.open commands still in a pending idempotency state
(proposed/sent/unknown) hold a slot against max_concurrent. Hermetic:
tmp_path only, no MT5, no network.
"""
import os
import sys
from datetime import datetime, timezone

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
        today_str="2026-10-16",
        risk_pct=0.5,
        max_concurrent=3,
        max_spread_points=50,
        max_daily_loss_pct=3.0,
        equity=1_000_000.0,
        balance=1_000_000.0,
        max_total_exposure_lots=0,
        open_lots=0.0,
        max_consecutive_losses=0,
        consecutive_losses=0,
        max_correlated_positions=0,
        require_stop_loss=True,
        risk_basis=200_000.0,
        market_open=True,
        tick_age_s=1.0,
        enforce_correlation_gate=False,
        enforce_spread_gate=False,
        enforce_single_position_per_symbol=False,
        enforce_consecutive_losses_gate=False,
    )
    ctx.update(over)
    return ctx


def sig(**over):
    s = {
        "id": "XPDUSD_M15_BUY_1792126800",
        "type": "signal.detected",
        "symbol": "XPDUSD",
        "timeframe": "M15",
        "direction": "BUY",
        "entry_price": 1310.914,
        "stop_loss": 1306.344,
        "take_profit": 1320.054,
        "candle_time": datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S"),
    }
    s.update(over)
    return s


def live_cfg(**over):
    cfg = dict(
        trading_enabled=True,
        dry_run=False,
        risk_per_trade_pct=0.5,
        max_concurrent_trades=10,
        max_daily_loss_pct=3.0,
        max_spread_points=50,
        max_correlated_positions=0,
        max_consecutive_losses=0,
        max_total_exposure_lots=0,
        require_stop_loss=True,
        capital_basis=200000,
        max_signal_age_hours=2.0,
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


def make_engine(tmp_path):
    files = tmp_path / "files"
    state = tmp_path / "run"
    files.mkdir()
    state.mkdir()
    return te.TraderEngine(files_dir=str(files), state_dir=str(state))


def claim_sent_open(eng, tag, symbol="XPDUSD"):
    """Simulate an emitted trade.open awaiting broker confirmation."""
    key = f"idem-key-{tag}"
    eng.idem.check_or_claim(key, {
        "command_id": f"cmd-{tag}",
        "cmd_type": "trade.open",
        "signal_id": f"sig-{tag}",
        "symbol": symbol,
        "direction": "BUY",
        "status": "proposed",
    })
    eng.idem.update(key, status="sent",
                    sent_at=datetime.now(timezone.utc).isoformat())
    return key


# ------------------------------------------------- pure gate behavior ----

def test_gate_in_flight_holds_slot():
    # 2 confirmed + 1 in-flight against a cap of 3 -> the next signal
    # must be refused. This is the exact shape of the 2026-10-16 race.
    open_pos = {f"t{i}": {"symbol": f"S{i}"} for i in range(2)}
    d, flags = te.check_gates(sig(), **gate_ctx(open_positions=open_pos,
                                                in_flight_count=1))
    assert d == "skipped:max_concurrent"
    assert flags["in_flight"] == 1


def test_gate_in_flight_zero_is_legacy_behavior():
    # No kwarg (default 0) and an explicit 0 both preserve the old
    # behavior: 2 confirmed against a cap of 3 passes.
    open_pos = {f"t{i}": {"symbol": f"S{i}"} for i in range(2)}
    d, _ = te.check_gates(sig(), **gate_ctx(open_positions=open_pos))
    assert d == "commanded"
    d, _ = te.check_gates(sig(), **gate_ctx(open_positions=open_pos,
                                            in_flight_count=0))
    assert d == "commanded"


def test_gate_in_flight_overshoot_replay_at_cap_10():
    # Incident shape at the real cap: 9 confirmed + 2 in-flight -> skip;
    # 8 confirmed + 2 in-flight -> pass (9 < 10).
    open9 = {f"t{i}": {"symbol": f"S{i}"} for i in range(9)}
    d, flags = te.check_gates(sig(), **gate_ctx(open_positions=open9,
                                                max_concurrent=10,
                                                in_flight_count=2))
    assert d == "skipped:max_concurrent"
    assert flags["in_flight"] == 2
    open8 = {f"t{i}": {"symbol": f"S{i}"} for i in range(8)}
    d, _ = te.check_gates(sig(), **gate_ctx(open_positions=open8,
                                            max_concurrent=10,
                                            in_flight_count=1))
    assert d == "commanded"


# ------------------------------------------------- idem store counting ---

def test_engine_in_flight_count_lifecycle(tmp_path):
    eng = make_engine(tmp_path)
    assert eng._in_flight_open_count() == 0
    k1 = claim_sent_open(eng, "a")
    k2 = claim_sent_open(eng, "b", symbol="EURUSD")
    assert eng._in_flight_open_count() == 2
    # A fill hands the slot to open_positions -> no longer in-flight.
    eng.idem.update(k1, status="filled", broker_ticket="123")
    assert eng._in_flight_open_count() == 1
    # A broker rejection frees the slot immediately.
    eng.idem.update(k2, status="failed", fail_reason="test")
    assert eng._in_flight_open_count() == 0
    # trade.close commands never hold an open slot.
    kc = "idem-key-close"
    eng.idem.check_or_claim(kc, {"command_id": "cmd-close-1",
                                 "cmd_type": "trade.close",
                                 "status": "proposed"})
    eng.idem.update(kc, status="sent")
    assert eng._in_flight_open_count() == 0


def test_engine_in_flight_survives_unknown(tmp_path):
    # EXECUTION_UNKNOWN may still have filled -> keep counting it
    # (fail-closed on capacity) until the reconciler resolves it.
    eng = make_engine(tmp_path)
    k = claim_sent_open(eng, "u")
    eng.idem.update(k, status="unknown")
    assert eng._in_flight_open_count() == 1


# ------------------------------------------------- decide() wiring -------

def test_decide_blocks_on_live_in_flight(tmp_path):
    eng = make_engine(tmp_path)
    claim_sent_open(eng, "live-1")
    open9 = {f"t{i}": {"symbol": f"S{i}"} for i in range(9)}
    d, flags = eng.decide(sig(), live_cfg(), True, live_specs(), True,
                          open9, 0.0, "2026-10-16")
    assert d == "skipped:max_concurrent"
    assert flags["in_flight_opens"] == 1


def test_decide_passes_when_fill_confirmed(tmp_path):
    eng = make_engine(tmp_path)
    k = claim_sent_open(eng, "live-2")
    eng.idem.update(k, status="filled", broker_ticket="999")
    open9 = {f"t{i}": {"symbol": f"S{i}"} for i in range(9)}
    d, flags = eng.decide(sig(), live_cfg(), True, live_specs(), True,
                          open9, 0.0, "2026-10-16")
    assert d == "commanded"
    assert flags["in_flight_opens"] == 0


def test_sequential_batch_stops_at_cap(tmp_path):
    """Replay the incident's signal burst through decide(): with 4
    confirmed positions and a cap of 10, the first 6 signals command
    (4+0 .. 4+5) and the 7th is refused (4+6 >= 10). Under the old
    gate (no in-flight count) every one of them would command."""
    eng = make_engine(tmp_path)
    open4 = {f"t{i}": {"symbol": f"S{i}"} for i in range(4)}
    for i in range(6):
        d, flags = eng.decide(sig(id=f"sig-batch-{i}"), live_cfg(), True,
                              live_specs(), True, open4, 0.0, "2026-10-16")
        assert d == "commanded", f"signal {i} should command"
        assert flags["in_flight_opens"] == i
        claim_sent_open(eng, f"batch-{i}")  # command emitted, fill pending
    d, flags = eng.decide(sig(id="sig-batch-6"), live_cfg(), True,
                          live_specs(), True, open4, 0.0, "2026-10-16")
    assert d == "skipped:max_concurrent"
    assert flags["in_flight_opens"] == 6
    # And the old blind spot, for the record: without the in-flight
    # count the same gate sees only 4 confirmed and waves it through.
    d, _ = te.check_gates(sig(id="sig-blind"), **gate_ctx(
        open_positions=open4, max_concurrent=10))
    assert d == "commanded"
