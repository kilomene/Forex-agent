"""Hermetic unit tests for supervise_stack.py's feed market-hours gate.

The quote-feed heartbeat (NovaSignals' nova_feed.json) is written from
OnTick only, so on a closed-market weekend the feed is *necessarily* stale.
Without a market-hours gate the supervisor would raise a false HARD FAILURE
(and pointlessly GUI re-attach) on every tick all weekend -- exactly what
happened on Sat 2026-10-10. These tests pin the gate: closed market =>
heal_feed returns True without touching the feed or the GUI.
"""
import os
import sys
import time

MT5 = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MT5 not in sys.path:
    sys.path.insert(0, MT5)

import supervise_stack  # noqa: E402


def _raise(msg):
    def _f(*a, **k):
        raise AssertionError(msg)
    return _f


def test_heal_feed_market_closed_skips_check(monkeypatch):
    """Weekend: heal_feed must be True without calling feed_fresh/gui_reattach."""
    monkeypatch.setattr(supervise_stack, "_market_open", lambda: False)
    monkeypatch.setattr(
        supervise_stack.watch_mt5, "feed_fresh",
        _raise("feed_fresh must not be called when market is closed"))
    monkeypatch.setattr(
        supervise_stack.watch_mt5, "gui_reattach",
        _raise("gui_reattach must not be called when market is closed"))
    assert supervise_stack.heal_feed() is True


def test_heal_feed_market_open_fresh(monkeypatch):
    monkeypatch.setattr(supervise_stack, "_market_open", lambda: True)
    monkeypatch.setattr(supervise_stack.watch_mt5, "feed_fresh",
                        lambda: (True, "feed age 9s"))
    assert supervise_stack.heal_feed() is True


def test_heal_feed_market_open_hard_failure(monkeypatch):
    """Weekday stale feed: still walks the full re-attach path and fails."""
    monkeypatch.setattr(supervise_stack, "_market_open", lambda: True)
    monkeypatch.setattr(supervise_stack.watch_mt5, "feed_fresh",
                        lambda: (False, "feed age 999s > 180s"))
    monkeypatch.setattr(supervise_stack.watch_mt5, "gui_reattach", lambda: False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert supervise_stack.heal_feed() is False


def test_market_gate_unavailable_falls_back_to_open(monkeypatch):
    """A broken market_hours import must never mask a real weekday outage."""
    monkeypatch.setitem(sys.modules, "market_hours", None)
    assert supervise_stack._market_open() is True


def test_market_gate_matches_market_hours(monkeypatch):
    """Sanity: the gate agrees with market_hours.is_market_open() right now."""
    import market_hours
    monkeypatch.delitem(sys.modules, "market_hours", raising=False)
    # force a re-import inside _market_open via the real module
    sys.modules["market_hours"] = market_hours
    assert supervise_stack._market_open() == bool(market_hours.is_market_open())
