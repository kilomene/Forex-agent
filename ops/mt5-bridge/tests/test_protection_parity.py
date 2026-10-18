"""Parity proof: the protection-plugin refactor of check_gates().

Phase 2 hard rule -- REFACTOR, not a behavior change. This module proves
that trade_executor.check_gates() (now the orchestrator over
protections.py filters) returns the IDENTICAL (decision, flags) as the
frozen pre-refactor implementation in tests/_legacy_gates.py for every
input.

Two layers:
  (a) a parametrized battery covering EVERY gate's trigger condition
      (plus boundary/edge cases), and
  (b) a seeded randomized fuzz (3000 cases over signals x configs).

Any parity failure = fix the refactor, never "fix" the test.

Weekday-independence: market_open is always passed explicitly and
candle_time fixtures are built from the current clock, so these tests
pass on weekends too (unlike the pre-existing
test_daily_loss_limit_trips_kill_switch_live weekend failure).
"""

import os
import random
import sys
from datetime import datetime, timezone

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_executor as te  # noqa: E402
from _legacy_gates import check_gates_legacy  # noqa: E402


# ---------------------------------------------------------------- fixtures

def fresh_candle():
    return datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S")


def base_sig(**over):
    s = {
        "id": "EURUSD_M15_BUY_1",
        "type": "signal.detected",
        "symbol": "EURUSD",
        "timeframe": "M15",
        "direction": "BUY",
        "entry_price": 1.1000,
        "stop_loss": 1.0950,
        "take_profit": 1.1100,
        "candle_time": fresh_candle(),
    }
    s.update(over)
    return s


def base_spec(**over):
    s = {
        "tick_value": 1.0,
        "tick_size": 0.00001,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "stops_level_points": 0,
        "freeze_level_points": 0,
        "spread_points": 10,
        "digits": 5,
        "point": 0.00001,
    }
    s.update(over)
    return s


def base_specs(symbol="EURUSD", symbols=None, account=None, **spec_over):
    syms = dict(symbols) if symbols is not None else {symbol: base_spec(**spec_over)}
    acct = {
        "server": "MetaQuotes-Demo",
        "currency": "USD",
        "equity": 200000.0,
        "balance": 200000.0,
        "free_margin": 190000.0,
        "margin_level": 1500.0,
        "time": fresh_candle(),
    }
    if account:
        acct.update(account)
    return {"symbols": syms, "account": acct}


def base_kwargs(**over):
    """Full-pass live config: every gate allows -> "commanded"."""
    kw = dict(
        enabled=True,
        dry_run=False,
        specs=base_specs(),
        specs_fresh=True,
        open_positions={},
        todays_profit=0.0,
        today_str="2026-10-18",
        risk_pct=0.05,
        max_concurrent=10,
        max_spread_points=50,
        max_daily_loss_pct=3.0,
        equity=200000.0,
        balance=200000.0,
        risk_basis=200000.0,
        max_total_exposure_lots=0,
        open_lots=0.0,
        max_consecutive_losses=0,
        consecutive_losses=0,
        max_correlated_positions=0,
        require_stop_loss=True,
        market_open=True,
        tick_age_s=None,
        enforce_correlation_gate=False,
        enforce_spread_gate=False,
        enforce_single_position_per_symbol=False,
        enforce_consecutive_losses_gate=False,
        mode="DEMO",
        enforce_mode_checks=False,
        mode_identity_ok=True,
        live_authorized=False,
        mode_risk_state=None,
        free_margin=None,
        margin_level=None,
        max_risk_per_trade_usd=100.0,
        emergency_drawdown_pct=3.0,
        price_reference=None,
        tick_value_overrides=None,
        blocked_symbols=None,
        max_signal_age_hours=2.0,
        in_flight_count=0,
    )
    kw.update(over)
    return kw


def run_both(sig, **kw_over):
    """(new_result, legacy_result) for identical inputs."""
    kw = base_kwargs(**kw_over)
    return te.check_gates(sig, **kw), check_gates_legacy(sig, **kw)


def open_pos(symbol, direction="BUY", ticket=None):
    return {ticket or ("t-" + symbol): {"symbol": symbol,
                                        "direction": direction}}


class StubState:
    """Attribute-style mode risk state (covers _mrs_get's hasattr path)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------- battery
# (name, sig_overrides, kw_overrides, expected_decision or None)

STALE = "2026.09.23 00:30:00"

BATTERY = [
    # -- gate 1: trading disabled -------------------------------------
    ("trading_disabled", {}, {"enabled": False}, "skipped:trading_disabled"),
    # -- gate 1a0: signal freshness ------------------------------------
    ("stale_signal_old", {"candle_time": STALE}, {}, "skipped:stale_signal"),
    ("stale_signal_missing", {"candle_time": None}, {}, "skipped:stale_signal"),
    ("stale_signal_garbage", {"candle_time": "not-a-time"}, {},
     "skipped:stale_signal"),
    ("stale_signal_disabled", {"candle_time": STALE},
     {"max_signal_age_hours": 0}, "commanded"),
    ("stale_signal_none_limit", {"candle_time": STALE},
     {"max_signal_age_hours": None}, "commanded"),
    # -- gate 1a: blocked symbols --------------------------------------
    ("symbol_blocked", {}, {"blocked_symbols": ["EURUSD"]},
     "skipped:symbol_blocked"),
    # -- gate 1b: risk basis -------------------------------------------
    ("risk_basis_none", {}, {"risk_basis": None},
     "skipped:risk_basis_unavailable"),
    ("risk_basis_zero", {}, {"risk_basis": 0}, "skipped:risk_basis_unavailable"),
    ("risk_basis_negative", {}, {"risk_basis": -100},
     "skipped:risk_basis_unavailable"),
    ("risk_basis_nan", {}, {"risk_basis": float("nan")},
     "skipped:risk_basis_unavailable"),
    ("risk_basis_inf", {}, {"risk_basis": float("inf")},
     "skipped:risk_basis_unavailable"),
    ("risk_basis_garbage", {}, {"risk_basis": "abc"},
     "skipped:risk_basis_unavailable"),
    # -- M0: mode identity ----------------------------------------------
    ("invalid_mode", {}, {"enforce_mode_checks": True, "mode": "BAD"},
     "skipped:invalid_mode"),
    ("invalid_mode_none", {}, {"enforce_mode_checks": True, "mode": None},
     "skipped:invalid_mode"),
    ("mode_identity_mismatch", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": False},
     "skipped:mode_identity_mismatch"),
    ("mode_identity_none", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": None},
     "skipped:mode_identity_mismatch"),
    ("live_not_authorized", {},
     {"enforce_mode_checks": True, "mode": "LIVE", "mode_identity_ok": True,
      "live_authorized": False}, "skipped:live_not_authorized"),
    # -- M1: margin data --------------------------------------------------
    ("margin_data_unavailable", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "equity": float("nan")}, "skipped:margin_data_unavailable"),
    ("margin_data_zero_balance", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "balance": 0}, "skipped:margin_data_unavailable"),
    # -- M2: spread known ---------------------------------------------------
    ("spread_unknown", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "specs": base_specs(spread_points=None)}, "skipped:spread_unknown"),
    # -- M3: stop loss (ALWAYS enforced) ------------------------------------
    ("missing_tp", {"take_profit": None}, {}, "skipped:missing_sl_tp"),
    ("missing_sl", {"stop_loss": None}, {}, "skipped:missing_sl_tp"),
    ("invalid_sl_wrong_side", {"stop_loss": 1.1050}, {},
     "skipped:invalid_sl"),
    ("invalid_sl_bad_numbers", {"stop_loss": "xx"}, {}, "skipped:invalid_sl"),
    ("invalid_sl_zero", {"stop_loss": 0}, {}, "skipped:missing_sl_tp"),
    ("invalid_sl_stop_level",
     {"entry_price": 1.1000, "stop_loss": 1.0950},
     {"specs": base_specs(stops_level_points=1000)}, "skipped:invalid_sl"),
    ("invalid_sl_freeze_level",
     {"entry_price": 1.1000, "stop_loss": 1.0999},
     {"specs": base_specs(point=0.00001, freeze_level_points=1000),
      "price_reference": 1.1000}, "skipped:invalid_sl"),
    ("valid_sl_sell", {"direction": "SELL", "entry_price": 1.1000,
                       "stop_loss": 1.1050, "take_profit": 1.0900},
     {}, "commanded"),
    # -- sizing needs specs ---------------------------------------------------
    ("stale_specs", {}, {"specs_fresh": False}, "skipped:stale_specs"),
    ("spec_missing", {},
     {"specs": base_specs(symbols={"GBPUSD": base_spec()})},
     "skipped:stale_specs"),
    ("specs_none", {}, {"specs": None}, "skipped:stale_specs"),
    # -- sizing errors ----------------------------------------------------------
    ("bad_specs_tick_value", {},
     {"specs": base_specs(tick_value=0)}, "skipped:bad_specs"),
    ("bad_specs_tick_size", {},
     {"specs": base_specs(tick_size=0)}, "skipped:bad_specs"),
    ("bad_specs_ea_invalid", {},
     {"specs": base_specs(tick_value_ok=False)}, "skipped:bad_specs"),
    ("bad_specs_crosscheck", {},
     {"specs": base_specs(tick_value=5.0, tick_size=0.00001,
                           contract_size=100000,
                           profit_currency="USD")}, "skipped:bad_specs"),
    ("volume_below_min", {},
     {"specs": base_specs(volume_min=50.0)}, "skipped:volume_below_min"),
    ("tick_override_fixes_specs", {},
     {"specs": base_specs(tick_value=0),
      "tick_value_overrides": {"EURUSD": 1.0}}, "commanded"),
    # -- M4: max risk per trade ---------------------------------------------------
    ("max_risk_per_trade", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "max_risk_per_trade_usd": 50.0}, "skipped:max_risk_per_trade"),
    ("max_risk_per_trade_boundary", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0},
      "max_risk_per_trade_usd": 100.0}, "commanded"),
    ("max_risk_per_trade_garbage_cap", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0},
      "max_risk_per_trade_usd": "bad"}, "commanded"),
    # -- M5: mode daily loss ---------------------------------------------------------
    ("mode_state_unavailable", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {}}, "skipped:mode_state_unavailable"),
    ("mode_state_none", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": None}, "skipped:mode_state_unavailable"),
    ("mode_daily_loss_tripped", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0,
                          "daily_loss_tripped": True}},
     "skipped:mode_daily_loss"),
    ("mode_daily_loss_computed", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0,
                          "realized_pl": -7000.0, "floating_pl": 0.0}},
     "skipped:mode_daily_loss"),
    ("mode_daily_loss_ok", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0,
                          "realized_pl": -1000.0}}, "commanded"),
    ("mode_daily_loss_attr_state", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": StubState(start_of_day_equity=200000.0,
                                   realized_pl=-7000.0)},
     "skipped:mode_daily_loss"),
    # -- M6: emergency drawdown ----------------------------------------------------------------
    ("emergency_drawdown_tripped", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0,
                          "kill_switch_tripped": True}},
     "skipped:emergency_drawdown"),
    ("emergency_drawdown_dd", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0,
                          "current_drawdown": 0.05}},
     "skipped:emergency_drawdown"),
    ("emergency_drawdown_pct_garbage", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "emergency_drawdown_pct": "bad",
      "mode_risk_state": {"start_of_day_equity": 200000.0,
                          "current_drawdown": 0.05}},
     "skipped:emergency_drawdown"),
    ("emergency_drawdown_ok", {},
     {"enforce_mode_checks": True, "mode": "DEMO", "mode_identity_ok": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0,
                          "current_drawdown": 0.01}}, "commanded"),
    # -- total exposure (clamp behavior!) ----------------------------------------------
    ("max_total_exposure_full", {},
     {"max_total_exposure_lots": 1.0, "open_lots": 1.0},
     "skipped:max_total_exposure"),
    ("max_total_exposure_over", {},
     {"max_total_exposure_lots": 1.0, "open_lots": 1.5},
     "skipped:max_total_exposure"),
    ("max_total_exposure_clamp", {},
     {"max_total_exposure_lots": 0.15, "open_lots": 0.0}, "commanded"),
    ("max_total_exposure_clamp_below_min", {},
     {"max_total_exposure_lots": 0.015, "open_lots": 0.01},
     "skipped:max_total_exposure"),
    ("max_total_exposure_disabled", {},
     {"max_total_exposure_lots": 0, "open_lots": 999.0}, "commanded"),
    # -- tick freshness --------------------------------------------------------------------
    ("stale_tick", {}, {"tick_age_s": 121}, "skipped:stale_tick"),
    ("tick_age_boundary", {}, {"tick_age_s": 120}, "commanded"),
    ("tick_age_none", {}, {"tick_age_s": None}, "commanded"),
    # -- dry_run -> intended ------------------------------------------------------------------
    ("intended", {}, {"dry_run": True}, "intended"),
    ("intended_clamped", {},
     {"dry_run": True, "max_total_exposure_lots": 0.15}, "intended"),
    ("dry_run_stale_specs", {},
     {"dry_run": True, "specs_fresh": False}, "skipped:stale_specs"),
    ("dry_run_market_closed", {},
     {"dry_run": True, "market_open": False}, "intended"),
    # -- market closed ---------------------------------------------------------------------------
    ("market_closed", {}, {"market_open": False}, "skipped:market_closed"),
    # -- server check ---------------------------------------------------------------------------------
    ("non_demo_server", {},
     {"specs": base_specs(account={"server": "MetaQuotes-Real"})},
     "skipped:non_demo_server"),
    ("non_demo_server_empty", {},
     {"specs": base_specs(account={"server": ""})},
     "skipped:non_demo_server"),
    ("live_on_demo_server", {},
     {"enforce_mode_checks": True, "mode": "LIVE", "mode_identity_ok": True,
      "live_authorized": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0}},
     "skipped:live_on_demo_server"),
    ("live_on_real_server", {},
     {"enforce_mode_checks": True, "mode": "LIVE", "mode_identity_ok": True,
      "live_authorized": True,
      "mode_risk_state": {"start_of_day_equity": 200000.0},
      "specs": base_specs(account={"server": "MetaQuotes-Real"})},
     "commanded"),
    # -- max concurrent (incl. in-flight) ------------------------------------------------------------------
    ("max_concurrent",
     {},
     {"open_positions": {("t%d" % i): {"symbol": "XAUUSD", "direction": "BUY"}
                         for i in range(10)}},
     "skipped:max_concurrent"),
    ("max_concurrent_in_flight",
     {},
     {"open_positions": {("t%d" % i): {"symbol": "XAUUSD", "direction": "BUY"}
                         for i in range(8)},
      "in_flight_count": 2},
     "skipped:max_concurrent"),
    ("max_concurrent_ok",
     {},
     {"open_positions": {("t%d" % i): {"symbol": "XAUUSD", "direction": "BUY"}
                         for i in range(9)}},
     "commanded"),
    # -- single position per symbol ----------------------------------------------------------------------------
    ("symbol_already_open", {},
     {"enforce_single_position_per_symbol": True,
      "open_positions": open_pos("EURUSD")}, "skipped:symbol_already_open"),
    ("symbol_already_open_disabled", {},
     {"enforce_single_position_per_symbol": False,
      "open_positions": open_pos("EURUSD")}, "commanded"),
    # -- correlated positions -------------------------------------------------------------------------------
    ("correlated_position",
     {"symbol": "GBPUSD"},
     {"enforce_correlation_gate": True, "max_correlated_positions": 1,
      "open_positions": open_pos("EURUSD", "BUY"),
      "specs": base_specs(symbols={"EURUSD": base_spec(),
                                   "GBPUSD": base_spec()})},
     "skipped:correlated_position"),
    ("correlated_position_nets_out",
     {"symbol": "USDJPY"},
     {"enforce_correlation_gate": True, "max_correlated_positions": 1,
      "open_positions": open_pos("EURUSD", "BUY"),
      "specs": base_specs(symbols={"EURUSD": base_spec(),
                                   "USDJPY": base_spec()})},
     "commanded"),
    ("correlated_position_unknown_pos_direction",
     {"symbol": "GBPUSD"},
     {"enforce_correlation_gate": True, "max_correlated_positions": 1,
      "open_positions": {"t1": {"symbol": "EURUSD"}},
      "specs": base_specs(symbols={"EURUSD": base_spec(),
                                   "GBPUSD": base_spec()})},
     "skipped:correlated_position"),
    ("correlated_position_disabled",
     {"symbol": "GBPUSD"},
     {"enforce_correlation_gate": False, "max_correlated_positions": 1,
      "open_positions": open_pos("EURUSD", "BUY"),
      "specs": base_specs(symbols={"EURUSD": base_spec(),
                                   "GBPUSD": base_spec()})},
     "commanded"),
    # -- consecutive losses ---------------------------------------------------------------------------------
    ("consecutive_losses", {},
     {"enforce_consecutive_losses_gate": True, "max_consecutive_losses": 3,
      "consecutive_losses": 3}, "skipped:consecutive_losses"),
    ("consecutive_losses_below", {},
     {"enforce_consecutive_losses_gate": True, "max_consecutive_losses": 3,
      "consecutive_losses": 2}, "commanded"),
    # -- spread cap ----------------------------------------------------------------------------------------------
    ("spread_too_wide", {},
     {"enforce_spread_gate": True,
      "specs": base_specs(spread_points=60)}, "skipped:spread_too_wide"),
    ("spread_boundary", {},
     {"enforce_spread_gate": True,
      "specs": base_specs(spread_points=50)}, "commanded"),
    ("spread_gate_disabled", {},
     {"enforce_spread_gate": False,
      "specs": base_specs(spread_points=600)}, "commanded"),
    # -- daily loss limit (with trip flag) ---------------------------------------------------------------------------
    ("daily_loss_limit", {},
     {"todays_profit": -6000.0}, "skipped:daily_loss_limit"),
    ("daily_loss_boundary", {},
     {"todays_profit": -5999.99}, "commanded"),
    ("daily_loss_profit", {},
     {"todays_profit": 500.0}, "commanded"),
    # -- floating drawdown --------------------------------------------------------------------------------------------------
    ("floating_drawdown", {},
     {"equity": 197000.0, "balance": 200000.0}, "skipped:floating_drawdown"),
    ("floating_drawdown_boundary", {},
     {"equity": 198000.0, "balance": 200000.0}, "skipped:floating_drawdown"),
    ("floating_drawdown_ok", {},
     {"equity": 198001.0, "balance": 200000.0}, "commanded"),
    ("floating_drawdown_no_balance", {},
     {"equity": 1.0, "balance": 0}, "commanded"),
    ("floating_drawdown_none_balance", {},
     {"equity": 1.0, "balance": None}, "commanded"),
    # -- full pass ---------------------------------------------------------------------------------------------------------------
    ("commanded_full_pass", {}, {}, "commanded"),
]


@pytest.mark.parametrize(
    "name,sig_over,kw_over,expected",
    [pytest.param(n, s, k, e, id=n) for (n, s, k, e) in BATTERY])
def test_gate_parity(name, sig_over, kw_over, expected):
    sig = base_sig(**sig_over)
    new, legacy = run_both(sig, **kw_over)
    assert new == legacy, (
        "PARITY FAILURE on gate case %r:\n  refactored=%r\n  legacy=%r"
        % (name, new, legacy))
    if expected is not None:
        assert new[0] == expected, (
            "case %r: expected decision %r, got %r" % (name, expected, new))


def test_parity_flag_details():
    """Spot-check that flags (not just decisions) match on representative
    gates: loud/trip/in_flight/sl_reason/volume carry-over."""
    cases = [
        ({"candle_time": STALE}, {}, {"loud": True}),
        ({"stop_loss": 1.1050}, {}, {"loud": True, "sl_reason": "wrong_sided_sl"}),
        ({}, {"todays_profit": -6000.0}, {"loud": True, "trip": True}),
        ({}, {"open_positions": {("t%d" % i): {"symbol": "X", "direction": "B"}
                                  for i in range(10)}},
         {"in_flight": 0}),
        ({}, {"dry_run": True}, None),  # volume asserted below (float floor)
        # clamp math is float-exact (floor(0.15/0.01) == 14 in IEEE754):
        # assert the clamped value matches legacy, whatever it is.
        ({}, {"dry_run": True, "max_total_exposure_lots": 0.15}, None),
        ({}, {}, None),  # volume asserted below (float floor)
        ({}, {"enforce_mode_checks": True, "mode": "DEMO",
              "mode_identity_ok": True,
              "mode_risk_state": {"start_of_day_equity": 200000.0,
                                  "daily_loss_tripped": True}},
         {"loud": True, "trip_mode_daily": True}),
        ({}, {"enforce_mode_checks": True, "mode": "DEMO",
              "mode_identity_ok": True,
              "mode_risk_state": {"start_of_day_equity": 200000.0,
                                  "kill_switch_tripped": True}},
         {"loud": True, "trip": True, "trip_mode_kill": True}),
    ]
    for sig_over, kw_over, expected_flags in cases:
        new, legacy = run_both(base_sig(**sig_over), **kw_over)
        assert new == legacy
        if expected_flags is not None:
            assert new[1] == expected_flags, (sig_over, kw_over, new[1])
        else:
            # volume sizing does a float floor (0.005/1e-5 tick math):
            # assert it is positive and sane, and identical to legacy
            # (the new == legacy assert above already proved identity).
            assert 0 < new[1]["volume"] < 0.25, new[1]
            assert new[1]["risk_amount"] == 100.0, new[1]


# ---------------------------------------------------------------- fuzz

FUZZ_CASES = 3000


def _fuzz_spec(rng):
    return {
        "tick_value": rng.choice([0, 0.0, 1.0, 5.0]),
        "tick_size": rng.choice([0, 0.00001, 0.001]),
        "volume_min": rng.choice([0, 0.01, 50.0]),
        "volume_max": rng.choice([0, 100.0]),
        "volume_step": rng.choice([0, 0.01, 0.1]),
        "stops_level_points": rng.choice([0, 1000]),
        "freeze_level_points": rng.choice([0, 1000]),
        "spread_points": rng.choice([None, 0, 10, 60, 200]),
        "digits": 5,
        "point": rng.choice([0, 0.00001]),
        "tick_value_ok": rng.choice([True, False]),
        "contract_size": rng.choice([0, 100000]),
        "profit_currency": rng.choice(["", "USD"]),
    }


def _fuzz_signal(rng, symbols):
    # Half the draws are fully valid signals (deep-path coverage); the
    # rest are broken in random ways.
    if rng.random() < 0.5:
        direction = rng.choice(["BUY", "SELL"])
        entry = 1.1000
        if direction == "BUY":
            sl, tp = 1.0950, 1.1100
        else:
            sl, tp = 1.1050, 1.0900
        return {
            "id": "fz-%d" % rng.randrange(10 ** 9),
            "type": "signal.detected",
            "symbol": rng.choice(symbols),
            "timeframe": rng.choice(["M15", "H1", "D1"]),
            "direction": direction,
            "entry_price": entry,
            "stop_loss": sl,
            "take_profit": tp,
            "candle_time": fresh_candle(),
        }
    return {
        "id": "fz-%d" % rng.randrange(10 ** 9),
        "type": "signal.detected",
        "symbol": rng.choice(symbols),
        "timeframe": rng.choice(["M15", "H1", "X9", None]),
        "direction": rng.choice(["BUY", "SELL", "buy", None, ""]),
        "entry_price": rng.choice([1.1, 0.0, -5, "xx", None]),
        "stop_loss": rng.choice([1.0950, 1.1050, 0, "xx", None]),
        "take_profit": rng.choice([1.11, None, 0]),
        "candle_time": rng.choice(
            [fresh_candle(), STALE, None, "garbage"]),
    }


def _fuzz_positions(rng, symbols):
    n = rng.randrange(0, 13)
    pos = {}
    for i in range(n):
        pos["fz-t%d" % i] = {
            "symbol": rng.choice(symbols),
            "direction": rng.choice(["BUY", "SELL", None, ""]),
        }
    return pos


def _fuzz_mode_state(rng):
    return rng.choice([
        None,
        {},
        {"start_of_day_equity": 200000.0},
        {"start_of_day_equity": 200000.0, "realized_pl": -7000.0,
         "floating_pl": 0.0},
        {"start_of_day_equity": 200000.0, "daily_loss_tripped": True},
        {"start_of_day_equity": 200000.0, "kill_switch_tripped": True},
        {"start_of_day_equity": 200000.0, "current_drawdown": 0.05},
        {"start_of_day_equity": 200000.0, "current_drawdown": 0.01},
        StubState(start_of_day_equity=200000.0, realized_pl=-7000.0),
    ])


def test_fuzz_parity():
    """3000 seeded randomized (signal, config) pairs: refactored and
    legacy check_gates must return identical (decision, flags)."""
    rng = random.Random(20261018)
    symbols = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "GBPZAR", "EURDKK",
               "US30", "XPDUSD", ""]
    mismatches = []
    for i in range(FUZZ_CASES):
        sig = _fuzz_signal(rng, symbols)
        sym = sig.get("symbol")
        # Specs sometimes cover a different symbol set (spec-missing path).
        spec_syms = rng.sample(symbols, rng.randrange(0, 4))
        specs = {
            "symbols": {s: _fuzz_spec(rng) for s in spec_syms if s},
            "account": {
                "server": rng.choice(
                    ["MetaQuotes-Demo", "MetaQuotes-Real", "", None]),
                "currency": "USD",
                "equity": 200000.0,
                "balance": 200000.0,
            },
        }
        kw = base_kwargs(
            enabled=rng.choice([True, False]),
            dry_run=rng.choice([True, False]),
            specs=specs,
            specs_fresh=rng.choice([True, False]),
            open_positions=_fuzz_positions(rng, symbols),
            todays_profit=rng.choice([-20000.0, -6000.0, -5999.0, 0.0, 500.0]),
            risk_pct=rng.choice([0.01, 0.05, 0.5, 2.0]),
            max_concurrent=rng.choice([1, 3, 10]),
            max_spread_points=50,
            max_daily_loss_pct=3.0,
            equity=rng.choice([None, 0, 197000.0, 198000.0, 200000.0,
                               float("nan")]),
            balance=rng.choice([None, 0, 200000.0]),
            risk_basis=rng.choice([None, 0, -1, float("nan"), float("inf"),
                                   "abc", 200000.0, 50000.0]),
            max_total_exposure_lots=rng.choice([0, 0.015, 0.15, 1.0, 5.0]),
            open_lots=rng.choice([0.0, 0.01, 1.0, 6.0]),
            max_consecutive_losses=rng.choice([0, 3]),
            consecutive_losses=rng.choice([0, 2, 3, 5]),
            max_correlated_positions=rng.choice([0, 1, 2]),
            market_open=rng.choice([True, False]),
            tick_age_s=rng.choice([None, 0, 120, 121, 1000]),
            enforce_correlation_gate=rng.choice([True, False]),
            enforce_spread_gate=rng.choice([True, False]),
            enforce_single_position_per_symbol=rng.choice([True, False]),
            enforce_consecutive_losses_gate=rng.choice([True, False]),
            mode=rng.choice([None, "DEMO", "LIVE", "BAD"]),
            enforce_mode_checks=rng.choice([True, False]),
            mode_identity_ok=rng.choice([True, False, None]),
            live_authorized=rng.choice([True, False]),
            mode_risk_state=_fuzz_mode_state(rng),
            free_margin=rng.choice([None, 100000.0, 0, float("nan")]),
            margin_level=rng.choice([None, 1500.0, 0]),
            max_risk_per_trade_usd=rng.choice(
                [10.0, 50.0, 100.0, 200.0, "bad", None]),
            emergency_drawdown_pct=rng.choice([1.0, 3.0, 10.0, "bad"]),
            price_reference=rng.choice([None, 1.1]),
            tick_value_overrides=rng.choice([{}, {sym: 1.0}]),
            blocked_symbols=rng.choice([[], [sym]]),
            max_signal_age_hours=rng.choice([None, 0, 2.0]),
            in_flight_count=rng.choice([0, 2, 5]),
        )
        new = te.check_gates(sig, **kw)
        legacy = check_gates_legacy(sig, **kw)
        if new != legacy:
            mismatches.append((i, sig, kw, new, legacy))
            if len(mismatches) >= 5:
                break
    assert not mismatches, (
        "FUZZ PARITY FAILURES (%d/%d): first=%r"
        % (len(mismatches), FUZZ_CASES, mismatches[:1]))
