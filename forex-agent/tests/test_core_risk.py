"""
Risk engine tests: the three hole fixes (equity daily-loss, volume
steps, foreign-position filtering), persisted state, and the
correlation rule. Uses a fake in-memory store per the storage contract
and a configurable fake broker. No MetaTrader5 anywhere.
"""

from datetime import date, datetime

import pytest

from broker import (
    AccountInfo,
    BrokerError,
    OrderRequest,
    Position,
    SymbolSpec,
    DAILY_LOSS_LIMIT,
    INVALID_ORDER,
    MAX_EXPOSURE,
    RISK_LIMIT_EXCEEDED,
)
from config import RiskConfig
from core.risk import (
    KILL_SWITCH_ENGAGED,
    RiskManager,
    calculate_position_size,
    clamp_volume,
)
from core.signals import Signal


class FakeStore:
    """In-memory stand-in for the storage contract."""

    def __init__(self):
        self._state = {}

    def get_risk_state(self):
        return dict(self._state)

    def set_risk_state(self, patch):
        self._state.update(patch)


def _spec(step=0.01, vmin=0.01, vmax=100.0):
    return SymbolSpec(name="EURUSD", volume_min=vmin, volume_max=vmax,
                      volume_step=step, tick_value=1.0, tick_size=0.00001,
                      contract_size=100000.0, digits=5, point=0.00001)


class FakeBroker:
    def __init__(self, balance=10000.0, equity=10000.0,
                 positions=None, spec=None):
        self._balance = balance
        self._equity = equity
        self._positions = positions or []
        self._spec = spec or _spec()

    def account_info(self):
        return AccountInfo(balance=self._balance, equity=self._equity,
                           currency="USD")

    def positions(self):
        return list(self._positions)

    def symbols(self, names=None):
        return [self._spec]


def _pos(ticket, symbol="EURUSD", direction="BUY", magic=20260817, volume=0.10):
    return Position(ticket=ticket, position_id=ticket, symbol=symbol,
                    direction=direction, volume=volume, price_open=1.10,
                    price_current=1.10, sl=1.09, tp=1.11, profit=0.0,
                    swap=0.0, magic=magic, comment=f"approved:sig-{ticket}",
                    time_open=datetime.now())


def _signal(**kw):
    base = dict(symbol="EURUSD", timeframe="H1", direction="BUY",
                entry_price=1.1000, stop_loss=1.0950, take_profit=1.1100,
                ema_fast=1.1, ema_slow=1.09, rsi_value=55.0, atr_value=0.005,
                candle_time=datetime(2026, 1, 1, 12), trigger="test")
    base.update(kw)
    return Signal(**base)


def _risk(**kw):
    base = dict(fixed_lot_size=0.10, use_percent_risk_sizing=False,
                max_risk_per_trade=0.01, max_daily_loss=0.03,
                max_open_positions=3, max_total_exposure_lots=1.0,
                max_consecutive_losses=4, max_correlated_positions=1,
                require_stop_loss=True)
    base.update(kw)
    return RiskConfig(**base)


def _seed_day(store, equity):
    store.set_risk_state({"day": date.today().isoformat(),
                          "start_of_day_equity": equity,
                          "recent_outcomes": []})


# --- hole (a): daily loss on EQUITY -----------------------------------------

def test_daily_loss_uses_equity_not_balance():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(), store=store)
    # Balance is flat — the old code would read 0% loss. Equity is down 4%.
    broker = FakeBroker(balance=10000.0, equity=9600.0)
    res = rm.check(_signal(), broker)
    assert res.allowed is False
    assert res.reason_code == DAILY_LOSS_LIMIT
    assert "4.00%" in res.reason


def test_small_equity_drawdown_allowed():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(), store=store)
    broker = FakeBroker(balance=10000.0, equity=9900.0)  # 1% < 3% limit
    res = rm.check(_signal(), broker)
    assert res.allowed is True


# --- hole (b): volume min/max/step -------------------------------------------

def test_volume_floored_to_step():
    v = clamp_volume(0.14, _spec(step=0.1, vmin=0.1))
    assert v == pytest.approx(0.1)  # floored, never rounded up past risk size


def test_volume_below_minimum_rejected():
    with pytest.raises(BrokerError) as e:
        clamp_volume(0.05, _spec(step=0.1, vmin=0.1))
    assert e.value.code == INVALID_ORDER


def test_risk_check_rejects_unclampable_volume():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(fixed_lot_size=0.05), store=store)
    broker = FakeBroker(spec=_spec(step=0.1, vmin=0.1))
    res = rm.check(_signal(), broker)
    assert res.allowed is False
    assert res.reason_code == INVALID_ORDER


def test_risk_check_clamps_volume_to_step():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(fixed_lot_size=0.14), store=store)
    broker = FakeBroker(spec=_spec(step=0.1, vmin=0.1))
    res = rm.check(_signal(), broker)
    assert res.allowed is True
    assert res.lot_size == pytest.approx(0.1)


# --- hole (c): foreign positions excluded --------------------------------------

def test_foreign_positions_do_not_consume_quota():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(max_open_positions=3), store=store)
    foreign = [_pos(i, magic=999999) for i in range(3)]  # manual/other-bot
    broker = FakeBroker(positions=foreign)
    res = rm.check(_signal(), broker)
    assert res.allowed is True  # quota is untouched by foreign positions


def test_own_positions_consume_quota():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(max_open_positions=3), store=store)
    own = [_pos(i) for i in range(3)]
    broker = FakeBroker(positions=own)
    res = rm.check(_signal(), broker)
    assert res.allowed is False
    assert res.reason_code == RISK_LIMIT_EXCEEDED


# --- correlation rule ------------------------------------------------------------

def test_correlated_exposure_flagged_and_blocked():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(max_correlated_positions=1,
                           max_total_exposure_lots=10.0), store=store)
    broker = FakeBroker(positions=[_pos(1, symbol="GBPUSD", direction="BUY")])
    res = rm.check(_signal(symbol="EURUSD", direction="BUY"), broker)
    assert res.allowed is False
    assert res.reason_code == MAX_EXPOSURE
    assert any("Correlated exposure" in n for n in res.notes)
    assert any("GBPUSD" in n and "positively correlated" in n for n in res.notes)


def test_correlated_exposure_allowed_when_under_limit():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(max_correlated_positions=2,
                           max_total_exposure_lots=10.0), store=store)
    broker = FakeBroker(positions=[_pos(1, symbol="GBPUSD", direction="BUY")])
    res = rm.check(_signal(symbol="EURUSD", direction="BUY"), broker)
    assert res.allowed is True
    assert any("Correlated exposure" in n for n in res.notes)  # still flagged as ground truth


def test_uncorrelated_or_opposite_direction_not_flagged():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(max_total_exposure_lots=10.0), store=store)
    # USDJPY is negatively correlated with EURUSD: opposite direction = stacked;
    # same direction here must NOT flag.
    broker = FakeBroker(positions=[_pos(1, symbol="USDJPY", direction="BUY")])
    res = rm.check(_signal(symbol="EURUSD", direction="BUY"), broker)
    assert res.allowed is True
    assert res.notes == []


# --- kill switch / SL / streak ----------------------------------------------------

def test_kill_switch_blocks():
    store = FakeStore()
    rm = RiskManager(_risk(), store=store)
    res = rm.check(_signal(), FakeBroker(), kill_switch_engaged=True)
    assert res.allowed is False
    assert res.reason_code == KILL_SWITCH_ENGAGED


def test_missing_sl_rejected():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(), store=store)
    sig = _signal()
    sig.stop_loss = None
    res = rm.check(sig, FakeBroker())
    assert res.allowed is False
    assert res.reason_code == INVALID_ORDER


def test_consecutive_loss_breaker_persists_across_restart():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm1 = RiskManager(_risk(max_consecutive_losses=4), store=store)
    rm1.sync_outcomes([True, False, False, False, False])  # 4 straight losses
    # "restart": a brand-new manager on the SAME store
    rm2 = RiskManager(_risk(max_consecutive_losses=4), store=store)
    res = rm2.check(_signal(), FakeBroker())
    assert res.allowed is False
    assert res.reason_code == RISK_LIMIT_EXCEEDED
    assert "Circuit breaker" in res.reason


def test_winning_streak_resets_breaker():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(max_consecutive_losses=4), store=store)
    rm.sync_outcomes([False, False, True])
    res = rm.check(_signal(), FakeBroker())
    assert res.allowed is True


# --- sizing math --------------------------------------------------------------------

def test_calculate_position_size_hand_checked():
    # tick 0.00001, $1/tick/lot; stop 0.0050 -> 500 ticks -> $500/lot risk.
    # $10k equity, 1% = $100 -> 0.20 lots.
    specs = {"tick_value": 1.0, "tick_size": 0.00001}
    v = calculate_position_size(10000.0, 1.0, 1.1000, 1.0950, "EURUSD", specs)
    assert v == pytest.approx(0.20)


def test_percent_sizing_path_in_check():
    store = FakeStore()
    _seed_day(store, 10000.0)
    rm = RiskManager(_risk(use_percent_risk_sizing=True, max_risk_per_trade=0.01,
                           max_total_exposure_lots=10.0), store=store)
    res = rm.check(_signal(), FakeBroker())
    assert res.allowed is True
    assert res.lot_size == pytest.approx(0.20)
