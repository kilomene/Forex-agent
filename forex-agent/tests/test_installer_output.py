"""Installer machine-readable output tests.

installer/install.sh --agent (alias --json) must emit a structured result
the agent can consume: precise states derived from REAL probes, never
invented. install-state.json keeps its legacy states so
installer/test_idempotent.sh stays green.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_INSTALLER = os.path.join(_ROOT, "installer", "install.sh")

ALLOWED_STATUSES = {"installed", "configured", "operational",
                    "broker_disconnected", "broker_connected",
                    "trading_disabled", "trading_ready",
                    "needs_credentials", "error"}

RESULT_KEYS = {"schema", "status", "prefix", "mode", "kill_switch_engaged",
               "signals", "events", "agent_notification", "mcp", "daemons",
               "manifest", "broker", "broker_detail", "broker_provider",
               "trading"}


def clean_env(**overrides):
    """Deterministic env: scrub credential/provider vars, then apply overrides."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/root"),
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    env = {k: v for k, v in env.items() if v}
    for key in ("FOREX_AGENT_HOME", "FOREX_AGENT_STORAGE", "FOREX_API_PORT",
                "FOREX_AGENT_NOTIFICATION_COMMAND",
                "BROKER_PROVIDER", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER",
                "WORKER_API_KEY", "DRY_RUN", "MODE"):
        env.pop(key, None)
    env.update(overrides)
    return env


def run_install(prefix, env, *flags, timeout=180):
    proc = subprocess.run([_INSTALLER, "--prefix", prefix,
                           "--non-interactive", *flags],
                          capture_output=True, text=True, cwd=_ROOT,
                          env=env, timeout=timeout)
    return proc


def agent_result_from_stdout(proc):
    """The installer prints the JSON result as its final stdout line."""
    candidates = [ln for ln in proc.stdout.splitlines()
                  if ln.strip().startswith("{")]
    assert candidates, "no JSON result on stdout:\n%s" % proc.stdout[-2000:]
    return json.loads(candidates[-1])


class TestInstallerAgentOutput(unittest.TestCase):
    def setUp(self):
        self.prefix = tempfile.mkdtemp(prefix="forex_inst_test_")

    def tearDown(self):
        # Never leave daemons running from the with-daemons test.
        subprocess.run([os.path.join(_ROOT, "scripts", "forex-daemons"), "stop"],
                       capture_output=True, timeout=60,
                       env=clean_env(FOREX_AGENT_HOME=self.prefix))
        shutil.rmtree(self.prefix, ignore_errors=True)

    def test_fresh_install_output_shape(self):
        proc = run_install(self.prefix, clean_env(), "--skip-daemons", "--agent")
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        result = agent_result_from_stdout(proc)
        self.assertIn(result["status"], ALLOWED_STATUSES)
        self.assertTrue(RESULT_KEYS <= set(result),
                        "missing keys: %s" % (RESULT_KEYS - set(result)))
        self.assertEqual(result["schema"], "forex-agent.install-result/1")
        self.assertEqual(result["prefix"], self.prefix)
        self.assertEqual(result["mode"], "dry_run")
        # --skip-daemons: no API/SSE server was started, so the honest
        # states are unavailable — the installer must not claim events
        # or notifications are running from daemon liveness alone.
        self.assertEqual(result["events"], "unavailable")
        self.assertEqual(result["agent_notification"], "unavailable")
        self.assertFalse(result["daemons"])
        # install-result.json on disk matches what was printed.
        with open(os.path.join(self.prefix, "install-result.json")) as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk, result)
        # Legacy state file keeps its states (test_idempotent.sh compat).
        with open(os.path.join(self.prefix, "install-state.json")) as fh:
            state = json.load(fh)
        self.assertIn(state["state"], ("ready", "needs_credentials"))
        # The manifest path in the result is real.
        self.assertTrue(os.path.isfile(result["manifest"]))
        with open(result["manifest"]) as fh:
            json.load(fh)

    def test_json_flag_alias(self):
        proc = run_install(self.prefix, clean_env(), "--skip-daemons", "--json")
        self.assertEqual(proc.returncode, 0)
        result = agent_result_from_stdout(proc)
        self.assertIn(result["status"], ALLOWED_STATUSES)

    def test_repeat_install_idempotent(self):
        secrets = os.path.join(self.prefix, "secrets.env")
        os.makedirs(self.prefix, exist_ok=True)
        with open(secrets, "w") as fh:
            fh.write("MT5_LOGIN='sentinel-login'\n")
        os.chmod(secrets, 0o600)
        before = open(secrets, "rb").read()

        r1 = agent_result_from_stdout(
            run_install(self.prefix, clean_env(), "--skip-daemons", "--agent"))
        r2 = agent_result_from_stdout(
            run_install(self.prefix, clean_env(), "--skip-daemons", "--agent"))
        self.assertEqual(r1["status"], r2["status"])
        self.assertEqual(open(secrets, "rb").read(), before,
                         "secrets file must never be overwritten")
        self.assertEqual(stat.S_IMODE(os.stat(secrets).st_mode), 0o600)

    def test_missing_credentials(self):
        proc = run_install(self.prefix,
                           clean_env(BROKER_PROVIDER="mt5"),
                           "--skip-daemons", "--agent")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        result = agent_result_from_stdout(proc)
        self.assertEqual(result["status"], "needs_credentials")
        self.assertEqual(result["required"],
                         ["MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"])

    def test_invalid_credential_reported(self):
        # Empty and whitespace-only values count as missing/invalid.
        for bad in ("", "   "):
            prefix = tempfile.mkdtemp(prefix="forex_inst_badcred_")
            try:
                proc = run_install(
                    prefix,
                    clean_env(BROKER_PROVIDER="mt5", MT5_LOGIN=bad,
                              MT5_PASSWORD="secret", MT5_SERVER="demo"),
                    "--skip-daemons", "--agent")
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
                result = agent_result_from_stdout(proc)
                self.assertEqual(result["status"], "needs_credentials",
                                 "bad login %r not detected" % bad)
                self.assertIn("MT5_LOGIN", result["required"])
                self.assertNotIn("MT5_PASSWORD", result["required"])
                self.assertNotIn("MT5_SERVER", result["required"])
            finally:
                shutil.rmtree(prefix, ignore_errors=True)

    def test_broker_unavailable_not_faked_as_connected(self):
        # Full install WITH daemons, broker expected but this machine has
        # no MT5 runtime: honest status must be broker_disconnected, never
        # broker_connected/trading_ready.
        env = clean_env(BROKER_PROVIDER="mt5", MT5_LOGIN="12345",
                        MT5_PASSWORD="secret", MT5_SERVER="demo",
                        FOREX_AGENT_HOME=self.prefix)
        proc = run_install(self.prefix, env, "--agent", timeout=300)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout[-2000:] + proc.stderr[-2000:])
        result = agent_result_from_stdout(proc)
        self.assertEqual(result["status"], "broker_disconnected")
        self.assertEqual(result["broker"], "disconnected")
        self.assertNotEqual(result["broker"], "connected")
        self.assertTrue(result["broker_detail"],
                        "broker_detail must explain why, not just say down")
        # Full install WITH services: the installer really probed
        # 127.0.0.1:$FOREX_API_PORT (TCP + /health + SSE handshake), so
        # "running" is earned, not inferred from daemon liveness.
        self.assertEqual(result["events"], "running")
        # No host-agent sink command in this env: the bridge runs but
        # delivers nowhere — reported honestly as unconfigured.
        self.assertEqual(result["agent_notification"], "unconfigured")
        self.assertTrue(result["daemons"])

    def test_agent_notification_configured_with_sink_command(self):
        # Full install WITH daemons and a host-agent sink command: the
        # bridge runs and a sink is set -> "configured".
        env = clean_env(FOREX_AGENT_NOTIFICATION_COMMAND="true",
                        FOREX_AGENT_HOME=self.prefix)
        proc = run_install(self.prefix, env, "--agent", timeout=300)
        self.assertEqual(proc.returncode, 0,
                         proc.stdout[-2000:] + proc.stderr[-2000:])
        result = agent_result_from_stdout(proc)
        self.assertEqual(result["events"], "running")
        self.assertEqual(result["agent_notification"], "configured")
        self.assertTrue(result["daemons"])

    def test_error_shape(self):
        proc = run_install(self.prefix, clean_env(),
                           "--skip-daemons", "--source", "/nonexistent", "--agent")
        self.assertNotEqual(proc.returncode, 0)
        result = agent_result_from_stdout(proc)
        self.assertEqual(result["status"], "error")
        self.assertIn("code", result["error"])
        self.assertIn("step", result["error"])
        with open(os.path.join(self.prefix, "install-result.json")) as fh:
            self.assertEqual(json.load(fh)["status"], "error")


if __name__ == "__main__":
    unittest.main()
