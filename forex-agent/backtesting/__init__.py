"""Backtesting: walk-forward strategy evaluation, fully separated from live execution."""

from .data import fetch_historical_candles
from .engine import extract_features, run_backtest, write_csv
from .errors import BacktestError
from .labeler import label_outcome

__all__ = [
    "BacktestError",
    "extract_features",
    "fetch_historical_candles",
    "label_outcome",
    "run_backtest",
    "write_csv",
]
