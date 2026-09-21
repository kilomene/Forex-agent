"""Tests for intelligence.experience + intelligence.reflections."""

import os
import tempfile

from intelligence.experience import ExperienceStore
from intelligence.reflections.outcome import compute_outcome
from intelligence.reflections.reflect import build_performance, record_reflection
from storage import Store


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return Store(path)


# -- compute_outcome: hand-computed values ---------------------------------
def test_compute_outcome_buy_win():
    assert compute_outcome("BUY", 1.0800, 1.0850) == "win"


def test_compute_outcome_buy_loss():
    assert compute_outcome("BUY", 1.0800, 1.0750) == "loss"


def test_compute_outcome_sell_win():
    assert compute_outcome("SELL", 1.0800, 1.0750) == "win"


def test_compute_outcome_sell_loss():
    assert compute_outcome("SELL", 1.0800, 1.0850) == "loss"


def test_compute_outcome_breakeven():
    assert compute_outcome("BUY", 1.0800, 1.0800) == "breakeven"
    assert compute_outcome("SELL", 1.0800, 1.0800) == "breakeven"


def test_compute_outcome_bad_direction():
    try:
        compute_outcome("HOLD", 1.0, 1.0)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid direction must raise ValueError")


# -- performance math -------------------------------------------------------
def test_build_performance_r_multiple():
    # BUY 1.0800, SL 1.0750 (risk 50 pips), TP 1.0900; closed 1.0850 -> +1R.
    perf = build_performance(
        direction="BUY", entry_price=1.0800, stop_loss=1.0750,
        take_profit=1.0900, closed_price=1.0850,
    )
    assert perf["outcome"] == "win"
    assert abs(perf["price_diff"] - 0.0050) < 1e-9
    assert abs(perf["r_multiple"] - 1.0) < 1e-9
    assert abs(perf["planned_rr"] - 2.0) < 1e-9


# -- reflection pipeline -----------------------------------------------------
def test_record_reflection_pipeline():
    s = _store()
    exp = ExperienceStore(s)
    signal = {
        "symbol": "EURUSD", "direction": "BUY", "strategy": "ema_rsi",
        "entry_price": 1.0800, "stop_loss": 1.0750, "take_profit": 1.0900,
        "candle_time": "2026-09-21T10:00:00+00:00",
        "trigger": "EMA20 crossed above EMA50, RSI=55.0",
    }
    rec = record_reflection(exp, signal, closed_price=1.0850, close_reason="take_profit")
    assert rec["outcome"] == "win"
    assert rec["symbol"] == "EURUSD"
    assert rec["performance"]["outcome"] == "win"
    assert "pending external-agent reflection" in rec["reflection_text"]

    # Persisted to the local journal as kind="reflection", queryable.
    rows = exp.query(kind="reflection", symbol="EURUSD")
    assert len(rows) == 1
    assert rows[0]["detail"]["outcome"] == "win"

    # Agent-supplied review text is stored verbatim, never invented.
    rec2 = record_reflection(
        exp, signal, closed_price=1.0740,
        reflection_text="Ignored bearish order block; RSI momentum was not enough.",
    )
    assert rec2["outcome"] == "loss"
    assert rec2["reflection_text"].startswith("Ignored bearish")
    s.close()


# -- experience store ----------------------------------------------------------
def test_experience_kinds_and_query():
    s = _store()
    exp = ExperienceStore(s)
    exp.record("regime", symbol="EURUSD", payload={"session": "london/new_york", "volatility": "high"})
    exp.record("failure", symbol="GBPUSD", payload={"what": "data gap", "candles_missing": 12})
    exp.record("pattern", symbol="EURUSD", payload={"note": "london open breakout held"})
    exp.record_trade(symbol="EURUSD", direction="SELL",
                     entry_price=1.0850, closed_price=1.0800)
    assert len(exp.query()) == 4
    assert len(exp.query(kind="trade")) == 1
    assert exp.query(kind="trade")[0]["outcome"] == "win"
    assert len(exp.query(symbol="EURUSD")) == 3
    try:
        exp.record("dream", payload={})
    except ValueError:
        pass
    else:
        raise AssertionError("unknown kind must raise ValueError")
    s.close()
