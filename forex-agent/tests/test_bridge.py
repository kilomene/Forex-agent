"""Integration tests for the persistent agent notification bridge.

Starts the real loopback API server (scripts/local_api) on an ephemeral
port and runs agent.events.bridge.Bridge against it with a fake
host-agent sink. Covers: live delivery with the documented NDJSON
envelope shape, durable cursor advance, restart resume/replay of missed
events, reconnect after an API restart, sink failure not advancing the
cursor, event-id dedup, SubprocessSink executable validation (no shell),
and SubprocessSink NDJSON delivery to a real child process.

Teardown is deterministic: the bridge thread is stopped and joined,
the HTTP server is shut down and closed, and any sink child process is
reaped — a failing test must not leak threads, sockets, or subprocesses.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.events import bridge as bridge_mod
from agent.events import bus
from agent.events.bridge import (AgentNotificationSink, Bridge,
                                 SubprocessSink)
from scripts import local_api
from storage import Store


def _sig_event(**kw):
    base = {"event": "signal.detected", "symbol": "EURUSD",
            "timeframe": "M15", "direction": "BUY"}
    base.update(kw)
    return base


class RecordingSink(AgentNotificationSink):
    """Fake host-agent sink: records delivered envelopes."""

    def __init__(self, fail_with=None):
        self.events = []
        self.fail_with = fail_with
        self._lock = threading.Lock()

    def deliver(self, event):
        if self.fail_with is not None:
            raise self.fail_with
        with self._lock:
            self.events.append(event)

    def health(self):
        with self._lock:
            return {"delivered": len(self.events)}


class BridgeHarness:
    """Real API server + real bridge + fake sink on a throwaway store."""

    def __init__(self):
        bus.reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="forex_bridge_")
        self.db = os.path.join(self.tmp, "bridge.db")
        self.store = Store(self.db)
        bus.configure(store=self.store)
        self.cursor_path = os.path.join(self.tmp, "agent-event.cursor")
        # shrink stream timing so the suite stays fast
        self._poll, self._keep = (local_api._SSE_POLL_INTERVAL_S,
                                  local_api._SSE_KEEPALIVE_S)
        local_api._SSE_POLL_INTERVAL_S = 0.2
        local_api._SSE_KEEPALIVE_S = 1.0
        self.server = None
        self.server_thread = None
        self.bridge = None
        self.sink = RecordingSink()
        self.start_server()

    # -- lifecycle -----------------------------------------------------
    def start_server(self, port=0):
        self.server = local_api.run(port)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=10)
            self.server = None

    def start_bridge(self):
        self.bridge = Bridge(
            api_base="http://127.0.0.1:%d" % self.port,
            cursor_path=self.cursor_path,
            sink=self.sink)
        self.bridge.start()
        # Deterministic sync: only publish once the server has
        # snapshotted this bridge's subscription. Publishing earlier
        # would race the "live tail" snapshot (an event journaled
        # before the server sees the subscriber is legitimately not
        # part of the live stream).
        if not self.bridge.subscribed.wait(timeout=20):
            raise AssertionError("bridge never established its subscription")

    def stop_bridge(self):
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge.join(timeout=15)
            thread = self.bridge._thread
            self.bridge = None
            if thread is not None and thread.is_alive():
                raise AssertionError("bridge thread did not stop")

    def close(self):
        try:
            self.stop_bridge()
        finally:
            self.stop_server()
            local_api._SSE_POLL_INTERVAL_S = self._poll
            local_api._SSE_KEEPALIVE_S = self._keep
            bus.reset_for_tests()
            self.store.close()

    # -- helpers -------------------------------------------------------
    def wait_for_events(self, count, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.sink._lock:
                if len(self.sink.events) >= count:
                    return
            time.sleep(0.1)
        with self.sink._lock:
            got = len(self.sink.events)
        raise AssertionError(
            "timed out waiting for %d sink events (got %d)"
            % (count, got))

    def read_cursor(self):
        return bridge_mod.read_cursor(self.cursor_path)


class TestBridgeDelivery(unittest.TestCase):
    def setUp(self):
        self.h = BridgeHarness()
        self.h.start_bridge()

    def tearDown(self):
        self.h.close()

    def test_live_event_delivered_with_documented_envelope(self):
        evt = bus.publish(_sig_event())
        self.h.wait_for_events(1)
        with self.h.sink._lock:
            env = self.h.sink.events[0]
        # documented envelope shape: {event_id, event, severity,
        # timestamp, payload}
        self.assertEqual(env["event_id"], evt["event_id"])
        self.assertEqual(env["event"], "signal.detected")
        self.assertEqual(env["severity"], "NOTICE")
        self.assertEqual(env["timestamp"], evt["ts"])
        self.assertEqual(env["payload"]["symbol"], "EURUSD")
        self.assertEqual(env["payload"]["direction"], "BUY")
        self.assertEqual(env["payload"]["event_id"], evt["event_id"])
        # durable cursor advanced exactly to the delivered event
        self.assertEqual(self.h.read_cursor(), evt["event_id"])
        self.assertEqual(self.h.bridge.cursor, evt["event_id"])

    def test_failed_delivery_does_not_advance_cursor(self):
        self.h.sink.fail_with = RuntimeError("host sink exploded")
        evt = bus.publish(_sig_event())
        time.sleep(4)  # bridge retries; cursor must not move
        with self.h.sink._lock:
            self.assertEqual(self.h.sink.events, [])
        self.assertIsNone(self.h.read_cursor())
        # fix the sink: the bridge reconnects with backoff and the
        # event replays from the unchanged cursor — no restart needed.
        self.h.sink.fail_with = None
        self.h.wait_for_events(1)
        with self.h.sink._lock:
            env = self.h.sink.events[0]
        self.assertEqual(env["event_id"], evt["event_id"])
        self.assertEqual(self.h.read_cursor(), evt["event_id"])

    def test_restart_resumes_missed_events_in_order(self):
        live = bus.publish(_sig_event(direction="BUY"))
        self.h.wait_for_events(1)
        self.h.stop_bridge()
        missed = [bus.publish(_sig_event(direction=d))["event_id"]
                  for d in ("SELL", "BUY")]
        self.h.start_bridge()
        self.h.wait_for_events(3)
        with self.h.sink._lock:
            ids = [e["event_id"] for e in self.h.sink.events]
        self.assertEqual(ids, [live["event_id"]] + missed)
        self.assertEqual(self.h.read_cursor(), missed[-1])

    def test_duplicate_frames_are_deduplicated(self):
        evt = bus.publish(_sig_event())
        self.h.wait_for_events(1)
        # re-injecting the same frame id must not redeliver
        self.h.bridge._on_event(
            evt["event_id"],
            json.dumps({"event_id": evt["event_id"],
                        "event": "signal.detected"}))
        time.sleep(1)
        with self.h.sink._lock:
            self.assertEqual(len(self.h.sink.events), 1)

    def test_reconnect_after_api_restart(self):
        evt = bus.publish(_sig_event(direction="BUY"))
        self.h.wait_for_events(1)
        # kill the API server out from under the bridge, then bring it
        # back on the SAME port; the bridge must reconnect with backoff
        # and keep delivering.
        port = self.h.port
        self.h.stop_server()
        time.sleep(2)
        self.h.start_server(port=port)
        self.assertEqual(self.h.port, port)
        later = bus.publish(_sig_event(direction="SELL"))
        self.h.wait_for_events(2)
        with self.h.sink._lock:
            ids = [e["event_id"] for e in self.h.sink.events]
        self.assertEqual(ids, [evt["event_id"], later["event_id"]])
        self.assertEqual(self.h.read_cursor(), later["event_id"])

    def test_stale_cursor_restarts_live_without_crashing(self):
        evt = bus.publish(_sig_event())
        self.h.wait_for_events(1)
        self.h.stop_bridge()
        # journal recreated since the cursor was written: the id is
        # unknown to the journal -> bridge drops the stale cursor and
        # restarts live instead of failing forever.
        with self.h.store._lock:
            self.h.store._conn.execute("DELETE FROM event_journal")
            self.h.store._conn.commit()
        self.h.start_bridge()
        time.sleep(4)
        self.assertTrue(self.h.bridge._thread.is_alive())
        later = bus.publish(_sig_event(direction="SELL"))
        self.h.wait_for_events(2)
        with self.h.sink._lock:
            ids = [e["event_id"] for e in self.h.sink.events]
        self.assertEqual(ids, [evt["event_id"], later["event_id"]])


class TestSubprocessSink(unittest.TestCase):
    def test_shell_metacharacters_are_not_interpreted(self):
        # ';' must NOT reach a shell: shlex tokenizes, so "echo"
        # receives the metacharacters as plain arguments and no file
        # is created.
        sink = SubprocessSink.from_command(
            "echo hello; touch /tmp/FOREX_BRIDGE_PWNED_XYZ")
        try:
            sink.deliver({"event_id": "evt_x", "event": "test.event",
                          "severity": "INFO", "timestamp": None,
                          "payload": {}})
        finally:
            sink.close()
        self.assertFalse(os.path.exists("/tmp/FOREX_BRIDGE_PWNED_XYZ"))

    def test_missing_executable_rejected(self):
        with self.assertRaises(ValueError):
            SubprocessSink.from_command("/nonexistent/forex-sink-xyz")
        with self.assertRaises(ValueError):
            SubprocessSink.from_command("definitely-not-a-real-binary-xyz")
        with self.assertRaises(ValueError):
            SubprocessSink.from_command("   ")

    def test_ndjson_delivery_to_real_child(self):
        tmp = tempfile.mkdtemp(prefix="forex_sink_")
        out_path = os.path.join(tmp, "collected.ndjson")
        collector = os.path.join(tmp, "collector.py")
        with open(collector, "w") as fh:
            fh.write("import sys\n"
                     "out = open(%r, 'a', buffering=1)\n"
                     "for line in sys.stdin:\n"
                     "    out.write(line)\n" % out_path)
        sink = SubprocessSink.from_command(
            "%s %s" % (sys.executable, collector))
        try:
            sink.deliver({"event_id": "evt_abc", "event": "signal.detected",
                          "severity": "NOTICE",
                          "timestamp": "2026-09-21T00:00:00+00:00",
                          "payload": {"symbol": "EURUSD"}})
            deadline = time.time() + 10
            lines = []
            while time.time() < deadline and not lines:
                time.sleep(0.2)
                if os.path.exists(out_path):
                    with open(out_path) as fh:
                        lines = fh.read().strip().splitlines()
            self.assertEqual(len(lines), 1)
            env = json.loads(lines[0])
            self.assertEqual(env["event_id"], "evt_abc")
            self.assertEqual(env["severity"], "NOTICE")
            self.assertEqual(env["payload"]["symbol"], "EURUSD")
        finally:
            sink.close()
            # child reaped: no leaked subprocess
            self.assertFalse(
                sink._child is not None and sink._child.poll() is None)

    def test_child_crash_is_surfaced_not_hidden(self):
        sink = SubprocessSink.from_command(
            "%s -c 'import sys; sys.exit(3)'" % sys.executable)
        # The child may accept one write into the pipe buffer before it
        # exits, and the crash counter only observes deaths between
        # deliveries, so pause briefly between attempts: repeated fast
        # crashes must surface as SinkError (never silently swallowed,
        # never an infinite respawn loop).
        try:
            with self.assertRaises(bridge_mod.SinkError):
                for _ in range(12):
                    sink.deliver({"event_id": "evt_x", "event": "test.event",
                                  "severity": "INFO", "timestamp": None,
                                  "payload": {}})
                    time.sleep(0.3)
        finally:
            sink.close()


if __name__ == "__main__":
    unittest.main()
