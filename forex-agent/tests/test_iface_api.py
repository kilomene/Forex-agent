"""Interface tests: localhost API (scripts/local_api).

Starts the real HTTP server on an ephemeral loopback port in a thread and
exercises every documented route. Asserts the bind address is 127.0.0.1.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

_TMP = tempfile.mkdtemp(prefix="forex_iface_api_")
os.environ["FOREX_AGENT_HOME"] = _TMP
os.environ["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "api.db")
os.environ.setdefault("BROKER_PROVIDER", "disconnected")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import local_api  # noqa: E402


def _http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class TestLocalApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = local_api.run(0)  # ephemeral port
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def test_binds_loopback_only(self):
        host, port = self.server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")

    def test_get_health(self):
        status, payload = _http("GET", self.url("/health"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("broker", payload["components"])

    def test_get_status(self):
        status, payload = _http("GET", self.url("/status"))
        self.assertEqual(status, 200)
        self.assertIn("status", payload)
        self.assertIn("components", payload)

    def test_get_signals(self):
        status, payload = _http("GET", self.url("/signals?limit=2"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIsInstance(payload["signals"], list)

    def test_get_positions_broker_down(self):
        status, payload = _http("GET", self.url("/positions"))
        self.assertEqual(status, 200)  # tool-level error, HTTP 200 envelope
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")

    def test_get_account_broker_down(self):
        status, payload = _http("GET", self.url("/account"))
        self.assertFalse(payload["ok"])

    def test_get_performance_broker_down(self):
        status, payload = _http("GET", self.url("/performance"))
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")

    def test_get_events(self):
        status, payload = _http("GET", self.url("/events?limit=5"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("events", payload)

    def test_unknown_route_404(self):
        status, payload = _http("GET", self.url("/teleport"))
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "NOT_FOUND")

    def test_post_analyze_broker_down(self):
        status, payload = _http("POST", self.url("/analyze"),
                                {"symbol": "EURUSD", "timeframe": "M15"})
        self.assertEqual(status, 422)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")

    def test_post_trade_request_dry_run(self):
        status, payload = _http("POST", self.url("/trade/request"), {
            "symbol": "EURUSD", "direction": "BUY", "volume": 0.1,
            "stop_loss": 1.0980})
        self.assertEqual(status, 422)
        self.assertFalse(payload["ok"])
        self.assertIn("error_code", payload)

    def test_post_position_close_broker_down(self):
        # Disconnected broker: the gateway reports BROKER_UNAVAILABLE
        # (structured), never a silent failure or a fake fill.
        status, payload = _http("POST", self.url("/position/close"),
                                {"ticket": "123"})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")

    def test_post_position_modify_broker_down(self):
        status, payload = _http("POST", self.url("/position/modify"),
                                {"ticket": "123", "stop_loss": 1.09})
        self.assertEqual(status, 422)
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")

    def test_post_invalid_json_400(self):
        req = urllib.request.Request(self.url("/analyze"), data=b"{oops",
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_post_unknown_route_404(self):
        status, payload = _http("POST", self.url("/trade/teleport"), {})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
