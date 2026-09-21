"""Interface tests: event bus (agent/events).

FakeStore implements the REAL storage.Store surface (enqueue_event /
dequeue_events / journal_add / journal_query) in memory, so these tests
verify the bus against the exact contract the storage builder documented
in storage/API_DEPS.md — without touching the real database.

Covers: schema validation rejects malformed events; publish dual-writes
(queue + pollable log); poll is non-destructive and chronological;
pub/sub delivery; webhook best-effort.
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
    """In-memory stand-in for storage.Store (real method names)."""

    def __init__(self):
        self.queue = []
        self.journal = []
        self._id = 0
        self.lock = threading.Lock()

    def enqueue_event(self, event):
        with self.lock:
            self._id += 1
            self.queue.append(dict(event))
            return self._id

    def dequeue_events(self, limit=100):
        with self.lock:
            out, self.queue = self.queue[:limit], self.queue[limit:]
            return out

    def journal_add(self, entry):
        with self.lock:
            self._id += 1
            stored = dict(entry)
            stored["_journal_id"] = self._id
            stored["_ts"] = entry.get("payload", {}).get("ts", "")
            self.journal.append(stored)
            return self._id

    def journal_query(self, limit=100, **filters):
        with self.lock:
            items = list(self.journal)
        kind = filters.get("kind")
        if kind:
            items = [e for e in items if e.get("kind") == kind]
        since = filters.get("since")
        if since:
            items = [e for e in items if e.get("_ts", "") >= since]
        items.sort(key=lambda e: e.get("_ts", ""), reverse=True)
        return items[:limit]


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
        evt = validate_event({"event": "broker.disconnected", "adapter": "mt5"})
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
            validate_event({"event": "risk.blocked", "reason": "DAILY_LOSS_LIMIT",
                            "halting_problem": "solved"})
        self.assertIn("halting_problem", str(ctx.exception))

    def test_malformed_ts_rejected(self):
        with self.assertRaises(ValueError):
            validate_event({"event": "risk.blocked", "reason": "X", "ts": "not-a-time"})

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            validate_event(["signal.detected"])

    def test_all_schema_types_have_field_lists(self):
        for name, schema in EVENT_SCHEMAS.items():
            self.assertIsInstance(schema["required"], list, name)
            self.assertIsInstance(schema["optional"], list, name)

    def test_target_arch_examples_validate(self):
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


class TestPublishAndPoll(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self.store = FakeStore()
        bus.configure(store=self.store)

    def tearDown(self):
        reset_for_tests()

    def test_publish_dual_writes_queue_and_log(self):
        publish({"event": "signal.detected", "symbol": "EURUSD",
                 "timeframe": "M15", "direction": "SELL"})
        self.assertEqual(len(self.store.queue), 1)
        self.assertEqual(self.store.queue[0]["event"], "signal.detected")
        logged = [e for e in self.store.journal if e["kind"] == "event"]
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["payload"]["symbol"], "EURUSD")

    def test_poll_is_chronological_and_non_destructive(self):
        publish({"event": "risk.blocked", "reason": "A",
                 "ts": "2026-09-21T10:00:00+00:00"})
        publish({"event": "risk.blocked", "reason": "B",
                 "ts": "2026-09-21T11:00:00+00:00"})
        first = poll(limit=10)
        second = poll(limit=10)
        self.assertEqual([e["reason"] for e in first], ["A", "B"])
        self.assertEqual(first, second)  # polling never consumes
        # ...and the durable queue is untouched by polling.
        self.assertEqual(len(self.store.queue), 2)

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

    def test_queue_drain_is_separate_from_poll(self):
        # The Worker-sync consumer drains the FIFO queue; the agent log stays.
        publish({"event": "trade.executed", "symbol": "EURUSD", "ticket": "1",
                 "side": "BUY", "volume": 0.1})
        drained = self.store.dequeue_events(limit=10)
        self.assertEqual(len(drained), 1)
        self.assertEqual(self.store.dequeue_events(), [])
        self.assertEqual(len(poll(limit=10)), 1)  # log intact


class TestPubSub(unittest.TestCase):
    def setUp(self):
        reset_for_tests()
        self.store = FakeStore()
        bus.configure(store=self.store)

    def tearDown(self):
        reset_for_tests()

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
        self.assertEqual(len(self.store.queue), 1)  # still persisted

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
        self.assertEqual(self.store.queue, [])
        self.assertEqual(received, [])


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
            bus.configure(webhook_url="http://127.0.0.1:%d/hook" % port)
            publish({"event": "kill_switch.activated", "source": "agent"})
        finally:
            server.shutdown()
            thread.join()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["event"], "kill_switch.activated")

    def test_webhook_failure_does_not_break_publish(self):
        bus.configure(webhook_url="http://127.0.0.1:1/nope")
        evt = publish({"event": "risk.blocked", "reason": "X"})
        self.assertEqual(evt["reason"], "X")
        self.assertEqual(len(self.store.queue), 1)


if __name__ == "__main__":
    unittest.main()
