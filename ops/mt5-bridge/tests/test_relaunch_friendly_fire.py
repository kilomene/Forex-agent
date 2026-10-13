"""Regression tests for the 2026-10-13 relaunch friendly-fire incident.

Incident: watch_mt5.relaunch() ran `pkill -f "run-mt5-tunnel[.]sh"` on its
120s timeout path. Supervisor ticks are 5 min apart but a launch can take
minutes (the WinInet reg helpers hang ~4 min each without the LD_PRELOAD
shim), so a timed-out tick's cleanup SIGKILLed the NEXT tick's fresh
launcher mid-flight. The broad pkill is gone: on timeout the supervisor now
SIGKILLs only its own launcher PID and returns False. These tests pin that.
"""
import subprocess
from unittest import mock

import pytest

import watch_mt5


def _relaunch_with_timeout():
    """Run relaunch() with Popen mocked to time out; return (result, proc)."""
    with mock.patch("subprocess.Popen") as Popen, \
            mock.patch("subprocess.run") as run, \
            mock.patch.object(watch_mt5, "log"):
        proc = mock.Mock()
        proc.communicate.side_effect = subprocess.TimeoutExpired("bash", 600)
        Popen.return_value = proc
        result = watch_mt5.relaunch()
        return result, proc, run


def test_relaunch_timeout_kills_only_own_launcher():
    result, proc, run = _relaunch_with_timeout()
    assert result is False
    proc.kill.assert_called_once_with()
    # The friendly-fire pkill is gone: no broad process kill on timeout.
    run.assert_not_called()


def test_relaunch_timeout_returns_false_without_pkill_in_source():
    import inspect
    src = inspect.getsource(watch_mt5.relaunch)
    # The docstring documents the removed pkill for history; pin that no
    # executable pkill call remains in the function body.
    assert '["pkill"' not in src
    assert "subprocess.run" not in src


def test_relaunch_timeout_uses_600s_budget():
    with mock.patch("subprocess.Popen") as Popen, \
            mock.patch.object(watch_mt5, "log"):
        proc = mock.Mock()
        proc.communicate.return_value = ("", None)
        proc.returncode = 0
        Popen.return_value = proc
        assert watch_mt5.relaunch() is True
        # communicate was given the 600s timeout kwarg
        _, kwargs = proc.communicate.call_args
        assert kwargs.get("timeout") == 600
