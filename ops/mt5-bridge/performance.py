#!/usr/bin/env python3
"""
Honest performance metrics for the Nova MT5 stack.

Borrowed idea: Freqtrade's standardized performance report
(`generate_strategy_stats()` in
freqtrade/optimize/optimize_reports/optimize_reports.py) -- one canonical,
risk-adjusted report instead of headline win rate / raw P&L. Borrowed
measurement infrastructure only; nothing here touches trading logic, risk
config, or the kill switch.

This module is pure functions over the journal. No I/O, no config, no
network, no trading state -- safe to call from any reporter.

THE DEDUP RULE (repair round 2, 2026-09-24; forensic round, 2026-10-17):
the raw journal holds ~10x mirrored/duplicated trade.closed events
(executor mirrors + reconciled broker-history backfills), which produced a
+$4.86M headline over 2,227 raw events vs 219 unique tickets. Every metric
below runs on the DEDUPED per-trade P&L series: one effective close per
broker ticket, where a trade.close_corrected revision supersedes that
ticket's provisional trade.closed (its swap/commission-corrected net wins)
REGARDLESS of journal order, and closes with no ticket are quarantined
individually (never merged into one bucket).

Metric conventions (documented so the numbers are checkable):
- Sharpe ratio: per-trade, risk-free 0. Sharpe = mean(pnl) / pstdev(pnl)
  (population std, ddof=0). Returns 0.0 for fewer than 2 trades or zero
  variance. Note: per-trade, not annualized -- Freqtrade's daily Sharpe is
  a different clock; we use the trade clock because trades are the
  observation unit here.
- Sortino ratio: mean(pnl) / downside_dev, where downside_dev is the
  population std of the LOSING trades only. Returns None when undefined
  (no losers, or zero downside variance) instead of infinity, so every
  output stays valid JSON (no inf/NaN anywhere in this module).
- Max drawdown: on the cumulative P&L curve (equity[t] = running sum of
  trade P&L from 0). max_drawdown = largest (peak - subsequent trough).
  Reported as a positive dollar amount plus the peak/trough equity values
  and their indices in the (time-ordered) series. Callers must pass trades
  in chronological order; use close_time_key() to sort close events.
- Profit factor: gross_wins / |gross_losses|. None when there are no
  losses (undefined, not infinite) so JSON stays valid.
- Expectancy: mean(pnl) per trade.

benchmark_comparison() is a Freqtrade `market_change` analog: for each
symbol, the passive long-only spot move over the observed window --
(first journaled price -> last journaled price) / first price -- using the
price fields actually present in the journal: signal.received.entry_price,
trade.opened.entry_price, trade.closed.exit_price, each with its event
time. HONEST LIMITS (read before quoting): (1) the journal is not a price
feed -- it only records prices around signal/open/close moments, so the
benchmark misses intraday moves and may mis-measure the true window;
(2) it is long-only while the strategy trades both directions, so it is
an imperfect comparator, not a target; (3) a symbol whose journaled
closes carry no symbol and no linkable ticket/signal_id is reported as
"no benchmark data", never estimated. Symbols with fewer than 2 usable
price points, or a first price of 0, likewise report "no benchmark data".

duplication_check() is the by-construction guard against another +$4.86M
headline: it recomputes the total P&L the RAW close events would print and
compares it to the DEDUPED total. Flag fires when the raw/deduped ratio
exceeds 1.5 or the absolute divergence exceeds $500 -- whichever trips
first. The real 2026-10-17 journal scores ratio ~10.2 / divergence ~$4.57M.
"""

import math


# ---------------------------------------------------------------------------
# Dedup core (single implementation; daily_summary, weekend_reflection and
# final_report all delegate here)
# ---------------------------------------------------------------------------

def effective_profit(ev):
    """Authoritative P&L for one close event.

    net_profit (gross profit + commission + swap) is preferred over the
    bare profit field. Mirrors trade_executor.eff_profit() (gate
    accounting) and experiment._eff_profit() (experiment record) so every
    reporter agrees with both. Non-numeric/missing fields count as 0.0.
    """
    for k in ("net_profit", "profit"):
        try:
            v = float(ev.get(k))
        except (TypeError, ValueError):
            continue
        if v == v:  # not NaN
            return v
    return 0.0


def close_date(ev):
    """Day attribution for a close event (YYYY-MM-DD).

    A trade.close_corrected revision carries the authoritative broker exit
    time; otherwise the event's own time. Dotted MT5 timestamps
    ("2026.09.22 02:58:01") and dashed ISO ("2026-09-22 06:10:54") both
    normalize to YYYY-MM-DD.
    """
    t = ev.get("exit_time_broker") or ev.get("time")
    return str(t or "")[:10].replace(".", "-")


def close_time_key(ev):
    """Sortable full timestamp for a close event.

    Uses the authoritative broker exit time when present (corrections),
    else the event time. Dotted MT5 timestamps normalize to dashed so
    mixed formats sort correctly as strings.
    """
    t = ev.get("exit_time_broker") or ev.get("time")
    return str(t or "")[:19].replace(".", "-")


def dedupe_closes(journal):
    """One effective close event per broker ticket.

    A trade.close_corrected revision supersedes that ticket's provisional
    trade.closed REGARDLESS of journal order (repair round 2, 2026-09-24):
    a late provisional close can never clobber an earlier correction. A
    provisional close only replaces another provisional (last-wins).
    Closes with no ticket are kept as their own individual events -- they
    are never merged into one bucket (old code keyed them all as str(None)).
    Returns (unique_events, raw_event_count).
    """
    closes = {}  # ticket -> (event, is_correction)
    ticketless = []
    raw = 0
    for e in journal:
        t = e.get("type")
        if t not in ("trade.closed", "trade.close_corrected"):
            continue
        raw += 1
        is_correction = (t == "trade.close_corrected")
        ticket = e.get("ticket")
        if ticket is None or ticket == "":
            ticketless.append(e)
            continue
        key = str(ticket)
        prev = closes.get(key)
        if prev is None or is_correction or not prev[1]:
            # A correction always wins (any order); a provisional close
            # only replaces another provisional (last-wins, as before).
            closes[key] = (e, is_correction)
    return [e for (e, _c) in closes.values()] + ticketless, raw


# ---------------------------------------------------------------------------
# Risk-adjusted metrics over a deduped per-trade P&L series
# ---------------------------------------------------------------------------

def _as_floats(profits):
    out = []
    for p in profits:
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if v == v:  # drop NaN
            out.append(v)
    return out


def sharpe_ratio(profits):
    """Per-trade Sharpe ratio, risk-free rate 0.

    mean(pnl) / population_std(pnl). 0.0 for <2 trades or zero variance.
    Never NaN, never inf.
    """
    p = _as_floats(profits)
    n = len(p)
    if n < 2:
        return 0.0
    mean = sum(p) / n
    var = sum((x - mean) ** 2 for x in p) / n
    if var <= 0.0:
        return 0.0
    return mean / math.sqrt(var)


def sortino_ratio(profits):
    """Per-trade Sortino ratio, risk-free rate 0.

    mean(pnl) / downside_deviation, downside over losing trades only.
    None when undefined (fewer than 2 trades, no losers, or zero downside
    variance) -- undefined is reported as missing, not infinite.
    """
    p = _as_floats(profits)
    if len(p) < 2:
        return None
    losses = [x for x in p if x < 0]
    if not losses:
        return None
    mean = sum(p) / len(p)
    dvar = sum(x ** 2 for x in losses) / len(losses)
    if dvar <= 0.0:
        return None
    return mean / math.sqrt(dvar)


def max_drawdown(profits):
    """Max drawdown on the cumulative P&L curve.

    equity[t] = running sum of trade P&L from 0. The caller must pass
    profits in CHRONOLOGICAL order (use close_time_key() to sort close
    events). Returns dict:
      {"max_drawdown": largest peak->trough decline as a positive dollar
       amount, "peak": peak equity value, "trough": trough equity value,
       "peak_index": index of the peak trade, "trough_index": index of
       the trough trade}.
    Empty series -> all zeros with indices None.
    """
    p = _as_floats(profits)
    if not p:
        return {"max_drawdown": 0.0, "peak": 0.0, "trough": 0.0,
                "peak_index": None, "trough_index": None}
    equity = 0.0
    peak = 0.0
    peak_idx = None  # peak at index None means "before the first trade"
    best = {"max_drawdown": 0.0, "peak": 0.0, "trough": 0.0,
            "peak_index": None, "trough_index": None}
    for i, x in enumerate(p):
        equity += x
        if equity > peak:
            peak = equity
            peak_idx = i
        dd = peak - equity
        if dd > best["max_drawdown"]:
            best = {"max_drawdown": dd, "peak": peak, "trough": equity,
                    "peak_index": peak_idx, "trough_index": i}
    return best


def profit_factor(profits):
    """gross_wins / |gross_losses|.

    None when there are no losses (undefined, not infinite) or no trades
    at all -- keeps every output valid JSON.
    """
    p = _as_floats(profits)
    if not p:
        return None
    gross_w = sum(x for x in p if x > 0)
    gross_l = -sum(x for x in p if x < 0)
    if gross_l <= 0.0:
        return None
    return gross_w / gross_l


def expectancy(profits):
    """Mean P&L per trade. 0.0 for an empty series."""
    p = _as_floats(profits)
    if not p:
        return 0.0
    return sum(p) / len(p)


def compute_metrics(profits):
    """All metrics in one JSON-safe dict. Never contains NaN or inf."""
    p = _as_floats(profits)
    dd = max_drawdown(p)
    return {
        "n_trades": len(p),
        "sharpe_ratio": sharpe_ratio(p),
        "sortino_ratio": sortino_ratio(p),
        "max_drawdown": dd,
        "profit_factor": profit_factor(p),
        "expectancy": expectancy(p),
    }


# ---------------------------------------------------------------------------
# Duplication guard: raw headline P&L vs deduped P&L
# ---------------------------------------------------------------------------

DUPLICATION_RATIO_THRESHOLD = 1.5   # raw/deduped total P&L ratio
DUPLICATION_ABS_THRESHOLD = 500.0   # absolute $ divergence


def duplication_check(journal):
    """Compare the raw close-event P&L total against the deduped total.

    The raw total is exactly what an unguarded reporter prints (and what
    produced the +$4.86M headline); the deduped total is the honest number.
    Returns a JSON-safe dict with raw/deduped event counts and totals, the
    |P&L| ratio, the absolute divergence, and a boolean flag:
      flagged = (|raw|/|deduped| > 1.5) OR (|raw - deduped| > $500),
      whichever trips first. The ratio uses absolute totals so
      mirror-duplicated losses cannot cancel and hide the inflation.
    When the deduped total is 0, the ratio is undefined (None) and only
    the absolute test applies. "clean" vs "flagged" is reported in `status`.
    """
    raw_total = 0.0
    raw_count = 0
    for e in journal:
        if e.get("type") in ("trade.closed", "trade.close_corrected"):
            raw_count += 1
            raw_total += effective_profit(e)
    deduped, _raw = dedupe_closes(journal)
    deduped_total = sum(effective_profit(e) for e in deduped)
    divergence = abs(raw_total - deduped_total)
    # Ratio uses |P&L|: mirrored duplicates of the same trades inflate |raw|
    # by roughly the duplication factor regardless of net sign (the
    # 2026-10-17 incident: |$4.82M| / |$253.6K| ~= 19). A signed ratio would
    # cancel and hide exactly the duplication it is meant to catch.
    ratio = (abs(raw_total) / abs(deduped_total)
             if deduped_total != 0 else None)

    flagged = False
    reason = "clean"

    if ratio is not None and ratio > DUPLICATION_RATIO_THRESHOLD:
        flagged = True
        reason = (f"raw/deduped |P&L| ratio {ratio:.2f} exceeds "
                  f"{DUPLICATION_RATIO_THRESHOLD}")
    if divergence > DUPLICATION_ABS_THRESHOLD:
        flagged = True
        reason = (f"absolute divergence ${divergence:,.2f} exceeds "
                  f"${DUPLICATION_ABS_THRESHOLD:,.2f}" +
                  (f"; |P&L| ratio {ratio:.2f}" if ratio is not None else ""))
    return {
        "raw_close_events": raw_count,
        "deduped_close_events": len(deduped),
        "raw_total_pnl": round(raw_total, 2),
        "deduped_total_pnl": round(deduped_total, 2),
        "raw_to_deduped_ratio": round(ratio, 3) if ratio is not None else None,
        "absolute_divergence": round(divergence, 2),
        "flagged": flagged,
        "status": "flagged" if flagged else "clean",
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Benchmark: Freqtrade market_change analog from journaled prices
# ---------------------------------------------------------------------------

def _price_points(journal):
    """(symbol, time_key, price) tuples from price-bearing journal events.

    Sources: signal.received.entry_price, trade.opened.entry_price,
    trade.closed.exit_price. Symbol is resolved via the event's own symbol
    first, then the ticket's trade.opened, then the signal_id's
    signal.received. Points with no usable price or no resolvable symbol
    are dropped (documented as "no benchmark data" downstream).
    """
    ticket_sym = {}
    signal_sym = {}
    for e in journal:
        t = e.get("type")
        if t == "trade.opened" and e.get("ticket") is not None and e.get("symbol"):
            ticket_sym[str(e["ticket"])] = e["symbol"]
        elif t == "signal.received" and e.get("signal_id") and e.get("symbol"):
            signal_sym[str(e["signal_id"])] = e["symbol"]

    def resolve_sym(e):
        if e.get("symbol"):
            return e["symbol"]
        tk = e.get("ticket")
        if tk is not None and str(tk) in ticket_sym:
            return ticket_sym[str(tk)]
        sid = e.get("signal_id")
        if sid and str(sid) in signal_sym:
            return signal_sym[str(sid)]
        return None

    points = []
    for e in journal:
        t = e.get("type")
        price_key = None
        if t == "signal.received":
            price_key = "entry_price"
        elif t == "trade.opened":
            price_key = "entry_price"
        elif t == "trade.closed":
            price_key = "exit_price"
        else:
            continue
        sym = resolve_sym(e)
        try:
            price = float(e.get(price_key))
        except (TypeError, ValueError):
            price = None
        if price is None or not (price == price) or price <= 0.0 or not sym:
            continue
        time_key = str(e.get("time") or "")[:19].replace(".", "-")
        if not time_key:
            continue
        points.append((sym, time_key, price))
    return points


def _all_candidate_symbols(journal):
    """Every symbol seen on a price-bearing event type, even when the price
    itself is missing -- those report {"benchmark_data": "none"} rather
    than vanishing silently."""
    ticket_sym = {}
    signal_sym = {}
    for e in journal:
        t = e.get("type")
        if t == "trade.opened" and e.get("ticket") is not None and e.get("symbol"):
            ticket_sym[str(e["ticket"])] = e["symbol"]
        elif t == "signal.received" and e.get("signal_id") and e.get("symbol"):
            signal_sym[str(e["signal_id"])] = e["symbol"]
    syms = set()
    for e in journal:
        if e.get("type") not in ("signal.received", "trade.opened",
                                 "trade.closed"):
            continue
        sym = e.get("symbol")
        if not sym:
            tk = e.get("ticket")
            if tk is not None and str(tk) in ticket_sym:
                sym = ticket_sym[str(tk)]
            else:
                sid = e.get("signal_id")
                if sid and str(sid) in signal_sym:
                    sym = signal_sym[str(sid)]
        if sym:
            syms.add(sym)
    return syms


def benchmark_comparison(journal):
    """Per-symbol buy-and-hold analog (Freqtrade `market_change` style).

    For each symbol with >= 2 usable journaled price points:
      benchmark_return_pct = (last_price - first_price) / first_price * 100
    over the symbol's own first-to-last journaled price window. This is a
    LONG-ONLY passive spot move -- the strategy trades both directions, so
    treat it as a rough market-context number, not a target. See module
    docstring for the honest limits.

    Symbols without usable price data report {"benchmark_data": "none"}
    rather than an invented number.
    """
    by_symbol = {}
    for sym, time_key, price in _price_points(journal):
        by_symbol.setdefault(sym, []).append((time_key, price))
    out = {}
    for sym in sorted(set(by_symbol) | _all_candidate_symbols(journal)):
        pts = sorted(by_symbol.get(sym, []))
        if len(pts) < 2:
            out[sym] = {"benchmark_data": "none"}
            continue
        first_key, first_price = pts[0]
        last_key, last_price = pts[-1]
        if first_price <= 0.0:
            out[sym] = {"benchmark_data": "none"}
            continue
        out[sym] = {
            "benchmark_return_pct": round((last_price - first_price) /
                                          first_price * 100.0, 3),
            "first_price": first_price,
            "last_price": last_price,
            "first_time": first_key,
            "last_time": last_key,
            "n_price_points": len(pts),
            "method": ("passive long-only spot move: "
                       "(last journaled price - first journaled price) / "
                       "first journaled price, over this symbol's own "
                       "observed window"),
        }
    return out
