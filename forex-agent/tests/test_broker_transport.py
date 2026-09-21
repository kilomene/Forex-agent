"""
Phase 2 — MT5/Linux transport tests.

Covers: honest BROKER_UNAVAILABLE when no MT5 runtime exists (no fake
data anywhere); remote gateway unreachable; structured broker_status()
for configured-but-unreachable vs connected; arbitrary-command rejection
at the gateway contract; remote gateway auth required. No MetaTrader5
import anywhere in this file.
"""

import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from broker import (
    AccountInfo,
    BrokerError,
    OrderRequest,
    OrderResult,
    SymbolSpec,
    BROKER_UNAVAILABLE,
)
from broker.disconnected import DisconnectedAdapter
from broker.mt5 import (
    OPERATIONS,
    MT5Adapter,
    MT5Transport,
    RemoteMT5GatewayTransport,
)


# ---------------------------------------------------------------------------
# Fake gateway HTTP server (contract-conformant, bearer-token guarded)
# ---------------------------------------------------------------------------

SERVER_TOKEN = "test-token-123"


class _GatewayHandler(BaseHTTPRequestHandler):
    def _auth_ok(self):
        return self.headers.get("Authorization") == f"Bearer {SERVER_TOKEN}"

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        if not self._auth_ok():
            self._send({"ok": False, "error_code": "AUTH",
                        "message": "bad token"}, 401)
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        if self.path == "/api/v1/ping":
            self._send({"ok": True, "data": {}})
        elif self.path == "/api/v1/account":
            self._send({"ok": True, "data": {
                "balance": 10000.0, "equity": 9900.0, "currency": "USD",
                "margin": 10.0, "free_margin": 9890.0, "leverage": 100,
                "login": 123, "server": "Test-Server"}})
        elif self.path == "/api/v1/terminal":
            self._send({"ok": True, "data": {
                "trade_allowed": True, "connected": True,
                "server": "Test-Server"}})
        elif self.path == "/api/v1/positions":
            self._send({"ok": True, "data": {"positions": []}})
        else:
            self._send({"ok": False, "error_code": "NOT_FOUND",
                        "message": "unknown endpoint"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        if not self._guard():
            return
        if self.path == "/api/v1/symbols":
            self._send({"ok": True, "data": {"symbols": [{
                "name": "EURUSD", "volume_min": 0.01, "volume_max": 100.0,
                "volume_step": 0.01, "tick_value": 1.0,
                "tick_size": 0.00001, "contract_size": 100000.0,
                "digits": 5, "point": 0.00001}]}})
        elif self.path == "/api/v1/quote":
            if payload.get("symbol") == "NOPE":
                # Semantic failure: passes through as a broker error code.
                self._send({"ok": False, "error_code": "INVALID_SYMBOL",
                            "message": "unknown symbol NOPE"})
            else:
                self._send({"ok": True, "data": {
                    "available": True, "bid": 1.1000, "ask": 1.1002,
                    "time": datetime.now().isoformat()}})
        else:
            self._send({"ok": False, "error_code": "NOT_FOUND",
                        "message": "unknown endpoint"}, 404)

    def log_message(self, *args):  # keep test output clean
        pass


@pytest.fixture()
def gateway_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------------------
# In-memory transport stub (for the "connected" status case)
# ---------------------------------------------------------------------------

class StubTransport(MT5Transport):
    def ping(self): return True
    def check(self): return None
    def terminal_status(self):
        return {"trade_allowed": True, "connected": True, "server": "Stub"}
    def account_info(self):
        return AccountInfo(balance=10000.0, equity=9900.0, currency="USD",
                           login=7, server="Stub")
    def symbols(self, names=None):
        return [SymbolSpec(name="EURUSD", volume_min=0.01, volume_max=100.0,
                           volume_step=0.01, tick_value=1.0, tick_size=0.00001)]
    def candles(self, symbol, timeframe, count=200): return []
    def quote(self, symbol): return None
    def positions(self): return []
    def orders(self): return []
    def submit_order(self, req):
        return OrderResult(ticket=1, symbol=req.symbol,
                           direction=req.direction, volume=req.volume, price=1.1)
    def modify_position(self, ticket, sl, tp): return None
    def close_position(self, ticket): return 1.1
    def deal_history(self, from_, to, position_id=None): return []


# ---------------------------------------------------------------------------
# 1. No runtime -> honest BROKER_UNAVAILABLE, never fake data
# ---------------------------------------------------------------------------

def test_mt5_unavailable_is_honest(monkeypatch):
    from broker.mt5.adapter import mt5 as mt5_state
    if mt5_state is not None:
        pytest.skip("MetaTrader5 genuinely present in this environment")
    monkeypatch.delenv("MT5_GATEWAY_URL", raising=False)

    adapter = MT5Adapter()
    assert adapter.transport_mode == "local"

    h = adapter.health()
    assert h.connected is False

    ops = [
        lambda: adapter.account_info(),
        lambda: adapter.symbols(),
        lambda: adapter.positions(),
        lambda: adapter.orders(),
        lambda: adapter.candles("EURUSD", "H1"),
        lambda: adapter.deal_history(datetime.now() - timedelta(days=1),
                                     datetime.now()),
        lambda: adapter.submit_order(OrderRequest(
            symbol="EURUSD", direction="BUY", volume=0.01,
            stop_loss=1.0900, take_profit=1.1100)),
        lambda: adapter.connect({"login": 1, "password": "x", "server": "y"}),
    ]
    for op in ops:
        with pytest.raises(BrokerError) as exc_info:
            op()
        assert exc_info.value.code == BROKER_UNAVAILABLE
        # The message must explain WHY — never a bare failure.
        assert "RUNTIME.md" in exc_info.value.message

    st = adapter.broker_status()["broker"]
    assert st["provider"] == "mt5"
    assert st["mode"] == "local"
    assert st["configured"] is True  # provider selected, runtime missing
    for flag in ("reachable", "connected", "account_available",
                 "market_data_available", "trading_available"):
        assert st[flag] is False, flag
    assert st["detail"].get("reason")


def test_disconnected_status_is_all_false():
    st = DisconnectedAdapter().broker_status()["broker"]
    assert st["provider"] == "disconnected"
    assert st["configured"] is False
    for flag in ("reachable", "connected", "account_available",
                 "market_data_available", "trading_available"):
        assert st[flag] is False, flag


# ---------------------------------------------------------------------------
# 2. Remote gateway unreachable -> structured errors, no fake data
# ---------------------------------------------------------------------------

def test_remote_gateway_unreachable(monkeypatch):
    monkeypatch.setenv("MT5_GATEWAY_URL", "http://127.0.0.1:1")  # closed port
    monkeypatch.setenv("MT5_GATEWAY_TOKEN", "tok")

    adapter = MT5Adapter()  # auto-detects remote from env
    assert adapter.transport_mode == "remote"

    with pytest.raises(BrokerError) as exc_info:
        adapter.connect({})
    assert exc_info.value.code == "GATEWAY_UNREACHABLE"
    assert "127.0.0.1" in str(exc_info.value.detail.get("url", ""))

    # health() must never raise, even with the gateway down
    assert adapter.health().connected is False

    # Operations report honestly instead of fabricating data.
    with pytest.raises(BrokerError) as exc_info:
        adapter.account_info()
    assert exc_info.value.code == "MT5_NOT_CONNECTED"

    st = adapter.broker_status()["broker"]
    assert st["provider"] == "mt5"
    assert st["mode"] == "remote"
    assert st["configured"] is True   # configured...
    assert st["reachable"] is False   # ...but unreachable
    assert st["connected"] is False
    assert st["account_available"] is False
    assert st["market_data_available"] is False
    assert st["trading_available"] is False


# ---------------------------------------------------------------------------
# 3. broker_status(): configured-but-unreachable vs connected
# ---------------------------------------------------------------------------

def test_broker_status_connected_stub():
    adapter = MT5Adapter(transport=StubTransport())
    adapter.connect({})  # stub check() passes
    st = adapter.broker_status()["broker"]
    assert st["mode"] == "remote"
    assert st["configured"] is True
    for flag in ("reachable", "connected", "account_available",
                 "market_data_available", "trading_available"):
        assert st[flag] is True, flag


def test_broker_status_never_raises():
    class BrokenTransport(MT5Transport):
        def ping(self): raise RuntimeError("boom")
        def check(self): raise RuntimeError("boom")
        def terminal_status(self): raise RuntimeError("boom")
        def account_info(self): raise RuntimeError("boom")
        def symbols(self, names=None): raise RuntimeError("boom")
        def candles(self, s, tf, count=200): raise RuntimeError("boom")
        def quote(self, s): raise RuntimeError("boom")
        def positions(self): raise RuntimeError("boom")
        def orders(self): raise RuntimeError("boom")
        def submit_order(self, req): raise RuntimeError("boom")
        def modify_position(self, t, sl, tp): raise RuntimeError("boom")
        def close_position(self, t): raise RuntimeError("boom")
        def deal_history(self, f, t, pid=None): raise RuntimeError("boom")

    adapter = MT5Adapter(transport=BrokenTransport())
    st = adapter.broker_status()["broker"]  # must not raise
    assert st["reachable"] is False
    assert st["connected"] is False


# ---------------------------------------------------------------------------
# 4. Arbitrary commands are rejected at the gateway contract
# ---------------------------------------------------------------------------

def test_operation_allowlist_is_closed():
    assert set(OPERATIONS) == {
        "ping", "terminal", "account", "symbols", "market_data", "quote",
        "positions", "orders", "submit", "modify", "close", "deal_history",
    }


def test_no_generic_execute_api():
    for attr in ("execute", "command", "rpc", "run", "eval", "raw_request",
                 "send_command"):
        assert not hasattr(RemoteMT5GatewayTransport, attr), attr


def test_arbitrary_operation_rejected_before_network():
    t = RemoteMT5GatewayTransport("http://127.0.0.1:9", token="x")
    for bogus in ("drop_table", "exec", "../../../etc/passwd", "SUBMIT "):
        with pytest.raises(BrokerError) as exc_info:
            t._request(bogus, {})
        assert exc_info.value.code == "GATEWAY_REJECTED"
        assert "arbitrary" in exc_info.value.message


# ---------------------------------------------------------------------------
# 5. Remote gateway auth is mandatory
# ---------------------------------------------------------------------------

def test_remote_gateway_token_required(monkeypatch):
    monkeypatch.delenv("MT5_GATEWAY_TOKEN", raising=False)
    with pytest.raises(BrokerError) as exc_info:
        RemoteMT5GatewayTransport("http://127.0.0.1:9", token="")
    assert exc_info.value.code == "CREDENTIALS_INVALID"

    # Env auto-detect refuses to build an unauthenticated adapter too.
    monkeypatch.setenv("MT5_GATEWAY_URL", "http://127.0.0.1:9")
    with pytest.raises(BrokerError) as exc_info:
        MT5Adapter()
    assert exc_info.value.code == "CREDENTIALS_INVALID"


def test_gateway_401_maps_to_auth_failed(gateway_server):
    t = RemoteMT5GatewayTransport(gateway_server, token="wrong-token")
    with pytest.raises(BrokerError) as exc_info:
        t.check()
    assert exc_info.value.code == "GATEWAY_AUTH_FAILED"
    assert t.ping() is False  # bool probe never raises


def test_gateway_roundtrip_and_semantic_passthrough(gateway_server):
    t = RemoteMT5GatewayTransport(gateway_server, token=SERVER_TOKEN)
    t.check()  # raises on failure
    assert t.ping() is True

    acct = t.account_info()
    assert acct.balance == 10000.0 and acct.equity == 9900.0
    assert acct.currency == "USD"

    syms = t.symbols(["EURUSD"])
    assert [s.name for s in syms] == ["EURUSD"]
    assert syms[0].volume_step == 0.01

    # Semantic broker failure passes through with its own code.
    with pytest.raises(BrokerError) as exc_info:
        t.quote("NOPE")
    assert exc_info.value.code == "INVALID_SYMBOL"

    # Adapter-level client validation still applies on the remote path.
    adapter = MT5Adapter(transport=t)
    adapter.connect({})
    with pytest.raises(BrokerError) as exc_info:
        adapter.submit_order(OrderRequest(
            symbol="EURUSD", direction="SIDEWAYS", volume=0.01,
            stop_loss=1.09, take_profit=1.11))
    assert exc_info.value.code == "INVALID_ORDER"

    st = adapter.broker_status()["broker"]
    assert st["reachable"] is True and st["connected"] is True
    assert st["account_available"] is True
    assert st["market_data_available"] is True
    assert st["trading_available"] is True
