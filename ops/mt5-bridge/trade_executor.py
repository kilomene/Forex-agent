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

Execution integrity (2026-09-23, Worker B):
  * STARTUP VALIDATION: main() pins the broker identity via trading_mode
    (load_mode -> fresh account block from nova_symbol_specs.json ->
    verify_identity). ANY failure journals mode.unavailable and exits
    non-zero -- the daemon never runs against an unverified account.
  * ACCOUNT IDENTITY LOCK: every broker-bound command (open/modify/close)
    re-verifies the freshest account block before command.sent; a mismatch
    aborts that operation only (account.identity_mismatch) -- the daemon
    keeps running and never connects anywhere else.
  * DURABLE IDEMPOTENCY: idempotency.py claims a sha256 order key before
    any command byte is written; duplicates are suppressed
    (execution.duplicate_suppressed) across restarts.
  * ORDER TIMEOUT: sent commands with no broker response within 60 s become
    EXECUTION_UNKNOWN and are reconciled against broker state before a
    single permitted resend -- never blind-retried.
  * FILLING MODES: a spec-provided filling_mode is carried in the command;
    an explicitly unsupported mode rejects the order (filling_mode_unsupported).
  * DISCONNECT: broker data stale beyond 300 s halts ALL new entries
    (mode.unavailable DEMO_UNAVAILABLE/LIVE_UNAVAILABLE) while risk
    management continues; fresh data journals mode.recovered and resumes.
    The executor only ever polls the pinned account -- no auto-switching.
"""

import json
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

import market_hours

try:
    # Worker A module: trading modes + broker identity. May not be landed
    # yet; startup validation fails closed when it is absent (see
    # TraderEngine.startup_validate) -- never continue unverified.
    import trading_mode as _trading_mode
    TRADING_MODE_AVAILABLE = True
    TRADING_MODE_IMPORT_ERROR = None
except ImportError as _e:
    _trading_mode = None
    TRADING_MODE_AVAILABLE = False
    TRADING_MODE_IMPORT_ERROR = _e

from idempotency import (IdempotencyStore, make_idempotency_key, parse_iso,
                         utcnow_iso)

# trading_mode.py is owned by Worker A and may not exist yet in the working
# tree. Code against its interface defensively: when it is absent the
# engine runs in local DEMO mode with the mode-identity checks disabled
# (documented); the moment the module lands, decide() enforces them.
try:
    import trading_mode as _trading_mode
    _TRADING_MODE_AVAILABLE = True
except ImportError:
    _trading_mode = None
    _TRADING_MODE_AVAILABLE = False

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
    "ai_veto_enabled": False,      # Worker C 2026-09-23: AI advisory veto.
                                   # False = advisory only (zero behavior
                                   # change). True lets a validated AI
                                   # NO_TRADE veto a commanded signal.
    "emergency_drawdown_pct": 3.0, # Worker C 2026-09-23: per-mode emergency
                                   # drawdown that trips the kill switch.
}

SPECS_MAX_AGE_S = 300

# --- Execution-integrity upgrade (Worker B, 2026-09-23) -------------------
# Startup validation refuses broker data older than this: stricter than the
# 300 s gate freshness because we pin the account identity at startup and
# will not pin it to a stale feed.
SPECS_STARTUP_MAX_AGE_S = 120
# Per-order identity lock: the account block must be this fresh or the
# single operation aborts (the daemon itself keeps running).
IDENTITY_FRESH_S = 120
# Disconnect behavior: broker data (specs file mtime and/or feed server
# time) older than DISCONNECT_STALE_S halts ALL new entries and journals
# mode.unavailable (DEMO_UNAVAILABLE / LIVE_UNAVAILABLE); data fresher
# than DISCONNECT_RECOVER_S journals mode.recovered and resumes.
DISCONNECT_STALE_S = 300
DISCONNECT_RECOVER_S = 120
# Order timeout: a sent command with no broker response (no
# trade.opened/trade.update/trade.rejected/trade.closed referencing it)
# within this window becomes EXECUTION_UNKNOWN and is reconciled against
# broker state before a single permitted resend. Never blind-retry.
ORDER_TIMEOUT_S = 60

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

# Worker C (2026-09-23) risk-engine upgrade constants.
# Hard per-trade planned-risk ceiling (USD). The owner-set risk config is
# risk_per_trade_pct=0.05 on a $200,000 capital basis = $100/trade; this
# constant makes that ceiling explicit and fail-closed in check_gates when
# the mode checks are enforced (the LIVE executor path always enables
# them). Identical for DEMO and LIVE.
MAX_RISK_PER_TRADE_USD = 100.0
# Default per-mode emergency drawdown (fraction of the mode's
# start-of-day equity) that trips the kill switch. Config-overridable via
# emergency_drawdown_pct; the owner-approved risk numbers are unchanged.
EMERGENCY_DRAWDOWN_PCT = 3.0

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
        # Worker B (2026-09-23): durable idempotency store
        # (idempotency.py); atomic tmp+rename writes.
        "idempotency": os.path.join(sd, "idempotency.json"),
        # Worker C (2026-09-23): raised by experiment.py on
        # DEMO_EXPERIMENT_COMPLETE; while present the engine refuses ALL
        # new entries. Cleared only by explicit operator action
        # (`experiment.py --resume`). Never auto-switches to LIVE.
        "experiment_halt": os.path.join(sd, "experiment_halt"),
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

def _finite_pos_number(x):
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) and x > 0)


def validate_stop_loss(sig, spec, price_reference=None):
    """Validate the signal's stop-loss as a broker-acceptable order SL.

    Returns (ok, reason). reason is "" on success, else one of:
      missing_sl          no stop_loss on the signal
      bad_sl_numbers      entry/SL not finite positive numbers
      wrong_sided_sl      BUY needs SL < entry, SELL needs SL > entry
      sl_inside_stop_level  SL distance < broker stops_level * point
      sl_inside_freeze_level  SL within freeze_level of the price reference
    Pure function: no I/O.
    """
    sl = sig.get("stop_loss")
    if not sl:
        # None, 0, "" -- no usable SL on the signal. (Kept as the
        # historical "missing" reason so callers/tests can distinguish
        # "no SL given" from "SL given but broker-invalid".)
        return False, "missing_sl"
    entry = sig.get("entry_price")
    direction = (sig.get("direction") or "").upper()
    if not (_finite_pos_number(entry) and _finite_pos_number(sl)):
        return False, "bad_sl_numbers"
    if direction == "BUY" and not sl < entry:
        return False, "wrong_sided_sl"
    if direction == "SELL" and not sl > entry:
        return False, "wrong_sided_sl"
    if direction not in ("BUY", "SELL"):
        return False, "wrong_sided_sl"
    spec = spec or {}
    try:
        point = float(spec.get("point") or 0)
    except (TypeError, ValueError):
        point = 0.0
    try:
        stops_level = float(spec.get("stops_level_points") or 0)
    except (TypeError, ValueError):
        stops_level = 0.0
    if point > 0 and stops_level > 0:
        if abs(entry - sl) / point < stops_level:
            return False, "sl_inside_stop_level"
    try:
        freeze_level = float(spec.get("freeze_level_points") or 0)
    except (TypeError, ValueError):
        freeze_level = 0.0
    ref = price_reference if _finite_pos_number(price_reference) else entry
    if point > 0 and freeze_level > 0 and _finite_pos_number(ref):
        if abs(ref - sl) / point < freeze_level:
            return False, "sl_inside_freeze_level"
    return True, ""


def _mrs_get(state, key, default=None):
    """Read a key from a ModeRiskState or a plain dict (tests use dicts)."""
    if state is None:
        return default
    if hasattr(state, key):
        return getattr(state, key)
    if isinstance(state, dict):
        return state.get(key, default)
    return default


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
                enforce_consecutive_losses_gate=True,
                # Worker C (2026-09-23) mode/integrity checklist. All new
                # checks are opt-in via enforce_mode_checks (default False
                # preserves legacy behavior for direct callers); the LIVE
                # executor path always enables them.
                mode=None, enforce_mode_checks=False, mode_identity_ok=None,
                live_authorized=False, mode_risk_state=None,
                free_margin=None, margin_level=None,
                max_risk_per_trade_usd=MAX_RISK_PER_TRADE_USD,
                emergency_drawdown_pct=EMERGENCY_DRAWDOWN_PCT,
                price_reference=None):
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
    Mode/integrity checklist (enforce_mode_checks=True), in order:
      M0 mode valid ("DEMO"/"LIVE"), account identity matches the mode
         record, LIVE authorization present when mode == LIVE.
      M1 equity/balance/free-margin/margin-level present and sane.
      M2 spread present in broker data.
      M3 mandatory valid broker-side SL -- ALWAYS enforced, even when
         enforce_mode_checks is False (see the no-SL audit note).
      M4 per-trade planned risk <= max_risk_per_trade_usd ($100).
      M5 per-mode daily loss < 3% of the mode's start-of-day equity;
         blocked entries journal risk.daily_loss_block via the
         trip_mode_daily flag (handled by the engine, which owns I/O).
      M6 per-mode emergency drawdown trips the kill switch
         (trip + trip_mode_kill flags).
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

    # The symbol spec is needed by the integrity checklist below.
    spec = (specs.get("symbols") or {}).get(symbol) if specs else None

    # ---- M. pre-entry integrity checklist (Worker C, 2026-09-23) ----
    # M0/M1/M2 run only when enforce_mode_checks is True. The LIVE
    # executor path always enables them; direct callers (unit tests)
    # default to the legacy behavior. M3 (mandatory valid broker-side SL)
    # ALWAYS runs: the no-SL audit found the old `require_stop_loss`
    # config flag could disable the SL gate entirely, so the flag is kept
    # for backward compatibility but can no longer disable the gate.
    if enforce_mode_checks:
        # M0. mode valid, account identity matches the mode record, LIVE
        # authorization present when mode == LIVE.
        if mode not in ("DEMO", "LIVE"):
            return "skipped:invalid_mode", {"loud": True}
        if mode_identity_ok is not True:
            return "skipped:mode_identity_mismatch", {"loud": True}
        if mode == "LIVE" and not live_authorized:
            return "skipped:live_not_authorized", {"loud": True}
        # M1. equity / balance / free margin / margin level present and
        # sane. Fail closed: a trade decision must never rest on missing
        # account data.
        acct = (specs.get("account") or {}) if specs else {}
        fm = acct.get("free_margin") if free_margin is None else free_margin
        ml = (acct.get("margin_level") if margin_level is None
              else margin_level)
        if not (_finite_pos_number(equity) and _finite_pos_number(balance)
                and _finite_pos_number(fm) and _finite_pos_number(ml)):
            return "skipped:margin_data_unavailable", {"loud": True}
        # M2. spread must come from live broker data (fail closed).
        if spec is None or spec.get("spread_points") is None:
            return "skipped:spread_unknown", {"loud": True}

    # M3. mandatory valid broker-side SL -- always enforced (see note).
    # A signal without SL/TP is never traded; a mis-sided SL, an SL
    # inside the broker's stop level, or an SL inside the freeze level is
    # rejected as invalid. Nothing reaching the command writer may lack a
    # valid SL (belt-and-braces re-check in handle_signal).
    if not sig.get("take_profit"):
        return "skipped:missing_sl_tp", {"loud": True}
    sl_ok, sl_reason = validate_stop_loss(
        sig, spec, price_reference=price_reference)
    if not sl_ok:
        if sl_reason == "missing_sl":
            return "skipped:missing_sl_tp", {"loud": True}
        return "skipped:invalid_sl", {"loud": True, "sl_reason": sl_reason}

    # sizing needs specs; in dry_run we still want full computed details,
    # so stale/missing specs and sizing failures journal their skip reason.
    if not specs_fresh or spec is None:
        return "skipped:stale_specs", {}
    volume, risk_amount, err = compute_volume(sizing_equity, risk_pct, entry, sl, spec)
    if err:
        return err, {}

    if enforce_mode_checks:
        # M4. per-trade planned risk hard cap (owner risk numbers
        # unchanged: $100/trade on the $200,000 capital basis).
        try:
            cap = float(max_risk_per_trade_usd)
        except (TypeError, ValueError):
            cap = MAX_RISK_PER_TRADE_USD
        if risk_amount > cap + 1e-9:
            return "skipped:max_risk_per_trade", {"loud": True}
        # M5. per-mode daily loss < 3% of the MODE's start-of-day equity.
        # Blocked entries are journaled as risk.daily_loss_block by the
        # engine (trip_mode_daily flag); the mode state owns the flag so
        # it survives restarts and never leaks between DEMO and LIVE.
        sod = _mrs_get(mode_risk_state, "start_of_day_equity")
        if not _finite_pos_number(sod):
            return "skipped:mode_state_unavailable", {"loud": True}
        if _mrs_get(mode_risk_state, "daily_loss_tripped", False):
            return ("skipped:mode_daily_loss",
                    {"loud": True, "trip_mode_daily": True})
        realized = _mrs_get(mode_risk_state, "realized_pl", 0.0) or 0.0
        floating = _mrs_get(mode_risk_state, "floating_pl", 0.0) or 0.0
        if -(realized + floating) >= 0.03 * sod:
            return ("skipped:mode_daily_loss",
                    {"loud": True, "trip_mode_daily": True})
        # M6. per-mode emergency drawdown trips the kill switch. The
        # engine applies the trip (file kill switch + mode-state flag +
        # kill_switch.tripped journal) from these flags.
        try:
            edd = float(emergency_drawdown_pct)
        except (TypeError, ValueError):
            edd = EMERGENCY_DRAWDOWN_PCT
        dd = _mrs_get(mode_risk_state, "current_drawdown", 0.0) or 0.0
        if (_mrs_get(mode_risk_state, "kill_switch_tripped", False)
                or dd >= edd / 100.0):
            return ("skipped:emergency_drawdown",
                    {"loud": True, "trip": True, "trip_mode_kill": True})

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

    # 4. server check (mode-aware). DEMO/legacy: refuse anything that is
    # not a demo server. LIVE: refuse demo servers outright -- a live
    # mode record trades on its bound live broker, never on demo
    # (NO_LIVE_TO_DEMO_FALLBACK); the M0 identity gate additionally binds
    # the exact server string to the mode record.
    server = ((specs.get("account") or {}).get("server")
              or specs.get("server") or "")
    if enforce_mode_checks and mode == "LIVE":
        if is_demo_server(server):
            return "skipped:live_on_demo_server", {"loud": True}
    elif not is_demo_server(server):
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


def build_command(sig, volume, filling_mode=None):
    """Command dict for the NovaTrader EA (live mode only).

    filling_mode (Worker B, 2026-09-23): the broker-required filling mode
    for the symbol (e.g. "IOC"/"FOK"/"RETURN") as published in the symbol
    specs. Included ONLY when the specs carry it; absent -> the key is
    omitted and the EA keeps its existing auto-selection (backward
    compatible: the default call returns exactly the legacy key set).
    """
    cmd = {
        "type": "trade.open",
        "id": f"cmd-{sig.get('id')}",
        "symbol": sig.get("symbol"),
        "direction": sig.get("direction"),
        "volume": volume,
        "sl": sig.get("stop_loss"),
        "tp": sig.get("take_profit"),
        "signal_id": sig.get("id"),
    }
    if filling_mode is not None:
        cmd["filling_mode"] = filling_mode
    return cmd


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
        # Worker C (2026-09-23): cached per-mode risk state (risk_state.py).
        # One instance per mode, reloaded from disk across restarts; DEMO
        # and LIVE ledgers never mix.
        self._mode_state = None
        self._mode_name = None
        # Worker B (2026-09-23): execution-integrity state.
        # _mode_record is pinned by startup_validate() (trading_mode
        # ModeRecord); the per-order identity lock engages only when it
        # is pinned. disconnect_halt blocks new entries while broker
        # data is stale (set/cleared by check_disconnect()).
        self._mode_record = None
        self.disconnect_halt = False
        self._identity_legacy_warned = False
        # Durable idempotency store (idempotency.py). Claims survive
        # restarts; stale claims flipped to "unknown" at load are
        # journaled once here so the sweeper reconciles them.
        self.idem = IdempotencyStore(self.paths["idempotency"])
        for _key, _rec in self.idem.drain_stale_notes():
            self.journal({
                "type": "execution.reconcile_required",
                "command_id": _rec.get("command_id"),
                "cmd_type": _rec.get("cmd_type"),
                "idempotency_key": _key,
                "signal_id": _rec.get("signal_id"),
                "symbol": _rec.get("symbol"),
                "status": "unknown",
                "reason": ("stale claim at (re)start: no broker evidence "
                           "within 10 min; reconciling before any resend"),
            })
            log(f"RECONCILE REQUIRED: {_rec.get('command_id')} "
                f"({_rec.get('cmd_type')}) marked unknown at startup")
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

    # -- mode / risk-state helpers (Worker C, 2026-09-23) -----------------
    def _broker_account_login(self):
        """Account login from the fresh broker positions feed (or None).

        The EA's specs account block does not publish the login; the
        positions feed carries it as "account".
        """
        try:
            path = os.path.join(self.paths["files_dir"],
                                "nova_positions.json")
            if (time.time() - os.path.getmtime(path)
                    > POSITIONS_FEED_MAX_AGE_S):
                return None
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            login = data.get("account")
            return int(login) if login is not None else None
        except (OSError, ValueError, TypeError):
            return None

    def _mode_info(self, specs):
        """Return (mode, identity_ok, live_authorized, enforce_mode_checks).

        Uses the real trading_mode interface (Worker A): load_mode() ->
        ModeRecord, verify_identity(record, login, server, acct_type),
        is_live_authorized(). The account block comes from the fresh
        specs; login falls back to the broker positions feed because the
        EA does not publish it in the specs account block.
        The LIVE executor path always enforces the mode/integrity
        checklist. DEMO keeps legacy behavior (the new gates stay opt-in
        via check_gates kwargs): the EA's specs feed does not yet publish
        free_margin/margin_level, so fail-closed M1 would halt the demo
        experiment on a data gap rather than a risk breach. Once the EA
        publishes those fields, flip `enforce` to cover DEMO as well.
        A present-but-broken trading_mode fails closed.
        """
        if not _TRADING_MODE_AVAILABLE:
            return "DEMO", True, False, False
        try:
            rec = _trading_mode.load_mode()
        except Exception as e:
            log(f"mode check failed closed: load_mode error: {e}")
            return "DEMO", False, False, True
        mode = getattr(rec, "mode", "DEMO") or "DEMO"
        acct = (specs.get("account") or {}) if specs else {}
        server = acct.get("server")
        login = acct.get("login")
        if login is None:
            login = self._broker_account_login()
        acct_type = "DEMO" if is_demo_server(server or "") else "LIVE"
        try:
            identity_ok = bool(_trading_mode.verify_identity(
                rec, login, server, acct_type))
        except Exception as e:
            log(f"mode identity mismatch (fail closed): {e}")
            identity_ok = False
        live_authorized = False
        if mode == "LIVE":
            try:
                live_authorized = bool(_trading_mode.is_live_authorized())
            except Exception as e:
                log(f"live authorization check failed closed: {e}")
        enforce = (mode == "LIVE")
        return mode, identity_ok, live_authorized, enforce

    def _get_mode_state(self, mode, today_str, equity):
        """Cached ModeRiskState for *mode*; rolls the day when needed."""
        try:
            from risk_state import ModeRiskState
        except ImportError as e:
            log(f"warning: risk_state unavailable: {e}")
            return None
        if self._mode_state is None or self._mode_name != mode:
            try:
                self._mode_state = ModeRiskState.load(
                    self.paths["state_dir"], mode)
            except ValueError as e:
                # Mode mismatch on disk: never mix DEMO/LIVE ledgers.
                log(f"!!! risk-state mode mismatch, failing closed: {e}")
                return None
            except OSError as e:
                log(f"warning: risk-state load failed: {e}")
                return None
            self._mode_name = mode
        try:
            if self._mode_state.roll_day_if_needed(today_str, equity):
                self._mode_state.save(self.paths["state_dir"])
        except OSError as e:
            log(f"warning: risk-state save failed: {e}")
        return self._mode_state

    def _ai_advise(self, sig, specs, decision, cfg):
        """Advisory AI call: journals {"type": "ai.decision"}.

        ADDITIVE ONLY: never raises, never forces a trade. Returns the
        advisory dict (or None). A veto is applied by the caller only when
        config ai_veto_enabled is true (default false = zero behavior
        change for the demo experiment).
        """
        try:
            import ai_engine
        except ImportError:
            return None
        try:
            spec = ((specs.get("symbols") or {}).get(sig.get("symbol"))
                    if specs else None)
            ctx = dict(server_market_state(specs)[1] or {})
            advisory = ai_engine.advise(sig, {
                "spread_points": (spec or {}).get("spread_points"),
                "session": ctx.get("session"),
            })
            dec = advisory["decision"]
            self.journal({
                "type": "ai.decision",
                "signal_id": sig.get("id"),
                "symbol": sig.get("symbol"),
                "direction": sig.get("direction"),
                "gate_decision": decision,
                "ai_decision": dec.decision,
                "ai_direction": dec.direction,
                "confidence": dec.confidence,
                "valid": advisory["valid"],
                "validation_errors": advisory["validation_errors"],
                "ai_version": advisory["ai_version"],
                "ml_status": advisory["ml_status"],
                "mode": self._mode_name or "DEMO",
            })
            return advisory
        except Exception as e:
            try:
                self.journal({"type": "ai.error",
                              "signal_id": sig.get("id"),
                              "error": str(e)})
            except Exception:
                pass
            log(f"warning: ai advisory failed (non-blocking): {e}")
            return None

    # -- journal / state -------------------------------------------------
    def journal(self, entry):
        entry = dict(entry)
        entry.setdefault("time", now_str())
        with open(self.paths["journal"], "a") as f:
            f.write(json.dumps(entry) + "\n")

    def save(self):
        save_state(self.paths["state"], self.state)

    # -- config / inputs -------------------------------------------------
    def load_specs(self, max_age_s=SPECS_MAX_AGE_S):
        """Return (specs_dict_or_None, fresh_bool).

        Freshness is judged on the specs' own "time" field against
        max_age_s (default SPECS_MAX_AGE_S). Callers that need a stricter
        bar (startup validation, identity lock) pass a smaller window.
        """
        try:
            with open(self.paths["specs"]) as f:
                specs = json.load(f)
        except (OSError, ValueError):
            return None, False
        dt = parse_mt5_time(specs.get("time"))
        fresh = bool(dt and (datetime.now() - dt).total_seconds() <= max_age_s)
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

    # ------------------------------------------------------------------
    # Worker B (2026-09-23): execution integrity -- startup validation,
    # account identity lock, filling modes, disconnect behavior, order
    # timeout sweep. All broker-bound commands flow through
    # _emit_command() so the identity lock + idempotency claim apply to
    # open, modify, and close alike.
    # ------------------------------------------------------------------

    # -- startup validation ---------------------------------------------
    def startup_validate(self):
        """Pin the broker identity before the daemon does anything.

        trading_mode.load_mode() -> fresh account block from
        nova_symbol_specs.json (refuses data older than
        SPECS_STARTUP_MAX_AGE_S) -> trading_mode.verify_identity().

        On ANY failure: journals {"type": "mode.unavailable", ...} and
        raises SystemExit(non-zero) -- the process stops. It never
        continues against an unverified account, never fakes a
        connection, never synthesizes prices.

        On success: logs "STARTUP MODE OK: mode=.. login=.. server=..
        type=..", pins self._mode_record, returns (record, account).
        """
        def fail(reason, status=None):
            st = status or "DEMO_UNAVAILABLE"
            try:
                self.journal({"type": "mode.unavailable", "status": st,
                              "reason": reason})
            except OSError:
                pass
            log(f"!!! STARTUP REFUSED [{st}]: {reason} -- process stopping")
            raise SystemExit(2)

        if not TRADING_MODE_AVAILABLE:
            fail("trading_mode module unavailable "
                 f"({TRADING_MODE_IMPORT_ERROR}); broker identity cannot "
                 "be verified", "DEMO_UNAVAILABLE")
        try:
            record = _trading_mode.load_mode()
        except Exception as e:
            fail(f"load_mode() failed: {e}",
                 getattr(e, "status", "DEMO_UNAVAILABLE"))
        mode = getattr(record, "mode", None)
        if record is None or mode not in _trading_mode.MODES:
            fail(f"invalid mode record: {record!r}", "DEMO_UNAVAILABLE")
        status = f"{str(mode).upper()}_UNAVAILABLE"
        specs, fresh = self.load_specs(max_age_s=SPECS_STARTUP_MAX_AGE_S)
        if specs is None:
            fail("nova_symbol_specs.json missing or unparsable; no broker "
                 "account block to verify against", status)
        if not fresh:
            fail(f"broker specs older than {SPECS_STARTUP_MAX_AGE_S}s; "
                 "refusing to pin identity to stale data", status)
        account = specs.get("account") or {}
        login = account.get("login")
        server = account.get("server")
        acct_type = account.get("type")
        if login is None or acct_type is None:
            fail(f"account block missing identity fields "
                 f"(login={login!r}, server={server!r}, type={acct_type!r}); "
                 "the EA must publish login and type in the account block",
                 status)
        if str(mode).lower() == "demo" and str(acct_type).upper() != "DEMO":
            fail(f"DEMO mode requires account type DEMO, broker reports "
                 f"{acct_type!r}", status)
        try:
            _trading_mode.verify_identity(record, login, server, acct_type)
        except Exception as e:
            fail(f"account identity mismatch: {e}", status)
        self._mode_record = record
        log(f"STARTUP MODE OK: mode={mode} login={login} server={server} "
            f"type={acct_type}")
        return record, account

    def _pinned_mode_str(self):
        """Mode string from the pinned record; "DEMO" when unpinned."""
        rec = getattr(self, "_mode_record", None)
        return str(getattr(rec, "mode", "DEMO") or "DEMO")

    def _unavailable_status(self):
        return f"{self._pinned_mode_str().upper()}_UNAVAILABLE"

    # -- account identity lock -------------------------------------------
    def read_account_block(self, max_age_s=IDENTITY_FRESH_S):
        """Freshest account block: (account_dict_or_None, fresh, reason)."""
        specs, fresh = self.load_specs(max_age_s=max_age_s)
        if specs is None:
            return None, False, "specs unavailable"
        if not fresh:
            return None, False, \
                f"specs older than {max_age_s}s"
        account = specs.get("account") or {}
        if not account:
            return None, False, "account block missing"
        return account, True, ""

    def assert_identity(self):
        """Re-read the freshest account block and verify it against the
        mode record pinned at startup. Returns (ok, reason); never
        raises. Called before EVERY command.sent (open/modify/close)."""
        record = getattr(self, "_mode_record", None)
        if not TRADING_MODE_AVAILABLE or record is None:
            return False, ("no validated mode record (startup validation "
                           "did not pin an identity)")
        account, fresh, reason = self.read_account_block()
        if not fresh:
            return False, f"account block not verifiable: {reason}"
        login = account.get("login")
        server = account.get("server")
        acct_type = account.get("type")
        if login is None or acct_type is None:
            return False, ("account block missing login/type; cannot "
                           "verify identity")
        try:
            _trading_mode.verify_identity(record, login, server, acct_type)
        except Exception as e:
            return False, f"identity mismatch: {e}"
        return True, ""

    # -- filling modes ----------------------------------------------------
    # Spec conventions (forward-compatible; absent fields -> legacy):
    #   spec["filling_mode"]            broker-required mode, e.g.
    #                                   "IOC"/"FOK"/"RETURN"; carried in
    #                                   the command payload when present.
    #   spec["filling_modes_supported"] explicit allow-list; a required
    #                                   mode outside it rejects the order.
    #   spec["filling_mode_unsupported"] True -> the broker reports the
    #                                   mode unsupported: reject, never
    #                                   pretend the order was sent.
    def check_filling_mode(self, sig, specs):
        """Return (ok, filling_mode_or_None, reason).

        ok=False means the broker explicitly reports the filling mode
        unsupported for this symbol: the caller must reject the order
        with reason 'filling_mode_unsupported' and send nothing.
        """
        spec = ((specs.get("symbols") or {}).get(sig.get("symbol"))
                if specs else None) or {}
        if spec.get("filling_mode_unsupported") is True:
            return (False, None,
                    "broker reports filling_mode_unsupported for "
                    f"{sig.get('symbol')}")
        mode = spec.get("filling_mode")
        supported = spec.get("filling_modes_supported")
        if isinstance(supported, list) and supported:
            effective = str(mode or "RETURN").upper()
            allowed = {str(s).upper() for s in supported}
            if effective not in allowed:
                return (False, None,
                        f"filling mode {effective} not in broker-supported "
                        f"{supported} for {sig.get('symbol')}")
        return True, mode, ""

    # -- command emission funnel ------------------------------------------
    def _idem_identity(self):
        """(mode, login) key material for idempotency keys.

        From the pinned mode record when present; deterministic
        ("demo"/"unknown") default otherwise so legacy/test paths still
        get stable keys.
        """
        record = getattr(self, "_mode_record", None)
        if record is not None:
            return (str(getattr(record, "mode", "DEMO") or "DEMO"),
                    str(getattr(record, "account_login", "unknown")
                        or "unknown"))
        return "DEMO", "unknown"

    def _emit_command(self, cmd, *, cmd_type, idem_key, idem_extra=None,
                      index_info=None):
        """Single funnel for EVERY broker-bound command (open/modify/close).

        1. Account identity lock (engages only when a mode record was
           pinned at startup; without one there is nothing to lock
           against and the legacy path applies with a one-time warning).
           On mismatch: journals account.identity_mismatch and aborts
           THIS operation only -- the daemon keeps running and never
           connects anywhere else.
        2. Durable idempotency claim BEFORE command.sent. On "duplicate":
           journals execution.duplicate_suppressed and skips.
        3. Stamps sent_at (UTC), appends to nova_commands.jsonl, journals
           command.sent.

        Returns (status, cmd_or_None); status is "sent", "duplicate", or
        "aborted:identity_mismatch".
        """
        if getattr(self, "_mode_record", None) is not None:
            ok, reason = self.assert_identity()
            if not ok:
                self.journal({
                    "type": "account.identity_mismatch",
                    "command_id": cmd.get("id"),
                    "cmd_type": cmd_type,
                    "symbol": cmd.get("symbol"),
                    "reason": reason,
                })
                log(f"!!! IDENTITY MISMATCH: {cmd.get('id')} ({cmd_type}) "
                    f"aborted: {reason} [daemon continues, no reconnect]")
                return "aborted:identity_mismatch", None
        elif not self._identity_legacy_warned:
            self._identity_legacy_warned = True
            log("warning: no mode record pinned (startup validation not "
                "run); per-order identity lock dormant")
        record = {"status": "proposed",
                  "command_id": cmd.get("id"),
                  "cmd_type": cmd_type,
                  "idempotency_key": cmd.get("idempotency_key"),
                  "generation": 0}
        record.update(idem_extra or {})
        if self.idem.check_or_claim(idem_key, record) == "duplicate":
            self.journal({
                "type": "execution.duplicate_suppressed",
                "command_id": cmd.get("id"),
                "cmd_type": cmd_type,
                "idempotency_key": idem_key,
                "signal_id": cmd.get("signal_id"),
                "symbol": cmd.get("symbol"),
            })
            log(f"DUPLICATE suppressed: {cmd.get('id')} ({cmd_type}); "
                f"idempotency key already claimed -- not re-sent")
            return "duplicate", None
        cmd["sent_at"] = utcnow_iso()
        with open(self.paths["commands"], "a") as f:
            f.write(json.dumps(cmd) + "\n")
        self.idem.update(idem_key, status="sent", sent_at=cmd["sent_at"])
        if index_info:
            cmd_index = self.state.get("cmd_index", {})
            cmd_index[cmd["id"]] = dict(index_info)
            self.state["cmd_index"] = dict(list(cmd_index.items())[-500:])
        self.journal({"type": "command.sent", "command": cmd})
        if cmd_type == "trade.open":
            log(f"COMMANDED {cmd['id']}: {cmd['direction']} {cmd['volume']} "
                f"{cmd['symbol']} sl={cmd['sl']} tp={cmd['tp']}")
        elif cmd_type == "trade.close":
            log(f"CLOSE-COMMANDED {cmd['id']}: "
                f"position {cmd.get('position_id')}")
        else:
            log(f"COMMANDED {cmd['id']}: {cmd_type}")
        return "sent", cmd

    def send_open_command(self, cmd, sig, flags):
        """Emit a prebuilt trade.open command via the funnel.

        cmd is the build_command() dict (may carry filling_mode);
        idempotency key = sha256(signal_id|mode|login|symbol|direction|
        volume|sl|tp). Returns (status, cmd_or_None)."""
        cmd["idempotency_key"] = uuid.uuid4().hex
        mode, login = self._idem_identity()
        try:
            vol_repr = repr(float(flags["volume"]))
        except (TypeError, ValueError):
            vol_repr = str(flags.get("volume"))
        key = make_idempotency_key(
            sig.get("id"), mode, login, sig.get("symbol"),
            sig.get("direction"), vol_repr, sig.get("stop_loss"),
            sig.get("take_profit"))
        extra = {"mode": mode, "account": login,
                 "symbol": sig.get("symbol"),
                 "direction": sig.get("direction"),
                 "volume": flags["volume"],
                 "sl": sig.get("stop_loss"), "tp": sig.get("take_profit"),
                 "signal_id": sig.get("id"),
                 "filling_mode": cmd.get("filling_mode")}
        index_info = {"signal_id": sig.get("id"),
                      "symbol": sig.get("symbol"),
                      "direction": sig.get("direction"),
                      "volume": flags["volume"],
                      "entry_price": sig.get("entry_price")}
        return self._emit_command(cmd, cmd_type="trade.open", idem_key=key,
                                  idem_extra=extra, index_info=index_info)

    def send_close_command(self, ticket, *, symbol=None, reason="manual"):
        """Emit a trade.close command via the funnel. No strategy call
        site yet -- available for risk-management use. The NovaTrader EA
        handles {"type":"trade.close","id":..,"position_id":..}."""
        cmd = {"type": "trade.close",
               "id": f"cmd-close-{ticket}-{uuid.uuid4().hex[:8]}",
               "position_id": ticket,
               "idempotency_key": uuid.uuid4().hex,
               "reason": reason}
        if symbol:
            cmd["symbol"] = symbol
        mode, login = self._idem_identity()
        key = make_idempotency_key("close", mode, login, ticket)
        extra = {"mode": mode, "account": login, "symbol": symbol,
                 "ticket": ticket, "reason": reason}
        return self._emit_command(cmd, cmd_type="trade.close",
                                  idem_key=key, idem_extra=extra,
                                  index_info={"ticket": ticket,
                                              "symbol": symbol})

    def send_modify_command(self, ticket, *, new_sl=None, new_tp=None,
                            symbol=None):
        """Emit a trade.modify command via the funnel. Reserved: the
        current NovaTrader EA does not implement trade.modify (it ignores
        unknown command types), so this has no call site until the EA
        supports it."""
        cmd = {"type": "trade.modify",
               "id": f"cmd-modify-{ticket}-{uuid.uuid4().hex[:8]}",
               "position_id": ticket,
               "idempotency_key": uuid.uuid4().hex}
        if new_sl is not None:
            cmd["sl"] = new_sl
        if new_tp is not None:
            cmd["tp"] = new_tp
        if symbol:
            cmd["symbol"] = symbol
        mode, login = self._idem_identity()
        key = make_idempotency_key("modify", mode, login, ticket,
                                   new_sl, new_tp)
        extra = {"mode": mode, "account": login, "symbol": symbol,
                 "ticket": ticket, "sl": new_sl, "tp": new_tp}
        return self._emit_command(cmd, cmd_type="trade.modify",
                                  idem_key=key, idem_extra=extra,
                                  index_info={"ticket": ticket,
                                              "symbol": symbol})

    # -- disconnect behavior -----------------------------------------------
    def broker_data_age_s(self):
        """Age of broker data in seconds: max(specs file mtime age, feed
        server-time age). inf when the specs file is missing entirely."""
        ages = []
        try:
            ages.append(time.time() - os.path.getmtime(self.paths["specs"]))
        except OSError:
            return float("inf")
        feed = load_feed(self.paths["files_dir"])
        if feed:
            st = parse_mt5_time(feed.get("server_time") or feed.get("time"))
            if st is not None:
                ages.append((datetime.now() - st).total_seconds())
        return max(ages) if ages else float("inf")

    def check_disconnect(self):
        """Halt new entries when broker data goes stale; resume on fresh
        data. Journals the transitions; never touches any other account
        or server -- the executor only ever polls the pinned account.

        Returns True while halted.
        """
        age = self.broker_data_age_s()
        if age > DISCONNECT_STALE_S:
            if not self.disconnect_halt:
                self.disconnect_halt = True
                self.journal({
                    "type": "mode.unavailable",
                    "status": self._unavailable_status(),
                    "mode": self._pinned_mode_str(),
                    "reason": (f"broker data stale: age {age:.0f}s > "
                               f"{DISCONNECT_STALE_S}s"),
                    "data_age_s": round(age, 1),
                })
                log(f"!!! DISCONNECT [{self._unavailable_status()}]: "
                    f"broker data stale ({age:.0f}s); ALL new entries "
                    f"halted -- polling the same account only, no "
                    f"switching, risk management continues")
            return True
        if self.disconnect_halt and age <= DISCONNECT_RECOVER_S:
            self.disconnect_halt = False
            self.journal({
                "type": "mode.recovered",
                "status": f"{self._pinned_mode_str().upper()}_AVAILABLE",
                "mode": self._pinned_mode_str(),
                "data_age_s": round(age, 1),
            })
            log(f"RECOVERED: broker data fresh ({age:.0f}s); new entries "
                f"resumed on the same account")
        return self.disconnect_halt

    # -- order timeout -> EXECUTION_UNKNOWN -> reconcile --------------------
    def _read_recent_trades(self, max_bytes=65536):
        """Parse the tail of nova_trades.jsonl into event dicts. The EA
        appends trade.opened synchronously after OrderSend, so absence of
        our command_id here (past the timeout) is broker-authoritative
        proof the order never executed."""
        events = []
        try:
            size = os.path.getsize(self.paths["trades"])
        except OSError:
            return events
        try:
            with open(self.paths["trades"], "r", encoding="utf-8",
                      errors="replace") as f:
                f.seek(max(0, size - max_bytes))
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return events

    def _read_broker_positions(self):
        """(positions, fresh) from the EA's nova_positions.json feed."""
        return load_broker_positions(self.paths["files_dir"])

    def _evidence_for_open(self, rec, positions, trades):
        """Broker evidence that a trade.open DID execute. Returns an
        evidence dict or None. Exact command_id match on trade.opened
        wins; a fresh position matching symbol/direction/volume opened
        after send is corroborating (journaled as heuristic)."""
        cmd_id = rec.get("command_id")
        for ev in trades:
            if (ev.get("type") == "trade.opened"
                    and ev.get("command_id") == cmd_id):
                return {"source": "trades_file", "ticket": ev.get("ticket"),
                        "deal": ev.get("deal"), "command_id": cmd_id}
        sent_at = parse_iso(rec.get("sent_at"))
        for p in positions or []:
            if str(p.get("symbol") or "").upper() != \
                    str(rec.get("symbol") or "").upper():
                continue
            if str(p.get("type") or "").upper() != \
                    str(rec.get("direction") or "").upper():
                continue
            try:
                if abs(float(p.get("volume") or 0)
                       - float(rec.get("volume") or 0)) > 1e-9:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                opened = float(p.get("open_time") or 0)
            except (TypeError, ValueError):
                opened = 0
            if sent_at is not None and opened:
                sent_epoch = sent_at.timestamp()
                if opened < sent_epoch - 120:
                    continue
            return {"source": "positions_feed_heuristic",
                    "ticket": p.get("ticket"),
                    "note": "matched symbol/direction/volume opened after "
                            "send; adopted, not re-sent"}
        return None

    def _proves_no_execution(self, rec, positions, trades, positions_fresh):
        """True only when broker state proves the order did NOT execute:
        no trade.opened/trade.rejected/trade.closed carrying our
        command_id in the trades file, and (for trade.open) a FRESH
        positions feed showing no matching open position.

        Stale feeds -> inconclusive (no resend), never a blind retry:
        absence of evidence is not evidence of absence when the broker
        view itself is stale. (Worker B proof review, 2026-09-23.)
        """
        cmd_id = rec.get("command_id")
        for ev in trades:
            if ev.get("command_id") == cmd_id and ev.get("type") in (
                    "trade.opened", "trade.rejected", "trade.closed"):
                return False
        if rec.get("cmd_type") == "trade.open":
            if not positions_fresh:
                return False  # stale broker view: cannot prove absence
            if self._evidence_for_open(rec, positions, trades) is not None:
                return False
            return True
        if rec.get("cmd_type") == "trade.close":
            ticket = rec.get("ticket")
            for p in positions or []:
                if str(p.get("ticket")) == str(ticket):
                    # Position still open and the EA never reported the
                    # close: proves the close command did not execute
                    # (when the feed is fresh).
                    return bool(positions_fresh)
            return False  # absent + no trade.closed: inconclusive
        return True

    def _reconcile_entry(self, key, rec, positions_provider=None,
                         trades_provider=None):
        """Reconcile one EXECUTION_UNKNOWN entry against broker state.

        Evidence of execution -> adopt the ticket (never resend).
        Proof of NO execution -> resend ONCE (new idempotency key linked
        via supersedes_key), unless the disconnect halt or an already-
        used resend forbids it. Inconclusive -> leave unknown.
        """
        positions, positions_fresh = (
            positions_provider() if positions_provider
            else self._read_broker_positions())
        trades = (trades_provider() if trades_provider
                  else self._read_recent_trades())
        evidence = None
        if rec.get("cmd_type") == "trade.open":
            evidence = self._evidence_for_open(rec, positions, trades)
        elif rec.get("cmd_type") == "trade.close":
            ticket = rec.get("ticket")
            for ev in trades:
                if (ev.get("type") == "trade.closed"
                        and str(ev.get("ticket")) == str(ticket)):
                    evidence = {"source": "trades_file", "ticket": ticket}
                    break
        if evidence:
            self.idem.update(key, status="filled",
                             broker_ticket=evidence.get("ticket"),
                             broker_deal=evidence.get("deal"))
            self.journal({
                "type": "execution.adopted",
                "command_id": rec.get("command_id"),
                "cmd_type": rec.get("cmd_type"),
                "idempotency_key": key,
                "evidence": evidence,
                "note": "broker state showed the order DID execute; "
                        "adopted the ticket instead of resending",
            })
            log(f"RECONCILED (adopted): {rec.get('command_id')} -> "
                f"ticket {evidence.get('ticket')} via "
                f"{evidence.get('source')} [no resend]")
            return "adopted"
        if not self._proves_no_execution(rec, positions, trades,
                                         positions_fresh):
            self.journal({
                "type": "execution.reconcile_deferred",
                "command_id": rec.get("command_id"),
                "cmd_type": rec.get("cmd_type"),
                "idempotency_key": key,
                "reason": "broker state inconclusive; leaving unknown, "
                          "no resend",
            })
            log(f"RECONCILE DEFERRED: {rec.get('command_id')} broker state "
                f"inconclusive -- left unknown, NOT resent")
            return "deferred"
        if self.disconnect_halt:
            self.journal({
                "type": "execution.resend_deferred",
                "command_id": rec.get("command_id"),
                "cmd_type": rec.get("cmd_type"),
                "idempotency_key": key,
                "reason": "disconnect_halt: broker data stale; resend "
                          "deferred until recovery",
            })
            log(f"RESEND DEFERRED: {rec.get('command_id')} (disconnect "
                f"halt) -- will retry reconcile next cycle")
            return "deferred"
        generation = int(rec.get("generation") or 0)
        if generation >= 1:
            self.idem.update(key, status="failed",
                             note="single resend already used; abandoned")
            self.journal({
                "type": "execution.abandoned",
                "command_id": rec.get("command_id"),
                "cmd_type": rec.get("cmd_type"),
                "idempotency_key": key,
                "reason": "broker proved no execution but the single "
                          "permitted resend was already used; will not "
                          "retry again",
            })
            log(f"!!! ABANDONED: {rec.get('command_id')} no execution and "
                f"resend already used -- manual review required")
            return "abandoned"
        self._resend_command(key, rec)
        return "resent"

    def _resend_command(self, key, rec):
        """Resend ONCE with a new idempotency key linked via
        supersedes_key. Only called after broker state proved the
        original never executed."""
        new_key = make_idempotency_key(key, "resend", "1")
        new_cmd_id = f"{rec.get('command_id')}-r1"
        new_idem_key = uuid.uuid4().hex
        cmd_type = rec.get("cmd_type") or "trade.open"
        if cmd_type == "trade.open":
            cmd = {"type": "trade.open", "id": new_cmd_id,
                   "symbol": rec.get("symbol"),
                   "direction": rec.get("direction"),
                   "volume": rec.get("volume"), "sl": rec.get("sl"),
                   "tp": rec.get("tp"),
                   "signal_id": rec.get("signal_id"),
                   "idempotency_key": new_idem_key,
                   "resend_of": rec.get("command_id")}
            if rec.get("filling_mode") is not None:
                cmd["filling_mode"] = rec.get("filling_mode")
            index_info = {"signal_id": rec.get("signal_id"),
                          "symbol": rec.get("symbol"),
                          "direction": rec.get("direction"),
                          "volume": rec.get("volume")}
        elif cmd_type == "trade.close":
            cmd = {"type": "trade.close", "id": new_cmd_id,
                   "position_id": rec.get("ticket"),
                   "idempotency_key": new_idem_key,
                   "resend_of": rec.get("command_id")}
            index_info = {"ticket": rec.get("ticket")}
        else:
            log(f"RESEND REFUSED: {rec.get('command_id')} cmd_type "
                f"{cmd_type} has no resend builder")
            return
        extra = dict(rec)
        extra.update({"status": "proposed", "command_id": new_cmd_id,
                      "idempotency_key": new_idem_key,
                      "supersedes_key": key, "generation": 1,
                      "broker_ticket": None, "broker_deal": None,
                      "sent_at": None,
                      "note": f"single permitted resend of {key}"})
        status, _ = self._emit_command(
            cmd, cmd_type=cmd_type, idem_key=new_key, idem_extra=extra,
            index_info=index_info)
        if status == "sent":
            self.idem.update(key, status="superseded",
                             superseded_by=new_key,
                             note=f"resent once as {new_cmd_id}")
            self.journal({
                "type": "execution.resent",
                "old_command_id": rec.get("command_id"),
                "new_command_id": new_cmd_id,
                "cmd_type": cmd_type,
                "old_idempotency_key": key,
                "new_idempotency_key": new_key,
                "supersedes_key": key,
                "reason": "broker proved no execution within timeout; "
                          "single permitted resend",
            })
            log(f"RESENT (once): {rec.get('command_id')} -> {new_cmd_id} "
                f"[supersedes {key[:12]}...]")
        else:
            # The funnel refused (identity mismatch / duplicate): leave
            # the original unknown for the next reconcile cycle.
            self.journal({
                "type": "execution.resend_deferred",
                "command_id": rec.get("command_id"),
                "cmd_type": cmd_type,
                "idempotency_key": key,
                "reason": f"resend funnel returned {status}; original "
                          "left unknown",
            })

    def sweep_timeouts(self, positions_provider=None, trades_provider=None):
        """Mark sent commands with no broker response within
        ORDER_TIMEOUT_S as EXECUTION_UNKNOWN, then reconcile each
        unknown entry against broker state. Runs every main-loop cycle.
        Providers are injectable for tests; default to the live files."""
        now = datetime.now(timezone.utc)
        for key, rec in list(self.idem.pending().items()):
            status = rec.get("status")
            if status in ("sent", "proposed"):
                anchor = parse_iso(rec.get("sent_at")) or parse_iso(
                    rec.get("created_at"))
                age_s = ((now - anchor).total_seconds()
                         if anchor is not None else float("inf"))
                if age_s <= ORDER_TIMEOUT_S:
                    continue
                self.idem.update(key, status="unknown")
                self.journal({
                    "type": "execution.unknown",
                    "command_id": rec.get("command_id"),
                    "cmd_type": rec.get("cmd_type"),
                    "idempotency_key": key,
                    "signal_id": rec.get("signal_id"),
                    "symbol": rec.get("symbol"),
                    "status": "EXECUTION_UNKNOWN",
                    "reason": (f"no broker response within "
                               f"{ORDER_TIMEOUT_S}s of send; reconciling "
                               f"broker state before any retry"),
                    "sent_at": rec.get("sent_at"),
                })
                log(f"!!! EXECUTION_UNKNOWN: {rec.get('command_id')} "
                    f"({rec.get('cmd_type')}) no broker response in "
                    f"{ORDER_TIMEOUT_S}s -- reconciling before any retry")
                rec = self.idem.get(key) or rec
            if (self.idem.get(key) or {}).get("status") == "unknown":
                try:
                    self._reconcile_entry(key, self.idem.get(key),
                                          positions_provider,
                                          trades_provider)
                except Exception as e:
                    log(f"sweep error reconciling "
                        f"{rec.get('command_id')}: {e}")

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
                    # Worker B (2026-09-23): broker evidence for the
                    # idempotency store -- this command is filled.
                    _ck, _cr = self.idem.find_by_command_id(
                        ev.get("command_id"))
                    if _ck:
                        self.idem.update(
                            _ck, status="filled",
                            broker_ticket=ev.get("ticket"),
                            broker_deal=ev.get("deal"))
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
                    # Worker C (2026-09-23): fold the close into the
                    # per-mode risk ledger so the daily-loss / drawdown
                    # gates see realized P&L even across restarts.
                    try:
                        ms = (self._mode_state or self._get_mode_state(
                            self._mode_name or "DEMO", None, None))
                        if ms is not None:
                            ms.record_closed_trade(
                                rec.get("net_profit")
                                if rec.get("net_profit") is not None
                                else rec.get("profit"),
                                rec.get("commission", 0.0),
                                rec.get("swap", 0.0))
                            ms.save(self.paths["state_dir"])
                    except OSError as e:
                        log(f"warning: risk-state save failed: {e}")
                    # Worker B (2026-09-23): a broker-confirmed close
                    # resolves any pending trade.close claim on this
                    # ticket (the EA's trade.closed carries no
                    # command_id, so match by ticket).
                    _ck, _cr = self.idem.find_by_ticket(
                        ev.get("ticket"), cmd_type="trade.close")
                    if _ck:
                        self.idem.update(_ck, status="filled")
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
                    # Worker B (2026-09-23): the EA refused the command --
                    # mark the claim failed so the sweeper never resends
                    # a proven-rejected order.
                    _ck, _cr = self.idem.find_by_command_id(
                        ev.get("command_id"))
                    if _ck:
                        self.idem.update(_ck, status="failed",
                                         fail_reason=ev.get("reason"))
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
        # Worker C (2026-09-23): mode + per-mode persistent risk state.
        # Defensive: with trading_mode.py absent this yields
        # ("DEMO", True, False, False) i.e. legacy behavior.
        mode, identity_ok, live_auth, enforce_mode = self._mode_info(specs)
        mode_state = self._get_mode_state(mode, today_str, account_equity)
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
            enforce_spread_gate=bool(
                cfg.get("enforce_spread_gate", False)),
            enforce_single_position_per_symbol=bool(
                cfg.get("enforce_single_position_per_symbol", False)),
            enforce_consecutive_losses_gate=bool(
                cfg.get("enforce_consecutive_losses_gate", False)),
            mode=mode,
            enforce_mode_checks=enforce_mode,
            mode_identity_ok=identity_ok,
            live_authorized=live_auth,
            mode_risk_state=mode_state,
            emergency_drawdown_pct=cfg.get("emergency_drawdown_pct",
                                          EMERGENCY_DRAWDOWN_PCT),
            price_reference=sig.get("entry_price"),
        )
        return decision, flags

    def handle_signal(self, sig, cfg, kill_on, specs, specs_fresh,
                      open_positions, todays_profit, today_str,
                      market_open=True, context=None,
                      open_lots=0.0, consecutive_losses=0, tick_age_s=None):
        # Worker C (2026-09-23): post-experiment trade halt. Raised by
        # experiment.py on DEMO_EXPERIMENT_COMPLETE; while present NO new
        # entries are taken. Cleared only by explicit operator action
        # (`experiment.py --resume`). Never auto-switches to LIVE.
        if os.path.exists(self.paths.get("experiment_halt", "")):
            decision = "skipped:experiment_complete"
            self.journal({
                "type": "trade.decision",
                "signal_id": sig.get("id"),
                "symbol": sig.get("symbol"),
                "direction": sig.get("direction"),
                "decision": decision,
                "context": context or {},
            })
            log(f"SKIPPED {decision}: {sig.get('id')} (experiment halt)")
            return decision
        decision, flags = self.decide(
            sig, cfg, kill_on, specs, specs_fresh,
            open_positions, todays_profit, today_str, market_open,
            open_lots=open_lots, consecutive_losses=consecutive_losses,
            tick_age_s=tick_age_s,
        )
        # Worker C (2026-09-23): advisory AI call. Additive only -- it
        # journals ai.decision and can never force a trade. A veto applies
        # only when config ai_veto_enabled is true (default false).
        advisory = self._ai_advise(sig, specs, decision, cfg)
        if (decision == "commanded" and cfg.get("ai_veto_enabled")
                and advisory is not None
                and advisory.get("valid")
                and advisory["decision"].decision == "NO_TRADE"):
            log(f"AI VETO: {sig.get('id')} ({sig.get('symbol')} "
                f"{sig.get('direction')}) -- "
                f"{advisory['decision'].thesis}")
            decision = "skipped:ai_veto"
            flags = {"vetoed_by": "ai", "ai_thesis":
                     advisory["decision"].thesis}
        # Worker C (2026-09-23): per-mode side effects from the M5/M6
        # flags. check_gates is pure; the engine owns the I/O.
        mode_state = self._mode_state
        if flags.get("trip_mode_daily") and mode_state is not None:
            try:
                mode_state.note_daily_loss_tripped()
                mode_state.save(self.paths["state_dir"])
            except OSError as e:
                log(f"warning: risk-state save failed: {e}")
            self.journal({
                "type": "risk.daily_loss_block",
                "mode": self._mode_name,
                "signal_id": sig.get("id"),
                "symbol": sig.get("symbol"),
                "start_of_day_equity": getattr(
                    mode_state, "start_of_day_equity", None),
                "daily_loss_pct": mode_state.daily_loss_pct()
                if hasattr(mode_state, "daily_loss_pct") else None,
            })
        if flags.get("trip_mode_kill") and mode_state is not None:
            try:
                mode_state.note_kill_switch_tripped()
                mode_state.save(self.paths["state_dir"])
            except OSError as e:
                log(f"warning: risk-state save failed: {e}")
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
        # Worker B (2026-09-23): disconnect halt. While broker data is
        # stale, NO new entries (commanded or intended) are taken;
        # existing risk management (gates above, kill switch, trade
        # tailing) keeps running.
        if self.disconnect_halt and decision in ("commanded", "intended"):
            decision = "skipped:disconnect_halt"
            log(f"HALTED (disconnect): {sig.get('id')} "
                f"({sig.get('symbol')} {sig.get('direction')}) not entered "
                f"[no fresh broker data]")
        if decision == "commanded":
            # Worker B (2026-09-23): real filling modes. A spec-provided
            # filling_mode rides in the command; an explicitly
            # unsupported mode rejects the order -- never pretend it was
            # sent. Absent spec fields -> legacy behavior unchanged.
            fm_ok, filling_mode, fm_reason = self.check_filling_mode(
                sig, specs)
            if not fm_ok:
                decision = "skipped:filling_mode_unsupported"
                entry["filling_mode_reason"] = fm_reason
                log(f"!!! FILLING MODE UNSUPPORTED: {sig.get('id')} "
                    f"({sig.get('symbol')}): {fm_reason} [no order sent]")
            else:
                cmd = build_command(sig, flags["volume"],
                                    filling_mode=filling_mode)
                # No-SL audit (Worker C, 2026-09-23): belt-and-braces. The M3
                # gate already refuses SL-less signals, but no code path may
                # ever write a command without a valid SL -- if one ever gets
                # here, refuse loudly instead of writing.
                if not cmd.get("sl"):
                    log("!!! REFUSAL: commanded but SL missing at write time "
                        f"-- refusing to write command for {sig.get('id')}")
                    self.journal({"type": "trade.rejected",
                                  "reason": "no_sl_at_write",
                                  "signal_id": sig.get("id"),
                                  "symbol": sig.get("symbol")})
                    decision = "skipped:invalid_sl"
                else:
                    # Worker B (2026-09-23): every command goes through
                    # the identity-lock + idempotency funnel.
                    send_status, _sent = self.send_open_command(
                        cmd, sig, flags)
                    if send_status == "sent":
                        entry["command_id"] = cmd["id"]
                    elif send_status == "duplicate":
                        decision = "skipped:duplicate_suppressed"
                    else:  # aborted:identity_mismatch
                        decision = "skipped:identity_mismatch"
                        entry["abort_detail"] = send_status
        elif decision == "intended":
            log(f"INTENDED (dry run): {sig.get('direction')} "
                f"{flags.get('volume')} {sig.get('symbol')} "
                f"risk=${flags.get('risk_amount')} "
                f"sl={sig.get('stop_loss')} tp={sig.get('take_profit')} "
                f"[no command written]")
        else:
            log(f"SKIPPED {decision}: {sig.get('id')} "
                f"({sig.get('symbol')} {sig.get('direction')})")
        # Worker B (2026-09-23): the decision can be reassigned after the
        # entry dict is built (disconnect halt, filling-mode reject,
        # identity abort, duplicate suppression, AI veto, no-SL refusal).
        # The journal must record the FINAL outcome, never a stale one.
        entry["decision"] = decision
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
                # Synthetic/test signals must never enter the trading path:
                # no synthetic data in the production trading flow (§18, §32).
                if sig.get("test") is True:
                    self.journal({
                        "type": "signal.rejected",
                        "reason": "test_signal",
                        "symbol": sig.get("symbol"),
                        "note": sig.get("note", ""),
                    })
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
                # Worker C (2026-09-23): refresh the mode ledger's floating
                # P&L from the broker feed -- the drawdown gate's input.
                # handle_signal/decide populated self._mode_state above.
                if bp_fresh and self._mode_state is not None:
                    try:
                        self._mode_state.update_floating(sum(
                            float(p.get("profit") or 0)
                            for p in broker_positions))
                    except (TypeError, ValueError):
                        pass
                offset = f.tell()
        # Persist the mode ledger once per pass (day rolls and floating
        # updates above are in-memory until now).
        if self._mode_state is not None:
            try:
                self._mode_state.save(self.paths["state_dir"])
            except OSError as e:
                log(f"warning: risk-state save failed: {e}")
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
        # Worker B (2026-09-23): disconnect check first -- a stale broker
        # feed halts new entries (risk management below keeps running).
        self.check_disconnect()
        # Worker B (2026-09-23): order timeout sweep -- EXECUTION_UNKNOWN
        # detection + broker-state reconcile before any single resend.
        self.sweep_timeouts()
        self.reconcile_phantom_positions()
        # Coordinator (2026-09-23): continuous local-vs-broker reconciliation
        # (§28). Read-only: journals reconcile.finding events, never marks a
        # position closed (only broker trade.closed does that). Skips when the
        # positions feed is stale.
        try:
            from reconcile import integrate as integrate_reconcile
            integrate_reconcile(self)
        except Exception as e:
            self.journal({"type": "reconcile.error", "error": str(e)[:200]})
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
    # Worker B (2026-09-23): startup validation pins the broker identity.
    # ANY failure journals mode.unavailable and stops the process
    # (non-zero) -- never run against an unverified account.
    try:
        eng.startup_validate()
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
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
