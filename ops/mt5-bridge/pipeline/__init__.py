#!/usr/bin/env python3
"""Continuous-evolution install pipeline orchestrator.

Gate order is FIXED and enforced here -- no stage can run before the
previous one passed:

  scan   pre-install hard-block scan (kill-switch resume, GBPZAR)
  (a)    full test suite green in the candidate tree
  (g)    EA recompile gate (only when *.mq5 changed)
  (b)    staging validation, wired end-to-end (real signals, real gates)
  (c)    shadow canary (dry-run; guard trip -> auto-rollback, gate d)
  (e)    install: snapshot, overlay, restart daemon
  (h)    post-install assertions (failure -> auto-rollback)

Any gate failure aborts the run; the version is marked rejected and the
failure is journaled with evidence. Nothing is ever skipped.
"""

import os
import re
import shutil
import sys

from . import lib
from . import manifest
from . import test_gate
from . import staging
from . import canary
from . import install as install_mod

def _note_of(detail, limit=300):
    if isinstance(detail, dict):
        note = detail.get("note") or ""
    else:
        note = str(detail)
    return note[:limit]


GATE_ORDER = ["scan", "a", "g", "b", "c", "e", "h"]

# A candidate may never resume trading or touch the GBPZAR exclusion.
# Best-effort static tripwire; the post-install asserts (gate h) are the
# authoritative enforcement.
_HARD_BLOCK_PATTERNS = [
    (re.compile(r"""open\s*\([^)]*trading_enabled[^)]*["']w"""),
     "writes the kill-switch file"),
    (re.compile(r"""trading_enabled.{0,40}["']1["']"""),
     "sets trading_enabled to '1'"),
]


def hard_block_scan(files_dir):
    """Return a list of findings; empty means clean."""
    findings = []
    for _rel, ap in lib.iter_code_files(files_dir):
        if not ap.endswith(".py"):
            continue
        try:
            with open(ap, errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        for pat, why in _HARD_BLOCK_PATTERNS:
            if pat.search(src):
                findings.append({"file": ap, "reason": why})
    return findings


def prepare_work(version_id, candidate_files_dir, bridge=None):
    """Build candidate_tree/ + baseline_tree/ once; reused by gates."""
    return staging.prepare_trees(version_id, candidate_files_dir, bridge)


def _record(bridge, version_id, state_override, entry):
    sdir = state_override or lib.state_dir(bridge)
    lib.append_jsonl(os.path.join(sdir, "evolution.log"), entry)


def _journal(bridge, state_override, event):
    sdir = state_override or lib.state_dir(bridge)
    lib.journal_event_to(sdir, event)


def run_pipeline(version_id, bridge=None, state_override=None,
                 soak_signals=canary.DEFAULT_SOAK_SIGNALS,
                 soak_seconds=canary.DEFAULT_SOAK_SECONDS,
                 skip_supervisor=False, no_restart=False,
                 journal_path=None):
    """Run every gate in order. Returns (ok, version_manifest).

    Raises lib.PipelineError when a gate rejects the version (the
    version is marked rejected and the failure journaled). Raises
    lib.PipelineHalt on infrastructure failure.
    """
    bridge = lib.bridge_root(bridge)
    vdir = lib.version_dir(version_id, bridge)
    files_dir = os.path.join(vdir, "files")
    man = manifest.read_version(version_id, bridge)
    if not man:
        raise lib.PipelineHalt(f"unknown version {version_id}")
    if not os.path.isdir(files_dir):
        raise lib.PipelineHalt(f"version {version_id} has no files/")

    # Tier gate: tier "zenas" is HARD-BLOCKED. Under Zenas's governing
    # 2026-10-18 order ("everything would be done by you tho auto update
    # and upgrading"), tier zenas covers EXACTLY:
    #   (a) anything that would resume trading (trading_enabled -> "1"),
    #   (b) any change to blocked_symbols.
    # The pipeline never installs these; they are Zenas's own actions.
    # There is no approval file: this is a refusal, not an approval
    # flow. (a) is also tripped early by hard_block_scan (gate "scan");
    # (b) is asserted unchanged post-install (gate "h").
    if man.get("tier") == "zenas":
        raise lib.PipelineError(
            f"version {version_id}: tier 'zenas' is hard-blocked -- the "
            f"pipeline never installs (a) kill-switch resumes or (b) "
            f"blocked_symbols changes; those are Zenas's own actions. "
            f"Refused")

    change_class = man.get("change_class", "refactor")
    changed_files = man.get("files", [])
    lib.log_line(f"[pipeline] running version {version_id} "
                 f"(tier={man.get('tier')}, class={change_class}, "
                 f"{len(changed_files)} files)")

    def gate_done(name, ok, detail):
        man["gates"][name] = {"ok": ok, "detail": detail,
                              "utc": lib.utc_now_iso()}
        man["stage"] = name
        manifest.write_version(man, bridge)
        _record(bridge, version_id, state_override, {
            "utc": lib.utc_now_iso(), "version": version_id,
            "gate": name, "ok": ok,
            "note": _note_of(detail, 300),
        })

    # -- scan: hard-block tripwire -------------------------------------
    findings = hard_block_scan(files_dir)
    if findings:
        gate_done("scan", False, {"findings": findings})
        _journal(bridge, state_override, {
            "type": "pipeline.rejected", "version": version_id,
            "gate": "scan", "findings": findings,
            "actor": "evolution-loop"})
        man["status"] = "rejected"
        manifest.write_version(man, bridge)
        raise lib.PipelineError(
            f"hard-block scan failed: {findings}")
    gate_done("scan", True, {"note": "no kill-switch/GBPZAR tripwires"})

    # -- work trees (shared by a/b/c) ----------------------------------
    cand_tree, _base_tree = prepare_work(version_id, files_dir, bridge)

    # -- gate (a) -------------------------------------------------------
    ok, rep = test_gate.run_test_gate(cand_tree, version_id, bridge)
    gate_done("a", ok, rep)
    if not ok:
        return _reject(bridge, version_id, state_override, man,
                       "a", rep)

    # -- gate (g): EA compile -------------------------------------------
    ok, details = install_mod.gate_g_ea_compile(
        changed_files, files_dir, version_id, bridge)
    ex5_artifacts = {d["file"]: d.get("ex5_artifact")
                     for d in details if d.get("ex5_artifact")}
    gate_done("g", ok, {"details": details})
    if not ok:
        return _reject(bridge, version_id, state_override, man,
                       "g", details)

    # -- gate (b): staging ----------------------------------------------
    ok, rep = staging.run_staging(
        version_id, files_dir, change_class, bridge,
        state_override=state_override)
    gate_done("b", ok, rep)
    lib.write_json(os.path.join(vdir, "staging_report.json"), rep)
    if not ok:
        return _reject(bridge, version_id, state_override, man,
                       "b", rep)

    # -- gate (c): shadow canary ----------------------------------------
    ok, rep = canary.run_shadow_canary(
        version_id, cand_tree, bridge, state_override=state_override,
        journal_path=journal_path,
        soak_signals=soak_signals, soak_seconds=soak_seconds)
    gate_done("c", ok, rep)
    # canary_report.json already written by the canary itself
    if not ok:
        return _reject(bridge, version_id, state_override, man,
                       "c", rep)

    # -- gate (e): install ----------------------------------------------
    try:
        asserts = install_mod.install_version(
            version_id, files_dir, changed_files, bridge,
            state_override=state_override,
            skip_supervisor=skip_supervisor,
            no_restart=no_restart,
            ex5_artifacts=ex5_artifacts)
    except lib.PipelineError as e:
        gate_done("e/h", False, {"error": str(e)})
        return _reject(bridge, version_id, state_override, man,
                       "e/h", str(e))
    gate_done("e", True, {"note": "installed; daemon restarted"})
    gate_done("h", True, {"asserts": [
        {"name": n, "ok": o, "detail": d} for n, o, d in asserts]})

    # -- mark installed --------------------------------------------------
    prev = manifest.read_installed(bridge).get("current")
    man["status"] = "installed"
    man["blocked_symbols_hash"] = install_mod._blocked_hash_now(
        state_override or lib.state_dir(bridge))
    manifest.write_version(man, bridge)
    manifest.write_installed(version_id, prev, bridge)
    _journal(bridge, state_override, {
        "type": "pipeline.installed", "version": version_id,
        "previous": prev, "actor": "evolution-loop"})
    lib.log_line(f"[pipeline] version {version_id} INSTALLED "
                 f"(previous={prev})")
    return True, man


def _reject(bridge, version_id, state_override, man, gate, detail):
    man["status"] = "rejected"
    manifest.write_version(man, bridge)
    note = _note_of(detail, 500)
    _journal(bridge, state_override, {
        "type": "pipeline.rejected", "version": version_id,
        "gate": gate, "note": note, "actor": "evolution-loop"})
    lib.log_line(f"[pipeline] version {version_id} REJECTED at gate {gate}")
    return False, man
