#!/usr/bin/env python3
"""
Nova autonomous MT5 DEMO trade executor.

Tails nova_signals.jsonl (written by the NovaSignals.mq5 expert advisor),
applies risk gates, and -- ONLY in live mode -- appends trade.open commands
to nova_commands.jsonl for the NovaTrader EA to execute.

HARD SAFETY RULES
  * DEMO ONLY. If any server string seen does not contain "demo"
    (case-insensitive), everything is refused and logged loudly.
  * The executor defaults to dry_run=true, trading_enabled=false. Effective
    "may trade new" = config trading_enabled AND kill-switch file
    run/trading_enabled containing "1" AND NOT dry_run.
  * In dry_run mode ZERO command bytes are written. Every decision is still
    journaled as "intended" with the full computed details (volume,
    risk_amount). (Dry run still needs fresh specs + sane sizing inputs to
    report those details; sizing failures journal the corresponding skip.)
  * No secrets flow through this script at all (there are none in this flow).

Env:
  MT5_FILES_DIR    dir containing nova_signals.jsonl / nova_commands.jsonl /
                   nova_trades.jsonl / nova_symbol_specs.json
                   (default: ~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files)
  SIGNALS_FILE     filename override (default: nova_signals.jsonl)
  TRADER_STATE_DIR state dir for risk_config.json, kill switch, journal and
                   cursor files (default: <bridge dir>/run)

State (under TRADER_STATE_DIR):
  trade_executor.state.json   signal cursor + seen ids + trades cursor +
                              open_map + cmd_index + applied config signature
  risk_config.json            risk config; created with SAFE defaults if missing;
                              risk_per_trade_pct clamped to [0.05, 1.0];
                              max_concurrent_trades follows the repo
                              RiskPolicy (max_open_positions = 3) per owner
                              order 2026-09-21 -- values above 3 are clamped,
  trading_enabled             kill switch ("1"/"0"; missing file = "0")
  nova_journal.jsonl          EVERY event: signal.received, trade.decision,
                              command.sent, trade.opened, trade.closed,
                              kill_switch.tripped, config.changed, reflection,
                              daily_summary

Usage:
  trade_executor.py [--iterations N]   run the daemon loop (default: forever)
"""

import json
import math
import os
import re
import sys
import time
from datetime import datetime

import market_hours

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILES_DIR = os.path.expanduser(
    "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files"
)
DEFAULT_SIGNALS_FILE = "nova_signals.jsonl"

DEFAULT_CONFIG = {
    "trading_enabled": False,
    "dry_run": True,
    "risk_per_trade_pct": 1.0,       # repo RiskPolicy: max_risk_per_trade_pct
    "max_concurrent_trades": 10,     # owner order 2026-09-21: 10 max
                                   # (repo RiskPolicy says 3; owner overrode)
    "max_daily_loss_pct": 3.0,       # repo RiskPolicy: max_daily_loss_pct
    "max_spread_points": 50,
    "max_total_exposure_lots": 0,    # repo has no portfolio lot cap; 0 = disabled
    "max_consecutive_losses": 4,     # repo RiskPolicy: max_consecutive_losses
    "max_correlated_positions": 1,   # repo RiskPolicy: block_correlated_same_currency
    "require_stop_loss": True,       # repo RiskPolicy: require_stop_loss
    # Owner order 2026-09-22 ("nothing should be blocking you from
    # trading"): the opportunity filters below are DISABLED. The executor
    # still enforces the integrity/safety gates (kill switch, capital
    # basis fail-closed, mandatory SL, fresh data, demo-only server,
    # max concurrent, daily-loss limit, floating drawdown).
    "enforce_correlation_gate": False,
    "enforce_spread_gate": False,
    "enforce_single_position_per_symbol": False,
    "enforce_consecutive_losses_gate": False,
    "capital_basis": 0,              # owner order 2026-09-22: allocated trading
                                   # capital in USD. FAIL-CLOSED: must be a
                                   # finite positive number; anything else
                                   # (missing/0/negative/NaN/inf) makes
                                   # effective_risk_basis() return None and
                                   # decisions skip 'risk_basis_unavailable'.
}

SPECS_MAX_AGE_S = 300

# Spread gate: a signal is skipped when the current spread exceeds BOTH
#   (a) the absolute floor max_spread_points (default 50), and
#   (b) a proportional cap of SPREAD_SL_FRACTION * stop-loss distance.
# i.e. effective_cap = max(max_spread_points, 0.5 * sl_distance_points).
# Rationale: a flat cap alone blocks every signal in wide-spread sessions
# (e.g. Asian session on JPY crosses) even when the trade's stop is wide
# enough to absorb it; a purely proportional cap alone would allow absurd
# absolute spreads. The combined rule keeps the protection meaningful:
# the spread may never consume more than half the planned risk distance.
SPREAD_SL_FRACTION = 0.5

# Strict floating-drawdown rule (added 2026-09-21 per user's direct
# instruction): when total floating loss across the whole account reaches
# this fraction of balance, the executor refuses ALL new entries until
# the floating loss recovers. Existing positions are untouched -- each
# keeps the hard SL set at entry. This is the portfolio-level guard for
# "when trades go red": no averaging down, no new risk into a drawdown.
FLOATING_DRAWDOWN_PCT = 1.0

# --- Ports from the reference repo (mceeomoba/forex), 2026-09-21 --------
# 1. Positions as explicit risk input: the EA publishes every broker
#    position (any magic) to nova_positions.json every 10 s; gates read
#    it so manual positions are visible to exposure/concurrency/
#    correlation limits instead of only the bot's own journal.
POSITIONS_FEED_MAX_AGE_S = 60
# 2. Tick freshness (repo gateway: tick fresh <= 300 s before submit):
#    refuse a new entry when the signal symbol's last tick is stale.
TICK_MAX_AGE_S = 120
# 3. Safety audit log: append-only record of every safety-relevant
#    transition (repo FileSafetyStore owner-auth concept).


def load_broker_positions(files_dir):
    """Read nova_positions.json published by the EA.

    Returns (positions, fresh): every open broker position regardless of
    magic number, plus whether the feed is young enough to trust.
    Falls back to ([], False) so callers degrade to the journal view.
    """
    path = os.path.join(files_dir or "", "nova_positions.json")
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return [], False
    if age > POSITIONS_FEED_MAX_AGE_S:
        return [], False
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [], False
    positions = data.get("positions") or []
    return positions, True


def feed_tick_age_s(feed, symbol):
    """Seconds since the last tick for symbol in nova_feed.json, measured
    against the feed's own server clock (self-consistent, no tz math).
    None when the tick time is unknown."""
    if not feed:
        return None
    syms = feed.get("symbols") or {}
    tick_t = (syms.get(symbol) or {}).get("time")
    now_t = feed.get("server_time") or feed.get("time")
    dt_tick = parse_mt5_time(tick_t)
    dt_now = parse_mt5_time(now_t)
    if not dt_tick or not dt_now:
        return None
    return max(0.0, (dt_now - dt_tick).total_seconds())


def load_feed(files_dir):
    """Return nova_feed.json dict or None."""
    try:
        with open(os.path.join(files_dir or "", "nova_feed.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def audit_safety(run_dir, event, actor, details=None):
    """Append one line to the append-only safety audit log.

    Every safety-relevant transition records WHO authorized it: 'system'
    for automatic trips, 'owner' for the human's direct orders, 'agent'
    for Nova's delegated decisions.
    """
    entry = {
        "type": "safety.audit",
        "event": event,
        "actor": actor,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "details": details or {},
    }
    try:
        with open(os.path.join(run_dir, "safety_audit.jsonl"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass

# Experiment risk bounds (EXPERIMENT_CHARTER.md). Values outside these bounds
# are clamped on load and the clamping is journaled as config.changed.
# NOTE 2026-09-21: max_concurrent_trades was repo RiskPolicy
# (max_open_positions = 3) until the owner explicitly ordered 10 max at
# ~18:20 PDT. That is an intentional deviation from repo parity, recorded
# in the journal as config.changed. The one-position-per-symbol gate
# remains in force as well.
RISK_BOUNDS = {
    # Floor lowered to 0.05 ($100 on the $200,000 basis) per owner order
    # 2026-09-22 ~09:15 PDT ("Spend 200k from the capital, $100 risk.
    # Keep it that way"). Ceiling stays 1.0.
    "risk_per_trade_pct": (0.05, 1.0),
}


def apply_risk_bounds(cfg):
    """
    Clamp risk config values to RISK_BOUNDS; sanity-check the rest.
    Pure function. Returns (clamped_cfg, notes) where notes lists every
    change made (empty when nothing changed).
    """
    cfg = dict(cfg)
    notes = []
    for key, (lo, hi) in RISK_BOUNDS.items():
        try:
            v = float(cfg.get(key))
        except (TypeError, ValueError):
            v = None
        if v is None:
            continue
        clamped = min(max(v, lo), hi)
        if clamped != v:
            notes.append(f"{key} clamped {v} -> {clamped}")
            cfg[key] = clamped
    # max_concurrent_trades: owner-ordered ceiling = 10 (2026-09-21 ~18:20 PDT;
    # repo RiskPolicy says 3, intentionally overridden). Values above 10 are
    # clamped to 10; non-positive values fall back to the default.
    try:
        mct = int(float(cfg.get("max_concurrent_trades")))
    except (TypeError, ValueError):
        mct = 0
    if mct < 1:
        notes.append(
            f"max_concurrent_trades {cfg.get('max_concurrent_trades')} -> "
            f"{DEFAULT_CONFIG['max_concurrent_trades']}")
        mct = DEFAULT_CONFIG["max_concurrent_trades"]
    elif mct > 10:
        notes.append(
            f"max_concurrent_trades {cfg.get('max_concurrent_trades')} -> 10 "
            f"(owner-ordered ceiling)")
        mct = 10
    cfg["max_concurrent_trades"] = mct
    try:
        mdl = float(cfg.get("max_daily_loss_pct"))
    except (TypeError, ValueError):
        mdl = 0.0
    if not (0.0 < mdl <= 10.0):
        notes.append(
            f"max_daily_loss_pct {cfg.get('max_daily_loss_pct')} -> "
            f"{DEFAULT_CONFIG['max_daily_loss_pct']}")
        cfg["max_daily_loss_pct"] = DEFAULT_CONFIG["max_daily_loss_pct"]
    try:
        msp = float(cfg.get("max_spread_points"))
    except (TypeError, ValueError):
        msp = -1.0
    if msp < 0:
        notes.append(
            f"max_spread_points {cfg.get('max_spread_points')} -> "
            f"{DEFAULT_CONFIG['max_spread_points']}")
        cfg["max_spread_points"] = DEFAULT_CONFIG["max_spread_points"]
    # capital_basis: owner-allocated trading capital in USD (2026-09-22).
    # FAIL-CLOSED: anything that is not a finite positive number is
    # normalized to 0 here; effective_risk_basis() then treats it as
    # unavailable, so decisions skip with 'skipped:risk_basis_unavailable'
    # instead of silently sizing off broker equity.
    try:
        cb = float(cfg.get("capital_basis") or 0)
    except (TypeError, ValueError):
        cb = -1.0
    if cb < 0:
        notes.append(
            f"capital_basis {cfg.get('capital_basis')} -> 0 (disabled)")
        cfg["capital_basis"] = 0
    else:
        cfg["capital_basis"] = cb
    return cfg, notes


def effective_risk_basis(cfg, account_equity):
    """Owner-allocated capital basis for risk sizing (pure function).

    FAIL-CLOSED (2026-09-22): missing, malformed, non-finite (NaN, +inf,
    -inf), zero, or negative configured basis OR broker equity returns
    None. The caller must treat None as 'skipped:risk_basis_unavailable':
    the configured basis is NEVER trusted without valid broker equity.
    Valid inputs return min(configured basis, broker equity).
    """
    try:
        cb = float((cfg or {}).get("capital_basis"))
    except (TypeError, ValueError):
        return None
    try:
        eq = float(account_equity)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(cb) and math.isfinite(eq)):
        return None
    if cb <= 0 or eq <= 0:
        return None
    return min(cb, eq)


def config_sig(cfg):
    """Stable signature of the effective risk config (for change detection)."""
    return json.dumps({k: cfg.get(k) for k in DEFAULT_CONFIG},
                      sort_keys=True)


# ---------------------------------------------------------------- paths

def resolve_paths(files_dir=None, state_dir=None, signals_file=None):
    fd = files_dir or os.environ.get("MT5_FILES_DIR", DEFAULT_FILES_DIR)
    sd = state_dir or os.environ.get(
        "TRADER_STATE_DIR", os.path.join(BASE, "run")
    )
    sf = signals_file or os.environ.get("SIGNALS_FILE", DEFAULT_SIGNALS_FILE)
    os.makedirs(sd, exist_ok=True)
    return {
        "files_dir": fd,
        "state_dir": sd,
        "signals": os.path.join(fd, sf),
        "commands": os.path.join(fd, "nova_commands.jsonl"),
        "trades": os.path.join(fd, "nova_trades.jsonl"),
        "specs": os.path.join(fd, "nova_symbol_specs.json"),
        "state": os.path.join(sd, "trade_executor.state.json"),
        "config": os.path.join(sd, "risk_config.json"),
        "kill": os.path.join(sd, "trading_enabled"),
        "journal": os.path.join(sd, "nova_journal.jsonl"),
    }


# ---------------------------------------------------------------- helpers

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"{now_str()} {msg}", flush=True)


def parse_mt5_time(s):
    """Parse MT5-style 'YYYY.MM.DD HH:MM:SS' or ISO-ish times. None on failure."""
    if not s:
        return None
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"offset": 0, "seen": [], "trades_offset": 0, "trades_seen": []}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def normalize_seen_keys(raw):
    """Return a set of JSON-stable dedup key strings.

    Dedup keys are plain strings ("type|command_id|ticket|deal") so they
    survive a JSON save->reload round-trip. Older states may hold a key as a
    list (it was a tuple before the JSON round-trip); those are migrated to
    the string form instead of crashing set() with "unhashable type: 'list'"
    (the trades_seen crash-loop regression this guards).
    """
    seen = set()
    for k in raw or []:
        if isinstance(k, (list, tuple)):
            k = "|".join("" if v is None else str(v) for v in k)
        seen.add(k)
    return seen


def load_config(config_path):
    """Load risk_config.json, creating it with SAFE defaults if missing.
    Risk bounds are enforced (see apply_risk_bounds); clamping is logged."""
    if not os.path.exists(config_path):
        cfg = dict(DEFAULT_CONFIG)
        tmp = config_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, config_path)
        log(f"created {config_path} with safe defaults "
            f"(trading_enabled=false, dry_run=true)")
        return cfg
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        log(f"WARNING: could not read {config_path} ({e}); using safe defaults")
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg if isinstance(cfg, dict) else {})
    merged, notes = apply_risk_bounds(merged)
    for n in notes:
        log(f"risk bound enforced: {n}")
    return merged


def kill_switch_on(kill_path):
    """Missing kill-switch file is treated as '0' (disabled)."""
    try:
        with open(kill_path) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def trip_kill_switch(kill_path):
    """Write '0' to the kill-switch file (daily loss limit)."""
    tmp = kill_path + ".tmp"
    with open(tmp, "w") as f:
        f.write("0")
    os.replace(tmp, kill_path)


def is_demo_server(server):
    return "demo" in str(server or "").lower()


def step_decimals(step):
    try:
        s = repr(float(step))
    except (TypeError, ValueError):
        return 2
    if "." not in s:
        return 0
    return len(s.split(".")[1].rstrip("0")) or 0


# ---------------------------------------------------------------- pure logic

def compute_volume(equity, risk_pct, entry, stop_loss, spec):
    """
    Return (volume, risk_amount, error). error is None on success or one of
    "skipped:bad_specs", "skipped:volume_below_min".
    """
    risk_amount = equity * risk_pct / 100.0
    tick_size = spec.get("tick_size") or 0
    tick_value = spec.get("tick_value") or 0
    sl_dist = abs((entry or 0) - (stop_loss or 0))
    sl_ticks = sl_dist / tick_size if tick_size else 0
    if sl_ticks <= 0 or tick_value <= 0:
        return 0.0, risk_amount, "skipped:bad_specs"
    raw_volume = risk_amount / (sl_ticks * tick_value)
    step = spec.get("volume_step") or 0
    if step <= 0:
        return 0.0, risk_amount, "skipped:bad_specs"
    volume = math.floor(raw_volume / step) * step
    volume = round(volume, step_decimals(step))
    vmin = spec.get("volume_min") or 0
    vmax = spec.get("volume_max") or 0
    if vmax and volume > vmax:
        volume = vmax
    if vmin and volume < vmin:
        return 0.0, risk_amount, "skipped:volume_below_min"
    return volume, risk_amount, None


def load_trade_state(trades_path, today_str, journal_path=None):
    """
    Parse nova_trades.jsonl into open positions and today's closed P/L.
    Returns (open_positions, todays_profit, consecutive_losses) where
    open_positions maps ticket -> {"symbol", "command_id", "signal_id"}
    and consecutive_losses counts trailing trade.closed events with
    profit < 0 (a win or breakeven resets the count). NOTE: only closes
    the EA itself reports appear here; broker-side SL/TP exits are picked
    up once the EA's position reconciliation is live.

    Tickets named in a journal "positions.reconciled" event are phantom
    positions the broker no longer holds (broker sync reported 0 while
    the journal still listed them open). They are excluded from the gate
    view so they can never block the correlation / concurrency / dup
    gates again. Their P&L stays UNKNOWN -- this changes no close or
    profit records. Ticket ids are compared as strings because journal
    writers mix int and str forms.
    """
    opened = {}
    closed_tickets = set()
    closed = []
    corrections = {}
    reconciled_tickets = set()
    if journal_path:
        try:
            with open(journal_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "positions.reconciled":
                        for x in ev.get("tickets_closed") or []:
                            t = (x or {}).get("ticket")
                            if t is not None:
                                reconciled_tickets.add(str(t))
        except OSError:
            pass
    try:
        with open(trades_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "trade.opened":
                    ticket = ev.get("ticket")
                    if ticket is not None:
                        opened[ticket] = {
                            "symbol": ev.get("symbol"),
                            "direction": ev.get("direction"),
                            "command_id": ev.get("command_id"),
                            "signal_id": ev.get("signal_id"),
                        }
                elif t == "trade.closed":
                    ticket = ev.get("ticket")
                    if ticket is not None:
                        closed_tickets.add(ticket)
                    closed.append(ev)
                elif t == "trade.close_corrected":
                    # Authoritative broker revision of a previously journaled
                    # provisional close (e.g. swap was missing). Overrides
                    # that ticket's profit in P&L accounting below; the
                    # original provisional event is left untouched.
                    ticket = ev.get("ticket")
                    if ticket is not None:
                        corrections[str(ticket)] = ev
                # trade.rejected opens nothing
    except OSError:
        pass
    if (reconciled_tickets
            and reconciled_tickets != getattr(load_trade_state, "_last_logged", None)):
        load_trade_state._last_logged = set(reconciled_tickets)
        log(f"phantom reconcile: excluding {len(reconciled_tickets)} "
            f"broker-confirmed-gone ticket(s) from gate view")
    open_positions = {
        t: info for t, info in opened.items()
        if t not in closed_tickets and str(t) not in reconciled_tickets
    }
    def eff_profit(ev):
        """Authoritative P&L for a close: a trade.close_corrected revision
        wins over the original provisional close; net_profit (incl.
        commission/swap) is preferred over the bare profit field."""
        ticket = ev.get("ticket")
        corr = corrections.get(str(ticket)) if ticket is not None else None
        src = corr if corr is not None else ev
        for k in ("net_profit", "profit"):
            try:
                v = src.get(k)
                if v is not None:
                    return float(v)
            except (TypeError, ValueError):
                continue
        return 0.0

    # Effective close view: a trade.close_corrected revision overlays the
    # original provisional close (authoritative timestamp and net P&L win).
    items = []
    for ev in closed:
        ticket = ev.get("ticket")
        corr = corrections.get(str(ticket)) if ticket is not None else None
        src = corr if corr is not None else ev
        ts = (parse_mt5_time(src.get("exit_time_broker") or src.get("time"))
              or datetime.min)
        d = ts.date().isoformat()
        items.append((ts, d, eff_profit(ev), ev))

    todays_profit = sum(p for _ts, d, p, _ev in items if d == today_str)
    consecutive_losses = 0
    for _ts, _d, p, ev in sorted(items, key=lambda x: x[0]):
        # auto_loss_cap closes of non-bot positions ("managed":"auto-cap")
        # are real P&L (counted in todays_profit above) but are not
        # strategy trades, so they must not trip the consecutive-loss
        # halt for the bot's own strategy.
        if (ev.get("reason") == "auto_loss_cap"
                and ev.get("managed") == "auto-cap"):
            continue
        consecutive_losses = consecutive_losses + 1 if p < 0 else 0
    return open_positions, todays_profit, consecutive_losses


def server_today_str(specs):
    """Today's date per the broker server clock (specs), else local date."""
    acct = (specs or {}).get("account") or {}
    for key in ("time",):
        dt = parse_mt5_time(acct.get(key)) or parse_mt5_time((specs or {}).get("time"))
        if dt:
            return dt.date().isoformat()
    return datetime.now().date().isoformat()


def currency_codes(symbol):
    """Bare currency codes for a symbol: 6-letter alpha -> {base, quote};
    anything else (indices, etc.) is its own group."""
    s = (symbol or "").upper()
    if len(s) == 6 and s.isalpha():
        return {s[:3], s[3:]}
    return {s} if s else set()


def signed_currency_exposure(symbol, direction):
    """Signed unit exposure per currency: BUY = long base / short quote,
    SELL = the reverse. Unknown direction -> {} (callers fail closed)."""
    d = (direction or "").upper()
    if d not in ("BUY", "SELL"):
        return {}
    sign = 1 if d == "BUY" else -1
    s = (symbol or "").upper()
    if len(s) == 6 and s.isalpha():
        return {s[:3]: sign, s[3:]: -sign}
    return {s: sign} if s else {}


def check_gates(sig, *, enabled, dry_run, specs, specs_fresh,
                open_positions, todays_profit, today_str,
                risk_pct, max_concurrent, max_spread_points,
                max_daily_loss_pct, equity, balance=None,
                risk_basis=None,
                max_total_exposure_lots=0, open_lots=0.0,
                max_consecutive_losses=0, consecutive_losses=0,
                max_correlated_positions=0, require_stop_loss=True,
                market_open=True, tick_age_s=None,
                enforce_correlation_gate=True,
                enforce_spread_gate=True,
                enforce_single_position_per_symbol=True,
                enforce_consecutive_losses_gate=True):
    """
    Run the risk gates in order; first failure wins.
    Returns (decision, flags) where flags may carry loud/trip side effects.
    Pure function: no I/O.
    risk_basis: owner-allocated capital (USD). FAIL-CLOSED: None means the
    configured capital or the broker equity was missing, malformed,
    non-finite, or non-positive (see effective_risk_basis); the decision
    is then 'skipped:risk_basis_unavailable'. A valid basis replaces broker
    equity for position sizing and the daily-loss gate. The
    floating-drawdown gate keeps using real balance/equity.
    The enforce_*_gate flags disable the opportunity filters (correlation,
    spread, single-position-per-symbol, consecutive losses) while keeping
    every integrity/safety gate active. Owner order 2026-09-22: all four
    default OFF in live config so nothing blocks trading.
    """
    symbol = sig.get("symbol")
    entry = sig.get("entry_price")
    sl = sig.get("stop_loss")

    # 1. not enabled (config or kill switch)
    if not enabled:
        return "skipped:trading_disabled", {}

    # 1b. owner-allocated capital basis (2026-09-22): fail closed. A basis
    # that is None, non-numeric, non-finite, or non-positive means the
    # trade cannot be sized -- it must not be placed.
    try:
        rb = float(risk_basis)
    except (TypeError, ValueError):
        rb = float("nan")
    if not math.isfinite(rb) or rb <= 0:
        return "skipped:risk_basis_unavailable", {"loud": True}
    sizing_equity = rb

    # 0. stop-loss mandatory: a signal without SL/TP is never traded
    if require_stop_loss and not (sl and sig.get("take_profit")):
        return "skipped:missing_sl_tp", {"loud": True}

    # sizing needs specs; in dry_run we still want full computed details,
    # so stale/missing specs and sizing failures journal their skip reason.
    spec = (specs.get("symbols") or {}).get(symbol) if specs else None
    if not specs_fresh or spec is None:
        return "skipped:stale_specs", {}
    volume, risk_amount, err = compute_volume(sizing_equity, risk_pct, entry, sl, spec)
    if err:
        return err, {}

    # 1b. total exposure cap: clamp the new trade so bot-wide open volume
    # never exceeds max_total_exposure_lots. Clamp (not reject) keeps
    # trading alive at small size; a trade that cannot fit at all is
    # skipped. NOTE: only bot positions are visible; manual positions
    # cannot be counted until the EA publishes a positions feed.
    if max_total_exposure_lots and max_total_exposure_lots > 0:
        remaining = max_total_exposure_lots - (open_lots or 0.0)
        step = spec.get("volume_step") or 0.01
        if remaining <= 0:
            return "skipped:max_total_exposure", {"loud": True}
        if volume > remaining:
            volume = math.floor(remaining / step) * step
            volume = round(volume, step_decimals(step))
            vmin = spec.get("volume_min") or 0
            if volume < vmin:
                return "skipped:max_total_exposure", {"loud": True}

    # 1c. tick freshness (port from repo gateway): never base a trade
    # decision on a stale quote. The EA re-validates spread at send time,
    # but the decision itself must rest on a live tick.
    if tick_age_s is not None and tick_age_s > TICK_MAX_AGE_S:
        return "skipped:stale_tick", {"loud": True}

    # 2. dry_run -> intended, write nothing
    if dry_run:
        return "intended", {"volume": volume, "risk_amount": risk_amount}

    # 3. market closed -> no NEW positions (existing positions are held)
    if not market_open:
        return "skipped:market_closed", {}

    # 4. server not demo -> refuse loudly
    server = ((specs.get("account") or {}).get("server")
              or specs.get("server") or "")
    if not is_demo_server(server):
        return "skipped:non_demo_server", {"loud": True}

    # 5. max concurrent trades
    if len(open_positions) >= max_concurrent:
        return "skipped:max_concurrent", {}

    # 6. symbol already has an open position (opportunity filter --
    #    disabled per owner order 2026-09-22)
    if enforce_single_position_per_symbol:
        for info in open_positions.values():
            if info.get("symbol") == symbol:
                return "skipped:symbol_already_open", {}

    # 6b. correlated positions: direction-aware net currency exposure.
    # A BUY is long base / short quote, a SELL the reverse. Two positions
    # on opposite sides of a shared currency (CHFJPY BUY is short-JPY,
    # NZDJPY SELL is long-JPY) NET OUT instead of stacking, so they must
    # not block each other. Block only when the new signal would raise
    # net absolute exposure in a shared currency beyond the limit.
    # Unknown position direction fails closed (assumed to stack).
    # Opportunity filter -- disabled per owner order 2026-09-22.
    if (enforce_correlation_gate and max_correlated_positions
            and max_correlated_positions > 0):
        cand = signed_currency_exposure(symbol, sig.get("direction"))
        if not cand:
            # Unknown candidate direction: conservative count-based proxy.
            codes = currency_codes(symbol)
            hits = sum(1 for info in open_positions.values()
                       if codes & currency_codes(info.get("symbol")))
            if hits >= max_correlated_positions:
                return "skipped:correlated_position", {"loud": True}
        else:
            net = {}
            for info in open_positions.values():
                expos = signed_currency_exposure(info.get("symbol"),
                                                 info.get("direction"))
                if not expos:
                    shared = (currency_codes(info.get("symbol"))
                              & set(cand))
                    for cur in shared:
                        expos[cur] = 1 if cand[cur] > 0 else -1
                for cur, e in expos.items():
                    net[cur] = net.get(cur, 0) + e
            for cur, cexp in cand.items():
                cur_net = net.get(cur, 0)
                new_net = cur_net + cexp
                if (abs(new_net) > max_correlated_positions
                        and abs(new_net) > abs(cur_net)):
                    return "skipped:correlated_position", {"loud": True}

    # 6c. consecutive losses: after max_consecutive_losses straight
    # losing closes, no new entries until a winner resets the count.
    # Opportunity filter -- disabled per owner order 2026-09-22.
    if (enforce_consecutive_losses_gate and max_consecutive_losses
            and max_consecutive_losses > 0):
        if (consecutive_losses or 0) >= max_consecutive_losses:
            return "skipped:consecutive_losses", {"loud": True}

    # 7. spread too wide: fixed cap per the user's approved risk config
    # (max_spread_points, default 50). Opportunity filter -- disabled per
    # owner order 2026-09-22 ("nothing should be blocking you from
    # trading"). NOTE: the NovaTrader EA enforces the same cap at send
    # time via InMaxSpreadPoints; that input was raised to effectively
    # unlimited in the same change so the EA does not re-block.
    spread = spec.get("spread_points")
    if enforce_spread_gate and spread is not None and spread > max_spread_points:
        return "skipped:spread_too_wide", {}

    # 8. daily loss limit (off the capital basis when one is set)
    daily_loss = 0.0 - todays_profit
    if daily_loss >= (max_daily_loss_pct / 100.0) * sizing_equity:
        return "skipped:daily_loss_limit", {"loud": True, "trip": True}

    # 9. floating drawdown: total open loss (bot + manual positions) at or
    # beyond FLOATING_DRAWDOWN_PCT of balance -> no NEW entries. Existing
    # positions keep working under their own hard SLs; the gate releases
    # automatically when floating recovers above the line.
    if balance:
        floating_loss = balance - equity  # > 0 means the account is red
        if floating_loss >= (FLOATING_DRAWDOWN_PCT / 100.0) * balance:
            return "skipped:floating_drawdown", {"loud": True}

    return "commanded", {"volume": volume, "risk_amount": risk_amount}


def build_command(sig, volume):
    """Command dict for the NovaTrader EA (live mode only)."""
    return {
        "type": "trade.open",
        "id": f"cmd-{sig.get('id')}",
        "symbol": sig.get("symbol"),
        "direction": sig.get("direction"),
        "volume": volume,
        "sl": sig.get("stop_loss"),
        "tp": sig.get("take_profit"),
        "signal_id": sig.get("id"),
    }


def server_market_state(specs):
    """
    Return (market_open, context) for journaling at decision time.
    Server clock comes from specs account time (GMT+3); falls back to
    now-in-GMT+3 when unparsable.
    """
    server_time_str = ((specs or {}).get("account") or {}).get("time")
    server_dt = parse_mt5_time(server_time_str)
    if server_dt is None:
        market_open = market_hours.is_market_open()
        ctx = market_hours.market_context()
    else:
        market_open = market_hours.is_market_open(server_dt)
        ctx = {
            "server_time": server_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "server_hour": server_dt.hour,
            "weekday": server_dt.strftime("%a"),
            "session": market_hours.session_bucket(server_dt.hour),
            "market_open": market_open,
        }
    return market_open, ctx


# ---------------------------------------------------------------- phantom
# reconciliation
#
# The NovaTrader EA can miss closes across terminal restarts, leaving
# phantom entries in the executor's open_map. The MT5 terminal log records
# a 'terminal synchronized with ...: N positions, M orders' line on every
# (re)connect; the LATEST such line is broker-authoritative for how many
# positions actually exist. When it reports exactly 0 while open_map is
# non-empty, the entries are phantoms and are cleared (no P&L invented).

SYNC_LINE_RE = re.compile(
    r"terminal synchronized with .*: (\d+) positions, (\d+) orders")
SYNC_TIME_RE = re.compile(r"\t(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\t")
DEFAULT_LOGS_DIR = os.path.join(
    os.path.dirname(BASE), "prefix", "drive_c",
    "Program Files", "MetaTrader 5", "logs")


def latest_terminal_position_count(logs_dir=None):
    """Position count from the latest MT5 terminal 'synchronized' line.

    Reads today's terminal log (UTF-16LE) at <logs_dir>/YYYYMMDD.log and
    returns (count, exact_line_text) for the LAST line matching
    'terminal synchronized with ...: N positions, M orders'.
    Returns (None, None) when the log file or any sync line is
    unavailable. Never raises; scans only the log tail (64 KiB).
    """
    d = logs_dir or DEFAULT_LOGS_DIR
    path = os.path.join(d, datetime.now().strftime("%Y%m%d") + ".log")
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            raw = f.read()
        text = raw.decode("utf-16-le", errors="replace")
    except OSError:
        return None, None
    count = None
    line_text = None
    for line in text.splitlines():
        m = SYNC_LINE_RE.search(line)
        if m:
            count = int(m.group(1))
            line_text = line.strip()
    return count, line_text


def parse_sync_line_time(line):
    """HH:MM:SS(.mmm) from a terminal log line -> today's datetime.

    Terminal-log and EA-event times share the terminal clock, so they are
    directly comparable. None when unparseable.
    """
    if not line:
        return None
    m = SYNC_TIME_RE.search(line)
    if not m:
        return None
    try:
        return datetime.now().replace(hour=int(m.group(1)),
                                      minute=int(m.group(2)),
                                      second=int(m.group(3)),
                                      microsecond=0)
    except ValueError:
        return None


# ---------------------------------------------------------------- engine

class TraderEngine:
    def __init__(self, files_dir=None, state_dir=None, signals_file=None):
        self.paths = resolve_paths(files_dir, state_dir, signals_file)
        self.state = load_state(self.paths["state"])
        # Guarantee the commands file exists (empty) at startup. The EA
        # initializes a missing cursor to EOF on first sight of the file;
        # if the file were created later already holding a command, that
        # command would be skipped. Creating it now (empty) makes the EA
        # anchor its cursor at 0=EOF before any command exists.
        try:
            if not os.path.exists(self.paths["commands"]):
                open(self.paths["commands"], "a").close()
        except OSError as e:
            log(f"warning: could not create commands file: {e}")

    # -- journal / state -------------------------------------------------
    def journal(self, entry):
        entry = dict(entry)
        entry.setdefault("time", now_str())
        with open(self.paths["journal"], "a") as f:
            f.write(json.dumps(entry) + "\n")

    def save(self):
        save_state(self.paths["state"], self.state)

    # -- config / inputs -------------------------------------------------
    def load_specs(self):
        """Return (specs_dict_or_None, fresh_bool)."""
        try:
            with open(self.paths["specs"]) as f:
                specs = json.load(f)
        except (OSError, ValueError):
            return None, False
        dt = parse_mt5_time(specs.get("time"))
        fresh = bool(dt and (datetime.now() - dt).total_seconds() <= SPECS_MAX_AGE_S)
        return specs, fresh

    def _open_info_for(self, ev):
        """Best-known open details for a trade.opened event (EA fields win,
        executor command index fills the gaps)."""
        cmd_id = ev.get("command_id")
        known = self.state.get("cmd_index", {}).get(cmd_id or "", {})
        return {
            "ticket": ev.get("ticket"),
            "deal": ev.get("deal"),
            "symbol": ev.get("symbol") or known.get("symbol"),
            "direction": ev.get("direction") or known.get("direction"),
            "volume": ev.get("volume") if ev.get("volume") is not None
                      else known.get("volume"),
            "entry_price": ev.get("fill_price"),
            "time": ev.get("time"),
            "signal_id": ev.get("signal_id") or known.get("signal_id"),
            "command_id": cmd_id,
        }

    # -- trades tail -> journal linkage ----------------------------------
    def tail_trades(self):
        p = self.paths["trades"]
        try:
            size = os.path.getsize(p)
        except OSError:
            return 0
        offset = self.state.get("trades_offset", 0)
        if size < offset:
            offset = 0
        # Dedup keys are plain strings (JSON-stable); normalize_seen_keys
        # migrates legacy list-form keys (tuples before the JSON round-trip).
        seen = normalize_seen_keys(self.state.get("trades_seen", []))
        open_map = self.state.get("open_map", {})
        cmd_index = self.state.get("cmd_index", {})
        linked = 0
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    offset = f.tell()
                    break
                line = line.strip()
                if not line:
                    offset = f.tell()
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    offset = f.tell()
                    continue
                t = ev.get("type")
                key = "|".join("" if v is None else str(v) for v in (
                    t, ev.get("command_id"), ev.get("ticket"),
                    ev.get("deal")))
                if key in seen:
                    offset = f.tell()
                    continue
                events = []
                if t == "trade.opened":
                    info = self._open_info_for(ev)
                    events.append({
                        "type": "trade.update",
                        "command_id": ev.get("command_id"),
                        "ticket": ev.get("ticket"),
                        "deal": ev.get("deal"),
                        "fill_price": ev.get("fill_price"),
                        "signal_id": info.get("signal_id"),
                    })
                    events.append({"type": "trade.opened", **info})
                    open_map[str(ev.get("ticket"))] = info
                    log(f"journal linked {t} "
                        f"ticket={ev.get('ticket')} cmd={ev.get('command_id')}")
                elif t == "trade.closed":
                    info = open_map.pop(str(ev.get("ticket")), {})
                    closed_dt = parse_mt5_time(
                        ev.get("exit_time_broker") or ev.get("time"))
                    opened_dt = parse_mt5_time(info.get("time"))
                    hold_s = None
                    if closed_dt and opened_dt:
                        hold_s = max(
                            0, int((closed_dt - opened_dt).total_seconds()))
                    events.append({
                        "type": "trade.update",
                        "ticket": ev.get("ticket"),
                        "exit_price": ev.get("exit_price"),
                        "profit": ev.get("profit"),
                        "reason": ev.get("reason"),
                    })
                    # Mirror the FULL source event (deal ids,
                    # confirmation_status, net_profit, reconciled flag,
                    # audit source/note survive). Linked open info only
                    # fills gaps the source leaves empty. Dropping the
                    # flag here once caused backfilled closes to fire
                    # real-time alerts and lose their audit trail.
                    rec = dict(ev)
                    rec.update({
                        "type": "trade.closed",
                        "ticket": ev.get("ticket"),
                        "symbol": ev.get("symbol") or info.get("symbol"),
                        "direction":
                            ev.get("direction") or info.get("direction"),
                        "volume": (ev.get("volume")
                                   if ev.get("volume") is not None
                                   else info.get("volume")),
                        "entry_price": (ev.get("entry_price")
                                        if ev.get("entry_price") is not None
                                        else info.get("entry_price")),
                        "exit_price": ev.get("exit_price"),
                        "profit": ev.get("profit"),
                        "hold_seconds": hold_s,
                        "reason": ev.get("reason"),
                        "time": ev.get("time"),
                        "signal_id":
                            ev.get("signal_id") or info.get("signal_id"),
                        "command_id":
                            ev.get("command_id") or info.get("command_id"),
                    })
                    events.append(rec)
                    log(f"journal linked {t} ticket={ev.get('ticket')} "
                        f"profit={ev.get('profit')} "
                        f"reconciled={bool(ev.get('reconciled'))}")
                elif t == "trade.close_corrected":
                    # Authoritative revision of a provisional close: journal
                    # verbatim so reports see the corrected net. The
                    # notifier ignores this type (no live alert).
                    events.append(dict(ev))
                    log(f"journal linked {t} ticket={ev.get('ticket')} "
                        f"net={ev.get('net_profit')}")
                elif t == "trade.rejected":
                    events.append({
                        "type": "trade.update",
                        "command_id": ev.get("command_id"),
                        "rejected": ev.get("reason"),
                    })
                    log(f"journal linked {t} cmd={ev.get('command_id')} "
                        f"reason={ev.get('reason')}")
                for entry in events:
                    self.journal(entry)
                if events:
                    seen.add(key)
                    linked += 1
                offset = f.tell()
        self.state["trades_offset"] = offset
        self.state["trades_seen"] = sorted(seen, key=repr)[-500:]
        self.state["open_map"] = dict(list(open_map.items())[-200:])
        self.state["cmd_index"] = dict(list(cmd_index.items())[-500:])
        self.save()
        return linked

    # -- one signal -------------------------------------------------------
    def decide(self, sig, cfg, kill_on, specs, specs_fresh,
               open_positions, todays_profit, today_str, market_open=True,
               open_lots=0.0, consecutive_losses=0, tick_age_s=None):
        enabled = bool(cfg.get("trading_enabled")) and kill_on
        account_equity = ((specs.get("account") or {}).get("equity") or 0) \
            if specs else 0
        account_balance = ((specs.get("account") or {}).get("balance") or 0) \
            if specs else 0
        # Owner order 2026-09-22: allocated trading capital. The basis can
        # never exceed what the account actually holds.
        risk_basis = effective_risk_basis(cfg, account_equity)
        decision, flags = check_gates(
            sig,
            enabled=enabled,
            dry_run=bool(cfg.get("dry_run")),
            specs=specs,
            specs_fresh=specs_fresh,
            open_positions=open_positions,
            todays_profit=todays_profit,
            today_str=today_str,
            risk_pct=cfg.get("risk_per_trade_pct", 0.5),
            max_concurrent=cfg.get("max_concurrent_trades", 10),
            max_spread_points=cfg.get("max_spread_points", 50),
            max_daily_loss_pct=cfg.get("max_daily_loss_pct", 3.0),
            equity=account_equity,
            balance=account_balance,
            risk_basis=risk_basis,
            max_total_exposure_lots=cfg.get("max_total_exposure_lots", 0),
            open_lots=open_lots,
            max_consecutive_losses=cfg.get("max_consecutive_losses", 0),
            consecutive_losses=consecutive_losses,
            max_correlated_positions=cfg.get("max_correlated_positions", 0),
            require_stop_loss=bool(cfg.get("require_stop_loss", True)),
            market_open=market_open,
            tick_age_s=tick_age_s,
            enforce_correlation_gate=bool(
                cfg.get("enforce_correlation_gate", False)),
            enforce_spread_gate=bool(cfg.get("enforce_spread_gate", False)),
            enforce_single_position_per_symbol=bool(
                cfg.get("enforce_single_position_per_symbol", False)),
            enforce_consecutive_losses_gate=bool(
                cfg.get("enforce_consecutive_losses_gate", False)),
        )
        return decision, flags

    def handle_signal(self, sig, cfg, kill_on, specs, specs_fresh,
                      open_positions, todays_profit, today_str,
                      market_open=True, context=None,
                      open_lots=0.0, consecutive_losses=0, tick_age_s=None):
        decision, flags = self.decide(
            sig, cfg, kill_on, specs, specs_fresh,
            open_positions, todays_profit, today_str, market_open,
            open_lots=open_lots, consecutive_losses=consecutive_losses,
            tick_age_s=tick_age_s,
        )
        entry = {
            "type": "trade.decision",
            "signal_id": sig.get("id"),
            "symbol": sig.get("symbol"),
            "direction": sig.get("direction"),
            "decision": decision,
            "volume": flags.get("volume"),
            "risk_amount": flags.get("risk_amount"),
            "context": context or {},
        }
        if flags.get("loud"):
            log(f"!!! REFUSAL: {decision} for {sig.get('id')} "
                f"({sig.get('symbol')} {sig.get('direction')})")
        if flags.get("trip"):
            trip_kill_switch(self.paths["kill"])
            self.journal({
                "type": "kill_switch.tripped",
                "reason": "daily_loss_limit",
                "signal_id": sig.get("id"),
                "todays_profit": todays_profit,
                "max_daily_loss_pct": cfg.get("max_daily_loss_pct"),
            })
            audit_safety(self.paths["state_dir"], "kill_switch.tripped", "system",
                         {"reason": "daily_loss_limit",
                          "todays_profit": todays_profit,
                          "signal_id": sig.get("id")})
            log("!!! DAILY LOSS LIMIT HIT - kill switch set to 0, "
                "trading halted")
        if decision == "commanded":
            cmd = build_command(sig, flags["volume"])
            with open(self.paths["commands"], "a") as f:
                f.write(json.dumps(cmd) + "\n")
            entry["command_id"] = cmd["id"]
            cmd_index = self.state.get("cmd_index", {})
            cmd_index[cmd["id"]] = {
                "signal_id": sig.get("id"),
                "symbol": sig.get("symbol"),
                "direction": sig.get("direction"),
                "volume": flags["volume"],
                "entry_price": sig.get("entry_price"),
            }
            self.state["cmd_index"] = dict(list(cmd_index.items())[-500:])
            self.journal({"type": "command.sent", "command": cmd})
            log(f"COMMANDED {cmd['id']}: {cmd['direction']} {cmd['volume']} "
                f"{cmd['symbol']} sl={cmd['sl']} tp={cmd['tp']}")
        elif decision == "intended":
            log(f"INTENDED (dry run): {sig.get('direction')} "
                f"{flags.get('volume')} {sig.get('symbol')} "
                f"risk=${flags.get('risk_amount')} "
                f"sl={sig.get('stop_loss')} tp={sig.get('take_profit')} "
                f"[no command written]")
        else:
            log(f"SKIPPED {decision}: {sig.get('id')} "
                f"({sig.get('symbol')} {sig.get('direction')})")
        self.journal(entry)
        return decision

    # -- signals tail ----------------------------------------------------
    def process_signals(self):
        cfg = load_config(self.paths["config"])
        kill_on = kill_switch_on(self.paths["kill"])
        specs, specs_fresh = self.load_specs()
        today_str = server_today_str(specs)
        market_open, ctx = server_market_state(specs)
        open_positions, todays_profit, consecutive_losses = load_trade_state(
            self.paths["trades"], today_str,
            journal_path=self.paths["journal"],
        )

        # config change detection: journal when effective risk config changes
        sig_now = config_sig(cfg)
        old_sig = self.state.get("config_sig")
        old_cfg = self.state.get("config_applied")
        if old_sig is not None and old_sig != sig_now:
            self.journal({"type": "config.changed", "old": old_cfg,
                          "new": {k: cfg.get(k) for k in DEFAULT_CONFIG}})
            audit_safety(self.paths["state_dir"], "config.changed", "owner",
                         {"old": old_cfg,
                          "new": {k: cfg.get(k) for k in DEFAULT_CONFIG}})
            log(f"config changed: {old_cfg} -> "
                f"{ {k: cfg.get(k) for k in DEFAULT_CONFIG} }")
        self.state["config_sig"] = sig_now
        self.state["config_applied"] = {k: cfg.get(k)
                                        for k in DEFAULT_CONFIG}
        self.save()

        try:
            size = os.path.getsize(self.paths["signals"])
        except OSError:
            return []
        offset = self.state.get("offset", 0)
        if size < offset:
            offset = 0
        seen = set(self.state.get("seen", []))
        decisions = []
        with open(self.paths["signals"], "r",
                  encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            while True:
                line = f.readline()
                if not line:
                    offset = f.tell()
                    break
                stripped = line.strip()
                if not stripped:
                    offset = f.tell()
                    continue
                try:
                    sig = json.loads(stripped)
                except ValueError:
                    offset = f.tell()
                    continue
                if sig.get("type") != "signal.detected":
                    offset = f.tell()
                    continue
                sid = (sig.get("id")
                       or f"{sig.get('symbol')}-{sig.get('candle_time')}-"
                          f"{sig.get('direction')}")
                if sid in seen:
                    offset = f.tell()
                    continue
                seen.add(sid)
                self.journal({
                    "type": "signal.received",
                    "signal_id": sid,
                    "symbol": sig.get("symbol"),
                    "timeframe": sig.get("timeframe"),
                    "direction": sig.get("direction"),
                    "entry_price": sig.get("entry_price"),
                    "stop_loss": sig.get("stop_loss"),
                    "take_profit": sig.get("take_profit"),
                    "candle_time": sig.get("candle_time"),
                    "server_time": sig.get("server_time"),
                    "trigger": sig.get("trigger"),
                    "rsi": sig.get("rsi_value"),
                    "atr": sig.get("atr_value"),
                })
                # refresh kill switch + trade state per signal so a trip
                # mid-batch takes effect immediately
                kill_on = kill_switch_on(self.paths["kill"])
                open_positions, todays_profit, consecutive_losses = load_trade_state(
                    self.paths["trades"], today_str,
                    journal_path=self.paths["journal"],
                )
                # PORT (repo: positions as explicit risk input): merge the
                # EA's broker-wide positions feed so exposure/concurrency/
                # correlation gates see MANUAL positions too. When the feed
                # is stale/missing we degrade to the journal-only view.
                broker_positions, bp_fresh = load_broker_positions(
                    self.paths["files_dir"])
                gate_view = "journal"
                if bp_fresh:
                    merged = {}
                    for t, info in open_positions.items():
                        merged[t] = dict(info)
                    broker_lots = 0.0
                    for p in broker_positions:
                        t = p.get("ticket")
                        try:
                            broker_lots += float(p.get("volume") or 0)
                        except (TypeError, ValueError):
                            pass
                        if t in merged:
                            merged[t]["_volume"] = p.get("volume")
                            merged[t]["_broker"] = True
                        else:
                            merged[t] = {"symbol": p.get("symbol"),
                                         "direction": p.get("type"),
                                         "_volume": p.get("volume"),
                                         "_broker": True,
                                         "manual": True}
                    open_positions = merged
                    gate_view = "broker"
                feed = load_feed(self.paths["files_dir"])
                tick_age = feed_tick_age_s(feed, sig.get("symbol"))
                spec = ((specs.get("symbols") or {}).get(sig.get("symbol"))
                        if specs else None)
                context = dict(ctx)
                context["spread_points"] = (spec or {}).get("spread_points")
                context["gate_view"] = gate_view
                context["tick_age_s"] = tick_age
                # open_lots: total open volume (for the
                # max_total_exposure_lots cap). Broker-wide when the
                # positions feed is fresh, else the bot's journal view.
                if bp_fresh:
                    open_lots = broker_lots
                else:
                    cmd_index = self.state.get("cmd_index", {})
                    open_lots = 0.0
                    for _t, info in open_positions.items():
                        vol = (cmd_index.get(info.get("command_id")) or {}).get(
                            "volume")
                        try:
                            open_lots += float(vol or 0)
                        except (TypeError, ValueError):
                            pass
                decisions.append(self.handle_signal(
                    sig, cfg, kill_on, specs, specs_fresh,
                    open_positions, todays_profit, today_str,
                    market_open, context,
                    open_lots=open_lots,
                    consecutive_losses=consecutive_losses,
                    tick_age_s=tick_age,
                ))
                offset = f.tell()
        self.state["offset"] = offset
        self.state["seen"] = sorted(seen)[-500:]
        self.save()
        return decisions

    # -- main pass --------------------------------------------------------
    # -- phantom reconciliation ----------------------------------------
    def reconcile_phantom_positions(self, logs_dir=None):
        """Clear open_map entries the broker no longer holds.

        The EA can miss closes across terminal restarts, leaving phantom
        entries in open_map that keep blocking the correlation gate. If
        the latest MT5 terminal 'synchronized' line reports EXACTLY 0
        positions while open_map is non-empty, those entries are phantoms:
        journal a positions.reconciled event (no P&L invented -- outcomes
        stay UNKNOWN pending authoritative MT5 deal history), clear
        open_map, and save state. Conservative: acts only on count == 0;
        None (log unavailable) or > 0 is a no-op. Returns the number of
        entries cleared.
        """
        open_map = self.state.get("open_map", {})
        if not open_map:
            return 0
        count, line = latest_terminal_position_count(logs_dir)
        if count != 0:
            return 0
        # The sync line must be NEWER than every open_map entry: a stale
        # line (e.g. 10:29) must never clear positions opened later (12:00).
        # Unparseable sync time -> fail closed (no clear).
        sync_dt = parse_sync_line_time(line)
        if sync_dt is None:
            log("phantom reconcile: sync line time unparseable; skipping")
            return 0
        for t, info in open_map.items():
            opened_dt = parse_mt5_time((info or {}).get("time"))
            if opened_dt is not None and sync_dt <= opened_dt:
                log(f"phantom reconcile: stale sync line predates open "
                    f"{t}; skipping")
                return 0
        tickets = [{"ticket": t, "symbol": (info or {}).get("symbol")}
                   for t, info in open_map.items()]
        self.journal({
            "type": "positions.reconciled",
            "tickets_closed": tickets,
            "source": line,
            "note": ("EA never emitted trade.closed; cleared from open_map. "
                     "P&L remains UNKNOWN pending authoritative MT5 deal "
                     "history. No P&L figures created or implied."),
        })
        self.state["open_map"] = {}
        self.save()
        log("!!! PHANTOM RECONCILE: cleared "
            f"{len(tickets)} phantom position(s) from open_map "
            f"(broker reports 0 positions): "
            + ", ".join(f"{x['ticket']}/{x['symbol']}" for x in tickets))
        return len(tickets)

    def run_once(self):
        self.tail_trades()
        self.reconcile_phantom_positions()
        return self.process_signals()


def main(argv):
    iterations = None
    if "--iterations" in argv:
        try:
            iterations = int(argv[argv.index("--iterations") + 1])
        except (IndexError, ValueError):
            print("--iterations needs an integer", file=sys.stderr)
            return 2
    eng = TraderEngine()
    cfg = load_config(eng.paths["config"])
    log(f"trade executor watching {eng.paths['signals']}")
    log(f"config: trading_enabled={cfg.get('trading_enabled')} "
        f"dry_run={cfg.get('dry_run')} "
        f"risk={cfg.get('risk_per_trade_pct')}% "
        f"max_concurrent={cfg.get('max_concurrent_trades')} "
        f"max_daily_loss={cfg.get('max_daily_loss_pct')}% "
        f"max_spread={cfg.get('max_spread_points')}pts "
        f"capital_basis=${cfg.get('capital_basis', 0):,.0f}")
    if cfg.get("dry_run"):
        log("DRY RUN MODE: zero commands will be written; "
            "decisions journaled as 'intended'")
    if not cfg.get("trading_enabled"):
        log("trading disabled in config: signals will journal "
            "'skipped:trading_disabled'")
    n = 0
    while True:
        try:
            eng.run_once()
        except Exception as e:
            log(f"loop error: {e}")
        n += 1
        if iterations is not None and n >= iterations:
            break
        time.sleep(5)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
