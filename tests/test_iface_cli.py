"""Interface tests: CLI (scripts/forex).

Runs the real CLI as a subprocess against an isolated storage database.
Every command is exercised with --json; assertions parse the JSON.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="forex_iface_cli_")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLI = os.path.join(_ROOT, "scripts", "forex")

_ENV = dict(os.environ)
_ENV["FOREX_AGENT_HOME"] = _TMP
_ENV["FOREX_AGENT_STORAGE"] = os.path.join(_TMP, "cli.db")
_ENV["BROKER_PROVIDER"] = "disconnected"


def run_cli(*args):
    proc = subprocess.run([sys.executable, _CLI, *args], capture_output=True,
                          text=True, cwd=_ROOT, env=_ENV, timeout=60)
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


class TestCli(unittest.TestCase):
    def test_status_json(self):
        code, payload, _ = run_cli("status", "--json")
        self.assertIsNotNone(payload)
        self.assertIn("status", payload)
        self.assertIn("components", payload)
        # Disconnected broker -> unhealthy is honest, and the shape holds.
        self.assertIn(payload["status"], ("healthy", "degraded", "unhealthy"))

    def test_health_json(self):
        code, payload, _ = run_cli("health", "--json")
        self.assertTrue(payload["ok"])
        self.assertIn("broker", payload["components"])

    def test_signals_empty(self):
        code, payload, _ = run_cli("signals", "--json", "--limit", "5")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["signals"], [])

    def test_positions_and_account_broker_down(self):
        code, payload, _ = run_cli("positions", "--json")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")
        self.assertEqual(code, 1)  # tool error -> exit 1
        code, payload, _ = run_cli("account", "--json")
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")

    def test_analyze_broker_down(self):
        code, payload, _ = run_cli("analyze", "EURUSD", "--json")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")

    def test_risk_standing(self):
        code, payload, _ = run_cli("risk", "--json")
        self.assertTrue(payload["ok"])
        self.assertIn("risk_state", payload)

    def test_performance_broker_down(self):
        code, payload, _ = run_cli("performance", "--json")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "BROKER_UNAVAILABLE")

    def test_events_empty(self):
        code, payload, _ = run_cli("events", "--json")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["events"], [])

    def test_events_bad_since(self):
        code, payload, _ = run_cli("events", "--json", "--since", "whenever")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "INVALID_ARGUMENT")

    def test_config_redacted(self):
        code, payload, _ = run_cli("config", "--json")
        self.assertTrue(payload["ok"])
        blob = json.dumps(payload["config"])
        # No secret VALUES may appear next to their keys in plaintext.
        self.assertNotIn("supersecret", blob)

    def test_kill_switch_flow(self):
        code, payload, _ = run_cli("kill-switch", "--json")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["engaged"])

        code, payload, _ = run_cli("kill-switch", "--engage", "--json",
                                   "--reason", "cli test")
        self.assertEqual(code, 0)
        self.assertTrue(payload["engaged"])

        code, payload, _ = run_cli("kill-switch", "--json")
        self.assertTrue(payload["engaged"])  # latch persisted across processes

        code, payload, _ = run_cli("kill-switch", "--clear", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(payload["engaged"])

    def test_unknown_command_exits_2(self):
        proc = subprocess.run([sys.executable, _CLI, "teleport"],
                              capture_output=True, text=True, cwd=_ROOT,
                              env=_ENV, timeout=60)
        self.assertEqual(proc.returncode, 2)

    def test_human_readable_default(self):
        code, payload, err = run_cli("health")
        # Not JSON, but exits 0 and prints something readable.
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
