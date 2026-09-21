"""Economic calendar — provider INTERFACE only.

There is no calendar data source in this subsystem, and there must never be
fabricated events. `get_calendar()` honestly reports
`{"available": False, "reason": "no provider configured"}` until a real
provider is wired.

To wire a real source later: implement `CalendarProvider` and pass an
instance to `get_calendar(..., provider=...)`. The interface mirrors the
contract the Worker's `get_economic_calendar` tool documented:

    GET {NEWS_CALENDAR_URL}?symbol=<symbol>&hours_ahead=<n>
    Headers: Authorization: Bearer <NEWS_CALENDAR_API_KEY> (if set)
    Response: {"events": [{"time": ..., "currency": ..., "impact": "high"|"medium"|"low",
                           "title": ...}]}

Secrets (NEWS_CALENDAR_URL / NEWS_CALENDAR_API_KEY) come from env or the
0600 secrets file — never hardcoded, never logged.
"""

import abc
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class CalendarEvent:
    """One real economic-calendar event, from a real provider."""
    time: str      # ISO-8601
    currency: str  # e.g. "USD"
    impact: str    # "high" | "medium" | "low"
    title: str
    source: str = ""  # provider name, for provenance


class CalendarProvider(abc.ABC):
    """Interface a real calendar source implements. Never returns invented events."""

    name: str = "base"

    @abc.abstractmethod
    def fetch(self, from_: datetime, to: datetime) -> list:
        """Return [CalendarEvent, ...] in [from_, to]. Raise on provider failure."""
        raise NotImplementedError


def get_calendar(symbol=None, hours_ahead: int = 24, provider: CalendarProvider = None) -> dict:
    """Upcoming high-impact events for a symbol's currencies — honestly.

    With no provider configured (the current state), returns
    {"available": False, "reason": "no provider configured"}.
    With a provider, returns {"available": True, "events": [...], "source": name}.
    Provider failures degrade to available=False with the reason — never to
    invented events.
    """
    if provider is None:
        return {"available": False, "reason": "no provider configured"}
    now = datetime.now(timezone.utc)
    try:
        events = provider.fetch(now, now + timedelta(hours=hours_ahead))
    except Exception as exc:  # noqa: BLE001 — degrade honestly, never fabricate
        return {"available": False, "reason": f"calendar provider error: {type(exc).__name__}"}
    if symbol:
        wanted = {c for c in _symbol_currencies(symbol)}
        events = [e for e in events if e.currency in wanted]
    return {
        "available": True,
        "source": provider.name,
        "events": [e.__dict__ for e in events],
    }


def _symbol_currencies(symbol: str) -> list:
    """Best-effort currency extraction for 6-letter pairs (EURUSD -> EUR, USD)."""
    symbol = (symbol or "").upper()
    if len(symbol) == 6:
        return [symbol[:3], symbol[3:]]
    return []
