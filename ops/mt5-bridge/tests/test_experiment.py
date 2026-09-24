"""Hermetic unit tests for the 30-day experiment extensions
(risk bounds, market-hours gate, full event journaling)."""
import json
import os
import sys
from datetime import datetime

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_executor as te  # noqa: E402


# ---------------------------------------------------------------- fixtures

def spec(**kw):
    s = {
        "tick_value": 1.0, "tick_size": 0.001, "volume_min": 0.01,
        "volume_max": 100.0, "volume_step": 0.01, "stops_level_points": 0,
        "spread_points": 20, "digits": 3, "point": 0.001,
    }
    s.update(kw)
    return s


def gate_ctx(**over):
    ctx = dict(
        enabled=True, dry_run=False,
        specs={"symbols": {"XPDUSD": spec()},
               "account": {"server": "MetaQuotes-Demo", "equity": 1_000_000.0}},
        specs_fresh=True, open_positions={}, todays_profit=0.0,
        today_str="2026-09-21", risk_pct=0.5, max_concurrent=3,
        max_spread_points=50, max_daily_loss_pct=2.0, equity=1_000_000.0,
        market_open=True,
        # fail-closed risk basis (2026-09-22): None/invalid -> skip.
        # Baseline fixtures carry a valid basis so other gates are tested.
        risk_basis=1_000_000.0,
    )
    ctx.update(over)
    return ctx


def sig(**over):
    s = {
        "id": "XPDUSD_M15_BUY_1790028000", "type": "signal.detected",
        "symbol": "XPDUSD", "timeframe": "M15", "direction": "BUY",
        "entry_price": 1310.914, "stop_loss": 1306.344,
        "take_profit": 1320.054,
        # Repair round 2 (2026-09-24): the advisory veto is always on, so
        # the live "commanded" path needs a TRADE advisory -- trend-aligned
        # indicator data (RR 2.0).
        "ema_fast": 1311.5, "ema_slow": 1310.0,
        "rsi_value": 55, "atr_value": 2.0,
    }
    s.update(over)
    return s


def make_sandbox(tmp_path, *, trading_enabled=True, dry_run=True,
                 server_time="2026.09.21 20:00:00"):
    files = tmp_path / "files"
    state = tmp_path / "run"
    files.mkdir()
    state.mkdir()
    now = datetime.now().strftime("%Y.%m.%d %H:%M:%S")
    specs = {
        "time": now,
        "account": {"equity": 1_000_000.0, "balance": 1_000_000.0,
                    "currency": "USD", "server": "MetaQuotes-Demo",
                    "time": server_time},
        "symbols": {"XPDUSD": spec()},
    }
    (files / "nova_symbol_specs.json").write_text(json.dumps(specs))
    (files / "nova_signals.jsonl").write_text(
        json.dumps(sig()) + "\n")
    (state / "risk_config.json").write_text(json.dumps({
        "trading_enabled": trading_enabled, "dry_run": dry_run,
        "risk_per_trade_pct": 0.5, "max_concurrent_trades": 3,
        "max_daily_loss_pct": 2.0, "max_spread_points": 50,
        "capital_basis": 200000,
    }))
    (state / "trading_enabled").write_text("1")
    return str(files), str(state)


def read_journal(state_dir):
    p = os.path.join(state_dir, "nova_journal.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


# ---------------------------------------------------------------- risk bounds

def test_risk_bounds_clamp_high():
    # 2026-09-21: risk_per_trade_pct clamped to 1.0 and
    # max_concurrent_trades clamped to the owner-ordered ceiling of 10
    # (repo RiskPolicy says 3; owner overrode to 10).
    cfg, notes = te.apply_risk_bounds({
        "risk_per_trade_pct": 2.5, "max_concurrent_trades": 25,
        "max_daily_loss_pct": 2.0, "max_spread_points": 50,
        "trading_enabled": True, "dry_run": False})
    assert cfg["risk_per_trade_pct"] == 1.0
    assert cfg["max_concurrent_trades"] == 10
    assert len(notes) == 2


def test_risk_bounds_concurrent_clamped_to_owner_max():
    cfg, notes = te.apply_risk_bounds({
        "risk_per_trade_pct": 0.5, "max_concurrent_trades": 100,
        "max_daily_loss_pct": 2.0, "max_spread_points": 50,
        "trading_enabled": True, "dry_run": False})
    assert cfg["max_concurrent_trades"] == 10
    assert len(notes) == 1


def test_risk_bounds_clamp_low():
    cfg, notes = te.apply_risk_bounds({
        "risk_per_trade_pct": 0.01, "max_concurrent_trades": 0,
        "max_daily_loss_pct": 2.0, "max_spread_points": 50,
        "trading_enabled": True, "dry_run": False})
    assert cfg["risk_per_trade_pct"] == 0.05
    assert cfg["max_concurrent_trades"] == te.DEFAULT_CONFIG["max_concurrent_trades"]
    assert len(notes) == 2


def test_risk_bounds_in_range_untouched():
    cfg, notes = te.apply_risk_bounds({
        "risk_per_trade_pct": 0.5, "max_concurrent_trades": 3,
        "max_daily_loss_pct": 2.0, "max_spread_points": 50,
        "trading_enabled": True, "dry_run": False})
    assert notes == []
    assert cfg["risk_per_trade_pct"] == 0.5


def test_risk_bounds_invalid_daily_loss_reset():
    cfg, notes = te.apply_risk_bounds({
        "risk_per_trade_pct": 0.5, "max_concurrent_trades": 3,
        "max_daily_loss_pct": -1.0, "max_spread_points": 50,
        "trading_enabled": True, "dry_run": False})
    assert cfg["max_daily_loss_pct"] == 3.0
    assert notes


def test_load_config_enforces_bounds(tmp_path):
    # 2026-09-21: max_concurrent_trades clamped to owner-ordered ceiling 10.
    p = str(tmp_path / "risk_config.json")
    json.dump({"risk_per_trade_pct": 5.0, "max_concurrent_trades": 99,
               "max_daily_loss_pct": 2.0, "max_spread_points": 50,
               "trading_enabled": False, "dry_run": True},
              open(p, "w"))
    cfg = te.load_config(p)
    assert cfg["risk_per_trade_pct"] == 1.0
    assert cfg["max_concurrent_trades"] == 10


# ---------------------------------------------------------------- market-hours gate

def test_gate_market_closed_live():
    d, _ = te.check_gates(sig(), **gate_ctx(market_open=False))
    assert d == "skipped:market_closed"


def test_gate_market_closed_dry_run_still_intended():
    # dry run creates no positions; it logs intended with full details
    d, flags = te.check_gates(sig(), **gate_ctx(dry_run=True,
                                                 market_open=False))
    assert d == "intended"
    assert flags["volume"] > 0


def test_gate_market_open_commanded():
    d, _ = te.check_gates(sig(), **gate_ctx(market_open=True))
    assert d == "commanded"


def test_server_market_state_weekday_open():
    specs = {"account": {"time": "2026.09.21 15:00:00"}}  # Monday server
    open_, ctx = te.server_market_state(specs)
    assert open_ is True
    assert ctx["weekday"] == "Mon"
    assert ctx["session"] == "newyork"
    assert ctx["server_hour"] == 15


def test_server_market_state_saturday_closed():
    specs = {"account": {"time": "2026.09.26 10:00:00"}}  # Saturday server
    open_, ctx = te.server_market_state(specs)
    assert open_ is False
    assert ctx["weekday"] == "Sat"


# ---------------------------------------------------------------- event journaling

def test_signal_received_journaled(tmp_path):
    files, state = make_sandbox(tmp_path)
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    eng.run_once()
    recvd = [e for e in read_journal(state)
             if e.get("type") == "signal.received"]
    assert len(recvd) == 1
    assert recvd[0]["signal_id"] == sig()["id"]
    assert recvd[0]["direction"] == "BUY"


def test_decision_carries_market_context(tmp_path):
    # Monday 15:00 server = market open, newyork session
    files, state = make_sandbox(tmp_path, server_time="2026.09.21 15:00:00")
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    eng.run_once()
    decs = [e for e in read_journal(state)
            if e.get("type") == "trade.decision"]
    assert len(decs) == 1
    ctx = decs[0]["context"]
    assert ctx["market_open"] is True
    assert ctx["session"] == "newyork"
    assert ctx["weekday"] == "Mon"
    assert ctx["spread_points"] == 20


def test_kill_switch_trip_journaled(tmp_path):
    files, state = make_sandbox(tmp_path, trading_enabled=True, dry_run=False)
    today = datetime.now().strftime("%Y.%m.%d")
    # Monday server time so market gate does not interfere
    with open(os.path.join(files, "nova_trades.jsonl"), "w") as f:
        f.write(json.dumps({
            "type": "trade.closed", "ticket": 1, "exit_price": 1.0,
            "profit": -20001.0, "reason": "sl",
            "time": "2026.09.21 10:00:00"}) + "\n")
    # make server today = 2026-09-21 so the loss counts toward today
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    decisions = eng.run_once()
    assert decisions[0] == "skipped:daily_loss_limit"
    trips = [e for e in read_journal(state)
             if e.get("type") == "kill_switch.tripped"]
    assert len(trips) == 1
    assert trips[0]["reason"] == "daily_loss_limit"
    with open(os.path.join(state, "trading_enabled")) as f:
        assert f.read() == "0"


def test_command_sent_journaled_live(tmp_path):
    files, state = make_sandbox(tmp_path, trading_enabled=True, dry_run=False)
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    eng.run_once()
    sents = [e for e in read_journal(state)
             if e.get("type") == "command.sent"]
    assert len(sents) == 1
    assert sents[0]["command"]["type"] == "trade.open"
    assert sents[0]["command"]["id"] == f"cmd-{sig()['id']}"


def test_market_closed_journaled_live(tmp_path):
    # Saturday 10:00 server -> market closed -> skipped:market_closed
    files, state = make_sandbox(tmp_path, trading_enabled=True, dry_run=False,
                                server_time="2026.09.26 10:00:00")
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    decisions = eng.run_once()
    assert decisions == ["skipped:market_closed"]
    decs = [e for e in read_journal(state)
            if e.get("type") == "trade.decision"]
    assert decs[0]["context"]["market_open"] is False
    cmd_p = os.path.join(files, "nova_commands.jsonl")
    assert os.path.getsize(cmd_p) == 0  # exists (startup touch), zero commands written


def test_config_changed_journaled(tmp_path):
    files, state = make_sandbox(tmp_path)
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    eng.run_once()  # baseline: no journal entry
    assert not [e for e in read_journal(state)
                if e.get("type") == "config.changed"]
    # change a value, run again
    cfg = json.load(open(os.path.join(state, "risk_config.json")))
    cfg["risk_per_trade_pct"] = 0.75
    json.dump(cfg, open(os.path.join(state, "risk_config.json"), "w"))
    eng2 = te.TraderEngine(files_dir=files, state_dir=state)
    eng2.run_once()
    changed = [e for e in read_journal(state)
               if e.get("type") == "config.changed"]
    assert len(changed) == 1
    assert changed[0]["old"]["risk_per_trade_pct"] == 0.5
    assert changed[0]["new"]["risk_per_trade_pct"] == 0.75


def test_trade_opened_closed_full_events_with_hold_time(tmp_path):
    files = tmp_path / "files"
    state = tmp_path / "run"
    files.mkdir()
    state.mkdir()
    # command index so opened links to symbol/direction/volume
    eng = te.TraderEngine(files_dir=str(files), state_dir=str(state))
    eng.state["cmd_index"] = {
        "cmd-S1": {"signal_id": "S1", "symbol": "XPDUSD", "direction": "BUY",
                   "volume": 1.5, "entry_price": 1310.9}}
    eng.save()
    (files / "nova_trades.jsonl").write_text("\n".join([
        json.dumps({"type": "trade.opened", "command_id": "cmd-S1",
                    "ticket": 12345, "deal": 99, "fill_price": 1310.9,
                    "time": "2026.09.21 22:15:00", "signal_id": "S1"}),
        json.dumps({"type": "trade.closed", "ticket": 12345,
                    "exit_price": 1320.0, "profit": 915.0, "reason": "tp",
                    "time": "2026.09.21 23:45:00"}),
    ]) + "\n")
    eng2 = te.TraderEngine(files_dir=str(files), state_dir=str(state))
    assert eng2.tail_trades() == 2
    journal = read_journal(str(state))
    opened = [e for e in journal if e.get("type") == "trade.opened"]
    closed = [e for e in journal if e.get("type") == "trade.closed"]
    assert len(opened) == 1 and len(closed) == 1
    assert opened[0]["symbol"] == "XPDUSD"
    assert opened[0]["direction"] == "BUY"
    assert opened[0]["volume"] == 1.5
    assert closed[0]["entry_price"] == 1310.9
    assert closed[0]["exit_price"] == 1320.0
    assert closed[0]["profit"] == 915.0
    assert closed[0]["hold_seconds"] == 90 * 60  # 22:15 -> 23:45
    assert closed[0]["reason"] == "tp"


def test_commands_file_created_at_startup(tmp_path):
    # Regression test for the 2026-09-21 incident: the EA anchors a missing
    # cursor at EOF on first sight of nova_commands.jsonl, so a commands
    # file created later already holding a command would be skipped. The
    # engine must guarantee the (empty) file exists at startup.
    files, state = make_sandbox(tmp_path)
    cmd_path = os.path.join(files, "nova_commands.jsonl")
    assert not os.path.exists(cmd_path)
    te.TraderEngine(files_dir=files, state_dir=state)
    assert os.path.exists(cmd_path)
    assert os.path.getsize(cmd_path) == 0
    # second init must not truncate an existing file with content
    with open(cmd_path, "w") as f:
        f.write('{"type":"trade.open"}\n')
    te.TraderEngine(files_dir=files, state_dir=state)
    assert os.path.getsize(cmd_path) > 0


def test_trades_seen_survives_json_roundtrip(tmp_path):
    # Regression: dedup keys were tuples; after a JSON round-trip they come
    # back as lists, and set() on a list-of-lists raised
    # "TypeError: unhashable type: 'list'", crash-looping the executor.
    import trade_executor as te
    files, state = make_sandbox(tmp_path)
    # simulate a persisted state as JSON leaves it: key tuple -> list
    st = {"offset": 0, "seen": [], "trades_offset": 0,
          "trades_seen": [["trade.opened", "cmd-X", 111, 222]]}
    json.dump(st, open(os.path.join(state, "trade_executor.state.json"), "w"))
    open(os.path.join(files, "nova_trades.jsonl"), "w").close()  # empty file
    eng2 = te.TraderEngine(files_dir=files, state_dir=state)
    assert eng2.tail_trades() == 0  # must not crash
    # and a re-run stays crash-free with the migrated string keys
    eng3 = te.TraderEngine(files_dir=files, state_dir=state)
    assert eng3.tail_trades() == 0
    st2 = json.load(open(os.path.join(state, "trade_executor.state.json")))
    assert all(isinstance(k, str) for k in st2["trades_seen"])


# ======================================================================
# Worker C (2026-09-23): experiment lifecycle (experiment.py).
# Record creation, update from a fixture journal, completion transition
# -> DEMO_EXPERIMENT_COMPLETE + new-entry halt, and no auto-switch.

import experiment as ex  # noqa: E402
from datetime import timezone  # noqa: E402


def _write_journal(path, events):
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _fixture_journal(path):
    _write_journal(path, [
        {"type": "signal.received", "signal_id": "s1", "time": "2026-09-22 10:00:00"},
        {"type": "trade.decision", "signal_id": "s1", "decision": "commanded",
         "time": "2026-09-22 10:00:01"},
        {"type": "trade.closed", "ticket": 1, "profit": 150.0,
         "time": "2026-09-22 11:00:00"},
        {"type": "trade.closed", "ticket": 2, "profit": -60.0,
         "time": "2026-09-22 12:00:00"},
        {"type": "trade.closed", "ticket": 3, "profit": -40.0,
         "time": "2026-09-22 13:00:00"},
        {"type": "kill_switch.tripped", "reason": "daily_loss_limit",
         "time": "2026-09-22 14:00:00"},
        # LIVE-scoped entry: must never leak into the DEMO record.
        {"type": "trade.closed", "ticket": 9, "profit": 99999.0,
         "mode": "LIVE", "time": "2026-09-22 15:00:00"},
    ])


def test_record_creation_defaults(tmp_path):
    rec = ex.init_record(str(tmp_path))
    assert rec["experiment_id"] == "forex-30day-20260921"
    assert rec["demo_account"] == 112975129
    assert rec["broker"] == "MetaQuotes"
    assert rec["server"] == "MetaQuotes-Demo"
    assert rec["end_time"].startswith("2026-10-21")
    assert rec["status"] == "RUNNING"
    assert rec["trade_halt"] is False
    # init is idempotent: never overwrites an existing record.
    rec["status"] = "MUTATED"
    with open(ex.record_path(str(tmp_path)), "w") as f:
        json.dump(rec, f)
    assert ex.init_record(str(tmp_path))["status"] == "MUTATED"


def test_update_from_fixture_journal(tmp_path):
    jp = os.path.join(str(tmp_path), "nova_journal.jsonl")
    _fixture_journal(jp)
    rec = ex.update_experiment_record(
        str(tmp_path), journal_path=jp,
        now=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc))
    assert rec["status"] == "RUNNING"
    assert rec["number_of_trades"] == 3  # LIVE ticket excluded
    assert rec["wins"] == 1
    assert rec["losses"] == 2
    assert rec["net_profit"] == 50.0  # 150 - 60 - 40; no 99999 leak
    assert rec["maximum_losing_streak"] == 2
    assert rec["kill_switch_events"] == 1
    assert rec["profit_factor"] == pytest.approx(1.5)


def test_completion_transition_halts_new_entries(tmp_path):
    jp = os.path.join(str(tmp_path), "nova_journal.jsonl")
    _fixture_journal(jp)
    rec = ex.update_experiment_record(
        str(tmp_path), journal_path=jp,
        now=datetime(2026, 10, 22, 0, 0, 1, tzinfo=timezone.utc))
    assert rec["status"] == "DEMO_EXPERIMENT_COMPLETE"
    assert rec["trade_halt"] is True
    assert rec["completed_at"] is not None
    assert os.path.exists(ex.halt_path(str(tmp_path)))
    # experiment.completed journaled exactly once.
    types = [json.loads(l).get("type") for l in open(jp)
             if l.strip()]
    assert types.count("experiment.completed") == 1
    # A second update does not re-transition or duplicate the journal.
    rec2 = ex.update_experiment_record(
        str(tmp_path), journal_path=jp,
        now=datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc))
    assert rec2["status"] == "DEMO_EXPERIMENT_COMPLETE"
    types = [json.loads(l).get("type") for l in open(jp)
             if l.strip()]
    assert types.count("experiment.completed") == 1


def test_halt_blocks_new_entries_no_auto_switch(tmp_path):
    import trade_executor as te
    run = str(tmp_path / "run")
    os.makedirs(run)
    jp = os.path.join(run, "nova_journal.jsonl")
    _write_journal(jp, [])
    # Complete the experiment -> halt raised.
    ex.update_experiment_record(
        run, journal_path=jp,
        now=datetime(2026, 10, 22, 0, 0, 1, tzinfo=timezone.utc))
    files = str(tmp_path / "files")
    os.makedirs(files)
    eng = te.TraderEngine(files_dir=files, state_dir=run)
    cfg = dict(te.DEFAULT_CONFIG)
    cfg.update({"trading_enabled": True, "dry_run": False,
                "capital_basis": 200000.0})
    s = {"id": "X1", "type": "signal.detected", "symbol": "XPDUSD",
         "timeframe": "M15", "direction": "BUY", "entry_price": 1310.914,
         "stop_loss": 1306.344, "take_profit": 1320.054}
    specs = {"symbols": {"XPDUSD": {
        "tick_value": 1.0, "tick_size": 0.001, "volume_min": 0.01,
        "volume_max": 100.0, "volume_step": 0.01,
        "stops_level_points": 0, "spread_points": 20, "digits": 3,
        "point": 0.001}},
        "account": {"server": "MetaQuotes-Demo", "equity": 200000.0,
                    "balance": 200000.0}}
    d = eng.handle_signal(s, cfg, True, specs, True, {}, 0.0,
                          "2026-10-22", market_open=True)
    assert d == "skipped:experiment_complete"
    # No command written despite an otherwise eligible signal.
    cmds = os.path.join(files, "nova_commands.jsonl")
    assert os.path.getsize(cmds) == 0
    # No LIVE switch anywhere: mode untouched, record still DEMO-scoped.
    rec = ex.init_record(run)
    assert rec["status"] == "DEMO_EXPERIMENT_COMPLETE"
    assert "LIVE" not in json.dumps(rec)
    journal_modes = {json.loads(l).get("mode") for l in open(jp)
                     if l.strip()} - {None}
    assert not journal_modes
    # Explicit operator action clears the halt (and only the halt).
    ex.resume_trading(run)
    assert ex.init_record(run)["trade_halt"] is False
    assert not os.path.exists(ex.halt_path(run))
