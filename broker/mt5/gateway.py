"""
MT5 gateway transport — the defined-operation contract between the Linux
agent host and a MetaTrader5 execution environment.

The operation set is FIXED (see gateway_contract.md). Two transports:

  * The LOCAL path: MT5Adapter's direct MetaTrader5 calls in adapter.py.
    Meaningful only where a compatible runtime exists (Windows host with
    an MT5 terminal, or a Wine-compatible runtime). See RUNTIME.md.

  * RemoteMT5GatewayTransport (below): stdlib-only HTTP client for a
    REMOTE gateway host — a separate machine that runs the MT5 terminal
    plus a small HTTP service exposing exactly the operation set below,
    authenticated with a bearer token.

SECURITY — no arbitrary command execution, ever:
  * The client exposes EXPLICIT per-operation methods only. There is no
    generic execute()/command()/rpc() API, and `_request()` refuses any
    operation name not present in OPERATIONS (GATEWAY_REJECTED).
  * Adding a new capability means adding a new explicit method AND a new
    contract entry — never a stringly-typed back door.
"""

from __future__ import annotations

import abc
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from broker import (
    AccountInfo,
    BrokerError,
    Deal,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    SymbolSpec,
    CONFIG_INVALID,
    CREDENTIALS_INVALID,
    GATEWAY_AUTH_FAILED,
    GATEWAY_REJECTED,
    GATEWAY_UNREACHABLE,
)
from core.market import Candle

logger = logging.getLogger("broker.mt5.gateway")

# ---------------------------------------------------------------------------
# The contract: operation -> HTTP binding. The client will ONLY ever call
# these endpoints. Anything else is rejected client-side before any byte
# leaves the machine.
# ---------------------------------------------------------------------------

OPERATIONS: Dict[str, Dict[str, str]] = {
    "ping":         {"method": "GET",  "path": "/api/v1/ping"},
    "terminal":     {"method": "GET",  "path": "/api/v1/terminal"},
    "account":      {"method": "GET",  "path": "/api/v1/account"},
    "symbols":      {"method": "POST", "path": "/api/v1/symbols"},
    "market_data":  {"method": "POST", "path": "/api/v1/market_data"},
    "quote":        {"method": "POST", "path": "/api/v1/quote"},
    "positions":    {"method": "GET",  "path": "/api/v1/positions"},
    "orders":       {"method": "GET",  "path": "/api/v1/orders"},
    "submit":       {"method": "POST", "path": "/api/v1/submit"},
    "modify":       {"method": "POST", "path": "/api/v1/modify"},
    "close":        {"method": "POST", "path": "/api/v1/close"},
    "deal_history": {"method": "POST", "path": "/api/v1/deal_history"},
}

GATEWAY_URL_ENV = "MT5_GATEWAY_URL"
GATEWAY_TOKEN_ENV = "MT5_GATEWAY_TOKEN"


# ---------------------------------------------------------------------------
# Transport interface (the operation contract as a Python ABC)
# ---------------------------------------------------------------------------

class MT5Transport(abc.ABC):
    """Fixed MT5 operation set. Implementations: the local direct-mt5
    path (adapter.py) and RemoteMT5GatewayTransport (below)."""

    @abc.abstractmethod
    def ping(self) -> bool:
        """Transport reachability + auth check. True when the runtime answers.
        Never raises — use check() when the failure reason matters."""

    def check(self) -> None:
        """Raise unless the transport answers and authenticates.
        Transports that cannot distinguish failure reasons may leave the
        default (NotImplementedError); ping() stays the safe probe."""
        raise NotImplementedError

    @abc.abstractmethod
    def terminal_status(self) -> dict:
        """Terminal capabilities, e.g. {"trade_allowed": bool, ...}."""

    @abc.abstractmethod
    def account_info(self) -> AccountInfo:
        ...

    @abc.abstractmethod
    def symbols(self, names: Optional[List[str]] = None) -> List[SymbolSpec]:
        ...

    @abc.abstractmethod
    def candles(self, symbol: str, timeframe: str, count: int = 200) -> List[Candle]:
        """CLOSED candles only, oldest -> newest."""

    @abc.abstractmethod
    def quote(self, symbol: str) -> Optional[Quote]:
        ...

    @abc.abstractmethod
    def positions(self) -> List[Position]:
        ...

    @abc.abstractmethod
    def orders(self) -> List[Order]:
        ...

    @abc.abstractmethod
    def submit_order(self, req: OrderRequest) -> OrderResult:
        """Broker-confirmed only."""

    @abc.abstractmethod
    def modify_position(self, ticket: int, sl: float, tp: float) -> None:
        ...

    @abc.abstractmethod
    def close_position(self, ticket: int) -> float:
        """Returns the broker-confirmed close price."""

    @abc.abstractmethod
    def deal_history(
        self, from_: datetime, to: datetime, position_id: Optional[int] = None
    ) -> List[Deal]:
        ...


# ---------------------------------------------------------------------------
# Response validation helpers
# ---------------------------------------------------------------------------

def _req(d: dict, key: str, types, op: str):
    if not isinstance(d, dict) or key not in d or not isinstance(d[key], types):
        raise BrokerError(
            GATEWAY_REJECTED,
            f"Malformed gateway response for op {op!r}: missing/invalid {key!r}.",
            detail={"op": op, "key": key},
        )
    return d[key]


def _parse_time(value: Any, op: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        raise BrokerError(
            GATEWAY_REJECTED,
            f"Malformed gateway response for op {op!r}: bad ISO time {value!r}.",
            detail={"op": op},
        )


# ---------------------------------------------------------------------------
# Remote gateway transport — HTTP + bearer token, stdlib only
# ---------------------------------------------------------------------------

class RemoteMT5GatewayTransport(MT5Transport):
    """HTTP client for a remote MT5 gateway host.

    Auth: bearer token (constructor arg or MT5_GATEWAY_TOKEN env). The
    token is REQUIRED — a remote gateway never runs unauthenticated.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 10.0,
    ):
        url = (base_url or os.environ.get(GATEWAY_URL_ENV, "") or "").strip().rstrip("/")
        if not url:
            raise BrokerError(
                CONFIG_INVALID,
                f"Remote MT5 gateway selected but no URL configured "
                f"(set {GATEWAY_URL_ENV}, e.g. https://mt5-gateway.example.com).",
            )
        tok = (token if token is not None else os.environ.get(GATEWAY_TOKEN_ENV, "") or "").strip()
        if not tok:
            raise BrokerError(
                CREDENTIALS_INVALID,
                f"Remote MT5 gateway requires a bearer token "
                f"(set {GATEWAY_TOKEN_ENV}). Refusing unauthenticated gateway use.",
            )
        if not url.lower().startswith("https://"):
            # The bearer token rides in the Authorization header: over
            # plain HTTP it is readable on the wire. Refusing would brick
            # legitimate LAN/test setups, so warn loudly instead.
            logger.warning(
                "MT5 gateway URL %r does not use https:// — the bearer "
                "token is sent in cleartext. Use https in production.",
                url.split("@")[-1],
            )
        self.base_url = url
        self._token = tok
        self.timeout = timeout

    # -- transport core -------------------------------------------------
    def _request(self, op: str, payload: Optional[dict] = None) -> dict:
        """Invoke ONE contracted operation. op MUST be in OPERATIONS;
        anything else raises GATEWAY_REJECTED before any network I/O."""
        spec = OPERATIONS.get(op)
        if spec is None:
            raise BrokerError(
                GATEWAY_REJECTED,
                f"Unknown gateway operation {op!r}. The gateway contract "
                f"allows only the defined operations "
                f"({', '.join(sorted(OPERATIONS))}); arbitrary commands "
                f"are never sent.",
                detail={"op": op},
            )
        url = self.base_url + spec["path"]
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = json.dumps(payload or {}).encode("utf-8") if spec["method"] == "POST" else None
        req = urlrequest.Request(url, data=data, headers=headers, method=spec["method"])
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise BrokerError(
                    GATEWAY_AUTH_FAILED,
                    f"Gateway rejected our credentials (HTTP {exc.code}). "
                    f"Check {GATEWAY_TOKEN_ENV}.",
                    detail={"op": op, "http_status": exc.code},
                )
            raise BrokerError(
                GATEWAY_UNREACHABLE,
                f"Gateway HTTP error {exc.code} on op {op!r}: {exc.reason}.",
                detail={"op": op, "http_status": exc.code},
            )
        except URLError as exc:
            raise BrokerError(
                GATEWAY_UNREACHABLE,
                f"MT5 gateway unreachable at {self.base_url} "
                f"(op {op!r}): {exc.reason}. See broker/mt5/RUNTIME.md.",
                detail={"op": op, "url": self.base_url,
                        "reason": str(exc.reason)},
            )
        except TimeoutError as exc:
            raise BrokerError(
                GATEWAY_UNREACHABLE,
                f"MT5 gateway timed out after {self.timeout}s (op {op!r}).",
                detail={"op": op},
            )
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise BrokerError(
                GATEWAY_REJECTED,
                f"Gateway returned non-JSON for op {op!r}.",
                detail={"op": op},
            )
        if not isinstance(body, dict) or "ok" not in body:
            raise BrokerError(
                GATEWAY_REJECTED,
                f"Malformed gateway envelope for op {op!r} "
                f"(expected {{\"ok\": ..., \"data\"|\"error_code\": ...}}).",
                detail={"op": op},
            )
        if not body["ok"]:
            # Semantic failure from the gateway — codes pass through so
            # callers see INVALID_SYMBOL / MARKET_CLOSED / etc., not a
            # transport error.
            raise BrokerError(
                str(body.get("error_code") or GATEWAY_REJECTED),
                str(body.get("message") or f"Gateway op {op!r} failed."),
                detail={"op": op, **(body.get("detail") or {})},
            )
        data_out = body.get("data")
        if not isinstance(data_out, dict):
            raise BrokerError(
                GATEWAY_REJECTED,
                f"Malformed gateway response for op {op!r}: 'data' not an object.",
                detail={"op": op},
            )
        return data_out

    # -- operations -------------------------------------------------------
    def check(self) -> None:
        """Raise on unreachable/auth failure (unlike ping, never swallows)."""
        self._request("ping")

    def ping(self) -> bool:
        try:
            self.check()
            return True
        except BrokerError:
            return False

    def terminal_status(self) -> dict:
        data = self._request("terminal")
        return {
            "trade_allowed": bool(data.get("trade_allowed", False)),
            "connected": bool(data.get("connected", False)),
            "server": str(data.get("server", "")),
        }

    def account_info(self) -> AccountInfo:
        d = self._request("account")
        return AccountInfo(
            balance=float(_req(d, "balance", (int, float), "account")),
            equity=float(_req(d, "equity", (int, float), "account")),
            currency=str(d.get("currency", "")),
            margin=float(d.get("margin", 0.0)),
            free_margin=float(d.get("free_margin", 0.0)),
            leverage=int(d.get("leverage", 0)),
            login=int(d.get("login", 0)),
            server=str(d.get("server", "")),
        )

    def symbols(self, names: Optional[List[str]] = None) -> List[SymbolSpec]:
        d = self._request("symbols", {"names": names or []})
        out = []
        for s in _req(d, "symbols", list, "symbols"):
            out.append(SymbolSpec(
                name=str(_req(s, "name", str, "symbols")),
                volume_min=float(_req(s, "volume_min", (int, float), "symbols")),
                volume_max=float(_req(s, "volume_max", (int, float), "symbols")),
                volume_step=float(_req(s, "volume_step", (int, float), "symbols")),
                tick_value=float(_req(s, "tick_value", (int, float), "symbols")),
                tick_size=float(_req(s, "tick_size", (int, float), "symbols")),
                contract_size=float(s.get("contract_size", 0.0)),
                digits=int(s.get("digits", 0)),
                point=float(s.get("point", 0.0)),
            ))
        return out

    def candles(self, symbol: str, timeframe: str, count: int = 200) -> List[Candle]:
        d = self._request("market_data",
                          {"symbol": symbol, "timeframe": timeframe, "count": count})
        out = []
        for c in _req(d, "candles", list, "market_data"):
            out.append(Candle(
                time=_parse_time(c.get("time"), "market_data"),
                open=float(_req(c, "open", (int, float), "market_data")),
                high=float(_req(c, "high", (int, float), "market_data")),
                low=float(_req(c, "low", (int, float), "market_data")),
                close=float(_req(c, "close", (int, float), "market_data")),
                volume=int(c.get("volume", 0)),
            ))
        return out  # gateway contract: server sends CLOSED candles only

    def quote(self, symbol: str) -> Optional[Quote]:
        d = self._request("quote", {"symbol": symbol})
        if not d.get("available", True):
            return None
        return Quote(
            symbol=symbol,
            bid=float(_req(d, "bid", (int, float), "quote")),
            ask=float(_req(d, "ask", (int, float), "quote")),
            time=_parse_time(d.get("time"), "quote"),
        )

    def positions(self) -> List[Position]:
        d = self._request("positions")
        return [self._position(p) for p in _req(d, "positions", list, "positions")]

    def orders(self) -> List[Order]:
        d = self._request("orders")
        out = []
        for o in _req(d, "orders", list, "orders"):
            out.append(Order(
                ticket=int(_req(o, "ticket", int, "orders")),
                symbol=str(_req(o, "symbol", str, "orders")),
                direction=str(_req(o, "direction", str, "orders")),
                volume=float(_req(o, "volume", (int, float), "orders")),
                price=float(_req(o, "price", (int, float), "orders")),
                sl=float(o.get("sl", 0.0)),
                tp=float(o.get("tp", 0.0)),
                magic=int(o.get("magic", 0)),
                comment=str(o.get("comment", "")),
                time_setup=_parse_time(o.get("time_setup"), "orders"),
            ))
        return out

    @staticmethod
    def _position(p: dict) -> Position:
        return Position(
            ticket=int(_req(p, "ticket", int, "positions")),
            position_id=int(_req(p, "position_id", int, "positions")),
            symbol=str(_req(p, "symbol", str, "positions")),
            direction=str(_req(p, "direction", str, "positions")),
            volume=float(_req(p, "volume", (int, float), "positions")),
            price_open=float(_req(p, "price_open", (int, float), "positions")),
            price_current=float(_req(p, "price_current", (int, float), "positions")),
            sl=float(p.get("sl", 0.0)),
            tp=float(p.get("tp", 0.0)),
            profit=float(p.get("profit", 0.0)),
            swap=float(p.get("swap", 0.0)),
            magic=int(p.get("magic", 0)),
            comment=str(p.get("comment", "")),
            time_open=_parse_time(p.get("time_open"), "positions"),
        )

    def submit_order(self, req: OrderRequest) -> OrderResult:
        d = self._request("submit", {
            "symbol": req.symbol,
            "direction": req.direction,
            "volume": req.volume,
            "stop_loss": req.stop_loss,
            "take_profit": req.take_profit,
            "comment": req.order_comment(),
            "idempotency_key": req.idempotency_key,
        })
        # Gateway returns broker-confirmed fills only (contract §submit).
        return OrderResult(
            ticket=int(_req(d, "ticket", int, "submit")),
            symbol=req.symbol,
            direction=req.direction,
            volume=float(_req(d, "volume", (int, float), "submit")),
            price=float(_req(d, "price", (int, float), "submit")),
            retcode=int(d.get("retcode", 0)),
            message=str(d.get("message", "")),
            raw=d.get("raw") or {},
        )

    def modify_position(self, ticket: int, sl: float, tp: float) -> None:
        self._request("modify", {"ticket": ticket, "sl": sl, "tp": tp})

    def close_position(self, ticket: int) -> float:
        d = self._request("close", {"ticket": ticket})
        return float(_req(d, "price", (int, float), "close"))

    def deal_history(
        self, from_: datetime, to: datetime, position_id: Optional[int] = None
    ) -> List[Deal]:
        d = self._request("deal_history", {
            "from": from_.isoformat(),
            "to": to.isoformat(),
            "position_id": position_id,
        })
        out = []
        for x in _req(d, "deals", list, "deal_history"):
            out.append(Deal(
                ticket=int(_req(x, "ticket", int, "deal_history")),
                position_id=int(_req(x, "position_id", int, "deal_history")),
                symbol=str(_req(x, "symbol", str, "deal_history")),
                direction=str(_req(x, "direction", str, "deal_history")),
                entry=str(_req(x, "entry", str, "deal_history")),
                volume=float(_req(x, "volume", (int, float), "deal_history")),
                price=float(_req(x, "price", (int, float), "deal_history")),
                profit=float(x.get("profit", 0.0)),
                commission=float(x.get("commission", 0.0)),
                swap=float(x.get("swap", 0.0)),
                magic=int(x.get("magic", 0)),
                comment=str(x.get("comment", "")),
                time=_parse_time(x.get("time"), "deal_history"),
            ))
        return out
