"""Interface tests: daemons + defensive Worker client.

Run from the forex-agent root:
    python3 -m unittest tests.test_iface_daemons -v

Covers:
  * worker_client: contract validation, retries, WORKER_UNAVAILABLE
    semantics, event->endpoint mapping.
  * common: idempotent PID files, stale-PID reclamation.
  * market_monitor: new-candle detection emits market.candle_closed.
  * signal_monitor: emits signal.detected once per candle (dedupe).
  * position_monitor: exit pass runs without crashing on empty book.
  * health_monitor: fail-closed kill-switch poll, worker dead-letter,
    performance snapshot journaling.
"""

import json
import os
import sys
import threading
import unittest
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("FOREX_AGENT_HOME", "/tmp/forex-test-home-daemons")

from tests.test_iface_fakes import FakeBroker, FakeGateway  # noqa: E402
from core.market import Candle  # noqa: E402


# ---------------------------------------------------------------------------
# Fake Worker HTTP server
# ---------------------------------------------------------------------------

class _WorkerHandler(BaseHTTPRequestHandler):
    """Behaviour is driven by class attributes set per-test."""
    routes = {}          # (method, path) -> list of (status, body) responses
    calls = []           # [(method, path, body)]

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(body.decode()) if body else None
        except ValueError:
            parsed = None
        type(self).calls.append((self.command, self.path, parsed))
        queue = type(self).routes.get((self.command, self.path), [])
        if queue:
            status, resp = queue.pop(0)
        else:
            status, resp = 404, {"error": "no route"}
        raw = json.dumps(resp).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _handle

    def log_message(self, *a):  # keep test output clean
        pass


def start_worker_server():
    server = HTTPServer(("127.0.0.1", 0), _WorkerHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class WorkerClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_worker_server()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        from daemon.worker_client import WorkerClient  # noqa: E402
        _WorkerHandler.routes = {}
        _WorkerHandler.calls = []
        self.client = WorkerClient("http://127.0.0.1:%d" % self.port,
                                   api_key="test-key", max_attempts=1)

    def test_get_kill_switch_ok(self):
        _WorkerHandler.routes[("GET", "/kill-switch")] = [
            (200, {"engaged": False, "close_all_requested_at": None})]
        result = self.client.get_kill_switch()
        self.assertEqual(result["engaged"], False)
        # Bearer auth header reached the fake server
        self.assertEqual(len(_WorkerHandler.calls), 1)

    def test_get_kill_switch_contract_violation(self):
        from daemon.worker_client import WorkerUnavailable  # noqa: E402
        _WorkerHandler.routes[("GET", "/kill-switch")] = [
            (200, {"engaged": "yes"})]  # wrong type
        with self.assertRaises(WorkerUnavailable) as ctx:
            self.client.get_kill_switch()
        self.assertEqual(ctx.exception.code, "WORKER_UNAVAILABLE")
        self.assertIn("contract", ctx.exception.reason)

    def test_connection_refused_is_worker_unavailable(self):
        from daemon.worker_client import WorkerClient, WorkerUnavailable  # noqa: E402
        dead = WorkerClient("http://127.0.0.1:1", max_attempts=1)
        with self.assertRaises(WorkerUnavailable) as ctx:
            dead.get_kill_switch()
        self.assertEqual(ctx.exception.code, "WORKER_UNAVAILABLE")

    def test_retries_then_succeeds(self):
        from daemon.worker_client import WorkerClient  # noqa: E402
        _WorkerHandler.routes[("GET", "/kill-switch")] = [
            (500, {"error": "boom"}),
            (500, {"error": "boom"}),
            (200, {"engaged": True}),
        ]
        client = WorkerClient("http://127.0.0.1:%d" % self.port, max_attempts=3)
        self.assertTrue(client.get_kill_switch()["engaged"])
        self.assertEqual(len(_WorkerHandler.calls), 3)

    def test_post_signal_skips_incomplete(self):
        result = self.client.post_signal({"symbol": "EURUSD"})
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(_WorkerHandler.calls, [])  # no HTTP at all

    def test_post_signal_full(self):
        _WorkerHandler.routes[("POST", "/signals")] = [(200, {"ok": True, "id": "s1"})]
        sig = {"symbol": "EURUSD", "timeframe": "H1", "direction": "BUY",
               "entry_price": 1.1, "stop_loss": 1.09, "take_profit": 1.12,
               "candle_time": "2026-09-21T00:00:00+00:00"}
        self.client.post_signal(sig)
        method, path, body = _WorkerHandler.calls[0]
        self.assertEqual((method, path), ("POST", "/signals"))
        self.assertEqual(body["symbol"], "EURUSD")

    def test_sync_events_mapping(self):
        _WorkerHandler.routes[("POST", "/signals")] = [(200, {"ok": True})]
        _WorkerHandler.routes[("POST", "/kill-switch")] = [(200, {"engaged": True})]
        events = [
            {"event": "signal.detected", "symbol": "EURUSD", "timeframe": "H1",
             "direction": "BUY", "entry_price": 1.1, "stop_loss": 1.09,
             "take_profit": 1.12, "candle_time": "2026-09-21T00:00:00+00:00"},
            {"event": "health.check", "ok": True},          # no endpoint -> skipped
            {"event": "kill_switch.activated", "source": "x"},  # -> POST /kill-switch
        ]
        result = self.client.sync_events(events)
        self.assertEqual(result, {"sent": 2, "skipped": 1})
        paths = [c[1] for c in _WorkerHandler.calls]
        self.assertEqual(paths, ["/signals", "/kill-switch"])

    def test_sync_events_raises_on_persistent_failure(self):
        from daemon.worker_client import WorkerUnavailable  # noqa: E402
        _WorkerHandler.routes[("POST", "/signals")] = [(500, {"error": "x"})]
        events = [{"event": "signal.detected", "symbol": "EURUSD", "timeframe": "H1",
                   "direction": "BUY", "entry_price": 1.1, "stop_loss": 1.09,
                   "take_profit": 1.12, "candle_time": "2026-09-21T00:00:00+00:00"}]
        with self.assertRaises(WorkerUnavailable):
            self.client.sync_events(events)


# ---------------------------------------------------------------------------
# PID file semantics
# ---------------------------------------------------------------------------

class PidfileTest(unittest.TestCase):
    def test_acquire_release_idempotent(self):
        from daemon import common  # noqa: E402
        self.assertTrue(common.acquire_pidfile("test_daemon_a"))
        self.assertFalse(common.acquire_pidfile("test_daemon_a"))  # already ours
        common.release_pidfile("test_daemon_a")
        self.assertTrue(common.acquire_pidfile("test_daemon_a"))
        common.release_pidfile("test_daemon_a")
        self.assertIsNone(common.read_pid("test_daemon_a"))

    def test_stale_pid_reclaimed(self):
        from daemon import common  # noqa: E402
        path = common.pid_file("test_daemon_b")
        with open(path, "w") as fh:
            fh.write("999999999")  # almost certainly not alive
        self.assertTrue(common.acquire_pidfile("test_daemon_b"))
        common.release_pidfile("test_daemon_b")


# ---------------------------------------------------------------------------
# Daemon run_once behaviour (isolated store/home per test)
# ---------------------------------------------------------------------------

def _fresh_env(monkey_name):
    """Point the agent home AND the storage DB at a fresh dir.

    storage.Store resolves its path from storage.store.DEFAULT_DB_PATH
    (read from FOREX_AGENT_STORAGE at import time), so patch the module
    global — setting the env var alone is not enough once imported.
    """
    import shutil  # noqa: E402
    from pathlib import Path  # noqa: E402
    home = "/tmp/forex-test-home-%s" % monkey_name
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    os.environ["FOREX_AGENT_HOME"] = home
    os.environ["FOREX_AGENT_STORAGE"] = os.path.join(home, "local.db")
    import storage.store  # noqa: E402
    storage.store.DEFAULT_DB_PATH = Path(home) / "local.db"
    return home


class _TestContext(unittest.TestCase):
    """Base: isolated agent home + patched backend seams."""

    def setUp(self):
        super().setUp()
        _fresh_env("daemons-%s" % self._testMethodName)
        from storage import Store  # noqa: E402
        from agent.events import bus  # noqa: E402
        bus.reset_for_tests()
        self.store = Store()
        bus.configure(store=self.store)
        from agent.tools import backend  # noqa: E402
        self._backend = backend
        self._saved = dict(backend._overrides)
        backend._overrides.clear()
        backend._overrides["broker"] = FakeBroker()
        backend._overrides["gateway"] = FakeGateway()
        backend._overrides["config"] = None

    def tearDown(self):
        self._backend._overrides.clear()
        self._backend._overrides.update(self._saved)
        super().tearDown()


class MarketMonitorTest(_TestContext):
    def test_new_candle_emits_event(self):
        from daemon.market_monitor import MarketMonitor  # noqa: E402
        from agent.events import bus  # noqa: E402

        class ShiftingBroker(FakeBroker):
            shift = 0

            def candles(self, symbol, timeframe, count=200):
                out = super().candles(symbol, timeframe, count=count)
                d = timedelta(minutes=self.shift)
                return [Candle(time=c.time + d, open=c.open, high=c.high,
                               low=c.low, close=c.close, volume=c.volume)
                        for c in out]

        broker = ShiftingBroker()
        self._backend._overrides["broker"] = broker
        mon = MarketMonitor(symbols=["EURUSD"], timeframes=["M15"])

        mon.run_once()  # baseline: no event (nothing new yet)
        events = [e for e in bus.poll() if e.get("event") == "market.candle_closed"]
        self.assertEqual(events, [])

        broker.shift = 30  # a new candle closed
        mon.run_once()
        events = [e for e in bus.poll() if e.get("event") == "market.candle_closed"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["symbol"], "EURUSD")
        self.assertEqual(events[0]["timeframe"], "M15")

        mon.run_once()  # same candle again: no duplicate
        events = [e for e in bus.poll() if e.get("event") == "market.candle_closed"]
        self.assertEqual(len(events), 1)


class SignalMonitorTest(_TestContext):
    def test_emits_signal_once_per_candle(self):
        from daemon.signal_monitor import SignalMonitor  # noqa: E402
        from agent.events import bus  # noqa: E402

        class FakeSignal:
            direction = "BUY"
            entry_price = 1.1
            stop_loss = 1.09
            take_profit = 1.12
            strategy = "ema_rsi"
            trigger = "test trigger"

        calls = []

        def fake_evaluate(symbol, timeframe):
            calls.append((symbol, timeframe))
            return FakeSignal()

        # Seed two closed-candle events.
        bus.publish({"event": "market.candle_closed", "symbol": "EURUSD",
                     "timeframe": "H1", "candle_time": "2026-09-21T00:00:00+00:00"})
        bus.publish({"event": "market.candle_closed", "symbol": "GBPUSD",
                     "timeframe": "H1", "candle_time": "2026-09-21T00:00:00+00:00"})

        mon = SignalMonitor(evaluate=fake_evaluate)
        mon.run_once()
        detected = [e for e in bus.poll() if e.get("event") == "signal.detected"]
        self.assertEqual(len(detected), 2)
        self.assertEqual(detected[0]["signal_id"],
                         "sig:EURUSD:H1:2026-09-21T00:00:00+00:00")

        mon.run_once()  # dedupe: no new events
        detected = [e for e in bus.poll() if e.get("event") == "signal.detected"]
        self.assertEqual(len(detected), 2)
        self.assertEqual(len(calls), 2)  # evaluated only the first time


class PositionMonitorTest(_TestContext):
    def test_exit_pass_empty_book(self):
        from daemon.position_monitor import PositionMonitor  # noqa: E402
        PositionMonitor().run_once()  # must not raise


class HealthMonitorTest(_TestContext):
    def test_fail_closed_on_unreadable_latch(self):
        from daemon.health_monitor import HealthMonitor  # noqa: E402

        class BrokenGateway:
            @property
            def kill_switch(self):
                raise RuntimeError("latch IO failure")

        self._backend._overrides["gateway"] = BrokenGateway()
        mon = HealthMonitor()
        mon.run_once()  # must not raise; fail-closed path exercised
        from agent.events import bus  # noqa: E402
        checks = [e for e in bus.poll() if e.get("event") == "health.check"]
        self.assertTrue(checks)
        self.assertFalse(checks[-1]["checks"]["kill_switch"]["read_ok"])
        self.assertTrue(checks[-1]["checks"]["kill_switch"]["engaged"])

    def test_worker_outage_dead_letters_events(self):
        from daemon.health_monitor import HealthMonitor  # noqa: E402
        from daemon.worker_client import WorkerClient  # noqa: E402
        from agent.events import bus  # noqa: E402

        store = self.store
        # A complete signal so the client actually attempts the HTTP POST
        # (incomplete ones are skipped locally, never sent).
        bus.publish({"event": "signal.detected", "symbol": "EURUSD",
                     "timeframe": "H1", "direction": "BUY", "entry_price": 1.1,
                     "stop_loss": 1.09, "take_profit": 1.12,
                     "candle_time": "2026-09-21T00:00:00+00:00"})
        # Worker that always fails.
        worker = WorkerClient("http://127.0.0.1:1", max_attempts=1)
        mon = HealthMonitor(worker=worker)
        mon.run_once()
        dead = store.journal_query(kind="worker_dead_letter", limit=5)
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0]["detail"]["events"][0]["event"], "signal.detected")

    def test_performance_sync_journals_snapshot(self):
        from daemon.health_monitor import HealthMonitor  # noqa: E402
        from daemon.worker_client import WorkerClient  # noqa: E402
        from agent.events import bus  # noqa: E402

        store = self.store
        worker = WorkerClient("", max_attempts=1)  # disabled worker
        mon = HealthMonitor(worker=worker)
        mon.run_once()  # first run triggers the 6h sync (no prior state)
        snaps = store.journal_query(kind="performance_snapshot", limit=5)
        self.assertEqual(len(snaps), 1)
        mon.run_once()  # second run: interval not elapsed, no duplicate
        snaps = store.journal_query(kind="performance_snapshot", limit=5)
        self.assertEqual(len(snaps), 1)


if __name__ == "__main__":
    unittest.main()
