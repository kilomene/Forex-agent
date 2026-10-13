"""Racheal's adversarial suite for self_tune.py (engagement 006).

Zero-trust: these tests use PRODUCTION data formats (the executor's
now_str() dashed timestamps, real journal shapes), not the formats the
feature's own tests assume. Each test documents a finding.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import self_tune as st


def dashed(dt):
    # EXACTLY what trade_executor.now_str() writes into the journal:
    # "%Y-%m-%d %H:%M:%S", UTC by construction.
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def dotted(dt):
    return dt.strftime("%Y.%m.%d %H:%M:%S")


def write_journal(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def closed(ticket, symbol, pnl, when, direction="BUY"):
    return {"type": "trade.closed", "ticket": ticket, "symbol": symbol,
            "direction": direction, "net_profit": pnl, "time": dotted(when)}


def sandbox(tmp_path, events, cfg=None, kill="1"):
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
    (state / "trading_enabled").write_text(kill)
    return str(state), str(files), str(jp)


def journal_types(jp):
    out = []
    for line in open(jp, encoding="utf-8", errors="replace"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line).get("type"))
            except ValueError:
                pass
    return out


# --- FINDING 1 (CRITICAL): the identity-mismatch kill trip is dead on
# real journal data. The executor journals account.identity_mismatch with
# now_str() -> "2026-10-13 17:25:27" (DASHED). _parse_mt5_time only parses
# dotted MT5 format -> ts None -> never counted -> kill never trips.

def test_kill_trips_on_real_executor_time_format(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [{"type": "account.identity_mismatch",
            "time": dashed(now - timedelta(hours=i))}
           for i in range(3)]
    state, files, jp = sandbox(tmp_path, evs)
    rep = st.run(state, files, live=True, now=now)
    kill = [f for f in rep["security_findings"]
            if f["kind"] == "identity_mismatch_burst"]
    assert kill and kill[0]["action"] == "kill", \
        "3 real-format mismatches must trip the kill switch"
    assert (tmp_path / "run" / "trading_enabled").read_text() == "0"
    assert "kill_switch.tripped" in journal_types(jp)


def test_old_mismatches_do_not_trip(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [{"type": "account.identity_mismatch",
            "time": dashed(now - timedelta(hours=25 + i))}
           for i in range(5)]
    state, files, jp = sandbox(tmp_path, evs)
    rep = st.run(state, files, live=True, now=now)
    assert not [f for f in rep["security_findings"]
                if f["kind"] == "identity_mismatch_burst"]
    assert (tmp_path / "run" / "trading_enabled").read_text() == "1"


# --- FINDING 2: non-string symbol in journal -> sorted() TypeError ->
# every future run crashes (the bad line is never consumed).

def test_malformed_symbol_does_not_crash_loop(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [closed(1, "EURUSD", 50.0, now - timedelta(days=1)),
           {"type": "trade.closed", "ticket": 2, "symbol": {"weird": 1},
            "direction": "BUY", "net_profit": -50.0,
            "time": dotted(now - timedelta(days=1))},
           {"type": "trade.closed", "ticket": 3, "symbol": ["X"],
            "direction": "BUY", "net_profit": -50.0,
            "time": dotted(now - timedelta(days=1))}]
    state, files, jp = sandbox(tmp_path, evs)
    rep = st.run(state, files, live=False, now=now)  # must not raise
    assert rep["mode"] == "advisory"


# --- FINDING 3: validate() must require blocked_symbols `new` to be a
# list. A bare string passes today (chars are strings) and turns the
# executor's `symbol in blocked` membership test into a substring test.

def test_validate_rejects_non_list_blocked_symbols():
    ok, rej = st.validate(
        [{"key": "blocked_symbols", "old": [], "new": "CHFJPY"}],
        {"blocked_symbols": []})
    assert ok == [] and len(rej) == 1


def test_validate_rejects_nan_inf_risk():
    for bad in (float("nan"), float("inf"), "nan"):
        ok, rej = st.validate(
            [{"key": "risk_per_trade_pct", "old": 0.05, "new": bad}],
            {"risk_per_trade_pct": 0.05})
        assert ok == [] and len(rej) == 1, bad


# --- FINDING 4: live mode rewrites risk_config.json and journals
# config.changed even when nothing was applied (mtime churn + noise).

def test_live_noop_writes_nothing(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [closed(i, "EURUSD", 50.0, now - timedelta(days=1))
           for i in range(5)]
    state, files, jp = sandbox(tmp_path, evs)
    cfg_path = tmp_path / "run" / "risk_config.json"
    before = cfg_path.read_bytes()
    rep = st.run(state, files, live=True, now=now)
    assert rep["applied"] == []
    assert cfg_path.read_bytes() == before, "no-op run must not rewrite config"
    assert "config.changed" not in journal_types(jp)


# --- FINDING 5: missing/corrupt config in live mode must fail closed
# (refuse to write), not fabricate a minimal config.

def test_live_refuses_missing_config(tmp_path):
    now = datetime.now(timezone.utc)
    state = tmp_path / "run"
    files = tmp_path / "files"
    state.mkdir()
    files.mkdir()
    jp = state / "nova_journal.jsonl"
    evs = [closed(i, "CHFJPY", -250.0, now - timedelta(days=1))
           for i in range(16)]
    write_journal(str(jp), evs)
    (state / "trading_enabled").write_text("1")
    # NO risk_config.json at all
    rep = st.run(str(state), str(files), live=True, now=now)
    assert not (state / "risk_config.json").exists(), \
        "must not fabricate a config when none could be read"
    assert rep["applied"] == []
    types = journal_types(str(jp))
    assert "self_tune.aborted" in types


# --- FINDING 6: advisory mode must change nothing on disk except the
# journal (which lives in state_dir).

def test_advisory_changes_only_journal(tmp_path):
    now = datetime.now(timezone.utc)
    evs = [closed(i, "CHFJPY", -250.0, now - timedelta(days=1))
           for i in range(16)]
    state, files, jp = sandbox(tmp_path, evs)
    (tmp_path / "files" / "nova_positions.json").write_text(json.dumps({
        "positions": [{"ticket": 9, "symbol": "EURUSD", "magic": 99999}]}))

    def snapshot(root):
        snap = {}
        for dp, _, fns in os.walk(root):
            for fn in fns:
                p = os.path.join(dp, fn)
                with open(p, "rb") as f:
                    snap[os.path.relpath(p, root)] = f.read()
        return snap

    before_state = snapshot(str(state))
    before_files = snapshot(str(files))
    st.run(state, files, live=False, now=now)
    after_state = snapshot(str(state))
    after_files = snapshot(str(files))
    assert before_files == after_files
    changed = [k for k in after_state
               if before_state.get(k) != after_state[k]]
    assert changed == ["nova_journal.jsonl"], changed


# --- live-mode invariant end-to-end: even with a poisoned journal, a
# live run can only ever change allowlisted keys inside hard bounds.

def test_live_poisoned_journal_cannot_escalate(tmp_path):
    now = datetime.now(timezone.utc)
    evs = []
    for i in range(16):  # toxic symbol via real path
        evs.append(closed(100 + i, "POISON", -500.0,
                          now - timedelta(days=1)))
    # junk the journal cannot smuggle keys: propose() only emits two keys
    state, files, jp = sandbox(tmp_path, evs)
    cfg_before = json.loads((tmp_path / "run" / "risk_config.json").read_text())
    rep = st.run(state, files, live=True, now=now)
    cfg_after = json.loads((tmp_path / "run" / "risk_config.json").read_text())
    for k, v in cfg_after.items():
        if k not in cfg_before:
            assert False, f"new key introduced: {k}"
        if v != cfg_before[k]:
            assert k in st.ALLOWLIST, f"non-allowlisted key changed: {k}"
            if k != "blocked_symbols":
                lo, hi = st.ALLOWLIST[k]
                assert lo <= float(v) <= hi, f"{k}={v} out of bounds"
    assert cfg_after["trading_enabled"] is True      # untouched
    assert cfg_after["dry_run"] is False              # untouched
    assert cfg_after["risk_per_trade_pct"] <= 0.05
    assert cfg_after["max_daily_loss_pct"] == 3.0     # untouched
    assert "POISON" in cfg_after["blocked_symbols"]   # intended effect only
