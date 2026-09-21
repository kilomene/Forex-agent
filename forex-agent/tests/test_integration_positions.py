"""Integration: gateway close_position / modify_position (build-master seam).

Covers the seam the interface-builder honestly flagged DEPENDENCY_UNAVAILABLE:
forex.close_position and forex.modify_position now route through the
ExecutionGateway — never the adapter directly.
"""
from datetime import datetime, timezone

import pytest

from broker import BrokerError, Position
from core.execution.gateway import ExecutionGateway
from core.execution.kill_switch import KillSwitch
from core.risk import RiskManager
from config import AppConfig


class FakeStore:
    def __init__(self):
        self.audits = []
        self._ks = {"engaged": False, "source": None, "ts": None}

    def audit(self, record):
        self.audits.append(record)
        return len(self.audits)

    def get_kill_switch(self):
        return dict(self._ks)

    def set_kill_switch(self, engaged, source):
        self._ks = {"engaged": engaged, "source": source, "ts": "now"}


class FakeBroker:
    def __init__(self):
        self.closed = []
        self.modified = []

    def positions(self):
        return [Position(ticket=111, position_id=111, symbol="EURUSD", direction="BUY", volume=0.10,
                         price_open=1.1000, price_current=1.1050, sl=1.0950,
                         tp=1.1100, profit=50.0, swap=0.0, magic=20260921,
                         comment="approved:sig-9",
                         time_open=datetime.now(timezone.utc))]

    def close_position(self, ticket):
        if ticket != 111:
            raise BrokerError("INVALID_ORDER", f"unknown ticket {ticket}")
        self.closed.append(ticket)
        return 1.1050

    def modify_order(self, ticket, sl, tp):
        if ticket != 111:
            raise BrokerError("INVALID_ORDER", f"unknown ticket {ticket}")
        self.modified.append((ticket, sl, tp))


def _gateway(broker=None):
    store = FakeStore()
    cfg = AppConfig()  # dry_run default; close/modify are not new entries
    ks = KillSwitch(store=store)
    gw = ExecutionGateway(config=cfg, adapter=broker or FakeBroker(),
                          risk_manager=RiskManager(cfg.risk, store),
                          kill_switch=ks, store=store)
    return gw, store


def test_close_position_broker_confirmed():
    gw, store = _gateway()
    r = gw.close_position(111, source="agent")
    assert r["ok"] is True
    assert r["close_price"] == 1.1050
    assert r["symbol"] == "EURUSD"
    assert store.audits and store.audits[-1]["kind"] == "close_position"


def test_close_unknown_ticket_structured_error():
    gw, _ = _gateway()
    r = gw.close_position(999)
    assert r["ok"] is False
    assert r["error_code"] == "INVALID_ORDER"


def test_close_allowed_while_kill_switch_engaged():
    gw, store = _gateway()
    gw.kill_switch.engage("agent")
    r = gw.close_position(111, source="agent")
    assert r["ok"] is True  # closing reduces risk — never blocked


def test_modify_sl_tighten_ok():
    gw, store = _gateway()
    r = gw.modify_position(111, stop_loss=1.0980, take_profit=1.1100)
    assert r["ok"] is True
    assert r["stop_loss"] == 1.0980
    assert store.audits[-1]["kind"] == "modify_position"


def test_modify_sl_loosen_rejected():
    gw, store = _gateway()
    broker = gw.adapter
    r = gw.modify_position(111, stop_loss=1.0900)  # BUY sl 1.0950 -> looser
    assert r["ok"] is False
    assert r["error_code"] == "INVALID_ORDER"
    assert "loosened" in r["message"]
    assert broker.modified == []  # never reached the broker


def test_modify_cannot_remove_sl():
    gw, _ = _gateway()
    r = gw.modify_position(111, stop_loss=0)
    assert r["ok"] is False
    assert r["error_code"] == "INVALID_ORDER"


def test_modify_nothing_to_change_rejected():
    gw, _ = _gateway()
    r = gw.modify_position(111)
    assert r["ok"] is False
    assert r["error_code"] == "INVALID_ORDER"
