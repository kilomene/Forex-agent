"""Issue 7 regression test: kill-switch source-of-truth anchors.

Two tiers:
- Hermetic (run everywhere, incl. CI): the source-of-truth doc anchors
  and the UTC-by-construction timestamp test.
- Live-deployment (run only where the trading machine's run/ state
  exists): the canonical journal kill_switch.tripped event and the
  halted run/trading_enabled file. These assert THIS machine's safety
  posture; on a fresh checkout (CI) they skip instead of failing, which
  is what killed the build on commit 23fad237.
"""
import json
import os
import sys
from datetime import datetime, timezone

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

RUN = os.path.join(BRIDGE, "run")
JOURNAL = os.path.join(RUN, "nova_journal.jsonl")
ENABLED = os.path.join(RUN, "trading_enabled")
DOC = os.path.join(BRIDGE, "docs", "KILL_SWITCH_SOURCE_OF_TRUTH.md")

CANON_TIME = "2026-09-23 21:05:18"  # UTC

needs_run_state = pytest.mark.skipif(
    not os.path.exists(JOURNAL),
    reason="live run state not present on this machine (e.g. CI checkout)")


def _kill_events():
    out = []
    with open(JOURNAL, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") in ("kill_switch.tripped", "kill_switch.released"):
                out.append(ev)
    return out


@needs_run_state
def test_canonical_kill_event_present():
    # Historical anchor: the 2026-09-23 trip must remain in the journal.
    # It is no longer asserted to be the LATEST trip: later legitimate
    # halts (e.g. the 2026-10-16 T3 max-concurrent halt) supersede it.
    evs = [e for e in _kill_events() if e.get("type") == "kill_switch.tripped"]
    assert len(evs) >= 1, "no kill_switch.tripped event in journal"
    canon = [e for e in evs if e.get("time") == CANON_TIME]
    assert canon, f"canonical {CANON_TIME} trip missing from journal"
    assert "parent/main-agent" in canon[0].get("tripped_by", "")


@needs_run_state
def test_kill_state_consistent():
    # Live invariant (not a state snapshot): the kill-switch file and the
    # journal must agree. "1" => the latest kill event is a release;
    # "0" => the latest kill event is a trip with attribution. This holds
    # across halts and resumes instead of rotting on the next state change.
    evs = _kill_events()
    assert evs, "no kill_switch events in journal"
    last = evs[-1]
    with open(ENABLED) as f:
        enabled = f.read().strip()
    if enabled == "1":
        assert last["type"] == "kill_switch.released", (
            f"trading_enabled=1 but latest kill event is {last['type']}")
    else:
        assert last["type"] == "kill_switch.tripped", (
            f"trading_enabled={enabled} but latest kill event is "
            f"{last['type']}")
        assert last.get("tripped_by"), "latest trip has no attribution"


def test_source_of_truth_doc_exists_and_names_anchors():
    assert os.path.exists(DOC), "KILL_SWITCH_SOURCE_OF_TRUTH.md missing"
    with open(DOC) as f:
        text = f.read()
    assert CANON_TIME in text
    assert "21:03:38" in text  # file-mtime ground truth
    assert "nova_journal.jsonl" in text


def test_journal_timestamps_are_utc_by_construction(tmp_path):
    # Repair round 2 (2026-09-24): the "UTC canonical source" claim must
    # hold on ANY host timezone, not just because this host happens to be
    # UTC. now_str() and every journal "time" stamper use
    # datetime.now(timezone.utc) explicitly. Under TZ=America/New_York the
    # old host-local code would stamp Eastern time; the fixed code must
    # still stamp UTC.
    import time as _time

    import trade_executor as te
    import risk_state
    import shadow_tracker

    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        _time.tzset()
        utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        assert te.now_str() == utc_now
        assert risk_state._now_str() == utc_now
        run = str(tmp_path / "run")
        os.makedirs(run)
        shadow_tracker.journal(run, {"type": "probe"})
        with open(os.path.join(run, "nova_journal.jsonl")) as f:
            ev = json.loads(f.readline())
        assert ev["time"] == utc_now
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _time.tzset()
