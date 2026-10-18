"""Unit tests for the new protection plugins and the filter framework.

The three new plugins (CooldownPeriod, StoplossGuard, SessionFilter) are
COMPLETE implementations that happen to be disabled by config -- not
skeletons. Each test below proves: the block fires exactly when it
should, stays silent when disabled or when its state/conditions are
absent, and carries unlock_until where the contract promises it.

Standing rule: enabling any of these is AUTO tier only through the
pipeline's plugin-observe / plugin-enforce flow (calm observe soak
inside preregistered bounds, engine wiring present). The engine's
legacy pipeline must NOT include them.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import protections as P  # noqa: E402


def ctx(**over):
    c = P.GateContext(
        enabled=True, dry_run=False, specs=None, specs_fresh=True,
        open_positions={}, todays_profit=0.0, today_str="2026-10-18",
        risk_pct=0.05, max_concurrent=10, max_spread_points=50,
        max_daily_loss_pct=3.0, equity=200000.0, balance=200000.0,
        risk_basis=200000.0, max_total_exposure_lots=0, open_lots=0.0,
        max_consecutive_losses=0, consecutive_losses=0,
        max_correlated_positions=0, require_stop_loss=True,
        market_open=True, tick_age_s=None,
        enforce_correlation_gate=False, enforce_spread_gate=False,
        enforce_single_position_per_symbol=False,
        enforce_consecutive_losses_gate=False,
        mode="DEMO", enforce_mode_checks=False, mode_identity_ok=True,
        live_authorized=False, mode_risk_state=None,
        free_margin=None, margin_level=None,
        max_risk_per_trade_usd=100.0, emergency_drawdown_pct=3.0,
        price_reference=None, tick_value_overrides=None,
        blocked_symbols=None, max_signal_age_hours=2.0, in_flight_count=0,
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


def sig(**over):
    s = {"id": "s1", "symbol": "EURUSD", "timeframe": "M15",
         "direction": "BUY", "entry_price": 1.1, "stop_loss": 1.09,
         "take_profit": 1.11}
    s.update(over)
    return s


# ---------------------------------------------------------------- framework

def test_new_plugins_disabled_by_default():
    assert P.CooldownPeriod().enabled is False
    assert P.StoplossGuard().enabled is False
    assert P.SessionFilter().enabled is False


def test_legacy_pipeline_excludes_new_plugins():
    """The engine call path must not enable the new plugins: they are not
    in the legacy pipeline at all."""
    filters = P.legacy_pipeline(ctx())
    names = [f.name for f in filters]
    assert "cooldown_period" not in names
    assert "stoploss_guard" not in names
    assert "session_filter" not in names
    assert len(filters) == 25  # every legacy gate, none added


def test_legacy_pipeline_order_matches_legacy_gate_order():
    filters = P.legacy_pipeline(ctx())
    names = [f.name for f in filters]
    assert names == [
        "trading_disabled", "stale_signal", "symbol_blocked",
        "risk_basis_unavailable", "mode_identity", "margin_data_unavailable",
        "spread_unknown", "stop_loss", "stale_specs", "sizing",
        "max_risk_per_trade", "mode_daily_loss", "emergency_drawdown",
        "max_total_exposure", "stale_tick", "dry_run_intended",
        "market_closed", "server_mode", "max_concurrent",
        "symbol_already_open", "correlated_position", "consecutive_losses",
        "spread_too_wide", "daily_loss_limit", "floating_drawdown",
    ]


def test_first_block_wins_and_disabled_skipped():
    class B1(P.ProtectionFilter):
        name = "b1"

        def evaluate(self, s, c):
            return ("block", "skipped:first", {"loud": True}, None)

    class B2(P.ProtectionFilter):
        name = "b2"

        def evaluate(self, s, c):
            return ("block", "skipped:second", {}, None)

    decision, flags = P.run_pipeline(sig(), ctx(), [B1(), B2()])
    assert (decision, flags) == ("skipped:first", {"loud": True})
    # disabled filters are skipped entirely
    decision, flags = P.run_pipeline(
        sig(), ctx(), [B1(enabled=False), B2()])
    assert decision == "skipped:second"


def test_decide_verdict_is_terminal():
    class D(P.ProtectionFilter):
        name = "d"

        def evaluate(self, s, c):
            return ("decide", "intended", {"volume": 1.0})

    class B(P.ProtectionFilter):
        name = "b"

        def evaluate(self, s, c):
            return ("block", "skipped:x", {}, None)

    decision, flags = P.run_pipeline(sig(), ctx(), [D(), B()])
    assert (decision, flags) == ("intended", {"volume": 1.0})


def test_all_allow_yields_commanded_with_sized_volume():
    c = ctx()
    c.volume, c.risk_amount = 0.19, 100.0
    decision, flags = P.run_pipeline(sig(), c, [])
    assert (decision, flags) == ("commanded", {"volume": 0.19,
                                               "risk_amount": 100.0})


# ---------------------------------------------------------------- CooldownPeriod

def test_cooldown_blocks_within_window():
    f = P.CooldownPeriod(enabled=True, cooldown_candles=3)
    c = ctx(last_exit_times={
        "EURUSD": datetime.now(timezone.utc) - timedelta(minutes=30)})
    verdict = f.evaluate(sig(timeframe="M15"), c)
    assert verdict[0] == "block"
    assert verdict[1] == "skipped:cooldown_period"
    assert verdict[3] is not None  # unlock_until carried
    assert verdict[3] > datetime.now(timezone.utc)


def test_cooldown_allows_after_window():
    f = P.CooldownPeriod(enabled=True, cooldown_candles=3)
    c = ctx(last_exit_times={
        "EURUSD": datetime.now(timezone.utc) - timedelta(hours=2)})
    assert f.evaluate(sig(timeframe="M15"), c) == P.ALLOW


def test_cooldown_other_symbol_unaffected():
    f = P.CooldownPeriod(enabled=True, cooldown_candles=3)
    c = ctx(last_exit_times={
        "GBPUSD": datetime.now(timezone.utc) - timedelta(minutes=1)})
    assert f.evaluate(sig(symbol="EURUSD", timeframe="M15"), c) == P.ALLOW


def test_cooldown_no_state_allows():
    f = P.CooldownPeriod(enabled=True)
    assert f.evaluate(sig(), ctx()) == P.ALLOW


def test_cooldown_disabled_is_silent():
    # "disabled" is enforced by the orchestrator: a disabled filter never
    # evaluates, so the pipeline falls through to "commanded".
    f = P.CooldownPeriod(enabled=False)
    c = ctx(last_exit_times={
        "EURUSD": datetime.now(timezone.utc) - timedelta(minutes=1)})
    decision, _ = P.run_pipeline(sig(), c, [f])
    assert decision == "commanded"


def test_cooldown_candle_parsing():
    f = P.CooldownPeriod(enabled=True, cooldown_candles=1)
    assert f._candle_minutes(sig(timeframe="M15"), 60) == 15
    assert f._candle_minutes(sig(timeframe="H1"), 60) == 60
    assert f._candle_minutes(sig(timeframe="H4"), 60) == 240
    assert f._candle_minutes(sig(timeframe="D1"), 60) == 1440
    assert f._candle_minutes(sig(timeframe="X9"), 60) == 60
    assert f._candle_minutes(sig(timeframe=None), 60) == 60


# ---------------------------------------------------------------- StoplossGuard

def _closes(*reasons, symbol="EURUSD"):
    return [{"symbol": symbol, "reason": r} for r in reasons]


def test_stoploss_guard_blocks_after_n_stopouts():
    f = P.StoplossGuard(enabled=True, max_stopouts=3, lookback_trades=10)
    c = ctx(recent_closes=_closes("tp", "sl", "sl", "sl"))
    verdict = f.evaluate(sig(), c)
    assert verdict[0] == "block"
    assert verdict[1] == "skipped:stoploss_guard"
    assert verdict[2]["stopouts"] == 3


def test_stoploss_guard_below_threshold_allows():
    f = P.StoplossGuard(enabled=True, max_stopouts=3, lookback_trades=10)
    c = ctx(recent_closes=_closes("tp", "sl", "sl", "tp"))
    assert f.evaluate(sig(), c) == P.ALLOW


def test_stoploss_guard_lookback_window_slides():
    f = P.StoplossGuard(enabled=True, max_stopouts=2, lookback_trades=3)
    # 2 stop-outs but only 1 inside the last 3 closes -> allow
    c = ctx(recent_closes=_closes("sl", "tp", "tp", "sl", "tp"))
    assert f.evaluate(sig(), c) == P.ALLOW
    # 2 stop-outs inside the last 3 -> block
    c = ctx(recent_closes=_closes("tp", "tp", "sl", "sl"))
    assert f.evaluate(sig(), c)[0] == "block"


def test_stoploss_guard_only_symbol_vs_global():
    closes = _closes("sl", "sl", "sl", symbol="GBPUSD")
    per_symbol = P.StoplossGuard(enabled=True, max_stopouts=3,
                                 only_symbol=True)
    assert per_symbol.evaluate(sig(symbol="EURUSD"), ctx(
        recent_closes=closes)) == P.ALLOW
    glob = P.StoplossGuard(enabled=True, max_stopouts=3, only_symbol=False)
    assert glob.evaluate(sig(symbol="EURUSD"), ctx(
        recent_closes=closes))[0] == "block"


def test_stoploss_guard_reason_matching():
    f = P.StoplossGuard(enabled=True, max_stopouts=2, lookback_trades=10)
    c = ctx(recent_closes=_closes("SL", "Stop_Loss", "manual"))
    assert f.evaluate(sig(), c)[0] == "block"  # case-insensitive
    c = ctx(recent_closes=_closes("tp", "manual", "timeout"))
    assert f.evaluate(sig(), c) == P.ALLOW  # losses that aren't stop-outs


def test_stoploss_guard_no_state_or_disabled():
    f = P.StoplossGuard(enabled=True)
    assert f.evaluate(sig(), ctx()) == P.ALLOW
    f = P.StoplossGuard(enabled=False)
    c = ctx(recent_closes=_closes("sl", "sl", "sl"))
    decision, _ = P.run_pipeline(sig(), c, [f])
    assert decision == "commanded"


# ---------------------------------------------------------------- SessionFilter

def test_session_filter_blocks_configured_session():
    f = P.SessionFilter(enabled=True, session_rules={"GBP*": {"asia"}})
    assert f.evaluate(sig(symbol="GBPUSD"),
                      ctx(session="asia"))[0] == "block"
    assert f.evaluate(sig(symbol="GBPUSD"),
                      ctx(session="london")) == P.ALLOW
    assert f.evaluate(sig(symbol="EURUSD"),
                      ctx(session="asia")) == P.ALLOW


def test_session_filter_block_reason_and_flags():
    f = P.SessionFilter(enabled=True, session_rules={"EUR*": {"late"}})
    verdict = f.evaluate(sig(symbol="EURUSD"), ctx(session="late"))
    assert verdict[1] == "skipped:session_blocked"
    assert verdict[2]["session"] == "late"


def test_session_filter_empty_rules_allow():
    f = P.SessionFilter(enabled=True, session_rules={})
    assert f.evaluate(sig(), ctx(session="asia")) == P.ALLOW


def test_session_filter_derives_session_from_server_now():
    f = P.SessionFilter(enabled=True, session_rules={"*": {"asia"}})
    # server_now 03:00 server time -> "asia" bucket
    server_now = datetime(2026, 10, 15, 3, 0)
    assert f.evaluate(sig(), ctx(server_now=server_now))[0] == "block"
    server_now = datetime(2026, 10, 15, 10, 0)  # london
    assert f.evaluate(sig(), ctx(server_now=server_now)) == P.ALLOW


def test_session_filter_no_session_knowledge_allows():
    f = P.SessionFilter(enabled=True, session_rules={"*": {"asia"}})
    assert f.evaluate(sig(), ctx()) == P.ALLOW


def test_session_filter_disabled_is_silent():
    f = P.SessionFilter(enabled=False, session_rules={"*": {"asia"}})
    decision, _ = P.run_pipeline(sig(), ctx(session="asia"), [f])
    assert decision == "commanded"
