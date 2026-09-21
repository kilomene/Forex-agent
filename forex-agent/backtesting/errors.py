"""Structured errors for backtesting.

Broker-level failures propagate as broker.BrokerError unchanged (already
structured: BROKER_UNAVAILABLE, INVALID_SYMBOL, ...). The codes below cover
backtest-specific problems.
"""

ORDER_BLOCKED = "ORDER_BLOCKED"            # something tried to order through a backtest
NO_DATA = "NO_DATA"                        # adapter returned nothing usable
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"  # requested range older than available data
CONFIG_INVALID = "CONFIG_INVALID"          # bad backtest arguments
SIGNAL_CANDLE_NOT_FOUND = "SIGNAL_CANDLE_NOT_FOUND"  # strategy timestamped outside its window


class BacktestError(Exception):
    """Structured backtest failure. Never carries secrets."""

    def __init__(self, code: str, message: str, detail: dict = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}
