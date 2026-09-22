"""Economic calendar: provider interface only — no fabricated events, ever."""

from .provider import CalendarEvent, CalendarProvider, get_calendar

__all__ = ["CalendarEvent", "CalendarProvider", "get_calendar"]
