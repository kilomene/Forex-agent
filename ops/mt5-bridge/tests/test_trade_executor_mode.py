"""Worker B (2026-09-23): execution-integrity tests for trade_executor.py.

Covers the forex executor integration upgrade:
  * startup broker identity validation (fail-closed, journals mode.unavailable)
  * per-order identity lock (aborts the operation, daemon keeps running)
  * durable idempotency (claim-before-write, restart duplicate suppression)
  * timeout -> EXECUTION_UNKNOWN -> broker-state reconcile -> single resend
  * filling modes (carried when present, rejected when broker-unsupported)
  * disconnect halt (mode.unavailable) and recovery (mode.recovered)

Hermetic: tmp_path only, no MT5, no network. Uses the REAL trading_mode
module pointed at an isolated run dir (same pattern as
test_production_readiness.py).
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_executor as te  # noqa: E402
import trading_mode as tm  # noqa: E402
from idempotency import (IdempotencyStore, make_idempotency_key,  # noqa: E402
                         parse_iso)

LOGIN = 112975129
SERVER = "MetaQuotes-Demo"


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """Isolated files/run dirs; real trading_mode pointed at tmp run dir."""
    files = tmp_path / "files"
    run = tmp_path / "run"
    files.mkdir()
    run.mkdir()
    real_run_dir = tm._run_dir()
    tm.set_run_dir(str(run))
    try:
        tm.load_mode()  # creates the sanctioned DEMO default record
        yield {"files": files, "run": run}
    finally:
        tm.set_run_dir(real_run_dir)


def write_specs(files, *, login=LOGIN, server=SERVER, acct_type="DEMO",
                age_s=0, symbols=None):
    t = (datetime.now() - timedelta(seconds=age_s)).strftime("%Y.%m.%d %H:%M:%S")
    account = {"equity": 1_000_000.0, "balance": 1_000_000.0,
               "currency": "USD", "server": server, "time": t}
    if login is not None:
        account["login"] = login
    if acct_type is not None:
        account["type"] = acct_type
    specs = {"time": t, "account": account,
             "symbols": symbols or {"XPDUSD": _spec()}}
    (files / "nova_symbol_specs.json").write_text(json.dumps(specs))
    return specs


def _spec(**kw):
    s = {"tick_value": 1.0, "tick_size": 0.001, "volume_min": 0.01,
         "volume_max": 100.0, "volume_step": 0.01, "stops_level_points": 0,
         "spread_points": 20, "digits": 3, "point": 0.001}
    s.update(kw)
    return s


def _sig(**over):
    s = {"id": "XPDUSD_M15_BUY_1790028000", "type": "signal.detected",
         "symbol": "XPDUSD", "timeframe": "M15", "direction": "BUY",
         "entry_price": 1310.914, "stop_loss": 1306.344,
         "take_profit": 1320.054}
    s.update(over)
    return s


def make_eng(iso, **kw):
    return te.TraderEngine(files_dir=str(iso["files"]),
                           state_dir=str(iso["run"]), **kw)


def read_journal(iso):
    p = iso["run"] / "nova_journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def journal_types(iso):
    return [e.get("type") for e in read_journal(iso)]


def read_commands(iso):
    p = iso["files"] / "nova_commands.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ------------------------------------------------- startup validation

def test_startup_validate_ok_pins_identity(iso):
    write_specs(iso["files"])
    eng = make_eng(iso)
    record, account = eng.startup_validate()
    assert record.mode == "DEMO"
    assert record.account_login == LOGIN
    assert record.broker_server == SERVER
    assert account["server"] == SERVER
    assert eng._mode_record is record


def test_startup_validate_wrong_account_type_fails_closed(iso):
    write_specs(iso["files"], acct_type="LIVE")
    eng = make_eng(iso)
    with pytest.raises(SystemExit) as ei:
        eng.startup_validate()
    assert ei.value.code == 2
    assert "mode.unavailable" in journal_types(iso)


def test_startup_validate_wrong_server_fails_closed(iso):
    write_specs(iso["files"], server="MetaQuotes-Other")
    eng = make_eng(iso)
    with pytest.raises(SystemExit):
        eng.startup_validate()
    assert "mode.unavailable" in journal_types(iso)


def test_startup_validate_stale_specs_fails_closed(iso):
    write_specs(iso["files"], age_s=300)
    eng = make_eng(iso)
    with pytest.raises(SystemExit):
        eng.startup_validate()
    types = journal_types(iso)
    assert "mode.unavailable" in types


def test_startup_validate_missing_specs_fails_closed(iso):
    eng = make_eng(iso)
    with pytest.raises(SystemExit):
        eng.startup_validate()
    assert "mode.unavailable" in journal_types(iso)


# ------------------------------------------------- per-order identity lock

def test_per_order_identity_mismatch_aborts_operation(iso):
    write_specs(iso["files"])
    eng = make_eng(iso)
    eng.startup_validate()
    # Broker now reports a different server mid-run.
    write_specs(iso["files"], server="MetaQuotes-Other")
    sig = _sig()
    cmd = te.build_command(sig, 1.0)
    status, sent = eng.send_open_command(cmd, sig, {"volume": 1.0})
    assert status == "aborted:identity_mismatch"
    assert sent is None
    assert read_commands(iso) == []  # nothing reached the broker file
    types = journal_types(iso)
    assert "account.identity_mismatch" in types
    assert "command.sent" not in types


def test_per_order_identity_ok_when_stable(iso):
    write_specs(iso["files"])
    eng = make_eng(iso)
    eng.startup_validate()
    ok, reason = eng.assert_identity()
    assert ok, reason


# ------------------------------------------------- idempotency / duplicates

def test_restart_duplicate_suppressed(iso):
    write_specs(iso["files"])
    eng1 = make_eng(iso)
    eng1.startup_validate()
    sig = _sig()
    cmd = te.build_command(sig, 1.0)
    status, sent = eng1.send_open_command(cmd, sig, {"volume": 1.0})
    assert status == "sent"
    assert len(read_commands(iso)) == 1

    # Simulate a daemon restart: brand-new engine, same durable dirs.
    eng2 = make_eng(iso)
    eng2.startup_validate()
    cmd2 = te.build_command(sig, 1.0)
    status2, sent2 = eng2.send_open_command(cmd2, sig, {"volume": 1.0})
    assert status2 == "duplicate"
    assert sent2 is None
    assert len(read_commands(iso)) == 1  # still exactly one broker command
    assert "execution.duplicate_suppressed" in journal_types(iso)


def test_idempotency_store_claim_duplicate_persists(tmp_path):
    p = str(tmp_path / "idem.json")
    s1 = IdempotencyStore(p)
    assert s1.check_or_claim("k1", {"status": "proposed",
                                    "command_id": "c1"}) == "new"
    assert s1.check_or_claim("k1", {"status": "proposed"}) == "duplicate"
    s2 = IdempotencyStore(p)  # new instance, same file
    assert s2.check_or_claim("k1", {"status": "proposed"}) == "duplicate"
    _k, rec = s2.find_by_command_id("c1")
    assert rec["status"] == "proposed"


def test_stale_claim_flips_unknown_at_load(tmp_path):
    p = str(tmp_path / "idem.json")
    s1 = IdempotencyStore(p)
    s1.check_or_claim("k-stale", {"status": "sent", "command_id": "c-stale"})
    old = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    s1.update("k-stale", sent_at=old, created_at=old)
    s2 = IdempotencyStore(p)
    assert s2.get("k-stale")["status"] == "unknown"
    notes = dict(s2.drain_stale_notes())
    assert "k-stale" in notes
    assert dict(s2.drain_stale_notes()) == {}  # one-time


def test_make_idempotency_key_stable():
    k1 = make_idempotency_key("a", 1, "x")
    k2 = make_idempotency_key("a", 1, "x")
    k3 = make_idempotency_key("a", 1, "y")
    assert k1 == k2 and k1 != k3 and len(k1) == 64


# ------------------------------------------------- timeout -> unknown -> reconcile

def _send_and_age(iso, eng, sig, volume=1.0, age_s=61):
    cmd = te.build_command(sig, volume)
    status, sent = eng.send_open_command(cmd, sig, {"volume": volume})
    assert status == "sent"
    key = next(k for k, r in eng.idem._records.items()
               if r.get("command_id") == sent["id"])
    old = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    eng.idem.update(key, sent_at=old)
    return sent, key


def test_timeout_unknown_then_adopted_no_double_fill(iso):
    write_specs(iso["files"])
    eng = make_eng(iso)
    eng.startup_validate()
    sig = _sig()
    sent, key = _send_and_age(iso, eng, sig)
    assert len(read_commands(iso)) == 1

    # Broker DID execute: fresh positions feed carries the matching fill.
    now_epoch = time.time()
    (iso["files"] / "nova_positions.json").write_text(json.dumps({
        "time": int(now_epoch), "account": LOGIN, "positions": [{
            "ticket": 777001, "symbol": "XPDUSD", "type": "BUY",
            "volume": 1.0, "open_time": now_epoch}]}))

    eng.sweep_timeouts()
    types = journal_types(iso)
    assert "execution.unknown" in types
    assert "execution.adopted" in types
    assert "execution.resent" not in types
    assert eng.idem.get(key)["status"] == "filled"
    assert eng.idem.get(key)["broker_ticket"] == 777001
    assert len(read_commands(iso)) == 1  # no duplicate order sent

    # A second sweep changes nothing.
    n_journal = len(read_journal(iso))
    eng.sweep_timeouts()
    assert len(read_journal(iso)) == n_journal


def test_timeout_proof_of_no_execution_resends_exactly_once(iso):
    write_specs(iso["files"])
    eng = make_eng(iso)
    eng.startup_validate()
    sig = _sig()
    sent, key = _send_and_age(iso, eng, sig)

    # Fresh EMPTY positions feed: broker proves the order never executed.
    (iso["files"] / "nova_positions.json").write_text(json.dumps({
        "time": int(time.time()), "account": LOGIN, "positions": []}))
    (iso["files"] / "nova_trades.jsonl").write_text("")

    eng.sweep_timeouts()
    types = journal_types(iso)
    assert "execution.unknown" in types
    assert "execution.resent" in types
    assert eng.idem.get(key)["status"] == "superseded"
    assert len(read_commands(iso)) == 2  # original + exactly one resend
    resent = read_commands(iso)[1]
    assert resent["id"] == sent["id"] + "-r1"

    # Age the RESEND past the timeout too: the single resend is already
    # used, so the entry is abandoned -- never a third command.
    new_key = next(k for k, r in eng.idem._records.items()
                   if r.get("command_id") == resent["id"])
    old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    eng.idem.update(new_key, sent_at=old)
    eng.sweep_timeouts()
    assert "execution.abandoned" in journal_types(iso)
    assert len(read_commands(iso)) == 2


def test_timeout_inconclusive_stays_unknown_no_resend(iso):
    write_specs(iso["files"])
    eng = make_eng(iso)
    eng.startup_validate()
    sig = _sig()
    _sent, key = _send_and_age(iso, eng, sig)
    # No positions file at all: broker state inconclusive -> deferred.
    eng.sweep_timeouts()
    assert "execution.reconcile_deferred" in journal_types(iso)
    assert eng.idem.get(key)["status"] == "unknown"
    assert len(read_commands(iso)) == 1


# ------------------------------------------------- filling modes

def test_check_filling_mode_unsupported_rejects(iso):
    eng = make_eng(iso)
    sig = _sig()
    specs = {"symbols": {"XPDUSD": _spec(filling_mode_unsupported=True)}}
    ok, mode, reason = eng.check_filling_mode(sig, specs)
    assert ok is False
    assert mode is None
    assert "filling_mode_unsupported" in reason


def test_check_filling_mode_allow_list_rejects(tmp_path):
    # check_filling_mode needs no broker files; a bare engine suffices.
    run = tmp_path / "run"
    run.mkdir()
    e = te.TraderEngine(files_dir=str(tmp_path), state_dir=str(run))
    sig = _sig()
    specs = {"symbols": {"XPDUSD": _spec(
        filling_mode="FOK", filling_modes_supported=["IOC"])}}
    ok, mode, reason = e.check_filling_mode(sig, specs)
    assert ok is False
    assert "FOK" in reason


def test_build_command_carries_filling_mode():
    sig = _sig()
    cmd = te.build_command(sig, 1.0, filling_mode="IOC")
    assert cmd["filling_mode"] == "IOC"
    cmd2 = te.build_command(sig, 1.0)
    assert "filling_mode" not in cmd2  # legacy schema unchanged


# ------------------------------------------------- disconnect halt / recovery

def _stale_specs_file(iso, age_s=400):
    write_specs(iso["files"])
    p = str(iso["files"] / "nova_symbol_specs.json")
    old = time.time() - age_s
    os.utime(p, (old, old))


def test_disconnect_halt_journals_unavailable(iso):
    write_specs(iso["files"])
    eng = make_eng(iso)
    _stale_specs_file(iso)
    assert eng.check_disconnect() is True
    assert eng.disconnect_halt is True
    types = journal_types(iso)
    assert "mode.unavailable" in types
    ev = [e for e in read_journal(iso)
          if e.get("type") == "mode.unavailable"][-1]
    assert ev["status"] == "DEMO_UNAVAILABLE"


def test_disconnect_recovery_journals_recovered(iso):
    write_specs(iso["files"])
    eng = make_eng(iso)
    _stale_specs_file(iso)
    assert eng.check_disconnect() is True
    write_specs(iso["files"])  # fresh again (mtime now)
    assert eng.check_disconnect() is False
    assert eng.disconnect_halt is False
    types = journal_types(iso)
    assert "mode.unavailable" in types
    assert "mode.recovered" in types


def test_disconnect_halt_blocks_new_entries(iso, tmp_path):
    # Full handle_signal path: halted engine must not command.
    files = iso["files"]
    now = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    specs = {"time": now,
             "account": {"equity": 1_000_000.0, "balance": 1_000_000.0,
                         "currency": "USD", "server": SERVER, "time": now,
                         "login": LOGIN, "type": "DEMO"},
             "symbols": {"XPDUSD": _spec()}}
    (files / "nova_symbol_specs.json").write_text(json.dumps(specs))
    (files / "nova_signals.jsonl").write_text(
        json.dumps(_sig()) + "\n")
    (iso["run"] / "risk_config.json").write_text(json.dumps({
        "trading_enabled": True, "dry_run": False,
        "risk_per_trade_pct": 0.05, "max_concurrent_trades": 10,
        "max_daily_loss_pct": 3.0, "max_spread_points": 50,
        "capital_basis": 200000}))
    (iso["run"] / "trading_enabled").write_text("1")
    eng = make_eng(iso)
    eng.startup_validate()
    # Force the halt flag as check_disconnect() would set it.
    eng.disconnect_halt = True
    cfg = te.load_config(str(iso["run"] / "risk_config.json"))
    decision = eng.handle_signal(
        _sig(), cfg, True, specs, True, {}, 0.0, "2026-09-23",
        market_open=True)
    assert decision == "skipped:disconnect_halt"
    assert read_commands(iso) == []


# ------------------------------------------------- synthetic test-signal gate
# signal_generator.py --test writes {"test": true} synthetic signals into the
# live signals file. They must never enter the trading path (§18, §32).

def test_synthetic_test_signals_rejected(iso):
    files = iso["files"]
    now = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    specs = {"time": now,
             "account": {"equity": 1_000_000.0, "balance": 1_000_000.0,
                         "currency": "USD", "server": SERVER, "time": now,
                         "login": LOGIN, "type": "DEMO"},
             "symbols": {}}
    (files / "nova_symbol_specs.json").write_text(json.dumps(specs))
    real_sig = {"type": "signal.detected", "id": "real-1", "symbol": "XPDUSD",
                "timeframe": "M15", "direction": "BUY", "entry_price": 100.0,
                "stop_loss": 99.0, "take_profit": 102.0,
                "candle_time": now, "server_time": now}
    fake_sig = {"type": "signal.detected", "id": "fake-1", "symbol": "XPDUSD",
                "timeframe": "M15", "direction": "BUY", "entry_price": 100.0,
                "stop_loss": 99.0, "take_profit": 102.0,
                "candle_time": now, "server_time": now,
                "test": True, "note": "synthetic bridge test"}
    (files / "nova_signals.jsonl").write_text(
        json.dumps(fake_sig) + "\n" + json.dumps(real_sig) + "\n")
    (iso["run"] / "risk_config.json").write_text(json.dumps({
        "trading_enabled": True, "dry_run": True,
        "risk_per_trade_pct": 0.05, "max_concurrent_trades": 10,
        "max_daily_loss_pct": 3.0, "max_spread_points": 50,
        "capital_basis": 200000}))
    eng = make_eng(iso)
    eng.startup_validate()
    eng.process_signals()
    types = journal_types(iso)
    assert "signal.rejected" in types
    rej = [e for e in read_journal(iso)
           if e.get("type") == "signal.rejected"]
    assert any(e.get("reason") == "test_signal" for e in rej)
    # The synthetic signal never became signal.received.
    received_ids = [e.get("signal_id") for e in read_journal(iso)
                    if e.get("type") == "signal.received"]
    assert "fake-1" not in received_ids
    assert "real-1" in received_ids
