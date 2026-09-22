"""Phase-4 tests: push event delivery (SSE stream + delivery journal).

Covers: event -> journaled with unique event_id; SSE stream delivers to a
subscriber; resume_from replays missed events in order; ack works;
duplicate delivery identifiable by event_id (dedup); severity mapping;
poll fallback GET /events/latest; legacy GET /events JSON route intact;
CLI events --since-id / --follow.

The server tests start the real scripts/local_api on an ephemeral
loopback port, exactly like tests/test_iface_events.py starts its fake
webhook server. SSE timing constants are shrunk for the suite and
restored afterwards.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.events import bus
from scripts import local_api
from storage import Store


def _sig_event(**kw):
    base = {"event": "signal.detected", "symbol": "EURUSD",
            "timeframe": "M15", "direction": "BUY"}
    base.update(kw)
    return base


class JournalTestCase(unittest.TestCase):
    """Bus + real Store on a throwaway SQLite file."""

    def setUp(self):
        bus.reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="forex_events_sse_")
        self.db = os.path.join(self.tmp, "events.db")
        self.store = Store(self.db)
        bus.configure(store=self.store)

    def tearDown(self):
        bus.reset_for_tests()
        self.store.close()


class TestEventJournal(JournalTestCase):
    def test_publish_journals_with_unique_event_id(self):
        e1 = bus.publish(_sig_event())
        e2 = bus.publish(_sig_event(direction="SELL"))
        self.assertTrue(e1["event_id"].startswith("evt_"))
        self.assertTrue(e2["event_id"].startswith("evt_"))
        self.assertNotEqual(e1["event_id"], e2["event_id"])
        rows = self.store.event_journal_latest(limit=10)
        self.assertEqual([r["event_id"] for r in rows],
                         [e1["event_id"], e2["event_id"]])
        self.assertEqual(rows[0]["severity"], "NOTICE")

    def test_republish_same_event_id_dedups_in_journal(self):
        e1 = bus.publish(_sig_event())
        bus.publish(dict(e1))  # retry carrying the same event_id
        rows = self.store.event_journal_latest(limit=10)
        self.assertEqual(len(rows), 1)  # journal dedups on event_id
        self.assertEqual(rows[0]["event_id"], e1["event_id"])
        # ...but the durable FIFO queue still recorded both attempts.
        self.assertEqual(len(self.store.dequeue_events(limit=10)), 2)

    def test_severity_mapping(self):
        cases = [
            (_sig_event(), "NOTICE"),
            ({"event": "trade.executed", "symbol": "EURUSD", "ticket": "1",
              "side": "BUY", "volume": 0.1}, "NOTICE"),
            ({"event": "trade.requested", "symbol": "EURUSD", "side": "BUY",
              "volume": 0.1, "idempotency_key": "k"}, "INFO"),
            ({"event": "risk.blocked", "reason": "MAX_POSITIONS"}, "WARNING"),
            ({"event": "broker.disconnected", "adapter": "mt5"}, "CRITICAL"),
            ({"event": "broker.connected", "adapter": "mt5"}, "INFO"),
            ({"event": "kill_switch.activated", "source": "agent"}, "CRITICAL"),
            ({"event": "health.check", "ok": True}, "INFO"),
        ]
        for payload, expected in cases:
            with self.subTest(event=payload["event"]):
                evt = bus.publish(payload)
                self.assertEqual(evt["severity"], expected)

    def test_daily_loss_limit_escalates_to_critical(self):
        evt = bus.publish({"event": "risk.blocked",
                           "reason": "DAILY_LOSS_LIMIT"})
        self.assertEqual(evt["severity"], "CRITICAL")
        evt2 = bus.publish({"event": "risk.blocked",
                            "reason": "daily_loss_limit hit"})
        self.assertEqual(evt2["severity"], "CRITICAL")

    def test_explicit_severity_overrides_mapping(self):
        evt = bus.publish(_sig_event(), severity="CRITICAL")
        self.assertEqual(evt["severity"], "CRITICAL")

    def test_invalid_severity_rejected(self):
        with self.assertRaises(ValueError):
            bus.publish(_sig_event(), severity="URGENT")
        with self.assertRaises(ValueError):
            bus.publish(dict(_sig_event(), severity="URGENT"))
        # ...and nothing was journaled.
        self.assertEqual(self.store.event_journal_latest(limit=10), [])

    def test_publish_still_backward_compatible(self):
        # Old-style call: no severity, no event_id — both auto-assigned.
        evt = bus.publish({"event": "trade.executed", "symbol": "EURUSD",
                           "ticket": "9", "side": "SELL", "volume": 0.2})
        self.assertTrue(evt["event_id"].startswith("evt_"))
        self.assertEqual(evt["severity"], "NOTICE")

    def test_replay_after_returns_missed_in_order(self):
        a = bus.publish(_sig_event(direction="BUY"))
        b = bus.publish(_sig_event(direction="SELL"))
        c = bus.publish({"event": "trade.executed", "symbol": "EURUSD",
                         "ticket": "2", "side": "BUY", "volume": 0.1})
        replayed = bus.replay_after(a["event_id"])
        self.assertEqual([e["event_id"] for e in replayed],
                         [b["event_id"], c["event_id"]])
        self.assertEqual(bus.replay_after(c["event_id"]), [])

    def test_replay_after_unknown_id_raises(self):
        bus.publish(_sig_event())
        with self.assertRaises(ValueError):
            bus.replay_after("evt_does_not_exist")

    def test_ack_is_cumulative(self):
        a = bus.publish(_sig_event(direction="BUY"))
        b = bus.publish(_sig_event(direction="SELL"))
        c = bus.publish({"event": "trade.executed", "symbol": "EURUSD",
                         "ticket": "3", "side": "BUY", "volume": 0.1})
        self.assertFalse(bus.acknowledge("evt_does_not_exist"))
        self.assertTrue(bus.acknowledge(b["event_id"]))
        got = {e["event_id"]: e["_acked"]
               for e in self.store.event_journal_latest(limit=10)}
        self.assertTrue(got[a["event_id"]])   # at/before b -> acked
        self.assertTrue(got[b["event_id"]])
        self.assertFalse(got[c["event_id"]])  # after b -> not acked

    def test_latest_events_chronological(self):
        ids = [bus.publish(_sig_event(direction=d))["event_id"]
               for d in ("BUY", "SELL", "BUY")]
        latest = bus.latest_events(limit=2)
        self.assertEqual([e["event_id"] for e in latest], ids[1:])
        # empty journal -> None cursor
        bus.reset_for_tests()
        fresh = Store(os.path.join(self.tmp, "empty.db"))
        bus.configure(store=fresh)
        try:
            self.assertIsNone(bus.last_event_id())
            self.assertEqual(bus.latest_events(), [])
        finally:
            fresh.close()
            bus.configure(store=self.store)


# ---------------------------------------------------------------------------
# HTTP server tests
# ---------------------------------------------------------------------------

class SSEClient:
    """Minimal SSE reader over http.client (urllib buffers; we stream)."""

    def __init__(self, port, path):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        self.conn.putrequest("GET", path)
        self.conn.putheader("Accept", "text/event-stream")
        self.conn.endheaders()
        self.resp = self.conn.getresponse()
        if self.resp.status != 200:
            body = self.resp.read().decode()
            raise AssertionError("SSE connect got %d: %s"
                                 % (self.resp.status, body))
        ctype = self.resp.getheader("Content-Type")
        assert ctype == "text/event-stream", ctype

    def read_frame(self, timeout=12):
        """Next non-comment SSE frame as {'id','event','data'}."""
        deadline = time.time() + timeout
        fid, fetype, fdata = None, None, None
        while time.time() < deadline:
            line = self.resp.fp.readline(65536)
            if not line:
                raise AssertionError("SSE stream closed mid-frame")
            text = line.decode("utf-8").rstrip("\n").rstrip("\r")
            if text == "":
                if fdata is not None:
                    return {"id": fid, "event": fetype,
                            "data": json.loads(fdata)}
                fid, fetype, fdata = None, None, None
                continue
            if text.startswith(":"):
                continue  # keep-alive / hello comment
            if text.startswith("id:"):
                fid = text[3:].strip()
            elif text.startswith("event:"):
                fetype = text[6:].strip()
            elif text.startswith("data:"):
                piece = text[5:].strip()
                fdata = piece if fdata is None else fdata + "\n" + piece
        raise socket.timeout("timed out waiting for SSE frame")

    def close(self):
        try:
            self.resp.close()
        finally:
            self.conn.close()


def _http_json(port, path, accept=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
    if accept:
        req.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class TestSSEStream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bus.reset_for_tests()
        cls.tmp = tempfile.mkdtemp(prefix="forex_sse_srv_")
        cls.db = os.path.join(cls.tmp, "sse.db")
        cls.store = Store(cls.db)
        bus.configure(store=cls.store)
        # shrink stream timing for the suite
        cls._poll, cls._keep = (local_api._SSE_POLL_INTERVAL_S,
                                local_api._SSE_KEEPALIVE_S)
        local_api._SSE_POLL_INTERVAL_S = 0.2
        local_api._SSE_KEEPALIVE_S = 1.0
        cls.server = local_api.run(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)
        local_api._SSE_POLL_INTERVAL_S = cls._poll
        local_api._SSE_KEEPALIVE_S = cls._keep
        bus.reset_for_tests()
        cls.store.close()

    def setUp(self):
        # fresh journal per test: wipe rows (same Store/connection)
        with self.store._lock:
            self.store._conn.execute("DELETE FROM event_journal")
            self.store._conn.commit()

    def test_sse_delivers_published_event(self):
        client = SSEClient(self.port, "/events")
        try:
            evt = bus.publish(_sig_event())
            frame = client.read_frame()
        finally:
            client.close()
        self.assertEqual(frame["id"], evt["event_id"])
        self.assertEqual(frame["event"], "signal.detected")
        data = frame["data"]
        self.assertEqual(data["event_id"], evt["event_id"])
        self.assertEqual(data["event"], "signal.detected")
        self.assertEqual(data["severity"], "NOTICE")
        self.assertEqual(data["timestamp"], evt["ts"])
        self.assertEqual(data["payload"]["symbol"], "EURUSD")
        self.assertEqual(data["payload"]["event_id"], evt["event_id"])

    def test_resume_from_replays_missed_in_order(self):
        a = bus.publish(_sig_event(direction="BUY"))
        b = bus.publish(_sig_event(direction="SELL"))
        c = bus.publish({"event": "trade.executed", "symbol": "EURUSD",
                         "ticket": "7", "side": "BUY", "volume": 0.1})
        client = SSEClient(self.port,
                           "/events?resume_from=%s" % urllib.parse.quote(a["event_id"]))
        try:
            f1 = client.read_frame()
            f2 = client.read_frame()
            # then a live event published after connect streams too
            live = bus.publish({"event": "broker.disconnected",
                                "adapter": "mt5"})
            f3 = client.read_frame()
        finally:
            client.close()
        self.assertEqual([f1["id"], f2["id"], f3["id"]],
                         [b["event_id"], c["event_id"], live["event_id"]])
        self.assertEqual(f3["event"], "broker.disconnected")
        self.assertEqual(f3["data"]["severity"], "CRITICAL")

    def test_resume_from_unknown_id_is_400(self):
        status, payload = _http_json(
            self.port, "/events?resume_from=evt_nope",
            accept="text/event-stream")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "INVALID_ARGUMENT")

    def test_ack_param(self):
        a = bus.publish(_sig_event(direction="BUY"))
        b = bus.publish(_sig_event(direction="SELL"))
        # unknown ack -> 400
        status, _ = _http_json(self.port, "/events?ack=evt_nope",
                               accept="text/event-stream")
        self.assertEqual(status, 400)
        # valid ack -> stream connects and the journal is marked
        client = SSEClient(self.port, "/events?ack=%s" % b["event_id"])
        client.close()
        got = {e["event_id"]: e["_acked"]
               for e in self.store.event_journal_latest(limit=10)}
        self.assertTrue(got[a["event_id"]])
        self.assertTrue(got[b["event_id"]])

    def test_events_latest_poll_fallback(self):
        a = bus.publish(_sig_event(direction="BUY"))
        b = bus.publish(_sig_event(direction="SELL"))
        status, payload = _http_json(
            self.port, "/events/latest?since=%s" % a["event_id"])
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["event_id"], b["event_id"])
        self.assertEqual(payload["last_event_id"], b["event_id"])
        # no since -> newest N, chronological
        status, payload = _http_json(self.port, "/events/latest?limit=10")
        self.assertEqual([e["event_id"] for e in payload["events"]],
                         [a["event_id"], b["event_id"]])
        # unknown since -> 400
        status, payload = _http_json(self.port, "/events/latest?since=evt_nope")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "INVALID_ARGUMENT")

    def test_events_json_route_unchanged(self):
        bus.publish(_sig_event())
        status, payload = _http_json(self.port, "/events?limit=5")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("count", payload)
        self.assertIn("events", payload)
        self.assertNotIn("last_event_id", payload)  # legacy shape untouched

    def test_binds_loopback_only(self):
        host, _ = self.server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")


class TestEventsCLI(unittest.TestCase):
    FOREX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "scripts", "forex")

    def setUp(self):
        bus.reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="forex_cli_events_")
        self.db = os.path.join(self.tmp, "cli.db")
        self.store = Store(self.db)
        bus.configure(store=self.store)
        self.env = dict(os.environ, FOREX_AGENT_STORAGE=self.db)

    def tearDown(self):
        bus.reset_for_tests()
        self.store.close()

    def _run(self, *args, timeout=20):
        return subprocess.run(
            [sys.executable, self.FOREX, "events", *args, "--json"],
            cwd=os.path.dirname(self.FOREX),
            env=self.env, capture_output=True, text=True, timeout=timeout)

    def test_cli_events_since_id(self):
        a = bus.publish(_sig_event(direction="BUY"))
        b = bus.publish(_sig_event(direction="SELL"))
        proc = self._run("--since-id", a["event_id"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["events"][0]["event_id"], b["event_id"])

    def test_cli_events_since_id_unknown(self):
        proc = self._run("--since-id", "evt_nope")
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "INVALID_ARGUMENT")

    def test_cli_events_follow_streams_new_events(self):
        proc = subprocess.Popen(
            [sys.executable, self.FOREX, "events", "--follow", "--json"],
            cwd=os.path.dirname(self.FOREX),
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        try:
            time.sleep(1.5)  # let the follower start tailing
            evt = bus.publish(_sig_event())  # same db file, other connection
            line = proc.stdout.readline()
            self.assertTrue(line, "follower produced no output")
            got = json.loads(line)
            self.assertEqual(got["event_id"], evt["event_id"])
            self.assertEqual(got["severity"], "NOTICE")
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
