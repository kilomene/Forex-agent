"""Tests for the MT5 watchdog's reboot-recovery helpers.

Regression context: on 2026-10-13 ~23:11 UTC ensure_wine burned its local
stash install rounds, the force-depends-version pass, and the apt-get
fallback in ~2 seconds -- all failing instantly on the dpkg/apt lock held
by the platform's os-intent replay -- and left NO diagnostics in the log
because every subprocess call swallowed its own output. The next reboot
must wait out the lock holder and log why an install step failed.
"""
import os
import subprocess
import sys
from unittest import mock

import pytest

import watch_mt5


@pytest.fixture()
def tmp_log(tmp_path, monkeypatch):
    log = tmp_path / "watch_mt5.log"
    monkeypatch.setattr(watch_mt5, "LOG_PATH", str(log))
    return log


def _run_result(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["x"], returncode=rc,
                                       stdout=stdout, stderr=stderr)


class TestAptLockHolder:
    def test_free_when_fuser_finds_nothing(self):
        with mock.patch("subprocess.run",
                        return_value=_run_result(rc=1, stdout="")):
            locked, desc = watch_mt5.apt_lock_holder()
        assert locked is False

    def test_held_reports_pid_and_command(self):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "fuser":
                return _run_result(rc=0, stdout="4321\n")
            if cmd[0] == "ps":
                return _run_result(rc=0, stdout="apt-get\n")
            raise AssertionError("unexpected command: %r" % (cmd,))

        with mock.patch("subprocess.run", side_effect=fake_run):
            locked, desc = watch_mt5.apt_lock_holder()
        assert locked is True
        assert "4321" in desc
        assert "apt-get" in desc
        assert "/var/lib/dpkg/lock-frontend" in desc

    def test_fuser_missing_degrades_gracefully(self):
        with mock.patch("subprocess.run",
                        side_effect=FileNotFoundError("fuser")):
            locked, desc = watch_mt5.apt_lock_holder()
        assert locked is False
        assert "fuser unavailable" in desc


class TestWaitForAptLock:
    def test_immediate_when_free(self):
        with mock.patch.object(watch_mt5, "apt_lock_holder",
                               return_value=(False, "")):
            with mock.patch("time.sleep") as slp:
                assert watch_mt5.wait_for_apt_lock() is True
        slp.assert_not_called()

    def test_timeout_when_holder_never_leaves(self, tmp_log):
        with mock.patch.object(watch_mt5, "apt_lock_holder",
                               return_value=(True, "stub holder")):
            with mock.patch("time.sleep", return_value=None):
                assert watch_mt5.wait_for_apt_lock(timeout_s=0.05,
                                                   poll_s=0.01) is False
        assert "still held" in tmp_log.read_text()

    def test_waits_then_proceeds(self):
        calls = {"n": 0}

        def holder():
            calls["n"] += 1
            return (calls["n"] < 3, "stub holder")

        with mock.patch.object(watch_mt5, "apt_lock_holder",
                               side_effect=holder):
            with mock.patch("time.sleep", return_value=None):
                assert watch_mt5.wait_for_apt_lock(timeout_s=60,
                                                   poll_s=0.01) is True
        assert calls["n"] == 3


class TestRunLogged:
    def test_success_passes_through_rc(self, tmp_log):
        r = watch_mt5.run_logged(["true"], timeout=10, tag="t-success")
        assert r.returncode == 0
        assert not tmp_log.exists() or "t-success" not in tmp_log.read_text()

    def test_failure_logs_rc_and_stderr(self, tmp_log):
        r = watch_mt5.run_logged(["false"], timeout=10, tag="t-failure")
        assert r.returncode != 0
        text = tmp_log.read_text()
        assert "t-failure" in text
        assert "rc=1" in text

    def test_failure_includes_stderr_tail(self, tmp_log):
        r = watch_mt5.run_logged(
            ["sh", "-c", "echo locked >&2; exit 100"],
            timeout=10, tag="t-stderr")
        assert r.returncode == 100
        text = tmp_log.read_text()
        assert "locked" in text


# Regression context (2026-10-14 ~20:41 UTC): after a host reboot the
# pre-install wait_for_apt_lock() returned while the os-intent replay
# then grabbed the dpkg lock in the TOCTOU window -- every dpkg round
# and the apt-get fallback burned in ~1 s on the lock and ensure_wine
# reported FAILED, even though the holder released minutes later.
# The fix: run_logged_lock_retry re-waits on lock-contention and
# retries the step instead of failing fast.
DPKG_LOCK_STDERR = ("dpkg: error: dpkg frontend lock was locked by another "
                    "process with pid 2704")
APTGET_LOCK_STDERR = ("E: Could not get lock /var/lib/dpkg/lock-frontend. "
                      "It is held by process 2704 (apt-get)")


class TestIsLockError:
    def test_dpkg_frontend_lock(self):
        assert watch_mt5.is_lock_error(DPKG_LOCK_STDERR) is True

    def test_aptget_could_not_get_lock(self):
        assert watch_mt5.is_lock_error(APTGET_LOCK_STDERR) is True

    def test_case_insensitive(self):
        assert watch_mt5.is_lock_error(
            "UNABLE TO ACQUIRE THE DPKG FRONTEND LOCK") is True

    def test_real_packaging_error_is_not_lock(self):
        assert watch_mt5.is_lock_error(
            "dpkg: dependency problems prevent configuration of wine64") \
            is False

    def test_none_and_empty(self):
        assert watch_mt5.is_lock_error(None) is False
        assert watch_mt5.is_lock_error("") is False


class TestRunLoggedLockRetry:
    def test_success_first_try_no_wait(self):
        with mock.patch.object(
                watch_mt5, "run_logged",
                return_value=_run_result(rc=0)) as rl, \
             mock.patch.object(
                 watch_mt5, "wait_for_apt_lock",
                 side_effect=AssertionError("should not wait")) as wt:
            r = watch_mt5.run_logged_lock_retry(["dpkg", "-i", "x"],
                                                60, "t")
        assert r.returncode == 0
        assert rl.call_count == 1
        wt.assert_not_called()

    def test_lock_error_then_success_retries_once(self, tmp_log):
        calls = {"n": 0}

        def fake_run_logged(cmd, timeout, tag):
            calls["n"] += 1
            if calls["n"] == 1:
                return _run_result(rc=2, stderr=DPKG_LOCK_STDERR)
            return _run_result(rc=0)

        with mock.patch.object(watch_mt5, "run_logged",
                               side_effect=fake_run_logged), \
             mock.patch.object(watch_mt5, "wait_for_apt_lock",
                               return_value=True) as wt:
            r = watch_mt5.run_logged_lock_retry(["dpkg", "-i", "x"],
                                                60, "t")
        assert r.returncode == 0
        assert calls["n"] == 2
        assert wt.call_count == 1
        assert "lock contention" in tmp_log.read_text()

    def test_non_lock_failure_not_retried(self):
        with mock.patch.object(
                watch_mt5, "run_logged",
                return_value=_run_result(
                    rc=1, stderr="dpkg: dependency problems")) as rl, \
             mock.patch.object(
                 watch_mt5, "wait_for_apt_lock",
                 side_effect=AssertionError("should not wait")) as wt:
            r = watch_mt5.run_logged_lock_retry(["dpkg", "-i", "x"],
                                                60, "t")
        assert r.returncode == 1
        assert rl.call_count == 1
        wt.assert_not_called()

    def test_persistent_lock_exhausts_retries(self):
        with mock.patch.object(
                watch_mt5, "run_logged",
                return_value=_run_result(rc=2,
                                        stderr=APTGET_LOCK_STDERR)) as rl, \
             mock.patch.object(watch_mt5, "wait_for_apt_lock",
                               return_value=True) as wt:
            r = watch_mt5.run_logged_lock_retry(["apt-get", "install"],
                                                60, "t", lock_retries=2)
        # 1 initial + 2 retries, then the last failure stands.
        assert r.returncode == 2
        assert rl.call_count == 3
        assert wt.call_count == 2

    def test_wait_timeout_stops_retrying(self, tmp_log):
        with mock.patch.object(
                watch_mt5, "run_logged",
                return_value=_run_result(rc=2,
                                        stderr=DPKG_LOCK_STDERR)) as rl, \
             mock.patch.object(watch_mt5, "wait_for_apt_lock",
                               return_value=False) as wt:
            r = watch_mt5.run_logged_lock_retry(["dpkg", "-i", "x"],
                                                60, "t")
        assert r.returncode == 2
        assert rl.call_count == 1  # no retry after the timed-out wait
        assert wt.call_count == 1
        assert "not retrying further" in tmp_log.read_text()


class TestEnsureWine:
    def test_noop_when_wine_already_ok(self):
        with mock.patch.object(watch_mt5, "wine_ok", return_value=True):
            with mock.patch.object(
                    watch_mt5, "wait_for_apt_lock",
                    side_effect=AssertionError("should not be called")):
                assert watch_mt5.ensure_wine() is True

    def test_waits_for_lock_before_installing(self, tmp_path,
                                              monkeypatch, tmp_log):
        # wine missing; lock held once then released; stash holds a fake
        # deb so the dpkg path is exercised without touching the system.
        monkeypatch.setattr(watch_mt5, "WINE_DEB_DIR", str(tmp_path))
        monkeypatch.setattr(watch_mt5, "WINE_DEBS", [])
        monkeypatch.setattr(watch_mt5, "WINE_DEP_GLOBS", [])
        monkeypatch.setattr(watch_mt5, "WINE_LINK",
                            str(tmp_path / "wine64-link"))
        calls = []

        def fake_run_logged(cmd, timeout, tag):
            calls.append(tag)
            return _run_result(rc=0)

        with mock.patch.object(watch_mt5, "wine_ok",
                               side_effect=[False, True, True]):
            with mock.patch.object(
                    watch_mt5, "wait_for_apt_lock",
                    side_effect=lambda *a, **k: calls.append("lock-wait")
                    or True):
                with mock.patch.object(watch_mt5, "run_logged",
                                       side_effect=fake_run_logged):
                    # debs list is empty -> falls to "no local debs" path;
                    # wine_ok True on re-check skips apt-get fallback.
                    assert watch_mt5.ensure_wine() is True
        assert calls[0] == "lock-wait"

    def test_exception_never_raises(self, tmp_path, monkeypatch, tmp_log):
        monkeypatch.setattr(watch_mt5, "WINE_DEB_DIR", "/nonexistent-dir-xyz")
        monkeypatch.setattr(watch_mt5, "APT_CACHE", "/nonexistent-dir-xyz")
        monkeypatch.setattr(watch_mt5, "WINE_LINK",
                            str(tmp_path / "wine64-link"))
        with mock.patch.object(watch_mt5, "wine_ok", return_value=False):
            with mock.patch.object(watch_mt5, "wait_for_apt_lock",
                                   return_value=True):
                with mock.patch.object(watch_mt5, "run_logged",
                                       return_value=_run_result(rc=1)):
                    assert watch_mt5.ensure_wine() is False
        assert "FAILED" in tmp_log.read_text()

    def test_mid_install_lock_contention_retries_not_fails(
            self, tmp_path, monkeypatch, tmp_log):
        # 2026-10-14 regression: the replay grabbed the dpkg lock AFTER
        # the pre-install wait, so dpkg -i failed with a lock error. The
        # old code burned the round and FAILED; the fix re-waits and
        # retries the same step.
        deb = tmp_path / "wine64_9.0_amd64.deb"
        deb.write_bytes(b"fake-deb")
        monkeypatch.setattr(watch_mt5, "WINE_DEB_DIR", str(tmp_path))
        monkeypatch.setattr(watch_mt5, "WINE_DEBS", [deb.name])
        monkeypatch.setattr(watch_mt5, "WINE_DEP_GLOBS", [])
        monkeypatch.setattr(watch_mt5, "WINE_LINK",
                            str(tmp_path / "wine64-link"))
        calls = {"n": 0}

        def fake_run_logged(cmd, timeout, tag):
            calls["n"] += 1
            # Every dpkg step first fails with lock contention, then
            # succeeds on the retry.
            if tag.startswith("ensure_wine dpkg") and calls["n"] % 2 == 1:
                return _run_result(rc=2, stderr=DPKG_LOCK_STDERR)
            return _run_result(rc=0)

        waits = {"n": 0}

        def fake_wait(**kwargs):
            waits["n"] += 1
            return True

        with mock.patch.object(watch_mt5, "wine_ok",
                               side_effect=[False, False, True, True]):
            with mock.patch.object(watch_mt5, "wait_for_apt_lock",
                                   side_effect=fake_wait):
                with mock.patch.object(watch_mt5, "run_logged",
                                       side_effect=fake_run_logged):
                    with mock.patch.object(watch_mt5, "dpkg_status",
                                           return_value="installed"):
                        assert watch_mt5.ensure_wine() is True
        # At least one mid-install wait happened (the pre-install wait
        # plus the retry waits).
        assert waits["n"] >= 2
        assert "ensure_wine OK" in tmp_log.read_text()
