#!/usr/bin/env python3
"""Active exit management for the Nova MT5 trading stack.

Evaluates exit policies per open BROKER position each cycle and emits
exit commands through the normal nova_commands.jsonl command path (so the
executor's identity lock and idempotency rules apply). A position is only
treated as closed when the broker confirms it via a trade.closed event —
this module never marks anything closed itself.

Policies (all broker-confirmed):
  * trailing_stop       - once unrealized profit reaches +trigger_r R, lock in
                          +lock_r R behind the peak; when price falls back to
                          the trail level, emit a close command. R is computed
                          from the BROKER position's own SL distance.
  * max_holding_time    - close positions open longer than the limit.
  * strategy_invalidated- caller-supplied predicate; default: none.
  * emergency_exit      - kill switch ON or emergency drawdown breached ->
                          close-all commands.

Pure/testable core: evaluate_exits(broker_positions, journal_state, now,
state, config, invalidation, kill_switch_on, emergency_drawdown).
Thin loop hook: integrate(engine) -- call once per executor cycle.

One-line integration (for Worker B / integrator, inside TraderEngine.run_once):
    from exit_manager import integrate as integrate_exits; integrate_exits(self)

This module is NOT wired into the live loop by default (avoid conflicts
with Worker B's executor changes).
"""

import json
import os
import sys
import time

BRIDGE = os.path.dirname(os.path.abspath(__file__))
if BRIDGE not in sys.path:
    sys.path.insert(0, BRIDGE)

# Default policy parameters. Tune via run/exit_config.json (optional).
DEFAULT_EXIT_CONFIG = {
    "trail_trigger_r": 2.0,        # arm trailing once unrealized >= +2R
    "trail_lock_r": 1.0,           # trail locks this many R behind the peak
    "max_holding_seconds": 72 * 3600,  # 72h default max hold
    "emergency_drawdown_pct": 1.0, # of capital basis -> close all
    "exit_config_path": None,      # filled at runtime; not persisted
}

EXIT_COMMAND_PREFIX = "exit"


# --------------------------------------------------------------------------
# R-multiple math (broker state authoritative)
# --------------------------------------------------------------------------

def position_r_multiple(pos):
    """Unrealized R for a broker position dict.

    R = favorable move / initial risk distance, where risk distance comes
    from the BROKER's own SL on the position. Returns None when SL is
    missing/zero (fail-closed: no trailing decisions without risk distance).
    """
    try:
        entry = float(pos.get("open_price"))
        cur = float(pos.get("current_price"))
        sl = float(pos.get("sl") or 0)
        direction = (pos.get("direction") or "").upper()
    except (TypeError, ValueError):
        return None
    risk_dist = abs(entry - sl)
    if risk_dist <= 0:
        return None
    if direction == "BUY":
        return (cur - entry) / risk_dist
    if direction == "SELL":
        return (entry - cur) / risk_dist
    return None


def _pos_time_s(pos):
    """Open time of a broker position as epoch seconds (None if unknown)."""
    ot = pos.get("open_time")
    if isinstance(ot, (int, float)) and ot > 0:
        return float(ot)
    return None


# --------------------------------------------------------------------------
# Pure evaluation
# --------------------------------------------------------------------------

def _make_close_command(ticket, reason, now_s):
    return {
        "type": "trade.close",
        "id": f"{EXIT_COMMAND_PREFIX}-{reason}-{ticket}-{int(now_s)}",
        "position_id": ticket,
        "reason": reason,
    }


def evaluate_exits(broker_positions, journal_state=None, now=None, state=None,
                   config=None, invalidation=None, kill_switch_on=False,
                   emergency_drawdown=False):
    """Evaluate exit policies for every open broker position.

    Args:
        broker_positions: list of broker position dicts (nova_positions.json).
        journal_state:    optional dict with per-ticket journal context
                          (entry sl/tp, signal ids). May be {}.
        now:              epoch seconds (default: time.time()).
        state:            persistent per-ticket exit state dict
                          {ticket: {"peak_r": float, "armed": bool}}.
        config:           policy params (defaults from DEFAULT_EXIT_CONFIG).
        invalidation:     optional callable(position, journal_state)
                          -> reason string or None.
        kill_switch_on:   bool -> emergency close-all.
        emergency_drawdown:bool -> emergency close-all.

    Returns (commands, new_state): commands are trade.close dicts (requests,
    NOT claims of closure); new_state is the updated per-ticket state.
    """
    now_s = now if now is not None else time.time()
    cfg = dict(DEFAULT_EXIT_CONFIG)
    if config:
        cfg.update(config)
    journal_state = journal_state or {}
    state = dict(state or {})
    commands = []

    emergency = bool(kill_switch_on or emergency_drawdown)
    for pos in broker_positions or []:
        ticket = pos.get("ticket")
        if ticket is None:
            continue
        key = str(ticket)

        # Emergency exit: close everything, no further policy evaluation.
        if emergency:
            commands.append(_make_close_command(
                ticket, "emergency_exit", now_s))
            continue

        # Max holding time.
        opened = _pos_time_s(pos)
        if opened is not None:
            held = now_s - opened
            if held >= cfg["max_holding_seconds"]:
                commands.append(_make_close_command(
                    ticket, "max_holding_time", now_s))
                continue

        # Strategy invalidation (caller-supplied predicate).
        if invalidation is not None:
            reason = invalidation(pos, journal_state.get(key, {}))
            if reason:
                commands.append(_make_close_command(
                    ticket, "strategy_invalidated", now_s))
                continue

        # Trailing stop (R-based, armed only past +trigger_r).
        r = position_r_multiple(pos)
        st = state.get(key, {"peak_r": 0.0, "armed": False})
        if r is not None:
            if r >= cfg["trail_trigger_r"]:
                st["armed"] = True
            if st["armed"]:
                st["peak_r"] = max(st.get("peak_r", 0.0), r)
                trail_level = st["peak_r"] - cfg["trail_lock_r"]
                if r <= trail_level:
                    commands.append(_make_close_command(
                        ticket, "trailing_stop", now_s))
                    continue
        state[key] = st

    # Drop state for tickets no longer open (keeps the dict bounded).
    open_keys = {str(p.get("ticket")) for p in broker_positions or []
                 if p.get("ticket") is not None}
    state = {k: v for k, v in state.items() if k in open_keys}
    return commands, state


# --------------------------------------------------------------------------
# Thin executor-loop hook (NOT wired in by default)
# --------------------------------------------------------------------------

def _load_exit_config(state_dir):
    cfg = dict(DEFAULT_EXIT_CONFIG)
    path = os.path.join(state_dir, "exit_config.json")
    try:
        with open(path) as f:
            user = json.load(f)
        if isinstance(user, dict):
            cfg.update(user)
    except (OSError, ValueError):
        pass
    return cfg


def integrate(engine, invalidation=None):
    """One executor-cycle hook. Call from TraderEngine.run_once():

        from exit_manager import integrate as integrate_exits
        integrate_exits(self)

    Reads broker positions via the executor's own loader, evaluates exit
    policies, appends trade.close commands through the normal command path,
    journals {"type": "exit.command", ...} for each, and persists exit state
    in engine.state["exit_state"]. Skips tickets that already have an
    unconfirmed exit command pending (idempotent across cycles).
    """
    import trade_executor as te

    paths = engine.paths
    cfg = _load_exit_config(paths["state_dir"])
    positions, fresh = te.load_broker_positions(paths["files_dir"])
    if not fresh:
        return {"status": "skipped", "reason": "stale_positions"}

    kill_on = te.kill_switch_on(paths["kill"])
    now_s = time.time()
    exit_state = engine.state.get("exit_state", {})
    pending = engine.state.get("exit_pending", {})  # ticket -> command id

    # journal_state: per-ticket context from the executor's open_map.
    journal_state = {}
    for t, info in (engine.state.get("open_map") or {}).items():
        journal_state[str(t)] = info

    commands, new_state = evaluate_exits(
        positions,
        journal_state=journal_state,
        now=now_s,
        state=exit_state,
        config=cfg,
        invalidation=invalidation,
        kill_switch_on=kill_on,
    )

    sent = []
    for cmd in commands:
        key = str(cmd["position_id"])
        if key in pending:
            continue  # already requested; wait for broker trade.closed
        with open(paths["commands"], "a") as f:
            f.write(json.dumps(cmd) + "\n")
        engine.journal({"type": "exit.command", "command": cmd,
                        "time": te.now_str()})
        pending[key] = cmd["id"]
        sent.append(cmd)

    # Clear pending flags once the broker confirms the close.
    broker_tickets = {str(p.get("ticket")) for p in positions}
    for key in list(pending):
        if key not in broker_tickets:
            pending.pop(key, None)

    engine.state["exit_state"] = new_state
    engine.state["exit_pending"] = pending
    engine.save()
    return {"status": "ok", "sent": len(sent), "commands": sent}
