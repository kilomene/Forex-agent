"""Interface tests: MCP server (agent/mcp — hand-rolled JSON-RPC 2.0/std io).

Covers: initialize handshake, tools/list (all 18 capabilities with
JSON Schema), tools/call success + tool-level ok:false (isError),
unknown tool / method / malformed input errors, notification silence,
and a real subprocess smoke test proving the stdio framing works
end to end (stdout stays pure protocol, logs go to stderr).
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="forex_iface_mcp_")
os.environ["FOREX_AGENT_HOME"] = _TMP
os.environ["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "test.db")
os.environ.setdefault("BROKER_PROVIDER", "disconnected")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.mcp import server  # noqa: E402
from agent.tools import backend  # noqa: E402

try:
    from tests.test_iface_fakes import FakeBroker, FakeGateway  # noqa: E402
except ImportError:
    from fakes import FakeBroker, FakeGateway  # noqa: E402


class BaseMcpTest(unittest.TestCase):
    def setUp(self):
        backend.reset_overrides()
        backend.set_override("broker", FakeBroker())
        backend.set_override("gateway", FakeGateway())


def req(method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class TestHandshake(BaseMcpTest):
    def test_initialize(self):
        resp = server.handle_request(req("initialize", {}, rid=1))
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "forex-agent")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_ping(self):
        self.assertEqual(server.handle_request(req("ping", {}, rid=2))["result"], {})

    def test_unknown_method(self):
        resp = server.handle_request(req("nope/method", {}, rid=3))
        self.assertEqual(resp["error"]["code"], -32601)

    def test_bad_jsonrpc_version(self):
        resp = server.handle_request({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        self.assertEqual(resp["error"]["code"], -32600)

    def test_notification_gets_no_response(self):
        msg = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.assertIsNone(server.handle_request(msg))

    def test_non_dict_ignored(self):
        self.assertIsNone(server.handle_request(["not", "a", "dict"]))


class TestToolsList(BaseMcpTest):
    def test_lists_all_capabilities(self):
        resp = server.handle_request(req("tools/list", {}, rid=1))
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(len(tools), 18)
        self.assertIn("forex.request_trade", names)
        self.assertIn("forex.get_health", names)
        for t in tools:
            self.assertIn("description", t)
            self.assertIn("inputSchema", t)
            self.assertEqual(t["inputSchema"]["type"], "object")


class TestToolsCall(BaseMcpTest):
    def test_call_health(self):
        resp = server.handle_request(req("tools/call", {
            "name": "forex.get_health", "arguments": {}}, rid=1))
        self.assertFalse(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])

    def test_tool_level_failure_is_not_protocol_error(self):
        # forex.request_trade in dry-run: tool says ok:false, protocol says ok.
        resp = server.handle_request(req("tools/call", {
            "name": "forex.request_trade",
            "arguments": {"symbol": "EURUSD", "direction": "BUY",
                          "volume": 0.1, "stop_loss": 1.0980}}, rid=2))
        self.assertIn("result", resp)
        self.assertTrue(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "DRY_RUN_BLOCKED")

    def test_unknown_tool(self):
        resp = server.handle_request(req("tools/call", {
            "name": "forex.teleport", "arguments": {}}, rid=3))
        self.assertEqual(resp["error"]["code"], -32601)

    def test_missing_name(self):
        resp = server.handle_request(req("tools/call", {"arguments": {}}, rid=4))
        self.assertEqual(resp["error"]["code"], -32602)

    def test_bad_arguments_shape(self):
        resp = server.handle_request(req("tools/call", {
            "name": "forex.get_health", "arguments": [1, 2]}, rid=5))
        self.assertEqual(resp["error"]["code"], -32602)


class TestStdioFraming(BaseMcpTest):
    def test_serve_roundtrip(self):
        script = "\n".join([
            json.dumps(req("initialize", {}, rid=1)),
            json.dumps(req("tools/list", {}, rid=2)),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(req("tools/call", {"name": "forex.get_session",
                                          "arguments": {}}, rid=3)),
            "not json at all",
        ]) + "\n"
        out = io.StringIO()
        server.serve(stdin=io.StringIO(script), stdout=out)
        lines = [json.loads(line) for line in out.getvalue().strip().split("\n")]
        # 4 responses: initialize, tools/list, tools/call, parse error.
        # The notification is silent.
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(len(lines[1]["result"]["tools"]), 18)
        self.assertEqual(lines[2]["id"], 3)
        self.assertEqual(lines[3]["error"]["code"], -32700)

    def test_subprocess_smoke(self):
        """End-to-end: real process, real stdio. stdout must be pure JSON-RPC."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["FOREX_AGENT_HOME"] = _TMP
        env["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "sub.db")
        env["BROKER_PROVIDER"] = "disconnected"
        proc = subprocess.run(
            [sys.executable, "-m", "agent.mcp.server"],
            input=json.dumps(req("tools/list", {}, rid=7)) + "\n",
            capture_output=True, text=True, cwd=root, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        resp = json.loads(proc.stdout.strip())
        self.assertEqual(resp["id"], 7)
        self.assertEqual(len(resp["result"]["tools"]), 18)


if __name__ == "__main__":
    unittest.main()
