#!/usr/bin/env python3
"""Wired replay harness for staging (gate b) and shadow canary (gate c).

The candidate tree and the current tree are each evaluated in an
isolated subprocess that imports THAT tree's trade_executor and calls
the real check_gates() on real journaled signals. No bridge modules are
imported in this process.

Replay simplifications (documented, identical for both trees):
* max_signal_age_hours=0 -- staleness would blank every old signal to
  skipped:stale_signal and prove nothing. The staleness gate itself is
  covered by unit tests.
* specs_fresh=True -- freshness is time-dependent; unit-tested.
* open_positions=[] and todays_profit=0.0 -- position-state-dependent
  behavior is out of scope for the replay; the divergence report flags
  any decision difference so a human can judge whether position state
  explains it.
* enforce_mode_checks=False, mode="DEMO" -- mirrors the halted demo
  engine's legacy path.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from . import lib

# The subprocess runner: imports the tree's own trade_executor and runs
# the real check_gates over inputs.jsonl. stdlib only.
RUNNER_SOURCE = r'''
import sys, json, os
tree_dir, inputs_path, outputs_path, cfg_path, specs_path = sys.argv[1:6]
sys.path.insert(0, tree_dir)
import trade_executor
cfg = json.load(open(cfg_path))
specs = json.load(open(specs_path))
equity = ((specs.get("account") or {}).get("equity") or 0)
balance = ((specs.get("account") or {}).get("balance") or 0)
try:
    risk_basis = trade_executor.effective_risk_basis(cfg, equity)
except Exception:
    risk_basis = None
out = open(outputs_path, "w")
for line in open(inputs_path):
    line = line.strip()
    if not line:
        continue
    item = json.loads(line)
    sig = item["signal"]
    ctx = item.get("context") or {}
    kw = dict(
        enabled=item.get("enabled", False),
        dry_run=bool(cfg.get("dry_run", True)),
        specs=specs,
        specs_fresh=True,
        open_positions=[],
        todays_profit=0.0,
        today_str=item.get("today_str"),
        risk_pct=cfg.get("risk_per_trade_pct", 0.05),
        max_concurrent=cfg.get("max_concurrent_trades", 10),
        in_flight_count=0,
        max_spread_points=cfg.get("max_spread_points", 50),
        max_daily_loss_pct=cfg.get("max_daily_loss_pct", 3.0),
        equity=equity,
        balance=balance,
        risk_basis=risk_basis,
        max_total_exposure_lots=cfg.get("max_total_exposure_lots", 0),
        open_lots=0.0,
        max_consecutive_losses=cfg.get("max_consecutive_losses", 0),
        consecutive_losses=0,
        max_correlated_positions=cfg.get("max_correlated_positions", 0),
        require_stop_loss=bool(cfg.get("require_stop_loss", True)),
        market_open=bool(ctx.get("market_open", True)),
        tick_age_s=None,
        enforce_correlation_gate=bool(cfg.get("enforce_correlation_gate", False)),
        enforce_spread_gate=bool(cfg.get("enforce_spread_gate", False)),
        enforce_single_position_per_symbol=bool(
            cfg.get("enforce_single_position_per_symbol", False)),
        enforce_consecutive_losses_gate=bool(
            cfg.get("enforce_consecutive_losses_gate", False)),
        mode="DEMO",
        enforce_mode_checks=False,
        price_reference=sig.get("entry_price"),
        tick_value_overrides=cfg.get("tick_value_overrides", {}),
        blocked_symbols=cfg.get("blocked_symbols", []),
        max_signal_age_hours=0,
    )
    try:
        decision, flags = trade_executor.check_gates(sig, **kw)
        rec = {"signal_id": sig.get("id"), "decision": decision,
               "flags": sorted((flags or {}).keys())}
    except Exception as e:  # noqa: BLE001 -- a crash IS the finding
        rec = {"signal_id": sig.get("id"), "decision": "ERROR",
               "error": repr(e)[:500]}
    out.write(json.dumps(rec) + "\n")
out.close()
print("replayed ok")
'''


def collect_signals(journal_path, days=14, cap=2000):
    """Return [(signal_dict, decision_context)] for recent signals.

    signal_dict is the journaled signal.received event (normalized to
    the shape check_gates expects: it must carry id/symbol/direction).
    decision_context comes from the matching journaled trade.decision.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    signals = []
    decisions = {}
    for ev in lib.iter_jsonl(journal_path):
        t = ev.get("type")
        if t == "signal.received":
            signals.append(ev)
        elif t == "trade.decision" and ev.get("signal_id"):
            decisions[ev["signal_id"]] = ev
    recent = []
    for ev in signals:
        ts = _parse_ts(ev.get("time"))
        if ts and ts < cutoff:
            continue
        recent.append(ev)
    recent = recent[-cap:]
    out = []
    for ev in recent:
        sig = {
            "id": ev.get("signal_id") or ev.get("id"),
            "symbol": ev.get("symbol"),
            "direction": ev.get("direction"),
            "timeframe": ev.get("timeframe"),
            "entry_price": ev.get("entry_price"),
            "candle_time": ev.get("candle_time"),
        }
        dec = decisions.get(sig["id"]) or {}
        ctx = dec.get("context") or {}
        out.append((sig, {
            "market_open": ctx.get("market_open", True),
            "session": ctx.get("session"),
            "journaled_decision": dec.get("decision"),
        }))
    return out


def _parse_ts(s):
    if not s:
        return None
    s = str(s).strip().replace(".", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def write_inputs(path, signals, *, enabled, today_str):
    n = 0
    with open(path, "w") as f:
        for sig, ctx in signals:
            f.write(json.dumps({
                "signal": sig,
                "context": {"market_open": ctx.get("market_open", True),
                            "session": ctx.get("session")},
                "enabled": enabled,
                "today_str": today_str,
                "journaled_decision": ctx.get("journaled_decision"),
            }) + "\n")
            n += 1
    return n


def run_tree(tree_dir, inputs_path, outputs_path, cfg_path, specs_path,
             timeout=1200):
    """Evaluate one tree over the inputs. Returns the runner's records."""
    runner_path = os.path.join(os.path.dirname(outputs_path),
                               "_replay_runner.py")
    with open(runner_path, "w") as f:
        f.write(RUNNER_SOURCE)
    rc, out, err = lib.run(
        [sys.executable, runner_path, tree_dir, inputs_path,
         outputs_path, cfg_path, specs_path],
        cwd=tree_dir, timeout=timeout)
    if rc != 0 or not os.path.exists(outputs_path):
        raise lib.PipelineHalt(
            f"replay runner failed in {tree_dir} (rc={rc}): "
            f"{(out + err)[-2000:]}")
    recs = []
    for line in open(outputs_path):
        line = line.strip()
        if line:
            recs.append(json.loads(line))
    return recs


def build_signal_ticket_map(journal_path):
    """signal_id -> ticket from trade.opened events."""
    m = {}
    for ev in lib.iter_jsonl(journal_path):
        if ev.get("type") == "trade.opened" and ev.get("signal_id") \
                and ev.get("ticket"):
            m[ev["signal_id"]] = ev["ticket"]
    return m


def build_ticket_pnl_map(journal_path):
    """ticket -> effective P&L using performance.dedupe_closes.

    Runs in a subprocess against the live tree so this module stays
    import-clean.
    """
    code = (
        "import sys, json; sys.path.insert(0, %r); "
        "import performance; "
        "journal=[json.loads(l) for l in open(%r) if l.strip()]; "
        "closes=[e for e in journal if e.get('type')=='trade.closed']; "
        "json.dump({str(e.get('ticket')): performance.effective_profit(e) "
        "for e in performance.dedupe_closes(closes) if e.get('ticket')}, "
        "open(%r,'w'))"
    )
    out_path = journal_path + ".ticket_pnl.json"
    rc, o, e = lib.run(
        [sys.executable, "-c",
         code % (lib.bridge_root(), journal_path, out_path)],
        timeout=300)
    if rc != 0:
        raise lib.PipelineHalt(f"ticket pnl map failed: {(o+e)[-1000:]}")
    data = lib.read_json(out_path, {})
    try:
        os.remove(out_path)
    except OSError:
        pass
    return {k: float(v) for k, v in data.items()}
