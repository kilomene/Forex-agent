"""
Broker abstraction — the Windows containment vessel.

BrokerAdapter is the ONLY seam through which core ever touches a broker.
`import MetaTrader5` may appear only under broker/mt5/; everything else
(including this module) imports cleanly on Linux with no broker present.

Error model: adapter methods return their dataclass on success and raise
BrokerError carrying a structured code on failure. Structured codes:
    BROKER_UNAVAILABLE, MT5_NOT_CONNECTED, INVALID_SYMBOL, MARKET_CLOSED,
    RISK_LIMIT_EXCEEDED, DAILY_LOSS_LIMIT, MAX_EXPOSURE, INVALID_ORDER,
    CONFIG_INVALID, CREDENTIALS_INVALID
(These codes are shared with the execution gateway's reject reasons.)

Bot-identity convention (ported from mt5_shared.py): this bot's own
positions/deals carry MAGIC_NUMBER and comments of the form
'approved:{signal_id}'. Position counting and exposure math MUST filter
on the magic number so manual/foreign positions never consume the bot's
quota — one of the original risk.py holes.
"""

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from core.market import Candle

# ---------------------------------------------------------------------------
# Structured error codes
# ---------------------------------------------------------------------------

BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
MT5_NOT_CONNECTED = "MT5_NOT_CONNECTED"
INVALID_SYMBOL = "INVALID_SYMBOL"
MARKET_CLOSED = "MARKET_CLOSED"
RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"
DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
MAX_EXPOSURE = "MAX_EXPOSURE"
INVALID_ORDER = "INVALID_ORDER"
CONFIG_INVALID = "CONFIG_INVALID"
CREDENTIALS_INVALID = "CREDENTIALS_INVALID"


class BrokerError(Exception):
    """Structured broker failure. Never leak secrets in `detail`."""

    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# Bot-identity convention (was mt5_shared.py)
# ---------------------------------------------------------------------------

MAGIC_NUMBER = 20260817


def extract_signal_id_from_comment(comment: str) -> Optional[str]:
    """Comments are set as 'approved:{signal_id}' at order placement."""
    if comment and comment.startswith("approved:"):
        return comment[len("approved:"):]
    return None


def is_own_position(magic: Optional[int]) -> bool:
    """True only for positions/deals placed by this bot."""
    return magic == MAGIC_NUMBER


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class AccountInfo:
    balance: float
    equity: float  # balance AND equity — daily-loss math uses equity
    currency: str = ""
    margin: float = 0.0
    free_margin: float = 0.0
    leverage: int = 0
    login: int = 0
    server: str = ""


@dataclass
class SymbolSpec:
    name: str
    volume_min: float
    volume_max: float
    volume_step: float
    tick_value: float   # account-currency value of one tick for 1.0 lot
    tick_size: float    # minimum price increment
    contract_size: float = 0.0
    digits: int = 0
    point: float = 0.0


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    time: datetime

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class Position:
    ticket: int
    position_id: int
    symbol: str
    direction: str  # "BUY" or "SELL"
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float   # floating P/L in account currency
    swap: float
    magic: int
    comment: str
    time_open: datetime

    @property
    def is_own(self) -> bool:
        return is_own_position(self.magic)

    @property
    def signal_id(self) -> Optional[str]:
        return extract_signal_id_from_comment(self.comment)


@dataclass
class Order:
    ticket: int
    symbol: str
    direction: str  # "BUY" or "SELL"
    volume: float
    price: float
    sl: float
    tp: float
    magic: int
    comment: str
    time_setup: datetime


@dataclass
class OrderRequest:
    symbol: str
    direction: str  # "BUY" or "SELL"
    volume: float
    stop_loss: float
    take_profit: float
    signal_id: Optional[str] = None
    comment: Optional[str] = None
    idempotency_key: Optional[str] = None

    def order_comment(self) -> str:
        if self.comment:
            return self.comment
        if self.signal_id:
            return f"approved:{self.signal_id}"
        return "forex-agent"


@dataclass
class OrderResult:
    """Broker-confirmed fill. submit_order() returns this ONLY after the
    broker confirms the trade — never a provisional/local fill."""
    ticket: int
    symbol: str
    direction: str
    volume: float
    price: float
    retcode: int = 0
    message: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Deal:
    ticket: int
    position_id: int
    symbol: str
    direction: str  # "BUY" or "SELL"
    entry: str      # "IN" | "OUT"
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    magic: int
    comment: str
    time: datetime


@dataclass
class BrokerHealth:
    connected: bool
    adapter: str
    server_time: Optional[datetime] = None
    message: str = ""


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class BrokerAdapter(abc.ABC):
    """Pure interface — no broker imports here, ever."""

    @abc.abstractmethod
    def connect(self, creds: dict) -> None:
        """Establish the broker session. Raises BrokerError on failure."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        ...

    @abc.abstractmethod
    def account_info(self) -> AccountInfo:
        """Balance AND equity. Raises MT5_NOT_CONNECTED when down."""

    @abc.abstractmethod
    def symbols(self, names: Optional[List[str]] = None) -> List[SymbolSpec]:
        """Trade specs incl. volume_min/max/step. Raises INVALID_SYMBOL."""

    @abc.abstractmethod
    def candles(self, symbol: str, timeframe: str, count: int = 200) -> List[Candle]:
        """CLOSED candles only, oldest -> newest. The adapter — not the
        caller — is responsible for excluding the forming candle."""

    @abc.abstractmethod
    def positions(self) -> List[Position]:
        ...

    @abc.abstractmethod
    def orders(self) -> List[Order]:
        ...

    @abc.abstractmethod
    def submit_order(self, req: OrderRequest) -> OrderResult:
        """Broker-confirmed only: returns after the broker confirms the
        fill; raises BrokerError (INVALID_ORDER / MT5_NOT_CONNECTED /
        MARKET_CLOSED) otherwise. Never returns a provisional fill."""

    @abc.abstractmethod
    def modify_order(self, ticket: int, sl: float, tp: float) -> None:
        """Adjust SL/TP of an open position. Raises INVALID_ORDER."""

    @abc.abstractmethod
    def close_position(self, ticket: int) -> float:
        """Close at market; returns the broker-confirmed close price."""

    @abc.abstractmethod
    def deal_history(
        self, from_: datetime, to: datetime, position_id: Optional[int] = None
    ) -> List[Deal]:
        ...

    @abc.abstractmethod
    def health(self) -> BrokerHealth:
        ...

    # -- optional extensions (default: unsupported) -------------------------
    def quote(self, symbol: str) -> Optional[Quote]:
        """Live bid/ask. Used opportunistically for spread + market-open
        checks; adapters that cannot provide it return None and the
        gateway skips those checks (recorded in the audit log)."""
        return None
