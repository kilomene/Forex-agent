"""
Ported from the original bridge's test_smc_analysis.py — verifies
core/smc against synthetic candle sequences with KNOWN, hand-constructed
patterns. No broker dependency; runs anywhere.
"""

import random
from collections import namedtuple

from core.smc import (
    analyze,
    detect_fair_value_gaps,
    detect_order_blocks,
    detect_structure_breaks,
    find_swing_points,
)

Candle = namedtuple("Candle", ["time", "open", "high", "low", "close"])


def make_candle(i, o, h, l, c):
    return Candle(time=f"t{i}", open=o, high=h, low=l, close=c)


def test_swing_detection():
    # Construct a clean fractal: candle 5 is a swing high (higher than 2 either side)
    candles = [make_candle(i, 1.0, 1.0 + 0.001 * i, 1.0 - 0.001 * i, 1.0) for i in range(11)]
    # Force index 5 to be a clear swing high
    candles[5] = make_candle(5, 1.0, 1.05, 0.95, 1.0)

    swings = find_swing_points(candles, left=2, right=2)
    high_indices = [s.index for s in swings if s.kind == "high"]
    assert 5 in high_indices, f"Expected index 5 to be detected as swing high, got highs at {high_indices}"


def test_fair_value_gap_bullish():
    # candle[0].high=1.10, candle[2].low=1.15 -> gap [1.10, 1.15], bullish FVG at index 1
    candles = [
        make_candle(0, 1.08, 1.10, 1.07, 1.09),
        make_candle(1, 1.12, 1.16, 1.11, 1.15),  # impulsive middle candle
        make_candle(2, 1.16, 1.18, 1.15, 1.17),
    ]
    gaps = detect_fair_value_gaps(candles)
    assert len(gaps) == 1 and gaps[0]["type"] == "bullish", f"Expected 1 bullish FVG, got {gaps}"
    assert gaps[0]["bottom"] == 1.10 and gaps[0]["top"] == 1.15


def test_fair_value_gap_bearish():
    # candle[0].low=1.20, candle[2].high=1.15 -> gap, bearish FVG at index 1
    candles = [
        make_candle(0, 1.22, 1.23, 1.20, 1.21),
        make_candle(1, 1.18, 1.19, 1.14, 1.15),
        make_candle(2, 1.14, 1.15, 1.13, 1.14),
    ]
    gaps = detect_fair_value_gaps(candles)
    assert len(gaps) == 1 and gaps[0]["type"] == "bearish", f"Expected 1 bearish FVG, got {gaps}"
    # gap between candle[0].low=1.20 and candle[2].high=1.15
    assert gaps[0]["bottom"] == 1.15 and gaps[0]["top"] == 1.20


def test_no_fvg_when_no_gap():
    # Overlapping ranges, no true gap
    candles = [
        make_candle(0, 1.10, 1.12, 1.09, 1.11),
        make_candle(1, 1.11, 1.13, 1.10, 1.12),
        make_candle(2, 1.11, 1.14, 1.10, 1.13),
    ]
    gaps = detect_fair_value_gaps(candles)
    assert len(gaps) == 0, f"Expected no FVG for overlapping candles, got {gaps}"


def test_structure_break_and_order_block():
    # Build: a confirmed swing high at index 3 (needs calmer candles after
    # it to confirm the fractal), then consolidation, then a bearish candle
    # right before a strong bullish impulsive candle that closes above the
    # swing high -> bullish structure break, with that bearish candle
    # identified as the order block.
    candles = [
        make_candle(0, 1.00, 1.01, 0.99, 1.00),
        make_candle(1, 1.00, 1.02, 0.99, 1.01),
        make_candle(2, 1.01, 1.03, 1.00, 1.02),
        make_candle(3, 1.02, 1.06, 1.01, 1.03),  # swing high candidate (high=1.06)
        make_candle(4, 1.03, 1.04, 1.02, 1.025),  # lower high, confirms right side
        make_candle(5, 1.025, 1.03, 1.01, 1.02),  # lower high, confirms right side
        make_candle(6, 1.02, 1.035, 1.00, 1.01),  # consolidation
        make_candle(7, 1.01, 1.025, 0.995, 1.005),  # consolidation
        make_candle(8, 1.005, 1.01, 0.98, 0.985),  # bearish candle right before impulse
        make_candle(9, 0.985, 1.09, 0.98, 1.08),  # strong bullish impulse, closes above 1.06
    ]
    swings = find_swing_points(candles, left=2, right=2)
    breaks = detect_structure_breaks(candles, swings)
    bullish_breaks = [b for b in breaks if b["direction"] == "bullish"]
    assert len(bullish_breaks) >= 1, f"Expected at least one bullish structure break, got {breaks}"

    blocks = detect_order_blocks(candles, breaks)
    bullish_blocks = [b for b in blocks if b["type"] == "bullish"]
    assert any(b["index"] == 8 for b in bullish_blocks), f"Expected order block at index 8, got {blocks}"


def test_analyze_end_to_end_no_crash():
    # Enough candles for a full analyze() run, just checking it produces a
    # well-formed summary without crashing on a somewhat realistic series.
    random.seed(42)
    price = 1.1000
    candles = []
    for i in range(60):
        o = price
        move = random.uniform(-0.0015, 0.0015)
        c = o + move
        h = max(o, c) + random.uniform(0, 0.0008)
        l = min(o, c) - random.uniform(0, 0.0008)
        candles.append(make_candle(i, o, h, l, c))
        price = c

    result = analyze(candles)
    assert result["available"] is True
    assert "market_structure" in result and "trend" in result["market_structure"]
    assert isinstance(result["fair_value_gaps_recent"], list)
    assert isinstance(result["order_blocks_recent"], list)
