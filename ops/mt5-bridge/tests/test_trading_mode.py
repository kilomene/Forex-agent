"""Tests for trading_mode.py. No network, no real broker, tmp run dir only."""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import trading_mode as tm
from trading_mode import (
    MODES,
    NO_LIVE_TO_DEMO_FALLBACK,
    AccountIdentityMismatch,
    ModeError,
    ModeRecord,
    ModeUnavailable,
)

MODE_ENV_VARS = (
    "DEMO_MT5_LOGIN", "DEMO_MT5_SERVER", "DEMO_MT5_PASSWORD",
    "LIVE_MT5_LOGIN", "LIVE_MT5_SERVER", "LIVE_MT5_PASSWORD",
)


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tm, "_RUN_DIR", str(tmp_path))
    for v in MODE_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def _future(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _past(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _auth(**over):
    rec = {
        "authorized_by": "Zenas",
        "live_login": 99887766,
        "live_server": "RealBroker-Live",
        "authorized_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": _future(),
        "note": "test authorization",
    }
    rec.update(over)
    return rec


def _demo_verifier(login=112975129, server="MetaQuotes-Demo", acct_type="DEMO"):
    def verifier(profile):
        assert profile["mode"] == "DEMO"
        return (login, server, acct_type, {"terminal": "ok"})
    return verifier


def _live_verifier(login=99887766, server="RealBroker-Live", acct_type="LIVE"):
    def verifier(profile):
        assert profile["mode"] == "LIVE"
        return (login, server, acct_type, {"terminal": "ok"})
    return verifier


# --- constants / defaults ---------------------------------------------------

def test_no_live_to_demo_fallback_constant_true():
    assert NO_LIVE_TO_DEMO_FALLBACK is True


def test_default_demo_creation(run_dir):
    rec = tm.load_mode()
    assert rec.mode == "DEMO"
    assert rec.account_login == 112975129
    assert rec.broker_server == "MetaQuotes-Demo"
    assert rec.account_type == "DEMO"
    assert rec.changed_by == "system-init"
    assert rec.version == 1
    assert os.path.exists(os.path.join(str(run_dir), "trading_mode.json"))


def test_record_survives_reload(run_dir):
    first = tm.load_mode()
    second = tm.load_mode()
    assert first.to_dict() == second.to_dict()
    # switching to the same mode is a no-op: no version bump, no new log line
    tm.switch_mode("DEMO", changed_by="op", reason="noop", broker_verifier=_demo_verifier())
    third = tm.load_mode()
    assert third.version == 1
    with open(os.path.join(str(run_dir), "trading_mode.log.jsonl")) as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == 1  # only the creation entry


def test_only_demo_live_accepted(run_dir):
    tm.load_mode()
    with pytest.raises(ModeError):
        tm.switch_mode("AUTO", changed_by="op", reason="x", broker_verifier=_demo_verifier())
    with pytest.raises(ModeError):
        tm.switch_mode("PAPER", changed_by="op", reason="x", broker_verifier=_demo_verifier())
    with pytest.raises(ModeError):
        tm.get_profile("SIMULATION")
    with pytest.raises(ModeError):
        tm.history_scope("DRY_LIVE")


def test_legacy_guard_passes_at_import():
    tm._assert_no_legacy_modes()  # raises if a forbidden token is in module source


# --- verify_identity ----------------------------------------------------------

def _rec():
    return ModeRecord(mode="DEMO", account_login=112975129, broker_server="MetaQuotes-Demo",
                      account_type="DEMO", changed_at="t", changed_by="t", reason="t", version=1)


def test_verify_identity_ok_and_normalizes():
    assert tm.verify_identity(_rec(), "112975129", "  MetaQuotes-Demo ", "demo") is True


@pytest.mark.parametrize("login,server,acct_type", [
    (999, "MetaQuotes-Demo", "DEMO"),          # wrong login
    (112975129, "Other-Server", "DEMO"),       # wrong server
    (112975129, "MetaQuotes-Demo", "LIVE"),    # wrong type
    ("notanint", "MetaQuotes-Demo", "DEMO"),   # unparseable login
])
def test_verify_identity_mismatch_raises(login, server, acct_type):
    with pytest.raises(AccountIdentityMismatch):
        tm.verify_identity(_rec(), login, server, acct_type)


# --- LIVE authorization --------------------------------------------------------

def test_live_switch_without_authorization_raises(run_dir, monkeypatch):
    tm.load_mode()
    monkeypatch.setenv("LIVE_MT5_LOGIN", "99887766")
    monkeypatch.setenv("LIVE_MT5_SERVER", "RealBroker-Live")
    monkeypatch.setenv("LIVE_MT5_PASSWORD", "secret")
    with pytest.raises(ModeError):
        tm.switch_mode("LIVE", changed_by="op", reason="x",
                       broker_verifier=_live_verifier(), operator_confirm=True)


def test_expired_authorization_rejected(run_dir, monkeypatch):
    tm.load_mode()
    tm.authorize_live(_auth(expires_at=_past()))
    assert tm.is_live_authorized() is False
    monkeypatch.setenv("LIVE_MT5_LOGIN", "99887766")
    monkeypatch.setenv("LIVE_MT5_SERVER", "RealBroker-Live")
    monkeypatch.setenv("LIVE_MT5_PASSWORD", "secret")
    with pytest.raises(ModeUnavailable) as ei:
        tm.switch_mode("LIVE", changed_by="op", reason="x",
                       broker_verifier=_live_verifier(), operator_confirm=True)
    assert ei.value.status == "LIVE_UNAVAILABLE"
    assert tm.load_mode().mode == "DEMO"


def test_authorize_live_roundtrip(run_dir):
    tm.load_mode()
    tm.authorize_live(_auth())
    assert tm.is_live_authorized() is True
    with pytest.raises(ModeError):
        tm.authorize_live({"authorized_by": "x"})  # missing fields


# --- atomicity -----------------------------------------------------------------

def test_failed_verifier_leaves_old_record(run_dir, monkeypatch):
    tm.load_mode()
    before = tm.load_mode().to_dict()
    log_before = open(os.path.join(str(run_dir), "trading_mode.log.jsonl")).read()
    monkeypatch.setenv("LIVE_MT5_LOGIN", "99887766")
    monkeypatch.setenv("LIVE_MT5_SERVER", "RealBroker-Live")
    monkeypatch.setenv("LIVE_MT5_PASSWORD", "secret")

    def bad_verifier(profile):
        raise RuntimeError("broker unreachable")

    # same-mode switch is a no-op: the verifier is never even called
    rec = tm.switch_mode("DEMO", changed_by="op", reason="x", broker_verifier=bad_verifier)
    assert rec.mode == "DEMO"
    # different-mode path with a failing verifier: old record must survive
    tm.authorize_live(_auth())
    with pytest.raises(RuntimeError):
        tm.switch_mode("LIVE", changed_by="op", reason="x", broker_verifier=bad_verifier,
                       operator_confirm=True)
    after = tm.load_mode().to_dict()
    assert after == before
    assert after["version"] == 1
    assert open(os.path.join(str(run_dir), "trading_mode.log.jsonl")).read() == log_before


def test_identity_mismatch_before_commit_leaves_old_record(run_dir, monkeypatch):
    tm.load_mode()
    tm.authorize_live(_auth())
    monkeypatch.setenv("LIVE_MT5_LOGIN", "99887766")
    monkeypatch.setenv("LIVE_MT5_SERVER", "RealBroker-Live")
    monkeypatch.setenv("LIVE_MT5_PASSWORD", "secret")
    # verifier reports a DIFFERENT login than the authorized target record
    with pytest.raises(AccountIdentityMismatch):
        tm.switch_mode("LIVE", changed_by="op", reason="x",
                       broker_verifier=_live_verifier(login=11111111),
                       operator_confirm=True)
    assert tm.load_mode().mode == "DEMO"
    assert tm.load_mode().version == 1


# --- happy path -----------------------------------------------------------------

def test_successful_demo_to_live_switch(run_dir, monkeypatch):
    tm.load_mode()
    tm.authorize_live(_auth())
    monkeypatch.setenv("LIVE_MT5_LOGIN", "99887766")
    monkeypatch.setenv("LIVE_MT5_SERVER", "RealBroker-Live")
    monkeypatch.setenv("LIVE_MT5_PASSWORD", "secret")
    rec = tm.switch_mode("LIVE", changed_by="Zenas", reason="live account ready",
                         broker_verifier=_live_verifier(), operator_confirm=True)
    assert rec.mode == "LIVE"
    assert rec.account_login == 99887766
    assert rec.broker_server == "RealBroker-Live"
    assert rec.account_type == "LIVE"
    assert rec.version == 2
    # durable + logged with prev_version and operator confirmation
    assert tm.load_mode().to_dict() == rec.to_dict()
    lines = [json.loads(l) for l in
             open(os.path.join(str(run_dir), "trading_mode.log.jsonl")) if l.strip()]
    assert lines[-1]["mode"] == "LIVE"
    assert lines[-1]["prev_version"] == 1
    assert lines[-1]["operator_confirmed"] is True


def test_first_live_transition_requires_operator_confirm(run_dir, monkeypatch):
    tm.load_mode()
    tm.authorize_live(_auth())
    monkeypatch.setenv("LIVE_MT5_LOGIN", "99887766")
    monkeypatch.setenv("LIVE_MT5_SERVER", "RealBroker-Live")
    monkeypatch.setenv("LIVE_MT5_PASSWORD", "secret")
    with pytest.raises(ModeError):
        tm.switch_mode("LIVE", changed_by="op", reason="x",
                       broker_verifier=_live_verifier(), operator_confirm=False)
    assert tm.load_mode().mode == "DEMO"


def test_live_to_demo_requires_explicit_reason_and_identity(run_dir, monkeypatch):
    # first go LIVE
    tm.load_mode()
    tm.authorize_live(_auth())
    monkeypatch.setenv("LIVE_MT5_LOGIN", "99887766")
    monkeypatch.setenv("LIVE_MT5_SERVER", "RealBroker-Live")
    monkeypatch.setenv("LIVE_MT5_PASSWORD", "secret")
    tm.switch_mode("LIVE", changed_by="Zenas", reason="going live",
                   broker_verifier=_live_verifier(), operator_confirm=True)
    # empty reason is refused (no silent downgrade)
    with pytest.raises(ModeError):
        tm.switch_mode("DEMO", changed_by="Zenas", reason="  ",
                       broker_verifier=_demo_verifier())
    # explicit switch back works and is logged
    rec = tm.switch_mode("DEMO", changed_by="Zenas", reason="back to demo experiment",
                         broker_verifier=_demo_verifier())
    assert rec.mode == "DEMO"
    assert rec.version == 3
    assert rec.reason == "back to demo experiment"


# --- positions never cross modes --------------------------------------------------

def test_switch_refused_with_open_positions(run_dir, monkeypatch):
    tm.load_mode()
    tm.authorize_live(_auth())
    monkeypatch.setenv("LIVE_MT5_LOGIN", "99887766")
    monkeypatch.setenv("LIVE_MT5_SERVER", "RealBroker-Live")
    monkeypatch.setenv("LIVE_MT5_PASSWORD", "secret")
    with pytest.raises(ModeError) as ei:
        tm.switch_mode("LIVE", changed_by="op", reason="x",
                       broker_verifier=_live_verifier(), operator_confirm=True,
                       positions_provider=lambda: [101, 102])
    assert "never carried across modes" in str(ei.value)
    assert tm.load_mode().mode == "DEMO"
    # explicit opt-in allows it
    rec = tm.switch_mode("LIVE", changed_by="op", reason="positions acknowledged closed",
                         broker_verifier=_live_verifier(), operator_confirm=True,
                         positions_provider=lambda: [101], allow_with_positions=True)
    assert rec.mode == "LIVE"


# --- credential namespaces --------------------------------------------------------

def test_get_profile_cannot_cross_read(run_dir, monkeypatch):
    monkeypatch.setenv("DEMO_MT5_LOGIN", "112975129")
    monkeypatch.setenv("DEMO_MT5_SERVER", "MetaQuotes-Demo")
    monkeypatch.setenv("DEMO_MT5_PASSWORD", "demo-secret-xyz")
    # LIVE vars unset -> raises, and the error must not leak the demo password
    with pytest.raises(ModeError) as ei:
        tm.get_profile("LIVE")
    assert str(ei.value) == "live credentials not configured"
    assert "demo-secret-xyz" not in str(ei.value)
    # DEMO profile reads only the DEMO namespace
    prof = tm.get_profile("DEMO")
    assert prof["login"] == 112975129
    assert prof["server"] == "MetaQuotes-Demo"
    assert prof["password"] == "demo-secret-xyz"
    assert "LIVE" not in repr(prof)


def test_redact_never_leaks_password():
    shown = tm.redact({"mode": "LIVE", "login": 1, "server": "s", "password": "hunter2"})
    assert shown["password"] == "set"
    assert "hunter2" not in json.dumps(shown)
    assert tm.redact({"mode": "LIVE", "login": 1, "server": "s",
                      "password": None})["password"] == "missing"


# --- history scopes ------------------------------------------------------------------

def test_history_scope_and_no_combining():
    demo = tm.history_scope("DEMO")
    live = tm.history_scope("LIVE")
    assert demo == {"journal": "nova_journal.jsonl", "trades": "nova_trades.jsonl", "scope": "DEMO"}
    assert live["scope"] == "LIVE"
    assert tm.ensure_single_scope(["DEMO", "DEMO", None]) is True
    with pytest.raises(ModeError):
        tm.ensure_single_scope(["DEMO", "LIVE"])


# ------------------------------------------------- switch_mode CLI
import subprocess
import sys as _sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cli(*argv):
    return subprocess.run(
        [_sys.executable, os.path.join(BRIDGE, "switch_mode.py"), *argv],
        capture_output=True, text=True, timeout=60)


def test_cli_status_shows_demo():
    r = _cli("--status")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["mode"] == "DEMO"
    assert data["no_live_to_demo_fallback"] is True


def test_cli_live_switch_refused_without_authorization():
    r = _cli("LIVE", "--by", "tester", "--reason", "test")
    assert r.returncode != 0
    assert "SWITCH REFUSED" in r.stderr
    assert "LIVE_UNAVAILABLE" in r.stderr
    # Mode unchanged.
    r2 = _cli("--status")
    assert json.loads(r2.stdout)["mode"] == "DEMO"


def test_cli_rejects_unknown_mode():
    r = _cli("PAPER", "--by", "tester", "--reason", "test")
    assert r.returncode != 0
