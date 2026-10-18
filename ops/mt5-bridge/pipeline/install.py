#!/usr/bin/env python3
"""Gate (e) install + (g) EA recompile gate + (h) post-install assertions.

Install order:
  1. refuse if the kill switch is live ("1") -- the pipeline never
     installs into a live-trading engine without an explicit override
     (and auto-updates NEVER flip the switch themselves);
  2. snapshot the current code tree -> versions/<id>/previous_snapshot/;
  3. overlay the candidate files onto the live bridge;
  4. EA versions: copy the freshly compiled .ex5 artifacts into the MT5
     prefix (both MQL5/Experts/ and MQL5/Experts/Advisors/, per the
     Navigator layout); GUI re-attach stays a manual step and is logged;
  5. restart the executor via relaunch_executor.py;
  6. post-install assertions (gate h). Any failure -> rollback.py
     --previous automatically, then re-verify.

Gate (g): any version touching *.mq5 must recompile cleanly with the
real MetaEditor64.exe under wine64 BEFORE install. The compile runs in
a /tmp sandbox (Z: drive mapping) so the live prefix is untouched;
compile failure rejects the version.
"""

import os
import re
import shutil
import sys
import time

from . import lib

MT5_DIR = os.path.expanduser(
    "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5")
WINEPREFIX = os.path.expanduser("~/workspace/mt5/prefix")


def _relaunch_script():
    return os.environ.get(
        "EVOLUTION_RELAUNCH_SCRIPT",
        os.path.expanduser("~/workspace/mt5/scripts/relaunch_executor.py"))


def _supervise_script():
    return os.environ.get(
        "EVOLUTION_SUPERVISE_SCRIPT",
        os.path.expanduser("~/workspace/mt5/supervise_stack.py"))


def _daemon_script(bridge):
    return os.environ.get(
        "EVOLUTION_DAEMON_SCRIPT",
        os.path.join(lib.bridge_root(bridge), "trade_executor.py"))


# ---------------------------------------------------------------- gate (g)

def _wine_bin():
    for c in ("wine64", "/usr/local/bin/wine64", "/usr/lib/wine/wine64"):
        if c == "wine64":
            p = shutil.which("wine64")
            if p:
                return p
        elif os.path.exists(c):
            return c
    return None


def compile_mq5(mq5_relpath, candidate_files_dir, version_id, bridge=None):
    """Gate (g): verified MQL5 recompile of a candidate .mq5.

    Returns (ok, detail). The compile happens in /tmp; the live MT5
    prefix is never touched by this gate.
    """
    src = os.path.join(candidate_files_dir, mq5_relpath)
    if not os.path.exists(src):
        return False, {"ok": False,
                       "note": f"mq5 {mq5_relpath} missing from version"}
    wine = _wine_bin()
    if not wine:
        return False, {"ok": False, "note": "wine64 not found; cannot "
                                            "verify MQL5 compile"}
    work = os.path.join("/tmp", f"evo_mq5_{version_id}")
    exp_dir = os.path.join(work, "MQL5", "Experts")
    os.makedirs(exp_dir, exist_ok=True)
    name = os.path.basename(mq5_relpath)
    dest = os.path.join(exp_dir, name)
    shutil.copy2(src, dest)
    # Wine maps Z: to /. Build the Z: path for the compile target.
    zpath = "Z:" + dest.replace("/", "\\")
    log_zpath = "Z:" + os.path.join(work, "compile.log").replace("/", "\\")
    me = os.path.join(MT5_DIR, "MetaEditor64.exe")
    if not os.path.exists(me):
        return False, {"ok": False,
                       "note": f"MetaEditor64.exe not found at {me}"}
    env = {"WINEPREFIX": WINEPREFIX,
           "WINEDEBUG": "-all"}
    rc, out, err = lib.run(
        [wine, me, f"/compile:{zpath}", f"/log:{log_zpath}"],
        cwd=MT5_DIR, timeout=900, env=env)
    log_text = ""
    log_host = os.path.join(work, "compile.log")
    if os.path.exists(log_host):
        with open(log_host, errors="replace") as f:
            log_text = f.read()
    ex5 = os.path.splitext(dest)[0] + ".ex5"
    errors = 0
    for m in re.finditer(r"(\d+)\s+errors?", log_text + "\n" + out):
        errors = max(errors, int(m.group(1)))
    ok = (errors == 0 and os.path.exists(ex5)
          and os.path.getmtime(ex5) >= os.path.getmtime(dest) - 1)
    detail = {"ok": ok, "file": mq5_relpath, "errors": errors,
              "ex5_built": os.path.exists(ex5),
              "returncode": rc,
              "log_tail": (log_text + out + err)[-1500:]}
    if ok:
        detail["ex5_artifact"] = ex5
    return ok, detail


def gate_g_ea_compile(changed_files, candidate_files_dir, version_id,
                      bridge=None):
    """Run gate (g) for every changed .mq5. Returns (ok, [details])."""
    mq5s = [f for f in changed_files if f.endswith(".mq5")]
    if not mq5s:
        return True, [{"ok": True, "note": "no .mq5 changes; gate (g) n/a"}]
    details = []
    all_ok = True
    for m in mq5s:
        lib.log_line(f"[gate-g] compiling {m}")
        ok, d = compile_mq5(m, candidate_files_dir, version_id, bridge)
        details.append(d)
        all_ok = all_ok and ok
    return all_ok, details


# ---------------------------------------------------------------- gate (h)

def post_install_asserts(bridge=None, state_override=None,
                         expected_blocked_hash=None,
                         skip_supervisor=False):
    """Gate (h). Returns a list of (name, ok, detail); raises
    PipelineError on the first failure."""
    bridge = lib.bridge_root(bridge)
    sdir = state_override or lib.state_dir(bridge)
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        if not ok:
            raise lib.PipelineError(
                f"post-install assert FAILED: {name}: {detail}")
        lib.log_line(f"[gate-h] ok: {name}")

    # 1. kill switch still "0" -- auto-updates never resume trading.
    try:
        with open(os.path.join(sdir, "trading_enabled")) as f:
            kill = f.read().strip()
    except OSError:
        kill = "<unreadable>"
    check("trading_enabled still '0'", kill == "0", f"value={kill!r}")

    # 2. risk config keys unchanged.
    cfg = lib.read_json(os.path.join(sdir, "risk_config.json"), {})
    check("risk_per_trade_pct == 0.05",
          abs(float(cfg.get("risk_per_trade_pct", -1)) - 0.05) < 1e-12,
          f"value={cfg.get('risk_per_trade_pct')!r}")
    check("max_concurrent_trades == 10",
          int(cfg.get("max_concurrent_trades", -1)) == 10,
          f"value={cfg.get('max_concurrent_trades')!r}")

    # 3. blocked_symbols untouched (GBPZAR exclusion is Zenas-only).
    import hashlib
    import json as _json
    syms = sorted(str(s).upper()
                  for s in (cfg.get("blocked_symbols") or []))
    h = hashlib.sha256(_json.dumps(syms).encode()).hexdigest()
    if expected_blocked_hash is None:
        expected_blocked_hash = os.environ.get(
            "EVOLUTION_BLOCKED_HASH") or _last_blocked_hash(bridge)
    check("blocked_symbols unchanged",
          h == expected_blocked_hash,
          f"hash={h[:12]} expected={str(expected_blocked_hash)[:12]}")

    # 4. daemon healthy: /proc cmdline shows the script as argv[1].
    pid = lib.daemon_pid(script_path=_daemon_script(bridge))
    check("executor daemon alive (argv[1] is trade_executor.py)",
          pid is not None, f"pid={pid}")

    # 5. supervisor reports healthy.
    if not skip_supervisor:
        sup = _supervise_script()
        rc, out, err = lib.run([sys.executable, sup, "--health"],
                               timeout=300)
        healthy_line = any(
            "daemon trade_executor.py: pid" in line
            for line in out.splitlines())
        check("supervise_stack --health exit 0 + executor pid present",
              rc == 0 and healthy_line,
              f"rc={rc} executor_pid_line={healthy_line}")
    else:
        lib.log_line("[gate-h] supervisor check skipped (--skip-supervisor)")
    return results


def _last_blocked_hash(bridge):
    from . import manifest as _manifest
    inst = _manifest.read_installed(bridge)
    cur = inst.get("current")
    if cur:
        v = _manifest.read_version(cur, bridge) or {}
        return v.get("blocked_symbols_hash")
    return None


# ---------------------------------------------------------------- gate (e)

def _auto_rollback(bridge, state_override, reason):
    lib.log_line(f"[install] {reason} -> auto-rollback")
    rb = os.path.join(lib.bridge_root(bridge), "rollback.py")
    cmd = [sys.executable, rb, "--previous",
           "--bridge-root", lib.bridge_root(bridge)]
    if state_override:
        cmd += ["--state-dir", state_override]
    env = {}
    for k in ("EVOLUTION_RELAUNCH_SCRIPT", "EVOLUTION_SUPERVISE_SCRIPT",
              "EVOLUTION_DAEMON_SCRIPT", "EVOLUTION_BLOCKED_HASH"):
        if k in os.environ:
            env[k] = os.environ[k]
    return lib.run(cmd, timeout=600, env=env or None)


def install_version(version_id, candidate_files_dir, changed_files,
                    bridge=None, state_override=None, skip_supervisor=False,
                    no_restart=False, ex5_artifacts=None):
    """Gate (e): install the candidate, restart, assert (h).

    Returns the asserts list. Raises PipelineError (after attempting
    rollback) on any failure.
    """
    bridge = lib.bridge_root(bridge)
    sdir = state_override or lib.state_dir(bridge)

    kill_path = os.path.join(sdir, "trading_enabled")
    try:
        kill = open(kill_path).read().strip()
    except OSError:
        kill = "0"
    if kill == "1" and not os.environ.get("EVOLUTION_ALLOW_LIVE_INSTALL"):
        raise lib.PipelineError(
            "refusing install while kill switch is live ('1'); the "
            "pipeline never installs into a live-trading engine "
            "unattended")

    expected_blocked = _blocked_hash_now(sdir)
    vdir = lib.version_dir(version_id, bridge)

    # Snapshot current code BEFORE touching anything.
    snap = os.path.join(vdir, "previous_snapshot")
    if os.path.exists(snap):
        shutil.rmtree(snap)
    n = lib.snapshot_tree(bridge, snap)
    lib.log_line(f"[install] snapshotted {n} live files -> previous_snapshot")

    # Overlay the candidate.
    m = lib.apply_files(candidate_files_dir, bridge)
    lib.log_line(f"[install] applied {m} candidate files to {bridge}")

    # EA artifacts into the MT5 prefix (live only; never in sandbox).
    mq5s = [f for f in changed_files if f.endswith(".mq5")]
    if mq5s and not state_override:
        _install_ea_artifacts(mq5s, bridge, ex5_artifacts or {})
    elif mq5s:
        lib.log_line("[install] sandbox: skipping live MT5 prefix copy "
                     "for .ex5 artifacts")

    # Restart the daemon.
    if not no_restart:
        rs = _relaunch_script()
        lib.log_line(f"[install] restarting executor via {rs}")
        env = {}
        if state_override:
            env["TRADER_STATE_DIR"] = state_override
        rc, out, err = lib.run([sys.executable, rs], timeout=180,
                               env=env or None)
        lib.log_line(f"[install] relaunch rc={rc}")
        time.sleep(5)
    else:
        lib.log_line("[install] daemon restart skipped (--no-restart)")

    # Post-install assertions; failure -> auto-rollback.
    try:
        results = post_install_asserts(
            bridge, state_override,
            expected_blocked_hash=expected_blocked,
            skip_supervisor=skip_supervisor)
    except lib.PipelineError as e:
        rc, out, err = _auto_rollback(
            bridge, state_override,
            f"post-install assert failed: {e}")
        raise lib.PipelineError(
            f"install rejected ({e}); auto-rollback rc={rc}") from e

    lib.journal_event_to(sdir, {
        "type": "pipeline.installed",
        "version": version_id,
        "files": changed_files,
        "actor": "evolution-loop",
    })
    return results


def _blocked_hash_now(sdir):
    import hashlib
    import json as _json
    cfg = lib.read_json(os.path.join(sdir, "risk_config.json"), {})
    syms = sorted(str(s).upper()
                  for s in (cfg.get("blocked_symbols") or []))
    return hashlib.sha256(_json.dumps(syms).encode()).hexdigest()


def _install_ea_artifacts(mq5s, bridge, ex5_artifacts):
    """Copy compiled .ex5 next to the live EAs.

    GUI re-attach (Navigator refresh + attach to chart) stays a manual
    step -- the pipeline cannot click through the terminal. Logged.
    """
    experts = os.path.join(MT5_DIR, "MQL5", "Experts")
    advisors = os.path.join(MT5_DIR, "MQL5", "Experts", "Advisors")
    for m in mq5s:
        # copy the candidate .mq5 into the prefix source tree
        src = os.path.join(bridge, m)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(experts, os.path.basename(m)))
        ex5 = ex5_artifacts.get(m)
        if ex5 and os.path.exists(ex5):
            for d in (experts, advisors):
                os.makedirs(d, exist_ok=True)
                shutil.copy2(ex5, os.path.join(
                    d, os.path.basename(ex5)))
                lib.log_line(f"[install] EA artifact -> {d}")
    lib.log_line("[install] MANUAL STEP REQUIRED: refresh Navigator and "
                 "re-attach the EA to its chart in the MT5 terminal")
