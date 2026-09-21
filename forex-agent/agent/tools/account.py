"""Account, positions, risk, performance, and trade-history capabilities.

Ports of the Worker tools calculate_position_risk / get_correlation_exposure /
get_performance_stats / get_trade_history, rehomed to local implementations
against the real BrokerAdapter, the persisted risk state, and the local
event log. Read-only: nothing here places, modifies, or closes anything.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import backend

logger = logging.getLogger("forex_agent.tools.account")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _position_to_dict(pos: Any) -> dict:
    data = _jsonable(pos)
    if not isinstance(data, dict):
        return {"repr": repr(pos)}
    # Enrich with the bot-identity helpers (not dataclass fields).
    try:
        data["is_own"] = bool(pos.is_own)
    except Exception:
        pass
    try:
        data["signal_id"] = pos.signal_id
    except Exception:
        pass
    return data


def _adapter():
    try:
        return backend.broker_adapter(), None
    except RuntimeError as exc:
        return None, backend.dep_unavailable("broker", exc)


def _kill_switch_state() -> dict:
    """Local kill-switch latch state (authoritative, from storage)."""
    try:
        state = backend.store().get_kill_switch()
        if isinstance(state, dict) and "engaged" in state:
            return {"engaged": bool(state["engaged"]),
                    "source": state.get("source"), "ts": state.get("ts")}
    except Exception as exc:
        logger.warning("kill-switch state read failed: %s", exc)
    return {"engaged": None, "note": "kill-switch state unavailable"}


def forex_get_positions() -> dict:
    """Currently open positions from the broker (ground truth).

    Each record carries is_own (magic-number filtered: True only for
    positions this bot opened) so foreign/manual positions are never
    confused with the bot's own exposure.
    """
    adapter, error = _adapter()
    if error:
        return error
    try:
        positions = adapter.positions()
    except Exception as exc:
        return backend.broker_error_result(exc, "Could not read positions")
    rows = [_position_to_dict(p) for p in (positions or [])]
    own = [r for r in rows if r.get("is_own")]
    return {"ok": True, "count": len(rows), "own_count": len(own),
            "positions": rows}


def forex_get_account() -> dict:
    """Broker account snapshot: balance AND equity, margin, currency."""
    adapter, error = _adapter()
    if error:
        return error
    try:
        info = adapter.account_info()
    except Exception as exc:
        return backend.broker_error_result(exc, "Could not read account info")
    return {"ok": True, "account": _jsonable(info)}


def _position_risk_calc(adapter, symbol: str, lot_size: float,
                       stop_distance: float, entry_price: Optional[float]) -> dict:
    """Port of the Worker calculate_position_risk tool: real pip-value math.

    Prefers real broker contract values (SymbolSpec.tick_value/tick_size);
    falls back to the JPY heuristic only when specs are unavailable,
    labeled honestly as an estimate.
    """
    # 1) Real broker specs when available.
    try:
        specs = adapter.symbols(names=[symbol]) or adapter.symbols() or []
        for spec in specs:
            name = getattr(spec, "name", "") or ""
            if name.upper() != symbol.upper():
                continue
            tick_value = getattr(spec, "tick_value", 0) or 0
            tick_size = getattr(spec, "tick_size", 0) or 0
            if tick_value > 0 and tick_size > 0 and stop_distance > 0:
                ticks = stop_distance / tick_size
                risk = ticks * tick_value * lot_size
                return {
                    "symbol": symbol, "lot_size": lot_size,
                    "stop_distance_ticks": round(ticks, 1),
                    "estimated_dollar_risk": round(risk, 2),
                    "note": ("Calculated from real broker-specified contract values "
                             "(tick_value/tick_size), not an estimate."),
                }
    except Exception as exc:
        logger.debug("symbol specs lookup failed: %s", exc)

    # 2) Honest JPY heuristic fallback.
    is_jpy = "JPY" in symbol.upper()
    pip_size = 0.01 if is_jpy else 0.0001
    pips = stop_distance / pip_size if pip_size else 0
    pip_value = (1000 / entry_price) if (is_jpy and entry_price) else 10.0
    return {
        "symbol": symbol, "lot_size": lot_size,
        "stop_distance_pips": round(pips, 1),
        "estimated_dollar_risk": round(pips * pip_value * lot_size, 2),
        "note": ("Broker trade specs were unavailable, so this is a forex-only "
                 "estimate (assumes a USD-denominated account) - may not be accurate "
                 "for non-forex instruments."),
    }


def forex_get_risk(symbol: Optional[str] = None, direction: Optional[str] = None,
                   lot_size: Optional[float] = None,
                   stop_loss_distance: Optional[float] = None,
                   entry_price: Optional[float] = None) -> dict:
    """Risk picture: kill-switch latch, persisted risk-engine state, open
    positions, correlated-exposure flags, and optional dollar-risk math.

    Port of calculate_position_risk + get_correlation_exposure. With no
    arguments it returns the standing risk state; with symbol/lot_size/
    stop_loss_distance it also computes the dollar risk of the hypothetical
    trade. Correlation flags use intelligence.correlation (the real port of
    knowledge.js) against the broker's live open positions — never an
    "executed, assumed open" ledger.
    """
    result: Dict[str, Any] = {"ok": True}

    # Kill-switch latch (local, authoritative — storage, not the cloud).
    ks = _kill_switch_state()
    result["kill_switch_engaged"] = ks.get("engaged")
    if ks.get("source"):
        result["kill_switch_source"] = ks["source"]

    # Persisted risk-engine state (daily-loss baseline, loss streak —
    # survives restarts, unlike the original in-memory risk.py).
    try:
        result["risk_state"] = backend.store().get_risk_state() or {}
    except Exception as exc:
        result["risk_state"] = {"note": "risk state unavailable: %s" % exc}

    # Open positions + correlation flags (needs the adapter).
    adapter, error = _adapter()
    open_positions: List[dict] = []
    if not error:
        try:
            open_positions = [_position_to_dict(p) for p in (adapter.positions() or [])]
        except Exception as exc:
            logger.warning("get_risk: positions read failed: %s", exc)
    result["open_position_count"] = len(open_positions)
    result["open_positions"] = open_positions
    if symbol and direction:
        try:
            corr = backend.correlation()
            result["correlation_flags"] = corr.check_correlated_exposure(
                {"symbol": symbol.upper(), "direction": direction.upper()},
                open_positions)
        except RuntimeError as exc:
            result["correlation_flags"] = []
            result["correlation_note"] = "correlation engine unavailable: %s" % exc
    else:
        result["correlation_flags"] = []

    # Hypothetical-trade dollar risk.
    if symbol and lot_size and stop_loss_distance:
        if error:
            result["position_risk"] = {
                "available": False,
                "note": "broker unavailable - cannot compute exact risk"}
        else:
            try:
                result["position_risk"] = _position_risk_calc(
                    adapter, symbol, float(lot_size),
                    float(stop_loss_distance), entry_price)
            except (TypeError, ValueError):
                return backend.err("INVALID_ARGUMENT",
                                   "lot_size and stop_loss_distance must be numbers")
    return result


def forex_get_performance(lookback_days: int = 30) -> dict:
    """Real track record: win rate, profit factor, net P/L.

    Port of get_performance_stats. Computed live from actual broker deal
    history via core.performance.compute_performance — ground truth, not
    vibes. With no closed own-trades in the window it says so honestly
    (total_closed_trades: 0) instead of inventing numbers.
    """
    try:
        lookback_days = max(1, min(int(lookback_days), 365))
    except (TypeError, ValueError):
        return backend.err("INVALID_ARGUMENT", "lookback_days must be an integer")
    try:
        perf = backend.performance_mod()
    except RuntimeError as exc:
        return backend.dep_unavailable("core.performance", exc)
    compute = getattr(perf, "compute_performance", None)
    to_dict = getattr(perf, "snapshot_to_dict", None)
    if not callable(compute) or not callable(to_dict):
        return backend.err("DEPENDENCY_UNAVAILABLE",
                           "core.performance does not expose compute_performance/"
                           "snapshot_to_dict yet")
    adapter, error = _adapter()
    if error:
        return error
    try:
        snapshot = compute(adapter, lookback_days=lookback_days)
    except Exception as exc:
        return backend.broker_error_result(exc, "Could not read deal history")
    data = to_dict(snapshot)
    if not isinstance(data, dict):
        return backend.err("DEPENDENCY_UNAVAILABLE",
                           "core.performance returned an unexpected shape")
    data = dict(data)
    data["ok"] = True
    data["available"] = bool(data.get("total_closed_trades"))
    if not data["available"]:
        data["note"] = ("No closed own-trades in the last %d days - no track "
                        "record to report yet." % lookback_days)
    return data


def forex_get_trade_history(symbol: Optional[str] = None, limit: int = 10) -> dict:
    """Recent trade outcomes from the local event-derived log.

    Port of the Worker get_trade_history tool: what fired before and what
    happened to it (executed, rejected, blocked, closed). The full cloud
    history lives in the Worker D1; this is the local record of what this
    subsystem did.
    """
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        return backend.err("INVALID_ARGUMENT", "limit must be an integer")
    try:
        from agent.events.bus import poll
        events = poll(limit=500)
    except Exception as exc:
        return backend.err("DEPENDENCY_UNAVAILABLE",
                           "Could not read event log: %s" % exc)

    wanted = {"trade.executed", "trade.rejected", "risk.blocked",
              "position.closed", "signal.rejected"}
    records = []
    for evt in events:
        if evt.get("event") not in wanted:
            continue
        if symbol and str(evt.get("symbol", "")).upper() != symbol.upper():
            continue
        records.append({
            "event": evt["event"],
            "ts": evt.get("ts"),
            "symbol": evt.get("symbol"),
            "ticket": evt.get("ticket"),
            "signal_id": evt.get("signal_id"),
            "reason": evt.get("reason"),
            "profit": evt.get("profit"),
        })
        if len(records) >= limit:
            break
    summary: Dict[str, int] = {}
    for rec in records:
        summary[rec["event"]] = summary.get(rec["event"], 0) + 1
    return {
        "ok": True, "count": len(records), "records": records,
        "summary": ("Last %d trade events: %s."
                    % (len(records), ", ".join("%d %s" % (c, e) for e, c in summary.items()))
                    if records else "No trade history in the local event log yet."),
        "note": "Local event-derived history. Full cloud history lives in the Worker D1.",
    }
