"""Tests for the experiment pre-registration protocol (phase 3).

Covers: the template fills into a valid prereg file, header parse/validate
rules, and the weekend_reflection wiring -- active prereg with variant
data publishes per-variant P&L (journal event + .md), no prereg file says
so honestly, and an active prereg with no variant data yet never invents
numbers. Also pins that the EXPERIMENT_CHARTER.md append did not corrupt
the existing charter.
"""
import json
import os
import sys
from datetime import date, timedelta

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import prereg  # noqa: E402
import weekend_reflection as wr  # noqa: E402

CHARTER = os.path.join(BRIDGE, "EXPERIMENT_CHARTER.md")
TEMPLATE = os.path.join(BRIDGE, "experiment_preregistration_template.md")


# ---------------------------------------------------------------- helpers

def filled_template(name="cooldown-6h", start=None, end=None):
    """The template with every placeholder filled -> must validate."""
    today = date.today()
    start = start or (today - timedelta(days=7)).isoformat()
    end = end or (today + timedelta(days=7)).isoformat()
    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read()
    # fill the machine header
    text = text.replace("<experiment-name, e.g. cooldown-6h>", name)
    text = text.replace("<YYYY-MM-DD>\nwindow_end: <YYYY-MM-DD>",
                        f"{start}\nwindow_end: {end}")
    text = text.replace("<variant-a>, <variant-b>", "baseline, cooldown-6h")
    text = text.replace("<N, e.g. 20>", "20")
    text = text.replace("<X, e.g. 500>", "500")
    text = text.replace(
        "<-12.40, per-trade expectancy over the 30 days before the window>",
        "-12.40")
    text = text.replace(
        "<path + sha256 + date, e.g. run/risk_config.json @ 2026-10-25 (sha256:abc123...)>",
        "run/risk_config.json @ 2026-10-10 (sha256:deadbeef)")
    # fill the human sections
    text = text.replace(
        "<State the single idea this window tests, in one or two sentences.\n"
        "If you have two ideas, you need two windows — never test two ideas in one\n"
        "window, because then neither result means anything.>",
        "A 6-hour same-symbol cooldown after any close reduces M15 churn.")
    text = text.replace(
        "Example: \"A 6-hour same-symbol cooldown after any close reduces churn on\n"
        "M15 noise without giving up the symbols' real moves.\"\n\n", "")
    text = text.replace("<EXPERIMENT NAME>", "cooldown-6h window")
    text = text.replace("- **Window start:** <YYYY-MM-DD> (no trades before this date count)",
                        f"- **Window start:** {start} (no trades before this date count)")
    text = text.replace("- **Window end:** <YYYY-MM-DD> (no trades after this date count)",
                        f"- **Window end:** {end} (no trades after this date count)")
    text = text.replace(
        "<List the symbols this window covers — either the full 77-symbol watchlist\n"
        "or an explicit subset. A subset must be named here BEFORE the window opens;\n"
        "shrinking the list mid-window because a symbol is losing is\n"
        "cherry-picking, not science.>",
        "Full 77-symbol watchlist.")
    text = text.replace(
        "- Snapshot reference: <path + sha256 + date — copy from the PREREG block>",
        "- Snapshot reference: run/risk_config.json @ 2026-10-10 (sha256:deadbeef)")
    text = text.replace("**$<X>** (copy from the PREREG block)", "**$500**")
    text = text.replace("**<N>** trades fire per\n  variant (copy from the PREREG block)",
                        "**20** trades fire per variant")
    text = text.replace("**<N>** trades fire per variant (copy from the PREREG block)",
                        "**20** trades fire per variant")
    text = text.replace("<min_trades>", "20")
    text = text.replace("- **<variant-a>** (control): <what it does — current behavior, unchanged>",
                        "- **baseline** (control): current behavior, unchanged")
    text = text.replace("- **<variant-b>**: <what changes, precisely enough that someone else could\n  reimplement it>",
                        "- **cooldown-6h**: no new position on a symbol within 6h of its last close")
    return text


def close(ticket, profit, day, variant=None, signal_id=None):
    e = {"type": "trade.closed", "ticket": ticket, "profit": profit,
         "symbol": "EURUSD", "direction": "BUY",
         "time": f"{day} 10:00:00"}
    if variant is not None:
        e["variant"] = variant
    if signal_id is not None:
        e["signal_id"] = signal_id
    return e


def decision(signal_id, variant):
    return {"type": "trade.decision", "signal_id": signal_id,
            "variant": variant, "symbol": "EURUSD",
            "time": "2026-10-12 09:00:00"}


def write_state(tmp_path, journal_events, prereg_text=None,
                prereg_name="prereg_demo.md"):
    (tmp_path / "nova_journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in journal_events) + "\n",
        encoding="utf-8")
    if prereg_text is not None:
        (tmp_path / prereg_name).write_text(prereg_text, encoding="utf-8")
    return str(tmp_path)


@pytest.fixture
def iso_today():
    return date.today().isoformat()


@pytest.fixture
def in_window(iso_today):
    d = date.today() - timedelta(days=2)
    return d.isoformat()


# ------------------------------------------------- template parse/validate

def test_template_renders_and_validates():
    text = filled_template()
    errors = prereg.validate(text)
    assert errors == [], f"filled template must validate: {errors}"
    header = prereg.parse_header(text)
    assert header["name"] == "cooldown-6h"
    assert header["window_start"] < header["window_end"]
    assert header["variants"] == "baseline, cooldown-6h"


def test_raw_template_does_not_validate_as_filled():
    # Placeholders are not dates -- the raw template must NOT pass, so a
    # half-filled file can never be picked up as an active experiment.
    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read()
    assert prereg.validate(text), "raw template must fail validation"


def test_validate_catches_bad_header():
    bad = filled_template().replace(
        "window_start: ", "window_start: not-a-date", 1)
    assert any("window_start" in e for e in prereg.validate(bad))

    flipped = filled_template(start="2026-11-02", end="2026-10-26")
    assert any("window_start" in e and "<=" in e
               for e in prereg.validate(flipped))

    no_variants = filled_template().replace(
        "variants: baseline, cooldown-6h", "variants: ,")
    assert any("variants" in e for e in prereg.validate(no_variants))

    no_header = "# no header here\n## Idea under test\n"
    assert prereg.validate(no_header) == \
        ["missing <!-- PREREG ... --> machine header"]
    assert prereg.parse_header(no_header) is None


def test_validate_requires_honesty_sections():
    text = filled_template()
    stripped = text.replace("WIN RATE ALONE DECIDES NOTHING",
                            "win rate is everything")
    assert any("WIN RATE ALONE DECIDES NOTHING" in e
               for e in prereg.validate(stripped))
    stripped2 = text.replace("## 9. REAL-MONEY STANDARD", "## 9. MONEY")
    assert any("real-money standard" in e
               for e in prereg.validate(stripped2))


def test_load_active_picks_window_covering_today(tmp_path, iso_today):
    active_text = filled_template(name="active-one")
    write_state(tmp_path, [], active_text, "prereg_active.md")
    # an expired window must NOT be picked up
    old = filled_template(name="old-one", start="2026-01-01",
                          end="2026-01-07")
    (tmp_path / "prereg_old.md").write_text(old, encoding="utf-8")
    # an invalid prereg must never be picked up, even in-window
    (tmp_path / "prereg_broken.md").write_text(
        filled_template(name="broken").replace("window_start:", "window_start: XX", 1),
        encoding="utf-8")
    found = prereg.load_active(str(tmp_path), iso_today)
    names = [h["name"] for _, h in found]
    assert names == ["active-one"]


# --------------------------------------- reflection wiring: variant P&L

def _run_reflection(tmp_path, monkeypatch):
    monkeypatch.setattr(wr, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(wr, "JOURNAL_PATH",
                        str(tmp_path / "nova_journal.jsonl"))
    assert wr.main() == 0
    md = (tmp_path / f"reflection_{date.today().isoformat()}.md").read_text(
        encoding="utf-8")
    events = [json.loads(l) for l in
              (tmp_path / "nova_journal.jsonl").read_text(
                  encoding="utf-8").splitlines() if l.strip()]
    reflections = [e for e in events if e.get("type") == "reflection"]
    assert len(reflections) == 1
    return md, reflections[0]


def test_reflection_publishes_per_variant_pnl(tmp_path, monkeypatch,
                                              in_window):
    journal = [
        decision("sig1", "cooldown-6h"),
        close(101, 120.0, in_window, variant="baseline"),
        close(102, -40.0, in_window, variant="baseline"),
        # variant via linked decision fallback (no tag on the close)
        close(103, 60.0, in_window, signal_id="sig1"),
        # outside the window: must not count
        close(104, 9999.0, "2026-01-05", variant="baseline"),
        # untagged close inside the window: excluded from variant P&L
        close(105, 10.0, in_window),
    ]
    state = write_state(tmp_path, journal,
                        filled_template(name="cooldown-6h"))
    md, event = _run_reflection(tmp_path, monkeypatch)

    assert "## Pre-registered experiments" in md
    assert "`cooldown-6h`" in md
    # baseline: 120 - 40 = +80 over 2 trades
    assert "baseline" in md and "+80.00" in md
    # cooldown-6h: +60 over 1 trade, via decision fallback
    assert "cooldown-6h" in md and "+60.00" in md
    # the out-of-window 9999 must not leak into any variant total
    assert "9999" not in md
    # untagged trade reported honestly, not hidden
    assert "no variant tag" in md

    blocks = event["preregistration"]
    assert len(blocks) == 1
    block = blocks[0]
    assert block["name"] == "cooldown-6h"
    assert block["variants"]["baseline"]["total_pnl"] == 80.0
    assert block["variants"]["baseline"]["trades"] == 2
    assert block["variants"]["cooldown-6h"]["total_pnl"] == 60.0
    assert block["variants"]["cooldown-6h"]["trades"] == 1
    assert block["untagged_trades_in_window"] == 1
    # journal block is strict-JSON safe
    json.dumps(event)


def test_reflection_no_prereg_file(tmp_path, monkeypatch, in_window):
    journal = [close(201, 50.0, in_window)]
    write_state(tmp_path, journal)  # no prereg file at all
    md, event = _run_reflection(tmp_path, monkeypatch)
    assert "## Pre-registered experiments" in md
    assert "No pre-registered experiment for this window." in md
    assert event["preregistration"] == []
    # existing sections untouched
    assert "## Proposed tweaks" in md
    assert "## By symbol" in md


def test_reflection_prereg_but_no_variant_data(tmp_path, monkeypatch,
                                              in_window):
    # closes exist but none are tagged and none fall in-variant: the
    # section must say so honestly, never invent numbers.
    journal = [close(301, 25.0, in_window)]  # untagged
    write_state(tmp_path, journal, filled_template(name="quiet-window"))
    md, event = _run_reflection(tmp_path, monkeypatch)
    assert "no variant data journaled yet" in md
    block = event["preregistration"][0]
    assert block["variants"]["baseline"]["data"] == "none"
    assert block["variants"]["baseline"]["trades"] == 0
    assert block["variants"]["cooldown-6h"]["data"] == "none"


def test_reflection_flags_inconclusive_and_loser(tmp_path, monkeypatch,
                                                in_window):
    # min_trades=20 in the fixture header: 2 trades -> inconclusive.
    # A losing variant is still published (a result, not a failure).
    journal = [
        close(401, -100.0, in_window, variant="baseline"),
        close(402, -100.0, in_window, variant="baseline"),
        close(403, -50.0, in_window, variant="cooldown-6h"),
    ]
    write_state(tmp_path, journal, filled_template(name="losing-window"))
    md, event = _run_reflection(tmp_path, monkeypatch)
    assert "INCONCLUSIVE" in md
    assert "-200.00" in md  # loser published, not hidden
    assert event["preregistration"][0]["variants"]["baseline"]["status"] == \
        "inconclusive"


def test_reflection_no_closes_still_shows_prereg_section(tmp_path,
                                                        monkeypatch):
    write_state(tmp_path, [], filled_template(name="early-window"))
    monkeypatch.setattr(wr, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(wr, "JOURNAL_PATH",
                        str(tmp_path / "nova_journal.jsonl"))
    assert wr.main() == 0
    md = (tmp_path / f"reflection_{date.today().isoformat()}.md").read_text(
        encoding="utf-8")
    assert "no closed trades yet" in md
    assert "## Pre-registered experiments" in md


# ------------------------------------------------- charter append intact

def test_charter_append_did_not_corrupt():
    with open(CHARTER, encoding="utf-8") as f:
        text = f.read()
    # original charter still intact
    assert text.startswith("# 30-Day Autonomous Demo Trading Experiment — Charter")
    for n in range(1, 10):
        assert f"## {n}." in text, f"original section {n} missing"
    assert "Only Zenas re-enables trading after a trip." in text
    assert "kill_switch.tripped" in text
    # the new protocol section is an APPEND at the end
    assert "## 10. Experiment pre-registration protocol" in text
    assert "WIN RATE ALONE DECIDES NOTHING" in text
    assert "Treat every demo dollar as real money" in text
    assert "run/prereg_<name>.md" in text
    # appended, not interleaved: §10 comes after §9's hard constraints
    assert text.index("## 10.") > text.index("## 9. Hard constraints")
    # 2026-10-18: the continuous-evolution loop section was appended after
    # the pre-registration protocol (still append-only, still at the end).
    assert "## Continuous evolution loop (2026-10-18)" in text
    assert text.index("## Continuous evolution loop") > text.index("## 10.")
    assert "python3 rollback.py --previous" in text
    assert text.rstrip().endswith("and\nZenas's approval rights stand.")
