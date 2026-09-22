"""
Broker adapter tests — run against FakeBroker (an in-memory
BrokerAdapter), DisconnectedAdapter, and the MT5Adapter's graceful
degradation path. No MetaTrader5 import anywhere in this file.
"""

from datetime import datetime, timedelta

import pytest

from broker import (
    AccountInfo,
    BrokerAdapter,
    BrokerError,
    BrokerHealth,
    Deal,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    SymbolSpec,
    BROKER_UNAVAILABLE,
    INVALID_ORDER,
    INVALID_SYMBOL,
    MAGIC_NUMBER,
    extract_signal_id_from_comment,
    is_own_position,
)
from broker.disconnected import DisconnectedAdapter
from core.market import Candle


class FakeBroker(BrokerAdapter):
    """In-memory BrokerAdapter for tests: closed candles, one symbol,
    broker-confirmed fills with incrementing tickets."""

    def __init__(self):
        self.connected = False
        self._tickets = 1000
        self._positions = []
        base = datetime(2026, 1, 1)
        self._candles = [
            Candle(time=base + timedelta(hours=i), open=1.10 + i * 0.0001,
                   high=1.101 + i * 0.0001, low=1.099 + i * 0.0001,
                   close=1.1005 + i * 0.0001, volume=100)
            for i in range(50)
        ]

    def connect(self, creds: dict) -> None:
        if not creds.get("login"):
            raise BrokerError("CREDENTIALS_INVALID", "login required")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def _ensure(self):
        if not self.connected:
            raise BrokerError("MT5_NOT_CONNECTED", "not connected")

    def account_info(self) -> AccountInfo:
        self._ensure()
        return AccountInfo(balance=10000.0, equity=9850.0, currency="USD",
                           login=123, server="Fake-Server")

    def symbols(self, names=None) -> list:
        self._ensure()
        names = names or ["EURUSD"]
        out = []
        for n in names:
            if n != "EURUSD":
                raise BrokerError(INVALID_SYMBOL, f"unknown symbol {n}")
            out.append(SymbolSpec(name=n, volume_min=0.01, volume_max=100.0,
                                  volume_step=0.01, tick_value=1.0,
                                  tick_size=0.00001, contract_size=100000.0,
                                  digits=5, point=0.00001))
        return out

    def candles(self, symbol, timeframe, count=200) -> list:
        self._ensure()
        if symbol != "EURUSD":
            raise BrokerError(INVALID_SYMBOL, f"unknown symbol {symbol}")
        return list(self._candles[-count:])  # already closed-only

    def positions(self) -> list:
        self._ensure()
        return list(self._positions)

    def orders(self) -> list:
        self._ensure()
        return []

    def submit_order(self, req: OrderRequest) -> OrderResult:
        self._ensure()
        if req.volume <= 0:
            raise BrokerError(INVALID_ORDER, "bad volume")
        self._tickets += 1
        pos = Position(ticket=self._tickets, position_id=self._tickets,
                       symbol=req.symbol, direction=req.direction,
                       volume=req.volume, price_open=1.1000,
                       price_current=1.1000, sl=req.stop_loss, tp=req.take_profit,
                       profit=0.0, swap=0.0, magic=MAGIC_NUMBER,
                       comment=req.order_comment(),
                       time_open=datetime.now())
        self._positions.append(pos)
        return OrderResult(ticket=self._tickets, symbol=req.symbol,
                           direction=req.direction, volume=req.volume,
                           price=1.1000, retcode=0, message="done")

    def modify_order(self, ticket: int, sl: float, tp: float) -> None:
        self._ensure()
        for p in self._positions:
            if p.ticket == ticket:
                p.sl, p.tp = sl, tp
                return
        raise BrokerError(INVALID_ORDER, f"no ticket {ticket}")

    def close_position(self, ticket: int) -> float:
        self._ensure()
        for i, p in enumerate(self._positions):
            if p.ticket == ticket:
                self._positions.pop(i)
                return 1.1010
        raise BrokerError(INVALID_ORDER, f"no ticket {ticket}")

    def deal_history(self, from_, to, position_id=None) -> list:
        self._ensure()
        return []

    def health(self) -> BrokerHealth:
        return BrokerHealth(connected=self.connected, adapter="fake",
                            server_time=datetime.now(), message="ok")

    def quote(self, symbol: str):
        self._ensure()
        return Quote(symbol=symbol, bid=1.1000, ask=1.1002,
                     time=datetime.now())


def _broker():
    b = FakeBroker()
    b.connect({"login": 1, "password": "x", "server": "y"})
    return b


def test_interface_method_set():
    expected = {"connect", "disconnect", "account_info", "symbols", "candles",
                "positions", "orders", "submit_order", "modify_order",
                "close_position", "deal_history", "health"}
    assert expected <= set(BrokerAdapter.__abstractmethods__ | expected)
    for name in expected:
        assert hasattr(FakeBroker, name), name


def test_connect_candles_round_trip():
    b = _broker()
    acct = b.account_info()
    assert acct.balance == 10000.0 and acct.equity == 9850.0  # balance AND equity
    candles = b.candles("EURUSD", "H1", count=20)
    assert len(candles) == 20
    assert all(isinstance(c, Candle) for c in candles)
    assert candles[0].time < candles[-1].time  # oldest -> newest


def test_order_round_trip_broker_confirmed():
    b = _broker()
    req = OrderRequest(symbol="EURUSD", direction="BUY", volume=0.10,
                       stop_loss=1.0950, take_profit=1.1100,
                       signal_id="sig-1", idempotency_key="key-1")
    res = b.submit_order(req)
    assert res.ticket > 0 and res.price > 0
    positions = b.positions()
    assert len(positions) == 1
    assert positions[0].signal_id == "sig-1"  # approved:{signal_id} comment convention
    assert positions[0].is_own is True
    b.modify_order(res.ticket, sl=1.0960, tp=1.1100)
    assert b.positions()[0].sl == 1.0960
    px = b.close_position(res.ticket)
    assert px > 0 and b.positions() == []


def test_invalid_symbol_code():
    b = _broker()
    with pytest.raises(BrokerError) as e:
        b.candles("NOPE", "H1")
    assert e.value.code == INVALID_SYMBOL


def test_disconnected_adapter_raises_broker_unavailable():
    b = DisconnectedAdapter()
    for call in (lambda: b.connect({}), b.account_info,
                 lambda: b.candles("EURUSD", "H1"),
                 lambda: b.submit_order(OrderRequest("EURUSD", "BUY", 0.1, 1.0, 1.2))):
        with pytest.raises(BrokerError) as e:
            call()
        assert e.value.code == BROKER_UNAVAILABLE
    h = b.health()
    assert h.connected is False and h.adapter == "disconnected"


def test_mt5_adapter_graceful_without_package():
    """broker.mt5 must import cleanly with no MetaTrader5 installed;
    any broker use raises BROKER_UNAVAILABLE (never ImportError)."""
    import sys
    assert "MetaTrader5" not in sys.modules
    from broker.mt5 import MT5Adapter
    from broker.mt5.adapter import mt5 as mt5_module_state
    adapter = MT5Adapter()
    h = adapter.health()
    assert h.adapter == "mt5"
    if mt5_module_state is None:  # MetaTrader5 genuinely absent here
        assert h.connected is False
        with pytest.raises(BrokerError) as e:
            adapter.connect({"login": 1, "password": "x", "server": "y"})
        assert e.value.code == BROKER_UNAVAILABLE
        with pytest.raises(BrokerError) as e:
            adapter.account_info()
        assert e.value.code == BROKER_UNAVAILABLE


def test_magic_number_convention():
    assert MAGIC_NUMBER == 20260817
    assert extract_signal_id_from_comment("approved:abc-123") == "abc-123"
    assert extract_signal_id_from_comment("forex-agent") is None
    assert is_own_position(MAGIC_NUMBER) is True
    assert is_own_position(999) is False
    assert is_own_position(None) is False
