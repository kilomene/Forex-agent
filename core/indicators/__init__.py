"""
Real technical indicator implementations operating on plain lists of floats.
No TA library dependency — these are the standard formulas, hand-verified,
so you know exactly what's driving a signal.

All functions take OHLC data as lists (oldest -> newest) and return
lists aligned to the input (padded with None where not enough data exists yet).
"""

from typing import List, Optional


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """Exponential moving average. Standard smoothing factor = 2/(period+1)."""
    if len(values) < period:
        return [None] * len(values)

    result: List[Optional[float]] = [None] * (period - 1)
    multiplier = 2 / (period + 1)

    # Seed with SMA of the first `period` values
    sma_seed = sum(values[:period]) / period
    result.append(sma_seed)

    prev = sma_seed
    for price in values[period:]:
        current = (price - prev) * multiplier + prev
        result.append(current)
        prev = current

    return result


def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    """Relative Strength Index using Wilder's smoothing (the standard method)."""
    if len(values) < period + 1:
        return [None] * len(values)

    result: List[Optional[float]] = [None] * period
    gains = []
    losses = []

    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calc_rsi(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        rs = ag / al
        return 100 - (100 / (1 + rs))

    result.append(calc_rsi(avg_gain, avg_loss))

    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result.append(calc_rsi(avg_gain, avg_loss))

    return result


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[Optional[float]]:
    """Average True Range using Wilder's smoothing. Used for stop-loss sizing."""
    n = len(closes)
    if n < period + 1:
        return [None] * n

    true_ranges = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    result: List[Optional[float]] = [None] * period
    avg = sum(true_ranges[1:period + 1]) / period
    result.append(avg)

    for i in range(period + 1, n):
        avg = (avg * (period - 1) + true_ranges[i]) / period
        result.append(avg)

    return result
