"""
Signal dataclass, lifecycle, and dedup.

A Signal is evidence of a strategy firing — it is NOT a trade. The path
to a trade is: Signal -> (agent/Worker approval) -> Execution Gateway ->
BrokerAdapter. Nothing here touches a broker.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# Lifecycle states mirror the Worker D1 `signals.status` column so local
# state and cloud state stay comparable.
class SignalStatus:
    DETECTED = "detected"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass
class Signal:
    symbol: str
    timeframe: str
    direction: str  # "BUY" or "SELL"
    entry_price: float
    stop_loss: float
    take_profit: float
    ema_fast: float
    ema_slow: float
    rsi_value: float
    atr_value: float
    candle_time: datetime
    trigger: str  # short machine description of what fired
    strategy: str = "ema_rsi"
    id: Optional[str] = None
    status: str = SignalStatus.DETECTED
    created_at: Optional[datetime] = None
    recent_candles: Optional[List[dict]] = None  # last N CLOSED OHLC candles, for the agent's tools
    smc_summary: Optional[dict] = None  # market structure / FVG / order blocks / liquidity zones
    symbol_specs: Optional[dict] = None  # broker tick_value/tick_size/contract_size/volume limits

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = datetime.now()
        if isinstance(self.candle_time, str):
            self.candle_time = datetime.fromisoformat(self.candle_time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("candle_time", "created_at"):
            v = d.get(k)
            d[k] = v.isoformat() if isinstance(v, datetime) else v
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Signal":
        kwargs = dict(d)
        for k in ("candle_time", "created_at"):
            if isinstance(kwargs.get(k), str):
                kwargs[k] = datetime.fromisoformat(kwargs[k])
        # Tolerate extra keys from the Worker payload.
        valid = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in kwargs.items() if k in valid}
        return cls(**kwargs)


class SignalDedup:
    """
    Port of main.py's `_last_signal_time` dedup: one signal per
    (symbol, candle_time). Prevents re-reporting the same candle's
    signal on every scan loop iteration.

    Because strategies now evaluate only CLOSED candles, candle_time is
    the last closed candle's time — stable, never a forming candle that
    could re-fire intra-candle (the original repaint vector).
    """

    def __init__(self) -> None:
        self._last_signal_time: Dict[str, datetime] = {}

    def is_duplicate(self, symbol: str, candle_time: datetime) -> bool:
        return self._last_signal_time.get(symbol) == candle_time

    def mark(self, symbol: str, candle_time: datetime) -> None:
        self._last_signal_time[symbol] = candle_time

    def check_and_mark(self, symbol: str, candle_time: datetime) -> bool:
        """Returns True if this (symbol, candle_time) was already seen."""
        if self.is_duplicate(symbol, candle_time):
            return True
        self.mark(symbol, candle_time)
        return False

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._last_signal_time.clear()
        else:
            self._last_signal_time.pop(symbol, None)
