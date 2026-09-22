"""Historical data fetch for backtesting — exclusively through BrokerAdapter.

`import MetaTrader5` must NEVER appear here (or anywhere in backtesting/).
All market data comes from `broker.BrokerAdapter.candles()`, which by
contract returns CLOSED candles only (oldest -> newest).

The adapter interface is count-based ("N most recent"), so a date range is
served by fetching up to `max_candles` and filtering to [start, end].
Requesting a start older than available history is an honest
INSUFFICIENT_HISTORY error, not silent truncation.
"""

from broker import BrokerAdapter, BrokerError  # noqa: F401  (contract import; also re-exported)

from .errors import CONFIG_INVALID, INSUFFICIENT_HISTORY, NO_DATA, BacktestError


def fetch_historical_candles(adapter: BrokerAdapter, symbol: str, timeframe: str,
                             start, end, max_candles: int = 100000) -> list:
    """Return closed candles for symbol/timeframe within [start, end].

    `start`/`end` are datetimes comparable to the candles' `time` fields
    (use timezone-aware UTC throughout). BrokerError (e.g.
    BROKER_UNAVAILABLE from DisconnectedAdapter) propagates unchanged.
    """
    if start >= end:
        raise BacktestError(CONFIG_INVALID, f"start ({start}) must be before end ({end})")
    if max_candles < 1:
        raise BacktestError(CONFIG_INVALID, "max_candles must be >= 1")

    candles = adapter.candles(symbol, timeframe, count=max_candles)
    if not candles:
        raise BacktestError(NO_DATA, f"adapter returned no candles for {symbol} {timeframe}")

    if candles[0].time > start:
        raise BacktestError(
            INSUFFICIENT_HISTORY,
            f"requested start {start} is older than available history "
            f"(earliest candle {candles[0].time}) for {symbol} {timeframe}",
            {"earliest": str(candles[0].time)},
        )

    in_range = [c for c in candles if start <= c.time <= end]
    if not in_range:
        raise BacktestError(
            NO_DATA,
            f"no candles in range [{start}, {end}] for {symbol} {timeframe} "
            f"(fetched {len(candles)} candles)",
        )
    return in_range
