"""Tests for scripts/production_readiness.py.

Runs the real evaluate()/main() against isolated tmp dirs (fresh broker
files, risk config, kill switch, journal) with the REAL trading_mode module
pointed at an isolated run dir via trading_mode.set_run_dir().

Scenarios:
  * READY_FOR_DEMO on a healthy demo setup
  * NOT_READY when the specs feed goes stale
  * never READY_FOR_LIVE without a valid live authorization + broker match
"""
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(os.path.dirname(BRIDGE), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, BRIDGE)

import production_readiness as pr  # noqa: E402
import trading_mode as tm  # noqa: E402

LOGIN = 112975129
SERVER = "MetaQuotes-Demo"


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """Isolated files/run dirs; trading_mode pointed at the tmp run dir."""
    files = tmp_path / "files"
    run = tmp_path / "run"
    files.mkdir()
    run.mkdir()

    (files / "nova_symbol_specs.json").write_text(json.dumps({
        "time": "2026.09.23 13:42:17",
        "account": {"equity": 1256341.44, "balance": 1256331.13,
                    "currency": "USD", "server": SERVER,
                    "time": "2026.09.23 13:42:17"},
        "symbols": {},
    }))
    (files / "nova_positions.json").write_text(json.dumps({
        "time": int(time.time()), "server_time": "2026.09.23 13:42:51",
        "account": LOGIN, "positions": []}))
    (files / "nova_commands.jsonl").write_text("")
    (files / "nova_trades.jsonl").write_text("")
    (run / "risk_config.json").write_text(json.dumps({
        "capital_basis": 200000, "dry_run": False,
        "risk_per_trade_pct": 0.05, "max_concurrent_trades": 10,
        "max_daily_loss_pct": 3.0, "max_spread_points": 50,
        "max_total_exposure_lots": 0, "max_consecutive_losses": 4,
        "max_correlated_positions": 1, "require_stop_loss": True,
        "enforce_correlation_gate": False, "enforce_spread_gate": False,
        "enforce_single_position_per_symbol": False,
        "enforce_consecutive_losses_gate": False,
        "trading_enabled": True}))
    (run / "trading_enabled").write_text("1")
    (run / "nova_journal.jsonl").write_text("")
    (run / "trade_executor.state.json").write_text(json.dumps({"offset": 0}))

    monkeypatch.setattr(pr, "FILES_DIR", str(files))
    monkeypatch.setattr(pr, "RUN_DIR", str(run))

    real_run_dir = tm._run_dir()
    tm.set_run_dir(str(run))
    try:
        tm.load_mode()  # creates the sanctioned DEMO default record
        yield {"files": files, "run": run}
    finally:
        tm.set_run_dir(real_run_dir)


def _main_output():
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = pr.main()  # main() returns the exit code; the __main__
        # guard wraps it in sys.exit() for real CLI use.
    return code, buf.getvalue()


def test_ready_for_demo(iso):
    verdict, reason, results = pr.evaluate()
    assert verdict == "READY_FOR_DEMO", \
        "expected READY_FOR_DEMO, failed: %s" % (
            [c for c in results if c["required"] and not c["ok"]],)
    code, out = _main_output()
    assert code == 0
    first_line = out.split("\n", 1)[0]
    assert first_line == "READY_FOR_DEMO"
    # JSON detail block follows the verdict line.
    detail = json.loads(out.split("\n", 1)[1])
    assert detail["verdict"] == "READY_FOR_DEMO"
    assert len(detail["checks"]) == len(pr.CHECKS)


def test_not_ready_when_specs_stale(iso):
    stale = time.time() - 600
    os.utime(iso["files"] / "nova_symbol_specs.json", (stale, stale))
    verdict, reason, results = pr.evaluate()
    assert verdict == "NOT_READY"
    code, out = _main_output()
    assert code == 2
    assert out.split("\n", 1)[0] == "NOT_READY"


def test_never_ready_for_live_without_authorization(iso):
    # Craft a LIVE mode record bound to the real demo broker identity.
    live_record = {
        "mode": "LIVE", "account_login": LOGIN, "broker_server": SERVER,
        "account_type": "LIVE", "changed_at": "2026-09-23T00:00:00+00:00",
        "changed_by": "test", "reason": "test", "version": 2,
    }
    (iso["run"] / "trading_mode.json").write_text(json.dumps(live_record))
    # No live_authorization.json on disk -> is_live_authorized() is False.
    assert tm.is_live_authorized() is False
    verdict, reason, results = pr.evaluate()
    assert verdict != "READY_FOR_LIVE"
    assert verdict == "NOT_READY"
    code, out = _main_output()
    assert code == 2
    assert out.split("\n", 1)[0] != "READY_FOR_LIVE"


def test_never_ready_for_live_with_identity_mismatch(iso):
    # Even a *valid* authorization cannot produce READY_FOR_LIVE when the
    # broker account block does not match the LIVE record.
    live_record = {
        "mode": "LIVE", "account_login": 99999999,
        "broker_server": "SomeOther-Server", "account_type": "LIVE",
        "changed_at": "2026-09-23T00:00:00+00:00",
        "changed_by": "test", "reason": "test", "version": 2,
    }
    (iso["run"] / "trading_mode.json").write_text(json.dumps(live_record))
    verdict, _, _ = pr.evaluate()
    assert verdict == "NOT_READY"


def test_no_forbidden_fallback_flag_asserted():
    assert tm.NO_LIVE_TO_DEMO_FALLBACK is True
