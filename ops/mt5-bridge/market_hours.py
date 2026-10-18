#!/usr/bin/env python3
"""
Nova market-hours gate for the 30-day demo trading experiment.

FX weekend (server time, GMT+3 / EEST for the whole experiment period):
  - Friday 22:00 GMT close  ->  Saturday 01:00 server
  - Sunday 22:00 GMT open   ->  Monday 01:00 server

The experiment rule is conservative: the market is treated as CLOSED for
ALL of Saturday (server) — even though the first hour of Saturday server
time is technically still Friday evening GMT — plus Sunday before 01:00
(server). New positions are only ever blocked by this gate, never
existing ones, so the conservative reading costs nothing and matches the
charter ("Saturday all day" closed).

Existing positions are always held; only NEW positions are blocked when
the market is closed (journaled as skipped:market_closed).

Session buckets from the server hour (for the learning loop):
  asia     00:00-07:59
  london   08:00-12:59
  newyork  13:00-20:59
  late     21:00-23:59
"""

from datetime import datetime

# Server timezone is GMT+3 (EEST) for the whole experiment period.
SERVER_UTC_OFFSET_H = 3

# Closed window: (weekday, start_hour) .. (weekday, end_hour), server time.
# Monday=0 ... Sunday=6. All of Saturday is closed (conservative rule).
# Racheal engagement #3 (2026-10-09): FIXED — CLOSED_TO was (6, 1) Sunday
# 01:00, but FX opens Sunday 22:00 GMT = Monday 01:00 server. The old code
# reported the market OPEN for all of Sunday, 24h early. Correct close:
# Saturday 00:00 server through Monday 01:00 server.
CLOSED_FROM = (5, 0)   # Saturday 00:00
CLOSED_TO = (0, 1)     # Monday   01:00


def session_bucket(server_hour):
    """Map a server hour (0-23) to a session name."""
    h = int(server_hour)
    if 0 <= h < 8:
        return "asia"
    if 8 <= h < 13:
        return "london"
    if 13 <= h < 21:
        return "newyork"
    return "late"


def is_market_open(server_dt=None):
    """
    True if the FX market is open at the given server datetime.
    server_dt: datetime in server time (GMT+3). Defaults to now in GMT+3.
    """
    if server_dt is None:
        from datetime import timezone, timedelta
        server_dt = datetime.now(timezone.utc) + timedelta(
            hours=SERVER_UTC_OFFSET_H)
    wd = server_dt.weekday()          # Monday=0
    hh = server_dt.hour + server_dt.minute / 60.0
    # Closed: all of Saturday (wd=5), all of Sunday (wd=6), and Monday
    # before 01:00 (wd=0, hh<1). Racheal engagement #3: the old code only
    # closed Sunday before 01:00, wrongly reporting OPEN for the rest of
    # Sunday when FX is actually closed until Sunday 22:00 GMT.
    if wd == 5:
        return False
    if wd == 6:
        return False
    if wd == 0 and hh < CLOSED_TO[1]:
        return False
    return True


def market_context(server_dt=None):
    """Context dict for journal entries at decision time."""
    from datetime import timezone, timedelta
    if server_dt is None:
        server_dt = datetime.now(timezone.utc) + timedelta(
            hours=SERVER_UTC_OFFSET_H)
    return {
        "server_time": server_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "server_hour": server_dt.hour,
        "weekday": server_dt.strftime("%a"),
        "session": session_bucket(server_dt.hour),
        "market_open": is_market_open(server_dt),
    }
