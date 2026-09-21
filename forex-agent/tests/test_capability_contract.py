"""Contract tests: agent/capabilities.json is a real capability contract.

Every command, path, and URL in the manifest must resolve to something
real: files exist, CLI commands parse, HTTP endpoints are actually served,
and broker/mode fields match live probes. The generator
(scripts/gen_manifest.py) is the source of truth; --check must pass.
"""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MANIFEST = os.path.join(_ROOT, "agent", "capabilities.json")
_CLI = os.path.join(_ROOT, "scripts", "forex")
_DAEMONS = os.path.join(_ROOT, "scripts", "forex-daemons")
_INSTALLER = os.path.join(_ROOT, "installer", "install.sh")

_TMP = tempfile.mkdtemp(prefix="forex_contract_")
_CLI_ENV = dict(os.environ)
_CLI_ENV["FOREX_AGENT_HOME"] = _TMP
_CLI_ENV["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "contract.db")
_CLI_ENV["BROKER_PROVIDER"] = "disconnected"

sys.path.insert(0, _ROOT)


def load_manifest():
    with open(_MANIFEST) as fh:
        return json.load(fh)


class TestManifestShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_manifest()

    def test_valid_json_with_required_sections(self):
        for section in ("name", "version", "contract_version", "platform",
                        "description", "interfaces", "lifecycle", "broker",
                        "mode", "registration", "tools", "daemons",
                        "safety_invariants"):
            self.assertIn(section, self.m, "missing section: %s" % section)

    def test_identity(self):
        self.assertEqual(self.m["name"], "forex-agent")
        self.assertRegex(self.m["version"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(self.m["contract_version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(self.m["platform"], "linux")
        self.assertTrue(self.m["description"].strip())

    def test_interface_sections(self):
        for iface in ("mcp", "cli", "local_api", "events"):
            self.assertIn(iface, self.m["interfaces"], "missing interface: %s" % iface)

    def test_lifecycle_sections(self):
        for key in ("install", "start", "stop", "restart", "status",
                    "health", "logs", "states"):
            self.assertIn(key, self.m["lifecycle"], "missing lifecycle key: %s" % key)

    def test_registration_is_generic(self):
        reg = self.m["registration"]
        self.assertEqual(reg["mechanism"], "manifest")
        self.assertIn("capabilities.json", reg["manifest_path"])
        # No vendor-specific registration files anywhere near the contract.
        for dirpath, _dirnames, filenames in os.walk(_ROOT):
            if ".git" in dirpath or "__pycache__" in dirpath:
                continue
            for fn in filenames:
                low = fn.lower()
                self.assertNotIn("grok", low, "vendor-specific file: %s" % fn)
                self.assertNotIn("instinct", low, "vendor-specific file: %s" % fn)
                if fn.endswith("_install.py"):
                    self.fail("vendor-specific installer file: %s" % fn)

    def test_generator_check_passes(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "scripts", "gen_manifest.py"),
             "--check"],
            capture_output=True, text=True, cwd=_ROOT, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class TestMcpInterface(unittest.TestCase):
    def test_server_module_matches_manifest(self):
        m = load_manifest()["interfaces"]["mcp"]
        self.assertTrue(os.path.isfile(os.path.join(_ROOT, "agent", "mcp", "server.py"))
                        or os.path.isfile(os.path.join(_ROOT, "agent", "mcp", "server.pyc")),
                        "mcp server module missing")
        from agent.mcp import server
        self.assertEqual(m["command"], "python3 -m agent.mcp.server")
        manifest = load_manifest()
        self.assertEqual(server.SERVER_NAME, manifest["name"])
        self.assertEqual(server.SERVER_VERSION, manifest["version"])

    def test_mcp_ping_over_stdio(self):
        proc = subprocess.run(
            [sys.executable, "-m", "agent.mcp.server"],
            input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n',
            capture_output=True, text=True, cwd=_ROOT, timeout=30,
            env=_CLI_ENV)
        self.assertIn('"result"', proc.stdout)


class TestCliInterface(unittest.TestCase):
    def test_cli_executable(self):
        self.assertTrue(os.path.isfile(_CLI))
        self.assertTrue(os.access(_CLI, os.X_OK))

    def test_every_listed_command_parses(self):
        m = load_manifest()["interfaces"]["cli"]
        help_proc = subprocess.run([sys.executable, _CLI, "--help"],
                                   capture_output=True, text=True, cwd=_ROOT,
                                   timeout=30, env=_CLI_ENV)
        for cmd in m["commands"]:
            self.assertIn(cmd, help_proc.stdout,
                          "command %r not in CLI help" % cmd)
            proc = subprocess.run([sys.executable, _CLI, cmd, "--help"],
                                  capture_output=True, text=True, cwd=_ROOT,
                                  timeout=30, env=_CLI_ENV)
            self.assertEqual(proc.returncode, 0,
                             "command %r --help failed" % cmd)

    def test_example_invocations_run(self):
        # Safe, read-only examples from the manifest (skip --follow: blocks).
        # Exit codes are honest: 0 ok, 1 command failed (e.g. status with a
        # disconnected broker), 2 usage error. All must emit valid JSON.
        cases = [
            ("status", "--json"),
            ("health", "--json"),
            ("broker-status", "--json"),
            ("config", "--json"),
        ]
        for args in cases:
            proc = subprocess.run([sys.executable, _CLI, *args],
                                  capture_output=True, text=True, cwd=_ROOT,
                                  timeout=60, env=_CLI_ENV)
            self.assertIn(proc.returncode, (0, 1),
                          " ".join(args) + ": " + proc.stderr[-500:])
            payload = json.loads(proc.stdout)  # must be JSON either way
            self.assertIsInstance(payload, dict)

    def test_events_example_args_parse(self):
        proc = subprocess.run([sys.executable, _CLI, "events", "--help"],
                              capture_output=True, text=True, cwd=_ROOT,
                              timeout=30, env=_CLI_ENV)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--since-id", "--follow", "--limit"):
            self.assertIn(flag, proc.stdout,
                          "events example flag %r not in CLI" % flag)


class TestLocalApiInterface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["FOREX_AGENT_HOME"] = _TMP
        os.environ["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "api.db")
        os.environ["BROKER_PROVIDER"] = "disconnected"
        from scripts import local_api
        cls.server = local_api.run(0)  # ephemeral port
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05},
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), body

    def test_manifest_base_url_shape(self):
        base = load_manifest()["interfaces"]["local_api"]["base_url"]
        self.assertTrue(base.startswith("http://127.0.0.1:"))
        self.assertIn("FOREX_API_PORT", base)

    def test_every_listed_get_endpoint_served(self):
        m = load_manifest()["interfaces"]["local_api"]["endpoints"]
        for route in m["GET"]:
            if route == "/events":
                continue  # content-negotiated; tested below
            status, _headers, body = self._get(route)
            self.assertNotEqual(status, 404, "GET %s not served" % route)
            self.assertEqual(status, 200, "GET %s -> %s" % (route, status))
            payload = json.loads(body)
            self.assertIn("ok", payload, "GET %s missing ok" % route)

    def test_post_routes_registered(self):
        from scripts import local_api
        for route in load_manifest()["interfaces"]["local_api"]["endpoints"]["POST"]:
            self.assertIn(route, local_api._POST_ROUTES,
                          "POST %s not registered" % route)

    def test_status_includes_broker_status(self):
        status, _headers, body = self._get("/status")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("broker_status", payload)
        self.assertIn("provider", payload["broker_status"])

    def test_events_sse_endpoint(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/events",
                     headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        try:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.getheader("Content-Type"))
        finally:
            conn.close()

    def test_events_poll_fallback(self):
        status, _headers, body = self._get("/events/latest")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("last_event_id", payload)


class TestLifecycleInterface(unittest.TestCase):
    def test_installer_and_daemon_scripts_exist(self):
        m = load_manifest()["lifecycle"]
        self.assertTrue(os.path.isfile(_INSTALLER) and os.access(_INSTALLER, os.X_OK))
        self.assertTrue(os.path.isfile(_DAEMONS) and os.access(_DAEMONS, os.X_OK))
        self.assertIn("installer/install.sh", m["install"]["command"])
        for cmd in (m["start"], m["stop"], m["restart"]):
            self.assertIn("scripts/forex-daemons", cmd)

    def test_daemon_status_runs(self):
        proc = subprocess.run([_DAEMONS, "status"], capture_output=True,
                              text=True, timeout=30, env=_CLI_ENV)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for name in ("market_monitor", "signal_monitor",
                     "position_monitor", "health_monitor"):
            self.assertIn(name, proc.stdout)

    def test_lifecycle_states_vocabulary(self):
        states = load_manifest()["lifecycle"]["states"]
        for s in ("installed", "configured", "operational",
                  "broker_disconnected", "broker_connected",
                  "trading_disabled", "trading_ready",
                  "needs_credentials", "error"):
            self.assertIn(s, states)


class TestBrokerAndMode(unittest.TestCase):
    def test_broker_fields_match_live_probe(self):
        from agent.tools import backend
        live = backend.broker_adapter().broker_status()["broker"]
        for field in load_manifest()["broker"]["fields"]:
            self.assertIn(field, live,
                          "manifest broker field %r not in live broker_status()" % field)

    def test_broker_never_fakes_connected(self):
        from agent.tools import backend
        live = backend.broker_adapter().broker_status()["broker"]
        # This sandbox has no MT5 runtime: the honest answer is disconnected.
        self.assertFalse(live["connected"])
        self.assertTrue(live["detail"].get("reason"))

    def test_mode_values_match_config(self):
        m = load_manifest()["mode"]
        self.assertEqual(m["default"], "dry_run")
        from agent.tools import backend
        self.assertIn(backend.app_config().mode, m["values"])

    def test_config_reports_mode(self):
        proc = subprocess.run([sys.executable, _CLI, "config", "--json"],
                              capture_output=True, text=True, cwd=_ROOT,
                              timeout=60, env=_CLI_ENV)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["config"]["mode"], "dry_run")


if __name__ == "__main__":
    unittest.main()
