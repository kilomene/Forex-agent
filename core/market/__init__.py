"""
Market data primitives: Candle + OHLC handling.

CLOSED-CANDLE CONTRACT (the repaint fix, enforced at the seams):
  * BrokerAdapter.candles() returns CLOSED candles only — the adapter
    fetches one extra candle and drops the still-forming one.
  * Defense in depth: strategy evaluation ALSO treats the final element
    of any candle list it receives as forming and excludes it from
    signal logic (see core.strategies). So even a raw feed handed
    straight to a strategy cannot produce a signal off an incomplete
    candle.

Every candle list in core is ordered oldest -> newest.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from core.indicators import ema


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "time": self.time.isoformat() if isinstance(self.time, datetime) else str(self.time),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Candle":
        t = d["time"]
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        return cls(
            time=t,
            open=float(d["open"]),
            high=float(d["high"]),
            low=float(d["low"]),
            close=float(d["close"]),
            volume=int(d.get("volume", 0)),
        )


def closed_only(candles: Sequence[Candle]) -> List[Candle]:
    """
    Drop the final candle, which is treated as still-forming.

    Call this on any raw candle feed before making trading decisions.
    Data sourced via BrokerAdapter.candles() is already closed-only, so
    calling this twice is harmless (it would drop one extra closed
    candle — callers must know which they have; the convention is:
    adapter output = closed, raw broker feed = call this once).
    """
    return list(candles[:-1])


def closes(candles: Sequence[Candle]) -> List[float]:
    return [c.close for c in candles]


def highs(candles: Sequence[Candle]) -> List[float]:
    return [c.high for c in candles]


def lows(candles: Sequence[Candle]) -> List[float]:
    return [c.low for c in candles]


def is_bullish(c: Candle) -> bool:
    return c.close > c.open


def is_bearish(c: Candle) -> bool:
    return c.close < c.open


def build_chart_payload(
    candles: Sequence[Candle], ema_fast_period: int, ema_slow_period: int
) -> Dict[str, Any]:
    """
    Pure OHLC+EMA payload packaging (ported from chart_data.py, data
    source rewired to BrokerAdapter candles).

    Expects CLOSED candles as returned by BrokerAdapter.candles().
    Duck-typed on .time/.open/.high/.low/.close like the original, so it
    stays testable without a broker.
    """
    closes_ = [c.close for c in candles]
    ema_fast_series = ema(closes_, ema_fast_period)
    ema_slow_series = ema(closes_, ema_slow_period)

    candle_dicts = [
        {
            "time": c.time.isoformat() if isinstance(c.time, datetime) else str(c.time),
            "open": c.open, "high": c.high, "low": c.low, "close": c.close,
        }
        for c in candles
    ]

    return {
        "candles": candle_dicts,
        "ema_fast": ema_fast_series,
        "ema_slow": ema_slow_series,
        "ema_fast_period": ema_fast_period,
        "ema_slow_period": ema_slow_period,
    }
