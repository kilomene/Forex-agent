"""Issue 7 regression test: kill-switch source-of-truth anchors.

Asserts the documented anchors still parse: the canonical journal
kill_switch.tripped event (time + tripped_by), the halted
run/trading_enabled file, and the source-of-truth doc itself.
"""
import json
import os
import sys
from datetime import datetime, timezone

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

RUN = os.path.join(BRIDGE, "run")
JOURNAL = os.path.join(RUN, "nova_journal.jsonl")
ENABLED = os.path.join(RUN, "trading_enabled")
DOC = os.path.join(BRIDGE, "docs", "KILL_SWITCH_SOURCE_OF_TRUTH.md")

CANON_TIME = "2026-09-23 21:05:18"  # UTC


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
            if ev.get("type") == "kill_switch.tripped":
                out.append(ev)
    return out


def test_canonical_kill_event_present():
    evs = _kill_events()
    assert len(evs) >= 1, "no kill_switch.tripped event in journal"
    ev = evs[-1]
    assert ev["time"] == CANON_TIME
    assert "parent/main-agent" in ev.get("tripped_by", "")


def test_trading_still_halted():
    with open(ENABLED) as f:
        assert f.read().strip() == "0"
    mtime = datetime.fromtimestamp(os.path.getmtime(ENABLED),
                                   tz=timezone.utc)
    assert (mtime.year, mtime.month, mtime.day) == (2026, 9, 23), (
        f"trading_enabled mtime moved unexpectedly: {mtime.isoformat()}")


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
