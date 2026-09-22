"""
Position exit management — ported from the original bridge's
exit_manager.py, now behind BrokerAdapter (no direct MetaTrader5).

SAFETY PRINCIPLE (enforced in code, not just documented): every action
here can only move a position toward LESS risk — tightening a stop,
locking in profit, or closing a trade that's overstayed. It never widens
a stop, never adds to a position, and never opens new exposure.

Three mechanisms, in order of precedence:
  1. Time-based exit — close a position open longer than
     `max_hold_hours` without hitting TP. Prevents "hope and hold."
  2. Trailing stop — once profit >= `trailing_activation_atr` * ATR,
     trail SL `trailing_atr_multiplier` * ATR behind price. Only moves
     the stop in the favorable direction (tightens), never loosens.
  3. Breakeven stop — once profit >= `breakeven_trigger_atr` * ATR,
     move SL to entry. Trade can no longer lose money.

Close reporting goes to an `on_close(signal_id, reason, close_price,
costs)` callback supplied by the caller (the daemon wires it to the
cloud sync / journal); every close also emits a `position.closed`
event. Only positions with the bot's magic number are touched.
"""

import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from broker import BrokerAdapter, Position
from config import ExitManagerConfig
from core import events as events_mod
from core.indicators import atr as atr_series
from core.market import Candle
from core.performance import get_position_costs

logger = logging.getLogger("positions")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def current_atr(adapter: BrokerAdapter, symbol: str, timeframe: str,
                period: int = 14) -> Optional[float]:
    """Latest ATR from closed candles via the adapter."""
    candles: List[Candle] = adapter.candles(symbol, timeframe, count=period + 5)
    if len(candles) < period + 1:
        return None
    series = atr_series([c.high for c in candles],
                        [c.low for c in candles],
                        [c.close for c in candles], period)
    return series[-1]


def _tighten_sl(adapter: BrokerAdapter, pos: Position, new_sl: float) -> bool:
    """Move SL only in the favorable direction. Returns True if modified."""
    is_buy = pos.direction == "BUY"
    improves = (is_buy and new_sl > pos.sl) or (not is_buy and new_sl < pos.sl)
    if not improves:
        return False
    try:
        adapter.modify_order(pos.ticket, sl=new_sl, tp=pos.tp)
    except Exception:
        logger.exception("Failed to tighten SL for ticket %s.", pos.ticket)
        return False
    logger.info("Tightened SL for ticket %s (%s) to %.5f",
                pos.ticket, pos.symbol, new_sl)
    return True


def _close_position(adapter: BrokerAdapter, pos: Position, reason: str,
                    on_close: Optional[Callable]) -> bool:
    try:
        close_price = adapter.close_position(pos.ticket)
    except Exception:
        logger.exception("Failed to close ticket %s (%s).", pos.ticket, pos.symbol)
        return False
    logger.info("Closed ticket %s (%s) @ %.5f — reason=%s",
                pos.ticket, pos.symbol, close_price, reason)
    costs = get_position_costs(adapter, pos.position_id)
    events_mod.emit({"event": "position.closed", "ticket": pos.ticket,
                     "symbol": pos.symbol, "reason": reason,
                     "close_price": close_price, "profit": pos.profit})
    signal_id = pos.signal_id
    if on_close is not None and signal_id:
        try:
            on_close(signal_id, reason, close_price, costs)
        except Exception:
            logger.exception("on_close callback failed for signal %s.", signal_id)
    return True


def manage_open_positions(
    adapter: BrokerAdapter,
    exit_cfg: ExitManagerConfig,
    timeframe: str,
    on_close: Optional[Callable[[str, str, float, Optional[dict]], None]] = None,
) -> Dict[str, int]:
    """One pass over the bot's own open positions. Returns stats.

    `on_close(signal_id, reason, close_price, costs)` is called for every
    close so the caller can sync the cloud / journal. Never raises for a
    single bad position — problems are logged and the pass continues.
    """
    stats = {"checked": 0, "closed": 0, "sl_tightened": 0, "skipped": 0}
    try:
        positions = adapter.positions()
    except Exception:
        logger.exception("Could not list positions — skipping exit pass.")
        return stats

    for pos in positions:
        if not pos.is_own:
            continue  # not opened by this bot, leave it alone
        stats["checked"] += 1
        try:
            _manage_one(adapter, exit_cfg, timeframe, pos, on_close, stats)
        except Exception:
            logger.exception("Exit management failed for ticket %s.", pos.ticket)
            stats["skipped"] += 1
    return stats


def _manage_one(adapter, exit_cfg, timeframe, pos: Position, on_close, stats) -> None:
    opened_at = _as_aware(pos.time_open)
    hours_open = (_now() - opened_at).total_seconds() / 3600

    atr_now = current_atr(adapter, pos.symbol, timeframe)
    if atr_now is None or atr_now <= 0:
        stats["skipped"] += 1
        return

    entry = pos.price_open
    current = pos.price_current
    is_buy = pos.direction == "BUY"
    profit_distance = (current - entry) if is_buy else (entry - current)

    # --- 1. Time-based exit takes precedence over everything else ---
    if hours_open >= exit_cfg.max_hold_hours:
        if _close_position(adapter, pos, "time_exit", on_close):
            stats["closed"] += 1
        return

    # --- 2. Trailing stop (only once sufficiently in profit) ---
    if profit_distance >= exit_cfg.trailing_activation_atr * atr_now:
        trail_distance = exit_cfg.trailing_atr_multiplier * atr_now
        new_sl = (current - trail_distance) if is_buy else (current + trail_distance)
        if _tighten_sl(adapter, pos, new_sl):
            stats["sl_tightened"] += 1
        return

    # --- 3. Breakeven stop (once modestly in profit) ---
    if profit_distance >= exit_cfg.breakeven_trigger_atr * atr_now:
        already_safe = (is_buy and pos.sl >= entry) or (not is_buy and pos.sl <= entry)
        if not already_safe and _tighten_sl(adapter, pos, entry):
            stats["sl_tightened"] += 1
