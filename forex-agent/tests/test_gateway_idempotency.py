"""Gateway idempotency tests (Phase 3).

Every gateway operation (request_trade / modify_position / close_position)
carries a request_id (uuid). A repeated request_id returns the ORIGINAL
decision/result — no double execution, no double modify, no double close —
and the idempotency record is persisted in SQLite (execution_idempotency)
so it survives process restart.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

import core.events as events_mod
from broker import (
    AccountInfo,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    SymbolSpec,
)
from config import AppConfig, RiskConfig
from core.execution import ExecutionGateway, KillSwitch, TradeRequest
from core.risk import RiskManager
from storage import Store


@pytest.fixture(autouse=True)
def _local_event_queue(monkeypatch):
    monkeypatch.setattr(events_mod, "_forward", lambda event: False)
    events_mod.drain_queue()
    yield
    events_mod.drain_queue()


class FakeStore:
    def __init__(self):
        self._risk = {}
        self._ks = {"engaged": False, "source": None, "ts": None}
        self.audits = []

    def get_risk_state(self):
        return dict(self._risk)

    def set_risk_state(self, patch):
        self._risk.update(patch)

    def get_kill_switch(self):
        return dict(self._ks)

    def set_kill_switch(self, engaged, source=None):
        self._ks = {"engaged": bool(engaged), "source": source,
                    "ts": datetime.now().isoformat()}

    def audit(self, record):
        self.audits.append(record)
        return len(self.audits)


def _spec():
    return SymbolSpec(name="EURUSD", volume_min=0.01, volume_max=100.0,
                      volume_step=0.01, tick_value=1.0, tick_size=0.00001,
                      contract_size=100000.0, digits=5, point=0.00001)


class FakeBroker:
    def __init__(self, equity=10000.0):
        self.equity = equity
        self.submits = []
        self.modify_calls = []
        self.close_calls = []

    def account_info(self):
        return AccountInfo(balance=10000.0, equity=self.equity, currency="USD")

    def positions(self):
        return [Position(ticket=111, position_id=111, symbol="EURUSD",
                         direction="BUY", volume=0.10,
                         price_open=1.1000, price_current=1.1050,
                         sl=1.0950, tp=1.1100, profit=50.0, swap=0.0,
                         magic=20260817, comment="test",
                         time_open=datetime.now(timezone.utc))]

    def symbols(self, names=None):
        return [_spec()]

    def quote(self, symbol):
        now = datetime.now(timezone.utc)
        return Quote(symbol=symbol, bid=1.1000, ask=1.1002,
                     time=now - timedelta(seconds=1))

    def submit_order(self, req: OrderRequest):
        self.submits.append(req)
        return OrderResult(ticket=777, symbol=req.symbol, direction=req.direction,
                           volume=req.volume, price=1.1000, retcode=0,
                           message="done")

    def modify_order(self, ticket, sl, tp):
        self.modify_calls.append((ticket, sl, tp))

    def close_position(self, ticket):
        self.close_calls.append(ticket)
        return 1.1050


def _risk_cfg():
    return RiskConfig(fixed_lot_size=0.10, use_percent_risk_sizing=False,
                      max_risk_per_trade=0.01, max_daily_loss=0.03,
                      max_open_positions=3, max_total_exposure_lots=10.0,
                      max_consecutive_losses=4, max_correlated_positions=5,
                      require_stop_loss=True)


def _gateway(monkeypatch, broker=None, store=None, live=True):
    store = store or FakeStore()
    if hasattr(store, "set_risk_state"):
        store.set_risk_state({"day": date.today().isoformat(),
                              "start_of_day_equity": 10000.0,
                              "recent_outcomes": []})
    if live:
        # Live config validates broker credentials at construction.
        monkeypatch.setenv("MT5_LOGIN", "1")
        monkeypatch.setenv("MT5_PASSWORD", "x")
        monkeypatch.setenv("MT5_SERVER", "y")
    cfg = replace(AppConfig(), mode="live") if live else AppConfig()
    broker = broker or FakeBroker()
    gw = ExecutionGateway(cfg, broker, RiskManager(_risk_cfg(), store=store),
                          KillSwitch(store=store), store=store)
    return gw, broker, store


def _req(request_id, **kw):
    base = dict(signal_id="sig-%s" % request_id, symbol="EURUSD",
                direction="BUY", stop_loss=1.0950, take_profit=1.1100,
                entry_price=1.1000, idempotency_key="key-%s" % request_id,
                request_id=request_id, source="agent")
    base.update(kw)
    return TradeRequest(**base)


# -- request_trade --------------------------------------------------------------

def test_duplicate_request_id_returns_original_no_second_submit(monkeypatch):
    gw, broker, _ = _gateway(monkeypatch, )
    first = gw.request_trade(_req("idem-trade-1"))
    assert first.approved is True and first.ticket == 777
    assert len(broker.submits) == 1
    # Same request_id, even a different signal: original decision, no new order.
    second = gw.request_trade(_req("idem-trade-1", signal_id="sig-other"))
    assert second.approved is True
    assert second.ticket == 777
    assert second.request_id == "idem-trade-1"
    assert len(broker.submits) == 1


def test_duplicate_request_id_rejection_is_sticky(monkeypatch):
    gw, broker, _ = _gateway(monkeypatch, live=False)  # dry-run
    first = gw.request_trade(_req("idem-dry-1"))
    assert first.approved is False
    second = gw.request_trade(_req("idem-dry-1"))
    assert second.approved is False
    assert second.reason_code == first.reason_code == "DRY_RUN_BLOCKED"
    assert second.state == "dry_run_simulated"
    assert broker.submits == []


def test_legacy_idempotency_key_still_replays_original(monkeypatch):
    gw, broker, _ = _gateway(monkeypatch, )
    first = gw.request_trade(_req("idem-key-a", idempotency_key="shared-key"))
    assert first.approved is True
    second = gw.request_trade(_req("idem-key-b", idempotency_key="shared-key"))
    assert second is first  # the ORIGINAL decision object
    assert len(broker.submits) == 1


def test_distinct_request_ids_execute_independently(monkeypatch):
    gw, broker, _ = _gateway(monkeypatch, )
    gw.request_trade(_req("idem-ind-1"))
    gw.request_trade(_req("idem-ind-2"))
    assert len(broker.submits) == 2


# -- modify / close ---------------------------------------------------------------

def test_duplicate_modify_request_id_no_second_broker_call(monkeypatch):
    gw, broker, _ = _gateway(monkeypatch, )
    first = gw.modify_position(111, stop_loss=1.0980, request_id="idem-mod-1")
    assert first["ok"] is True
    assert broker.modify_calls == [(111, 1.0980, 1.1100)]
    second = gw.modify_position(111, stop_loss=1.0990, request_id="idem-mod-1")
    assert second["ok"] is True
    assert second["stop_loss"] == 1.0980  # ORIGINAL result, not the new args
    assert broker.modify_calls == [(111, 1.0980, 1.1100)]


def test_duplicate_close_request_id_no_second_broker_call(monkeypatch):
    gw, broker, _ = _gateway(monkeypatch, )
    first = gw.close_position(111, request_id="idem-cls-1")
    assert first["ok"] is True and first["close_price"] == 1.1050
    assert broker.close_calls == [111]
    second = gw.close_position(111, request_id="idem-cls-1")
    assert second["ok"] is True and second["close_price"] == 1.1050
    assert broker.close_calls == [111]


def test_failed_modify_idempotency_sticky(monkeypatch):
    gw, broker, _ = _gateway(monkeypatch, )
    first = gw.modify_position(999, stop_loss=1.0980, request_id="idem-modfail")
    assert first["ok"] is False and first["error_code"] == "POSITION_NOT_FOUND"
    second = gw.modify_position(999, stop_loss=1.0980, request_id="idem-modfail")
    assert second["error_code"] == "POSITION_NOT_FOUND"
    assert broker.modify_calls == []


# -- persistence across restart -----------------------------------------------------

def test_idempotency_survives_process_restart(monkeypatch, tmp_path):
    """Same SQLite file, brand-new gateway/broker/store objects: the
    original decisions come back and the broker is never touched twice."""
    db = str(tmp_path / "idem.db")

    gw1, broker1, _ = _gateway(monkeypatch, store=Store(db))
    d1 = gw1.request_trade(_req("restart-trade"))
    assert d1.approved is True
    m1 = gw1.modify_position(111, stop_loss=1.0980, request_id="restart-mod")
    assert m1["ok"] is True
    c1 = gw1.close_position(111, request_id="restart-cls")
    assert c1["ok"] is True
    assert len(broker1.submits) == 1
    assert len(broker1.modify_calls) == 1
    assert len(broker1.close_calls) == 1

    # "Restart": everything rebuilt from scratch against the same DB file.
    gw2, broker2, _ = _gateway(monkeypatch, store=Store(db))
    d2 = gw2.request_trade(_req("restart-trade"))
    assert d2.approved is True and d2.ticket == 777
    assert d2.state == "executed"
    m2 = gw2.modify_position(111, stop_loss=1.0999, request_id="restart-mod")
    assert m2["ok"] is True and m2["stop_loss"] == 1.0980
    c2 = gw2.close_position(111, request_id="restart-cls")
    assert c2["ok"] is True and c2["close_price"] == 1.1050
    # The fresh broker saw ZERO write calls: nothing executed twice.
    assert broker2.submits == []
    assert broker2.modify_calls == []
    assert broker2.close_calls == []


def test_idempotency_table_written_to_sqlite(monkeypatch, tmp_path):
    db = str(tmp_path / "idem2.db")
    store = Store(db)
    gw, _, _ = _gateway(monkeypatch, store=store)
    gw.request_trade(_req("sql-trade"))
    gw.modify_position(111, stop_loss=1.0980, request_id="sql-mod")
    rec_trade = store.idem_get("sql-trade")
    rec_mod = store.idem_get("sql-mod")
    assert rec_trade["operation"] == "request_trade"
    assert rec_trade["decision"]["ticket"] == 777
    assert rec_trade["decision"]["state"] == "executed"
    assert rec_mod["operation"] == "modify_position"
    assert rec_mod["decision"]["ok"] is True
    assert store.idem_get("no-such-key") is None
