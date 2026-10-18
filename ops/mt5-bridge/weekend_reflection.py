#!/usr/bin/env python3
"""
Nova weekend reflection (Sat + Sun 10:00 PT crons) for the 30-day experiment.

Reads run/nova_journal.jsonl, computes win rate by symbol / direction /
session / weekday, avg win, avg loss, expectancy, and streaks. Writes
`reflection` records to the journal and a proposal file
run/reflection_<YYYY-MM-DD>.md. Proposals are FOR APPROVAL ONLY --
this script never changes strategy or config.

Env:
  TRADER_STATE_DIR  state dir holding nova_journal.jsonl (default: <bridge dir>/run)
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily_summary  # noqa: E402  (per-ticket dedup rule, repair round 2)
import performance  # noqa: E402  (honest performance metrics, phase 1)
import prereg  # noqa: E402  (experiment pre-registration protocol, phase 3)

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))
JOURNAL_PATH = os.path.join(STATE_DIR, "nova_journal.jsonl")


def load_journal():
    out = []
    try:
        with open(JOURNAL_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def dedup_closed(journal):
    """One event per broker ticket (the honest close count).

    Delegates to performance.dedupe_closes (the single implementation of
    the repair-round-2 rule): a trade.close_corrected revision supersedes
    that ticket's provisional trade.closed REGARDLESS of journal order;
    a provisional close only replaces another provisional; closes with no
    ticket are kept as their own events. Returns (unique_events,
    raw_event_count).

    Without this, the raw journal's ~10x duplication (mirrored +
    reconciled close events) inflates every headline stat.
    """
    return performance.dedupe_closes(journal)


def stats(profits):
    """Per-group win/loss stats.

    profit_factor is None (not inf) when there are no losses, so every
    journaled reflection event stays valid strict JSON -- the same
    convention as performance.profit_factor.
    """
    n = len(profits)
    wins = sum(1 for p in profits if p > 0)
    gross_w = sum(p for p in profits if p > 0)
    gross_l = -sum(p for p in profits if p < 0)
    return {
        "trades": n, "wins": wins,
        "win_rate": wins / n * 100 if n else 0.0,
        "avg_win": gross_w / wins if wins else 0.0,
        "avg_loss": -(gross_l / (n - wins)) if n - wins else 0.0,
        "expectancy": sum(profits) / n if n else 0.0,
        "total": sum(profits),
        "profit_factor": gross_w / gross_l if gross_l > 0 else None,
    }


def _prereg_md_lines(today, journal):
    """Markdown lines for the 'Pre-registered experiments' section.

    Returns (lines, journal_blocks). With no active prereg file the
    section honestly states there is none; with an active file but no
    variant data yet, prereg.render_md_lines says so per variant --
    numbers are never invented.
    """
    lines = ["## Pre-registered experiments"]
    blocks = []
    active = prereg.load_active(STATE_DIR, today)
    if not active:
        lines.append("No pre-registered experiment for this window.")
        lines.append("")
        return lines, blocks
    for path, header in active:
        name = header["name"]
        variants, untagged = prereg.collect_variant_stats(journal, header)
        blocks.append(prereg.journal_block(name, header, variants,
                                           untagged))
        lines.extend(prereg.render_md_lines(name, header, variants,
                                            untagged))
    lines.append("")
    return lines, blocks


def _dup_line(dup):
    """One-line duplication-guard status for the reflection .md file."""
    if dup["flagged"]:
        return (f"Duplication check: **FLAGGED** -- {dup['reason']}. "
                f"Raw journal shows {dup['raw_close_events']} close events "
                f"totalling {dup['raw_total_pnl']:+.2f}; deduped to "
                f"{dup['deduped_close_events']} unique trades totalling "
                f"{dup['deduped_total_pnl']:+.2f}. Trust the deduped total.")
    return (f"Duplication check: clean -- {dup['raw_close_events']} close "
            f"events dedupe to {dup['deduped_close_events']} unique trades "
            f"({dup['raw_total_pnl']:+.2f} vs {dup['deduped_total_pnl']:+.2f}).")


def main():
    today = datetime.now().date().isoformat()
    journal = load_journal()
    closes, raw_close_events = dedup_closed(journal)

    decisions = {}
    for e in journal:
        if e.get("type") == "trade.decision" and e.get("signal_id"):
            decisions[e["signal_id"]] = e

    if not closes:
        print("no closed trades yet -- nothing to reflect on")
        prereg_lines, _ = _prereg_md_lines(today, journal)
        with open(os.path.join(STATE_DIR, f"reflection_{today}.md"), "w") as f:
            f.write(f"# Weekend reflection {today}\n\nno closed trades yet.\n\n"
                    + "\n".join(prereg_lines))
        return 0

    # enrich closes with symbol/direction/session/weekday from the decision
    enriched = []
    for e in closes:
        d = decisions.get(e.get("signal_id"), {})
        ctx = d.get("context", {}) or {}
        # net_profit (gross + commission + swap) wins over bare profit --
        # same rule as the daily summary and the executor's gate accounting.
        profit = daily_summary._eff_profit(e)
        enriched.append({
            "symbol": e.get("symbol") or d.get("symbol") or "?",
            "direction": e.get("direction") or d.get("direction") or "?",
            "session": ctx.get("session") or "?",
            "weekday": ctx.get("weekday") or "?",
            "profit": profit,
            "time": str(e.get("time", "")),
        })
    enriched.sort(key=lambda r: r["time"])

    by_symbol = defaultdict(list)
    by_dir = defaultdict(list)
    by_session = defaultdict(list)
    by_weekday = defaultdict(list)
    for r in enriched:
        by_symbol[r["symbol"]].append(r["profit"])
        by_dir[r["direction"]].append(r["profit"])
        by_session[r["session"]].append(r["profit"])
        by_weekday[r["weekday"]].append(r["profit"])

    # streaks (chronological)
    best_win = worst_loss = cur = 0
    cw = cl = 0
    for r in enriched:
        if r["profit"] > 0:
            cw += 1
            cl = 0
            best_win = max(best_win, cw)
        elif r["profit"] < 0:
            cl += 1
            cw = 0
            worst_loss = max(worst_loss, cl)

    overall = stats([r["profit"] for r in enriched])

    # Honest performance block (Freqtrade-style report, phase 1): metrics
    # over the deduped, time-ordered per-trade series, the by-construction
    # duplication guard, and the passive-market benchmark. `enriched` is
    # already sorted chronologically, which the drawdown needs.
    perf = performance.compute_metrics([r["profit"] for r in enriched])
    dup = performance.duplication_check(journal)
    bench = performance.benchmark_comparison(journal)
    sym_perf = {k: performance.compute_metrics(v)
               for k, v in by_symbol.items()}

    # Pre-registered experiments (phase 3): computed BEFORE the journal
    # write so the reflection event carries the same block the .md shows.
    prereg_lines, prereg_blocks = _prereg_md_lines(today, journal)

    reflection = {
        "type": "reflection",
        "date": today,
        "overall": overall,
        "performance": perf,
        "duplication_check": dup,
        "benchmark": bench,
        "preregistration": prereg_blocks,  # [] when no prereg is active
        "by_symbol": {k: stats(v) for k, v in sorted(by_symbol.items())},
        "by_direction": {k: stats(v) for k, v in sorted(by_dir.items())},
        "by_session": {k: stats(v) for k, v in sorted(by_session.items())},
        "by_weekday": {k: stats(v) for k, v in sorted(by_weekday.items())},
        "longest_win_streak": best_win,
        "longest_loss_streak": worst_loss,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps(reflection) + "\n")

    # proposal file (approval only)
    dd = perf["max_drawdown"]
    pf_s = f"{perf['profit_factor']:.2f}" if perf["profit_factor"] is not None else "n/a"
    sort_s = f"{perf['sortino_ratio']:+.2f}" if perf["sortino_ratio"] is not None else "n/a"
    lines = [f"# Weekend reflection — {today}", "",
             f"Closed trades so far: **{overall['trades']}**",
             f"({raw_close_events} journaled close events deduped to "
             f"{overall['trades']} unique trades by broker ticket)",
             f"Win rate: **{overall['win_rate']:.1f}%** | "
             f"Avg win: {overall['avg_win']:+.2f} | "
             f"Avg loss: {overall['avg_loss']:+.2f} | "
             f"Expectancy: {overall['expectancy']:+.2f}/trade | "
             f"Total: {overall['total']:+.2f}",
             f"Longest win streak: {best_win}, longest loss streak: {worst_loss}",
             f"Risk-adjusted (deduped, per-trade): Sharpe "
             f"{perf['sharpe_ratio']:+.2f} | Sortino {sort_s} | PF {pf_s} | "
             f"maxDD {dd['max_drawdown']:.2f} "
             f"(peak {dd['peak']:+.2f} -> trough {dd['trough']:+.2f})",
             _dup_line(dup),
             ""]
    lines.append("## By symbol")
    for sym, s in sorted(reflection["by_symbol"].items(),
                         key=lambda kv: kv[1]["total"]):
        sp = sym_perf[sym]
        spf = f"{sp['profit_factor']:.2f}" if sp["profit_factor"] is not None else "n/a"
        sdd = sp["max_drawdown"]["max_drawdown"]
        lines.append(f"- {sym}: {s['trades']} trades, {s['win_rate']:.1f}% win, "
                     f"{s['total']:+.2f}, Sharpe {sp['sharpe_ratio']:+.2f}, "
                     f"PF {spf}, maxDD {sdd:.2f}")
    lines.append("")
    lines.append("## Benchmark (passive long-only spot move, journaled prices)")
    lines.append("_Freqtrade `market_change` analog -- a rough market-context "
                 "number, not a target. The strategy trades both directions "
                 "and the journal is not a price feed; see performance.py "
                 "for the honest limits._")
    for sym, b in sorted(bench.items()):
        if b.get("benchmark_data") == "none":
            lines.append(f"- {sym}: no benchmark data")
        else:
            lines.append(f"- {sym}: {b['benchmark_return_pct']:+.3f}% "
                         f"({b['first_price']} -> {b['last_price']}, "
                         f"{b['n_price_points']} price points, "
                         f"{b['first_time']} -> {b['last_time']})")
    lines.append("")
    lines.extend(prereg_lines)
    lines.append("## By direction")
    for d, s in sorted(reflection["by_direction"].items()):
        lines.append(f"- {d}: {s['trades']} trades, {s['win_rate']:.1f}% win, "
                     f"{s['total']:+.2f}")
    lines.append("")
    lines.append("## By session (server time)")
    for sname, s in sorted(reflection["by_session"].items()):
        lines.append(f"- {sname}: {s['trades']} trades, {s['win_rate']:.1f}% "
                     f"win, {s['total']:+.2f}")
    lines.append("")
    lines.append("## By weekday")
    for wday, s in sorted(reflection["by_weekday"].items()):
        lines.append(f"- {wday}: {s['trades']} trades, {s['win_rate']:.1f}% "
                     f"win, {s['total']:+.2f}")
    lines.append("")
    lines.append("## Proposed tweaks (require Zenas's approval — never auto-applied)")
    n = 1
    weak = [k for k, s in reflection["by_symbol"].items()
            if s["trades"] >= 20 and s["win_rate"] < 35]
    for sym in weak:
        s = reflection["by_symbol"][sym]
        lines.append(f"{n}. Consider excluding {sym}: {s['win_rate']:.1f}% win "
                     f"rate over {s['trades']} trades.")
        n += 1
    bad_sess = [k for k, s in reflection["by_session"].items()
                if s["trades"] >= 10 and s["total"] < 0]
    for sname in bad_sess:
        s = reflection["by_session"][sname]
        lines.append(f"{n}. Consider skipping the {sname} session: "
                     f"{s['total']:+.2f} over {s['trades']} trades.")
        n += 1
    if worst_loss >= 5:
        lines.append(f"{n}. Longest losing streak is {worst_loss}: consider a "
                     f"consecutive-loss circuit breaker (e.g. pause after 4 "
                     f"straight losers).")
        n += 1
    if overall["avg_loss"] != 0 and \
            overall["avg_win"] / abs(overall["avg_loss"]) < 1.0:
        lines.append(f"{n}. Avg win ({overall['avg_win']:+.2f}) is smaller than "
                     f"avg loss ({overall['avg_loss']:+.2f}): review SL/TP "
                     f"placement.")
        n += 1
    if n == 1:
        lines.append("1. No changes proposed: everything within tolerance.")
    lines.append("")
    out_path = os.path.join(STATE_DIR, f"reflection_{today}.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {out_path} ({overall['trades']} closed trades reflected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
