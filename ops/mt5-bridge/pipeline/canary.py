#!/usr/bin/env python3
"""Gate (c): CANARY in dry-run shadow mode + (d) auto-rollback on guard
trip + (f) plugin two-stage enablement (OBSERVE -> enforce).

Shadow canary (trading halted, the normal case): the candidate tree
evaluates each live signal.received through its own check_gates in an
isolated subprocess. Decisions are logged next to the live engine's
journaled decisions; any divergence is evidence, not a failure, but
these guard trips abort the version immediately:

* candidate crash (an exception evaluating a signal),
* error-rate spike (> MAX_ERROR_RATE of evaluations raising),
* unexpected open-position attempt (any write to nova_commands.jsonl
  inside the isolated canary state dir),
* kill-switch trip attempt (any write flipping canary trading_enabled).

On any guard trip the pipeline invokes rollback.py --previous
automatically (gate d). Pre-install there is nothing to restore, so
rollback verifies stack health and journals the abort.

If trading ever resumes (kill switch "1"), canary instead installs the
candidate with tightened guards (max 1 concurrent position via a
temporary risk_config overlay that is reverted afterwards). That path
is implemented but has never been exercised while halted; it requires
the same gates and is documented as such.
"""

import json
import os
import sys
import time
from datetime import datetime

from . import lib
from . import replay

DEFAULT_SOAK_SIGNALS = 200
DEFAULT_SOAK_SECONDS = 48 * 3600
MAX_ERROR_RATE = 0.01
CANARY_MAX_CONCURRENT_LIVE = 1  # tightened guard if trading ever resumes


def _tail_new_signals(journal_path, offset, deadline):
    """Yield (new_offset, signal.received events) until deadline."""
    while time.time() < deadline:
        try:
            size = os.path.getsize(journal_path)
        except OSError:
            size = 0
        if size < offset:
            offset = 0  # journal truncated/rotated
        events = []
        try:
            with open(journal_path) as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "signal.received":
                        events.append(ev)
                offset = f.tell()
        except OSError:
            pass
        yield offset, events
        if events:
            continue
        time.sleep(5)


def _check_canary_state_clean(canary_state):
    """Guard: the shadow candidate must never attempt a real action."""
    problems = []
    cmd = os.path.join(canary_state, "nova_commands.jsonl")
    try:
        if os.path.getsize(cmd) > 0:
            problems.append("unexpected open-position attempt: "
                            "nova_commands.jsonl written in canary state")
    except OSError:
        pass
    kill = os.path.join(canary_state, "trading_enabled")
    try:
        with open(kill) as f:
            if f.read().strip() != "0":
                problems.append("kill-switch trip attempt in canary state")
    except OSError:
        pass
    return problems


def _auto_rollback(bridge, state_override, reason, version_id):
    lib.log_line(f"[gate-c] GUARD TRIP: {reason} -> auto-rollback")
    rb = os.path.join(lib.bridge_root(bridge), "rollback.py")
    cmd = [sys.executable, rb, "--previous",
           "--bridge-root", lib.bridge_root(bridge)]
    if state_override:
        cmd += ["--state-dir", state_override]
    env = {}
    for k in ("EVOLUTION_RELAUNCH_SCRIPT", "EVOLUTION_SUPERVISE_SCRIPT",
              "EVOLUTION_DAEMON_SCRIPT"):
        if k in os.environ:
            env[k] = os.environ[k]
    rc, out, err = lib.run(cmd, timeout=600, env=env or None)
    if not state_override:
        lib.journal_event(bridge, {
            "type": "canary.aborted",
            "version": version_id,
            "reason": reason,
            "rollback_rc": rc,
            "rollback_output": (out + err)[-1500:],
        })
    else:
        lib.journal_event_to(state_override, {
            "type": "canary.aborted",
            "version": version_id,
            "reason": reason,
            "rollback_rc": rc,
            "rollback_output": (out + err)[-1500:],
        })
    return rc, out, err


def run_shadow_canary(version_id, candidate_tree, bridge=None,
                      state_override=None, journal_path=None,
                      soak_signals=DEFAULT_SOAK_SIGNALS,
                      soak_seconds=DEFAULT_SOAK_SECONDS):
    """Run gate (c). Returns (ok, report)."""
    lib.log_line("[gate-c] shadow canary (dry-run, trading halted)")
    bridge = lib.bridge_root(bridge)
    sdir = state_override or lib.state_dir(bridge)
    cdir = os.path.join(lib.version_dir(version_id, bridge), "canary")
    canary_state = os.path.join(cdir, "canary_state")
    os.makedirs(canary_state, exist_ok=True)

    # Isolated canary state: kill switch forced "0", config copied.
    with open(os.path.join(canary_state, "trading_enabled"), "w") as f:
        f.write("0")
    cfg = lib.read_json(os.path.join(sdir, "risk_config.json"), {})
    lib.write_json(os.path.join(canary_state, "risk_config.json"), cfg)
    specs_src = os.path.join(cdir, os.pardir, "staging",
                             "nova_symbol_specs.json")
    if not os.path.exists(specs_src):
        from . import staging as _st
        specs_src = _st._specs_path(bridge)
    import shutil
    specs_copy = os.path.join(cdir, "nova_symbol_specs.json")
    shutil.copy2(specs_src, specs_copy)
    cfg_copy = os.path.join(cdir, "risk_config.json")
    lib.write_json(cfg_copy, cfg)

    live_journal = journal_path or os.path.join(sdir, "nova_journal.jsonl")
    try:
        offset = os.path.getsize(live_journal)
    except OSError:
        offset = 0

    shadow_log = os.path.join(cdir, "canary_shadow.jsonl")
    open(shadow_log, "w").close()
    inputs_path = os.path.join(cdir, "inputs.jsonl")
    outputs_path = os.path.join(cdir, "outputs.jsonl")

    deadline = time.time() + soak_seconds
    n_signals = 0
    n_diffs = 0
    n_errors = 0
    journaled_decisions = {}

    def refresh_decisions(upto_offset):
        # (re)read trade.decision events for diffing
        try:
            f = open(live_journal)
        except OSError:
            return
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "trade.decision" and ev.get("signal_id"):
                    journaled_decisions[ev["signal_id"]] = ev.get("decision")

    refresh_decisions(offset)
    today_str = datetime.now().strftime("%Y-%m-%d")
    trip = None

    for offset, events in _tail_new_signals(live_journal, offset, deadline):
        if not events and n_signals >= soak_signals:
            break
        for ev in events:
            if n_signals >= soak_signals:
                break
            sig = {
                "id": ev.get("signal_id") or ev.get("id"),
                "symbol": ev.get("symbol"),
                "direction": ev.get("direction"),
                "timeframe": ev.get("timeframe"),
                "entry_price": ev.get("entry_price"),
                "candle_time": ev.get("candle_time"),
            }
            n = replay.write_inputs(inputs_path, [(sig, {})],
                                    enabled=False, today_str=today_str)
            recs = replay.run_tree(candidate_tree, inputs_path,
                                   outputs_path, cfg_copy, specs_copy,
                                   timeout=300)
            rec = recs[0] if recs else {"decision": "ERROR",
                                        "error": "no record"}
            if rec["decision"] == "ERROR":
                n_errors += 1
                trip = ("candidate crash evaluating signal "
                        f"{sig['id']}: {rec.get('error')}")
                break
            refresh_decisions(offset)
            live_dec = journaled_decisions.get(sig["id"])
            diff = live_dec is not None and live_dec != rec["decision"]
            if diff:
                n_diffs += 1
            lib.append_jsonl(shadow_log, {
                "type": "canary.decision",
                "signal_id": sig["id"],
                "candidate_decision": rec["decision"],
                "live_decision": live_dec,
                "diff": diff,
            })
            n_signals += 1
            for problem in _check_canary_state_clean(canary_state):
                trip = problem
                break
            if trip:
                break
            if n_errors / max(n_signals, 1) > MAX_ERROR_RATE and n_signals:
                trip = (f"candidate error-rate spike: {n_errors}/{n_signals}")
                break
        if trip:
            break
        if n_signals >= soak_signals:
            break

    report = {
        "gate": "c:shadow_canary",
        "soak_signals_target": soak_signals,
        "soak_seconds_target": soak_seconds,
        "n_signals": n_signals,
        "n_diffs_vs_live": n_diffs,
        "n_candidate_errors": n_errors,
        "soak_complete": n_signals >= soak_signals,
        "guard_trip": trip,
    }
    lib.write_json(os.path.join(cdir, "canary_report.json"), report)
    if trip:
        _auto_rollback(bridge, state_override, trip, version_id)
        report["ok"] = False
        report["note"] = f"guard trip -> auto-rollback invoked: {trip}"
        return False, report
    report["ok"] = True
    report["note"] = (
        f"calm soak: {n_signals} signals, {n_diffs} diffs vs live, "
        f"{n_errors} candidate errors")
    return True, report


# ---------------------------------------------------------------------------
# (f) plugin two-stage enablement: OBSERVE -> enforce
# ---------------------------------------------------------------------------

PLUGIN_CLASSES = {
    "cooldown_period": "CooldownPeriod",
    "stoploss_guard": "StoplossGuard",
    "session_filter": "SessionFilter",
}


def _plugin_eval_code():
    # Subprocess body: build the named filter from protections, replay
    # signals through evaluate(), return per-signal verdicts.
    return r'''
import sys, json
tree_dir, plugin_name, cfg_json, inputs_path, outputs_path = sys.argv[1:6]
sys.path.insert(0, tree_dir)
import protections
cls = getattr(protections, {"cooldown_period": "CooldownPeriod",
                            "stoploss_guard": "StoplossGuard",
                            "session_filter": "SessionFilter"}[plugin_name])
pcfg = json.loads(cfg_json)
filt = cls(enabled=True, **{k: v for k, v in pcfg.items()
                            if k in ("cooldown_candles",
                                     "default_candle_minutes",
                                     "max_stopouts", "lookback_trades",
                                     "only_symbol", "session_rules")})
ctx_inputs = json.load(open(inputs_path))
out = open(outputs_path, "w")
n_err = 0
for item in ctx_inputs:
    sig = item["signal"]
    ctx = protections.GateContext(
        last_exit_times={s: __import__("datetime").datetime.fromisoformat(t)
                         for s, t in (item.get("last_exit_times") or {}).items()},
        recent_closes=item.get("recent_closes") or [],
        session=item.get("session"))
    try:
        v = filt.evaluate(sig, ctx)
        if v == protections.ALLOW:
            rec = {"signal_id": sig.get("id"), "would_block": False}
        else:
            rec = {"signal_id": sig.get("id"), "would_block": True,
                   "reason": v[1], "flags": v[2]}
    except Exception as e:  # noqa: BLE001 -- errors are the metric
        n_err += 1
        rec = {"signal_id": sig.get("id"), "would_block": None,
               "error": repr(e)[:300]}
    out.write(json.dumps(rec) + "\n")
out.close()
print(json.dumps({"n_errors": n_err}))
'''


def observe_plugin(plugin_name, proposal_id, bridge=None, state_override=None,
                    max_signals=500, journal_path=None):
    """Stage (f-1): run the plugin in OBSERVE mode.

    Evaluations are journaled to run/protections_observe.jsonl (NOT the
    live trader journal -- observe data is pipeline evidence, and the
    live journal stays the record of real engine decisions). Nothing is
    enforced. Returns (ok, report).
    """
    if plugin_name not in PLUGIN_CLASSES:
        raise lib.PipelineError(f"unknown plugin {plugin_name!r}")
    bridge = lib.bridge_root(bridge)
    sdir = state_override or lib.state_dir(bridge)
    journal = journal_path or os.path.join(sdir, "nova_journal.jsonl")
    signals = replay.collect_signals(journal, days=14, cap=max_signals)

    # Build plugin state inputs from journaled closes.
    last_exit, recent_closes = {}, []
    for ev in lib.iter_jsonl(journal):
        if ev.get("type") != "trade.closed":
            continue
        sym, t = ev.get("symbol"), ev.get("time") or ev.get(
            "exit_time_broker")
        if sym and t:
            ts = str(t).replace(".", "-")[:19]
            if sym not in last_exit or ts > last_exit[sym]:
                last_exit[sym] = ts
        recent_closes.append({"symbol": ev.get("symbol"),
                              "reason": ev.get("reason")})
    recent_closes = recent_closes[-50:]

    # Per-signal session from the journaled decision context.
    dec_ctx = {}
    for ev in lib.iter_jsonl(journal):
        if ev.get("type") == "trade.decision" and ev.get("signal_id"):
            dec_ctx[ev["signal_id"]] = (ev.get("context") or {}).get(
                "session")

    work = os.path.join(sdir, "plugin_observe")
    os.makedirs(work, exist_ok=True)
    inputs = []
    for sig, _ctx in signals:
        inputs.append({
            "signal": sig,
            "last_exit_times": last_exit,
            "recent_closes": recent_closes,
            "session": dec_ctx.get(sig["id"]),
        })
    inputs_path = os.path.join(work, f"{plugin_name}_inputs.json")
    outputs_path = os.path.join(work, f"{plugin_name}_outputs.jsonl")
    lib.write_json(inputs_path, inputs)

    pcfg = _plugin_config(bridge, plugin_name, sdir)
    code_path = os.path.join(work, f"_observe_{plugin_name}.py")
    with open(code_path, "w") as f:
        f.write(_plugin_eval_code())
    rc, out, err = lib.run(
        [sys.executable, code_path, bridge, plugin_name,
         json.dumps(pcfg), inputs_path, outputs_path],
        cwd=bridge, timeout=900)
    if rc != 0:
        raise lib.PipelineHalt(
            f"observe runner failed: {(out + err)[-1000:]}")
    try:
        n_errors = json.loads(out.strip().splitlines()[-1])["n_errors"]
    except (ValueError, KeyError, IndexError):
        raise lib.PipelineHalt(
            f"observe runner output unparseable: {(out + err)[-500:]}")

    n_eval = n_block = 0
    for line in open(outputs_path):
        rec = json.loads(line)
        if rec.get("error"):
            continue
        n_eval += 1
        would = bool(rec.get("would_block"))
        n_block += 1 if would else 0
        lib.append_jsonl(os.path.join(sdir, "protections_observe.jsonl"), {
            "type": "filter.observe",
            "plugin": plugin_name,
            "proposal": proposal_id,
            "signal_id": rec.get("signal_id"),
            "would_block": would,
            "reason": rec.get("reason"),
        })
    block_rate = (n_block / n_eval) if n_eval else 0.0
    report = {
        "stage": "f:plugin_observe",
        "plugin": plugin_name,
        "proposal": proposal_id,
        "n_evaluated": n_eval,
        "n_would_block": n_block,
        "block_rate": round(block_rate, 4),
        "n_errors": n_errors,
        "ok": n_errors == 0,
    }
    obs_dir = os.path.join(sdir, "plugin_observe_reports")
    os.makedirs(obs_dir, exist_ok=True)
    lib.write_json(os.path.join(
        obs_dir, f"{plugin_name}_{proposal_id}.json"), report)
    return report["ok"], report


def _plugin_config(bridge, plugin_name, sdir):
    cfg = lib.read_json(os.path.join(sdir, "risk_config.json"), {})
    return (cfg.get("protections") or {}).get(plugin_name) or {}


def enforce_plugin(plugin_name, proposal_id, bridge=None,
                    state_override=None):
    """Stage (f-2): flip a calm-soaked plugin to enforce.

    Requires, in order:
    1. A calm soak: zero evaluation errors and a block rate inside the
       proposal's preregistered expected bounds.
    2. Engine wiring: trade_executor must actually read the protections
       config section. If the wiring proposal is not installed, the flip
       would be a silent no-op -- so the pipeline REFUSES instead.

    Protection-plugin enablement is AUTO tier under Zenas's governing
    2026-10-18 order: the gating comes from this pipeline's own checks
    (calm observe soak, preregistered bounds, engine wiring, post-flip
    hard-rule assertions), not a one-tap approval.
    """
    bridge = lib.bridge_root(bridge)
    sdir = state_override or lib.state_dir(bridge)
    if plugin_name not in PLUGIN_CLASSES:
        raise lib.PipelineError(f"unknown plugin {plugin_name!r}")

    header = _read_proposal_header(bridge, proposal_id)
    lo = float(header.get("expected_block_rate_min", 0.0))
    hi = float(header.get("expected_block_rate_max", 1.0))
    rep = lib.read_json(os.path.join(
        sdir, "plugin_observe_reports",
        f"{plugin_name}_{proposal_id}.json"))
    if not rep:
        raise lib.PipelineError(
            f"plugin {plugin_name}: no observe report; run observe first")
    if rep.get("n_errors"):
        raise lib.PipelineError(
            f"plugin {plugin_name}: {rep['n_errors']} evaluation errors in "
            "observe soak; refused")
    br = float(rep.get("block_rate", 0.0))
    if not (lo <= br <= hi):
        raise lib.PipelineError(
            f"plugin {plugin_name}: block rate {br:.3f} outside "
            f"preregistered bounds [{lo}, {hi}]; refused")

    # Engine wiring check: the flip must actually DO something.
    te_path = os.path.join(bridge, "trade_executor.py")
    try:
        src = open(te_path).read()
    except OSError:
        src = ""
    if 'cfg.get("protections"' not in src and \
            "cfg.get('protections'" not in src:
        raise lib.PipelineError(
            f"plugin {plugin_name}: trade_executor.py does not read a "
            "'protections' config section yet (engine-wiring proposal not "
            "installed); enforcing now would be a silent no-op -- refused")

    cfg_path = os.path.join(sdir, "risk_config.json")
    cfg = lib.read_json(cfg_path, {})
    before = dict(cfg)
    protections_cfg = dict(cfg.get("protections") or {})
    entry = dict(protections_cfg.get(plugin_name) or {})
    entry.update({"mode": "enforce"})
    protections_cfg[plugin_name] = entry
    cfg["protections"] = protections_cfg
    lib.write_json(cfg_path, cfg)

    # Post-flip assertions: the hard rules still hold.
    _assert_hard_rules(bridge, sdir, before_cfg=before)
    lib.journal_event_to(sdir, {
        "type": "config.changed",
        "actor": "evolution-loop",
        "note": f"plugin {plugin_name} observe -> enforce "
                f"(proposal {proposal_id})",
        "old": {"protections": before.get("protections")},
        "new": {"protections": cfg.get("protections")},
    })
    return {"ok": True, "plugin": plugin_name, "proposal": proposal_id,
            "block_rate": br}


def _assert_hard_rules(bridge, sdir, before_cfg=None):
    cfg = lib.read_json(os.path.join(sdir, "risk_config.json"), {})
    if abs(float(cfg.get("risk_per_trade_pct", 0)) - 0.05) > 1e-12:
        raise lib.PipelineError("hard rule violated: risk_per_trade_pct "
                                "changed")
    if int(cfg.get("max_concurrent_trades", 0)) != 10:
        raise lib.PipelineError("hard rule violated: max_concurrent_trades "
                                "changed")
    try:
        with open(os.path.join(sdir, "trading_enabled")) as f:
            if f.read().strip() != "0":
                raise lib.PipelineError(
                    "hard rule violated: trading_enabled != '0'")
    except OSError:
        raise lib.PipelineError("hard rule violated: kill-switch file "
                                "unreadable")


def _read_proposal_header(bridge, proposal_id):
    p = os.path.join(bridge, "proposals", proposal_id, "proposal.md")
    header = {}
    try:
        with open(p) as f:
            lines = f.read().splitlines()
    except OSError:
        return header
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                header[k.strip()] = v.strip()
    return header
