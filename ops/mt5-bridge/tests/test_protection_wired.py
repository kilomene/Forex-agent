"""Wired end-to-end parity: the real TraderEngine signal path.

Phase 2 build-quality directive (Zenas, 2026-10-18): function-level parity
alone does not finish the job. This module drives the REAL engine path --

    handle_signal -> decide -> check_gates -> trade.decision journaling
    (+ kill-switch trips, AI advisory, command emission)

-- in two identical fresh sandboxes: once with the refactored
check_gates (protections.py filters), once with the frozen legacy
implementation monkeypatched in. The journaled event streams (decision
strings, skip reasons, flags), the kill-switch file, and the commands
file must be IDENTICAL.

Normalization: journal "time" stamps are wall-clock (stripped);
command.sent entries carry a uuid idempotency_key and a sent_at stamp
(both normalized to placeholders).

Mode/integrity gates (M0-M6) are covered at function level in
test_protection_parity.py: the wired path resolves enforce_mode_checks
from the on-disk mode record (DEMO -> off), exactly like the live
executor, so M-gates are not reachable through handle_signal here.
"""

import json
import os
import sys
from datetime import datetime, timezone

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trade_executor as te  # noqa: E402
from _legacy_gates import check_gates_legacy  # noqa: E402


# ---------------------------------------------------------------- builders

def fresh_candle():
    return datetime.now(timezone.utc).strftime("%Y.%m.%d %H:%M:%S")


def wired_spec(**over):
    s = {
        "tick_value": 1.0,
        "tick_size": 0.00001,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "stops_level_points": 0,
        "freeze_level_points": 0,
        "spread_points": 10,
        "digits": 5,
        "point": 0.00001,
    }
    s.update(over)
    return s


def wired_specs(symbols=("EURUSD", "GBPUSD"), account=None, **spec_over):
    acct = {
        "server": "MetaQuotes-Demo",
        "currency": "USD",
        "equity": 200000.0,
        "balance": 200000.0,
        "time": fresh_candle(),
    }
    if account:
        acct.update(account)
    return {
        "symbols": {s: wired_spec(**spec_over) for s in symbols},
        "account": acct,
    }


def wired_sig(name, **over):
    s = {
        "id": "wired-%s" % name,
        "type": "signal.detected",
        "symbol": "EURUSD",
        "timeframe": "M15",
        "direction": "BUY",
        "entry_price": 1.1000,
        "stop_loss": 1.0950,
        "take_profit": 1.1100,
        "candle_time": fresh_candle(),
    }
    s.update(over)
    return s


# Trend-aligned indicators (same pattern as test_trader FIXTURE_SIGNALS):
# the AI advisory returns a validated TRADE for these, so the live
# "commanded" case exercises the real send path.
TRADE_INDICATORS = {
    "ema_fast": 1.1010, "ema_slow": 1.0990,
    "rsi_value": 55, "atr_value": 0.002,
}


def wired_cfg(**over):
    c = {
        "trading_enabled": True,
        "dry_run": True,
        "risk_per_trade_pct": 0.05,
        "max_concurrent_trades": 10,
        "max_daily_loss_pct": 3.0,
        "max_spread_points": 50,
        "max_total_exposure_lots": 0,
        "max_consecutive_losses": 0,
        "max_correlated_positions": 0,
        "require_stop_loss": True,
        "enforce_correlation_gate": False,
        "enforce_spread_gate": False,
        "enforce_single_position_per_symbol": False,
        "enforce_consecutive_losses_gate": False,
        "emergency_drawdown_pct": 3.0,
        "tick_value_overrides": {},
        "blocked_symbols": [],
        "max_signal_age_hours": 2.0,
        "capital_basis": 200000,
    }
    c.update(over)
    return c


# ---------------------------------------------------------------- battery
# (name, sig_overrides, cfg_overrides, engine_kw_overrides, specs)

def _ten_positions():
    return {("wt%d" % i): {"symbol": "XAUUSD", "direction": "BUY"}
            for i in range(10)}


WIRED_BATTERY = [
    ("intended", {}, {}, {}, None),
    ("trading_disabled", {}, {"trading_enabled": False}, {}, None),
    ("stale_signal", {"candle_time": "2026.09.23 00:30:00"}, {}, {}, None),
    ("symbol_blocked", {}, {"blocked_symbols": ["EURUSD"]}, {}, None),
    ("risk_basis_unavailable", {}, {"capital_basis": 0}, {}, None),
    ("stale_specs_fresh_false", {}, {}, {"specs_fresh": False}, None),
    ("bad_specs", {}, {}, {}, wired_specs(tick_value=0)),
    ("exposure_clamp", {}, {"max_total_exposure_lots": 0.15}, {}, None),
    ("stale_tick", {}, {}, {"tick_age_s": 999}, None),
    ("market_closed", {}, {}, {"market_open": False}, None),
    ("non_demo_server", {}, {}, {},
     wired_specs(account={"server": "MetaQuotes-Real"})),
    ("max_concurrent", {}, {}, {"open_positions": _ten_positions()}, None),
    ("symbol_already_open",
     {}, {"enforce_single_position_per_symbol": True},
     {"open_positions": {"wt0": {"symbol": "EURUSD", "direction": "BUY"}}},
     None),
    ("spread_too_wide", {}, {"enforce_spread_gate": True}, {},
     wired_specs(spread_points=60)),
    ("consecutive_losses",
     {}, {"enforce_consecutive_losses_gate": True,
           "max_consecutive_losses": 3},
     {"consecutive_losses": 3}, None),
    ("correlated_position",
     {"symbol": "GBPUSD"}, {"enforce_correlation_gate": True,
                             "max_correlated_positions": 1},
     {"open_positions": {"wt0": {"symbol": "EURUSD", "direction": "BUY"}}},
     None),
    ("daily_loss_limit", {}, {"dry_run": False}, {"todays_profit": -6000.0},
     None),
    ("floating_drawdown", {}, {}, {},
     wired_specs(account={"equity": 197000.0, "balance": 200000.0})),
    ("missing_sl_tp", {"take_profit": None}, {}, {}, None),
    ("invalid_sl", {"stop_loss": 1.1050}, {}, {}, None),
    ("commanded_live", dict(TRADE_INDICATORS), {"dry_run": False}, {}, None),
]

# ---------------------------------------------------------------- runner

def read_journal(state_dir):
    p = os.path.join(state_dir, "nova_journal.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p) if l.strip()]


def norm_journal(entries):
    out = []
    for e in entries:
        e = dict(e)
        e.pop("time", None)  # wall-clock; identical inputs, different instants
        if e.get("type") == "command.sent":
            cmd = dict(e.get("command") or {})
            cmd["idempotency_key"] = "<uuid>"  # uuid4 per emission
            cmd["sent_at"] = "<ts>"            # wall-clock
            e["command"] = cmd
        out.append(e)
    return out


def norm_commands(files_dir):
    p = os.path.join(files_dir, "nova_commands.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    for l in open(p):
        if not l.strip():
            continue
        cmd = json.loads(l)
        cmd["idempotency_key"] = "<uuid>"
        cmd["sent_at"] = "<ts>"
        out.append(cmd)
    return out


def run_case(root, tag, name, sig_over, cfg_over, eng_over, specs):
    files = os.path.join(root, "files_" + tag)
    state = os.path.join(root, "state_" + tag)
    os.makedirs(files, exist_ok=True)
    os.makedirs(state, exist_ok=True)
    eng = te.TraderEngine(files_dir=files, state_dir=state)
    sig = wired_sig(name, **sig_over)
    cfg = wired_cfg(**cfg_over)
    specs = wired_specs() if specs is None else specs
    kw = dict(open_positions={}, todays_profit=0.0, market_open=True,
              open_lots=0.0, consecutive_losses=0, tick_age_s=None,
              specs_fresh=True, context={"wired_case": name})
    kw.update(eng_over)
    decision = eng.handle_signal(
        sig, cfg, True, specs, kw.pop("specs_fresh"), kw["open_positions"],
        kw["todays_profit"], "2026-10-18", market_open=kw["market_open"],
        context=kw["context"], open_lots=kw["open_lots"],
        consecutive_losses=kw["consecutive_losses"],
        tick_age_s=kw["tick_age_s"])
    kill_path = os.path.join(state, "trading_enabled")
    kill = open(kill_path).read() if os.path.exists(kill_path) else None
    return {
        "decision": decision,
        "journal": norm_journal(read_journal(state)),
        "kill": kill,
        "commands": norm_commands(files),
    }


@pytest.mark.parametrize("case", [c[0] for c in WIRED_BATTERY])
def test_wired_parity(tmp_path, monkeypatch, case):
    (name, sig_over, cfg_over, eng_over, specs) = next(
        c for c in WIRED_BATTERY if c[0] == case)
    root = str(tmp_path)
    res_new = run_case(root, "new", name, sig_over, cfg_over, eng_over, specs)
    monkeypatch.setattr(te, "check_gates", check_gates_legacy)
    res_leg = run_case(root, "leg", name, sig_over, cfg_over, eng_over, specs)
    assert res_new == res_leg, (
        "WIRED PARITY FAILURE on case %r:\n  refactored=%r\n  legacy=%r"
        % (name, res_new, res_leg))
