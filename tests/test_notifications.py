"""Phase-6 tests: notification channels + dispatcher.

Covers: channel abstraction (is_configured False when unconfigured);
dispatcher routes by severity; one failing channel never blocks others;
AgentChannel is always available and idempotent; FCM unconfigured is
honestly skipped; webhook timeout is isolated; the dispatcher consumes
the delivery journal via the bus's public read API without duplicates
(event_id dedup); retries are bounded and notification-only.
"""

import os
import socket
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.events import bus
from agent.notifications import channels as ch
from agent.notifications import dispatcher as disp_mod
from agent.notifications.dispatcher import NotificationDispatcher
from storage import Store


def _sig_event(**kw):
    base = {"event": "signal.detected", "symbol": "EURUSD",
            "timeframe": "M15", "direction": "BUY"}
    base.update(kw)
    return base


class RecordingChannel(ch.NotificationChannel):
    """Test double: records send() calls; can be told to fail."""

    def __init__(self, name, fail_times=0):
        super().__init__(enabled=True)
        self.name = name
        self.fail_times = fail_times
        self.calls = []

    def is_configured(self):
        return True

    def send(self, event):
        self.calls.append(event)
        if len(self.calls) <= self.fail_times:
            raise ch.NotificationError("boom")


class ChannelAbstractionTestCase(unittest.TestCase):
    def test_agent_channel_always_available(self):
        agent = ch.AgentChannel()
        self.assertTrue(agent.is_configured())
        self.assertEqual(agent.name, "agent")

    def test_webhook_unconfigured_without_url(self):
        self.assertFalse(ch.WebhookChannel(enabled=True, url="").is_configured())
        self.assertFalse(ch.WebhookChannel(enabled=False,
                                           url="http://x").is_configured())
        self.assertTrue(ch.WebhookChannel(enabled=True,
                                          url="http://x/hook").is_configured())

    def test_worker_unconfigured_without_base_url(self):
        self.assertFalse(ch.WorkerChannel(enabled=True, base_url="").is_configured())
        self.assertTrue(ch.WorkerChannel(enabled=True,
                                         base_url="http://w").is_configured())
        self.assertFalse(ch.WorkerChannel(enabled=False,
                                          base_url="http://w").is_configured())

    def test_fcm_unconfigured_by_default(self):
        # No push path exists in the Worker contract yet -> honestly False.
        fcm = ch.FCMChannel()
        self.assertFalse(fcm.is_configured())
        self.assertFalse(ch.FCMChannel(enabled=True, project_id="p",
                                       worker_base_url="http://w",
                                       push_path="").is_configured())
        self.assertTrue(ch.FCMChannel(enabled=True, project_id="p",
                                      worker_base_url="http://w",
                                      push_path="/notify").is_configured())

    def test_channel_factory_unknown_name(self):
        with self.assertRaises(ch.NotificationError):
            ch.channel("pager")

    def test_health_has_no_secrets(self):
        hook = ch.WebhookChannel(enabled=True,
                                 url="http://x/hook?token=SECRET123")
        health = hook.health()
        self.assertNotIn("SECRET123", str(health))
        self.assertIn("host", health)


class DispatcherRoutingTestCase(unittest.TestCase):
    def setUp(self):
        self.a = RecordingChannel("agent")
        self.w = RecordingChannel("worker")
        self.f = RecordingChannel("fcm")
        self.h = RecordingChannel("webhook")
        self.d = NotificationDispatcher(
            channels=[self.a, self.w, self.f, self.h],
            routing={"CRITICAL": ("agent", "worker", "fcm", "webhook"),
                     "INFO": ("agent",)},
            max_attempts=1, state_dir=tempfile.mkdtemp(prefix="notify_"))

    def test_routes_by_severity(self):
        self.d.dispatch_event({"event_id": "e1", "severity": "CRITICAL"})
        self.assertEqual(len(self.a.calls), 1)
        self.assertEqual(len(self.w.calls), 1)
        self.assertEqual(len(self.f.calls), 1)
        self.assertEqual(len(self.h.calls), 1)
        self.d.dispatch_event({"event_id": "e2", "severity": "INFO"})
        self.assertEqual(len(self.a.calls), 2)
        self.assertEqual(len(self.w.calls), 1)  # INFO -> agent only
        self.assertEqual(len(self.f.calls), 1)
        self.assertEqual(len(self.h.calls), 1)

    def test_one_channel_failing_does_not_block_others(self):
        bad = RecordingChannel("worker", fail_times=99)
        d = NotificationDispatcher(
            channels=[self.a, bad, self.h],
            routing={"WARNING": ("agent", "worker", "webhook")},
            max_attempts=1, state_dir=tempfile.mkdtemp(prefix="notify_"))
        results = d.dispatch_event({"event_id": "e9", "severity": "WARNING"})
        self.assertEqual(results["agent"], "sent")
        self.assertTrue(results["worker"].startswith("failed:"))
        self.assertEqual(results["webhook"], "sent")
        self.assertEqual(len(self.a.calls), 1)
        self.assertEqual(len(self.h.calls), 1)

    def test_unconfigured_channel_honestly_skipped(self):
        fcm = ch.FCMChannel()  # unconfigured by default
        d = NotificationDispatcher(
            channels=[self.a, fcm],
            routing={"CRITICAL": ("agent", "fcm")},
            max_attempts=1, state_dir=tempfile.mkdtemp(prefix="notify_"))
        results = d.dispatch_event({"event_id": "e3", "severity": "CRITICAL"})
        self.assertEqual(results["agent"], "sent")
        self.assertEqual(results["fcm"], "skipped: not configured")
        self.assertEqual(len(self.a.calls), 1)

    def test_retries_are_bounded_and_notification_only(self):
        flaky = RecordingChannel("agent", fail_times=1)
        d = NotificationDispatcher(channels=[flaky], max_attempts=3,
                                   backoff_s=0,
                                   state_dir=tempfile.mkdtemp(prefix="notify_"))
        results = d.dispatch_event({"event_id": "e4", "severity": "INFO"})
        self.assertEqual(results["agent"], "sent")
        self.assertEqual(len(flaky.calls), 2)  # 1 failure + 1 retry

        always_bad = RecordingChannel("agent", fail_times=99)
        d2 = NotificationDispatcher(channels=[always_bad], max_attempts=2,
                                    backoff_s=0,
                                    state_dir=tempfile.mkdtemp(prefix="notify_"))
        results = d2.dispatch_event({"event_id": "e5", "severity": "INFO"})
        self.assertTrue(results["agent"].startswith("failed:"))
        self.assertEqual(len(always_bad.calls), 2)  # exactly max_attempts


class WebhookIsolationTestCase(unittest.TestCase):
    def test_webhook_timeout_does_not_block_agent_channel(self):
        real_urlopen = urllib.request.urlopen

        def _hang(req, timeout=None):
            raise socket.timeout("timed out")

        urllib.request.urlopen = _hang
        try:
            hook = ch.WebhookChannel(enabled=True, url="http://127.0.0.1:9/x",
                                     timeout=0.5)
            agent = RecordingChannel("agent")
            d = NotificationDispatcher(
                channels=[agent, hook],
                routing={"WARNING": ("agent", "webhook")},
                max_attempts=1,
                state_dir=tempfile.mkdtemp(prefix="notify_"))
            results = d.dispatch_event(
                {"event_id": "e6", "severity": "WARNING"})
            self.assertEqual(results["agent"], "sent")
            self.assertTrue(results["webhook"].startswith("failed:"))
            self.assertEqual(len(agent.calls), 1)
        finally:
            urllib.request.urlopen = real_urlopen


class JournalConsumptionTestCase(unittest.TestCase):
    """Dispatcher reads the real delivery journal via the bus's public
    read API and never double-processes an event."""

    def setUp(self):
        bus.reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="forex_notify_")
        self.db = os.path.join(self.tmp, "events.db")
        self.store = Store(self.db)
        bus.configure(store=self.store)
        self.state_dir = tempfile.mkdtemp(prefix="notify_state_")

    def tearDown(self):
        bus.reset_for_tests()
        self.store.close()

    def _dispatcher(self, **kw):
        self.rec = RecordingChannel("agent")
        kw.setdefault("start_at_head", False)
        kw.setdefault("state_dir", self.state_dir)
        return NotificationDispatcher(channels=[self.rec], **kw)

    def test_consumes_journal_without_duplicates(self):
        e1 = bus.publish(_sig_event())
        e2 = bus.publish(_sig_event(direction="SELL"))
        e3 = bus.publish(_sig_event(symbol="GBPUSD"))
        d = self._dispatcher()
        processed = d.run_once()
        self.assertEqual(processed, 3)
        self.assertEqual([e["event_id"] for e in self.rec.calls],
                         [e1["event_id"], e2["event_id"], e3["event_id"]])
        # Second pass: nothing new, no duplicates.
        self.assertEqual(d.run_once(), 0)
        self.assertEqual(len(self.rec.calls), 3)
        # A new event is picked up exactly once.
        e4 = bus.publish(_sig_event(symbol="USDJPY"))
        self.assertEqual(d.run_once(), 1)
        self.assertEqual(self.rec.calls[-1]["event_id"], e4["event_id"])
        self.assertEqual(len(self.rec.calls), 4)

    def test_cursor_persists_across_restarts(self):
        bus.publish(_sig_event())
        bus.publish(_sig_event(direction="SELL"))
        d = self._dispatcher()
        self.assertEqual(d.run_once(), 2)
        cursor = d.cursor
        self.assertIsNotNone(cursor)
        # A fresh dispatcher in the same state dir resumes, not replays.
        d2 = self._dispatcher()
        self.assertEqual(d2.cursor, cursor)
        self.assertEqual(d2.run_once(), 0)
        self.assertEqual(len(self.rec.calls), 0)

    def test_agent_channel_send_verifies_journal_presence(self):
        event = bus.publish(_sig_event())
        agent = ch.AgentChannel()
        before = bus.latest_events(limit=100)
        agent.send(event)  # already journaled: no re-publish
        after = bus.latest_events(limit=100)
        self.assertEqual(len(before), len(after))

    def test_agent_channel_rejournal_is_idempotent(self):
        event = bus.publish(_sig_event())
        agent = ch.AgentChannel()
        # Simulate an event the journal somehow missed: re-journal must
        # not create a duplicate row (INSERT OR IGNORE on event_id).
        missing = dict(event)
        missing["event_id"] = "evt_" + "0" * 32
        before = bus.latest_events(limit=1000)
        agent.send(missing)
        agent.send(missing)
        after = bus.latest_events(limit=1000)
        self.assertEqual(len(after), len(before) + 1)


class ConfigWiringTestCase(unittest.TestCase):
    def test_env_webhook_url_enables_webhook_channel(self):
        os.environ["NOTIFICATIONS_ENABLED"] = "1"
        os.environ["NOTIFY_CHANNEL_WEBHOOK"] = "1"
        os.environ["NOTIFY_WEBHOOK_URL"] = "http://127.0.0.1:9/hook"
        try:
            from config.config import load_config  # noqa: PLC0415
            cfg = load_config()
            d = disp_mod.build_dispatcher_from_config(cfg)
            self.assertTrue(d.channels["webhook"].is_configured())
            self.assertTrue(d.channels["agent"].is_configured())
            # FCM stays honestly off without credentials/push path.
            self.assertFalse(d.channels["fcm"].is_configured())
            # Worker channel off: worker not configured in this env.
            self.assertFalse(d.channels["worker"].is_configured())
            self.assertEqual(cfg.notifications.max_attempts, 2)
        finally:
            del os.environ["NOTIFICATIONS_ENABLED"]
            del os.environ["NOTIFY_CHANNEL_WEBHOOK"]
            del os.environ["NOTIFY_WEBHOOK_URL"]

    def test_redacted_config_masks_webhook_url(self):
        os.environ["NOTIFY_WEBHOOK_URL"] = "http://x/hook?token=SECRET123"
        try:
            from config.config import load_config  # noqa: PLC0415
            redacted = load_config().redacted()
            self.assertNotIn("SECRET123", str(redacted))
            self.assertEqual(redacted["notifications"]["webhook_url"], "***")
        finally:
            del os.environ["NOTIFY_WEBHOOK_URL"]


class _FakeHTTPResponse:
    """Minimal urlopen stand-in: context manager with status + read()."""

    def __init__(self, status=200, body=b'{"ok": true}'):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TelegramChannelTestCase(unittest.TestCase):
    def setUp(self):
        self._real_urlopen = urllib.request.urlopen

    def tearDown(self):
        urllib.request.urlopen = self._real_urlopen

    def _ch(self, **kw):
        kw.setdefault("enabled", True)
        kw.setdefault("bot_token", "TESTTOKEN123")
        kw.setdefault("chat_id", "987654321")
        return ch.TelegramChannel(**kw)

    def _install_fake(self, response=None, exc=None):
        captured = {}

        def _fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["timeout"] = timeout
            if exc is not None:
                raise exc
            return response or _FakeHTTPResponse()

        urllib.request.urlopen = _fake
        return captured

    def test_telegram_unconfigured_by_default(self):
        self.assertFalse(ch.TelegramChannel().is_configured())

    def test_telegram_needs_token_and_chat(self):
        self.assertFalse(
            self._ch(bot_token="", chat_id="1").is_configured())
        self.assertFalse(
            self._ch(bot_token="t", chat_id="").is_configured())
        self.assertFalse(
            self._ch(enabled=False).is_configured())
        self.assertTrue(self._ch().is_configured())

    def test_telegram_send_posts_to_bot_api(self):
        captured = self._install_fake()
        chan = self._ch()
        chan.send({"event_id": "e1", "severity": "NOTICE",
                   "event": "signal.detected", "symbol": "EURUSD",
                   "timeframe": "M15", "direction": "BUY",
                   "entry_price": 1.0852, "stop_loss": 1.083,
                   "take_profit": 1.0895})
        self.assertEqual(
            captured["url"],
            "https://api.telegram.org/botTESTTOKEN123/sendMessage")
        import json as _json
        payload = _json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(payload["chat_id"], "987654321")
        text = payload["text"]
        self.assertIn("SIGNAL DETECTED", text)
        self.assertIn("EURUSD", text)
        self.assertIn("BUY", text)
        self.assertIn("1.0852", text)

    def test_telegram_generic_event_is_one_liner(self):
        captured = self._install_fake()
        self._ch().send({"event_id": "e2", "severity": "CRITICAL",
                         "event": "risk.kill_switch_engaged"})
        import json as _json
        payload = _json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(payload["text"],
                         "[CRITICAL] risk.kill_switch_engaged")

    def test_telegram_http_error_raises(self):
        import urllib.error as _uerr
        captured = self._install_fake(
            exc=_uerr.HTTPError("u", 401, "Unauthorized", {}, None))
        with self.assertRaises(ch.NotificationError):
            self._ch().send({"event_id": "e3", "severity": "NOTICE"})

    def test_telegram_network_error_raises(self):
        import urllib.error as _uerr
        self._install_fake(exc=_uerr.URLError("conn refused"))
        with self.assertRaises(ch.NotificationError):
            self._ch().send({"event_id": "e4", "severity": "NOTICE"})

    def test_telegram_ok_false_raises(self):
        self._install_fake(
            _FakeHTTPResponse(200, b'{"ok": false, "error_code": 400}'))
        with self.assertRaises(ch.NotificationError):
            self._ch().send({"event_id": "e5", "severity": "NOTICE"})

    def test_telegram_health_has_no_secrets(self):
        health = self._ch().health()
        self.assertNotIn("TESTTOKEN123", str(health))
        self.assertTrue(health["token_set"])
        self.assertTrue(health["chat_id_set"])
        self.assertTrue(health["configured"])

    def test_telegram_channel_factory(self):
        chan = ch.channel("telegram", bot_token="t", chat_id="1")
        self.assertIsInstance(chan, ch.TelegramChannel)

    def test_telegram_failure_does_not_block_agent_channel(self):
        import urllib.error as _uerr
        self._install_fake(exc=_uerr.URLError("down"))
        agent = RecordingChannel("agent")
        tg = self._ch()
        d = NotificationDispatcher(
            channels=[agent, tg],
            routing={"NOTICE": ("agent", "telegram")},
            max_attempts=1, state_dir=tempfile.mkdtemp(prefix="notify_"))
        results = d.dispatch_event({"event_id": "e6", "severity": "NOTICE",
                                    "event": "signal.detected"})
        self.assertEqual(results["agent"], "sent")
        self.assertTrue(results["telegram"].startswith("failed:"))
        self.assertEqual(len(agent.calls), 1)

    def test_env_telegram_token_enables_channel_and_is_redacted(self):
        os.environ["NOTIFICATIONS_ENABLED"] = "1"
        os.environ["NOTIFY_CHANNEL_TELEGRAM"] = "1"
        os.environ["TELEGRAM_BOT_TOKEN"] = "SECRET_BOT_TOKEN"
        os.environ["TELEGRAM_CHAT_ID"] = "12345"
        try:
            from config.config import load_config  # noqa: PLC0415
            cfg = load_config()
            d = disp_mod.build_dispatcher_from_config(cfg)
            self.assertTrue(d.channels["telegram"].is_configured())
            self.assertTrue(cfg.notifications.channel_telegram)
            redacted = cfg.redacted()
            self.assertNotIn("SECRET_BOT_TOKEN", str(redacted))
            self.assertEqual(
                redacted["notifications"]["telegram_bot_token"], "***")
            # NOTICE routing includes telegram by default.
            self.assertIn("telegram", d.routing.get("NOTICE", ()))
        finally:
            del os.environ["NOTIFICATIONS_ENABLED"]
            del os.environ["NOTIFY_CHANNEL_TELEGRAM"]
            del os.environ["TELEGRAM_BOT_TOKEN"]
            del os.environ["TELEGRAM_CHAT_ID"]


if __name__ == "__main__":
    unittest.main()
