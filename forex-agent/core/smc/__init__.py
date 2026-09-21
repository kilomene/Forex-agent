"""
Chart structure analysis: turns a list of candles into the structured
information a discretionary SMC (Smart Money Concepts) trader would read
off a chart by eye. Every function here is a real, deterministic algorithm
against a stated definition — SMC terminology varies somewhat between
traders, so the exact definition used is documented on each function
rather than assumed to be "the" definition.

Operates on any candle-like object with .time/.open/.high/.low/.close
attributes (duck-typed deliberately, so this module has zero dependency
on MetaTrader5 and is fully testable on any platform — see
test_smc_analysis.py, which runs without Windows or MT5).

Session info is deliberately NOT duplicated here — that's already served
by the Worker's get_session_info tool (src/agent/knowledge.js), no need
for two implementations of the same UTC-clock lookup.
"""

from dataclasses import dataclass, asdict
from typing import List, Optional


@dataclass
class SwingPoint:
    index: int
    kind: str  # "high" | "low"
    price: float
    time: str


def find_swing_points(candles: List, left: int = 2, right: int = 2) -> List[SwingPoint]:
    """
    Fractal-based swing detection: a candle at index i is a swing HIGH if
    its high is strictly greater than the highs of `left` candles before
    and `right` candles after it (a 5-candle fractal by default). Swing
    LOW is the mirror condition on lows. This is the standard fractal
    definition used across most SMC/price-action methodologies.
    """
    swings = []
    n = len(candles)
    for i in range(left, n - right):
        window_highs = [candles[j].high for j in range(i - left, i + right + 1) if j != i]
        window_lows = [candles[j].low for j in range(i - left, i + right + 1) if j != i]

        if all(candles[i].high > h for h in window_highs):
            swings.append(SwingPoint(i, "high", candles[i].high, str(candles[i].time)))
        if all(candles[i].low < l for l in window_lows):
            swings.append(SwingPoint(i, "low", candles[i].low, str(candles[i].time)))

    return swings


def analyze_market_structure(swings: List[SwingPoint]) -> dict:
    """
    Reads the sequence of swing highs and swing lows to classify trend and
    detect structure breaks.

    Definitions used:
      - Uptrend: the last two swing highs are rising AND the last two
        swing lows are rising (higher highs, higher lows).
      - Downtrend: mirror condition (lower highs, lower lows).
      - Ranging: neither condition holds.
      - Bullish structure break: most recent swing high's price is
        exceeded by a later close — this is what SMC traders call a BOS
        (break of structure) confirming upside continuation, or a CHoCH
        (change of character) if it follows a downtrend.
      - Bearish structure break: mirror condition on swing lows.
    """
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    trend = "ranging"
    if len(highs) >= 2 and len(lows) >= 2:
        highs_rising = highs[-1].price > highs[-2].price
        lows_rising = lows[-1].price > lows[-2].price
        highs_falling = highs[-1].price < highs[-2].price
        lows_falling = lows[-1].price < lows[-2].price

        if highs_rising and lows_rising:
            trend = "uptrend"
        elif highs_falling and lows_falling:
            trend = "downtrend"

    return {
        "trend": trend,
        "last_swing_high": asdict(highs[-1]) if highs else None,
        "last_swing_low": asdict(lows[-1]) if lows else None,
    }


def detect_structure_breaks(candles: List, swings: List[SwingPoint]) -> List[dict]:
    """
    Finds points where a candle's close breaks beyond the most recent
    prior swing high (bullish break) or swing low (bearish break) at that
    point in time — walks forward chronologically so each break only
    "knows about" swings that existed before it (no lookahead).
    """
    breaks = []
    highs_so_far: List[SwingPoint] = []
    lows_so_far: List[SwingPoint] = []
    swing_by_index = {s.index: s for s in swings}

    for i, candle in enumerate(candles):
        if i in swing_by_index:
            s = swing_by_index[i]
            (highs_so_far if s.kind == "high" else lows_so_far).append(s)

        if highs_so_far and candle.close > highs_so_far[-1].price:
            breaks.append({"index": i, "direction": "bullish", "level": highs_so_far[-1].price, "time": str(candle.time)})
            highs_so_far.pop()  # that level is now broken, don't re-trigger on it
        if lows_so_far and candle.close < lows_so_far[-1].price:
            breaks.append({"index": i, "direction": "bearish", "level": lows_so_far[-1].price, "time": str(candle.time)})
            lows_so_far.pop()

    return breaks


def detect_trend_lines(swings: List[SwingPoint]) -> dict:
    """
    Simple two-point trend lines: connects the last two swing highs for a
    resistance line and the last two swing lows for a support line, and
    reports the slope. This is a straightforward linear connection, not a
    fitted regression across all points — good enough as a directional
    reference, not a precision tool.
    """
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    def line_from(points):
        if len(points) < 2:
            return None
        p1, p2 = points[-2], points[-1]
        if p2.index == p1.index:
            return None
        slope = (p2.price - p1.price) / (p2.index - p1.index)
        return {"from": asdict(p1), "to": asdict(p2), "slope_per_candle": slope}

    return {"resistance_line": line_from(highs), "support_line": line_from(lows)}


def detect_support_resistance_zones(swings: List[SwingPoint], tolerance_pct: float = 0.1) -> List[dict]:
    """
    Clusters swing highs/lows that occurred at similar price levels into
    zones — a level touched multiple times is a stronger S/R zone than one
    touched once. tolerance_pct is how close (as % of price) two swings
    need to be to count as "the same level".
    """
    zones = []
    for kind in ("high", "low"):
        points = sorted([s for s in swings if s.kind == kind], key=lambda s: s.price)
        cluster: List[SwingPoint] = []
        for p in points:
            if cluster and abs(p.price - cluster[-1].price) / cluster[-1].price * 100 > tolerance_pct:
                zones.append(_zone_from_cluster(cluster, kind))
                cluster = []
            cluster.append(p)
        if cluster:
            zones.append(_zone_from_cluster(cluster, kind))

    return sorted(zones, key=lambda z: -z["touch_count"])


def _zone_from_cluster(cluster: List[SwingPoint], kind: str) -> dict:
    prices = [p.price for p in cluster]
    return {
        "type": "resistance" if kind == "high" else "support",
        "price_low": min(prices),
        "price_high": max(prices),
        "touch_count": len(cluster),
    }


def detect_liquidity_zones(swings: List[SwingPoint], tolerance_pct: float = 0.05) -> dict:
    """
    Equal highs / equal lows — a well-known SMC concept: when price makes
    two or more highs at nearly the same level, retail stop-losses tend to
    cluster just above that level (buy-side liquidity), and the mirror for
    equal lows (sell-side liquidity below). Tighter tolerance than general
    S/R zones since "equal" implies a closer match.
    """
    buy_side = [z for z in detect_support_resistance_zones(
        [s for s in swings if s.kind == "high"], tolerance_pct
    ) if z["touch_count"] >= 2]
    sell_side = [z for z in detect_support_resistance_zones(
        [s for s in swings if s.kind == "low"], tolerance_pct
    ) if z["touch_count"] >= 2]

    return {"buy_side_liquidity": buy_side, "sell_side_liquidity": sell_side}


def detect_fair_value_gaps(candles: List) -> List[dict]:
    """
    Classic 3-candle FVG definition:
      Bullish FVG at candle i: candle[i-1].high < candle[i+1].low
        (a gap between the wick of the candle two back and the wick of the
        next candle, left unfilled by candle i's range)
      Bearish FVG at candle i: candle[i-1].low > candle[i+1].high
    Only the most recent 20 are kept when summarizing for the wire (see
    analyze()) to avoid an ever-growing payload.
    """
    gaps = []
    for i in range(1, len(candles) - 1):
        prev, nxt = candles[i - 1], candles[i + 1]
        if prev.high < nxt.low:
            gaps.append({"type": "bullish", "index": i, "bottom": prev.high, "top": nxt.low, "time": str(candles[i].time)})
        if prev.low > nxt.high:
            gaps.append({"type": "bearish", "index": i, "bottom": nxt.high, "top": prev.low, "time": str(candles[i].time)})
    return gaps


def detect_order_blocks(candles: List, structure_breaks: List[dict]) -> List[dict]:
    """
    Classic order block definition: the last opposite-colored candle
    before an impulsive move that causes a structure break.
      Bullish order block: the last bearish (red) candle before an up-move
        that produces a bullish structure break.
      Bearish order block: the last bullish (green) candle before a
        down-move that produces a bearish structure break.
    Scans backward from each structure-break candle to find that candle.
    """
    blocks = []
    for brk in structure_breaks:
        break_index = brk["index"]
        wanted_color = "bearish" if brk["direction"] == "bullish" else "bullish"

        for j in range(break_index, max(break_index - 15, -1), -1):
            c = candles[j]
            is_bearish = c.close < c.open
            is_bullish = c.close > c.open
            if (wanted_color == "bearish" and is_bearish) or (wanted_color == "bullish" and is_bullish):
                blocks.append({
                    "type": "bullish" if brk["direction"] == "bullish" else "bearish",
                    "index": j,
                    "high": c.high,
                    "low": c.low,
                    "time": str(c.time),
                    "caused_break_at_index": break_index,
                })
                break

    return blocks


def analyze(candles: List, swing_left: int = 2, swing_right: int = 2) -> dict:
    """
    Runs the full analysis pipeline and returns a compact summary safe to
    send over the wire — only the most recent/relevant items from each
    category, not the full history, so the payload stays small regardless
    of how many candles were analyzed.
    """
    if len(candles) < (swing_left + swing_right + 3):
        return {"available": False, "note": "Not enough candles for structure analysis."}

    swings = find_swing_points(candles, swing_left, swing_right)
    structure = analyze_market_structure(swings)
    breaks = detect_structure_breaks(candles, swings)
    trend_lines = detect_trend_lines(swings)
    sr_zones = detect_support_resistance_zones(swings)
    liquidity = detect_liquidity_zones(swings)
    fvgs = detect_fair_value_gaps(candles)
    order_blocks = detect_order_blocks(candles, breaks)

    current_price = candles[-1].close
    nearby_sr = [z for z in sr_zones if abs(current_price - (z["price_low"] + z["price_high"]) / 2) / current_price < 0.02]

    return {
        "available": True,
        "market_structure": structure,
        "recent_structure_breaks": breaks[-5:],
        "trend_lines": trend_lines,
        "support_resistance_near_price": nearby_sr[:5],
        "liquidity_zones": {
            "buy_side": liquidity["buy_side_liquidity"][:3],
            "sell_side": liquidity["sell_side_liquidity"][:3],
        },
        "fair_value_gaps_recent": fvgs[-5:],
        "order_blocks_recent": order_blocks[-5:],
    }
