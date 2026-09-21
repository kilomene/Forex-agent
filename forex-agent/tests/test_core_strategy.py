"""
Strategy tests, including the forming-candle repaint regression.

The original bridge's signal_engine.evaluate() indexed candles[-1] while
claiming "most recently CLOSED candle" — but MT5's copy_rates_from_pos
returns the still-forming candle last, so signals could appear and
vanish intra-candle. EmaRsiStrategy must NEVER emit (or suppress) a
signal because of the forming candle.
"""

from datetime import datetime, timedelta

from config import EmaRsiStrategyConfig
from core.market import Candle
from core.strategies import EmaRsiStrategy, Strategy


def _cfg():
    return EmaRsiStrategyConfig(
        ema_fast=5, ema_slow=10, rsi_period=7, atr_period=7,
        rsi_overbought=70.0, rsi_oversold=30.0,
        atr_sl_multiplier=1.5, atr_tp_multiplier=3.0,
    )


def _mk(i, c):
    return Candle(
        time=datetime(2026, 1, 1) + timedelta(minutes=15 * i),
        open=c, high=c + 0.2, low=c - 0.2, close=c, volume=10,
    )


# Decline then sharp reversal; the EMA5/EMA10 cross confirms on the
# closed candle at index 17 (close 103.0, RSI 65 < 70).
CLOSES = [105.0, 104.6, 104.2, 103.8, 103.4, 103.0, 102.6, 102.2, 101.8,
          101.4, 101.0, 100.6, 100.2, 100.0, 100.3, 100.9, 101.8, 103.0,
          104.5, 106.0]


def _candles(n, forming_close):
    cs = [_mk(i, c) for i, c in enumerate(CLOSES[:n])]
    cs.append(_mk(999, forming_close))  # the forming candle
    return cs


def test_strategy_interface_contract():
    assert issubclass(EmaRsiStrategy, Strategy)
    assert hasattr(EmaRsiStrategy, "evaluate")


def test_confirmed_cross_on_closed_candle_emits_signal():
    strat = EmaRsiStrategy(_cfg(), timeframe="M15")
    candles = _candles(18, forming_close=103.2)  # closed through idx 17
    sig = strat.evaluate("EURUSD", candles)
    assert sig is not None
    assert sig.direction == "BUY"
    assert sig.entry_price == 103.0
    # The signal is keyed to the last CLOSED candle — never the forming one.
    assert sig.candle_time == candles[-2].time
    assert sig.candle_time != candles[-1].time
    assert sig.stop_loss < sig.entry_price < sig.take_profit
    assert len(sig.recent_candles) <= 30
    assert sig.smc_summary is not None and sig.smc_summary["available"] is True


def test_no_signal_when_cross_exists_only_on_forming_candle():
    """Repaint regression: a violent up-spike on the FORMING candle would
    read as a fresh cross under the old candles[-1] indexing. The fixed
    strategy must return None — the cross is unconfirmed."""
    strat = EmaRsiStrategy(_cfg(), timeframe="M15")
    candles = _candles(17, forming_close=115.0)  # closed through idx 16, no cross there
    assert strat.evaluate("EURUSD", candles) is None


def test_signal_invariant_to_forming_candle():
    """Same closed history with two wildly different forming candles must
    produce the identical signal (or identical None)."""
    strat = EmaRsiStrategy(_cfg(), timeframe="M15")
    a = _candles(18, forming_close=103.2)
    b = _candles(18, forming_close=140.0)
    sig_a = strat.evaluate("EURUSD", a)
    sig_b = strat.evaluate("EURUSD", b)
    assert (sig_a is None) == (sig_b is None)
    if sig_a is not None:
        assert sig_a.direction == sig_b.direction
        assert sig_a.candle_time == sig_b.candle_time
        assert sig_a.entry_price == sig_b.entry_price


def test_no_signal_on_flat_market():
    strat = EmaRsiStrategy(_cfg(), timeframe="M15")
    candles = [_mk(i, 100.0) for i in range(30)] + [_mk(999, 100.0)]
    assert strat.evaluate("EURUSD", candles) is None


def test_not_enough_candles_returns_none():
    strat = EmaRsiStrategy(_cfg(), timeframe="M15")
    candles = [_mk(i, 100.0 + i * 0.1) for i in range(8)] + [_mk(999, 101.0)]
    assert strat.evaluate("EURUSD", candles) is None
