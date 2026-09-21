"""Interface tests: event bus (agent/events).

Uses a fake in-memory store — never touches storage/, broker/, or core/.
Covers: schema validation rejects malformed events, pub/sub delivery,
persistence to the queue, poll(since, limit), and webhook best-effort.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent.events import bus
from agent.events.bus import (
    EVENT_SCHEMAS,
    poll,
    publish,
    reset_for_tests,
    subscribe,
    unsubscribe,
    validate_event,
)


class FakeStore:
    """Minimal stand-in for storage.store.Store (see agent/API_DEPS.md)."""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def append_event(self, event):
        with self.lock:
            self.events.append(dict(event))

    def get_events(self, since=None, limit=100):
        with self.lock:
            items = list(self.events)
        if since is not None:
            items = [e for e in items if e["ts"] >= since]
        return items[-limit:]


class TestSchemaValidation(unittest.TestCase):
    def test_valid_signal_detected(self):
        evt = validate_event({
            "event": "signal.detected",
            "ts": "2026-09-21T12:00:00+00:00",
            "symbol": "EURUSD",
            "timeframe": "M15",
            "direction": "BUY",
        })
        self.assertEqual(evt["symbol"], "EURUSD")

    def test_missing_ts_gets_stamped(self):
        evt = validate_event({
            "event": "broker.disconnected",
            "adapter": "mt5",
        })
        self.assertIn("ts", evt)
        self.assertTrue(evt["ts"].endswith("+00:00"))

    def test_unknown_event_type_rejected(self):
        with self.assertRaises(ValueError):
            validate_event({"event": "trade.teleported", "ts": "2026-09-21T12:00:00+00:00"})

    def test_missing_required_field_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_event({"event": "trade.executed", "symbol": "EURUSD"})
        self.assertIn("ticket", str(ctx.exception))

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_event({
                "event": "risk.blocked",
                "reason": "DAILY_LOSS_LIMIT",
                "halting_problem": "solved",
            })
        self.assertIn("halting_problem", str(ctx.exception))

    def test_malformed_ts_rejected(self):
        with self.assertRaises(ValueError):
            validate_event({"event": "risk.blocked", "reason": "X", "ts": "not-a-time"})

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            validate_event(["signal.detected"])

    def test_all_schema_examples_validate(self):
        # Every documented example shape must at least declare required fields.
        for name, schema in EVENT_SCHEMAS.items():
            self.assertIsInstance(schema["required"], list, name)
            self.assertIsInstance(schema["optional"], list, name)

    def test_target_arch_examples_validate(self):
        # The exact example events from TARGET_ARCHITECTURE section 4.
        validate_event({"event": "signal.detected", "ts": "2026-09-21T00:00:00+00:00",
                        "symbol": "EURUSD", "timeframe": "M15", "direction": "BUY",
                        "confidence": 0.81, "strategy": "ema_rsi"})
        validate_event({"event": "trade.executed", "ts": "2026-09-21T00:00:00+00:00",
                        "symbol": "EURUSD", "ticket": "123456", "side": "BUY",
                        "volume": 0.10, "idempotency_key": "abc"})
        validate_event({"event": "risk.blocked", "ts": "2026-09-21T00:00:00+00:00",
                        "reason": "DAILY_LOSS_LIMIT", "detail": {}})
        validate_event({"event": "broker.disconnected", "ts": "2026-09-21T00:00:00+00:00",
                        "adapter": "mt5"})
        validate_event({"event": "kill_switch.activated", "ts": "2026-09-21T00:00:00+00:00",
                        "source": "agent"})


class TestPubSub(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self.store = FakeStore()
        bus.configure(store=self.store)

    def tearDown(self):
        reset_for_tests()

    def test_publish_persists_to_queue(self):
        publish({"event": "signal.detected", "symbol": "EURUSD",
                 "timeframe": "M15", "direction": "SELL"})
        self.assertEqual(len(self.store.events), 1)
        self.assertEqual(self.store.events[0]["event"], "signal.detected")
        self.assertIn("ts", self.store.events[0])

    def test_subscriber_receives_event(self):
        received = []
        subscribe("trade.executed", received.append)
        evt = publish({"event": "trade.executed", "symbol": "EURUSD",
                       "ticket": "1", "side": "BUY", "volume": 0.1})
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["ticket"], "1")
        self.assertEqual(received[0]["ts"], evt["ts"])

    def test_wildcard_subscriber(self):
        received = []
        subscribe("*", received.append)
        publish({"event": "risk.blocked", "reason": "X"})
        publish({"event": "broker.disconnected", "adapter": "mt5"})
        self.assertEqual(len(received), 2)

    def test_failing_subscriber_does_not_break_others(self):
        good = []
        def bad(evt):
            raise RuntimeError("boom")
        subscribe("risk.blocked", bad)
        subscribe("risk.blocked", good.append)
        publish({"event": "risk.blocked", "reason": "X"})
        self.assertEqual(len(good), 1)
        self.assertEqual(len(self.store.events), 1)  # still persisted

    def test_unsubscribe(self):
        received = []
        subscribe("risk.blocked", received.append)
        self.assertTrue(unsubscribe("risk.blocked", received.append))
        publish({"event": "risk.blocked", "reason": "X"})
        self.assertEqual(received, [])

    def test_invalid_event_never_persists_or_dispatches(self):
        received = []
        subscribe("*", received.append)
        with self.assertRaises(ValueError):
            publish({"event": "nope.not_real", "x": 1})
        self.assertEqual(self.store.events, [])
        self.assertEqual(received, [])


class TestPoll(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self.store = FakeStore()
        bus.configure(store=self.store)

    def tearDown(self):
        reset_for_tests()

    def test_poll_returns_persisted_events(self):
        publish({"event": "risk.blocked", "reason": "A",
                 "ts": "2026-09-21T10:00:00+00:00"})
        publish({"event": "risk.blocked", "reason": "B",
                 "ts": "2026-09-21T11:00:00+00:00"})
        got = poll(limit=10)
        self.assertEqual([e["reason"] for e in got], ["A", "B"])

    def test_poll_since_filters(self):
        publish({"event": "risk.blocked", "reason": "A",
                 "ts": "2026-09-21T10:00:00+00:00"})
        publish({"event": "risk.blocked", "reason": "B",
                 "ts": "2026-09-21T11:00:00+00:00"})
        got = poll(since="2026-09-21T10:30:00+00:00")
        self.assertEqual([e["reason"] for e in got], ["B"])

    def test_poll_limit(self):
        for i in range(5):
            publish({"event": "risk.blocked", "reason": str(i)})
        self.assertEqual(len(poll(limit=2)), 2)

    def test_poll_bad_since_rejected(self):
        with self.assertRaises(ValueError):
            poll(since="yesterday-ish")

    def test_poll_bad_limit_rejected(self):
        with self.assertRaises(ValueError):
            poll(limit=0)


class TestWebhook(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self.store = FakeStore()
        bus.configure(store=self.store)

    def tearDown(self):
        reset_for_tests()

    def test_webhook_receives_event(self):
        delivered = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                delivered.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bus.configure(webhook_url=f"http://127.0.0.1:{port}/hook")
            publish({"event": "kill_switch.activated", "source": "agent"})
        finally:
            server.shutdown()
            thread.join()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["event"], "kill_switch.activated")

    def test_webhook_failure_does_not_break_publish(self):
        # Nothing listening on this port: must still persist + return.
        bus.configure(webhook_url="http://127.0.0.1:1/nope")
        evt = publish({"event": "risk.blocked", "reason": "X"})
        self.assertEqual(evt["reason"], "X")
        self.assertEqual(len(self.store.events), 1)


if __name__ == "__main__":
    unittest.main()
