"""
Ported from the original bridge's test_indicators.py — sanity tests for
core/indicators against hand-checkable values. No broker dependency;
runs anywhere.
"""

from core.indicators import atr, ema, rsi


def test_ema_basic():
    # Simple rising sequence: SMA seed then EMA smoothing
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    result = ema(values, period=3)
    assert result[0] is None and result[1] is None
    assert abs(result[2] - 2.0) < 1e-9  # SMA(1,2,3) = 2
    # EMA(4) with period 3: multiplier=0.5 -> (4-2)*0.5+2 = 3.0
    assert abs(result[3] - 3.0) < 1e-9


def test_rsi_all_gains():
    # Strictly increasing prices -> RSI should approach 100
    values = list(range(1, 20))
    result = rsi(values, period=14)
    assert result[-1] > 95


def test_rsi_all_losses():
    values = list(range(20, 1, -1))
    result = rsi(values, period=14)
    assert result[-1] < 5


def test_rsi_flat():
    values = [10] * 20
    result = rsi(values, period=14)
    # No gains or losses at all -> defined as 100 by this implementation's
    # zero-loss convention (avg_loss == 0)
    assert result[-1] == 100.0


def test_atr_basic():
    highs = [10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 14, 13, 15, 16, 17]
    lows = [9, 9, 10, 9, 11, 12, 11, 13, 14, 13, 12, 11, 13, 14, 15]
    closes = [9.5, 10, 11, 10, 12, 13, 12, 14, 15, 14, 13, 12, 14, 15, 16]
    result = atr(highs, lows, closes, period=14)
    assert result[-1] is not None and result[-1] > 0
