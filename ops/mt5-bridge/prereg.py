#!/usr/bin/env python3
"""
Experiment pre-registration protocol (Hummingbot-style honest lab book).

A pre-registration file lives at ``run/prereg_<name>.md`` (see
``experiment_preregistration_template.md``) and is filled in BEFORE its
window opens. The machine-readable header is an HTML comment so the
rendered markdown stays clean::

    <!-- PREREG
    name: cooldown-6h
    window_start: 2026-10-26
    window_end: 2026-11-02
    variants: baseline, cooldown-6h
    min_trades: 20
    stop_drawdown_usd: 500
    baseline_expectancy: -12.40
    config_snapshot: run/risk_config.json @ 2026-10-25 (sha256:abc123...)
    -->

weekend_reflection.py imports this module: it finds prereg files whose
window covers the reflection date, groups the window's deduped closes by
the ``variant`` tag journaled on the close event (falling back to the
linked trade.decision's tag), and publishes per-variant P&L -- winners
AND losers. No variant data journaled yet is reported honestly, never
invented.

The prereg file is FROZEN at window start. This module never writes to
prereg files and never touches trading state.
"""

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import performance  # noqa: E402  (dedup rule + honest metrics, single impl)

BASE = os.path.dirname(os.path.abspath(__file__))

# Keys the machine header must carry. Optional keys may be absent.
REQUIRED_KEYS = ("name", "window_start", "window_end", "variants")
OPTIONAL_KEYS = ("min_trades", "stop_drawdown_usd", "baseline_expectancy",
                 "config_snapshot")
INT_KEYS = ("min_trades",)
FLOAT_KEYS = ("stop_drawdown_usd", "baseline_expectancy")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HEADER_RE = re.compile(r"<!--\s*PREREG\s*\n(.*?)-->", re.DOTALL)

UNTAGGED = "untagged"  # closes inside the window with no variant tag


def parse_header(text):
    """Parse the <!-- PREREG ... --> machine header.

    Returns a dict with string values (numbers kept as strings; see
    validate/coerce) or None when no header block is present.
    """
    m = HEADER_RE.search(text or "")
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key in REQUIRED_KEYS + OPTIONAL_KEYS and value:
            out[key] = value
    return out or None


def validate(text):
    """Validate a filled prereg file. Returns a list of error strings.

    Empty list == valid. Checks: header present, all required keys,
    date format + start <= end, non-empty variant list, numeric fields
    parse, and the required human sections exist (idea, stop conditions,
    metrics, variants, real-money standard).
    """
    errors = []
    header = parse_header(text)
    if header is None:
        return ["missing <!-- PREREG ... --> machine header"]
    for key in REQUIRED_KEYS:
        if key not in header:
            errors.append(f"header missing required key: {key}")
    for key in INT_KEYS:
        if key in header:
            try:
                v = int(header[key])
                if v < 0:
                    errors.append(f"header key {key} must be >= 0")
            except ValueError:
                errors.append(f"header key {key} must be an integer")
    for key in FLOAT_KEYS:
        if key in header:
            try:
                float(header[key])
            except ValueError:
                errors.append(f"header key {key} must be a number")
    for key in ("window_start", "window_end"):
        if key in header and not DATE_RE.match(header[key]):
            errors.append(f"header key {key} must be YYYY-MM-DD")
    if DATE_RE.match(header.get("window_start", "")) and \
            DATE_RE.match(header.get("window_end", "")):
        if header["window_start"] > header["window_end"]:
            errors.append("window_start must be <= window_end")
    variants = [v.strip() for v in header.get("variants", "").split(",")
                if v.strip()]
    if "variants" in header and not variants:
        errors.append("variants list is empty")
    # required human sections (headings from the template)
    lowered = (text or "").lower()
    for section in ("idea under test", "stop conditions",
                    "metrics that decide", "variants",
                    "real-money standard"):
        if section not in lowered:
            errors.append(f"missing required section: {section!r}")
    if "win rate alone decides nothing" not in lowered:
        errors.append("must state explicitly: WIN RATE ALONE DECIDES NOTHING")
    return errors


def prereg_paths(state_dir):
    """All prereg files in the state dir, sorted by name."""
    return sorted(glob.glob(os.path.join(state_dir, "prereg_*.md")))


def load_active(state_dir, today):
    """(path, header) for every prereg whose window covers `today`
    (YYYY-MM-DD). Files with an unparseable/invalid header are skipped --
    a broken prereg must never silently shape a reflection."""
    active = []
    for path in prereg_paths(state_dir):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if validate(text):
            continue
        header = parse_header(text)
        if header["window_start"] <= today <= header["window_end"]:
            active.append((path, header))
    return active


def _variant_of(close, decisions):
    """Variant tag for a close: its own tag, else the linked decision's."""
    tag = (close.get("variant") or "").strip()
    if tag:
        return tag
    d = decisions.get(close.get("signal_id"), {})
    return (d.get("variant") or "").strip() or UNTAGGED


def collect_variant_stats(journal, header):
    """Per-variant stats for the prereg window.

    Groups the window's deduped closes (broker-exit-time attribution, the
    same rule as every other reflection stat) by variant tag. Returns
    (variants, untagged_trades) where variants maps each pre-registered
    variant name to a JSON-safe stats dict; pre-registered variants with
    no journaled trades get ``trades: 0`` and ``data: "none"`` -- the
    caller reports "no variant data journaled yet" honestly.

    Extra journaled tags not listed in the header are reported under their
    own name so nothing is hidden; closes with no tag at all count as
    untagged.
    """
    closes, _ = performance.dedupe_closes(journal)
    decisions = {}
    for e in journal:
        if e.get("type") == "trade.decision" and e.get("signal_id"):
            decisions[e["signal_id"]] = e

    start, end = header["window_start"], header["window_end"]
    by_variant = {}
    untagged = 0
    for e in closes:
        day = performance.close_date(e)
        if not (start <= day <= end):
            continue
        tag = _variant_of(e, decisions)
        if tag == UNTAGGED:
            untagged += 1
            continue
        by_variant.setdefault(tag, []).append(
            performance.effective_profit(e))

    registered = [v.strip() for v in header["variants"].split(",")
                  if v.strip()]
    min_trades = int(header["min_trades"]) if header.get("min_trades") \
        else 0
    stop_dd = float(header["stop_drawdown_usd"]) \
        if header.get("stop_drawdown_usd") else None

    variants = {}
    for name in registered + [t for t in by_variant if t not in registered]:
        profits = by_variant.get(name, [])
        if not profits:
            variants[name] = {"trades": 0, "data": "none"}
            continue
        m = performance.compute_metrics(profits)
        wins = sum(1 for p in profits if p > 0)
        status = "ok"
        notes = []
        if min_trades and len(profits) < min_trades:
            status = "inconclusive"
            notes.append(f"fewer than {min_trades} trades so far -- "
                         "inconclusive, not promising")
        dd = m["max_drawdown"]["max_drawdown"]
        if stop_dd is not None and dd > stop_dd:
            status = "stop_tripped"
            notes.append(f"window drawdown {dd:.2f} exceeds stop "
                         f"{stop_dd:.2f}")
        variants[name] = {
            "trades": len(profits),
            "wins": wins,
            "total_pnl": round(sum(profits), 2),
            "expectancy": round(m["expectancy"], 2),
            "sharpe_ratio": m["sharpe_ratio"],
            "profit_factor": m["profit_factor"],
            "max_drawdown": round(dd, 2),
            "status": status,
            "notes": notes,
            "data": "present",
            "registered": name in registered,
        }
    return variants, untagged


def journal_block(name, header, variants, untagged):
    """JSON-safe block for the reflection journal event."""
    block = {
        "name": name,
        "window_start": header["window_start"],
        "window_end": header["window_end"],
        "min_trades": int(header["min_trades"]) if header.get("min_trades")
        else None,
        "stop_drawdown_usd": float(header["stop_drawdown_usd"])
        if header.get("stop_drawdown_usd") else None,
        "baseline_expectancy": float(header["baseline_expectancy"])
        if header.get("baseline_expectancy") else None,
        "variants": variants,
        "untagged_trades_in_window": untagged,
    }
    return block


def render_md_lines(name, header, variants, untagged):
    """Markdown lines for the reflection's pre-registered experiments
    section. Never invents numbers: variants with no data say so."""
    lines = [
        f"- `{name}` (window {header['window_start']} -> "
        f"{header['window_end']}): {len([v for v in variants.values() if v.get('registered', True)])} "
        f"variant(s) pre-registered.",
    ]
    for vname, v in variants.items():
        tag = "" if v.get("registered", True) else " (not pre-registered)"
        if v.get("data") == "none":
            lines.append(f"  - {vname}{tag}: no variant data journaled yet.")
            continue
        pf = (f"{v['profit_factor']:.2f}"
              if v["profit_factor"] is not None else "n/a")
        line = (f"  - {vname}{tag}: {v['trades']} trades, "
                f"{v['total_pnl']:+.2f} total, expectancy "
                f"{v['expectancy']:+.2f}/trade, Sharpe "
                f"{v['sharpe_ratio']:+.2f}, PF {pf}, maxDD "
                f"{v['max_drawdown']:.2f}")
        if v["status"] == "inconclusive":
            line += " -- INCONCLUSIVE (below min_trades)"
        elif v["status"] == "stop_tripped":
            line += " -- STOP TRIPPED (drawdown stop)"
        if v["notes"]:
            line += " [" + "; ".join(v["notes"]) + "]"
        lines.append(line)
    if untagged:
        lines.append(f"  - {untagged} window trade(s) carry no variant tag "
                     f"(excluded from variant P&L).")
    if header.get("baseline_expectancy"):
        lines.append(f"  - Baseline expectancy (pre-window): "
                     f"{float(header['baseline_expectancy']):+.2f}/trade. "
                     f"WIN RATE ALONE DECIDES NOTHING -- the deciding "
                     f"metrics are Sharpe, max drawdown, profit factor, "
                     f"and expectancy vs this baseline.")
    return lines
