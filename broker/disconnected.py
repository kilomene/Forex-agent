"""
DisconnectedAdapter — lets core run on Linux with no broker attached.

Every broker operation raises BrokerError(BROKER_UNAVAILABLE). Read-only
introspection (health()) reports the disconnected state without raising,
so monitors can distinguish "no broker configured" from a failure.
"""

from datetime import datetime
from typing import List, Optional

from broker import (
    AccountInfo,
    BrokerAdapter,
    BrokerError,
    BrokerHealth,
    Deal,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    BROKER_UNAVAILABLE,
)
from core.market import Candle


class DisconnectedAdapter(BrokerAdapter):
    adapter_name = "disconnected"

    _MSG = (
        "No broker connected (DisconnectedAdapter). Market data, account "
        "state and order submission are unavailable; core analysis still runs."
    )

    def _unavailable(self) -> BrokerError:
        return BrokerError(BROKER_UNAVAILABLE, self._MSG)

    def connect(self, creds: dict) -> None:
        raise self._unavailable()

    def disconnect(self) -> None:
        return None

    def account_info(self) -> AccountInfo:
        raise self._unavailable()

    def symbols(self, names: Optional[List[str]] = None) -> List[SymbolSpec]:
        raise self._unavailable()

    def candles(self, symbol: str, timeframe: str, count: int = 200) -> List[Candle]:
        raise self._unavailable()

    def positions(self) -> List[Position]:
        raise self._unavailable()

    def orders(self) -> List[Order]:
        raise self._unavailable()

    def submit_order(self, req: OrderRequest) -> OrderResult:
        raise self._unavailable()

    def modify_order(self, ticket: int, sl: float, tp: float) -> None:
        raise self._unavailable()

    def close_position(self, ticket: int) -> float:
        raise self._unavailable()

    def deal_history(
        self, from_: datetime, to: datetime, position_id: Optional[int] = None
    ) -> List[Deal]:
        raise self._unavailable()

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=False,
            adapter=self.adapter_name,
            server_time=datetime.now(),
            message=self._MSG,
        )

    def broker_status(self) -> dict:
        """Structured status: nothing is configured, so every capability
        flag is False. Honest by construction — no probes, no fake data."""
        return {"broker": {
            "provider": "disconnected",
            "configured": False,
            "reachable": False,
            "connected": False,
            "account_available": False,
            "market_data_available": False,
            "trading_available": False,
            "detail": {"reason": "No broker configured (provider=disconnected)."},
        }}
