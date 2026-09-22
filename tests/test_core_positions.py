"""
Position exit-manager tests: time exit -> trailing -> breakeven precedence,
SL only ever tightens (never loosens), foreign-magic positions untouched.
No MetaTrader5.
"""

from datetime import datetime, timedelta, timezone

import pytest

import core.events as events_mod
from broker import MAGIC_NUMBER, Deal, Position
from config import ExitManagerConfig
from core.market import Candle
from core.positions import manage_open_positions


@pytest.fixture(autouse=True)
def _local_event_queue(monkeypatch):
    monkeypatch.setattr(events_mod, "_forward", lambda event: False)
    events_mod.drain_queue()
    yield
    events_mod.drain_queue()


def _candles(n=20, high=1.1010, low=1.0990, close=1.1000):
    """Constant-range candles -> ATR == high-low == 0.0020 exactly."""
    base = datetime(2026, 9, 20, tzinfo=timezone.utc)
    return [Candle(time=base + timedelta(hours=i), open=close,
                   high=high, low=low, close=close)
            for i in range(n)]


class FakeBroker:
    def __init__(self, positions=None, deals=None):
        self._positions = positions or []
        self._deals = deals or []
        self.modified = []   # (ticket, sl, tp)
        self.closed = []     # ticket

    def positions(self):
        return list(self._positions)

    def modify_order(self, ticket, sl, tp):
        self.modified.append((ticket, sl, tp))

    def close_position(self, ticket):
        self.closed.append(ticket)
        return 1.0995

    def deal_history(self, from_, to, position_id=None):
        ds = self._deals
        if position_id is not None:
            ds = [d for d in ds if d.position_id == position_id]
        return list(ds)

    def candles(self, symbol, timeframe, count=200):
        return _candles(count)


def _pos(ticket=11, direction="BUY", entry=1.1000, current=1.1005,
         sl=1.0950, tp=1.1100, hours_open=1.0, magic=MAGIC_NUMBER,
         comment="approved:sig-t", position_id=501):
    return Position(
        ticket=ticket, position_id=position_id, symbol="EURUSD",
        direction=direction, volume=0.10, price_open=entry,
        price_current=current, sl=sl, tp=tp, profit=5.0, swap=0.0,
        magic=magic, comment=comment,
        time_open=datetime.now(timezone.utc) - timedelta(hours=hours_open))


def _cfg(**kw):
    base = dict(enabled=True, max_hold_hours=48.0, breakeven_trigger_atr=1.0,
                trailing_activation_atr=2.0, trailing_atr_multiplier=1.5,
                check_interval_seconds=60)
    base.update(kw)
    return ExitManagerConfig(**base)


def _deal(position_id=501, profit=0.0, commission=-2.5, swap=-1.0):
    return Deal(ticket=900, position_id=position_id, symbol="EURUSD",
                direction="BUY", entry="OUT", volume=0.10, price=1.0995,
                profit=profit, commission=commission, swap=swap,
                magic=MAGIC_NUMBER, comment="approved:sig-t",
                time=datetime.now(timezone.utc))


def test_time_exit_closes_overdue_position():
    broker = FakeBroker(positions=[_pos(hours_open=50.0)],
                        deals=[_deal(), _deal(profit=0.0, commission=-2.5, swap=0.0)])
    calls = []
    stats = manage_open_positions(broker, _cfg(), "H1",
                                  on_close=lambda *a: calls.append(a))
    assert stats["closed"] == 1 and broker.closed == [11]
    assert calls and calls[0][:3] == ("sig-t", "time_exit", 1.0995)
    assert calls[0][3] == {"commission": -5.0, "swap": -1.0}
    names = [e["event"] for e in events_mod.drain_queue()]
    assert "position.closed" in names


def test_trailing_tightens_sl_when_deep_in_profit():
    # profit 0.0060 >= 2 * 0.002 -> trail to 1.1060 - 1.5*0.002 = 1.1030
    broker = FakeBroker(positions=[_pos(current=1.1060, sl=1.0950)])
    stats = manage_open_positions(broker, _cfg(), "H1")
    assert stats["sl_tightened"] == 1
    assert broker.modified == [(11, pytest.approx(1.1030), 1.1100)]
    assert broker.closed == []


def test_trailing_never_loosens_sl():
    # computed trail 1.1030 is WORSE than current sl 1.1040 -> no modify
    broker = FakeBroker(positions=[_pos(current=1.1060, sl=1.1040)])
    stats = manage_open_positions(broker, _cfg(), "H1")
    assert stats["sl_tightened"] == 0 and broker.modified == []


def test_breakeven_moves_sl_to_entry():
    # profit 0.0025 >= 1*0.002 but < 2*0.002 -> breakeven, not trailing
    broker = FakeBroker(positions=[_pos(current=1.1025, sl=1.0950)])
    stats = manage_open_positions(broker, _cfg(), "H1")
    assert stats["sl_tightened"] == 1
    assert broker.modified == [(11, pytest.approx(1.1000), 1.1100)]


def test_breakeven_skipped_when_already_safe():
    broker = FakeBroker(positions=[_pos(current=1.1025, sl=1.1005)])
    stats = manage_open_positions(broker, _cfg(), "H1")
    assert broker.modified == []


def test_sell_trailing_moves_sl_down():
    # SELL profit 0.0060 -> new sl = 1.0940 + 0.003 = 1.0970 < 1.1050 -> tighten
    broker = FakeBroker(positions=[_pos(direction="SELL", current=1.0940,
                                        sl=1.1050, tp=1.0900)])
    stats = manage_open_positions(broker, _cfg(), "H1")
    assert broker.modified == [(11, pytest.approx(1.0970), 1.0900)]


def test_foreign_magic_position_untouched():
    broker = FakeBroker(positions=[_pos(hours_open=99.0, magic=424242,
                                        comment="manual")])
    stats = manage_open_positions(broker, _cfg(), "H1")
    assert stats["checked"] == 0
    assert broker.closed == [] and broker.modified == []


def test_no_action_when_flat_or_small_profit():
    broker = FakeBroker(positions=[_pos(current=1.1005)])  # profit 0.0005 < 1*ATR
    stats = manage_open_positions(broker, _cfg(), "H1")
    assert stats == {"checked": 1, "closed": 0, "sl_tightened": 0, "skipped": 0}
