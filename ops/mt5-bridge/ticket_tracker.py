#!/usr/bin/env python3
"""Defense-in-depth verifier for the EA-owned nova_positions_seen.json.

The seen-ticket file is OWNED AND WRITTEN BY THE NovaTrader EA
(SaveSeenTickets / LoadSeenTickets in NovaTrader.mq5). This module never
writes it -- it only READS and VERIFIES it against the durable open/close
history (EA nova_trades.jsonl + executor nova_journal.jsonl), so a silent
ticket loss is caught by an independent Python check instead of going
unnoticed.

Why this exists (2026-09-23, issues 1+2): the EA's loader used to
`break` on the first malformed entry and its parser required the exact
literal "ticket": -- one bad entry dropped every later ticket from
memory, and the next wholesale save deleted them from the file
permanently. The EA source is fixed; this module is the independent
watchdog that proves the file still parses and still matches reality.

WIRING (repair round 2, 2026-09-24): supervise.py calls
audit_seen_file() on its 5-minute tick and logs SEEN-AUDIT lines to
run/supervisor.log -- read-only, never writes the EA file. A finding
(verify["ok"] False or corrupt spans) is LOUD in the log; journaling the
finding as a durable event is the documented next step and happens only
on a Zenas-authorized restart, when the executor itself can own the
journal write. It must NEVER write nova_positions_seen.json itself --
the EA owns that file.

Pure/testable core; no MT5, no network, no writes to the EA file.
"""

import json
import os
import re
import sys

BRIDGE = os.path.dirname(os.path.abspath(__file__))
if BRIDGE not in sys.path:
    sys.path.insert(0, BRIDGE)

try:
    from reconcile import rebuild_open_close_sets
except ImportError:  # pragma: no cover - hermetic tests stub this
    rebuild_open_close_sets = None

_TICKET_RE = re.compile(r'"ticket"\s*:\s*(\d+)')
_FIELD_RES = {
    "symbol": re.compile(r'"symbol"\s*:\s*"([^"]*)"'),
    "direction": re.compile(r'"(?:type|direction)"\s*:\s*"([^"]*)"'),
    "volume": re.compile(r'"volume"\s*:\s*([0-9.]+)'),
    "tracked_at": re.compile(r'"tracked_at"\s*:\s*(\d+)'),
    "missing_since": re.compile(r'"missing_since"\s*:\s*(\d+)'),
}


def _entry_fields(span):
    """Extract ticket fields from one entry's text span, whitespace-tolerant."""
    m = _TICKET_RE.search(span)
    if not m:
        return None
    entry = {"ticket": int(m.group(1)), "raw": span.strip()[:400]}
    for name, rx in _FIELD_RES.items():
        fm = rx.search(span)
        if fm:
            val = fm.group(1)
            if name in ("tracked_at", "missing_since"):
                entry[name] = int(val)
            elif name == "volume":
                try:
                    entry[name] = float(val)
                except ValueError:
                    entry[name] = None
            else:
                entry[name] = val
        else:
            entry[name] = None
    return entry


def parse_seen_file(path):
    """Whitespace-tolerantly parse the EA's nova_positions_seen.json.

    Returns {"entries": [...], "corrupt": [...], "tickets": [...]}.
    "entries" are parsed ticket dicts; "corrupt" are text spans that
    contain no parseable ticket key; "tickets" is the ticket id list.
    A completely unreadable / missing file yields empty entries and a
    single corrupt note -- never raises for content reasons.
    """
    result = {"entries": [], "corrupt": [], "tickets": []}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError as e:
        result["corrupt"].append(f"<unreadable file: {e}>")
        return result
    if not txt.strip():
        return result
    # Fast path: clean JSON parses as-is.
    try:
        doc = json.loads(txt)
        # Repair round 2 (2026-09-24): the EA writes {"tickets": [...]};
        # ANY other schema (e.g. {"seen": [...]}, a bare list) must fail
        # LOUDLY. The old code did doc.get("tickets", []) and silently
        # returned zero tickets on schema drift -- indistinguishable from
        # "no positions tracked".
        if isinstance(doc, dict) and isinstance(doc.get("tickets"), list):
            items = doc["tickets"]
        else:
            keys = (sorted(doc.keys()) if isinstance(doc, dict)
                    else type(doc).__name__)
            result["corrupt"].append(
                f"<unrecognized seen-file schema: {keys}>")
            return result
        for it in items:
            if isinstance(it, dict) and it.get("ticket") is not None:
                result["entries"].append({
                    "ticket": int(it["ticket"]),
                    "symbol": it.get("symbol"),
                    "direction": it.get("type") or it.get("direction"),
                    "volume": it.get("volume"),
                    "tracked_at": it.get("tracked_at"),
                    "missing_since": it.get("missing_since"),
                    "raw": json.dumps(it)[:400],
                })
                result["tickets"].append(int(it["ticket"]))
            else:
                result["corrupt"].append(json.dumps(it)[:200])
        return result
    except ValueError:
        pass
    # Tolerant path: scan ticket-key spans (mirrors the EA's fixed loop).
    spans = []
    matches = list(_TICKET_RE.finditer(txt))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(txt)
        spans.append(txt[m.start():end])
    if not spans and txt.strip():
        result["corrupt"].append(txt.strip()[:200])
        return result
    for span in spans:
        entry = _entry_fields(span)
        if entry is None:
            result["corrupt"].append(span.strip()[:200])
        else:
            result["entries"].append(entry)
            result["tickets"].append(entry["ticket"])
    return result


def verify_seen_against_history(seen_tickets, open_tickets, closed_tickets):
    """Cross-check seen tickets against durable open/close history sets.

    All arguments are iterables of ticket ids (ints or numeric strings).
    Returns a JSON-safe report dict. Findings:
      untracked_open    - open per durable history but absent from seen file
      tracked_but_closed - in seen file but closed per durable history
      duplicate_tickets - ticket appears >1x in the seen file
    """
    def norm(ids):
        out = set()
        for t in ids:
            try:
                out.add(int(t))
            except (TypeError, ValueError):
                continue
        return out

    seen = norm(seen_tickets)
    opens = norm(open_tickets)
    closed = norm(closed_tickets)
    counts = {}
    for t in seen_tickets:
        try:
            t = int(t)
        except (TypeError, ValueError):
            continue
        counts[t] = counts.get(t, 0) + 1
    report = {
        "seen_count": len(seen),
        "open_count": len(opens),
        "closed_count": len(closed),
        "untracked_open": sorted(opens - seen - closed),
        "tracked_but_closed": sorted(seen & closed),
        "duplicate_tickets": sorted(t for t, c in counts.items() if c > 1),
    }
    report["ok"] = not (report["untracked_open"]
                        or report["tracked_but_closed"]
                        or report["duplicate_tickets"])
    return report


def audit_seen_file(seen_path, trades_path, journal_path):
    """Full audit: parse the EA seen file, rebuild durable history sets,
    and cross-check. Returns {"parsed": ..., "history": ..., "verify": ...}.
    Read-only: never writes any file."""
    if rebuild_open_close_sets is None:  # pragma: no cover
        raise RuntimeError("reconcile.rebuild_open_close_sets unavailable")
    parsed = parse_seen_file(seen_path)
    history = rebuild_open_close_sets(trades_path, journal_path)
    verify = verify_seen_against_history(
        parsed["tickets"], history["open_tickets"], history["closed_tickets"])
    verify["corrupt_spans"] = len(parsed["corrupt"])
    if parsed["corrupt"]:
        verify["ok"] = False
    return {"parsed": parsed, "history": {
        "open_tickets": history["open_tickets"],
        "closed_tickets": history["closed_tickets"],
        "phantoms": history["phantoms"],
        "mirror_gaps": history["mirror_gaps"],
    }, "verify": verify}
