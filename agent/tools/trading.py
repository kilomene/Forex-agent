"""Trade-affecting capabilities. THE safety-critical module.

INVARIANT: every function here routes through core.execution.gateway.
The agent NEVER touches the broker adapter directly - the gateway
enforces, in order: idempotency, kill-switch latch, dry-run block,
mandatory SL, per-trade risk, daily loss (on equity), position/exposure
limits, volume clamping to broker steps, spread limits, market-open
check, and symbol whitelist. Every decision is audit-logged by the
gateway via storage, and the gateway itself emits the trade lifecycle
events (trade.requested/executed/rejected, risk.blocked, kill_switch.activated)
— this module never re-emits trade events; it only announces
kill_switch.cleared, which the gateway stays silent on.

Nothing in this module may import the broker package or MetaTrader5.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Optional

from agent.events.bus import publish as _publish_bus

from . import backend

logger = logging.getLogger("forex_agent.tools.trading")

_VALID_DIRECTIONS = ("BUY", "SELL")


def _gateway():
    try:
        return backend.gateway(), None
    except RuntimeError as exc:
        return None, backend.dep_unavailable("core.execution.gateway", exc)


def _supports_request_id(fn) -> bool:
    """True when a gateway method accepts the request_id kwarg.

    Seam compatibility: older gateway doubles (e.g. the interface
    fakes) predate request_id. We degrade to the legacy signature
    rather than failing — never by retrying a call that may already
    have executed.
    """
    try:
        return "request_id" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _gateway_call(fn, *args, request_id=None, **kwargs):
    """Call a gateway method, passing request_id only when supported."""
    if request_id is not None and _supports_request_id(fn):
        return fn(*args, request_id=request_id, **kwargs)
    return fn(*args, **kwargs)


def _decision_to_result(decision) -> dict:
    """Map core.execution.gateway.GatewayDecision to a tool result.

    The result always carries the lifecycle ``state`` — "executed"
    appears ONLY after broker confirmation; dry-run or unconfirmed
    submissions report their true state (dry_run_simulated, rejected,
    failed, ...), never executed.
    """
    result = {
        "ok": bool(decision.approved),
        "idempotency_key": decision.idempotency_key,
        "request_id": getattr(decision, "request_id", "") or "",
        "state": getattr(decision, "state", "") or "",
        "volume": decision.volume,
    }
    if decision.approved:
        result.update({
            "ticket": decision.ticket,
            "price": decision.price,
            "audit_id": decision.audit_id,
            "audit_pending": decision.audit_pending,
        })
        if decision.reason_code:
            result["note"] = decision.reason
    else:
        result.update({
            "error_code": decision.reason_code or "REJECTED",
            "message": decision.reason,
            "audit_id": decision.audit_id,
            "audit_pending": decision.audit_pending,
        })
    return result


def forex_request_trade(symbol: str, direction: str, volume: float,
                        stop_loss: float, take_profit: Optional[float] = None,
                        signal_id: Optional[str] = None,
                        idempotency_key: Optional[str] = None,
                        request_id: Optional[str] = None) -> dict:
    """Request a trade through the execution gateway. Returns its verdict.

    On approval the gateway has ALREADY submitted to the broker and the
    result carries the broker-confirmed ticket/price with state "executed".
    On rejection it returns ok:false with a structured error_code
    (KILL_SWITCH_ENGAGED, DRY_RUN_BLOCKED, RISK_LIMIT_EXCEEDED,
    INVALID_SYMBOL, ...). Trades are never reported as executed before
    broker confirmation: dry-run replays report state "dry_run_simulated".
    ``request_id`` (uuid) is the idempotency identity: repeating a
    request_id returns the ORIGINAL decision — never a second order.
    """
    gateway, error = _gateway()
    if error:
        return error
    if not symbol or not isinstance(symbol, str):
        return backend.err("INVALID_SYMBOL", "symbol must be a non-empty string")
    direction = str(direction).upper()
    if direction not in _VALID_DIRECTIONS:
        return backend.err("INVALID_DIRECTION",
                           "direction must be BUY or SELL, got %r" % direction)
    try:
        volume = float(volume)
        stop_loss = float(stop_loss)
        take_profit = float(take_profit) if take_profit is not None else None
    except (TypeError, ValueError):
        return backend.err("INVALID_ARGUMENT",
                           "volume, stop_loss, take_profit must be numbers")
    if volume <= 0:
        return backend.err("INVALID_ARGUMENT", "volume must be positive")
    if stop_loss <= 0:
        return backend.err("INVALID_ARGUMENT",
                           "stop_loss is mandatory and must be positive")

    try:
        from core.execution.gateway import TradeRequest
    except ImportError as exc:
        return backend.dep_unavailable("core.execution.gateway", exc)

    req = TradeRequest(
        signal_id=signal_id or "agent:%s" % uuid.uuid4().hex[:12],
        symbol=symbol.upper(),
        direction=direction,
        stop_loss=stop_loss,
        take_profit=take_profit if take_profit is not None else 0.0,
        volume=volume,  # informational; the risk engine does the sizing
        idempotency_key=idempotency_key or uuid.uuid4().hex,
        request_id=request_id or uuid.uuid4().hex,
        source="agent",
    )
    try:
        decision = gateway.request_trade(req)
    except Exception as exc:
        logger.exception("gateway request_trade raised")
        return backend.err("GATEWAY_ERROR", "Execution gateway failed: %s" % exc)
    return _decision_to_result(decision)


def forex_close_position(ticket, request_id: Optional[str] = None) -> dict:
    """Close an open position by ticket.

    Routes through the execution gateway (never the adapter directly).
    Closing is risk-reducing, so it is allowed even while the kill switch
    is engaged. The gateway audit-logs the decision and emits
    position.closed; the broker-confirmed close price is returned.
    ``request_id`` (uuid) is the idempotency identity: repeating a
    request_id returns the ORIGINAL result — never a second close.
    """
    gateway, error = _gateway()
    if error:
        return error
    try:
        result = _gateway_call(gateway.close_position, ticket, source="agent",
                               request_id=request_id)
    except Exception as exc:
        logger.exception("gateway close_position raised")
        return backend.err("GATEWAY_ERROR", "Execution gateway failed: %s" % exc)
    return result


def forex_modify_position(ticket, stop_loss: Optional[float] = None,
                          take_profit: Optional[float] = None,
                          request_id: Optional[str] = None) -> dict:
    """Modify SL/TP on an open position.

    Routes through the execution gateway. The mandatory-SL rule is
    enforced: an SL can be tightened but never removed. Modifies are
    blocked while the kill switch is engaged. The gateway audit-logs
    the decision and emits position.modified. ``request_id`` (uuid) is
    the idempotency identity: repeating a request_id returns the
    ORIGINAL result — never a second modify.
    """
    gateway, error = _gateway()
    if error:
        return error
    try:
        sl = float(stop_loss) if stop_loss is not None else None
        tp = float(take_profit) if take_profit is not None else None
    except (TypeError, ValueError):
        return backend.err("INVALID_ARGUMENT",
                           "stop_loss and take_profit must be numbers")
    try:
        result = _gateway_call(gateway.modify_position, ticket,
                               stop_loss=sl, take_profit=tp, source="agent",
                               request_id=request_id)
    except Exception as exc:
        logger.exception("gateway modify_position raised")
        return backend.err("GATEWAY_ERROR", "Execution gateway failed: %s" % exc)
    return result


def forex_kill_switch(engage: bool = True, reason: Optional[str] = None) -> dict:
    """Engage or clear the kill switch via the gateway's KillSwitch.

    The latch lives in storage (get/set_kill_switch) — the source of
    truth; KillSwitch fails CLOSED (unreadable latch => trading stops).
    Engaging also triggers the gateway to block all new entries, and the
    health_monitor daemon sweeps open positions closed. The gateway emits
    kill_switch.activated itself; this tool emits kill_switch.cleared on
    disengage (the gateway stays silent there).
    """
    gateway, error = _gateway()
    if error:
        return error
    kill_switch = getattr(gateway, "kill_switch", None)
    if kill_switch is None:
        return backend.err("GATEWAY_ERROR",
                           "gateway exposes no kill_switch controller")
    try:
        if engage:
            state = kill_switch.engage(source="agent")
        else:
            state = kill_switch.disengage(source="agent")
    except Exception as exc:
        logger.exception("kill-switch call raised")
        return backend.err("GATEWAY_ERROR", "Kill switch failed: %s" % exc)
    if not isinstance(state, dict):
        return backend.err("GATEWAY_ERROR", "kill switch returned a non-dict state")
    engaged = bool(state.get("engaged", engage))
    if not engaged:
        # The gateway only announces engagement; announce the clear.
        try:
            _publish_bus({"event": "kill_switch.cleared", "source": "agent"})
        except Exception as exc:
            logger.warning("kill_switch.cleared emission failed: %s", exc)
    result = {"ok": True, "engaged": engaged, "source": state.get("source")}
    if engage and reason:
        result["reason"] = reason
    return result
