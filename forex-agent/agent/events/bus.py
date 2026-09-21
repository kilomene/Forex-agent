"""Event bus: proactive subsystem -> agent delivery.

Every subsystem occurrence worth an agent's attention is validated against
the structured schema below, persisted to local storage, fanned out to
in-process subscribers, and optionally POSTed to a configured webhook URL.
The agent never has to poll "do we have a signal?" — events are emitted
proactively and remain pollable via CLI/API.

Schema (TARGET ARCHITECTURE section 4):
    {"event": "<type>", "ts": "<ISO-8601>", ...type-specific fields}

Persistence (writes via the real storage.Store surface —
see agent/API_DEPS.md):
  * ``enqueue_event`` — the durable FIFO queue. The health_monitor daemon
    drains it (``dequeue_events``) and forwards to the Worker cloud layer.
    Destructive reads, so the agent's pollable log does NOT read this.
  * ``journal_add(kind="event", ...)`` — the pollable event LOG.
    ``poll()`` reads it back non-destructively via
    ``journal_query(kind="event", since=..., limit=...)``.
  * ``event_journal_add`` — the push-event DELIVERY journal (Phase 4).
    Every published event is recorded here with its unique ``event_id``
    and severity. The SSE stream (scripts/local_api.py ``GET /events``)
    replays from this journal, so a reconnecting agent can resume with
    ``resume_from=<event_id>`` and never miss — or double-process — an
    event. Duplicates are impossible: ``event_id`` is UNIQUE, a retried
    publish of the same id is ignored, and the agent can always dedup
    on ``event_id``.
If storage later gains a dedicated event-log table / peek_events, the bus
can migrate poll() onto it; the journal fallback is documented, not hidden.

Severity (Phase 4): every event carries ``severity`` in
INFO / NOTICE / WARNING / CRITICAL. ``publish`` accepts an explicit
severity; otherwise it is mapped from the event type
(``DEFAULT_SEVERITY``), with ``risk.blocked`` escalated to CRITICAL when
its reason is the daily-loss limit. Every event also carries a unique
``event_id`` (``evt_`` + uuid4 hex), auto-assigned when absent.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import urllib.request
import uuid
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
    "market.candle_closed": {
        "required": ["symbol", "timeframe", "candle_time"],
        "optional": ["note"],
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
    "health.check": {
        "required": ["ok"],
        "optional": ["checks", "daemon", "note"],
    },
    "performance.snapshot": {
        "required": ["period_days"],
        "optional": ["snapshot", "daemon"],
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
_META_FIELDS = ("event", "ts", "event_id", "severity")

# ---------------------------------------------------------------------------
# Severity (Phase 4): INFO / NOTICE / WARNING / CRITICAL.
# ---------------------------------------------------------------------------

SEVERITIES = ("INFO", "NOTICE", "WARNING", "CRITICAL")

DEFAULT_SEVERITY: Dict[str, str] = {
    # Market intelligence -------------------------------------------------
    "signal.detected": "NOTICE",
    "signal.approved": "NOTICE",
    "signal.rejected": "NOTICE",
    "market.data_stale": "WARNING",
    "market.candle_closed": "INFO",
    # Trading -------------------------------------------------------------
    "trade.requested": "INFO",
    "trade.executed": "NOTICE",
    "trade.rejected": "WARNING",
    "position.closed": "NOTICE",
    "position.modified": "NOTICE",
    "position.external_close": "WARNING",
    # Risk / safety -------------------------------------------------------
    # risk.blocked is WARNING, escalated to CRITICAL when the reason is
    # the daily-loss limit (see _resolve_severity).
    "risk.blocked": "WARNING",
    "kill_switch.activated": "CRITICAL",
    "kill_switch.cleared": "NOTICE",
    # Broker --------------------------------------------------------------
    "broker.connected": "INFO",
    "broker.disconnected": "CRITICAL",
    # Worker (cloud sync layer) -------------------------------------------
    "worker.unavailable": "WARNING",
    "worker.reconnected": "NOTICE",
    # Daemons / health ----------------------------------------------------
    "daemon.started": "INFO",
    "daemon.stopped": "WARNING",
    "health.check": "INFO",
    "health.degraded": "WARNING",
    "health.recovered": "NOTICE",
    "performance.snapshot": "INFO",
}


def new_event_id() -> str:
    """Unique event id: ``evt_`` + uuid4 hex."""
    return "evt_" + uuid.uuid4().hex


def _resolve_severity(event_name: Optional[str], reason=None,
                      explicit: Optional[str] = None) -> str:
    """Resolve an event's severity.

    Precedence: explicit severity argument > ``severity`` already on the
    event dict > type mapping > INFO. ``risk.blocked`` whose reason names
    the daily-loss limit is escalated to CRITICAL.
    """
    if explicit is not None:
        if explicit not in SEVERITIES:
            raise ValueError(
                "invalid severity %r; must be one of %s"
                % (explicit, ", ".join(SEVERITIES)))
        return explicit
    if (event_name == "risk.blocked" and reason is not None
            and "DAILY_LOSS" in str(reason).upper()):
        return "CRITICAL"
    return DEFAULT_SEVERITY.get(event_name, "INFO")

# Journal kind under which the pollable event log is kept.
_EVENT_JOURNAL_KIND = "event"


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

    Returns a normalized *copy* (ts stamped if absent). ``event_id`` and
    ``severity`` are accepted as implicit meta fields on every event type;
    a present-but-invalid ``severity`` is rejected. Raises ValueError
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

    if "severity" in event and event["severity"] is not None:
        if event["severity"] not in SEVERITIES:
            problems.append("severity must be one of %s, got %r"
                            % (", ".join(SEVERITIES), event["severity"]))

    if "event_id" in event and event["event_id"] is not None:
        if not isinstance(event["event_id"], str) or not event["event_id"]:
            problems.append("event_id must be a non-empty string")

    if problems:
        raise ValueError("invalid event %r: %s" % (name, "; ".join(problems)))

    normalized = copy.deepcopy(event)
    if not normalized.get("ts"):
        normalized["ts"] = utcnow_iso()
    return normalized


# ---------------------------------------------------------------------------
# Store plumbing (storage.Store, owned by the storage builder;
# see agent/API_DEPS.md for the exact expected surface).
# ---------------------------------------------------------------------------

_store_override = None
_store_lock = threading.Lock()


def configure(store=None, webhook_url: Optional[str] = None) -> None:
    """Inject dependencies (used by tests and embedding hosts).

    store: storage.Store-compatible object (enqueue_event / journal_add /
           journal_query — see agent/API_DEPS.md).
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
    """Lazily build storage.Store (owned by the storage builder)."""
    global _cached_store
    if _cached_store is not None:
        return _cached_store
    try:
        from storage import Store  # noqa: PLC0415 (lazy, documented dep)
    except ImportError as exc:
        raise RuntimeError(
            "storage is unavailable; the event bus requires the storage "
            "package (see agent/API_DEPS.md). Original error: %s" % exc
        ) from exc
    _cached_store = Store()
    return _cached_store


def _get_store():
    if _store_override is not None:
        return _store_override
    return _default_store()


def _store_append_event(store, event: dict) -> None:
    """Durable queue write (+ legacy append_event fallback for old fakes)."""
    if hasattr(store, "enqueue_event"):
        store.enqueue_event(event)
    elif hasattr(store, "append_event"):
        store.append_event(event)
    else:
        raise RuntimeError("store has no enqueue_event/append_event")


def _store_log_event(store, event: dict) -> None:
    """Pollable-log write via the journal (kind='event')."""
    if hasattr(store, "journal_add"):
        store.journal_add({
            "kind": _EVENT_JOURNAL_KIND,
            "symbol": event.get("symbol"),
            "direction": event.get("direction"),
            "payload": event,
        })
    elif hasattr(store, "append_event"):
        pass  # legacy fakes: append_event already persisted it
    else:
        raise RuntimeError("store has no journal_add")


def _store_journal_event(store, event: dict) -> None:
    """Push-delivery journal write (Phase 4).

    Every published event lands in the persistent event journal with its
    unique event_id — this is what the SSE stream replays from. Stores
    without the event_journal_* surface (legacy fakes) are skipped: the
    queue + pollable log above remain the backward-compatible record.
    """
    if not hasattr(store, "event_journal_add"):
        logger.debug("store has no event_journal_add; skipping delivery journal")
        return
    store.event_journal_add(
        event_id=event["event_id"],
        event=event["event"],
        severity=event.get("severity", "INFO"),
        ts=event["ts"],
        payload=event,
    )


def _journal_store():
    """The configured store, or RuntimeError when it has no event journal."""
    store = _get_store()
    if not hasattr(store, "event_journal_add"):
        raise RuntimeError(
            "event journal unavailable: configured store has no "
            "event_journal_* methods (use storage.Store)")
    return store


def _store_read_events(store, since: Optional[str], limit: int) -> List[dict]:
    """Non-destructive pollable-log read, chronological order."""
    if hasattr(store, "journal_query"):
        filters = {"kind": _EVENT_JOURNAL_KIND}
        if since:
            filters["since"] = since
        entries = store.journal_query(limit=limit, **filters)
        events = [e["payload"] for e in entries if isinstance(e.get("payload"), dict)]
        events.reverse()  # journal_query is newest-first; poll is chronological
        return events
    if hasattr(store, "get_events"):
        return store.get_events(since=since, limit=limit)
    raise RuntimeError("store has no journal_query/get_events")


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
        # Webhook is best-effort: the durable record is local storage.
        # Never let a webhook failure break publishing.
        logger.warning("webhook delivery failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def publish(event: dict, severity: Optional[str] = None) -> dict:
    """Validate, persist (queue + pollable log + delivery journal), fan out,
    webhook-deliver.

    ``severity`` optionally overrides the mapped default (one of
    INFO/NOTICE/WARNING/CRITICAL). A unique ``event_id`` (``evt_`` + uuid4)
    is auto-assigned when the event doesn't carry one. Returns the
    normalized stored event. Raises ValueError on schema violations and
    RuntimeError if the storage backend is unavailable.

    Backward compatible: ``publish(event)`` behaves exactly as before,
    except the returned event now also carries ``event_id`` and
    ``severity``.
    """
    working = dict(event) if isinstance(event, dict) else event
    if isinstance(working, dict):
        if not working.get("event_id"):
            working["event_id"] = new_event_id()
        working["severity"] = _resolve_severity(
            working.get("event"),
            reason=working.get("reason"),
            explicit=severity if severity is not None else working.get("severity"),
        )
    normalized = validate_event(working)
    store = _get_store()
    _store_append_event(store, normalized)
    _store_log_event(store, normalized)
    _store_journal_event(store, normalized)
    _dispatch(normalized)
    _deliver_webhook(normalized)
    return normalized


def poll(since: Optional[str] = None, limit: int = 100) -> List[dict]:
    """Read the pollable event log, chronological order, capped at ``limit``.

    ``since`` is an ISO-8601 timestamp; only events logged at/after it are
    returned. Non-destructive: polling never consumes the queue.
    Raises ValueError on a malformed ``since``.
    """
    if since is not None:
        _parse_ts(since)  # validate early
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive int")
    return _store_read_events(_get_store(), since, limit or 100)


# ---------------------------------------------------------------------------
# Delivery journal reads (Phase 4): replay / resume / ack for push consumers.
# ---------------------------------------------------------------------------

def replay_after(event_id: str, limit: int = 1000) -> List[dict]:
    """Events journaled strictly after ``event_id``, chronological order.

    Used by reconnecting SSE subscribers (``resume_from``) and the
    ``--since-id`` poll fallback. Raises ValueError when ``event_id`` is
    unknown — a client holding an id the journal never saw is almost
    certainly talking to a fresh/rotated journal and must say so loudly,
    not silently miss events.
    """
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive int")
    return _journal_store().event_journal_after(event_id, limit=limit)


def latest_events(limit: int = 50) -> List[dict]:
    """Newest ``limit`` journaled events, chronological order."""
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive int")
    return _journal_store().event_journal_latest(limit=limit)


def last_event_id() -> Optional[str]:
    """Most recently journaled event_id, or None when the journal is empty."""
    return _journal_store().event_journal_last_id()


def acknowledge(event_id: str) -> bool:
    """Acknowledge receipt of ``event_id``.

    Cumulative: the named event and every event journaled at/before it
    are marked delivered. Returns True when the id was known, False when
    unknown. Ack is a delivery marker only — replays still return acked
    events, and the agent dedups on ``event_id``.
    """
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    return _journal_store().event_journal_ack(event_id) > 0


def reset_for_tests() -> None:
    """Clear subscribers and injected store. Tests only."""
    global _store_override, _cached_store
    with _sub_lock:
        _subscribers.clear()
    with _store_lock:
        _store_override = None
    _cached_store = None
    _webhook_url[0] = None
