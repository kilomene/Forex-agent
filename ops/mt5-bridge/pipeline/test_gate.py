#!/usr/bin/env python3
"""Gate (a): FULL TEST SUITE GREEN.

Runs `python3 -m pytest tests/ -q` inside the candidate work tree.
Any failure rejects the version -- except the documented Sunday
market-closed environmental failures (exactly the two known test IDs,
and only while the market is closed per market_hours).

The pytest run happens in a subprocess with cwd set to the candidate
tree, so the candidate's own code (not this process's imports) is what
gets tested. Full output is stored as pytest_output.txt in the version
dir as install evidence.
"""

import os
import re
import sys

from . import lib


def _market_open(bridge):
    """market_hours.is_market_open() evaluated against the LIVE tree.

    Imported in a subprocess so a broken candidate tree cannot affect
    the answer (and so this module stays import-clean).
    """
    code = ("import sys; sys.path.insert(0, %r); "
            "import market_hours; "
            "print('OPEN' if market_hours.is_market_open() else 'CLOSED')"
            % lib.bridge_root(bridge))
    rc, out, _err = lib.run([sys.executable, "-c", code], timeout=60)
    if rc != 0:
        return True  # fail open on infra error: do NOT excuse failures
    return out.strip() == "OPEN"


_FAILED_RE = re.compile(r"^FAILED (\S+)", re.MULTILINE)
_SUMMARY_RE = re.compile(r"(\d+) failed, (\d+) passed")
_SUMMARY_RE2 = re.compile(r"(\d+) passed")


def parse_pytest_output(text):
    failed_ids = _FAILED_RE.findall(text)
    m = _SUMMARY_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2)), failed_ids
    m = _SUMMARY_RE2.search(text)
    if m:
        return 0, int(m.group(1)), failed_ids
    return None, None, failed_ids


def run_test_gate(work_dir, version_id, bridge=None):
    """Run gate (a). Returns (ok, report dict). Raises PipelineHalt on
    infrastructure failure."""
    lib.log_line(f"[gate-a] pytest in {work_dir}")
    # tests/test_supervise_stack.py resolves `supervise_stack` from the
    # grandparent of the bridge dir (~/workspace/mt5/supervise_stack.py,
    # outside the versioned tree). Inside a candidate work tree that
    # grandparent does not exist, so the module would fail to import at
    # collection. PYTHONPATH keeps the import working without touching
    # the test.
    mt5_dir = os.path.dirname(lib.bridge_root(bridge))
    env = {"PYTHONPATH": mt5_dir + os.pathsep +
           os.environ.get("PYTHONPATH", "")}
    rc, out, err = lib.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=work_dir, timeout=1200, env=env)
    combined = out + ("\n" + err if err else "")
    vdir = lib.version_dir(version_id, bridge)
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, "pytest_output.txt"), "w") as f:
        f.write(combined)
    n_failed, n_passed, failed_ids = parse_pytest_output(combined)
    report = {
        "gate": "a:full_test_suite",
        "returncode": rc,
        "failed": n_failed,
        "passed": n_passed,
        "failed_ids": failed_ids,
    }
    if n_failed is None:
        raise lib.PipelineHalt(
            f"gate (a): could not parse pytest output (rc={rc})")
    if n_failed == 0:
        report["ok"] = True
        report["note"] = "full suite green"
        return True, report
    # Environmental exception: exactly the two known Sunday
    # market-closed failures, and only while the market is closed.
    market_open = _market_open(bridge)
    report["market_open"] = market_open
    if (not market_open
            and set(failed_ids) == lib.KNOWN_ENV_FAILURES
            and n_failed == len(lib.KNOWN_ENV_FAILURES)):
        report["ok"] = True
        report["note"] = ("only the two documented Sunday market-closed "
                          "environmental failures; excused")
        return True, report
    report["ok"] = False
    report["note"] = (f"{n_failed} test failure(s); version rejected "
                      f"(market_open={market_open})")
    return False, report
