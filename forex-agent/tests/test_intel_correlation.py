"""Tests for intelligence.correlation (port of knowledge.js)."""

from datetime import datetime, timezone

from intelligence.correlation import (
    check_correlated_exposure,
    correlated_pairs,
    current_session_info,
)


def test_stacked_positive_same_direction_flagged():
    signal = {"symbol": "EURUSD", "direction": "BUY"}
    positions = [{"symbol": "GBPUSD", "direction": "BUY", "status": "open"}]
    flags = check_correlated_exposure(signal, positions)
    assert len(flags) == 1
    assert flags[0] == (
        "Correlated exposure: open BUY GBPUSD position is positively "
        "correlated with this BUY EURUSD signal — this may be effectively "
        "doubling the same directional bet rather than diversifying."
    )


def test_stacked_negative_opposite_direction_flagged():
    signal = {"symbol": "EURUSD", "direction": "BUY"}
    positions = [{"symbol": "USDJPY", "direction": "SELL", "status": "open"}]
    flags = check_correlated_exposure(signal, positions)
    assert len(flags) == 1
    assert "negatively correlated" in flags[0]


def test_positive_opposite_direction_not_flagged():
    signal = {"symbol": "EURUSD", "direction": "BUY"}
    positions = [{"symbol": "GBPUSD", "direction": "SELL", "status": "open"}]
    assert check_correlated_exposure(signal, positions) == []


def test_negative_same_direction_not_flagged():
    signal = {"symbol": "EURUSD", "direction": "BUY"}
    positions = [{"symbol": "USDJPY", "direction": "BUY", "status": "open"}]
    assert check_correlated_exposure(signal, positions) == []


def test_same_symbol_skipped():
    signal = {"symbol": "EURUSD", "direction": "BUY"}
    positions = [{"symbol": "EURUSD", "direction": "BUY", "status": "open"}]
    assert check_correlated_exposure(signal, positions) == []


def test_unknown_symbol_no_relations():
    assert correlated_pairs("NOPEUSD") == {}
    signal = {"symbol": "EURUSD", "direction": "BUY"}
    positions = [{"symbol": "NOPEUSD", "direction": "BUY", "status": "open"}]
    assert check_correlated_exposure(signal, positions) == []


def test_closed_positions_not_double_counted():
    # The original memory.js treated every "executed" signal as open forever.
    # Here only broker-reported open positions count; explicitly closed ones
    # are skipped even if passed in.
    signal = {"symbol": "EURUSD", "direction": "BUY"}
    positions = [
        {"symbol": "GBPUSD", "direction": "BUY", "status": "closed"},
        {"symbol": "AUDUSD", "direction": "BUY", "status": "CLOSED"},
    ]
    assert check_correlated_exposure(signal, positions) == []


def test_session_info_known_windows():
    # 14:00 UTC -> london + new_york active, overlap flagged.
    info = current_session_info(datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc))
    assert info["utc_hour"] == 14
    assert set(info["active_sessions"]) == {"london", "new_york"}
    assert info["overlaps"] == ["london/new_york"]
    assert "overlap" in info["liquidity_note"]

    # 03:00 UTC -> only tokyo.
    info = current_session_info(datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc))
    assert info["active_sessions"] == ["tokyo"]
    assert info["overlaps"] == []
