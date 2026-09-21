"""Walk-forward backtest engine — FULLY SEPARATED from live execution.

Separation guarantees (structural, not just conventional):
  1. `run_backtest` wraps whatever adapter it receives in `_ReadOnlyAdapter`.
     Only read paths (candles/health/symbols/positions/...) are forwarded;
     `submit_order` / `modify_order` / `close_position` raise
     BacktestError(ORDER_BLOCKED). A backtest run is structurally incapable
     of placing a live order — even if handed the live MT5Adapter.
  2. This module never imports MetaTrader5, never touches the execution
     gateway, and asserts nothing about dry-run: it cannot trade, period.
  3. Data flows one way: adapter -> strategy -> labeler -> rows.

CLOSED-CANDLE FIX (the repaint bug, fixed at the seam):
  The walk passes `window = candles[:i+1]` to `strategy.evaluate()` — the
  same shape the live path hands the strategy. Per the Strategy contract
  (core/strategies), the LAST element of that window is treated as forming
  and excluded from signal logic, so signals are evaluated on closed
  candles only — exactly like live. The backtest must NOT pre-truncate the
  window: doing so would evaluate one candle deeper than live and silently
  diverge from production behavior. The signal's `candle_time` (the closed
  candle it fired on) anchors the outcome labeling.

Strategy interface: anything with `.evaluate(symbol, candles) -> Signal|None`
(core.strategies.Strategy), or a plain callable `(symbol, candles) -> signal`.
The signal is duck-typed: direction, entry_price, stop_loss, take_profit,
candle_time, ema_fast/slow, rsi_value, atr_value, smc_summary.
"""

import argparse
import csv
import logging
from datetime import datetime

from .data import fetch_historical_candles
from .errors import CONFIG_INVALID, ORDER_BLOCKED, SIGNAL_CANDLE_NOT_FOUND, BacktestError
from .labeler import label_outcome

logger = logging.getLogger("backtesting")


# ---------------------------------------------------------------------------
# Structural separation: the read-only adapter wrapper
# ---------------------------------------------------------------------------

_BLOCKED_METHODS = ("submit_order", "modify_order", "close_position")


class _ReadOnlyAdapter:
    """Wraps a BrokerAdapter so a backtest cannot place orders, structurally.

    Every attribute except the order-writing methods is forwarded to the
    wrapped adapter. The three order methods raise BacktestError(ORDER_BLOCKED)
    instead of reaching the broker.
    """

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        if name in _BLOCKED_METHODS:
            def _blocked(*args, **kwargs):
                raise BacktestError(
                    ORDER_BLOCKED,
                    f"backtesting is structurally incapable of placing orders; "
                    f"blocked call to {name}",
                )

            return _blocked
        return getattr(object.__getattribute__(self, "_inner"), name)


# ---------------------------------------------------------------------------
# Feature extraction (canonical 8-feature schema)
# ---------------------------------------------------------------------------

def extract_features(signal) -> dict:
    """Flat numeric feature vector, matching intelligence.ml.FEATURE_COLUMNS.

    Must stay in lockstep with the schema: a model trained on these columns
    can only be served against identically-computed inference features.
    """
    from intelligence.ml import FEATURE_COLUMNS  # local import: keep backtesting import-light

    ema_fast = getattr(signal, "ema_fast", None)
    ema_slow = getattr(signal, "ema_slow", None)
    entry_price = getattr(signal, "entry_price", None)
    ema_spread_pct = (
        (ema_fast - ema_slow) / entry_price
        if ema_fast is not None and ema_slow is not None and entry_price
        else 0.0
    )

    trend = "ranging"
    smc_summary = getattr(signal, "smc_summary", None) or {}
    if isinstance(smc_summary, dict) and smc_summary.get("available"):
        trend = smc_summary.get("market_structure", {}).get("trend", "ranging")

    row = {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "rsi_value": getattr(signal, "rsi_value", None),
        "atr_value": getattr(signal, "atr_value", None),
        "ema_spread_pct": ema_spread_pct,
        "smc_trend_uptrend": 1 if trend == "uptrend" else 0,
        "smc_trend_downtrend": 1 if trend == "downtrend" else 0,
        "smc_trend_ranging": 1 if trend == "ranging" else 0,
    }
    assert list(row) == FEATURE_COLUMNS, "feature schema drift — update together"
    return row


# ---------------------------------------------------------------------------
# Walk-forward engine
# ---------------------------------------------------------------------------

def _evaluate(strategy, symbol, window):
    evaluate = getattr(strategy, "evaluate", None)
    if callable(evaluate):
        return evaluate(symbol, window)
    return strategy(symbol, window)  # plain callable


def _entry_index(candles, signal, symbol) -> int:
    """Locate the closed candle the signal fired on, via its candle_time."""
    candle_time = getattr(signal, "candle_time", None)
    if candle_time is not None:
        for j, c in enumerate(candles):
            if c.time == candle_time:
                return j
    raise BacktestError(
        SIGNAL_CANDLE_NOT_FOUND,
        f"signal for {symbol} is timestamped at {candle_time}, which is not in "
        "its evaluation window — refusing to label against the wrong candle",
    )


def run_backtest(adapter, strategy, symbols, start, end, *,
                 timeframe: str = "H1",
                 min_history: int = 60,
                 max_lookahead: int = 200,
                 max_candles: int = 100000) -> list:
    """Walk history forward, evaluate the strategy, label every signal.

    Returns a list of {symbol, strategy, direction, candle_time, <features>,
    outcome} rows — one per signal that fired. `symbols`: iterable of str.
    `start`/`end`: datetimes bounding the backtest window.

    At each step i the strategy sees `candles[:i+1]`; per the Strategy
    contract its last element is the forming candle and is excluded from
    signal logic — the same closed-candle semantics as live. The outcome is
    labeled forward from the signal's own candle (entry candle excluded
    from its own outcome check — no lookahead).
    """
    if not symbols:
        raise BacktestError(CONFIG_INVALID, "symbols must be non-empty")
    if min_history < 2:
        raise BacktestError(CONFIG_INVALID, "min_history must be >= 2")

    ro = _ReadOnlyAdapter(adapter)
    logger.info(
        "Backtest starting: adapter wrapped read-only — live order submission "
        "is structurally disabled for this run."
    )

    rows = []
    for symbol in symbols:
        candles = fetch_historical_candles(ro, symbol, timeframe, start, end,
                                           max_candles=max_candles)
        logger.info("Fetched %d candles for %s %s", len(candles), symbol, timeframe)
        for i in range(min_history, len(candles)):
            # The strategy treats window[-1] as forming — same as live.
            window = candles[: i + 1]
            signal = _evaluate(strategy, symbol, window)
            if signal is None:
                continue
            entry_index = _entry_index(candles, signal, symbol)
            outcome = label_outcome(
                candles, entry_index, signal.direction,
                signal.stop_loss, signal.take_profit, max_lookahead,
            )
            candle_time = getattr(signal, "candle_time", None)
            rows.append({
                "symbol": symbol,
                "strategy": getattr(strategy, "name", getattr(signal, "strategy", None)),
                "direction": signal.direction,
                "candle_time": candle_time.isoformat() if hasattr(candle_time, "isoformat") else str(candle_time),
                **extract_features(signal),
                "outcome": outcome,
            })
        logger.info("Backtest produced %d labeled signals for %s (%s)",
                    len([r for r in rows if r["symbol"] == symbol]), symbol,
                    _outcome_summary(rows, symbol))
    return rows


def _outcome_summary(rows, symbol) -> str:
    sub = [r for r in rows if r["symbol"] == symbol]
    wins = sum(1 for r in sub if r["outcome"] == "win")
    losses = sum(1 for r in sub if r["outcome"] == "loss")
    timeouts = sum(1 for r in sub if r["outcome"] == "timeout")
    return f"{wins} win, {losses} loss, {timeouts} timeout"


def write_csv(rows: list, output_path: str) -> None:
    if not rows:
        logger.warning("No rows to write.")
        return
    fieldnames = list(rows[0].keys())
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d rows to %s", len(rows), output_path)


# ---------------------------------------------------------------------------
# CLI: backtest_dataset.csv generation against a real adapter
# ---------------------------------------------------------------------------

def _build_mt5_adapter():
    """MT5Adapter from env creds. Windows-only import, kept inside main()."""
    import os

    from broker.mt5.adapter import MT5Adapter

    login = os.environ.get("MT5_LOGIN")
    password = os.environ.get("MT5_PASSWORD")
    server = os.environ.get("MT5_SERVER")
    if not (login and password and server):
        raise BacktestError(
            CONFIG_INVALID,
            "MT5_LOGIN / MT5_PASSWORD / MT5_SERVER env vars are required for --adapter mt5",
        )
    adapter = MT5Adapter()
    adapter.connect({"login": int(login), "password": password, "server": server})
    return adapter


def _build_strategy(timeframe):
    from core.strategies import EmaRsiStrategy

    try:
        from config import EmaRsiStrategyConfig  # config-builder's loader
        cfg = EmaRsiStrategyConfig()
    except Exception:  # noqa: BLE001 — fall back to documented defaults
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            ema_fast=20, ema_slow=50, rsi_period=14, atr_period=14,
            rsi_overbought=70.0, rsi_oversold=30.0,
            atr_sl_multiplier=1.5, atr_tp_multiplier=3.0,
        )
    return EmaRsiStrategy(cfg, timeframe=timeframe)


def main():
    parser = argparse.ArgumentParser(
        description="Walk-forward backtest: same strategy semantics as live "
                    "(forming candle excluded), data via BrokerAdapter, "
                    "structurally incapable of live orders."
    )
    parser.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY,AUDUSD")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output", default="backtest_dataset.csv")
    parser.add_argument("--min-history", type=int, default=60)
    parser.add_argument("--max-lookahead", type=int, default=200)
    parser.add_argument("--adapter", default="mt5", choices=["mt5"],
                        help="data source adapter (backtest is read-only regardless)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    adapter = _build_mt5_adapter()
    try:
        strategy = _build_strategy(args.timeframe)
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d")
        rows = run_backtest(
            adapter, strategy, args.symbols.split(","), start, end,
            timeframe=args.timeframe, min_history=args.min_history,
            max_lookahead=args.max_lookahead,
        )
        write_csv(rows, args.output)
    finally:
        adapter.disconnect()


if __name__ == "__main__":
    main()
