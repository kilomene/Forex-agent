#!/usr/bin/env python3
"""Permanent regression check: lesson P&L claims must match journal-verified
broker close P&L.

Root cause it guards (2026-09-23, TRACK2): several trade lessons transcribed
the close P&L with the leading hundreds digit dropped (e.g. journal net
-526.26 was written as -26.26), understating losses ~8x-32x. The qualitative
analysis in those lessons was computed from the true figures, so only the
transcribed numbers were wrong.

Usage:
    python3 check_lesson_pnl.py [--db PATH] [--journal PATH]

Exit 0 when every closed-trade lesson's stated P&L matches the verified
journal figure within $0.015 and every journal close has a lesson row.
Exit 1 otherwise (prints every divergence).

Library use:
    from check_lesson_pnl import extract_claimed_pnl, verified_close_pnl, check_lessons
"""
import json
import os
import re
import sqlite3
import sys

BRIDGE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = "/home/hatch/workspace/mt5/data/trade_learning.db"
DEFAULT_JOURNAL = os.path.join(BRIDGE, "run", "nova_journal.jsonl")

TOLERANCE = 0.015  # cents: figures are rounded to 2dp

# Ordered patterns that extract a *stated close P&L* from lesson prose.
# Each has exactly one capture group holding the numeric figure.
# All observed formats in the live lessons are covered:
#   "Realized -26.26" / "Realized loss (-26.26)" / "Realized net -2016.15"
#   "+10,845.73 banked" / "+227.75 banked"
#   "loss (-26.26)" / "Loss -$137.98 vs $100 planned risk"
#   "broker close, -$197.49" / "Stopped out ... for -$107.34" /
#   "Scratched at 3792.25 (+0.08)" / "full -1R honored: -9.91" /
#   "SL hit at 0.9392 after ~37 minutes (-105.08, ...)"
#   "exit = entry 3803.62 -> 0.0" / "-> +20,162.64 banked"
_PNL_PATTERNS = [
    re.compile(r"realized\s+(?:net\s+|loss\s+)?\(?(-?\$?[\d,]+\.\d{2})\)?", re.I),
    re.compile(r"([+-]\$?[\d,]+\.\d{2})\s+banked", re.I),
    re.compile(r"loss\s+\(?(-?\$?[\d,]+\.\d{2})\)?", re.I),
    re.compile(
        r"(?:close|closed|stopped|hit|scratched|exit|exited|honored)"
        r"[\s\S]{0,80}?([+-]\$?[\d,]+\.\d{2})",
        re.I,
    ),
    re.compile(r"->\s*\(?(-?\$?[\d,]*\.\d{1,2})\)?(?!\d)"),
    re.compile(r"\(\s*(-\$(?:[\d,]+\.\d{2}))"),
    # "Stopped out ... for -$107.34" / "for +$192.04" — in these lessons a
    # signed figure after "for" is always the realized close P&L.
    re.compile(r"\bfor\s+([+-]\$?[\d,]+\.\d{2})", re.I),
]

# A candidate sitting next to the word "swap" is a swap component, not the P&L.
_SWAP_NEAR = re.compile(r"swap", re.I)
# A candidate inside a "(prior ...)" parenthetical refers to a *different*
# trade's P&L (e.g. "(prior EURGBP SELL -102.22 ...)"), not this lesson's.
_PRIOR_PAREN = re.compile(r"\(prior[^)]*$", re.I)


def _to_float(token):
    return float(token.replace("$", "").replace(",", ""))


def extract_claimed_pnl(text):
    """Return the sorted unique list of P&L figures the lesson text claims.

    Only signed figures (or figures after '->') in P&L phrasing are picked
    up, so entry/SL/TP prices, RSI, lot sizes, ATR and planned-risk mentions
    ("~$100 plan") are not treated as P&L claims. Swap components are
    excluded via the swap guard.
    """
    found = []
    text = text or ""
    for pat in _PNL_PATTERNS:
        for m in pat.finditer(text):
            start, end = m.span(1)
            context = text[max(0, start - 12): end + 12]
            if _SWAP_NEAR.search(context):
                continue
            if _PRIOR_PAREN.search(text[max(0, start - 60): start]):
                continue
            try:
                found.append(round(_to_float(m.group(1)), 2))
            except ValueError:
                continue
    return sorted(set(found))


def verified_close_pnl(journal_path):
    """Map ticket -> verified close P&L from the journal.

    Prefers net_profit (broker-verified, includes commission+swap) and falls
    back to profit. For tickets closed more than once, the last event carrying
    a non-null profit wins (provisional reconciled lines carry profit=None).
    trade.close_corrected events supersede the trade.closed they name.
    """
    pnl = {}       # ticket -> figure (last non-null-profit trade.closed wins)
    corrected = {}  # ticket -> figure (trade.close_corrected supersedes)
    with open(journal_path) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            typ = ev.get("type")
            if typ not in ("trade.closed", "trade.close_corrected"):
                continue
            ticket = ev.get("ticket")
            if ticket is None:
                continue
            figure = ev.get("net_profit")
            if figure is None:
                figure = ev.get("profit")
            if figure is None:
                continue
            if typ == "trade.close_corrected":
                corrected[str(ticket)] = round(float(figure), 2)
            else:
                pnl[str(ticket)] = round(float(figure), 2)
    pnl.update(corrected)
    return pnl


def _lesson_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ref, kind, why_entered, why_exited, what_worked, "
        "what_failed, what_to_change FROM lessons"
    ).fetchall()
    conn.close()
    return rows


def check_lessons(db_path=DEFAULT_DB, journal_path=DEFAULT_JOURNAL):
    """Run the full integrity check. Returns a dict of findings."""
    verified = verified_close_pnl(journal_path)
    rows = _lesson_rows(db_path)
    by_ref = {str(r["ref"]): r for r in rows}

    missing_lessons = sorted(t for t in verified if t not in by_ref)
    orphan_lessons = sorted(r for r in by_ref if r not in verified)

    mismatches = []      # (ref, claimed_list, verified)
    missing_claims = []  # refs whose text states no P&L at all
    for ref in sorted(by_ref):
        if ref not in verified:
            continue
        text = " ".join(str(by_ref[ref][c] or "") for c in
                        ("why_entered", "why_exited", "what_worked",
                         "what_failed", "what_to_change"))
        claims = extract_claimed_pnl(text)
        if not claims:
            missing_claims.append(ref)
            continue
        if any(abs(c - verified[ref]) > TOLERANCE for c in claims):
            mismatches.append((ref, claims, verified[ref]))

    return {
        "verified": verified,
        "missing_lessons": missing_lessons,
        "orphan_lessons": orphan_lessons,
        "mismatches": mismatches,
        "missing_claims": missing_claims,
    }


def main(argv):
    db_path = DEFAULT_DB
    journal_path = DEFAULT_JOURNAL
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--db" and args:
            db_path = args.pop(0)
        elif a == "--journal" and args:
            journal_path = args.pop(0)
        else:
            print(f"unknown arg: {a}", file=sys.stderr)
            return 2

    res = check_lessons(db_path, journal_path)
    problems = 0

    for ref, claims, want in res["mismatches"]:
        problems += 1
        print(f"MISMATCH ticket {ref}: lesson claims {claims} "
              f"but journal-verified P&L is {want:+.2f}")
    for ref in res["missing_claims"]:
        problems += 1
        print(f"NO_PNL_CLAIM ticket {ref}: lesson states no P&L figure, "
              f"journal-verified P&L is {res['verified'][ref]:+.2f}")
    for ref in res["missing_lessons"]:
        problems += 1
        print(f"MISSING_LESSON ticket {ref}: journal has a verified close "
              f"({res['verified'][ref]:+.2f}) but no lesson row exists")
    for ref in res["orphan_lessons"]:
        print(f"WARNING orphan lesson {ref}: no journal close found "
              f"(ok if the trade is still open)")

    ok = len(res["verified"])
    print(f"checked {ok} closed tickets: "
          f"{problems} problem(s), {len(res['orphan_lessons'])} orphan warning(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
