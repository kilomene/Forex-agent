#!/usr/bin/env python3
"""Gate (b): STAGING VALIDATION, wired end-to-end.

The candidate tree and the pristine current tree each run the REAL
check_gates() over REAL journaled signals in isolated subprocesses.

* change_class == "refactor": the two decision sequences must be
  byte-identical (decision + flags). Any divergence rejects.
* change_class == "behavior": the candidate's decisions are scored by
  attributable outcomes -- signal -> trade.opened ticket ->
  deduped trade.closed P&L via performance.dedupe_closes -- against the
  journaled current outcomes. Real-money standard: the candidate must
  not lose more in total, must not regress any risk-adjusted metric
  beyond tolerance, and must strictly improve at least one of
  {Sharpe, profit factor, max drawdown}. Fewer than MIN_ATTRIBUTABLE
  attributable trades, or attribution coverage below
  MIN_ATTRIBUTION_COVERAGE, means insufficient evidence -> reject.

"doesn't lose much" fails: candidate_total >= current_total is a hard
requirement, not a vibe.
"""

import json
import os
import sys
from datetime import datetime

from . import lib
from . import replay

MIN_ATTRIBUTABLE = 30
MIN_ATTRIBUTION_COVERAGE = 0.50
REGRESSION_TOL = 0.02  # 2% relative tolerance on no-regression metrics


def _specs_path(bridge):
    # The EA-owned specs file; the executor reads it from the MT5 Files
    # dir. We copy it into the staging area so replay is hermetic.
    p = os.path.expanduser(
        "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/"
        "MQL5/Files/nova_symbol_specs.json")
    if os.path.exists(p):
        return p
    # fallback: any specs file under the state dir
    cands = [os.path.join(lib.state_dir(bridge), f)
             for f in ("nova_symbol_specs.json", "specs.json")]
    for c in cands:
        if os.path.exists(c):
            return c
    raise lib.PipelineHalt("gate (b): no symbol specs file found for replay")


def prepare_trees(version_id, candidate_files_dir, bridge=None):
    """Build candidate_tree/ and baseline_tree/ under the version dir."""
    vdir = lib.version_dir(version_id, bridge)
    cand_tree = os.path.join(vdir, "candidate_tree")
    base_tree = os.path.join(vdir, "baseline_tree")
    for d in (cand_tree, base_tree):
        if os.path.exists(d):
            import shutil
            shutil.rmtree(d)
    lib.log_line("[gate-b] snapshotting live bridge -> baseline_tree")
    lib.snapshot_tree(lib.bridge_root(bridge), base_tree)
    lib.log_line("[gate-b] building candidate_tree (baseline + patch)")
    lib.snapshot_tree(lib.bridge_root(bridge), cand_tree)
    lib.apply_files(candidate_files_dir, cand_tree)
    return cand_tree, base_tree


def _compute_metrics_subprocess(profits, bridge):
    code = (
        "import sys, json; sys.path.insert(0, %r); "
        "import performance; "
        "m = performance.compute_metrics(%r); "
        "m['total_pnl'] = sum(%r); "
        "print(json.dumps(m))"
    )
    rc, out, err = lib.run(
        [sys.executable, "-c", code % (lib.bridge_root(bridge),
                                      profits, profits)],
        timeout=120)
    if rc != 0:
        raise lib.PipelineHalt(f"metrics subprocess failed: {(out+err)[-500:]}")
    return json.loads(out.strip().splitlines()[-1])


def _maxdd_value(m):
    dd = m.get("max_drawdown")
    if isinstance(dd, dict):
        return float(dd.get("max_drawdown", 0.0))
    return float(dd or 0.0)


def _better(new, old, tol=REGRESSION_TOL):
    """Strict improvement beyond tolerance (handles None)."""
    if new is None or old is None:
        return new is not None and old is None
    if old == 0:
        return new > 0
    return (new - old) / abs(old) > tol


def _no_regression(new, old, tol=REGRESSION_TOL, lower_is_better=False):
    if new is None and old is None:
        return True
    if new is None or old is None:
        return False
    if old == 0:
        return (new <= 0) if lower_is_better else (new >= 0)
    rel = (new - old) / abs(old)
    return rel <= tol if lower_is_better else rel >= -tol


def compare_behavior(candidate_series, current_series, bridge):
    """Real-money comparison of two P&L series. Returns (ok, detail)."""
    mc = _compute_metrics_subprocess(candidate_series, bridge)
    mb = _compute_metrics_subprocess(current_series, bridge)
    detail = {"candidate": mc, "current": mb, "checks": {}}
    checks = detail["checks"]
    # Hard: must not lose more in total.
    checks["total_pnl_no_worse"] = mc["total_pnl"] >= mb["total_pnl"]
    # No regression beyond tolerance on risk-adjusted metrics.
    checks["sharpe_no_regression"] = _no_regression(
        mc["sharpe_ratio"], mb["sharpe_ratio"])
    checks["profit_factor_no_regression"] = _no_regression(
        mc["profit_factor"], mb["profit_factor"])
    checks["max_drawdown_no_regression"] = _no_regression(
        _maxdd_value(mc), _maxdd_value(mb), lower_is_better=True)
    # Strict improvement on at least one.
    checks["sharpe_improved"] = _better(mc["sharpe_ratio"], mb["sharpe_ratio"])
    checks["profit_factor_improved"] = _better(
        mc["profit_factor"], mb["profit_factor"])
    checks["max_drawdown_improved"] = _better(
        _maxdd_value(mb), _maxdd_value(mc))
    improved_one = (checks["sharpe_improved"]
                    or checks["profit_factor_improved"]
                    or checks["max_drawdown_improved"])
    ok = (checks["total_pnl_no_worse"]
          and checks["sharpe_no_regression"]
          and checks["profit_factor_no_regression"]
          and checks["max_drawdown_no_regression"]
          and improved_one)
    detail["improved_at_least_one"] = improved_one
    detail["verdict"] = "PASS" if ok else "FAIL"
    return ok, detail


def run_staging(version_id, candidate_files_dir, change_class, bridge=None,
                state_override=None, signals_cap=2000, signals_days=14):
    """Run gate (b). Returns (ok, report)."""
    lib.log_line(f"[gate-b] staging validation ({change_class})")
    bridge = lib.bridge_root(bridge)
    sdir = state_override or lib.state_dir(bridge)
    cand_tree, base_tree = prepare_trees(
        version_id, candidate_files_dir, bridge)

    stage_dir = os.path.join(lib.version_dir(version_id, bridge), "staging")
    os.makedirs(stage_dir, exist_ok=True)
    journal = os.path.join(sdir, "nova_journal.jsonl")
    cfg = _read_cfg(sdir)
    specs_src = _specs_path(bridge)
    specs_copy = os.path.join(stage_dir, "nova_symbol_specs.json")
    import shutil
    shutil.copy2(specs_src, specs_copy)
    cfg_copy = os.path.join(stage_dir, "risk_config.json")
    lib.write_json(cfg_copy, cfg)

    signals = replay.collect_signals(journal, days=signals_days,
                                     cap=signals_cap)
    inputs_path = os.path.join(stage_dir, "inputs.jsonl")
    kill = _read_kill_switch_file(sdir)
    enabled = kill == "1" and bool(cfg.get("trading_enabled"))
    today_str = datetime.now().strftime("%Y-%m-%d")
    n = replay.write_inputs(inputs_path, signals, enabled=enabled,
                            today_str=today_str)
    lib.log_line(f"[gate-b] replaying {n} signals through both trees")

    cand_recs = replay.run_tree(cand_tree, inputs_path,
                                os.path.join(stage_dir, "candidate_out.jsonl"),
                                cfg_copy, specs_copy)
    base_recs = replay.run_tree(base_tree, inputs_path,
                                os.path.join(stage_dir, "baseline_out.jsonl"),
                                cfg_copy, specs_copy)

    report = {
        "gate": "b:staging",
        "change_class": change_class,
        "n_signals": n,
        "simplifications": [
            "max_signal_age_hours=0 (staleness gate is unit-tested)",
            "specs_fresh=True (freshness is unit-tested)",
            "open_positions=[] / todays_profit=0.0 (position-state replay "
            "is out of scope; divergences are reported for human review)",
            "enforce_mode_checks=False, mode=DEMO (halted demo legacy path)",
        ],
    }

    cand_by_id = {r["signal_id"]: r for r in cand_recs}
    base_by_id = {r["signal_id"]: r for r in base_recs}
    divergences = []
    for sid, cr in cand_by_id.items():
        br = base_by_id.get(sid)
        if br is None:
            divergences.append({"signal_id": sid, "kind": "missing_baseline"})
        elif (cr["decision"], cr.get("flags")) != \
                (br["decision"], br.get("flags")):
            divergences.append({
                "signal_id": sid,
                "candidate": cr["decision"], "baseline": br["decision"],
            })
    crashes = [r for r in cand_recs if r["decision"] == "ERROR"]
    report["n_divergences"] = len(divergences)
    report["n_candidate_crashes"] = len(crashes)
    report["divergences"] = divergences[:50]
    report["crashes"] = crashes[:20]
    lib.write_json(os.path.join(stage_dir, "divergences.json"),
                   {"divergences": divergences, "crashes": crashes})

    if crashes:
        report["ok"] = False
        report["note"] = (f"candidate crashed on {len(crashes)} signal(s); "
                          "rejected")
        return False, report

    if change_class == "refactor":
        ok = len(divergences) == 0
        report["ok"] = ok
        report["note"] = ("decision-identical across "
                          f"{n} signals" if ok else
                          f"{len(divergences)} decision divergence(s); "
                          "refactor must be decision-identical; rejected")
        return ok, report

    # ---- behavior: attributable-outcome comparison ----
    sig_ticket = replay.build_signal_ticket_map(journal)
    ticket_pnl = replay.build_ticket_pnl_map(journal)
    journaled = {}
    for sig, ctx in signals:
        if ctx.get("journaled_decision"):
            journaled[sig["id"]] = ctx["journaled_decision"]

    cand_series, curr_series = [], []
    unattributable_new = 0
    cand_commanded = 0
    for sig, _ctx in signals:
        sid = sig["id"]
        cr = cand_by_id.get(sid)
        if cr is None or cr["decision"] == "ERROR":
            continue
        ticket = sig_ticket.get(sid)
        pnl = ticket_pnl.get(str(ticket)) if ticket else None
        if journaled.get(sid) == "commanded" and pnl is not None:
            curr_series.append(pnl)
        if cr["decision"] == "commanded":
            cand_commanded += 1
            if pnl is not None:
                cand_series.append(pnl)
            else:
                unattributable_new += 1

    report["n_attributable_current"] = len(curr_series)
    report["n_attributable_candidate"] = len(cand_series)
    report["n_unattributable_candidate_commands"] = unattributable_new
    coverage = (len(cand_series) / cand_commanded) if cand_commanded else 1.0
    report["attribution_coverage"] = round(coverage, 4)

    if len(cand_series) < MIN_ATTRIBUTABLE or \
            len(curr_series) < MIN_ATTRIBUTABLE:
        report["ok"] = False
        report["note"] = (
            f"insufficient evidence: {len(cand_series)} attributable "
            f"candidate trades / {len(curr_series)} current trades "
            f"(need >= {MIN_ATTRIBUTABLE} each); rejected")
        return False, report
    if coverage < MIN_ATTRIBUTION_COVERAGE:
        report["ok"] = False
        report["note"] = (
            f"attribution coverage {coverage:.1%} < "
            f"{MIN_ATTRIBUTION_COVERAGE:.0%}; insufficient evidence; rejected")
        return False, report

    ok, detail = compare_behavior(cand_series, curr_series, bridge)
    report["metrics_comparison"] = detail
    report["ok"] = ok
    report["note"] = ("candidate wins on risk-adjusted metrics (real-money "
                      "standard)" if ok else
                      "candidate failed the real-money metric comparison; "
                      "rejected")
    return ok, report


def _read_cfg(sdir):
    cfg = lib.read_json(os.path.join(sdir, "risk_config.json"), {})
    if not cfg:
        raise lib.PipelineHalt("gate (b): risk_config.json missing/empty")
    return cfg


def _read_kill_switch_file(sdir):
    try:
        with open(os.path.join(sdir, "trading_enabled")) as f:
            return f.read().strip()
    except OSError:
        return "0"
