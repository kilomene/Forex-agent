"""Hermetic unit tests for market_hours.py (no MT5, no network)."""
import os
import sys
from datetime import datetime

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import market_hours as mh  # noqa: E402


def dt(weekday, hour, minute=0):
    """Server-time datetime on the week of 2026-09-21 (Mon)."""
    # 2026-09-21 is a Monday.
    base = datetime(2026, 9, 21, 12, 0, 0)
    return base.replace(day=base.day + weekday, hour=hour, minute=minute)


def test_weekday_open():
    for wd in (0, 1, 2, 3, 4):  # Mon-Fri
        assert mh.is_market_open(dt(wd, 12)) is True


def test_friday_late_open_saturday_early_closed():
    # Friday 23:59 server -> market still open (close is Sat 01:00 server)
    assert mh.is_market_open(dt(4, 23, 59)) is True
    # Saturday 01:00 server -> closed
    assert mh.is_market_open(dt(5, 1, 0)) is False
    # Saturday midday -> closed
    assert mh.is_market_open(dt(5, 12, 0)) is False
    # Saturday 23:59 -> closed
    assert mh.is_market_open(dt(5, 23, 59)) is False


def test_sunday_before_01_closed_after_open():
    assert mh.is_market_open(dt(6, 0, 30)) is False
    assert mh.is_market_open(dt(6, 1, 0)) is True
    assert mh.is_market_open(dt(6, 12, 0)) is True


def test_saturday_early_morning_boundary():
    # Saturday 00:59 server -> closed (all of Saturday is closed,
    # conservative experiment rule)
    assert mh.is_market_open(dt(5, 0, 59)) is False


def test_saturday_all_day_closed():
    # Every hour of Saturday server time is closed.
    for h in (0, 3, 6, 9, 12, 15, 18, 21, 23):
        assert mh.is_market_open(dt(5, h, 30)) is False


def test_session_buckets():
    assert mh.session_bucket(0) == "asia"
    assert mh.session_bucket(7) == "asia"
    assert mh.session_bucket(8) == "london"
    assert mh.session_bucket(12) == "london"
    assert mh.session_bucket(13) == "newyork"
    assert mh.session_bucket(20) == "newyork"
    assert mh.session_bucket(21) == "late"
    assert mh.session_bucket(23) == "late"


def test_market_context_fields():
    ctx = mh.market_context(dt(0, 15, 30))  # Monday 15:30 server
    assert ctx["server_hour"] == 15
    assert ctx["weekday"] == "Mon"
    assert ctx["session"] == "newyork"
    assert ctx["market_open"] is True


def test_market_context_closed_weekend():
    ctx = mh.market_context(dt(5, 10, 0))  # Saturday 10:00 server
    assert ctx["weekday"] == "Sat"
    assert ctx["market_open"] is False
