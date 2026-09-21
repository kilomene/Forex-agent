"""Tests for intelligence.economic_calendar: honest interface, no fabricated events."""

from datetime import datetime, timezone

from intelligence.economic_calendar import (
    CalendarEvent,
    CalendarProvider,
    get_calendar,
)


def test_no_provider_honestly_unavailable():
    result = get_calendar(symbol="EURUSD")
    assert result == {"available": False, "reason": "no provider configured"}


def test_provider_interface_is_abstract():
    try:
        CalendarProvider()
    except TypeError:
        pass
    else:
        raise AssertionError("CalendarProvider must be abstract")


class FakeProvider(CalendarProvider):
    name = "fake"

    def fetch(self, from_, to):
        assert from_ < to
        return [
            CalendarEvent(time="2026-09-22T12:30:00+00:00", currency="USD",
                          impact="high", title="Fake CPI", source="fake"),
            CalendarEvent(time="2026-09-22T13:00:00+00:00", currency="JPY",
                          impact="low", title="Fake JPY item", source="fake"),
        ]


def test_provider_events_returned_and_filtered():
    result = get_calendar(symbol="EURUSD", hours_ahead=24, provider=FakeProvider())
    assert result["available"] is True
    assert result["source"] == "fake"
    # EURUSD -> EUR, USD: only the USD event survives.
    assert [e["currency"] for e in result["events"]] == ["USD"]
    assert result["events"][0]["title"] == "Fake CPI"


def test_provider_failure_degrades_honestly():
    class Broken(CalendarProvider):
        name = "broken"

        def fetch(self, from_, to):
            raise RuntimeError("boom")

    result = get_calendar(provider=Broken())
    assert result["available"] is False
    assert "provider error" in result["reason"]
    assert "events" not in result  # never fabricate events on failure
