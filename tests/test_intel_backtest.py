"""Tests for backtesting/: the real labeler module, the closed-candle fix,
adapter-driven data fetch, and structural live-order separation."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backtesting import (
    BacktestError,
    extract_features,
    fetch_historical_candles,
    label_outcome,
    run_backtest,
)
from backtesting.engine import _ReadOnlyAdapter
from broker import BrokerError, BROKER_UNAVAILABLE
from core.market import Candle
from intelligence.ml import FEATURE_COLUMNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mk_candle(i, o, h, l, c, base=datetime(2026, 1, 1)):
    return Candle(time=base + timedelta(hours=i), open=o, high=h, low=l, close=c, volume=100)


def flat_series(n, price=1.0800, base=datetime(2026, 1, 1)):
    return [mk_candle(i, price, price, price, price, base) for i in range(n)]


def spike(i, base=datetime(2026, 1, 1)):
    # Bullish spike vs the flat series: would trigger the test strategy
    # if (and only if) it is evaluated as a CLOSED candle.
    return mk_candle(i, 1.0790, 1.0860, 1.0785, 1.0850, base)


class FakeAdapter:
    """Duck-typed BrokerAdapter with recording order methods."""

    def __init__(self, candles):
        self._candles = candles
        self.order_calls = []

    def candles(self, symbol, timeframe, count=200):
        return list(self._candles[-count:])

    def health(self):
        return SimpleNamespace(connected=True)

    def submit_order(self, req):
        self.order_calls.append(("submit", req))
        return {"ticket": 1}

    def modify_order(self, ticket, sl, tp):
        self.order_calls.append(("modify", ticket))

    def close_position(self, ticket):
        self.order_calls.append(("close", ticket))


class ContractStrategy:
    """Mimics the core.strategies.Strategy contract: the LAST element of the
    window is treated as forming and excluded from signal logic."""

    name = "test_cross"

    def __init__(self):
        self.seen_windows = []

    def evaluate(self, symbol, candles):
        self.seen_windows.append(candles)
        closed = candles[:-1]  # THE contract: forming candle excluded
        if len(closed) < 2:
            return None
        prev, last = closed[-2], closed[-1]
        if prev.close <= prev.open and last.close > last.open and last.close > prev.high:
            return SimpleNamespace(
                symbol=symbol, direction="BUY", entry_price=last.close,
                stop_loss=last.low - 0.0010, take_profit=last.high + 0.0020,
                candle_time=last.time, ema_fast=1.1, ema_slow=1.0,
                rsi_value=55.0, atr_value=0.0010,
                smc_summary={"available": True,
                             "market_structure": {"trend": "uptrend"}},
                trigger="test-cross", strategy="test_cross",
            )
        return None


# ---------------------------------------------------------------------------
# 1. Labeler — the REAL module (original test_backtest.py tested a copy)
# ---------------------------------------------------------------------------

def test_label_buy_clean_tp_hit():
    candles = [mk_candle(0, 1.0800, 1.0810, 1.0790, 1.0800),
               mk_candle(1, 1.0800, 1.0820, 1.0795, 1.0815),
               mk_candle(2, 1.0815, 1.0860, 1.0810, 1.0850)]
    assert label_outcome(candles, 0, "BUY", 1.0750, 1.0850) == "win"


def test_label_buy_clean_sl_hit():
    candles = [mk_candle(0, 1.0800, 1.0810, 1.0790, 1.0800),
               mk_candle(1, 1.0800, 1.0805, 1.0740, 1.0745)]
    assert label_outcome(candles, 0, "BUY", 1.0750, 1.0900) == "loss"


def test_label_sell_mirror():
    candles = [mk_candle(0, 1.0800, 1.0810, 1.0790, 1.0800),
               mk_candle(1, 1.0800, 1.0805, 1.0740, 1.0745)]
    assert label_outcome(candles, 0, "SELL", 1.0850, 1.0750) == "win"


def test_label_ambiguous_candle_conservative_tiebreak():
    candles = [mk_candle(0, 1.0800, 1.0810, 1.0790, 1.0800),
               mk_candle(1, 1.0800, 1.0900, 1.0700, 1.0800)]
    assert label_outcome(candles, 0, "BUY", 1.0750, 1.0850) == "loss"


def test_label_timeout():
    candles = [mk_candle(0, 1.08, 1.081, 1.079, 1.080)]
    candles += [mk_candle(i, 1.080, 1.082, 1.078, 1.080) for i in range(1, 10)]
    assert label_outcome(candles, 0, "BUY", 1.0750, 1.0900, max_lookahead=5) == "timeout"


def test_label_no_lookahead_on_entry_candle():
    candles = [mk_candle(0, 1.0800, 1.0900, 1.0700, 1.0800),
               mk_candle(1, 1.0800, 1.0810, 1.0790, 1.0805)]
    assert label_outcome(candles, 0, "BUY", 1.0750, 1.0850, max_lookahead=1) == "timeout"


# ---------------------------------------------------------------------------
# 2. Closed-candle fix: forming candle must not fake a signal
# ---------------------------------------------------------------------------

def test_forming_candle_cannot_fake_a_signal():
    candles = flat_series(69) + [spike(69)]  # spike ONLY on the final ("forming") candle
    adapter = FakeAdapter(candles)
    strategy = ContractStrategy()
    rows = run_backtest(adapter, strategy, ["EURUSD"],
                        start=candles[0].time, end=candles[-1].time,
                        min_history=60)
    assert rows == [], f"forming-candle spike must not emit a signal, got {rows}"
    # And the engine handed the strategy full windows (no pre-truncation):
    # every window ends with the newest candle — live parity.
    assert [w[-1].time for w in strategy.seen_windows] == \
        [candles[i].time for i in range(60, 70)]


def test_closed_candle_trigger_fires_with_right_timestamp():
    candles = flat_series(70)
    candles[68] = spike(68)  # trigger on a CLOSED candle at the final step
    adapter = FakeAdapter(candles)
    strategy = ContractStrategy()
    rows = run_backtest(adapter, strategy, ["EURUSD"],
                        start=candles[0].time, end=candles[-1].time,
                        min_history=60)
    assert len(rows) == 1
    row = rows[0]
    assert row["direction"] == "BUY"
    assert row["candle_time"] == candles[68].time.isoformat()
    assert row["strategy"] == "test_cross"
    assert list(row)[4:12] == FEATURE_COLUMNS  # schema columns present, in order


# ---------------------------------------------------------------------------
# 3. Structural separation: a backtest cannot place a live order
# ---------------------------------------------------------------------------

def test_backtest_never_calls_order_methods():
    candles = flat_series(68) + [spike(68)] + [mk_candle(69, 1.08, 1.0805, 1.0795, 1.08)]
    adapter = FakeAdapter(candles)
    run_backtest(adapter, ContractStrategy(), ["EURUSD"],
                 start=candles[0].time, end=candles[-1].time, min_history=60)
    assert adapter.order_calls == [], "backtest must never touch order methods"


def test_read_only_wrapper_blocks_orders_structurally():
    adapter = FakeAdapter(flat_series(5))
    ro = _ReadOnlyAdapter(adapter)
    assert ro.candles("EURUSD", "H1") == adapter.candles("EURUSD", "H1")  # reads pass through
    for meth, args in [("submit_order", ({"symbol": "EURUSD"},)),
                       ("modify_order", (1, 1.0, 2.0)),
                       ("close_position", (1,))]:
        with pytest.raises(BacktestError) as exc_info:
            getattr(ro, meth)(*args)
        assert exc_info.value.code == "ORDER_BLOCKED"


# ---------------------------------------------------------------------------
# 4. Data fetch goes through the adapter (never MetaTrader5 directly)
# ---------------------------------------------------------------------------

def test_fetch_filters_range_and_detects_short_history():
    candles = [mk_candle(i, 1.08, 1.081, 1.079, 1.08,
                         base=datetime(2026, 2, 1)) for i in range(100)]
    adapter = FakeAdapter(candles)
    got = fetch_historical_candles(adapter, "EURUSD", "H1",
                                  start=datetime(2026, 2, 1),
                                  end=datetime(2026, 2, 1) + timedelta(hours=9))
    assert len(got) == 10
    assert got[0].time == datetime(2026, 2, 1)

    with pytest.raises(BacktestError) as exc_info:
        fetch_historical_candles(adapter, "EURUSD", "H1",
                                 start=datetime(2026, 1, 1),  # older than history
                                 end=datetime(2026, 2, 2))
    assert exc_info.value.code == "INSUFFICIENT_HISTORY"


def test_fetch_propagates_broker_unavailable():
    class Down:
        def candles(self, symbol, timeframe, count=200):
            raise BrokerError(BROKER_UNAVAILABLE, "down")

    with pytest.raises(BrokerError) as exc_info:
        fetch_historical_candles(Down(), "EURUSD", "H1",
                                 start=datetime(2026, 1, 1),
                                 end=datetime(2026, 2, 1))
    assert exc_info.value.code == BROKER_UNAVAILABLE


# ---------------------------------------------------------------------------
# 5. Feature extraction matches the canonical schema
# ---------------------------------------------------------------------------

def test_extract_features_schema_and_math():
    signal = SimpleNamespace(
        ema_fast=1.1, ema_slow=1.0, rsi_value=55.0, atr_value=0.0010,
        entry_price=1.0850,
        smc_summary={"available": True, "market_structure": {"trend": "uptrend"}},
    )
    feats = extract_features(signal)
    assert list(feats) == FEATURE_COLUMNS
    assert abs(feats["ema_spread_pct"] - (0.1 / 1.0850)) < 1e-12
    assert feats["smc_trend_uptrend"] == 1
    assert feats["smc_trend_ranging"] == 0


def test_run_backtest_rejects_empty_symbols():
    with pytest.raises(BacktestError) as exc_info:
        run_backtest(FakeAdapter([]), ContractStrategy(), [],
                     start=datetime(2026, 1, 1), end=datetime(2026, 2, 1))
    assert exc_info.value.code == "CONFIG_INVALID"
