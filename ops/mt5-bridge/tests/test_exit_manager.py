"""Hermetic unit tests for exit_manager.py (no MT5, no network)."""
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import exit_manager as em  # noqa: E402

NOW = 1_790_160_000.0  # fixed clock for determinism


def pos(ticket=1, symbol="EURUSD", direction="BUY", volume=1.0,
        open_price=1.1000, current_price=1.1000, sl=1.0900, tp=1.1200,
        open_time=None):
    return {
        "ticket": ticket, "symbol": symbol, "direction": direction,
        "volume": volume, "open_price": open_price,
        "current_price": current_price, "profit": 0.0,
        "sl": sl, "tp": tp,
        "open_time": NOW - 3600 if open_time is None else open_time,
    }


def test_trailing_stop_arms_past_2r_and_fires_on_pullback():
    # BUY: risk distance = 1.1000-1.0900 = 0.0100 (100 pips).
    p = pos(current_price=1.1250)  # +250 pips = +2.5R -> arms trailing
    cmds, state = em.evaluate_exits([p], now=NOW, state={})
    assert cmds == []  # armed but price still above the trail level
    assert state["1"]["armed"] is True
    assert state["1"]["peak_r"] == pytest.approx(2.5)

    # Price pulls back to +1.2R, below trail level (peak 2.5R - lock 1R = 1.5R).
    p2 = pos(current_price=1.1120)
    cmds2, _ = em.evaluate_exits([p2], now=NOW, state=state)
    assert len(cmds2) == 1
    cmd = cmds2[0]
    assert cmd["type"] == "trade.close"
    assert cmd["position_id"] == 1
    assert cmd["reason"] == "trailing_stop"
    assert cmd["id"].startswith("exit-trailing_stop-1-")


def test_no_trailing_before_trigger():
    p = pos(current_price=1.1150)  # +1.5R < +2R trigger
    cmds, state = em.evaluate_exits([p], now=NOW, state={})
    assert cmds == []
    assert state["1"]["armed"] is False


def test_trailing_stop_sell_direction():
    # SELL: risk = 1.1000-1.0900... use sl above entry for a SELL.
    p = pos(direction="SELL", open_price=1.1000, sl=1.1100,
            current_price=1.0750)  # +2.5R in favor
    cmds, state = em.evaluate_exits([p], now=NOW, state={})
    assert cmds == []
    assert state["1"]["armed"] is True
    p2 = pos(direction="SELL", open_price=1.1000, sl=1.1100,
             current_price=1.0880)  # +1.2R -> below trail level
    cmds2, _ = em.evaluate_exits([p2], now=NOW, state=state)
    assert len(cmds2) == 1
    assert cmds2[0]["reason"] == "trailing_stop"


def test_max_holding_time_fires():
    p = pos(open_time=NOW - 73 * 3600)  # 73h > 72h default
    cmds, _ = em.evaluate_exits([p], now=NOW, state={})
    assert len(cmds) == 1
    assert cmds[0]["reason"] == "max_holding_time"
    assert cmds[0]["type"] == "trade.close"


def test_max_holding_time_not_before_limit():
    p = pos(open_time=NOW - 71 * 3600)
    cmds, _ = em.evaluate_exits([p], now=NOW, state={})
    assert cmds == []


def test_emergency_exit_closes_all():
    positions = [pos(ticket=1), pos(ticket=2), pos(ticket=3)]
    cmds, _ = em.evaluate_exits(positions, now=NOW, state={},
                                kill_switch_on=True)
    assert len(cmds) == 3
    assert {c["position_id"] for c in cmds} == {1, 2, 3}
    assert all(c["reason"] == "emergency_exit" for c in cmds)
    assert all(c["type"] == "trade.close" for c in cmds)


def test_emergency_drawdown_closes_all():
    positions = [pos(ticket=7)]
    cmds, _ = em.evaluate_exits(positions, now=NOW, state={},
                                emergency_drawdown=True)
    assert len(cmds) == 1
    assert cmds[0]["reason"] == "emergency_exit"


def test_strategy_invalidation_callback():
    def inval(position, ctx):
        if position["symbol"] == "EURUSD":
            return "news_blackout"
        return None
    cmds, _ = em.evaluate_exits([pos(ticket=1), pos(ticket=2, symbol="GBPUSD")],
                                now=NOW, state={}, invalidation=inval)
    assert len(cmds) == 1
    assert cmds[0]["position_id"] == 1
    assert cmds[0]["reason"] == "strategy_invalidated"


def test_exits_are_commands_not_claims():
    # evaluate_exits is pure: it returns command REQUESTS and never asserts
    # a closure. Commands carry the EA's trade.close shape.
    p = pos(ticket=42, open_time=NOW - 80 * 3600)
    cmds, _ = em.evaluate_exits([p], now=NOW, state={})
    assert len(cmds) == 1
    cmd = cmds[0]
    assert set(cmd) >= {"type", "id", "position_id", "reason"}
    assert cmd["type"] == "trade.close"
    assert "closed" not in cmd["type"]
    # No 'trade.closed' claim anywhere in the output.
    assert not any("trade.closed" in str(c) for c in cmds)


def test_missing_sl_fails_closed_for_trailing():
    # No broker SL -> R undefined -> no trailing decision (fail-closed).
    p = pos(current_price=1.1500, sl=0)  # huge profit but no risk distance
    cmds, state = em.evaluate_exits([p], now=NOW, state={})
    assert cmds == []
    assert state["1"]["armed"] is False


def test_state_pruned_for_closed_tickets():
    state = {"1": {"peak_r": 2.5, "armed": True},
             "999": {"peak_r": 3.0, "armed": True}}
    _, new_state = em.evaluate_exits([pos(ticket=1)], now=NOW, state=state)
    assert "1" in new_state
    assert "999" not in new_state


def test_command_ids_unique_per_cycle():
    p = pos(ticket=5, open_time=NOW - 80 * 3600)
    cmds, _ = em.evaluate_exits([p], now=NOW, state={})
    cmds2, _ = em.evaluate_exits([p], now=NOW + 5, state={})
    assert cmds[0]["id"] != cmds2[0]["id"]
