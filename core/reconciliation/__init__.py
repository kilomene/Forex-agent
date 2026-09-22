"""
Reconciliation — ported from the original bridge's reconciliation.py,
now behind BrokerAdapter (no direct MetaTrader5).

Closes a real gap: the cloud/sync layer only learns a position closed
when the bot itself triggers the close (positions exit manager:
trailing stop / breakeven / time exit). A position closed manually at
the broker — directly, outside the bot — would otherwise never reach
the sync layer, silently degrading correlation-exposure checks,
reflection generation, and the picture of what's still open.

This runs periodically, comparing the broker's real open positions
(ground truth) against every signal the bot believes is still open
(supplied by the caller, e.g. read from the store journal by the
daemon). Anything believed open but actually closed gets reconciled by
looking up the real closing deal and reporting it through the same
`on_close(signal_id, reason, price, costs)` callback the exit manager
uses — which means downstream reflection now fires for EVERY close,
not just the ones the bot triggered itself.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional, Set

from broker import BrokerAdapter, extract_signal_id_from_comment, is_own_position
from core import events as events_mod
from core.performance import get_position_costs

logger = logging.getLogger("reconciliation")


def get_open_signal_ids(adapter: BrokerAdapter) -> Set[str]:
    """Signal IDs currently open per the broker itself — ground truth."""
    try:
        positions = adapter.positions()
    except Exception:
        logger.exception("Could not list positions for reconciliation.")
        return set()
    open_ids = set()
    for pos in positions:
        if not pos.is_own:
            continue
        signal_id = pos.signal_id
        if signal_id:
            open_ids.add(signal_id)
    return open_ids


def find_closing_deal(adapter: BrokerAdapter, signal_id: str,
                      lookback_days: int = 14) -> Optional[Dict]:
    """Finds the actual closing deal for a signal_id in the broker's deal
    history, matched by the same comment convention used at order
    placement (`approved:<signal_id>`)."""
    to = datetime.now(timezone.utc)
    from_ = to - timedelta(days=lookback_days)
    try:
        deals = adapter.deal_history(from_, to) or []
    except Exception:
        logger.exception("Deal history lookup failed during reconciliation.")
        return None

    matching = [
        d for d in deals
        if is_own_position(d.magic)
        and d.entry == "OUT"
        and extract_signal_id_from_comment(d.comment) == signal_id
    ]
    if not matching:
        return None

    # Most recent matching close, in case of partial closes (take the last).
    latest = max(matching, key=lambda d: d.time)
    return {"price": latest.price, "profit": latest.profit,
            "time": latest.time, "position_id": latest.position_id,
            "symbol": latest.symbol, "ticket": latest.ticket}


def reconcile(
    adapter: BrokerAdapter,
    believed_open_signal_ids: Set[str],
    on_close: Callable[[str, str, float, Optional[dict]], None],
    lookback_days: int = 14,
) -> int:
    """Reconcile externally-closed positions. Returns the number of
    positions reconciled (closed outside the bot and now reported).

    `believed_open_signal_ids` is the set of signal IDs the bot thinks
    are still open (the daemon reads this from the store journal).
    `on_close(signal_id, reason, price, costs)` receives each reconciled
    close with reason="reconciled_external_close".
    """
    broker_open_ids = get_open_signal_ids(adapter)

    reconciled_count = 0
    for signal_id in sorted(believed_open_signal_ids):
        if signal_id in broker_open_ids:
            continue  # still genuinely open, nothing to reconcile

        closing_deal = find_closing_deal(adapter, signal_id,
                                         lookback_days=lookback_days)
        if closing_deal is None:
            # Not open at the broker, but no matching close found either —
            # could be a signal older than the lookback window, or a data
            # gap. Log and skip rather than guess at a close price.
            logger.warning(
                "Signal %s not open at broker and no closing deal found "
                "within lookback window — skipping.", signal_id)
            continue

        logger.info(
            "Reconciling signal %s: closed externally @ %.5f (profit=%.2f)",
            signal_id, closing_deal["price"], closing_deal["profit"])
        costs = get_position_costs(adapter, closing_deal["position_id"])
        events_mod.emit({"event": "position.external_close",
                         "ticket": closing_deal["position_id"],
                         "symbol": closing_deal["symbol"],
                         "note": f"reconciled_external_close:{signal_id}"})
        try:
            on_close(signal_id, "reconciled_external_close",
                     closing_deal["price"], costs)
        except Exception:
            logger.exception("on_close callback failed for signal %s.", signal_id)
            continue
        reconciled_count += 1

    return reconciled_count
