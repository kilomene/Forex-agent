"""Signal dataclass / lifecycle / dedup tests."""

from datetime import datetime

from core.signals import Signal, SignalDedup, SignalStatus


def _signal(**kw):
    base = dict(
        symbol="EURUSD", timeframe="H1", direction="BUY",
        entry_price=1.1000, stop_loss=1.0950, take_profit=1.1100,
        ema_fast=1.1001, ema_slow=1.0999, rsi_value=55.0, atr_value=0.005,
        candle_time=datetime(2026, 1, 1, 12, 0, 0),
        trigger="EMA5 crossed above EMA10, RSI=55.0",
    )
    base.update(kw)
    return Signal(**base)


def test_signal_defaults():
    s = _signal()
    assert s.status == SignalStatus.DETECTED
    assert s.strategy == "ema_rsi"
    assert s.created_at is not None


def test_signal_round_trip():
    s = _signal(id="sig-1", status=SignalStatus.APPROVED)
    d = s.to_dict()
    s2 = Signal.from_dict(d)
    assert s2.id == "sig-1"
    assert s2.status == SignalStatus.APPROVED
    assert s2.candle_time == s.candle_time
    assert s2.entry_price == s.entry_price


def test_signal_from_dict_tolerates_extra_keys():
    d = _signal().to_dict()
    d["worker_extra_field"] = "ignored"
    s = Signal.from_dict(d)
    assert s.symbol == "EURUSD"


def test_dedup_marks_and_detects():
    dd = SignalDedup()
    t = datetime(2026, 1, 1, 12, 0, 0)
    assert dd.is_duplicate("EURUSD", t) is False
    assert dd.check_and_mark("EURUSD", t) is False  # first sighting
    assert dd.check_and_mark("EURUSD", t) is True   # duplicate
    assert dd.is_duplicate("EURUSD", t) is True


def test_dedup_is_per_symbol():
    dd = SignalDedup()
    t = datetime(2026, 1, 1, 12, 0, 0)
    dd.mark("EURUSD", t)
    assert dd.is_duplicate("GBPUSD", t) is False  # same candle time, other symbol


def test_dedup_reset():
    dd = SignalDedup()
    t = datetime(2026, 1, 1, 12, 0, 0)
    dd.mark("EURUSD", t)
    dd.reset("EURUSD")
    assert dd.is_duplicate("EURUSD", t) is False
    dd.mark("EURUSD", t)
    dd.reset()
    assert dd.is_duplicate("EURUSD", t) is False
