"""Capability registry: the 18 forex.* capabilities as local Python functions.

Each entry: {"description", "input_schema" (JSON Schema), "func"}.
The MCP server (agent/mcp), the CLI (scripts/forex), and the local API
(scripts/local_api.py) all dispatch through this registry - one
implementation, three interfaces.

Capability names follow the TARGET ARCHITECTURE section 7 surface:
forex.market_data, forex.analyze, forex.get_signal, forex.get_signals,
forex.get_positions, forex.get_account, forex.get_risk, forex.get_performance,
forex.get_trade_history, forex.get_smc, forex.get_session, forex.get_calendar,
forex.get_experience, forex.get_health, forex.request_trade,
forex.close_position, forex.modify_position, forex.kill_switch.
"""

from __future__ import annotations

from typing import Callable, Dict

from .account import (
    forex_get_account,
    forex_get_performance,
    forex_get_positions,
    forex_get_risk,
    forex_get_trade_history,
)
from .market import forex_analyze, forex_get_smc, forex_market_data
from .misc import forex_get_calendar, forex_get_experience, forex_get_health, forex_get_session
from .signals import forex_get_signal, forex_get_signals
from .trading import forex_close_position, forex_kill_switch, forex_modify_position, forex_request_trade


def _schema(properties: dict, required: list | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties,
                    "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


CAPABILITIES: Dict[str, Dict[str, object]] = {
    "forex.market_data": {
        "description": "Recent closed OHLC candles for a symbol/timeframe, oldest first.",
        "input_schema": _schema({
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "default": "M15"},
            "count": {"type": "integer", "default": 30},
        }, ["symbol"]),
        "func": forex_market_data,
    },
    "forex.analyze": {
        "description": "Deterministic analysis: EMA/RSI/ATR snapshot, SMC structure, "
                       "strategy evaluation on closed candles. ml_prediction is honestly "
                       "unavailable until a real model exists.",
        "input_schema": _schema({
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "default": "M15"},
        }, ["symbol"]),
        "func": forex_analyze,
    },
    "forex.get_signal": {
        "description": "Full local record for one signal id, including lifecycle status.",
        "input_schema": _schema({"signal_id": {"type": "string"}}, ["signal_id"]),
        "func": forex_get_signal,
    },
    "forex.get_signals": {
        "description": "Recent signals with derived lifecycle status (detected, approved, "
                       "rejected, executed, blocked, closed), newest first.",
        "input_schema": _schema({
            "status": {"type": "string"},
            "limit": {"type": "integer", "default": 20},
        }),
        "func": forex_get_signals,
    },
    "forex.get_positions": {
        "description": "Currently open positions from the broker (ground truth).",
        "input_schema": _schema({}),
        "func": forex_get_positions,
    },
    "forex.get_account": {
        "description": "Broker account snapshot: balance and equity, margin, currency.",
        "input_schema": _schema({}),
        "func": forex_get_account,
    },
    "forex.get_risk": {
        "description": "Risk picture: kill-switch state, risk engine state, open positions, "
                       "correlated-exposure flags, and optional dollar-risk math for a "
                       "hypothetical trade (symbol, direction, lot_size, stop_loss_distance).",
        "input_schema": _schema({
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["BUY", "SELL"]},
            "lot_size": {"type": "number"},
            "stop_loss_distance": {"type": "number"},
            "entry_price": {"type": "number"},
        }),
        "func": forex_get_risk,
    },
    "forex.get_performance": {
        "description": "Real track record (win rate, profit factor, net P/L) "
                       "computed from actual broker deal history.",
        "input_schema": _schema({
            "lookback_days": {"type": "integer", "default": 30},
        }),
        "func": forex_get_performance,
    },
    "forex.get_trade_history": {
        "description": "Recent trade outcomes from the local event-derived ledger.",
        "input_schema": _schema({
            "symbol": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        }),
        "func": forex_get_trade_history,
    },
    "forex.get_smc": {
        "description": "Smart Money Concepts structure: trend, BOS, S/R zones, liquidity "
                       "zones, fair value gaps, order blocks.",
        "input_schema": _schema({
            "symbol": {"type": "string"},
            "timeframe": {"type": "string", "default": "M15"},
        }, ["symbol"]),
        "func": forex_get_smc,
    },
    "forex.get_session": {
        "description": "Currently active forex sessions (Tokyo/London/New York) and the "
                       "liquidity implication.",
        "input_schema": _schema({}),
        "func": forex_get_session,
    },
    "forex.get_calendar": {
        "description": "Upcoming high-impact economic events, if a calendar source is "
                       "configured. Returns available:false honestly otherwise - never "
                       "invents events.",
        "input_schema": _schema({
            "symbol": {"type": "string"},
            "hours_ahead": {"type": "integer", "default": 24},
        }),
        "func": forex_get_calendar,
    },
    "forex.get_experience": {
        "description": "Past self-reflections on a symbol: what was reasoned beforehand vs "
                       "what actually happened, including documented mistakes.",
        "input_schema": _schema({
            "symbol": {"type": "string"},
            "limit": {"type": "integer", "default": 5},
        }),
        "func": forex_get_experience,
    },
    "forex.get_health": {
        "description": "Structured health of every subsystem component: broker, kill "
                       "switch, event bus, Worker reachability, daemons.",
        "input_schema": _schema({}),
        "func": forex_get_health,
    },
    "forex.request_trade": {
        "description": "Request a trade via the Execution Gateway (risk checks, kill "
                       "switch, dry-run block enforced; audit-logged). Returns the "
                       "gateway verdict - never reports execution before broker "
                       "confirmation. stop_loss is mandatory.",
        "input_schema": _schema({
            "symbol": {"type": "string"},
            "direction": {"type": "string", "enum": ["BUY", "SELL"]},
            "volume": {"type": "number"},
            "stop_loss": {"type": "number"},
            "take_profit": {"type": "number"},
            "signal_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "request_id": {"type": "string"},
        }, ["symbol", "direction", "volume", "stop_loss"]),
        "func": forex_request_trade,
    },
    "forex.close_position": {
        "description": "Close an open position by ticket through the execution "
                       "gateway (audit-logged; broker-confirmed close price "
                       "returned). Allowed even while the kill switch is "
                       "engaged (closing reduces risk). Repeating a request_id "
                       "returns the original result — never a second close.",
        "input_schema": _schema({
            "ticket": {"type": "string"},
            "request_id": {"type": "string"},
        }, ["ticket"]),
        "func": forex_close_position,
    },
    "forex.modify_position": {
        "description": "Modify SL/TP on an open position through the execution "
                       "gateway. Mandatory SL is enforced (tighten only, never "
                       "loosen or remove). Blocked while the kill switch is "
                       "engaged. Repeating a request_id returns the original "
                       "result — never a second modify.",
        "input_schema": _schema({
            "ticket": {"type": "string"},
            "stop_loss": {"type": "number"},
            "take_profit": {"type": "number"},
            "request_id": {"type": "string"},
        }, ["ticket"]),
        "func": forex_modify_position,
    },
    "forex.kill_switch": {
        "description": "Engage (default) or clear the kill switch. Engaging latches "
                       "locally below the agent layer: the gateway blocks all new "
                       "entries immediately.",
        "input_schema": _schema({
            "engage": {"type": "boolean", "default": True},
            "reason": {"type": "string"},
        }),
        "func": forex_kill_switch,
    },
}


def get_capability(name: str) -> dict:
    if name not in CAPABILITIES:
        raise KeyError("unknown capability: %r" % name)
    return CAPABILITIES[name]


def call(name: str, arguments: dict | None = None) -> dict:
    """Dispatch a capability by name with a JSON-style argument dict."""
    cap = get_capability(name)
    func: Callable = cap["func"]  # type: ignore[assignment]
    args = dict(arguments or {})
    try:
        result = func(**args)
    except TypeError as exc:
        return {"ok": False, "error_code": "INVALID_ARGUMENT",
                "message": "bad arguments for %s: %s" % (name, exc)}
    if not isinstance(result, dict):
        return {"ok": False, "error_code": "INTERNAL_ERROR",
                "message": "%s returned a non-dict result" % name}
    return result


def list_capabilities() -> list:
    return [{"name": name, "description": cap["description"],
             "input_schema": cap["input_schema"]}
            for name, cap in CAPABILITIES.items()]
