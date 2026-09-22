"""Interface tests: agent tool registry (agent/tools).

Tests run against the REAL sibling implementations the build master now
has in place:
  - real storage.Store (tmp database per test, never the dev/prod DB)
  - real BrokerAdapter subclass (FakeBroker, deterministic candles)
  - real core.indicators / core.smc / core.strategies / core.market
  - real intelligence.correlation / intelligence.experience
  - FakeGateway standing in for core.execution.gateway (not built yet)

FakeBroker.submit_order/modify_order/close_position RAISE if called, so
any direct broker call from a trading tool fails the suite loudly.

Invariants: trading tools route ONLY through the gateway; dry-run blocks
by default; kill-switch state reads from the local storage latch;
MetaTrader5 is never imported outside broker/mt5/.
"""

import ast
import glob
import os
import sys
import tempfile
import unittest

# Fresh, isolated environment BEFORE any agent code is imported.
_TMP = tempfile.mkdtemp(prefix="forex_iface_tools_")
os.environ["FOREX_AGENT_HOME"] = _TMP
os.environ["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "test.db")
os.environ.setdefault("BROKER_PROVIDER", "disconnected")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.events import bus  # noqa: E402
from agent.tools import backend, registry, trading  # noqa: E402
from agent.tools.registry import get_capability as _get_capability  # noqa: E402
try:
    from tests.test_iface_fakes import FakeBroker, FakeGateway  # noqa: E402
except ImportError:  # direct script run: tests/ is sys.path[0]
    from fakes import FakeBroker, FakeGateway  # noqa: E402


def tool(name):
    """The raw capability function (registry.call wraps this for dict args)."""
    return _get_capability(name)["func"]


def _fresh_store():
    """A brand-new real Store database for one test."""
    from storage import Store
    path = os.path.join(_TMP, "store_%s.db" % next(_fresh_store.counter))
    store = Store(path)
    bus.configure(store=store)
    backend.set_override("store", store)
    return store


_fresh_store.counter = iter(range(100000))


class BaseToolTest(unittest.TestCase):
    def setUp(self):
        bus.reset_for_tests()
        backend.reset_overrides()
        self.store = _fresh_store()
        self.broker = FakeBroker()
        backend.set_override("broker", self.broker)
        self.gateway = FakeGateway(store=self.store)
        backend.set_override("gateway", self.gateway)

    def tearDown(self):
        bus.reset_for_tests()
        backend.reset_overrides()


class TestRegistry(BaseToolTest):
    def test_registry_has_all_capabilities(self):
        names = set(c["name"] for c in registry.list_capabilities())
        expected = {
            "forex.market_data", "forex.analyze", "forex.get_signal",
            "forex.get_signals", "forex.get_positions", "forex.get_account",
            "forex.get_risk", "forex.get_performance", "forex.get_trade_history",
            "forex.get_smc", "forex.get_session", "forex.get_calendar",
            "forex.get_experience", "forex.get_health", "forex.request_trade",
            "forex.close_position", "forex.modify_position", "forex.kill_switch",
        }
        self.assertEqual(names, expected)

    def test_each_capability_is_callable(self):
        for name in (c["name"] for c in registry.list_capabilities()):
            self.assertTrue(callable(tool(name)), name)

    def test_unknown_tool_raises(self):
        with self.assertRaises(KeyError):
            tool("forex.teleport")


class TestMarketData(BaseToolTest):
    def test_market_data_returns_closed_candles(self):
        result = tool("forex.market_data")("EURUSD", "M15", 5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 5)
        self.assertEqual(result["symbol"], "EURUSD")
        for row in result["candles"]:
            for key in ("time", "open", "high", "low", "close"):
                self.assertIn(key, row)

    def test_market_data_bad_symbol(self):
        result = tool("forex.market_data")("", "M15", 5)
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result["error_code"], "INVALID_SYMBOL")


class TestAnalyze(BaseToolTest):
    def test_analyze_returns_real_indicators(self):
        result = tool("forex.analyze")("EURUSD", "M15")
        self.assertTrue(result["ok"])
        ind = result["indicators"]
        for key in ("ema_fast", "ema_slow", "rsi", "atr"):
            self.assertIn(key, ind)
        self.assertGreater(ind["ema_fast"], 0)
        self.assertGreaterEqual(ind["rsi"], 0)
        self.assertLessEqual(ind["rsi"], 100)
        self.assertGreater(ind["atr"], 0)

    def test_analyze_returns_smc_structure(self):
        result = tool("forex.analyze")("EURUSD", "M15")
        smc = result["smc"]
        self.assertTrue(smc.get("available"))
        self.assertIn("market_structure", smc)
        self.assertIn("order_blocks", smc)
        self.assertIn("fair_value_gaps", smc)

    def test_analyze_strategy_key_present(self):
        result = tool("forex.analyze")("EURUSD", "M15")
        self.assertIn("strategy_signal", result)
        self.assertIn("strategy", result)

    def test_analyze_ml_is_honest_stub(self):
        # intelligence.ml serving is PARKED: the real module reports
        # available:false itself; the tool passes it through.
        result = tool("forex.analyze")("EURUSD", "M15")
        ml = result["ml_prediction"]
        self.assertFalse(ml["available"])
        self.assertIn("reason", ml)

    def test_get_smc(self):
        result = tool("forex.get_smc")("EURUSD", "M15")
        self.assertTrue(result.get("available"))
        self.assertEqual(result["symbol"], "EURUSD")
        self.assertIn("liquidity_zones", result)


class TestAccount(BaseToolTest):
    def test_get_positions_empty(self):
        result = tool("forex.get_positions")()
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["positions"], [])

    def test_get_account_reports_equity(self):
        result = tool("forex.get_account")()
        self.assertTrue(result["ok"])
        acct = result["account"]
        self.assertEqual(acct["balance"], 10000.0)
        self.assertEqual(acct["equity"], 10050.0)
        self.assertEqual(acct["currency"], "USD")

    def test_get_risk_hypothetical_trade(self):
        # tick_value=1.0, tick_size=0.00001, stop 0.0020 -> 200 ticks
        # 200 * 1.0 * 0.1 lot = $20
        result = tool("forex.get_risk")(
            symbol="EURUSD", direction="BUY", lot_size=0.1,
            stop_loss_distance=0.0020)
        self.assertTrue(result["ok"])
        self.assertFalse(result["kill_switch_engaged"])
        pr = result["position_risk"]
        self.assertEqual(pr["estimated_dollar_risk"], 20.0)
        self.assertEqual(result["open_position_count"], 0)

    def test_get_risk_standing_state(self):
        result = tool("forex.get_risk")()
        self.assertTrue(result["ok"])
        self.assertIn("risk_state", result)
        self.assertIn("correlation_flags", result)

    def test_get_performance_no_closed_trades(self):
        # FakeBroker.deal_history() is empty: honest zero-trade report,
        # computed from real (empty) deal history - never invented.
        result = tool("forex.get_performance")()
        self.assertTrue(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["total_closed_trades"], 0)
        self.assertIn("win_rate_pct", result)

    def test_get_trade_history_lifecycle(self):
        bus.publish({"event": "signal.detected", "symbol": "EURUSD",
                     "timeframe": "M15", "direction": "BUY", "signal_id": "sig-1"})
        bus.publish({"event": "trade.executed", "symbol": "EURUSD", "ticket": "1",
                     "side": "BUY", "volume": 0.1, "signal_id": "sig-1"})
        bus.publish({"event": "position.closed", "ticket": "1", "symbol": "EURUSD",
                     "reason": "test", "profit": 12.5})
        result = tool("forex.get_trade_history")()
        self.assertTrue(result["ok"])
        # Only trade events (executed/closed) count as history; the
        # signal.detected event belongs to the signal ledger.
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["records"][0]["event"], "trade.executed")
        filtered = tool("forex.get_trade_history")(symbol="GBPUSD")
        self.assertEqual(filtered["count"], 0)


class TestMisc(BaseToolTest):
    def test_get_session(self):
        result = tool("forex.get_session")()
        self.assertTrue(result["ok"])
        self.assertIn("active_sessions", result)
        self.assertTrue(len(result["active_sessions"]) >= 1)

    def test_get_calendar_unconfigured(self):
        result = tool("forex.get_calendar")()
        self.assertTrue(result["ok"])
        self.assertFalse(result["available"])

    def test_get_experience(self):
        from intelligence.experience import ExperienceStore
        exp = ExperienceStore(self.store)
        exp.record("reflection", symbol="EURUSD",
                   payload={"lesson": "chased a late entry"})
        result = tool("forex.get_experience")(symbol="EURUSD")
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["reflections"][0]["body"]["lesson"],
                         "chased a late entry")

    def test_get_health(self):
        result = tool("forex.get_health")()
        self.assertTrue(result["ok"])
        comps = result["components"]
        self.assertEqual(comps["broker"]["status"], "pass")
        self.assertEqual(comps["kill_switch"]["status"], "pass")
        self.assertEqual(comps["event_bus"]["status"], "pass")
        # Worker unconfigured -> degraded, never fail (local safety unaffected)
        self.assertEqual(comps["worker"]["status"], "degraded")
        self.assertEqual(result["status"], "degraded")


class TestSignals(BaseToolTest):
    def _publish_lifecycle(self, signal_id):
        bus.publish({"event": "signal.detected", "symbol": "EURUSD",
                     "timeframe": "M15", "direction": "BUY",
                     "signal_id": signal_id, "confidence": 0.8})
        bus.publish({"event": "signal.approved", "signal_id": signal_id,
                     "symbol": "EURUSD"})
        bus.publish({"event": "trade.executed", "symbol": "EURUSD", "ticket": "1",
                     "side": "BUY", "volume": 0.1, "signal_id": signal_id,
                     "idempotency_key": "key-" + signal_id})
        bus.publish({"event": "position.closed", "ticket": "1", "symbol": "EURUSD",
                     "reason": "test", "profit": 5.0})
        return tool("forex.get_signal")(signal_id)

    def test_get_signal_lifecycle(self):
        result = self._publish_lifecycle("sig-a")
        self.assertTrue(result["ok"])
        self.assertTrue(result["found"])
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["symbol"], "EURUSD")

    def test_get_signal_not_found(self):
        result = tool("forex.get_signal")("nope")
        self.assertTrue(result["ok"])
        self.assertFalse(result["found"])

    def test_get_signals_lists(self):
        self._publish_lifecycle("sig-b")
        result = tool("forex.get_signals")()
        self.assertTrue(result["ok"])
        ids = [s["signal_id"] for s in result["signals"]]
        self.assertIn("sig-b", ids)

    def test_get_signals_limit(self):
        for i in range(3):
            bus.publish({"event": "signal.detected", "symbol": "EURUSD",
                         "timeframe": "M15", "direction": "BUY",
                         "signal_id": "lim-%d" % i})
        result = tool("forex.get_signals")(limit=2)
        self.assertEqual(len(result["signals"]), 2)


class TestTrading(BaseToolTest):
    def test_request_trade_dry_run_blocked(self):
        result = tool("forex.request_trade")(
            symbol="EURUSD", direction="BUY", volume=0.1, stop_loss=1.0980,
            signal_id="t1")
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result["error_code"], "DRY_RUN_BLOCKED")
        self.assertEqual(self.broker.submit_calls, [])  # adapter untouched

    def test_request_trade_success_never_touches_adapter(self):
        self.gateway.live = True
        result = tool("forex.request_trade")(
            symbol="EURUSD", direction="BUY", volume=0.1, stop_loss=1.0980,
            take_profit=1.1040, signal_id="t2")
        self.assertTrue(result["ok"])
        self.assertEqual(result["ticket"], 777001)
        # Even on success, the tool must not have called the adapter directly.
        self.assertEqual(self.broker.submit_calls, [])

    def test_request_trade_invalid_direction(self):
        result = tool("forex.request_trade")(
            symbol="EURUSD", direction="SIDEWAYS", volume=0.1, stop_loss=1.0980)
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result["error_code"], "INVALID_DIRECTION")

    def test_request_trade_idempotency_reuse(self):
        self.gateway.live = True
        kwargs = dict(symbol="EURUSD", direction="BUY", volume=0.1,
                      stop_loss=1.0980, signal_id="t3",
                      idempotency_key="dup-key")
        first = tool("forex.request_trade")(**kwargs)
        self.assertTrue(first["ok"])
        second = tool("forex.request_trade")(**kwargs)
        self.assertFalse(second.get("ok", True))
        self.assertEqual(second["error_code"], "DUPLICATE_REQUEST")

    def test_close_and_modify_route_through_gateway(self):
        """forex.close_position / forex.modify_position route through the
        execution gateway — never the broker adapter directly (the fake
        broker raises AssertionError on any direct call)."""
        close = tool("forex.close_position")(12345)
        self.assertTrue(close.get("ok"), close)
        self.assertEqual(close["close_price"], 1.1050)
        modify = tool("forex.modify_position")(12345, stop_loss=1.0990)
        self.assertTrue(modify.get("ok"), modify)
        self.assertEqual(modify["stop_loss"], 1.0990)
        # Mandatory SL: the gateway refuses SL removal.
        bad = tool("forex.modify_position")(12345, stop_loss=0)
        self.assertFalse(bad.get("ok", True))
        self.assertEqual(bad["error_code"], "INVALID_ORDER")
        self.assertEqual(self.broker.submit_calls, [])

    def test_kill_switch_engages_latch(self):
        result = tool("forex.kill_switch")(engage=True,
                                                       reason="test engage")
        self.assertTrue(result["ok"])
        state = self.store.get_kill_switch()
        self.assertTrue(state["engaged"])
        names = [e["payload"]["event"]
                 for e in self.store.journal_query(limit=20, kind="event")]
        self.assertIn("kill_switch.activated", names)

    def test_kill_switch_blocks_gateway_trades(self):
        self.gateway.live = True
        tool("forex.kill_switch")(engage=True, reason="latched")
        result = tool("forex.request_trade")(
            symbol="EURUSD", direction="BUY", volume=0.1, stop_loss=1.0980)
        self.assertFalse(result.get("ok", True))
        self.assertEqual(result["error_code"], "KILL_SWITCH_ENGAGED")

    def test_kill_switch_clears_latch(self):
        tool("forex.kill_switch")(engage=True)
        result = tool("forex.kill_switch")(engage=False)
        self.assertTrue(result["ok"])
        self.assertFalse(self.store.get_kill_switch()["engaged"])
        names = [e["payload"]["event"]
                 for e in self.store.journal_query(limit=20, kind="event")]
        self.assertIn("kill_switch.cleared", names)

    def test_trading_py_imports_no_broker(self):
        """AST check: the gateway boundary — trading.py must not import the
        broker package at all, so a trading tool can never reach the adapter."""
        path = os.path.join(os.path.dirname(trading.__file__), "trading.py")
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name == "broker"
                                     or alias.name.startswith("broker."),
                                     "trading.py imports broker")
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "broker")
                self.assertFalse((node.module or "").startswith("broker."))


class TestNoMetaTrader5(unittest.TestCase):
    def test_no_metatrader5_import_anywhere_in_agent_code(self):
        """MetaTrader5 may only ever be imported inside broker/mt5/."""
        base = os.path.join(os.path.dirname(registry.__file__), "..", "..")
        hits = []
        for path in glob.glob(os.path.join(base, "agent", "**", "*.py"),
                              recursive=True):
            tree = ast.parse(open(path).read())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        names = [node.module]
                for name in names:
                    if "metatrader5" in name.lower():
                        hits.append("%s: %s" % (path, name))
        self.assertEqual(hits, [], "MetaTrader5 imported outside broker/mt5/: %s"
                         % hits)


if __name__ == "__main__":
    unittest.main()
