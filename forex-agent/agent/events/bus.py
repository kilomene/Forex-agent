"""Event bus: proactive subsystem -> agent delivery.

Every subsystem occurrence worth an agent's attention is validated against
the structured schema below, persisted to the storage event queue, fanned
out to in-process subscribers, and optionally POSTed to a configured
webhook URL. The agent never has to poll "do we have a signal?" — events
are emitted proactively and remain pollable via CLI/API.

Schema (TARGET ARCHITECTURE section 4):
    {"event": "<type>", "ts": "<ISO-8601>", ...type-specific fields}

Cross-area contract: persistence goes through ``storage.store.Store``
(see agent/API_DEPS.md). A store can be injected via ``configure()``
(which is what tests do); otherwise the bus lazily imports storage.store.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("forex_agent.events")

# ---------------------------------------------------------------------------
# Schema: event type -> required / optional field names.
# "ts" and "event" are required on every event and validated separately.
# ---------------------------------------------------------------------------

EVENT_SCHEMAS: Dict[str, Dict[str, List[str]]] = {
    # Market intelligence -------------------------------------------------
    "signal.detected": {
        "required": ["symbol", "timeframe", "direction"],
        "optional": ["signal_id", "entry_price", "stop_loss", "take_profit",
                     "confidence", "strategy", "trigger", "candle_time", "note"],
    },
    "signal.approved": {
        "required": ["signal_id"],
        "optional": ["symbol", "direction", "source"],
    },
    "signal.rejected": {
        "required": ["signal_id", "reason"],
        "optional": ["symbol", "detail"],
    },
    "market.data_stale": {
        "required": ["symbol"],
        "optional": ["timeframe", "last_tick_age_s", "note"],
    },
    # Trading -------------------------------------------------------------
    "trade.requested": {
        "required": ["symbol", "side", "volume", "idempotency_key"],
        "optional": ["signal_id", "stop_loss", "take_profit", "requested_by"],
    },
    "trade.executed": {
        "required": ["symbol", "ticket", "side", "volume"],
        "optional": ["idempotency_key", "price", "signal_id", "spread_at_entry"],
    },
    "trade.rejected": {
        "required": ["reason"],
        "optional": ["symbol", "signal_id", "detail"],
    },
    "position.closed": {
        "required": ["ticket", "symbol", "reason"],
        "optional": ["close_price", "profit", "commission", "swap"],
    },
    "position.modified": {
        "required": ["ticket"],
        "optional": ["symbol", "stop_loss", "take_profit"],
    },
    "position.external_close": {
        "required": ["ticket", "symbol"],
        "optional": ["note"],
    },
    # Risk / safety -------------------------------------------------------
    "risk.blocked": {
        "required": ["reason"],
        "optional": ["symbol", "signal_id", "detail"],
    },
    "kill_switch.activated": {
        "required": ["source"],
        "optional": ["reason"],
    },
    "kill_switch.cleared": {
        "required": ["source"],
        "optional": ["note"],
    },
    # Broker --------------------------------------------------------------
    "broker.connected": {
        "required": ["adapter"],
        "optional": ["account", "server"],
    },
    "broker.disconnected": {
        "required": ["adapter"],
        "optional": ["error"],
    },
    # Worker (cloud sync layer) -------------------------------------------
    "worker.unavailable": {
        "required": ["operation"],
        "optional": ["error", "retry_in_s"],
    },
    "worker.reconnected": {
        "required": [],
        "optional": ["operation"],
    },
    # Daemons / health ----------------------------------------------------
    "daemon.started": {
        "required": ["daemon"],
        "optional": ["pid"],
    },
    "daemon.stopped": {
        "required": ["daemon"],
        "optional": ["reason", "pid"],
    },
    "health.degraded": {
        "required": ["component"],
        "optional": ["detail", "error_code"],
    },
    "health.recovered": {
        "required": ["component"],
        "optional": ["detail"],
    },
}

# Every event also carries these implicitly.
_META_FIELDS = ("event", "ts")


def _parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp or raise ValueError."""
    if not isinstance(value, str) or not value:
        raise ValueError("ts must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)  # raises ValueError on garbage


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_event(event: dict) -> dict:
    """Validate an event dict against the structured schema.

    Returns a normalized *copy* (ts stamped if absent). Raises ValueError
    listing every problem found — malformed events are rejected, never
    silently repaired (except for the missing-ts convenience stamp).
    """
    problems: List[str] = []
    if not isinstance(event, dict):
        raise ValueError("event must be a dict, got %s" % type(event).__name__)

    name = event.get("event")
    if not isinstance(name, str) or name not in EVENT_SCHEMAS:
        raise ValueError("unknown or missing event type: %r" % (name,))

    schema = EVENT_SCHEMAS[name]
    for field in schema["required"]:
        if field not in event or event[field] is None:
            problems.append("missing required field %r for event %r" % (field, name))

    known = set(schema["required"]) | set(schema["optional"]) | set(_META_FIELDS)
    for field in event:
        if field not in known:
            problems.append("unknown field %r for event %r" % (field, name))

    if "ts" in event and event["ts"] is not None:
        try:
            _parse_ts(event["ts"])
        except ValueError:
            problems.append("ts is not valid ISO-8601: %r" % (event["ts"],))

    if problems:
        raise ValueError("invalid event %r: %s" % (name, "; ".join(problems)))

    normalized = copy.deepcopy(event)
    if not normalized.get("ts"):
        normalized["ts"] = utcnow_iso()
    return normalized


# ---------------------------------------------------------------------------
# Store plumbing (storage.store is owned by the storage builder; see
# agent/API_DEPS.md for the expected interface).
# ---------------------------------------------------------------------------

_store_override = None
_store_lock = threading.Lock()


def configure(store=None, webhook_url: Optional[str] = None) -> None:
    """Inject dependencies (used by tests and embedding hosts).

    store: object with ``append_event(event: dict)`` and
           ``get_events(since=None, limit=100) -> list[dict]``.
    webhook_url: optional HTTPS URL receiving every published event.
    """
    global _store_override
    with _store_lock:
        if store is not None:
            _store_override = store
        if webhook_url is not None:
            _webhook_url[0] = webhook_url


_webhook_url: List[Optional[str]] = [None]
_cached_store = None


def _default_store():
    """Lazily import storage.store.Store (owned by the storage builder)."""
    global _cached_store
    if _cached_store is not None:
        return _cached_store
    try:
        from storage.store import Store  # noqa: PLC0415 (lazy, documented dep)
    except ImportError as exc:
        raise RuntimeError(
            "storage.store is unavailable; the event bus requires the storage "
            "package (see agent/API_DEPS.md). Original error: %s" % exc
        ) from exc
    _cached_store = Store()
    return _cached_store


def _get_store():
    if _store_override is not None:
        return _store_override
    return _default_store()


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

_subscribers: Dict[str, List[Callable[[dict], None]]] = {}
_sub_lock = threading.Lock()


def subscribe(event_type: str, handler: Callable[[dict], None]) -> None:
    """Register ``handler(event_dict)`` for an event type (or "*" for all).

    Handler exceptions are caught and logged — one bad subscriber never
    breaks publishing for the others.
    """
    if not callable(handler):
        raise ValueError("handler must be callable")
    with _sub_lock:
        _subscribers.setdefault(event_type, []).append(handler)


def unsubscribe(event_type: str, handler: Callable[[dict], None]) -> bool:
    with _sub_lock:
        handlers = _subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
    return False


def _dispatch(event: dict) -> None:
    with _sub_lock:
        handlers = list(_subscribers.get(event["event"], [])) + list(_subscribers.get("*", []))
    for handler in handlers:
        try:
            handler(event)
        except Exception:
            logger.exception("event subscriber failed for %s", event.get("event"))


def _deliver_webhook(event: dict) -> None:
    url = _webhook_url[0]
    if not url:
        return
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status >= 300:
                logger.warning("webhook delivery returned HTTP %s", resp.status)
    except Exception as exc:
        # Webhook is best-effort: the durable record is the local event
        # log. Never let a webhook failure break publishing.
        logger.warning("webhook delivery failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def publish(event: dict) -> dict:
    """Validate, persist, fan out, and webhook-deliver one event.

    Returns the normalized stored event. Raises ValueError on schema
    violations and RuntimeError if the storage backend is unavailable.
    """
    normalized = validate_event(event)
    _get_store().append_event(normalized)
    _dispatch(normalized)
    _deliver_webhook(normalized)
    return normalized


def poll(since: Optional[str] = None, limit: int = 100) -> List[dict]:
    """Read persisted events, newest-first cap ``limit``.

    ``since`` is an ISO-8601 timestamp; only events at/after it are
    returned. Raises ValueError on a malformed ``since``.
    """
    if since is not None:
        _parse_ts(since)  # validate early
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive int")
    return _get_store().get_events(since=since, limit=limit or 100)


def reset_for_tests() -> None:
    """Clear subscribers and injected store. Tests only."""
    global _store_override, _cached_store
    with _sub_lock:
        _subscribers.clear()
    with _store_lock:
        _store_override = None
    _cached_store = None
    _webhook_url[0] = None
