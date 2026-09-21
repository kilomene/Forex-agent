"""
Event shim — the local end of the event system (TARGET §4).

emit(event: dict) validates the event, then tries to forward it to the
real bus at agent.events.bus.publish (owned by the interface-builder);
when that import fails (bus not installed yet, standalone checkout), the
event is appended to a module-level in-process queue that the bus owner
drains later via drain_queue(). The build master rewires this to the
real bus when it lands.

Schema (TARGET §4): every event carries at least:
    {"event": "<dotted.name>", "ts": "<iso8601>", ...}
Known names: signal.detected, trade.requested, trade.executed,
trade.rejected, risk.blocked, broker.disconnected, kill_switch.activated,
kill_switch.deactivated, position.closed, health.check. Other dotted
names are accepted (the bus owner may add more) but must match
^[a-z0-9_]+(\\.[a-z0-9_]+)+$.
"""

import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, List

logger = logging.getLogger("events")

_EVENT_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

KNOWN_EVENTS = frozenset({
    "signal.detected",
    "trade.requested",
    "trade.executed",
    "trade.rejected",
    "risk.blocked",
    "broker.disconnected",
    "kill_switch.activated",
    "kill_switch.deactivated",
    "position.closed",
    "health.check",
})

_queue: Deque[dict] = deque(maxlen=10000)


def validate(event: dict) -> dict:
    """Validate + normalize. Raises ValueError on schema violations."""
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")
    name = event.get("event")
    if not name or not isinstance(name, str):
        raise ValueError("event['event'] is required and must be a string")
    if not _EVENT_RE.match(name):
        raise ValueError(f"event name {name!r} must be dotted lowercase (e.g. 'trade.executed')")
    if name not in KNOWN_EVENTS:
        logger.debug("Emitting unregistered event name: %s", name)
    ts = event.get("ts")
    if not ts:
        event = dict(event)
        event["ts"] = datetime.now(timezone.utc).isoformat()
    elif not isinstance(ts, str):
        raise ValueError("event['ts'] must be an ISO-8601 string")
    return event


def _forward(event: dict) -> bool:
    try:
        from agent.events.bus import publish  # owned by interface-builder
    except ImportError:
        return False
    publish(event)
    return True


def emit(event: dict) -> dict:
    """Validate and publish; falls back to the in-process queue when the
    real bus isn't importable. Returns the normalized event. Never raises
    on bus failure — producers must not break because observability is
    down (the queue preserves the events)."""
    event = validate(event)
    try:
        if _forward(event):
            return event
    except Exception:
        logger.exception("Event bus publish failed — queueing locally.")
    _queue.append(event)
    return event


def drain_queue(limit: int = 1000) -> List[dict]:
    """Pop up to `limit` queued events (for the bus owner to forward)."""
    out = []
    while _queue and len(out) < limit:
        out.append(_queue.popleft())
    return out


def queued_count() -> int:
    return len(_queue)
