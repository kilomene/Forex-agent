"""
Reconciliation tests: externally-closed positions are detected via the
broker's deal history and reported through on_close; signals with no
closing deal are skipped, not guessed. No MetaTrader5.
"""

from datetime import datetime, timedelta, timezone

import pytest

import core.events as events_mod
from broker import MAGIC_NUMBER, Deal, Position
from core.reconciliation import (
    find_closing_deal,
    get_open_signal_ids,
    reconcile,
)


@pytest.fixture(autouse=True)
def _local_event_queue(monkeypatch):
    monkeypatch.setattr(events_mod, "_forward", lambda event: False)
    events_mod.drain_queue()
    yield
    events_mod.drain_queue()


def _open_pos(signal_id, ticket=21, magic=MAGIC_NUMBER):
    return Position(
        ticket=ticket, position_id=601, symbol="EURUSD", direction="BUY",
        volume=0.10, price_open=1.1000, price_current=1.1010, sl=1.0950,
        tp=1.1100, profit=10.0, swap=0.0, magic=magic,
        comment=f"approved:{signal_id}",
        time_open=datetime.now(timezone.utc) - timedelta(hours=2))


def _close_deal(signal_id, position_id=602, profit=12.5, magic=MAGIC_NUMBER):
    return Deal(ticket=950, position_id=position_id, symbol="EURUSD",
                direction="BUY", entry="OUT", volume=0.10, price=1.1012,
                profit=profit, commission=-2.5, swap=0.0, magic=magic,
                comment=f"approved:{signal_id}",
                time=datetime.now(timezone.utc) - timedelta(hours=1))


class FakeBroker:
    def __init__(self, positions=None, deals=None):
        self._positions = positions or []
        self._deals = deals or []

    def positions(self):
        return list(self._positions)

    def deal_history(self, from_, to, position_id=None):
        ds = self._deals
        if position_id is not None:
            ds = [d for d in ds if d.position_id == position_id]
        return [d for d in ds if from_ <= d.time <= to]


def test_reconcile_reports_externally_closed():
    broker = FakeBroker(
        positions=[_open_pos("s1")],
        deals=[_close_deal("s2"), _close_deal("s2", position_id=603, profit=1.0)],
    )
    calls = []
    n = reconcile(broker, {"s1", "s2", "s3"},
                  on_close=lambda *a: calls.append(a))
    assert n == 1  # s1 still open; s3 has no closing deal -> skipped
    assert calls and calls[0][0] == "s2"
    assert calls[0][1] == "reconciled_external_close"
    assert calls[0][2] == pytest.approx(1.1012)
    assert calls[0][3] == {"commission": -2.5, "swap": 0.0}
    evts = events_mod.drain_queue()
    ext = [e for e in evts if e["event"] == "position.external_close"]
    assert len(ext) == 1 and ext[0]["ticket"] == 603  # latest close wins
    assert ext[0]["symbol"] == "EURUSD"


def test_reconcile_nothing_to_do():
    broker = FakeBroker(positions=[_open_pos("s1")], deals=[])
    assert reconcile(broker, {"s1"}, on_close=lambda *a: None) == 0


def test_find_closing_deal_ignores_foreign_magic_and_entries():
    broker = FakeBroker(deals=[
        _close_deal("s9", magic=424242),                       # foreign magic
        Deal(ticket=1, position_id=604, symbol="EURUSD", direction="BUY",
             entry="IN", volume=0.1, price=1.1, profit=0.0, commission=-2.5,
             swap=0.0, magic=MAGIC_NUMBER, comment="approved:s9",
             time=datetime.now(timezone.utc)),                 # entry, not OUT
    ])
    assert find_closing_deal(broker, "s9") is None


def test_get_open_signal_ids_only_own_magic():
    broker = FakeBroker(positions=[_open_pos("s1"),
                                    _open_pos("sX", magic=424242)])
    assert get_open_signal_ids(broker) == {"s1"}
