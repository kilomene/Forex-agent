"""
Strategy interface + the EMA/RSI strategy (ported from signal_engine.py).

CLOSED-CANDLE CONTRACT (repaint fix):
    The final candle in the input list is ALWAYS treated as forming and
    EXCLUDED from signal logic. A cross is only evaluated on the last two
    CLOSED candles. This fixes the original bridge's repaint bug, where
    evaluate() indexed candles[-1] — the still-forming candle from
    copy_rates_from_pos — so a signal could appear and vanish intra-candle
    and the Worker could be notified of crosses that never confirmed.

    Callers: pass the raw feed; the strategy drops the forming candle
    itself (defense in depth — BrokerAdapter.candles() already returns
    closed-only, so adapter-fed callers lose nothing).
"""

import abc
import logging
from typing import List, Optional, Sequence

from config import EmaRsiStrategyConfig
from core.indicators import atr as atr_series
from core.indicators import ema as ema_series
from core.indicators import rsi as rsi_series
from core.market import Candle, closed_only
from core.signals import Signal
from core.smc import analyze as analyze_smc

logger = logging.getLogger("strategies")


class Strategy(abc.ABC):
    """A deterministic signal generator.

    evaluate(symbol, candles) -> Signal | None

    Contract:
      * `candles` is ordered oldest -> newest; the LAST element is
        treated as forming and excluded from signal logic.
      * Returns a Signal only on a fresh trigger on closed candles,
        else None.
      * Pure w.r.t. the market: no broker calls, no I/O, no randomness.
    """

    name: str = "base"

    @abc.abstractmethod
    def evaluate(self, symbol: str, candles: Sequence[Candle]) -> Optional[Signal]:
        ...


class EmaRsiStrategy(Strategy):
    """
    Port of the original signal_engine.evaluate(): EMA(fast/slow) cross
    + RSI filter -> Signal with ATR-based SL/TP; attaches recent closed
    candles + SMC summary as evidence.

    Entry logic (on closed candles only):
      BUY  -> EMA(fast) crosses above EMA(slow) AND RSI not overbought
      SELL -> EMA(fast) crosses below EMA(slow) AND RSI not oversold
    """

    name = "ema_rsi"

    def __init__(self, cfg: EmaRsiStrategyConfig, timeframe: str = "H1"):
        self.cfg = cfg
        self.timeframe = timeframe

    def evaluate(self, symbol: str, candles: Sequence[Candle]) -> Optional[Signal]:
        cfg = self.cfg
        # THE FIX: never evaluate the forming candle.
        closed = closed_only(candles)

        closes = [c.close for c in closed]
        highs = [c.high for c in closed]
        lows = [c.low for c in closed]

        if len(closes) < max(cfg.ema_slow, cfg.rsi_period, cfg.atr_period) + 2:
            logger.debug("%s: not enough candles yet", symbol)
            return None

        ema_fast_s = ema_series(closes, cfg.ema_fast)
        ema_slow_s = ema_series(closes, cfg.ema_slow)
        rsi_s = rsi_series(closes, cfg.rsi_period)
        atr_s = atr_series(highs, lows, closes, cfg.atr_period)

        # Last two FULLY CLOSED candles — i/j index the closed window,
        # so closed[-1] is the last confirmed candle, never forming.
        i, prev_i = -1, -2

        fast_now, fast_prev = ema_fast_s[i], ema_fast_s[prev_i]
        slow_now, slow_prev = ema_slow_s[i], ema_slow_s[prev_i]
        rsi_now = rsi_s[i]
        atr_now = atr_s[i]

        if None in (fast_now, fast_prev, slow_now, slow_prev, rsi_now, atr_now):
            return None

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        entry_price = closes[i]
        candle_time = closed[i].time

        # Last 30 CLOSED candles as plain dicts, for the agent's tools.
        # Real OHLC, not a placeholder.
        recent = closed[-30:] if len(closed) >= 30 else closed
        recent_candles = [
            {
                "time": c.time.isoformat(),
                "open": c.open, "high": c.high, "low": c.low, "close": c.close,
            }
            for c in recent
        ]

        # SMC structure analysis on the full closed window (more context
        # than just the last 30) so swing points/structure breaks have
        # enough history to be meaningfully confirmed.
        smc_summary = analyze_smc(closed)

        if crossed_up and rsi_now < cfg.rsi_overbought:
            sl = entry_price - atr_now * cfg.atr_sl_multiplier
            tp = entry_price + atr_now * cfg.atr_tp_multiplier
            return Signal(
                symbol=symbol, timeframe=self.timeframe, direction="BUY",
                entry_price=entry_price, stop_loss=sl, take_profit=tp,
                ema_fast=fast_now, ema_slow=slow_now, rsi_value=rsi_now, atr_value=atr_now,
                candle_time=candle_time, strategy=self.name,
                trigger=f"EMA{cfg.ema_fast} crossed above EMA{cfg.ema_slow}, RSI={rsi_now:.1f}",
                recent_candles=recent_candles,
                smc_summary=smc_summary,
            )

        if crossed_down and rsi_now > cfg.rsi_oversold:
            sl = entry_price + atr_now * cfg.atr_sl_multiplier
            tp = entry_price - atr_now * cfg.atr_tp_multiplier
            return Signal(
                symbol=symbol, timeframe=self.timeframe, direction="SELL",
                entry_price=entry_price, stop_loss=sl, take_profit=tp,
                ema_fast=fast_now, ema_slow=slow_now, rsi_value=rsi_now, atr_value=atr_now,
                candle_time=candle_time, strategy=self.name,
                trigger=f"EMA{cfg.ema_fast} crossed below EMA{cfg.ema_slow}, RSI={rsi_now:.1f}",
                recent_candles=recent_candles,
                smc_summary=smc_summary,
            )

        return None
