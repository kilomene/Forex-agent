"""
Execution Gateway + KillSwitch tests.

Covers: kill-switch blocks, dry-run blocks live orders, missing SL
rejected, duplicate idempotency key prevented, duplicate signal
rejected, symbol whitelist, stale-tick market-closed, audit logging,
and kill-switch latch persistence across store reopen. No MetaTrader5.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

import core.events as events_mod
from broker import (
    AccountInfo,
    BrokerError,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    SymbolSpec,
)
from config import AppConfig
from core.events import drain_queue
from core.execution import ExecutionGateway, KillSwitch, TradeRequest
from core.risk import DRY_RUN_BLOCKED, KILL_SWITCH_ENGAGED, RiskManager
from config import RiskConfig


@pytest.fixture(autouse=True)
def _local_event_queue(monkeypatch):
    """Force the event shim into local-queue mode so emission assertions
    are hermetic (the real bus would persist to storage.Store instead)."""
    monkeypatch.setattr(events_mod, "_forward", lambda event: False)
    events_mod.drain_queue()
    yield
    events_mod.drain_queue()


class FakeStore:
    """In-memory stand-in for the storage contract."""

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

    def query_audit(self, limit=100, since=None):
        return self.audits[-limit:]


def _spec():
    return SymbolSpec(name="EURUSD", volume_min=0.01, volume_max=100.0,
                      volume_step=0.01, tick_value=1.0, tick_size=0.00001,
                      contract_size=100000.0, digits=5, point=0.00001)


class FakeBroker:
    def __init__(self, equity=10000.0, tick_age_seconds=1, spread=0.0002):
        self.equity = equity
        self.tick_age = tick_age_seconds
        self.spread = spread
        self.submits = []

    def account_info(self):
        return AccountInfo(balance=10000.0, equity=self.equity, currency="USD")

    def positions(self):
        return []

    def symbols(self, names=None):
        return [_spec()]

    def quote(self, symbol):
        now = datetime.now()
        return Quote(symbol=symbol, bid=1.1000, ask=1.1000 + self.spread,
                     time=now - timedelta(seconds=self.tick_age))

    def submit_order(self, req: OrderRequest):
        self.submits.append(req)
        return OrderResult(ticket=777, symbol=req.symbol, direction=req.direction,
                           volume=req.volume, price=1.1000, retcode=0,
                           message="done")


def _risk_cfg(**kw):
    base = dict(fixed_lot_size=0.10, use_percent_risk_sizing=False,
                max_risk_per_trade=0.01, max_daily_loss=0.03,
                max_open_positions=3, max_total_exposure_lots=10.0,
                max_consecutive_losses=4, max_correlated_positions=5,
                require_stop_loss=True)
    base.update(kw)
    return RiskConfig(**base)


def _gateway(monkeypatch, broker=None, live=False, **risk_kw):
    store = FakeStore()
    store.set_risk_state({"day": date.today().isoformat(),
                          "start_of_day_equity": 10000.0,
                          "recent_outcomes": []})
    cfg = AppConfig()
    if live:
        monkeypatch.setenv("MT5_LOGIN", "1")
        monkeypatch.setenv("MT5_PASSWORD", "x")
        monkeypatch.setenv("MT5_SERVER", "y")
        cfg = replace(AppConfig(), mode="live")
    broker = broker or FakeBroker()
    rm = RiskManager(_risk_cfg(**risk_kw), store=store)
    ks = KillSwitch(store=store)
    gw = ExecutionGateway(cfg, broker, rm, ks, store=store)
    return gw, broker, store, ks


def _req(**kw):
    base = dict(signal_id="sig-1", symbol="EURUSD", direction="BUY",
                stop_loss=1.0950, take_profit=1.1100, entry_price=1.1000,
                idempotency_key="key-1", source="agent")
    base.update(kw)
    return TradeRequest(**base)


def test_kill_switch_blocks_and_never_touches_broker(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch)
    ks.engage("agent")
    d = gw.request_trade(_req())
    assert d.approved is False
    assert d.reason_code == KILL_SWITCH_ENGAGED
    assert broker.submits == []


def test_dry_run_blocks_live_orders_after_full_pipeline(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch)  # dry_run by default
    assert gw.config.dry_run is True
    d = gw.request_trade(_req())
    assert d.approved is False
    assert d.reason_code == DRY_RUN_BLOCKED
    assert broker.submits == []  # nothing reached the broker
    # ...but the pipeline ran: risk sized the volume, audit recorded it
    assert d.volume == pytest.approx(0.10)
    assert store.audits and store.audits[-1]["reason_code"] == DRY_RUN_BLOCKED
    assert store.audits[-1]["risk_inputs"]["equity"] == 10000.0


def test_live_mode_approves_and_confirms(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch, live=True)
    d = gw.request_trade(_req())
    assert d.approved is True
    assert d.ticket == 777 and d.price == 1.1000
    assert len(broker.submits) == 1
    assert broker.submits[0].comment is None  # comment applied at submit
    assert broker.submits[0].order_comment() == "approved:sig-1"  # comment convention kept
    assert broker.submits[0].idempotency_key == "key-1"
    audit = store.audits[-1]
    assert audit["approved"] is True
    assert audit["broker"]["ticket"] == 777


def test_missing_sl_rejected(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch, live=True)
    d = gw.request_trade(_req(stop_loss=None))
    assert d.approved is False
    assert d.reason_code == "INVALID_ORDER"
    assert broker.submits == []


def test_duplicate_idempotency_key_returns_original_decision(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch, live=True)
    d1 = gw.request_trade(_req())
    d2 = gw.request_trade(_req())  # same key replayed
    assert d1.approved is True and d2.approved is True
    assert d2.ticket == d1.ticket
    assert len(broker.submits) == 1  # broker saw exactly one order


def test_duplicate_signal_id_rejected(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch, live=True)
    d1 = gw.request_trade(_req(signal_id="sig-9", idempotency_key="k-a"))
    assert d1.approved is True
    d2 = gw.request_trade(_req(signal_id="sig-9", idempotency_key="k-b"))
    assert d2.approved is False
    assert d2.reason_code == "INVALID_ORDER"
    assert "duplicate" in d2.reason.lower()
    assert len(broker.submits) == 1


def test_symbol_whitelist_rejected(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch, live=True)
    d = gw.request_trade(_req(symbol="FAKEPAIR"))
    assert d.approved is False
    assert d.reason_code == "INVALID_SYMBOL"
    assert broker.submits == []


def test_stale_tick_blocks_as_market_closed(monkeypatch):
    broker = FakeBroker(tick_age_seconds=3600)  # stale — market closed
    gw, broker, store, ks = _gateway(monkeypatch, broker=broker, live=True)
    d = gw.request_trade(_req())
    assert d.approved is False
    assert d.reason_code == "MARKET_CLOSED"
    assert broker.submits == []


def test_broker_rejection_becomes_structured_reject(monkeypatch):
    class RejectBroker(FakeBroker):
        def submit_order(self, req):
            raise BrokerError("INVALID_ORDER", "price requote")
    gw, broker, store, ks = _gateway(monkeypatch, broker=RejectBroker(), live=True)
    d = gw.request_trade(_req())
    assert d.approved is False
    assert d.reason_code == "INVALID_ORDER"
    # signal NOT marked executed — a retry with a new key may proceed
    assert "sig-1" not in gw._executed_signal_ids


def test_events_emitted_for_request_and_outcome(monkeypatch):
    drain_queue()
    gw, broker, store, ks = _gateway(monkeypatch, live=True)
    gw.request_trade(_req())
    names = [e["event"] for e in drain_queue()]
    assert "trade.requested" in names
    assert "trade.executed" in names


def test_risk_block_emits_event(monkeypatch):
    drain_queue()
    gw, broker, store, ks = _gateway(monkeypatch)
    ks.engage("local")
    gw.request_trade(_req())
    names = [e["event"] for e in drain_queue()]
    assert "risk.blocked" in names


# --- kill-switch latch ------------------------------------------------------------

def test_kill_switch_latch_persists_across_reopen():
    store = FakeStore()
    ks1 = KillSwitch(store=store)
    assert ks1.is_engaged() is False
    s1 = ks1.engage("agent")
    assert s1["engaged"] is True and s1["source"] == "agent"
    ts1 = s1["ts"]
    # "reopen": new instance on the same store
    ks2 = KillSwitch(store=store)
    assert ks2.is_engaged() is True
    # idempotent: re-engaging keeps the original ts/source
    s2 = ks2.engage("worker")
    assert s2["ts"] == ts1 and s2["source"] == "agent"
    ks2.disengage("agent")
    ks3 = KillSwitch(store=store)
    assert ks3.is_engaged() is False


def test_kill_switch_fails_closed_on_store_error():
    class BadStore(FakeStore):
        def get_kill_switch(self):
            raise RuntimeError("disk gone")
    ks = KillSwitch(store=BadStore())
    assert ks.is_engaged() is True  # fail closed


def test_kill_switch_close_all_only_own_positions(monkeypatch):
    store = FakeStore()

    class BrokerWithPositions(FakeBroker):
        def positions(self):
            return [
                Position(ticket=1, position_id=1, symbol="EURUSD", direction="BUY",
                         volume=0.1, price_open=1.1, price_current=1.1, sl=1.09,
                         tp=1.11, profit=0.0, swap=0.0, magic=20260817,
                         comment="approved:s1", time_open=datetime.now()),
                Position(ticket=2, position_id=2, symbol="EURUSD", direction="BUY",
                         volume=0.1, price_open=1.1, price_current=1.1, sl=1.09,
                         tp=1.11, profit=0.0, swap=0.0, magic=424242,
                         comment="manual", time_open=datetime.now()),
            ]

        def close_position(self, ticket):
            self.closed_tickets = getattr(self, "closed_tickets", [])
            self.closed_tickets.append(ticket)
            return 1.1005

    broker = BrokerWithPositions()
    ks = KillSwitch(store=store, adapter=broker)
    out = ks.close_all()
    assert out["closed"] == 1 and out["failed"] == 0
    assert broker.closed_tickets == [1]  # foreign ticket 2 untouched
