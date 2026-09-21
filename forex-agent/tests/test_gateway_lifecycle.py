"""Gateway lifecycle tests (Phase 3).

Covers the explicit trade-request lifecycle state machine
(requested -> validated -> risk_checked -> approved -> submitted ->
broker_acknowledged -> executed; terminal: rejected / failed /
dry_run_simulated / duplicate_suppressed), the modify/close lifecycle,
and the architectural guarantee that the agent can never reach the
broker adapter's write methods except through the gateway.

Rules under test:
  * "executed" is reported ONLY after broker confirmation. Dry-run and
    unconfirmed submissions report their true state, never executed.
  * Every state transition is audit-logged (kind == "trade_state").
  * Kill switch blocks request_trade and modify_position; close_position
    stays allowed (closing reduces risk — pinned by
    test_close_allowed_while_kill_switch_engaged).
  * Direct adapter.submit_order/modify_order/close_position calls raise
    GATEWAY_BYPASS_ATTEMPTED outside the gateway's execution_scope.
"""

import ast
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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
from config import AppConfig, RiskConfig
from core.events import drain_queue
from core.execution import ExecutionGateway, KillSwitch, TradeRequest
from core.execution.broker_guard import (
    GATEWAY_BYPASS_ATTEMPTED,
    GatewayOnlyAdapter,
    execution_scope,
)
from core.risk import DRY_RUN_BLOCKED, KILL_SWITCH_ENGAGED, RiskManager


@pytest.fixture(autouse=True)
def _local_event_queue(monkeypatch):
    """Hermetic event emission (same pattern as test_core_gateway)."""
    monkeypatch.setattr(events_mod, "_forward", lambda event: False)
    events_mod.drain_queue()
    yield
    events_mod.drain_queue()


class FakeStore:
    """In-memory store stand-in (no idem_* methods -> memory-only idempotency)."""

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


def _position(ticket=111, direction="BUY", sl=1.0950, tp=1.1100):
    return Position(ticket=ticket, position_id=ticket, symbol="EURUSD",
                    direction=direction, volume=0.10,
                    price_open=1.1000, price_current=1.1050,
                    sl=sl, tp=tp, profit=50.0, swap=0.0,
                    magic=20260817, comment="test",
                    time_open=datetime.now(timezone.utc))


class FakeBroker:
    """Deterministic broker double with write-call counters."""

    def __init__(self, equity=10000.0, tick_age_seconds=1, spread=0.0002,
                 positions=None, reject_submit=None):
        self.equity = equity
        self.tick_age = tick_age_seconds
        self.spread = spread
        self._positions = positions if positions is not None else [_position()]
        self.reject_submit = reject_submit
        self.submits = []
        self.modify_calls = []
        self.close_calls = []

    def account_info(self):
        return AccountInfo(balance=10000.0, equity=self.equity, currency="USD")

    def positions(self):
        return list(self._positions)

    def symbols(self, names=None):
        return [_spec()]

    def quote(self, symbol):
        now = datetime.now(timezone.utc)
        return Quote(symbol=symbol, bid=1.1000, ask=1.1000 + self.spread,
                     time=now - timedelta(seconds=self.tick_age))

    def submit_order(self, req: OrderRequest):
        self.submits.append(req)
        if self.reject_submit is not None:
            raise self.reject_submit
        return OrderResult(ticket=777, symbol=req.symbol, direction=req.direction,
                           volume=req.volume, price=1.1000, retcode=0,
                           message="done")

    def modify_order(self, ticket, sl, tp):
        self.modify_calls.append((ticket, sl, tp))

    def close_position(self, ticket):
        self.close_calls.append(ticket)
        return 1.1050


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
        # Live config validates broker credentials at construction.
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
                idempotency_key="key-1", request_id="req-1", source="agent")
    base.update(kw)
    return TradeRequest(**base)


def _states(store, request_id):
    return [a["state"] for a in store.audits
            if a.get("kind") == "trade_state" and a.get("request_id") == request_id]


# -- lifecycle: happy path ----------------------------------------------------

def test_lifecycle_full_success_live(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, live=True)
    d = gw.request_trade(_req())
    assert d.approved is True
    assert d.state == "executed"  # ONLY after broker confirmation
    assert d.ticket == 777 and d.price == 1.1000
    assert broker.submits != []
    assert _states(store, "req-1") == [
        "requested", "validated", "risk_checked", "approved",
        "submitted", "broker_acknowledged", "executed",
    ]
    assert gw.lifecycle("req-1")[-1]["state"] == "executed"
    # Final audit is the trade decision, honestly marked executed.
    assert store.audits[-1]["kind"] == "trade_decision"
    assert store.audits[-1]["state"] == "executed"


def test_dry_run_never_reports_executed(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, )  # dry_run default
    d = gw.request_trade(_req())
    assert d.approved is False
    assert d.ticket is None
    assert d.state == "dry_run_simulated"  # true state, never "executed"
    assert d.reason_code == DRY_RUN_BLOCKED
    assert broker.submits == []  # the full pipeline ran; nothing left the building
    assert _states(store, "req-1") == [
        "requested", "validated", "risk_checked", "dry_run_simulated",
    ]


# -- lifecycle: rejected paths --------------------------------------------------

def test_lifecycle_rejected_kill_switch(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch, live=True)
    ks.engage("test")
    d = gw.request_trade(_req())
    assert d.approved is False and d.state == "rejected"
    assert d.reason_code == KILL_SWITCH_ENGAGED
    assert broker.submits == []
    assert _states(store, "req-1") == ["requested", "rejected"]


def test_lifecycle_rejected_bad_symbol(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, live=True)
    d = gw.request_trade(_req(symbol="FAKE", request_id="req-bad"))
    assert d.approved is False and d.state == "rejected"
    assert d.reason_code == "INVALID_SYMBOL"
    assert broker.submits == []
    assert _states(store, "req-bad")[-1] == "rejected"


def test_lifecycle_rejected_missing_sl(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, live=True)
    d = gw.request_trade(_req(stop_loss=0.0, request_id="req-nosl"))
    assert d.approved is False and d.state == "rejected"
    assert broker.submits == []
    assert _states(store, "req-nosl")[-1] == "rejected"


# -- lifecycle: failed path -------------------------------------------------------

def test_lifecycle_failed_on_broker_rejection(monkeypatch):
    broker = FakeBroker(reject_submit=BrokerError("INVALID_ORDER", "nope"))
    gw, broker, store, _ = _gateway(monkeypatch, broker=broker, live=True)
    d = gw.request_trade(_req(request_id="req-fail"))
    assert d.approved is False
    assert d.state == "failed"  # submitted but never confirmed -> failed, not executed
    assert d.reason_code == "INVALID_ORDER"
    assert d.ticket is None
    assert _states(store, "req-fail") == [
        "requested", "validated", "risk_checked", "approved",
        "submitted", "failed",
    ]


# -- modify / close lifecycle + validation ----------------------------------------

def test_modify_lifecycle_success(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, )
    r = gw.modify_position(111, stop_loss=1.0980, take_profit=1.1100,
                           request_id="mod-1")
    assert r["ok"] is True and r["state"] == "executed"
    assert r["request_id"] == "mod-1"
    assert broker.modify_calls == [(111, 1.0980, 1.1100)]
    assert _states(store, "mod-1") == [
        "requested", "validated", "approved",
        "submitted", "broker_acknowledged", "executed",
    ]
    assert store.audits[-1]["kind"] == "modify_position"


def test_modify_sell_loosening_rejected(monkeypatch):
    broker = FakeBroker(positions=[_position(111, "SELL", sl=1.1050, tp=1.0950)])
    gw, broker, store, _ = _gateway(monkeypatch, broker=broker)
    # SELL stop may only move DOWN; moving it up loosens -> rejected.
    r = gw.modify_position(111, stop_loss=1.1080, request_id="mod-sell")
    assert r["ok"] is False
    assert r["error_code"] == "INVALID_ORDER"
    assert "loosened" in r["message"]
    assert r["state"] == "rejected"
    assert broker.modify_calls == []
    assert _states(store, "mod-sell")[-1] == "rejected"


def test_modify_nonexistent_position(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, )
    r = gw.modify_position(999, stop_loss=1.0980, request_id="mod-ghost")
    assert r["ok"] is False
    assert r["error_code"] == "POSITION_NOT_FOUND"
    assert r["state"] == "rejected"
    assert broker.modify_calls == []


def test_modify_invalid_tp_rejected(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, )
    r = gw.modify_position(111, stop_loss=1.0980, take_profit=-1.0,
                           request_id="mod-badtp")
    assert r["ok"] is False
    assert r["error_code"] == "INVALID_SL_TP"
    assert broker.modify_calls == []


def test_close_lifecycle_success(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, )
    r = gw.close_position(111, request_id="cls-1")
    assert r["ok"] is True and r["state"] == "executed"
    assert r["close_price"] == 1.1050
    assert broker.close_calls == [111]
    assert _states(store, "cls-1") == [
        "requested", "validated", "approved",
        "submitted", "broker_acknowledged", "executed",
    ]
    assert store.audits[-1]["kind"] == "close_position"


def test_close_nonexistent_position_structured_error(monkeypatch):
    gw, broker, store, _ = _gateway(monkeypatch, )
    r = gw.close_position(999, request_id="cls-ghost")
    assert r["ok"] is False
    assert r["error_code"] == "INVALID_ORDER"  # pinned by existing suite
    assert r["state"] == "rejected"
    assert broker.close_calls == []


def test_close_broker_unavailable_structured(monkeypatch):
    class DownBroker(FakeBroker):
        def positions(self):
            raise BrokerError("BROKER_UNAVAILABLE", "terminal down")

        def close_position(self, ticket):
            raise AssertionError("must not reach broker")

    gw, broker, store, _ = _gateway(monkeypatch, broker=DownBroker())
    r = gw.close_position(111, request_id="cls-down")
    assert r["ok"] is False
    assert r["error_code"] == "BROKER_UNAVAILABLE"
    assert r["state"] == "rejected"


# -- kill switch across all three ---------------------------------------------------

def test_kill_switch_blocks_request_and_modify_but_not_close(monkeypatch):
    gw, broker, store, ks = _gateway(monkeypatch, live=True)
    ks.engage("test")
    d = gw.request_trade(_req(request_id="req-ks"))
    assert d.approved is False and d.reason_code == KILL_SWITCH_ENGAGED
    m = gw.modify_position(111, stop_loss=1.0980, request_id="mod-ks")
    assert m["ok"] is False and m["error_code"] == KILL_SWITCH_ENGAGED
    assert broker.modify_calls == []
    # Closing reduces risk: still allowed while engaged.
    c = gw.close_position(111, request_id="cls-ks")
    assert c["ok"] is True and broker.close_calls == [111]


# -- architectural enforcement -------------------------------------------------------

def test_adapter_write_guard_blocks_direct_calls(monkeypatch):
    broker = FakeBroker()
    guarded = GatewayOnlyAdapter(broker)
    # Reads pass through with no scope.
    assert guarded.account_info().equity == 10000.0
    assert guarded.positions() != []
    # Writes raise outside the gateway's scope.
    for name, args in (("submit_order", (OrderRequest(
            symbol="EURUSD", direction="BUY", volume=0.1,
            stop_loss=1.09, take_profit=1.11),)),
                       ("modify_order", (111, 1.0980, 1.1100)),
                       ("close_position", (111,))):
        with pytest.raises(BrokerError) as excinfo:
            getattr(guarded, name)(*args)
        assert excinfo.value.code == GATEWAY_BYPASS_ATTEMPTED
    assert broker.submits == [] and broker.modify_calls == [] and broker.close_calls == []
    # Inside the scope (where only the gateway / kill-switch sweep go), writes work.
    with execution_scope():
        guarded.close_position(111)
    assert broker.close_calls == [111]


def test_gateway_end_to_end_through_guard(monkeypatch):
    broker = FakeBroker()
    guarded = GatewayOnlyAdapter(broker)
    store = FakeStore()
    store.set_risk_state({"day": date.today().isoformat(),
                          "start_of_day_equity": 10000.0, "recent_outcomes": []})
    monkeypatch.setenv("MT5_LOGIN", "1")
    monkeypatch.setenv("MT5_PASSWORD", "x")
    monkeypatch.setenv("MT5_SERVER", "y")
    cfg = replace(AppConfig(), mode="live")
    gw = ExecutionGateway(cfg, guarded, RiskManager(_risk_cfg(), store=store),
                          KillSwitch(store=store), store=store)
    d = gw.request_trade(_req(request_id="req-guard"))
    assert d.approved is True and d.state == "executed"
    assert len(broker.submits) == 1  # exactly one broker call, via the gateway
    # ...and the agent holding the same guarded adapter still cannot call it directly.
    with pytest.raises(BrokerError) as excinfo:
        guarded.submit_order(broker.submits[0])
    assert excinfo.value.code == GATEWAY_BYPASS_ATTEMPTED


def test_backend_exposes_only_guarded_adapter(monkeypatch):
    from agent.tools import backend
    try:
        adapter = backend.broker_adapter()
    finally:
        backend.reset_adapter_cache()
    assert isinstance(adapter, GatewayOnlyAdapter)
    with pytest.raises(BrokerError) as excinfo:
        adapter.submit_order(object())
    assert excinfo.value.code == GATEWAY_BYPASS_ATTEMPTED


def test_agent_tools_never_touch_broker_writes(monkeypatch):
    """Static check: no direct adapter write path exists in agent/tools.

    The sanctioned construction import (backend._build_adapter's lazy
    broker.mt5.adapter import) is allowed; everything else — any
    submit_order/modify_order/close_position call on an adapter-shaped
    name, and any broker import in the tool module the agent invokes —
    is forbidden.
    """
    tools_dir = Path(__file__).resolve().parents[1] / "agent" / "tools"
    write_attrs = {"submit_order", "modify_order", "close_position"}
    for path in sorted(tools_dir.glob("*.py")):
        src = path.read_text()
        tree = ast.parse(src, filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in write_attrs
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"adapter", "broker"}):
                raise AssertionError(
                    f"{path.name}: direct {node.func.value.id}.{node.func.attr}() call")
        # MetaTrader5 itself is never imported outside broker/mt5/.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "MetaTrader5":
                raise AssertionError(f"{path.name} imports MetaTrader5")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "MetaTrader5", \
                        f"{path.name} imports MetaTrader5"
    # The tool module the agent invokes must not import the broker package at all.
    tree = ast.parse((tools_dir / "trading.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("broker"):
            raise AssertionError("agent/tools/trading.py imports from broker")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("broker"), \
                    "agent/tools/trading.py imports broker"


def test_daemon_exit_policy_writes_allowed_only_in_scope(monkeypatch):
    """Regression: the position_monitor's standing exit policy must keep
    working through the broker write-guard (it now runs inside
    execution_scope), while arbitrary direct writes stay blocked."""
    from core.market import Candle
    from core.positions import manage_open_positions
    from config import ExitManagerConfig

    class ExitBroker(FakeBroker):
        def candles(self, symbol, timeframe, count=200):
            now = datetime.now(timezone.utc)
            out = []
            for i in range(25):
                base = 1.1000 + i * 0.0001
                out.append(Candle(time=now - timedelta(hours=i),
                                  open=base, high=base + 0.0010,
                                  low=base - 0.0010, close=base + 0.0002))
            return out

        def positions(self):
            pos = _position()
            # Own position, opened long ago -> time-based exit fires first.
            return [replace(pos, time_open=datetime.now(timezone.utc)
                            - timedelta(hours=72))]

    broker = ExitBroker()
    guarded = GatewayOnlyAdapter(broker)
    cfg = ExitManagerConfig()

    # Outside the scope the guard blocks the daemon's direct writes too.
    stats = manage_open_positions(guarded, cfg, "H1")
    assert stats["closed"] == 0
    assert broker.close_calls == []

    # Inside the scope (exactly what daemon/position_monitor now does),
    # the standing exit policy works again.
    with execution_scope():
        stats = manage_open_positions(guarded, cfg, "H1")
    assert stats["closed"] == 1
    assert broker.close_calls == [111]
