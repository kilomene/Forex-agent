"""Signal capabilities: local signal ledger derived from the event queue.

The subsystem's own signal lifecycle (detected -> approved/rejected ->
executed -> closed) is recorded as events; these tools read that local
record. The cloud (Worker D1) ledger remains the long-term store, but the
agent never depends on the network to see what the subsystem just did.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from . import backend

logger = logging.getLogger("forex_agent.tools.signals")

# Lifecycle derivation: latest related event wins.
_STATUS_RANK = {
    "signal.detected": 0,
    "signal.approved": 1,
    "signal.rejected": 2,
    "trade.requested": 3,
    "trade.executed": 4,
    "trade.rejected": 4,
    "risk.blocked": 4,
    "position.closed": 5,
}
_EVENT_TO_STATUS = {
    "signal.detected": "detected",
    "signal.approved": "approved",
    "signal.rejected": "rejected",
    "trade.requested": "requested",
    "trade.executed": "executed",
    "trade.rejected": "rejected",
    "risk.blocked": "blocked",
    "position.closed": "closed",
}

_TRACKED = set(_STATUS_RANK)


def _store():
    try:
        return backend.store(), None
    except RuntimeError as exc:
        return None, backend.dep_unavailable("storage", exc)


def _signal_id_of(event: dict) -> Optional[str]:
    for key in ("signal_id", "id"):
        value = event.get(key)
        if value:
            return str(value)
    return None


def _lifecycle(signal_events: List[dict]) -> Dict[str, dict]:
    """Fold tracked events into per-signal records with derived status.

    position.closed events carry a ticket, not a signal_id (the daemon
    closes by ticket). They are attributed to a signal through the
    ticket -> signal_id map built from trade.executed events.
    """
    ordered = sorted(signal_events, key=lambda e: e.get("ts", ""))
    ticket_to_signal: Dict[str, str] = {}
    for evt in ordered:
        if evt.get("event") in ("trade.executed", "trade.requested",
                                "trade.rejected", "risk.blocked"):
            sid, ticket = _signal_id_of(evt), evt.get("ticket")
            if sid and ticket:
                ticket_to_signal[str(ticket)] = sid
    records: Dict[str, dict] = {}
    for evt in ordered:
        name = evt.get("event")
        if name not in _TRACKED:
            continue
        sid = _signal_id_of(evt)
        if not sid and name == "position.closed":
            sid = ticket_to_signal.get(str(evt.get("ticket")))
        if not sid:
            continue
        rec = records.setdefault(sid, {"signal_id": sid, "status": "detected",
                                       "symbol": None, "direction": None,
                                       "timeframe": None, "history": [],
                                       "_rank": 0})
        if evt.get("symbol"):
            rec["symbol"] = evt["symbol"]
        if evt.get("direction"):
            rec["direction"] = evt["direction"]
        if evt.get("timeframe"):
            rec["timeframe"] = evt["timeframe"]
        if name == "signal.detected" and not rec.get("detected_at"):
            rec["detected_at"] = evt.get("ts")
            for k in ("entry_price", "stop_loss", "take_profit", "confidence",
                      "strategy", "trigger"):
                if evt.get(k) is not None:
                    rec[k] = evt[k]
        # Status only moves forward through the lifecycle.
        if _STATUS_RANK[name] >= rec["_rank"]:
            rec["_rank"] = _STATUS_RANK[name]
            rec["status"] = _EVENT_TO_STATUS[name]
            rec["status_at"] = evt.get("ts")
        rec["history"].append({"event": name, "ts": evt.get("ts"),
                              "reason": evt.get("reason")})
    for rec in records.values():
        rec.pop("_rank", None)
    return records


def _recent_tracked_events(limit: int = 500) -> List[dict]:
    from agent.events.bus import poll
    events = poll(limit=limit)
    return [e for e in events if e.get("event") in _TRACKED]


def forex_get_signal(signal_id: str) -> dict:
    """Full local record for one signal, including lifecycle status."""
    store, error = _store()
    if error:
        return error
    if not signal_id:
        return backend.err("INVALID_ARGUMENT", "signal_id is required")
    try:
        records = _lifecycle(_recent_tracked_events())
    except Exception as exc:
        return backend.err("DEPENDENCY_UNAVAILABLE", "Could not read event queue: %s" % exc)
    rec = records.get(str(signal_id))
    if not rec:
        return {"ok": True, "found": False, "signal_id": str(signal_id),
                "note": "No local record for this signal id. It may predate the "
                        "local event log or live only in the Worker cloud ledger."}
    result = {"ok": True, "found": True, "signal_id": str(signal_id)}
    result.update(rec)
    return result


def forex_get_signals(status: Optional[str] = None, limit: int = 20) -> dict:
    """Recent signals with derived lifecycle status, newest first.

    status filter: detected|approved|rejected|requested|executed|blocked|closed.
    """
    store, error = _store()
    if error:
        return error
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        return backend.err("INVALID_ARGUMENT", "limit must be an integer")
    if status is not None and status not in set(_EVENT_TO_STATUS.values()):
        return backend.err("INVALID_ARGUMENT",
                           "unknown status %r (expected one of %s)"
                           % (status, sorted(set(_EVENT_TO_STATUS.values()))))
    try:
        records = _lifecycle(_recent_tracked_events())
    except Exception as exc:
        return backend.err("DEPENDENCY_UNAVAILABLE", "Could not read event queue: %s" % exc)
    ordered = sorted(records.values(),
                     key=lambda r: r.get("detected_at") or r.get("status_at") or "",
                     reverse=True)
    if status:
        ordered = [r for r in ordered if r["status"] == status]
    return {"ok": True, "count": len(ordered[:limit]), "signals": ordered[:limit],
            "note": "Local event-derived ledger. The Worker D1 holds the full cloud history."}
