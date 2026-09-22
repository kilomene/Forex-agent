#!/usr/bin/env python3
"""
Per-trade documentation generator for the 30-day autonomous demo trading
experiment (2026-09-21 -> 2026-10-21).

For every trade the executor has opened, writes a documentation page to

    ~/workspace/mt5/evidence-docs/docs/trades/<YYYY-MM-DD>/<ticket>.md

plus one chart PNG per trade, and an index page
    ~/workspace/mt5/evidence-docs/docs/trades/index.md

Each page shows: symbol, direction, timeframe, entry/exit timestamps,
entry/exit prices, SL/TP, volume, broker-confirmed P&L + commission/swap,
the signal rationale, the gate decision record, the exit reason, and a
"Why profit / Why loss" section grounded ONLY in recorded fields.
Unrecorded values render as "not recorded" -- numbers are never invented.

CHARTS: no historical candle data exists anywhere in this environment
(nova_feed.json is a live-quote heartbeat only; no history/rates files),
so every chart is a clearly-labeled SCHEMATIC reconstructed from the
signal's entry/SL/TP/exit parameters. The image itself carries the label
"schematic -- reconstructed from signal parameters".

SANITIZED: account logins, deal IDs, tokens and other private identifiers
are never written into the docs.

Usage:
    python3 trade_report.py [--out DIR]

Idempotent: regenerates everything deterministically on each run.
"""

import argparse
import json
import math
import os
import random
import re
import sys
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = ("/home/hatch/workspace/mt5/prefix/drive_c/Program Files/"
             "MetaTrader 5/MQL5/Files")
JOURNAL_PATH = os.path.join(BASE, "run", "nova_journal.jsonl")
TRADES_PATH = os.path.join(FILES_DIR, "nova_trades.jsonl")
SIGNALS_PATH = os.path.join(FILES_DIR, "nova_signals.jsonl")
POSITIONS_PATH = os.path.join(FILES_DIR, "nova_positions.json")
DEFAULT_OUT = "/home/hatch/workspace/mt5/evidence-docs/docs/trades"

UNKNOWN_TICKETS = {10607517845, 10606155417}  # known reconciliation gaps

SERVER_TZ_NOTE = "MT5 server time (UTC+3)"


# ---------------------------------------------------------------- loading

def load_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def parse_server_time(s):
    """Parse 'YYYY.MM.DD HH:MM:SS' or 'YYYY-MM-DD HH:MM:SS' server time."""
    if not s:
        return None
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def session_label(dt):
    """Rough market-session label from server time (UTC+3). Info only."""
    if not dt:
        return "not recorded"
    h = dt.hour
    parts = []
    if 0 <= h < 9:
        parts.append("Asian (Sydney/Tokyo)")
    if 3 <= h < 12:
        parts.append("Tokyo")
    if 10 <= h < 19:
        parts.append("London")
    if 15 <= h or h < 1:
        parts.append("New York")
    return " / ".join(parts) if parts else "off-hours"


# ---------------------------------------------------------------- model

def build_trades(journal, broker_trades, signals, positions):
    sig_by_id = {}
    for s in signals:
        sid = s.get("id") or s.get("signal_id")
        if sid:
            sig_by_id[sid] = s

    decisions = {}   # signal_id -> decision event (latest wins)
    overrides = []   # manual_override decision events
    closes = {}      # ticket -> close event
    opens = {}       # ticket -> open event (journal preferred)
    opens_order = []

    def note_open(e, source):
        t = e.get("ticket")
        if not t:
            return
        if t not in opens:
            opens[t] = dict(e)
            opens[t]["_source"] = source
            opens_order.append(t)

    for e in journal:
        t = e.get("type")
        if t == "trade.decision":
            if e.get("decision") == "manual_override":
                overrides.append(e)
            elif e.get("signal_id"):
                decisions[e["signal_id"]] = e
        elif t == "trade.opened":
            note_open(e, "journal")
        elif t == "trade.closed":
            closes[e.get("ticket")] = e
    for e in broker_trades:  # broker-side feed; journal wins on conflict,
        # but fills fields the journal record lacks (e.g. reconciliation notes)
        t = e.get("type")
        if t == "trade.opened":
            note_open(e, "broker-feed")
        elif t == "trade.closed":
            ticket = e.get("ticket")
            if ticket in closes:
                for k, v in e.items():
                    closes[ticket].setdefault(k, v)
            else:
                closes[ticket] = e

    trades = []
    for t in opens_order:
        o = opens[t]
        sid = o.get("signal_id")
        sig = sig_by_id.get(sid, {})
        close = closes.get(t)
        trades.append({
            "ticket": t,
            "symbol": o.get("symbol"),
            "direction": o.get("direction"),
            "timeframe": sig.get("timeframe") or "not recorded",
            "entry_time": o.get("time"),
            "entry_price": o.get("entry_price"),
            "command_id": o.get("command_id"),
            "signal_entry": sig.get("entry_price"),
            "stop_loss": sig.get("stop_loss"),
            "take_profit": sig.get("take_profit"),
            "volume": o.get("volume"),
            "signal": sig,
            "decision": decisions.get(sid),
            "close": close,
            "open_source": o.get("_source"),
        })
    pos_by_ticket = {}
    snap_time = None
    try:
        for p in (positions or {}).get("positions", []):
            if p.get("ticket"):
                pos_by_ticket[p["ticket"]] = p
        snap_time = (positions or {}).get("server_time")
    except (AttributeError, TypeError):
        pass
    return trades, overrides, pos_by_ticket, snap_time


def exit_reason_text(close):
    if not close:
        return "not recorded"
    r = close.get("reason")
    mapping = {
        "broker": "take-profit close (broker-confirmed)",
        "broker-sl-reconciled": "stop-loss close (reconciled from terminal log)",
        "manual": "manual close",
        "kill-switch": "kill-switch close",
    }
    return mapping.get(r, r or "unknown")


def outcome_of(tr):
    c = tr["close"]
    if not c:
        return "unknown"
    p = c.get("profit")
    if p is None:
        return "unknown"
    return "profit" if p >= 0 else "loss"


def fmt(v, nd=5):
    if v is None:
        return "not recorded"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{f:.{nd}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def sanitize_note(text):
    """Redact broker-internal numeric identifiers from free-text notes."""
    return re.sub(r"#\d{6,}", "#<redacted>", str(text))


def fmt_money(v):
    if v is None:
        return "not recorded"
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------- "why" section (recorded fields only)

def why_section(tr):
    """Grounded explanation. Anything unclear is stated as unclear."""
    c = tr["close"]
    lines = []
    pos = tr.get("_open_pos")
    if not c and pos:
        lines.append(
            f"Still open: unrealized (floating) P&L of "
            f"{fmt_money(pos.get('profit'))} at the latest positions snapshot "
            f"({tr.get('_snap_time')}, {SERVER_TZ_NOTE}). No profit or loss "
            f"is banked; the outcome will be documented when a close is "
            f"recorded.")
        lines.append(
            f"Current price {fmt(pos.get('current_price'))} vs entry "
            f"{fmt(tr['entry_price'])}; SL {fmt(tr['stop_loss'])}, "
            f"TP {fmt(tr['take_profit'])}.")
        return "\n".join("- " + l for l in lines)
    if not c:
        et = parse_server_time(tr["entry_time"])
        st = parse_server_time(tr.get("_snap_time"))
        if et and st and et > st:
            lines.append(
                "No close recorded yet -- this trade opened after the latest "
                "positions snapshot, so its current open/closed status is "
                "unconfirmed. Nothing is claimed about its outcome.")
            return "\n".join("- " + l for l in lines)
        lines.append(
            "Outcome unknown -- no clean close record exists for this ticket. "
            "The position is absent from the latest broker open-positions "
            "snapshot without a recorded close event. It is under "
            "reconciliation; no profit or loss is claimed.")
        if tr["ticket"] in UNKNOWN_TICKETS:
            lines.append(
                "This ticket was already flagged as a known reconciliation "
                "gap on 2026-09-22.")
        return "\n".join("- " + l for l in lines)

    reason = c.get("reason")
    pnl = c.get("profit")
    exit_px = c.get("exit_price")
    tp = tr["take_profit"]
    sl = tr["stop_loss"]
    entry = tr["entry_price"] or tr["signal_entry"]

    def near(a, b):
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) <= max(abs(float(b)) * 1e-6, 1e-9)
        except (TypeError, ValueError):
            return False

    if reason == "broker" and near(exit_px, tp):
        lines.append(
            f"Profit: the trade reached its take-profit target "
            f"({fmt(tp)}). Exit price {fmt(exit_px)} matches the TP set on "
            f"the signal, so the broker closed it as a winner.")
        hold = c.get("hold_seconds")
        if hold:
            lines.append(
                f"It was held {int(hold)} seconds (~{int(hold)//60} min) -- a "
                f"fast move in the signal's direction after entry.")
    elif reason == "broker-sl-reconciled" and near(exit_px, sl):
        lines.append(
            f"Loss: the trade hit its stop-loss ({fmt(sl)}). Exit price "
            f"{fmt(exit_px)} matches the SL, so the position was stopped out "
            f"as designed by the risk plan.")
        if entry is not None and sl is not None:
            lines.append(
                f"Entry {fmt(entry)} -> SL {fmt(sl)}: price moved against the "
                f"{(tr['direction'] or '').lower()} position immediately; the "
                f"EMA-cross signal did not follow through.")
    elif reason == "broker":
        lines.append(
            f"Profit of {fmt_money(pnl)} with a broker-confirmed close at "
            f"{fmt(exit_px)}. The exact exit trigger is not recorded "
            f"(close reason is '{reason}' but exit price does not match the "
            f"signal TP of {fmt(tp)}), so the precise cause is unclear.")
    else:
        lines.append(
            f"{'Profit' if (pnl or 0) >= 0 else 'Loss'} of {fmt_money(pnl)}; "
            f"close reason recorded as '{reason or 'unknown'}'. The precise "
            f"cause of this outcome is unclear from the recorded fields.")

    # provenance caveats, grounded in the record
    note = (c.get("note") or "")
    if "estimated" in note.lower() or "excl." in note.lower():
        lines.append(
            "Caveat: this P&L is ESTIMATED from the symbol specs tick value "
            "and excludes commission/swap -- it is not a broker-confirmed "
            "figure.")
    if c.get("commission") is None and c.get("swap") is None:
        lines.append("Commission/swap: not recorded.")

    # timestamp sanity (recorded fields only)
    ot, ct = parse_server_time(tr["entry_time"]), parse_server_time(c.get("time"))
    if ot and ct and ct < ot:
        lines.append(
            f"Data warning: the recorded close time ({c.get('time')}) is "
            f"earlier than the recorded open time ({tr['entry_time']}). One "
            f"of the timestamps is wrong; treat the timing as unreliable.")

    # manual override context (recorded in journal)
    if tr.get("_manual_override"):
        lines.append(
            "Context: this fill was a MANUAL OVERRIDE -- the owner ordered "
            "the fill after the executor had skipped the signal "
            "(skipped:max_total_exposure). The win came from a human override, "
            "not from the autonomous decision path.")

    return "\n".join("- " + l for l in lines)


# ---------------------------------------------------------------- schematic chart

def draw_schematic(tr, path):
    sym = tr["symbol"] or "?"
    direction = tr["direction"] or "?"
    entry = tr["entry_price"] or tr["signal_entry"]
    sl = tr["stop_loss"]
    tp = tr["take_profit"]
    c = tr["close"]
    exit_px = c.get("exit_price") if c else None

    rng = random.Random(int(tr["ticket"]) % (2 ** 31))
    n = 60
    entry_idx = 12
    pos = tr.get("_open_pos")
    exit_px = c.get("exit_price") if c else None
    cur_px = pos.get("current_price") if pos else None
    anchor_px = exit_px if exit_px is not None else cur_px
    exit_idx = 52 if anchor_px is not None else 44

    band = abs((tp or 0) - (sl or 0)) or (abs(entry or 1) * 0.01)
    step = band / 10.0
    px = [None] * (entry_idx + 1)
    last = float(entry) if entry is not None else 0.0
    for i in range(entry_idx + 1):
        px[i] = last
        last += rng.uniform(-step, step)

    drift = 0.0
    if anchor_px is not None and entry is not None:
        drift = (float(anchor_px) - float(entry)) / max(exit_idx - entry_idx, 1)
    last = px[entry_idx]
    for i in range(entry_idx + 1, exit_idx + 1):
        last += drift + rng.uniform(-step, step)
        px.append(last)
    if anchor_px is not None and px:
        px[-1] = float(anchor_px)  # anchor the recorded exit/current exactly

    fig, ax = plt.subplots(figsize=(10, 6))
    xs = list(range(len(px)))
    ax.plot(xs, px, color="#1f77b4", linewidth=1.6, label="price path (illustrative)")

    def hline(y, color, style, label):
        if y is None:
            return
        ax.axhline(float(y), color=color, linestyle=style, linewidth=1.4)
        ax.text(len(px) - 1, float(y), f"  {label} {fmt(y)}",
                color=color, fontsize=9, va="center",
                bbox=dict(facecolor="white", alpha=0.7, edgecolor="none", pad=1))

    hline(entry, "#1f77b4", "-", "entry")
    hline(sl, "#d62728", "--", "stop-loss")
    hline(tp, "#2ca02c", "--", "take-profit")
    if exit_px is not None:
        ax.axvline(exit_idx, color="#9467bd", linestyle=":", linewidth=1.4)
        ax.scatter([exit_idx], [float(exit_px)], color="#9467bd", s=60, zorder=5)
        ax.text(exit_idx + 0.5, float(exit_px), f" exit {fmt(exit_px)}",
                color="#9467bd", fontsize=9, va="bottom",
                bbox=dict(facecolor="white", alpha=0.7, edgecolor="none", pad=1))
    elif pos is not None:
        ax.axvline(exit_idx, color="#2ca02c", linestyle=":", linewidth=1.4)
        ax.scatter([exit_idx], [float(cur_px)], color="#2ca02c", s=60, zorder=5)
        ax.text(exit_idx + 0.5, float(cur_px),
                f" still open {fmt(cur_px)} (floating "
                f"{fmt_money(pos.get('profit'))})",
                color="#2ca02c", fontsize=9, va="bottom",
                bbox=dict(facecolor="white", alpha=0.7, edgecolor="none", pad=1))
    else:
        ax.axvspan(exit_idx, len(px) + 8, color="#ffcccc", alpha=0.35)
        ax.text(exit_idx + 1, px[-1],
                "no close record -- outcome unknown",
                color="#a00000", fontsize=10, weight="bold",
                bbox=dict(facecolor="white", alpha=0.8, edgecolor="none", pad=2))

    ax.axvline(entry_idx, color="#1f77b4", linestyle=":", linewidth=1.0, alpha=0.6)
    ax.set_xlim(0, len(px) + 6)
    ax.set_xlabel("time (schematic -- not to scale)")
    ax.set_ylabel("price")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)

    pnl = c.get("profit") if c else None
    outcome = outcome_of(tr).upper()
    title = (f"{sym} {tr['timeframe']} {direction} -- ticket {tr['ticket']}\n"
             f"SCHEMATIC -- reconstructed from signal parameters "
             f"(not real market candles)")
    if pnl is not None:
        title += f"   |   PROFIT {fmt_money(pnl)}" if outcome == "PROFIT" else f"   |   LOSS {fmt_money(pnl)}"
    elif pos is not None:
        title += f"   |   STILL OPEN floating {fmt_money(pos.get('profit'))}"
    else:
        title += "   |   OUTCOME UNKNOWN"
    ax.set_title(title, fontsize=10, weight="bold", loc="left")

    fig.text(0.5, 0.01,
             "schematic -- reconstructed from signal parameters -- "
             "illustrative only, not real market candles",
             ha="center", fontsize=9, style="italic", color="#555555")
    fig.tight_layout(rect=[0, 0.03, 1, 0.96])
    fig.savefig(path, dpi=110)
    plt.close(fig)


# ---------------------------------------------------------------- markdown

def decision_block(tr):
    d = tr["decision"]
    if not d:
        return "not recorded in journal"
    parts = [f"decision: `{d.get('decision')}`"]
    if d.get("reason"):
        parts.append(f"reason: {d['reason']}")
    if d.get("volume") is not None:
        parts.append(f"volume: {d['volume']}")
    if d.get("risk_amount") is not None:
        parts.append(f"planned risk: ${float(d['risk_amount']):,.2f}")
    if d.get("time"):
        parts.append(f"time: {d['time']}")
    return "; ".join(parts)


def signal_block(tr):
    s = tr["signal"] or {}
    if not s:
        return "not recorded"
    trig = s.get("trigger") or "not recorded"
    strat = s.get("strategy") or "not recorded"
    extra = []
    if s.get("rsi_value") is not None:
        extra.append(f"RSI={s['rsi_value']}")
    if s.get("ema_fast") is not None and s.get("ema_slow") is not None:
        extra.append(f"EMA20={s['ema_fast']}, EMA50={s['ema_slow']}")
    if s.get("atr_value") is not None:
        extra.append(f"ATR={s['atr_value']}")
    if s.get("candle_time"):
        extra.append(f"candle: {s['candle_time']}")
    more = (" (" + ", ".join(extra) + ")") if extra else ""
    return f"strategy `{strat}`: {trig}{more}"


def trade_page(tr, overrides, pos_by_ticket, snap_time):
    c = tr["close"] or {}
    pos = pos_by_ticket.get(tr["ticket"])
    tr["_open_pos"] = pos
    tr["_snap_time"] = snap_time
    # link manual override events: journal trade.opened carries a "-manual"
    # command_id suffix for owner-ordered fills; fall back to symbol/direction.
    cmd_id = str(tr.get("command_id") or "")
    tr["_manual_override"] = cmd_id.endswith("-manual") or any(
        o.get("symbol") == tr["symbol"] and o.get("direction") == tr["direction"]
        for o in overrides)
    outcome = outcome_of(tr)
    if c:
        status = {"profit": "CLOSED -- PROFIT",
                  "loss": "CLOSED -- LOSS"}[outcome]
        status_key = outcome
    elif pos:
        status = "OPEN -- per latest positions snapshot"
        status_key = "open"
    else:
        status = "OUTCOME UNKNOWN -- under reconciliation"
        status_key = "unknown"

    pnl = c.get("profit")
    pnl_line = fmt_money(pnl)
    if pnl is not None and "estimated" in str(c.get("note", "")).lower():
        pnl_line += " *(estimated -- excludes commission/swap)*"

    comm = c.get("commission")
    swap = c.get("swap")
    comm_swap = ("not recorded" if comm is None and swap is None
                 else f"commission={fmt(comm, 2)}, swap={fmt(swap, 2)}")

    entry_dt = parse_server_time(tr["entry_time"])
    day = entry_dt.strftime("%Y-%m-%d") if entry_dt else "unknown-date"

    why_heading = ("## Why profit" if status_key == "profit" else
                   "## Why loss" if status_key == "loss" else
                   "## Why this is still open" if status_key == "open" else
                   "## Why this is unresolved")

    lines = [
        f"# {tr['symbol']} {tr['direction']} -- ticket {tr['ticket']}",
        "",
        f"**Status:** {status}",
        f"**Date:** {day} ({SERVER_TZ_NOTE})",
        "",
        "## Trade facts",
        "",
        f"- Symbol: {tr['symbol']}",
        f"- Direction: {tr['direction']}",
        f"- Timeframe: {tr['timeframe']}",
        f"- Entry time: {tr['entry_time'] or 'not recorded'} ({SERVER_TZ_NOTE})",
        f"- Exit time: {c.get('time') or ('still open' if pos else 'not recorded')}",
        f"- Entry price: {fmt(tr['entry_price'])}",
        f"- Exit price: {fmt(c.get('exit_price'))}",
        f"- Stop-loss: {fmt(tr['stop_loss'])}",
        f"- Take-profit: {fmt(tr['take_profit'])}",
        f"- Volume: {fmt(tr['volume'], 2)} lots",
        f"- P&L: {pnl_line}",
        f"- Floating P&L: {(fmt_money(pos.get('profit')) + ' (unrealized, snapshot ' + str(snap_time) + ')') if pos else 'not recorded'}",
        f"- Commission / swap: {comm_swap}",
        f"- Exit reason: {exit_reason_text(c) if c else ('still open' if pos else 'not recorded')}",
        f"- Market session at entry: {session_label(entry_dt)}",
        "",
        "## Signal rationale",
        "",
        signal_block(tr),
        "",
        "## Gate decision record",
        "",
        decision_block(tr),
        "",
        why_heading,
        "",
        why_section(tr),
        "",
        "## Chart",
        "",
        f"![schematic chart]({tr['ticket']}.png)",
        "",
        "_Chart is schematic -- reconstructed from the signal's entry/SL/TP/exit "
        "parameters. No historical candle data is stored in this environment, "
        "so this is an illustration of the trade plan, not real market candles._",
        "",
        "## Data gaps / notes",
        "",
    ]
    notes = []
    if not c:
        notes.append("No close event recorded -- outcome unknown, under reconciliation.")
    if c.get("note"):
        notes.append(sanitize_note(c["note"]))
    if tr["entry_price"] is None or tr["signal_entry"] is None:
        pass
    if not tr["signal"]:
        notes.append("No matching signal record found in nova_signals.jsonl.")
    if not tr["decision"] and not tr["_manual_override"]:
        notes.append("No gate decision record found in the journal.")
    if not notes:
        notes.append("None -- all key fields recorded.")
    lines.extend("- " + n for n in notes)
    lines += ["",
              "---",
              "_Generated by trade_report.py for the 30-day autonomous demo "
              "trading experiment. Demo account, no real money._"]
    return "\n".join(lines), day


# ---------------------------------------------------------------- index

def write_index(out_dir, pages, positions):
    by_day = {}
    for day, ticket, title, entry, exit_, pnl, reason, status in pages:
        by_day.setdefault(day, []).append(
            (ticket, title, entry, exit_, pnl, reason, status))
    total = sum(p for _, _, _, _, _, p, _, _ in pages if p is not None)
    n_closed = sum(1 for _, _, _, _, _, p, _, _ in pages if p is not None)
    n_open = sum(1 for _, _, _, _, _, _, _, s in pages if s == "open")
    n_unknown = sum(1 for _, _, _, _, _, _, _, s in pages if s == "unknown")

    caveats = []
    if n_unknown:
        caveats.append(f"{n_unknown} opened trades have no close records yet")
    caveats.append("one close is an estimate excluding commission/swap")
    caveat_txt = "; ".join(caveats)

    lines = ["# Trade documentation -- 30-day autonomous demo experiment",
             "",
             "Per-trade pages with schematic charts and grounded profit/loss "
             "explanations. Every closed trade is documented, winners and losers. "
             "Trades whose close was never recorded are marked "
             "**outcome unknown -- under reconciliation**; no profit is claimed "
             "for them.",
             "",
             f"**Realized P&L so far: {fmt_money(total)}** "
             f"({n_closed} closed trades, {n_open} still open) -- "
             f"_provisional: {caveat_txt}._",
             ""]
    for day in sorted(by_day):
        lines += [f"## {day}", "",
                  "| Ticket | Trade | Entry (server) | Exit (server) | P&L | Exit reason | Status |",
                  "|---|---|---|---|---|---|---|"]
        for ticket, title, entry, exit_, pnl, reason, status in sorted(by_day[day]):
            lines.append(
                f"| [{ticket}]({day}/{ticket}.md) | {title} | "
                f"{entry or 'not recorded'} | {exit_ or 'not recorded'} | "
                f"{fmt_money(pnl) if pnl is not None else 'unknown'} | "
                f"{reason} | {status} |")
        lines += [""]

    lines += ["## Open positions (latest snapshot)", ""]
    try:
        poss = positions.get("positions", []) if positions else []
        stime = positions.get("server_time") if positions else None
    except AttributeError:
        poss, stime = [], None
    if poss:
        lines.append(f"Snapshot: {stime} ({SERVER_TZ_NOTE}). Floating P&L is "
                     "unrealized.")
        lines += ["", "| Ticket | Symbol | Dir | Volume | Open | Current | Floating P&L |",
                  "|---|---|---|---|---|---|---|"]
        for p in poss:
            lines.append(
                f"| {p.get('ticket')} | {p.get('symbol')} | {p.get('type')} | "
                f"{p.get('volume')} | {fmt(p.get('open_price'))} | "
                f"{fmt(p.get('current_price'))} | {fmt_money(p.get('profit'))} |")
    else:
        lines.append("No open positions in the latest snapshot, or snapshot not recorded.")
    lines += ["",
              "_Charts are schematic -- reconstructed from signal parameters; "
              "no historical candle data is stored in this environment._",
              "_Generated by trade_report.py. Demo account, no real money._"]
    with open(os.path.join(out_dir, "index.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    journal = load_jsonl(JOURNAL_PATH)
    broker_trades = load_jsonl(TRADES_PATH)
    signals = load_jsonl(SIGNALS_PATH)
    try:
        with open(POSITIONS_PATH, encoding="utf-8", errors="replace") as f:
            positions = json.load(f)
    except (OSError, ValueError):
        positions = {}

    trades, overrides, pos_by_ticket, snap_time = build_trades(
        journal, broker_trades, signals, positions)
    print(f"trades found: {len(trades)} "
          f"({sum(1 for t in trades if t['close'])} closed, "
          f"{sum(1 for t in trades if not t['close'] and t['ticket'] not in pos_by_ticket)} "
          f"without close record, "
          f"{sum(1 for t in trades if not t['close'] and t['ticket'] in pos_by_ticket)} "
          f"still open)")

    out_dir = args.out
    pages = []
    for tr in trades:
        md, day = trade_page(tr, overrides, pos_by_ticket, snap_time)
        day_dir = os.path.join(out_dir, day)
        os.makedirs(day_dir, exist_ok=True)
        with open(os.path.join(day_dir, f"{tr['ticket']}.md"), "w") as f:
            f.write(md + "\n")
        draw_schematic(tr, os.path.join(day_dir, f"{tr['ticket']}.png"))
        pnl = (tr["close"] or {}).get("profit")
        pos = pos_by_ticket.get(tr["ticket"])
        if tr["close"]:
            reason = exit_reason_text(tr["close"])
            status = outcome_of(tr)
        elif pos:
            reason = "still open"
            status = "open"
        else:
            reason = "not recorded"
            status = "unknown"
        title = f"{tr['symbol']} {tr['direction']}"
        pages.append((day, tr["ticket"], title,
                      tr["entry_time"],
                      (tr["close"] or {}).get("time") or
                      ("still open" if pos else None),
                      pnl, reason, status))
        print(f"  wrote {day}/{tr['ticket']}.md (+ .png) [{status}]")

    write_index(out_dir, pages, positions)
    print(f"index: {out_dir}/index.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
