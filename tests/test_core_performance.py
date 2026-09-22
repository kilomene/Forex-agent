"""
Performance review + cost tracking tests: honest stats from deal history,
magic-number filtering, cost summation. No MetaTrader5.
"""

from datetime import datetime, timedelta, timezone

from broker import MAGIC_NUMBER, Deal
from core.performance import (
    compute_performance,
    get_position_costs,
    get_recent_outcomes,
    snapshot_to_dict,
)


def _deal(ticket, position_id, entry, profit, symbol="EURUSD",
          commission=-2.5, swap=0.0, magic=MAGIC_NUMBER,
          comment="approved:s1", days_ago=1):
    return Deal(ticket=ticket, position_id=position_id, symbol=symbol,
                direction="BUY", entry=entry, volume=0.10, price=1.1000,
                profit=profit, commission=commission, swap=swap,
                magic=magic, comment=comment,
                time=datetime.now(timezone.utc) - timedelta(days=days_ago))


class FakeBroker:
    def __init__(self, deals):
        self._deals = deals

    def deal_history(self, from_, to, position_id=None):
        ds = self._deals
        if position_id is not None:
            ds = [d for d in ds if d.position_id == position_id]
        return [d for d in ds if from_ <= d.time <= to]


def _broker():
    return FakeBroker([
        _deal(1, 101, "IN", 0.0, commission=-2.5, days_ago=5),
        _deal(2, 101, "OUT", 50.0, commission=-2.5, swap=-1.0, days_ago=5),
        _deal(3, 102, "IN", 0.0, commission=-3.0, days_ago=3),
        _deal(4, 102, "OUT", -30.0, commission=-3.0, days_ago=3),
        _deal(5, 103, "IN", 0.0, commission=-2.0, days_ago=1),
        _deal(6, 103, "OUT", 0.0, commission=-2.0, days_ago=1),   # breakeven
        _deal(7, 999, "OUT", 1000.0, magic=424242, comment="manual",
              commission=0.0, days_ago=2),                        # foreign
    ])


def test_compute_performance_honest_stats():
    snap = compute_performance(_broker(), lookback_days=30)
    assert snap.total_closed_trades == 3
    assert (snap.wins, snap.losses, snap.breakeven) == (1, 1, 1)
    assert snap.win_rate_pct == 33.3
    assert snap.total_profit == 20.0
    assert snap.profit_factor == round(50.0 / 30.0, 2)
    assert snap.average_win == 50.0 and snap.average_loss == -30.0
    assert snap.largest_win == 50.0 and snap.largest_loss == -30.0
    assert snap.by_symbol["EURUSD"]["count"] == 3
    # commission+swap summed over ALL own deals (entry + exit), foreign excluded
    assert snap.total_commission == round(-2.5 - 2.5 - 3.0 - 3.0 - 2.0 - 2.0, 2)
    assert snap.total_swap == -1.0
    assert snap.net_profit == round(20.0 + snap.total_commission + snap.total_swap, 2)


def test_compute_performance_empty_history():
    snap = compute_performance(FakeBroker([]), lookback_days=30)
    assert snap.total_closed_trades == 0
    assert snap.win_rate_pct is None and snap.profit_factor is None
    assert snap.total_profit == 0.0


def test_recent_outcomes_chronological_and_limited():
    outcomes = get_recent_outcomes(_broker(), limit=2, lookback_days=30)
    # chronological: ..., loss (-30), breakeven (0) -> both False
    assert outcomes == [False, False]
    all_outcomes = get_recent_outcomes(_broker(), limit=20, lookback_days=30)
    assert all_outcomes == [True, False, False]


def test_position_costs_sums_entry_and_exit():
    costs = get_position_costs(_broker(), 101)
    assert costs == {"commission": -5.0, "swap": -1.0}


def test_position_costs_none_when_no_deals():
    assert get_position_costs(_broker(), 12345) is None


def test_snapshot_to_dict_roundtrip():
    d = snapshot_to_dict(compute_performance(_broker()))
    assert d["total_closed_trades"] == 3 and d["wins"] == 1
