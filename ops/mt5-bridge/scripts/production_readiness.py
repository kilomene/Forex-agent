#!/usr/bin/env python3
"""Production readiness gate for the Nova MT5 trading stack.

Evaluates a battery of checks and prints EXACTLY ONE verdict on stdout:
    READY_FOR_DEMO | READY_FOR_LIVE | NOT_READY
followed by a JSON detail block.

Exit code 0 for READY_*, 2 for NOT_READY.

HONESTY RULES (hard):
  * READY_FOR_LIVE requires mode==LIVE AND a valid live authorization AND a
    broker verification against the LIVE account that actually succeeded
    (real account block match). Since no LIVE account exists, this script
    must never print READY_FOR_LIVE on this machine.
  * Any failed required check -> NOT_READY. No partial passes.

Path overrides (for tests): MT5_FILES_DIR, TRADER_STATE_DIR.
"""

import glob
import json
import os
import re
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
# This script lives at ~/workspace/mt5/scripts/; the bridge package is the
# sibling directory ~/workspace/mt5/bridge/.
BRIDGE = os.path.join(os.path.dirname(SCRIPTS), "bridge")
if BRIDGE not in sys.path:
    sys.path.insert(0, BRIDGE)

FILES_DIR = os.environ.get(
    "MT5_FILES_DIR",
    os.path.expanduser(
        "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files"))
RUN_DIR = os.environ.get("TRADER_STATE_DIR",
                         os.path.join(BRIDGE, "run"))

SPECS_MAX_AGE_S = 300
POSITIONS_MAX_AGE_S = 60
EXECUTOR_STATE_MAX_AGE_S = 180
RECONCILE_ALERT_WINDOW_S = 3600

# Forbidden adapter stems (matched against code identifiers via tokenize in
# check_no_fake_adapters; comments and string literals are not code).
FORBIDDEN_NAME_RE = r"(?i)^(papers?|simulat\w*)$"
FORBIDDEN_SUBSTRINGS = ("fake_broker", "dry_live", "safe_demo")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _check(name, ok, detail, required=True):
    return {"name": name, "ok": bool(ok), "detail": detail,
            "required": required}


def _file_age_s(path):
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _code_lines(path):
    """Non-comment source lines (// and # comments stripped)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        code = line.split("#", 1)[0]
        out.append(code)
    return out


# --------------------------------------------------------------------------
# mode record (Worker A's trading_mode.py is canonical; JSON fallback)
# --------------------------------------------------------------------------

def _load_mode_record():
    """Return (record_dict_or_None, source, detail)."""
    try:
        import trading_mode as tm
    except ImportError:
        tm = None
    if tm is not None:
        try:
            rec = tm.load_mode()
        except Exception as e:  # noqa: BLE001 - report, don't crash
            return None, "trading_mode.py", f"load_mode() raised: {e}"
        d = {
            "mode": getattr(rec, "mode", None),
            "account_login": getattr(rec, "account_login", None),
            "broker_server": getattr(rec, "broker_server", None),
            "account_type": getattr(rec, "account_type", None),
        }
        return d, "trading_mode.py", "loaded via trading_mode.load_mode()"
    # JSON fallback (written by an authorized setup step, never by hand)
    jp = os.path.join(RUN_DIR, "trading_mode.json")
    data = _read_json(jp)
    if isinstance(data, dict):
        return data, "run/trading_mode.json", "loaded JSON fallback"
    return None, "none", "no mode record: trading_mode.py missing and " \
                         "run/trading_mode.json absent"


def _mode_module():
    try:
        import trading_mode as tm
        return tm
    except ImportError:
        return None


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------

def check_mode_record():
    rec, src, detail = _load_mode_record()
    if rec is None:
        return _check("mode_record", False,
                      f"mode record missing ({src}): {detail}")
    mode = (rec.get("mode") or "").upper()
    if mode not in ("DEMO", "LIVE"):
        return _check("mode_record", False,
                      f"mode record present ({src}) but mode={mode!r} invalid")
    return _check("mode_record", True,
                  f"mode={mode} via {src}; login={rec.get('account_login')}; "
                  f"server={rec.get('broker_server')}")


def check_mode_valid():
    rec, _, _ = _load_mode_record()
    mode = ((rec or {}).get("mode") or "").upper()
    return _check("mode_valid", mode in ("DEMO", "LIVE"),
                  f"mode={mode!r} (must be DEMO or LIVE)")


def check_demo_config():
    rec, _, _ = _load_mode_record()
    mode = ((rec or {}).get("mode") or "").upper()
    return _check("demo_config", mode == "DEMO",
                  f"mode={mode!r}; demo config applies only in DEMO mode")


def check_live_config():
    rec, _, _ = _load_mode_record()
    mode = ((rec or {}).get("mode") or "").upper()
    if mode != "LIVE":
        return _check("live_config", True,
                      f"mode={mode!r}; live config not required in DEMO mode")
    # Canonical check via Worker A's namespace reader. Values are NEVER
    # printed; only redact()'s safe display form (password shown as
    # set/missing) is reported.
    tm = _mode_module()
    if tm is None:
        return _check("live_config", False, "trading_mode.py missing")
    try:
        profile = tm.get_profile("LIVE")
    except Exception as e:  # noqa: BLE001 - ModeError("live credentials...")
        return _check("live_config", False,
                      f"LIVE_MT5_* env incomplete: {e}")
    red = tm.redact(profile)
    return _check("live_config", True,
                  "LIVE_MT5_* present: login=%s server=%s password=%s "
                  "(redacted display only)" % (
                      red.get("login"), red.get("server"),
                      red.get("password")))


def check_no_forbidden_fallback():
    tm = _mode_module()
    if tm is None:
        return _check("no_forbidden_fallback", False,
                      "cannot verify NO_LIVE_TO_DEMO_FALLBACK: "
                      "trading_mode.py missing")
    flag = getattr(tm, "NO_LIVE_TO_DEMO_FALLBACK", None)
    if flag is not True:
        return _check("no_forbidden_fallback", False,
                      f"NO_LIVE_TO_DEMO_FALLBACK={flag!r} (must be True)")
    # Scan bridge code for live->demo fallback logic outside comments.
    # The sanctioned NO_LIVE_TO_DEMO_FALLBACK identifier and blocklist
    # definitions (lines mentioning FORBIDDEN) are not violations.
    hits = []
    for path in glob.glob(os.path.join(BRIDGE, "*.py")):
        base = os.path.basename(path)
        if base.startswith("test_") or os.path.basename(
                os.path.dirname(path)) == "tests":
            continue
        for i, code in enumerate(_code_lines(path), 1):
            # The sanctioned no-fallback identifier in any case is not a
            # violation (e.g. JSON keys referencing the constant).
            code2 = re.sub(r"no_live_to_demo_fallback", "", code,
                           flags=re.IGNORECASE)
            if "forbidden" in code2.lower():
                continue
            low = code2.lower()
            if "fallback" in low and "demo" in low:
                hits.append(f"{base}:{i}")
            if re.search(r"NO_LIVE_TO_DEMO_FALLBACK\s*=\s*False", code):
                hits.append(f"{base}:{i}")
    if hits:
        return _check("no_forbidden_fallback", False,
                      f"live->demo fallback code found: {hits[:5]}")
    return _check("no_forbidden_fallback", True,
                  "NO_LIVE_TO_DEMO_FALLBACK=True; no fallback code found")


def _broker_account_block():
    """(login, server, positions_fresh, specs_fresh, detail)."""
    pos = _read_json(os.path.join(FILES_DIR, "nova_positions.json"))
    specs = _read_json(os.path.join(FILES_DIR, "nova_symbol_specs.json"))
    pos_age = _file_age_s(os.path.join(FILES_DIR, "nova_positions.json"))
    specs_age = _file_age_s(os.path.join(FILES_DIR, "nova_symbol_specs.json"))
    login = (pos or {}).get("account")
    server = ((specs or {}).get("account") or {}).get("server")
    detail = (f"positions age={pos_age:.0f}s login={login}; "
              f"specs age={specs_age:.0f}s server={server}"
              if pos_age is not None and specs_age is not None
              else "broker files missing")
    return (login, server,
            pos_age is not None and pos_age <= POSITIONS_MAX_AGE_S,
            specs_age is not None and specs_age <= SPECS_MAX_AGE_S, detail)


def check_account_identity():
    rec, _, _ = _load_mode_record()
    login, server, pos_fresh, specs_fresh, detail = _broker_account_block()
    if not (pos_fresh and specs_fresh):
        return _check("account_identity", False,
                      f"broker account block not fresh: {detail}")
    if rec is None:
        return _check("account_identity", False,
                      "no mode record to verify identity against")
    ok_login = str(rec.get("account_login")) == str(login)
    ok_server = str(rec.get("broker_server") or "").lower() == \
        str(server or "").lower()
    return _check("account_identity", ok_login and ok_server,
                  f"record login={rec.get('account_login')} vs broker "
                  f"{login} ({'match' if ok_login else 'MISMATCH'}); record "
                  f"server={rec.get('broker_server')} vs broker {server} "
                  f"({'match' if ok_server else 'MISMATCH'})")


def check_broker_server():
    rec, _, _ = _load_mode_record()
    _, server, pos_fresh, specs_fresh, detail = _broker_account_block()
    if not (pos_fresh and specs_fresh):
        return _check("broker_server", False,
                      f"broker server unknown (stale files): {detail}")
    mode = ((rec or {}).get("mode") or "").upper()
    if mode == "DEMO" and "demo" not in str(server).lower():
        return _check("broker_server", False,
                      f"DEMO mode but broker server={server!r} is not a "
                      f"demo server")
    if rec is not None and str(rec.get("broker_server") or "").lower() != \
            str(server or "").lower():
        return _check("broker_server", False,
                      f"mode record server={rec.get('broker_server')!r} != "
                      f"broker server={server!r}")
    return _check("broker_server", True, f"broker server={server}")


def check_broker_connectivity():
    _, _, pos_fresh, specs_fresh, detail = _broker_account_block()
    return _check("broker_connectivity", pos_fresh and specs_fresh,
                  f"positions fresh={pos_fresh}, specs fresh={specs_fresh}; "
                  f"{detail}")


def _executor_alive():
    # 1) state file freshness (the loop rewrites state every cycle)
    age = _file_age_s(os.path.join(RUN_DIR, "trade_executor.state.json"))
    if age is not None and age <= EXECUTOR_STATE_MAX_AGE_S:
        return True, f"executor state fresh ({age:.0f}s old)"
    # 2) live process scan (/proc cmdline, excluding our own pid)
    me = str(os.getpid())
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or pid == me:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode(
                        "utf-8", "replace")
            except OSError:
                continue
            if "trade_executor.py" in cmd and "production_readiness" not in cmd:
                return True, f"executor process alive (pid {pid})"
    except OSError:
        pass
    return False, "no fresh executor state and no live trade_executor process"


def check_execution_capability():
    cmd_path = os.path.join(FILES_DIR, "nova_commands.jsonl")
    writable = os.access(cmd_path, os.W_OK) if os.path.exists(cmd_path) \
        else os.access(FILES_DIR, os.W_OK)
    alive, alive_detail = _executor_alive()
    ok = writable and alive
    return _check("execution_capability", ok,
                  f"commands file writable={writable}; {alive_detail}")


def check_risk_engine():
    cfg = _read_json(os.path.join(RUN_DIR, "risk_config.json"))
    if not isinstance(cfg, dict):
        return _check("risk_engine", False, "risk_config.json missing/unparseable")
    problems = []
    rpt = cfg.get("risk_per_trade_pct")
    if not isinstance(rpt, (int, float)) or not (0.05 <= rpt <= 1.0):
        problems.append(f"risk_per_trade_pct={rpt!r} outside [0.05, 1.0]")
    mct = cfg.get("max_concurrent_trades")
    if not isinstance(mct, int) or not (1 <= mct <= 10):
        problems.append(f"max_concurrent_trades={mct!r} outside [1, 10]")
    mdl = cfg.get("max_daily_loss_pct")
    if not isinstance(mdl, (int, float)) or not (0 < mdl <= 10):
        problems.append(f"max_daily_loss_pct={mdl!r} outside (0, 10]")
    cb = cfg.get("capital_basis")
    if not isinstance(cb, (int, float)) or cb <= 0:
        problems.append(f"capital_basis={cb!r} must be positive")
    if problems:
        return _check("risk_engine", False, "; ".join(problems))
    return _check("risk_engine", True,
                  f"risk={rpt}% max_concurrent={mct} daily_loss={mdl}% "
                  f"basis=${cb:,.0f}")


def check_kill_switch():
    path = os.path.join(RUN_DIR, "trading_enabled")
    try:
        with open(path) as f:
            val = f.read().strip()
    except OSError:
        return _check("kill_switch", False,
                      "kill switch file run/trading_enabled unreadable")
    return _check("kill_switch", val in ("0", "1"),
                  f"kill switch readable, value={val!r} "
                  f"({'ARMED' if val == '1' else 'SAFE/TRIPPED'})")


def check_reconciliation():
    _, _, pos_fresh, _, detail = _broker_account_block()
    if not pos_fresh:
        return _check("reconciliation", False,
                      f"positions feed stale; cannot reconcile: {detail}")
    # Scan the journal tail for unresolved critical reconcile findings.
    cutoff = time.time() - RECONCILE_ALERT_WINDOW_S
    critical = []
    try:
        with open(os.path.join(RUN_DIR, "nova_journal.jsonl")) as f:
            lines = f.readlines()
    except OSError:
        lines = []
    for line in lines[-5000:]:
        if '"reconcile.finding"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("severity") != "critical":
            continue
        if ev.get("resolved"):
            continue
        critical.append(ev.get("type"))
    if critical:
        return _check("reconciliation", False,
                      f"{len(critical)} unresolved critical reconcile "
                      f"finding(s) in the last hour: {critical[:3]}")
    return _check("reconciliation", True,
                  "positions fresh; no unresolved critical findings in "
                  "the last hour")


def check_durable_storage():
    if not os.path.isdir(RUN_DIR) or not os.access(RUN_DIR, os.W_OK):
        return _check("durable_storage", False,
                      f"run dir {RUN_DIR} missing or not writable")
    jp = os.path.join(RUN_DIR, "nova_journal.jsonl")
    if not os.access(jp, os.W_OK):
        return _check("durable_storage", False,
                      "journal not appendable")
    return _check("durable_storage", True,
                  f"run dir writable; journal appendable")


def check_ai_health():
    for mod in ("ai_engine",):
        try:
            __import__(mod)
            return _check("ai_health", True, f"{mod} importable",
                          required=False)
        except ImportError:
            continue
    return _check("ai_health", True,
                  "note: ai_engine not present in bridge; no AI component "
                  "to health-check (informational only)",
                  required=False)


def check_no_fake_adapters():
    """Scan bridge code for fake/paper adapter *identifiers*.

    Uses tokenize: comments and string literals (docstrings, prose) are
    skipped, so a docstring that merely discusses a no-simulation policy
    is not a violation. Only real code identifiers (function/class/
    variable names) containing the forbidden stems are flagged.
    """
    import tokenize
    name_re = re.compile(r"(?i)^(papers?|simulat\w*)$")
    substrings = ("fake_broker", "dry_live", "safe_demo")
    hits = []
    for path in sorted(glob.glob(os.path.join(BRIDGE, "*.py"))):
        base = os.path.basename(path)
        if base.startswith("test_"):
            continue
        if os.path.basename(os.path.dirname(path)) == "tests":
            continue
        try:
            with tokenize.open(path) as f:
                tokens = list(tokenize.generate_tokens(f.readline))
        except (OSError, SyntaxError, tokenize.TokenError):
            continue
        seen = set()
        for tok in tokens:
            if tok.type != tokenize.NAME:
                continue
            name = tok.string
            low = name.lower()
            flagged = any(name_re.match(p) for p in name.split("_")) or \
                any(s in low for s in substrings)
            if flagged and (base, name) not in seen:
                seen.add((base, name))
                hits.append(f"{base}:{tok.start[0]}:{name}")
    if hits:
        return _check("no_fake_adapters", False,
                      f"forbidden adapter identifiers in code: {hits[:8]}")
    return _check("no_fake_adapters", True,
                  "no placeholder-adapter identifiers in bridge code")


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------

CHECKS = [
    check_mode_record,
    check_mode_valid,
    check_demo_config,
    check_live_config,
    check_no_forbidden_fallback,
    check_account_identity,
    check_broker_server,
    check_broker_connectivity,
    check_execution_capability,
    check_risk_engine,
    check_kill_switch,
    check_reconciliation,
    check_durable_storage,
    check_ai_health,
    check_no_fake_adapters,
]


def _live_authorization_valid():
    """True only if the mode module reports a valid LIVE authorization."""
    tm = _mode_module()
    if tm is None:
        return False, "trading_mode.py missing"
    fn = getattr(tm, "is_live_authorized", None)
    if not callable(fn):
        return False, "trading_mode.is_live_authorized not available"
    try:
        ok = bool(fn())
    except Exception as e:  # noqa: BLE001
        return False, f"is_live_authorized raised: {e}"
    return ok, f"is_live_authorized()={ok}"


def _live_broker_verified(results):
    """True only if the broker account block matched the LIVE record."""
    by_name = {c["name"]: c for c in results}
    ident = by_name.get("account_identity")
    rec, _, _ = _load_mode_record()
    mode = ((rec or {}).get("mode") or "").upper()
    return (mode == "LIVE" and ident is not None and ident["ok"],
            f"mode={mode}, account_identity ok="
            f"{ident['ok'] if ident else None}")


def evaluate():
    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as e:  # noqa: BLE001 - a check must never crash us
            results.append(_check(fn.__name__.replace("check_", ""),
                                 False, f"check raised: {e}"))
    failed_required = [c for c in results
                       if c["required"] and not c["ok"]]
    rec, _, _ = _load_mode_record()
    mode = ((rec or {}).get("mode") or "").upper()

    if failed_required:
        verdict = "NOT_READY"
        reason = ("failed required checks: "
                  + ", ".join(c["name"] for c in failed_required))
    elif mode == "LIVE":
        auth_ok, auth_detail = _live_authorization_valid()
        verified, ver_detail = _live_broker_verified(results)
        if auth_ok and verified:
            verdict = "READY_FOR_LIVE"
            reason = f"live authorized ({auth_detail}); broker verified " \
                     f"({ver_detail})"
        else:
            verdict = "NOT_READY"
            reason = (f"LIVE mode but live authorization valid={auth_ok} "
                      f"({auth_detail}); broker verified={verified} "
                      f"({ver_detail})")
    elif mode == "DEMO":
        verdict = "READY_FOR_DEMO"
        reason = "all required checks passed in DEMO mode"
    else:
        verdict = "NOT_READY"
        reason = f"mode={mode!r} is not DEMO or LIVE"
    return verdict, reason, results


def main():
    verdict, reason, results = evaluate()
    # Exactly one verdict line first, then the JSON detail block.
    print(verdict)
    print(json.dumps({"verdict": verdict, "reason": reason,
                      "checks": results}, indent=2))
    return 0 if verdict in ("READY_FOR_DEMO", "READY_FOR_LIVE") else 2


if __name__ == "__main__":
    sys.exit(main())
