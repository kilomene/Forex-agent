"""Worker C (2026-09-23): per-mode risk state + mode/integrity gates.

Covers risk_state.py (DEMO/LIVE independence, atomic persistence across
re-instantiation, day roll) and the new check_gates M0-M6 gates
(opt-in via enforce_mode_checks=True).
"""
import json
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_executor as te  # noqa: E402
from risk_state import ModeRiskState, state_path  # noqa: E402


# ---------------------------------------------------------------- fixtures

def spec(**kw):
    s = {
        "tick_value": 1.0, "tick_size": 0.001, "volume_min": 0.01,
        "volume_max": 100.0, "volume_step": 0.01, "stops_level_points": 0,
        "spread_points": 20, "digits": 3, "point": 0.001,
    }
    s.update(kw)
    return s


def mode_state_dict(sod=200_000.0, realized=0.0, floating=0.0,
                    dd=0.0, daily_tripped=False, kill_tripped=False):
    return {
        "mode": "DEMO", "day": "2026-09-23",
        "start_of_day_equity": sod,
        "realized_pl": realized, "floating_pl": floating,
        "commission": 0.0, "swap": 0.0,
        "peak_equity": sod, "current_drawdown": dd,
        "daily_loss_tripped": daily_tripped,
        "kill_switch_tripped": kill_tripped,
    }


def mode_ctx(**over):
    """check_gates kwargs where every M-gate PASSES (LIVE-mode enforced)."""
    ctx = dict(
        enabled=True, dry_run=False,
        specs={"symbols": {"XPDUSD": spec()},
               "account": {"server": "LIVE-Broker", "equity": 200_000.0,
                           "balance": 200_000.0, "free_margin": 190_000.0,
                           "margin_level": 1500.0}},
        specs_fresh=True, open_positions={}, todays_profit=0.0,
        today_str="2026-09-23", risk_pct=0.05, max_concurrent=10,
        max_spread_points=50, max_daily_loss_pct=3.0,
        equity=200_000.0, balance=200_000.0, risk_basis=200_000.0,
        market_open=True,
        mode="LIVE", enforce_mode_checks=True, mode_identity_ok=True,
        live_authorized=True, mode_risk_state=mode_state_dict(),
    )
    ctx.update(over)
    return ctx


def sig(**over):
    s = {
        "id": "XPDUSD_M15_BUY_1", "type": "signal.detected",
        "symbol": "XPDUSD", "timeframe": "M15", "direction": "BUY",
        "entry_price": 1310.914, "stop_loss": 1306.344,
        "take_profit": 1320.054,
    }
    s.update(over)
    return s


# ---------------------------------------------------------------- state file

def test_state_paths_are_mode_segregated(tmp_path):
    assert state_path(str(tmp_path), "DEMO").endswith("risk_state_DEMO.json")
    assert state_path(str(tmp_path), "LIVE").endswith("risk_state_LIVE.json")
    assert (state_path(str(tmp_path), "DEMO")
            != state_path(str(tmp_path), "LIVE"))
    with pytest.raises(ValueError):
        state_path(str(tmp_path), "PAPER")


def test_demo_loss_does_not_affect_live(tmp_path):
    rd = str(tmp_path)
    demo = ModeRiskState.load(rd, "DEMO")
    demo.roll_day_if_needed("2026-09-23", 200_000.0)
    demo.record_closed_trade(-7_000.0)  # blows the 3% daily loss
    demo.note_daily_loss_tripped()
    demo.save(rd)
    live = ModeRiskState.load(rd, "LIVE")
    assert live.realized_pl == 0.0
    assert live.daily_loss_tripped is False
    assert live.breached_daily_loss(3.0) is False
    assert demo.breached_daily_loss(3.0) is True


def test_persistence_across_reinstantiation(tmp_path):
    rd = str(tmp_path)
    s = ModeRiskState.load(rd, "DEMO")
    s.roll_day_if_needed("2026-09-23", 200_000.0)
    s.record_closed_trade(-500.0, commission=2.0, swap=-1.0)
    s.update_floating(-100.0)
    s.save(rd)
    # Fresh instance from disk: everything survives the restart.
    s2 = ModeRiskState.load(rd, "DEMO")
    assert s2.day == "2026-09-23"
    assert s2.start_of_day_equity == 200_000.0
    assert s2.realized_pl == -500.0
    assert s2.floating_pl == -100.0
    assert s2.commission == 2.0
    assert s2.swap == -1.0
    assert s2.current_drawdown > 0
    # The file is valid JSON (atomic write never tears).
    with open(state_path(rd, "DEMO")) as f:
        json.load(f)


def test_mode_mismatch_never_mixes(tmp_path):
    rd = str(tmp_path)
    ModeRiskState.load(rd, "DEMO").save(rd)
    path = state_path(rd, "DEMO")
    with open(path) as f:
        data = json.load(f)
    data["mode"] = "LIVE"
    with open(path, "w") as f:
        json.dump(data, f)
    with pytest.raises(ValueError):
        ModeRiskState.load(rd, "DEMO")


def test_day_roll_resets_daily_but_keeps_kill_switch(tmp_path):
    rd = str(tmp_path)
    s = ModeRiskState.load(rd, "DEMO")
    s.roll_day_if_needed("2026-09-23", 200_000.0)
    s.record_closed_trade(-1_000.0)
    s.note_daily_loss_tripped()
    s.note_kill_switch_tripped()
    assert s.roll_day_if_needed("2026-09-24", 199_000.0) is True
    assert s.realized_pl == 0.0
    assert s.daily_loss_tripped is False
    assert s.start_of_day_equity == 199_000.0
    # Kill switch survives the day roll: explicit operator action only.
    assert s.kill_switch_tripped is True
    s.clear_kill_switch()
    assert s.kill_switch_tripped is False


# ---------------------------------------------------------------- M0: mode

def test_m0_invalid_mode_blocked():
    d, flags = te.check_gates(sig(), **mode_ctx(mode="PAPER"))
    assert d == "skipped:invalid_mode"
    assert flags.get("loud") is True


def test_m0_identity_mismatch_blocked():
    d, _ = te.check_gates(sig(), **mode_ctx(mode_identity_ok=False))
    assert d == "skipped:mode_identity_mismatch"


def test_m0_live_without_authorization_blocked():
    d, _ = te.check_gates(sig(), **mode_ctx(live_authorized=False))
    assert d == "skipped:live_not_authorized"


def test_m0_live_on_demo_server_blocked():
    specs = {"symbols": {"XPDUSD": spec()},
             "account": {"server": "MetaQuotes-Demo", "equity": 200_000.0,
                         "balance": 200_000.0, "free_margin": 190_000.0,
                         "margin_level": 1500.0}}
    d, flags = te.check_gates(sig(), **mode_ctx(specs=specs))
    assert d == "skipped:live_on_demo_server"
    assert flags.get("loud") is True


def test_m0_demo_needs_no_live_auth():
    specs = {"symbols": {"XPDUSD": spec()},
             "account": {"server": "MetaQuotes-Demo", "equity": 200_000.0,
                         "balance": 200_000.0, "free_margin": 190_000.0,
                         "margin_level": 1500.0}}
    d, _ = te.check_gates(
        sig(), **mode_ctx(mode="DEMO", live_authorized=False,
                          specs=specs,
                          mode_risk_state=mode_state_dict()))
    assert d == "commanded"


# ---------------------------------------------------------------- M1/M2

def test_m1_missing_margin_data_blocked():
    specs = {"symbols": {"XPDUSD": spec()},
             "account": {"server": "LIVE-Broker", "equity": 200_000.0,
                         "balance": 200_000.0}}  # no free_margin/level
    d, flags = te.check_gates(sig(), **mode_ctx(specs=specs))
    assert d == "skipped:margin_data_unavailable"
    assert flags.get("loud") is True


def test_m1_zero_equity_blocked():
    d, _ = te.check_gates(sig(), **mode_ctx(equity=0.0))
    assert d == "skipped:margin_data_unavailable"


def test_m2_missing_spread_blocked():
    specs = {"symbols": {"XPDUSD": spec(spread_points=None)},
             "account": {"server": "LIVE-Broker", "equity": 200_000.0,
                         "balance": 200_000.0, "free_margin": 190_000.0,
                         "margin_level": 1500.0}}
    d, _ = te.check_gates(sig(), **mode_ctx(specs=specs))
    assert d == "skipped:spread_unknown"


# ---------------------------------------------------------------- M3: SL

def test_m3_missing_sl_blocked_keeps_legacy_reason():
    d, flags = te.check_gates(sig(stop_loss=None), **mode_ctx())
    assert d == "skipped:missing_sl_tp"
    assert flags.get("loud") is True


def test_m3_wrong_sided_sl_buy_blocked():
    d, flags = te.check_gates(
        sig(direction="BUY", stop_loss=1315.0), **mode_ctx())
    assert d == "skipped:invalid_sl"
    assert flags.get("sl_reason") == "wrong_sided_sl"


def test_m3_wrong_sided_sl_sell_blocked():
    d, flags = te.check_gates(
        sig(direction="SELL", entry_price=1310.914, stop_loss=1306.344,
            take_profit=1320.054), **mode_ctx())
    assert d == "skipped:invalid_sl"
    assert flags.get("sl_reason") == "wrong_sided_sl"


def test_m3_sl_inside_stop_level_blocked():
    # stops_level 200 points = 0.2 price units; SL only 0.1 away.
    d, flags = te.check_gates(
        sig(entry_price=1310.914, stop_loss=1310.814),
        **mode_ctx(specs={"symbols": {"XPDUSD": spec(stops_level_points=200)},
                          "account": mode_ctx()["specs"]["account"]}))
    assert d == "skipped:invalid_sl"
    assert flags.get("sl_reason") == "sl_inside_stop_level"


def test_m3_sl_inside_freeze_level_blocked():
    # freeze 500 points = 0.5; SL 0.1 from the price reference.
    specs = {"symbols": {"XPDUSD": spec(stops_level_points=0,
                                       freeze_level_points=500)},
             "account": mode_ctx()["specs"]["account"]}
    d, flags = te.check_gates(
        sig(entry_price=1310.914, stop_loss=1310.814),
        **mode_ctx(specs=specs, price_reference=1310.914))
    assert d == "skipped:invalid_sl"
    assert flags.get("sl_reason") == "sl_inside_freeze_level"


def test_m3_valid_sl_passes():
    d, _ = te.check_gates(sig(), **mode_ctx())
    assert d == "commanded"


def test_validate_stop_loss_unit():
    ok, why = te.validate_stop_loss(sig(), spec())
    assert (ok, why) == (True, "")
    ok, why = te.validate_stop_loss(sig(stop_loss=None), spec())
    assert (ok, why) == (False, "missing_sl")
    ok, why = te.validate_stop_loss(sig(stop_loss="abc"), spec())
    assert (ok, why) == (False, "bad_sl_numbers")


# ---------------------------------------------------------------- M4/M5/M6

def test_m4_risk_over_100_blocked():
    # risk_pct 1.0 on $200k basis = $2000 planned risk -> over the cap.
    d, flags = te.check_gates(sig(), **mode_ctx(risk_pct=1.0))
    assert d == "skipped:max_risk_per_trade"
    assert flags.get("loud") is True


def test_m4_risk_at_100_allowed():
    d, _ = te.check_gates(sig(), **mode_ctx(risk_pct=0.05))
    assert d == "commanded"


def test_m5_daily_loss_blocks_entries():
    st = mode_state_dict(sod=200_000.0, realized=-6_000.0)
    d, flags = te.check_gates(sig(), **mode_ctx(mode_risk_state=st))
    assert d == "skipped:mode_daily_loss"
    assert flags.get("trip_mode_daily") is True


def test_m5_tripped_flag_blocks_even_after_recovery():
    st = mode_state_dict(sod=200_000.0, realized=0.0, daily_tripped=True)
    d, _ = te.check_gates(sig(), **mode_ctx(mode_risk_state=st))
    assert d == "skipped:mode_daily_loss"


def test_m5_small_loss_does_not_block():
    st = mode_state_dict(sod=200_000.0, realized=-100.0)
    d, _ = te.check_gates(sig(), **mode_ctx(mode_risk_state=st))
    assert d == "commanded"


def test_m6_emergency_drawdown_trips_kill():
    st = mode_state_dict(sod=200_000.0, dd=0.035)
    d, flags = te.check_gates(sig(), **mode_ctx(mode_risk_state=st))
    assert d == "skipped:emergency_drawdown"
    assert flags.get("trip") is True
    assert flags.get("trip_mode_kill") is True


def test_m6_kill_switch_tripped_blocks():
    st = mode_state_dict(kill_tripped=True)
    d, _ = te.check_gates(sig(), **mode_ctx(mode_risk_state=st))
    assert d == "skipped:emergency_drawdown"


def test_m5_missing_state_fails_closed():
    d, _ = te.check_gates(sig(), **mode_ctx(mode_risk_state=None))
    assert d == "skipped:mode_state_unavailable"


# ---------------------------------------------------------------- compat

def test_legacy_behavior_without_mode_checks():
    """Direct callers that do not opt in keep the legacy behavior."""
    ctx = dict(
        enabled=True, dry_run=False,
        specs={"symbols": {"XPDUSD": spec()},
               "account": {"server": "MetaQuotes-Demo",
                           "equity": 1_000_000.0}},
        specs_fresh=True, open_positions={}, todays_profit=0.0,
        today_str="2026-09-21", risk_pct=0.5, max_concurrent=3,
        max_spread_points=50, max_daily_loss_pct=2.0,
        equity=1_000_000.0, risk_basis=1_000_000.0, market_open=True,
    )
    d, _ = te.check_gates(sig(), **ctx)
    assert d == "commanded"
