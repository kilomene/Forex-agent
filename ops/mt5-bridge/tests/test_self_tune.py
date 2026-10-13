"""Hermetic unit tests for self_tune.py (owner order 2026-10-13).

The self-tune loop may only ever change allowlisted config keys inside
hard bounds. These tests pin the invariants: it can never enable trading,
never leave dry-run, never raise risk above the owner ceiling, never touch
immutable keys, and blocked_symbols is add-only.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import self_tune as st


def write_journal(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def closed(ticket, symbol, pnl, when=None, direction="BUY"):
    return {"type": "trade.closed", "ticket": ticket, "symbol": symbol,
            "direction": direction, "net_profit": pnl,
            "time": (when or datetime.now(timezone.utc)).strftime("%Y.%m.%d %H:%M:%S")}


def sandbox(tmp_path, events, cfg=None):
    state = tmp_path / "run"
    files = tmp_path / "files"
    state.mkdir()
    files.mkdir()
    jp = state / "nova_journal.jsonl"
    write_journal(str(jp), events)
    base_cfg = {"trading_enabled": True, "dry_run": False,
                "risk_per_trade_pct": 0.05, "max_concurrent_trades": 10,
                "max_daily_loss_pct": 3.0, "blocked_symbols": []}
    base_cfg.update(cfg or {})
    (state / "risk_config.json").write_text(json.dumps(base_cfg))
    (state / "trading_enabled").write_text("1")
    return str(state), str(files), str(jp)


# --- analysis -----------------------------------------------------------

def test_toxic_symbol_proposed(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [closed(i, "CHFJPY", -250.0,
                  when=now - timedelta(days=1)) for i in range(16)]
    state, files, jp = sandbox(tmp_path, evs)
    rep = st.run(state, files, live=False, now=now)
    keys = [p["key"] for p in rep["proposals"]]
    assert "blocked_symbols" in keys
    prop = next(p for p in rep["proposals"] if p["key"] == "blocked_symbols")
    assert "CHFJPY" in prop["new"]
    # advisory: config untouched
    cfg = json.loads((tmp_path / "run" / "risk_config.json").read_text())
    assert cfg["blocked_symbols"] == []


def test_healthy_symbol_not_excluded(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [closed(i, "EURUSD", 150.0 if i % 2 == 0 else -80.0,
                  when=now - timedelta(days=1)) for i in range(16)]
    state, files, jp = sandbox(tmp_path, evs)
    rep = st.run(state, files, live=False, now=now)
    assert not [p for p in rep["proposals"] if p["key"] == "blocked_symbols"]


def test_risk_scales_down_on_bleed(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [closed(i, "EURUSD", -150.0, when=now - timedelta(days=1))
           for i in range(20)]
    state, files, jp = sandbox(tmp_path, evs)
    rep = st.run(state, files, live=True, now=now)
    cfg = json.loads((tmp_path / "run" / "risk_config.json").read_text())
    assert cfg["risk_per_trade_pct"] == pytest.approx(0.025)
    assert "risk_per_trade_pct" in rep["applied"]


def test_risk_scales_up_bounded_by_ceiling(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [closed(i, "EURUSD", 120.0, when=now - timedelta(days=1))
           for i in range(20)]
    state, files, jp = sandbox(tmp_path, evs,
                               cfg={"risk_per_trade_pct": 0.04})
    rep = st.run(state, files, live=True, now=now)
    cfg = json.loads((tmp_path / "run" / "risk_config.json").read_text())
    assert cfg["risk_per_trade_pct"] == pytest.approx(0.05)
    # never above the owner ceiling even if it keeps winning
    rep2 = st.run(state, files, live=True, now=now)
    cfg2 = json.loads((tmp_path / "run" / "risk_config.json").read_text())
    assert cfg2["risk_per_trade_pct"] == pytest.approx(0.05)


# --- hard invariants ----------------------------------------------------

def test_validate_rejects_enable_trading():
    ok, rej = st.validate(
        [{"key": "trading_enabled", "old": False, "new": True}], {})
    assert ok == [] and len(rej) == 1


def test_validate_rejects_dry_run_off():
    ok, rej = st.validate(
        [{"key": "dry_run", "old": True, "new": False}], {})
    assert ok == [] and len(rej) == 1


def test_validate_rejects_risk_above_ceiling():
    ok, rej = st.validate(
        [{"key": "risk_per_trade_pct", "old": 0.05, "new": 0.5}],
        {"risk_per_trade_pct": 0.05})
    assert ok == [] and len(rej) == 1


def test_validate_rejects_immutable_and_unknown_keys():
    for key in ("max_daily_loss_pct", "require_stop_loss", "capital_basis",
                "kill_switch", "magic_number"):
        ok, rej = st.validate([{"key": key, "old": 1, "new": 2}], {})
        assert ok == [] and len(rej) == 1, key


def test_validate_blocked_symbols_add_only():
    ok, rej = st.validate(
        [{"key": "blocked_symbols", "old": ["CHFJPY"], "new": []}],
        {"blocked_symbols": ["CHFJPY"]})
    assert ok == [] and len(rej) == 1
    ok, _ = st.validate(
        [{"key": "blocked_symbols", "old": ["CHFJPY"],
          "new": ["CHFJPY", "GBPAUD"]}],
        {"blocked_symbols": ["CHFJPY"]})
    assert len(ok) == 1


def test_validate_bounds():
    ok, rej = st.validate(
        [{"key": "profit_plunge_keep_pct", "old": 0.5, "new": 0.95}], {})
    assert ok == [] and len(rej) == 1
    ok, _ = st.validate(
        [{"key": "profit_plunge_keep_pct", "old": 0.5, "new": 0.6}], {})
    assert len(ok) == 1


# --- security sweep -----------------------------------------------------

def test_unknown_magic_alerted(tmp_path):
    now = datetime.now(timezone.utc)
    state, files, jp = sandbox(tmp_path, [])
    (tmp_path / "files" / "nova_positions.json").write_text(json.dumps({
        "positions": [{"ticket": 1, "symbol": "EURUSD", "magic": 99999}]}))
    findings = st.security_sweep(jp, [{"ticket": 1, "symbol": "EURUSD",
                                       "magic": 99999}], now=now)
    assert any(f["kind"] == "unknown_magic" and f["action"] == "alert"
               for f in findings)


def test_identity_mismatch_burst_trips_kill(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [{"type": "account.identity_mismatch",
            "time": (now - timedelta(hours=i)).strftime("%Y.%m.%d %H:%M:%S")}
           for i in range(3)]
    state, files, jp = sandbox(tmp_path, evs)
    rep = st.run(state, files, live=True, now=now)
    kill = [f for f in rep["security_findings"]
            if f["kind"] == "identity_mismatch_burst"]
    assert kill and kill[0]["action"] == "kill"
    assert (tmp_path / "run" / "trading_enabled").read_text() == "0"


def test_no_false_kill_on_single_mismatch(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [{"type": "account.identity_mismatch",
            "time": now.strftime("%Y.%m.%d %H:%M:%S")}]
    state, files, jp = sandbox(tmp_path, evs)
    rep = st.run(state, files, live=True, now=now)
    assert (tmp_path / "run" / "trading_enabled").read_text() == "1"


# --- rollback -----------------------------------------------------------

def test_rollback_reverts_degraded_change(tmp_path):
    now = datetime.now(timezone.utc)
    # first run: bleeding window -> risk halved, snapshot records baseline
    evs = [closed(i, "EURUSD", -150.0, when=now - timedelta(days=2))
           for i in range(20)]
    state, files, jp = sandbox(tmp_path, evs)
    st.run(state, files, live=True, now=now)
    cfg = json.loads((tmp_path / "run" / "risk_config.json").read_text())
    assert cfg["risk_per_trade_pct"] == pytest.approx(0.025)
    # second run: even worse window -> expectancy clearly degraded -> revert
    evs2 = evs + [closed(100 + i, "EURUSD", -400.0,
                         when=now - timedelta(hours=1)) for i in range(20)]
    write_journal(jp, evs2)
    rep = st.run(state, files, live=True, now=now)
    assert rep["rollback"] is not None
    cfg2 = json.loads((tmp_path / "run" / "risk_config.json").read_text())
    assert cfg2["risk_per_trade_pct"] == pytest.approx(0.05)
