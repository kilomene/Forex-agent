#!/usr/bin/env python3
"""
Per-trade documentation generator for the 30-day autonomous demo trading
experiment (2026-09-21 -> 2026-10-21).

For every trade the executor has opened, writes a documentation page to

    ~/workspace/mt5/evidence-docs/docs/trades/<YYYY-MM-DD>/<ticket>.md

plus one chart PNG per trade, and an index page
    ~/workspace/mt5/evidence-docs/docs/trades/index.md

Each page shows: symbol, direction, timeframe, entry/exit timestamps,
entry/exit prices, SL/TP, volume, P&L, the signal rationale, the gate
decision record, the exit reason, and a "Why ..." section grounded ONLY in
recorded fields. Unrecorded values render as "not recorded" -- numbers are
never invented.

OUTCOME CLASSIFICATION (hard rule -- this is what keeps the evidence
honest):
  * profit / loss / breakeven labels are used ONLY when the close carries
    authoritative broker-confirmed NET P&L. A close counts as authoritative
    when it is explicitly marked confirmation="broker" (set by the
    DEAL_POSITION_ID reconciliation pipeline), or -- for EA-recorded closes
    -- when reason == "broker" and the record note contains no estimation
    language.
  * Derived/estimated amounts (specs tick-value math, terminal-log linkage,
    "excl. commission/swap") are labeled "provisional estimate" and NEVER
    drive exact outcome labels.
  * An exit at/near the entry price is NOT a breakeven unless authoritative
    net P&L is exactly zero. Before costs it is a provisional estimate.
  * Trades with no close record (or a close record with no P&L figure) are
    "OUTCOME UNKNOWN -- under reconciliation". No exact P&L is claimed.

CHARTS: no historical candle data exists anywhere in this environment
(nova_feed.json is a live-quote heartbeat only; no history/rates files),
so every chart is a clearly-labeled SCHEMATIC reconstructed from the
signal's entry/SL/TP/exit parameters. The image itself visibly carries the
exact text "schematic -- reconstructed from signal parameters". Schematic
images are never described as historical candle charts.

SANITIZED + PRIVACY-GATED: account logins, deal/order IDs, tokens and other
private identifiers are never written into the docs, and every run ends
with a fail-on-match privacy scan over all generated .md and .png files
(text + PNG metadata). Any match fails the run with a non-zero exit.

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
import struct
import sys
import zlib
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

SERVER_TZ_NOTE = "MT5 server time (UTC+3)"

SCHEMATIC_LABEL = "schematic \u2014 reconstructed from signal parameters"


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
    corrections = {}  # ticket -> trade.close_corrected (authoritative revision)
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
        elif t == "trade.close_corrected" and e.get("ticket") is not None:
            corrections[e["ticket"]] = e
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
        elif t == "trade.close_corrected" and e.get("ticket") is not None:
            corrections[e["ticket"]] = e
    # Authoritative revisions (QueryClose reconciliation) overlay the
    # provisional close they supersede: net P&L, costs, confirmation and
    # audit trail win. The original provisional record is left untouched.
    for ticket, corr in corrections.items():
        base = closes.get(ticket)
        if base is None:
            closes[ticket] = dict(corr)
            continue
        merged = dict(base)
        for k in ("profit", "commission", "swap", "net_profit",
                  "confirmation", "confirmation_status", "note", "source",
                  "deal_in", "deal_out", "exit_time_broker", "reason",
                  "reconciled", "prior_profit_estimate"):
            if corr.get(k) is not None:
                merged[k] = corr[k]
        merged["corrected_from_provisional"] = True
        closes[ticket] = merged

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
            "signal_id": sid,
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


# ---------------------------------------------------------------- outcome classification
#
# The single most important function in this file. profit/loss/breakeven are
# claims about REALIZED money and may only be stated when the close record
# is authoritative broker-confirmed NET P&L. Everything else is provisional
# or unknown, and is labeled as such.

# Words in a close note that mark the P&L as derived rather than confirmed.
ESTIMATE_WORDS = ("estimat", "excl.", "excludes", "excluding",
                  "provisional", "derived", "approx")

# Close reasons the EA writes for fills it executed against the broker feed.
BROKER_REASON = "broker"


def confirmation_of(close):
    """'broker' if the close is authoritative, 'provisional' if it is a
    derived/estimated close, None if there is no close record.

    Authoritative means broker-confirmed NET P&L: either an explicit
    confirmation="broker" marker (set by the DEAL_POSITION_ID
    reconciliation pipeline, which also supplies commission/swap), or an
    EA-recorded broker close (reason == "broker") whose note carries no
    estimation language."""
    if not close:
        return None
    # The reconciliation pipeline writes confirmation_status="broker-confirmed";
    # older code used a bare "confirmation" field. Accept both.
    explicit = str(close.get("confirmation")
                   or close.get("confirmation_status") or "").strip().lower()
    if explicit in ("broker", "broker-confirmed", "authoritative"):
        return "broker"
    if explicit in ("provisional", "estimated", "estimate", "unconfirmed",
                    "derived"):
        return "provisional"
    note = str(close.get("note") or "").lower()
    if any(w in note for w in ESTIMATE_WORDS):
        return "provisional"
    if close.get("reason") == BROKER_REASON:
        return "broker"
    # Anything else (terminal-log reconstruction, manual notes, unknown
    # reasons) is derived by construction.
    return "provisional"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def close_net(close):
    """Authoritative net P&L for a close: net_profit (incl. commission/swap)
    wins over the bare profit field. None when neither is usable."""
    if not close:
        return None
    return _num(close.get("net_profit")) \
        if _num(close.get("net_profit")) is not None \
        else _num(close.get("profit"))


def near(a, b):
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= max(abs(float(b)) * 1e-6, 1e-9)
    except (TypeError, ValueError):
        return False


def is_entry_price_exit(tr):
    """True when the recorded exit price is at/near the entry price."""
    c = tr.get("close")
    if not c:
        return False
    entry = tr.get("entry_price")
    if entry is None:
        entry = (tr.get("signal") or {}).get("entry_price")
    return near(c.get("exit_price"), entry)


def classify_outcome(tr):
    """One of: confirmed_profit, confirmed_loss, confirmed_breakeven,
    provisional, open, unknown.

    Only authoritative broker-confirmed NET P&L yields the confirmed_*
    classes. Derived/estimated closes are 'provisional' (an entry-price
    exit before costs is provisional, NOT a breakeven). No close record,
    or a close record with no usable P&L figure, is 'unknown'."""
    c = tr.get("close")
    pos = tr.get("_open_pos")
    if not c:
        return "open" if pos else "unknown"
    conf = confirmation_of(c)
    pf = close_net(c)
    if conf != "broker":
        return "provisional" if pf is not None else "unknown"
    if pf is None:
        return "unknown"
    if pf == 0:
        return "confirmed_breakeven"
    return "confirmed_profit" if pf > 0 else "confirmed_loss"


STATUS_TEXT = {
    "confirmed_profit": "CLOSED \u2014 PROFIT (broker-confirmed)",
    "confirmed_loss": "CLOSED \u2014 LOSS (broker-confirmed)",
    "confirmed_breakeven": "CLOSED \u2014 BREAKEVEN (broker-confirmed)",
    "provisional": "CLOSED \u2014 PROVISIONAL ESTIMATE (unconfirmed)",
    "open": "OPEN \u2014 per latest positions snapshot",
    "unknown": "OUTCOME UNKNOWN \u2014 under reconciliation",
}

INDEX_STATUS = {
    "confirmed_profit": "profit (broker-confirmed)",
    "confirmed_loss": "loss (broker-confirmed)",
    "confirmed_breakeven": "breakeven (broker-confirmed)",
    "provisional": "provisional estimate",
    "open": "open",
    "unknown": "unknown",
}

WHY_HEADING = {
    "confirmed_profit": "## Why profit \u2014 broker-confirmed",
    "confirmed_loss": "## Why loss \u2014 broker-confirmed",
    "confirmed_breakeven": "## Why breakeven \u2014 broker-confirmed",
    "provisional": "## Why this is only a provisional estimate",
    "open": "## Why this is still open",
    "unknown": "## Why this is unresolved",
}


def exit_reason_text(close):
    if not close:
        return "not recorded"
    r = close.get("reason")
    base = {
        "broker": "broker-recorded close",
        "broker-sl-reconciled": "stop-loss close (reconstructed from terminal log)",
        "manual": "manual close",
        "kill-switch": "kill-switch close",
    }.get(r, r or "unknown")
    if confirmation_of(close) == "broker":
        return base + " \u2014 broker-confirmed"
    return base + " \u2014 provisional (unconfirmed)"


def pnl_line(tr, cls):
    c = tr.get("close") or {}
    p = close_net(c)
    if cls.startswith("confirmed"):
        return f"{fmt_money(p)} (broker-confirmed net)"
    if cls == "provisional":
        return (f"{fmt_money(p)} (provisional estimate \u2014 excludes "
                f"commission/swap; NOT broker-confirmed)")
    return "unknown"


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

def why_section(tr, cls):
    """Grounded explanation. Anything unclear is stated as unclear.
    Provisional closes never get profit/loss stated as fact."""
    c = tr["close"]
    lines = []
    pos = tr.get("_open_pos")
    if cls == "open":
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
    if cls == "unknown":
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
        return "\n".join("- " + l for l in lines)

    if cls == "provisional":
        lines.append(
            "This close is a PROVISIONAL ESTIMATE, not a broker-confirmed "
            "result. No profit or loss is claimed for this trade; any figure "
            "shown is for context only.")
        reason = c.get("reason") or "unknown"
        note = sanitize_note(c.get("note") or "")
        basis = f"close reason '{reason}'"
        if note:
            basis += f"; record note: \"{note}\""
        lines.append(f"Basis: {basis}.")
        if is_entry_price_exit(tr):
            lines.append(
                f"Exit price {fmt(c.get('exit_price'))} is at/near the entry "
                f"price {fmt(tr['entry_price'] or (tr.get('signal') or {}).get('entry_price'))} "
                f"before costs -- this is NOT a breakeven. Net P&L is unknown "
                f"until the broker confirms it (commission/swap unknown).")
        else:
            lines.append(
                "Commission/swap: not recorded -- excluded from the estimate.")
        ot, ct = parse_server_time(tr["entry_time"]), parse_server_time(c.get("time"))
        if ot and ct and ct < ot:
            lines.append(
                f"Data warning: the recorded close time ({c.get('time')}) is "
                f"earlier than the recorded open time ({tr['entry_time']}). One "
                f"of the timestamps is wrong; treat the timing as unreliable.")
        return "\n".join("- " + l for l in lines)

    # confirmed_* : grounded in the broker-confirmed record
    reason = c.get("reason")
    pnl = close_net(c)
    exit_px = c.get("exit_price")
    tp = tr["take_profit"]
    sl = tr["stop_loss"]
    entry = tr["entry_price"] or (tr.get("signal") or {}).get("entry_price")

    if cls == "confirmed_profit" and near(exit_px, tp):
        lines.append(
            f"Broker-confirmed profit: the trade reached its take-profit "
            f"target ({fmt(tp)}). Exit price {fmt(exit_px)} matches the TP "
            f"set on the signal, so the broker closed it as a winner.")
        hold = c.get("hold_seconds")
        if hold:
            lines.append(
                f"It was held {int(hold)} seconds (~{int(hold)//60} min) -- a "
                f"fast move in the signal's direction after entry.")
    elif cls == "confirmed_loss" and near(exit_px, sl):
        lines.append(
            f"Broker-confirmed loss: the trade hit its stop-loss ({fmt(sl)}). "
            f"Exit price {fmt(exit_px)} matches the SL, so the position was "
            f"stopped out as designed by the risk plan.")
        if entry is not None and sl is not None:
            lines.append(
                f"Entry {fmt(entry)} -> SL {fmt(sl)}: price moved against the "
                f"{(tr['direction'] or '').lower()} position immediately; the "
                f"EMA-cross signal did not follow through.")
    elif cls == "confirmed_breakeven":
        lines.append(
            f"Broker-confirmed breakeven: authoritative net P&L is exactly "
            f"$0.00 (gross, commission and swap accounted).")
    else:
        lines.append(
            f"Broker-confirmed {'profit' if (pnl or 0) > 0 else 'loss'} of "
            f"{fmt_money(pnl)} at {fmt(exit_px)}. The exact exit trigger is "
            f"not recorded (close reason is '{reason or 'unknown'}'), so the "
            f"precise cause is unclear from the recorded fields.")

    if c.get("commission") is None and c.get("swap") is None:
        lines.append("Commission/swap: not recorded in the close event.")
    else:
        lines.append(
            f"Commission {fmt(c.get('commission'), 2)}, swap "
            f"{fmt(c.get('swap'), 2)} (included in the confirmed net figure).")

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
            "the fill after the executor had skipped the signal. The result "
            "came from a human override, not from the autonomous decision "
            "path.")

    return "\n".join("- " + l for l in lines)


# ---------------------------------------------------------------- schematic chart

def draw_schematic(tr, path):
    """Draw the schematic chart. The figure visibly carries the exact text
    SCHEMATIC_LABEL and is never described as historical candles. Returns
    the matplotlib figure (also saved to path)."""
    sym = tr["symbol"] or "?"
    direction = tr["direction"] or "?"
    entry = tr["entry_price"] or (tr.get("signal") or {}).get("entry_price")
    sl = tr["stop_loss"]
    tp = tr["take_profit"]
    c = tr["close"]
    pos = tr.get("_open_pos")
    cls = classify_outcome(tr)

    rng = random.Random(int(tr["ticket"]) % (2 ** 31))
    n = 60
    entry_idx = 12
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
                f"{fmt_money(pos.get('profit'))}",
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

    pnl = close_net(c) if c else None
    title = (f"{sym} {tr['timeframe']} {direction} -- ticket {tr['ticket']}\n"
             f"SCHEMATIC \u2014 reconstructed from signal parameters "
             f"(not real market candles)")
    if cls == "confirmed_profit":
        title += f"   |   PROFIT {fmt_money(pnl)} (broker-confirmed)"
    elif cls == "confirmed_loss":
        title += f"   |   LOSS {fmt_money(pnl)} (broker-confirmed)"
    elif cls == "confirmed_breakeven":
        title += f"   |   BREAKEVEN {fmt_money(pnl)} (broker-confirmed)"
    elif cls == "provisional":
        title += (f"   |   PROVISIONAL ESTIMATE \u2248{fmt_money(pnl)} "
                  f"(unconfirmed)")
    elif cls == "open":
        title += f"   |   STILL OPEN floating {fmt_money(pos.get('profit'))}"
    else:
        title += "   |   OUTCOME UNKNOWN"
    ax.set_title(title, fontsize=10, weight="bold", loc="left")

    fig.text(0.5, 0.01,
             SCHEMATIC_LABEL + " \u00b7 "
             "illustrative only, not real market candles",
             ha="center", fontsize=9, style="italic", color="#555555")
    fig.tight_layout(rect=[0, 0.03, 1, 0.96])
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return fig


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
    cls = classify_outcome(tr)
    status = STATUS_TEXT[cls]

    comm = c.get("commission")
    swap = c.get("swap")
    comm_swap = ("not recorded" if comm is None and swap is None
                 else f"commission={fmt(comm, 2)}, swap={fmt(swap, 2)}")

    entry_dt = parse_server_time(tr["entry_time"])
    day = entry_dt.strftime("%Y-%m-%d") if entry_dt else "unknown-date"

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
        f"- Exit time: {c.get('exit_time_broker') or c.get('time') or ('still open' if pos else 'not recorded')}",
        f"- Entry price: {fmt(tr['entry_price'])}",
        f"- Exit price: {fmt(c.get('exit_price'))}",
        f"- Stop-loss: {fmt(tr['stop_loss'])}",
        f"- Take-profit: {fmt(tr['take_profit'])}",
        f"- Volume: {fmt(tr['volume'], 2)} lots",
        f"- P&L: {pnl_line(tr, cls)}",
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
        WHY_HEADING[cls],
        "",
        why_section(tr, cls),
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
    if cls == "unknown":
        notes.append("No close event recorded -- outcome unknown, under reconciliation.")
    if cls == "provisional":
        notes.append("Close is a provisional estimate -- not broker-confirmed; "
                     "excluded from the realized P&L total.")
    if c.get("note"):
        notes.append(sanitize_note(c["note"]))
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
    for day, ticket, title, entry, exit_, pnl, reason, cls in pages:
        by_day.setdefault(day, []).append(
            (ticket, title, entry, exit_, pnl, reason, cls))
    confirmed = [(t, p) for _, t, _, _, _, p, _, c in pages
                 if c.startswith("confirmed") and p is not None]
    total = sum(p for _, p in confirmed)
    n_conf = len(confirmed)
    n_prov = sum(1 for _, _, _, _, _, _, _, c in pages if c == "provisional")
    n_open = sum(1 for _, _, _, _, _, _, _, c in pages if c == "open")
    n_unknown = sum(1 for _, _, _, _, _, _, _, c in pages if c == "unknown")

    caveats = []
    if n_prov:
        caveats.append(f"excludes {n_prov} provisional estimate(s) (not counted)")
    if n_unknown:
        caveats.append(f"{n_unknown} outcome(s) unknown -- under reconciliation")
    if n_open:
        caveats.append(f"{n_open} still open")
    caveat_txt = "; ".join(caveats) if caveats else "all closes broker-confirmed"

    lines = ["# Trade documentation -- 30-day autonomous demo experiment",
             "",
             "Per-trade pages with schematic charts and grounded profit/loss "
             "explanations. Every closed trade is documented, winners and losers. "
             "Trades whose close was never authoritatively confirmed are marked "
             "as provisional estimates or **outcome unknown -- under "
             "reconciliation**; no profit is claimed for them.",
             "",
             f"**Realized P&L so far: {fmt_money(total)}** "
             f"({n_conf} broker-confirmed close(s)) -- "
             f"_provisional: {caveat_txt}._",
             ""]
    for day in sorted(by_day):
        lines += [f"## {day}", "",
                  "| Ticket | Trade | Entry (server) | Exit (server) | P&L | Exit reason | Status |",
                  "|---|---|---|---|---|---|---|"]
        for ticket, title, entry, exit_, pnl, reason, cls in sorted(by_day[day]):
            if cls.startswith("confirmed"):
                pnl_txt = fmt_money(pnl)
            elif cls == "provisional":
                pnl_txt = f"{fmt_money(pnl)} (provisional)"
            elif cls == "open":
                pnl_txt = "still open"
            else:
                pnl_txt = "unknown"
            lines.append(
                f"| [{ticket}]({day}/{ticket}.md) | {title} | "
                f"{entry or 'not recorded'} | {exit_ or 'not recorded'} | "
                f"{pnl_txt} | "
                f"{reason} | {INDEX_STATUS[cls]} |")
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


# ---------------------------------------------------------------- privacy checker
#
# Fail-on-match gate over every generated evidence file (.md text and .png
# text metadata). The trade's OWN position ticket (from the filename) and its
# own signal/command identifiers are allowlisted; everything else shaped
# like a broker identifier or credential fails the run.

# Bare digit runs shaped like deal/order/position tickets (11+ digits).
TICKET_RUN_RE = re.compile(r"\d{11,}")
# Bare 9-digit runs (account-login shaped). 10-digit epoch timestamps used in
# signal/command IDs are intentionally NOT matched.
ACCT_RUN_RE = re.compile(r"(?<!\d)\d{9}(?!\d)")
# Labeled identifiers: "account 12345678", "deal #10334...", "order: 10608..."
LABELED_ID_RE = re.compile(
    r"(?i)\b(?P<label>account|login|acct|deal|order|position|ticket)"
    r"[\s_#:=-]*#?(?P<num>\d{6,})")
CREDENTIAL_RES = [
    re.compile(r"(?i)\b(password|passwd|pwd|api[_-]?key|secret|private[_-]?key)"
               r"\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(token)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
]


def png_text_chunks(path):
    """Extract tEXt/iTXt/zTXt metadata strings from a PNG without decoding
    image data (avoids false positives from compressed pixel bytes)."""
    texts = []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return texts
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return texts
    off = 8
    while off + 8 <= len(data):
        (ln,) = struct.unpack(">I", data[off:off + 4])
        ctype = data[off + 4:off + 8]
        chunk = data[off + 8:off + 8 + ln]
        if ctype == b"IEND":
            break
        if ctype == b"tEXt":
            _, _, txt = chunk.partition(b"\x00")
            texts.append(txt.decode("latin-1", "replace"))
        elif ctype == b"zTXt":
            _, _, rest = chunk.partition(b"\x00")
            if len(rest) > 1:
                try:
                    texts.append(zlib.decompress(rest[1:]).decode(
                        "latin-1", "replace"))
                except Exception:
                    pass
        elif ctype == b"iTXt":
            parts = chunk.split(b"\x00", 5)
            if len(parts) == 6:
                txt = parts[5]
                if parts[2][:1] == b"\x01":
                    try:
                        txt = zlib.decompress(txt)
                    except Exception:
                        pass
                try:
                    texts.append(txt.decode("utf-8", "replace"))
                except Exception:
                    texts.append(txt.decode("latin-1", "replace"))
        off += 12 + ln
    return texts


def scan_text_for_privacy(text, allowed_ids, where):
    """Return a list of violation strings found in text."""
    violations = []
    for m in TICKET_RUN_RE.finditer(text):
        if m.group(0) not in allowed_ids:
            violations.append(
                f"{where}: ticket-shaped number {m.group(0)}")
    for m in ACCT_RUN_RE.finditer(text):
        if m.group(0) not in allowed_ids:
            violations.append(
                f"{where}: 9-digit identifier {m.group(0)}")
    for m in LABELED_ID_RE.finditer(text):
        if m.group("num") not in allowed_ids:
            violations.append(
                f"{where}: labeled identifier "
                f"'{m.group('label')}' -> {m.group('num')}")
    for rx in CREDENTIAL_RES:
        for m in rx.finditer(text):
            violations.append(f"{where}: possible credential '{m.group(0)[:40]}'")
    return violations


def scan_evidence_file(path, allowed_ids):
    """Scan one generated .md or .png file. Returns violation strings."""
    if path.endswith(".md"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return [f"{path}: unreadable"]
        return scan_text_for_privacy(text, allowed_ids, path)
    if path.endswith(".png"):
        violations = []
        for i, txt in enumerate(png_text_chunks(path)):
            violations.extend(scan_text_for_privacy(
                txt, allowed_ids, f"{path}:png-text[{i}]"))
        return violations
    return []


def file_allowed_ids(path, extra=()):
    """Identifiers that may legitimately appear in a generated file: the
    trade's own ticket (from the filename) plus caller-supplied extras."""
    ids = set(re.findall(r"\d+", os.path.basename(path)))
    ids.update(str(x) for x in extra)
    return ids


def run_privacy_scan(out_dir, index_extra_ids=()):
    """Walk out_dir scanning every .md/.png. Returns the violation list."""
    violations = []
    scanned = 0
    for root, _, files in os.walk(out_dir):
        for fn in sorted(files):
            if not (fn.endswith(".md") or fn.endswith(".png")):
                continue
            path = os.path.join(root, fn)
            allowed = file_allowed_ids(path)
            if fn == "index.md":
                allowed |= set(str(x) for x in index_extra_ids)
            violations.extend(scan_evidence_file(path, allowed))
            scanned += 1
    return violations, scanned


# ---------------------------------------------------------------- main

def own_ids(tr):
    """The trade's own identifiers (ticket, signal/command IDs) that are
    expected to appear in its evidence files."""
    ids = set()
    sig = tr.get("signal") or {}
    for v in (tr.get("ticket"), tr.get("signal_id"), tr.get("command_id"),
              sig.get("signal_id"), sig.get("id")):
        if v:
            ids.update(re.findall(r"\d+", str(v)))
    return ids


def generate(out_dir, journal, broker_trades, signals, positions):
    trades, overrides, pos_by_ticket, snap_time = build_trades(
        journal, broker_trades, signals, positions)
    print(f"trades found: {len(trades)} "
          f"({sum(1 for t in trades if t['close'])} closed, "
          f"{sum(1 for t in trades if not t['close'] and t['ticket'] not in pos_by_ticket)} "
          f"without close record, "
          f"{sum(1 for t in trades if not t['close'] and t['ticket'] in pos_by_ticket)} "
          f"still open)")

    pages = []
    for tr in trades:
        md, day = trade_page(tr, overrides, pos_by_ticket, snap_time)
        day_dir = os.path.join(out_dir, day)
        os.makedirs(day_dir, exist_ok=True)
        with open(os.path.join(day_dir, f"{tr['ticket']}.md"), "w") as f:
            f.write(md + "\n")
        draw_schematic(tr, os.path.join(day_dir, f"{tr['ticket']}.png"))
        cls = classify_outcome(tr)
        pnl = close_net(tr["close"])
        pos = pos_by_ticket.get(tr["ticket"])
        if tr["close"]:
            reason = exit_reason_text(tr["close"])
            exit_ = (tr["close"] or {}).get("exit_time_broker") or \
                (tr["close"] or {}).get("time")
        elif pos:
            reason = "still open"
            exit_ = "still open"
        else:
            reason = "not recorded"
            exit_ = None
        title = f"{tr['symbol']} {tr['direction']}"
        pages.append((day, tr["ticket"], title, tr["entry_time"], exit_,
                      pnl, reason, cls))
        print(f"  wrote {day}/{tr['ticket']}.md (+ .png) [{cls}]")

    write_index(out_dir, pages, positions)
    print(f"index: {out_dir}/index.md")

    index_extra = set()
    for tr in trades:
        index_extra.update(own_ids(tr))
    violations, scanned = run_privacy_scan(out_dir, index_extra)
    if violations:
        print("PRIVACY CHECK FAILED -- refusing to publish evidence:",
              file=sys.stderr)
        for v in violations[:50]:
            print(f"  VIOLATION: {v}", file=sys.stderr)
        if len(violations) > 50:
            print(f"  ... and {len(violations) - 50} more", file=sys.stderr)
    else:
        print(f"privacy check: clean ({scanned} files scanned)")
    return {"pages": pages, "violations": violations, "scanned": scanned}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    journal = load_jsonl(JOURNAL_PATH)
    broker_trades = load_jsonl(TRADES_PATH)
    signals = load_jsonl(SIGNALS_PATH)
    try:
        with open(POSITIONS_PATH, encoding="utf-8", errors="replace") as f:
            positions = json.load(f)
    except (OSError, ValueError):
        positions = {}

    result = generate(args.out, journal, broker_trades, signals, positions)
    return 1 if result["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
