"""
Performance review + cost tracking — ported from the original bridge's
performance_review.py and cost_tracking.py, now behind BrokerAdapter
(no direct MetaTrader5).

Self-performance review uses the broker's own deal history as ground
truth — not a status approximation. Deal history returns every actual
closed deal on the account, including real profit/loss, which is the
only honest source for "how has this bot actually performed."

Filters to deals with the bot's magic number so trades placed manually
or by something else on the same account are never mixed in.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from broker import BrokerAdapter, is_own_position

logger = logging.getLogger("performance")


@dataclass
class PerformanceSnapshot:
    period_start: str
    period_end: str
    total_closed_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate_pct: Optional[float]
    total_profit: float
    average_win: Optional[float]
    average_loss: Optional[float]
    profit_factor: Optional[float]  # gross profit / gross loss, None if no losses
    largest_win: Optional[float]
    largest_loss: Optional[float]
    by_symbol: dict
    total_commission: float
    total_swap: float
    net_profit: float  # total_profit + total_commission + total_swap


def _own_exit_deals(adapter: BrokerAdapter, from_: datetime, to: datetime):
    deals = adapter.deal_history(from_, to) or []
    return sorted(
        [d for d in deals if is_own_position(d.magic) and d.entry == "OUT"],
        key=lambda d: d.time,
    )


def get_recent_outcomes(adapter: BrokerAdapter, limit: int = 20,
                        lookback_days: int = 14) -> List[bool]:
    """Most recent `limit` trade outcomes (True=won, False=loss/breakeven)
    in chronological order, straight from the broker's real deal history.
    This feeds the risk manager's consecutive-loss circuit breaker — using
    actual account history means it reflects every kind of close (SL hit,
    TP hit, manual close, time-exit), not just closes the bot triggered
    itself."""
    to = datetime.now(timezone.utc)
    from_ = to - timedelta(days=lookback_days)
    exit_deals = _own_exit_deals(adapter, from_, to)
    return [d.profit > 0 for d in exit_deals[-limit:]]


def compute_performance(adapter: BrokerAdapter,
                        lookback_days: int = 30) -> PerformanceSnapshot:
    """Honest statistics from real closed-deal history. Only "OUT" deals
    count toward win/loss — entry deals have profit=0 by definition and
    would otherwise dilute the stats."""
    to = datetime.now(timezone.utc)
    from_ = to - timedelta(days=lookback_days)

    deals = adapter.deal_history(from_, to) or []
    exit_deals = [d for d in deals
                  if is_own_position(d.magic) and d.entry == "OUT"]

    # Commission is commonly charged on BOTH the entry and exit deal, and
    # swap accrues as its own separate deals the longer a position stays
    # open — summing only exit_deals here would undercount real cost.
    # This uses every deal for our magic number in the window, not just
    # the exit-side subset used for win/loss stats above.
    all_our_deals = [d for d in deals if is_own_position(d.magic)]
    total_commission = sum(d.commission for d in all_our_deals)
    total_swap = sum(d.swap for d in all_our_deals)

    wins = [d for d in exit_deals if d.profit > 0]
    losses = [d for d in exit_deals if d.profit < 0]
    breakeven = [d for d in exit_deals if d.profit == 0]

    total_profit = sum(d.profit for d in exit_deals)
    gross_profit = sum(d.profit for d in wins)
    gross_loss = abs(sum(d.profit for d in losses))

    by_symbol: Dict[str, dict] = {}
    for d in exit_deals:
        s = by_symbol.setdefault(d.symbol, {"count": 0, "profit": 0.0, "wins": 0})
        s["count"] += 1
        s["profit"] += d.profit
        if d.profit > 0:
            s["wins"] += 1

    return PerformanceSnapshot(
        period_start=from_.isoformat(),
        period_end=to.isoformat(),
        total_closed_trades=len(exit_deals),
        wins=len(wins),
        losses=len(losses),
        breakeven=len(breakeven),
        win_rate_pct=round(len(wins) / len(exit_deals) * 100, 1) if exit_deals else None,
        total_profit=round(total_profit, 2),
        average_win=round(gross_profit / len(wins), 2) if wins else None,
        average_loss=round(-gross_loss / len(losses), 2) if losses else None,
        profit_factor=round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        largest_win=round(max((d.profit for d in wins), default=0), 2) if wins else None,
        largest_loss=round(min((d.profit for d in losses), default=0), 2) if losses else None,
        by_symbol=by_symbol,
        total_commission=round(total_commission, 2),
        total_swap=round(total_swap, 2),
        net_profit=round(total_profit + total_commission + total_swap, 2),
    )


def snapshot_to_dict(snapshot: PerformanceSnapshot) -> dict:
    return asdict(snapshot)


def get_position_costs(adapter: BrokerAdapter,
                       position_id: int) -> Optional[Dict[str, float]]:
    """Real trade costs from the broker's deal history — not estimated.

    Returns {"commission": float, "swap": float} summed across every deal
    belonging to this position (the deal that opened it AND the deal(s)
    that closed it — commission is often charged on both sides, and swap
    accrues the longer a position stays open).

    Returns None if no matching deals are found — callers should treat
    this as "cost data not available yet" rather than assuming zero cost,
    since deals can take a moment to appear in history right after a
    close is confirmed.
    """
    # The adapter's deal_history is windowed; use a generous window so a
    # long-held position's entry deal is still included.
    to = datetime.now(timezone.utc)
    from_ = to - timedelta(days=365 * 5)
    try:
        deals = adapter.deal_history(from_, to, position_id=position_id) or []
    except Exception:
        logger.exception("Deal history lookup failed for position %s.", position_id)
        return None
    deals = [d for d in deals if d.position_id == position_id]
    if not deals:
        logger.warning("No deals found for position %s.", position_id)
        return None
    return {"commission": round(sum(d.commission for d in deals), 2),
            "swap": round(sum(d.swap for d in deals), 2)}
