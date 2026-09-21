"""Test fakes for interface tests.

FakeBroker: a real BrokerAdapter subclass returning deterministic synthetic
data. Its trade-affecting methods RAISE if called — proving agent/tools
never touches the adapter for trades (all trading goes through the
gateway). FakeGateway: mirrors the REAL core.execution.ExecutionGateway
interface (request_trade(TradeRequest) -> GatewayDecision, .kill_switch
with engage/disengage/is_engaged), using the real TradeRequest and
GatewayDecision dataclasses for fidelity. Dry-run default; flip
``live=True`` to approve trades.
"""

import math
from datetime import datetime, timedelta, timezone

from broker import (
    AccountInfo,
    BrokerAdapter,
    BrokerHealth,
    SymbolSpec,
)

from core.execution.gateway import GatewayDecision, TradeRequest
from core.market import Candle


class FakeBroker(BrokerAdapter):
    adapter_name = "fake"

    def __init__(self):
        self.submit_calls = []

    def connect(self, creds):
        return None

    def disconnect(self):
        return None

    def account_info(self):
        return AccountInfo(balance=10000.0, equity=10050.0, currency="USD",
                           margin=0.0, free_margin=10050.0, leverage=100,
                           login=12345, server="FakeServer")

    def symbols(self, names=None):
        specs = [SymbolSpec(name="EURUSD", volume_min=0.01, volume_max=100.0,
                            volume_step=0.01, tick_value=1.0, tick_size=0.00001,
                            contract_size=100000.0, digits=5, point=0.00001),
                 SymbolSpec(name="GBPUSD", volume_min=0.01, volume_max=100.0,
                            volume_step=0.01, tick_value=1.0, tick_size=0.00001,
                            contract_size=100000.0, digits=5, point=0.00001)]
        if names:
            wanted = {n.upper() for n in names}
            specs = [s for s in specs if s.name.upper() in wanted]
        return specs

    def candles(self, symbol, timeframe, count=200):
        count = max(1, int(count))
        base = datetime(2026, 9, 21, tzinfo=timezone.utc)
        out = []
        for i in range(count):
            o = 1.1000 + 0.0001 * i + 0.00005 * math.sin(i)
            c = o + 0.00005 * math.cos(i * 2)
            out.append(Candle(time=base + timedelta(minutes=15 * i),
                              open=round(o, 5),
                              high=round(max(o, c) + 0.0001, 5),
                              low=round(min(o, c) - 0.0001, 5),
                              close=round(c, 5),
                              volume=100 + i))
        return out

    def positions(self):
        return []

    def orders(self):
        return []

    def submit_order(self, req):
        self.submit_calls.append(req)
        raise AssertionError("agent/tools must never call the broker adapter "
                             "directly - trades go through the gateway")

    def modify_order(self, ticket, sl, tp):
        raise AssertionError("agent/tools must never call the broker adapter directly")

    def close_position(self, ticket):
        raise AssertionError("agent/tools must never call the broker adapter directly")

    def deal_history(self, from_=None, to=None):
        return []

    def health(self):
        return BrokerHealth(connected=True, adapter="fake",
                            server_time=datetime.now(timezone.utc))


class FakeKillSwitch:
    """Mirrors core.execution.kill_switch.KillSwitch (engage/disengage/
    is_engaged/state), latching into the injected store when present."""

    def __init__(self, store=None):
        self._store = store
        self._engaged = False

    def _write(self, engaged, source):
        self._engaged = engaged
        ts = datetime.now(timezone.utc).isoformat()
        if self._store is not None:
            self._store.set_kill_switch(engaged, source)
        if engaged:
            # Mirror the real KillSwitch: it announces engagement via
            # core.events.emit, which forwards to agent.events.bus.publish.
            try:
                from core import events as events_mod
                events_mod.emit({"event": "kill_switch.activated", "ts": ts,
                                 "source": source})
            except Exception:
                pass
        return {"engaged": engaged, "source": source, "ts": ts}

    def state(self):
        return {"engaged": self._engaged, "source": None, "ts": None}

    def is_engaged(self):
        if self._store is not None:
            try:
                return bool(self._store.get_kill_switch().get("engaged", False))
            except Exception:
                return True  # fail closed, like the real one
        return self._engaged

    def engage(self, source="local"):
        return self._write(True, source)

    def disengage(self, source="local"):
        return self._write(False, source)


class FakeGateway:
    """Mirrors core.execution.gateway.ExecutionGateway: request_trade(req)
    takes the real TradeRequest and returns a real GatewayDecision."""

    def __init__(self, store=None, live=False):
        self.kill_switch = FakeKillSwitch(store=store)
        self.live = live
        self.calls = []
        self._seen_keys = set()

    def request_trade(self, req: TradeRequest) -> GatewayDecision:
        assert isinstance(req, TradeRequest), type(req)
        self.calls.append(req)
        if self.kill_switch.is_engaged():
            return GatewayDecision(
                approved=False, reason="Kill switch is engaged — no new entries allowed",
                reason_code="KILL_SWITCH_ENGAGED", idempotency_key=req.idempotency_key)
        if not self.live:
            return GatewayDecision(
                approved=False, reason="Dry-run mode: live orders are blocked by default.",
                reason_code="DRY_RUN_BLOCKED", idempotency_key=req.idempotency_key)
        if req.idempotency_key in self._seen_keys:
            return GatewayDecision(
                approved=False, reason="Duplicate idempotency key",
                reason_code="DUPLICATE_REQUEST", idempotency_key=req.idempotency_key)
        self._seen_keys.add(req.idempotency_key)
        return GatewayDecision(
            approved=True, reason="approved", reason_code="",
            idempotency_key=req.idempotency_key, volume=req.volume or 0.1,
            ticket=777001, price=1.1000)
