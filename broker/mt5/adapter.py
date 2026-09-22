"""
MT5Adapter — implements BrokerAdapter against a local MT5 terminal
or a remote MT5 gateway host.

This is the ONLY place `import MetaTrader5` may appear in the whole
project. It absorbs BOTH original dialects:
  * MT5Client methods (connect, candles, balance, positions, specs,
    market orders) from mt5_client.py
  * the direct mt5.* calls from exit_manager.py (SL modify, close),
    reconciliation.py (positions_get, history_deals_get) and
    performance_review.py / cost_tracking.py (deal history)

Transports (see broker/mt5/RUNTIME.md and gateway_contract.md):
  * LOCAL (default): direct MetaTrader5 calls. Only meaningful where a
    compatible runtime exists (Windows host + terminal, or a
    Wine-compatible runtime). On Linux without one, every operation
    raises BrokerError(BROKER_UNAVAILABLE) — honest, never fake.
  * REMOTE: when the MT5_GATEWAY_URL env var is set (token via
    MT5_GATEWAY_TOKEN), all operations route through
    broker/mt5/gateway.py's RemoteMT5GatewayTransport: a FIXED,
    authenticated operation set over HTTP. No arbitrary commands, ever.

The BrokerAdapter method signatures are unchanged — the Forex core keeps
calling BrokerAdapter and never knows which transport is underneath.

Graceful degradation: if the MetaTrader5 package is missing (Linux),
importing this module still works; every broker operation raises
BrokerError(BROKER_UNAVAILABLE) via _require_mt5(). Never raises at
import time.
"""

import logging
import os
import sys
from datetime import datetime
from typing import List, Optional

try:
    import MetaTrader5 as mt5
except ImportError:  # Linux / no terminal — degraded, not fatal
    mt5 = None  # type: ignore[assignment]

from broker import (
    MAGIC_NUMBER,
    AccountInfo,
    BrokerAdapter,
    BrokerError,
    BrokerHealth,
    Deal,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    SymbolSpec,
    BROKER_UNAVAILABLE,
    CREDENTIALS_INVALID,
    GATEWAY_AUTH_FAILED,
    GATEWAY_UNREACHABLE,
    INVALID_ORDER,
    INVALID_SYMBOL,
    MARKET_CLOSED,
    MT5_NOT_CONNECTED,
    CONFIG_INVALID,
)
from broker.mt5.gateway import RemoteMT5GatewayTransport
from broker.mt5.shared import extract_signal_id_from_comment  # noqa: F401  (kept for adapter-internal use)
from core.market import Candle

logger = logging.getLogger("broker.mt5")

# Set to "1" to silence the non-Windows platform warning when you run a
# Wine-compatible MT5 runtime (unsupported; see broker/mt5/RUNTIME.md).
NONWINDOWS_RUNTIME_ENV = "MT5_ALLOW_NONWINDOWS_RUNTIME"


def _require_mt5():
    """Guard every direct-MetaTrader5 call.

    Windows-specific assumptions isolated here:
      1. The MetaTrader5 package ships Windows-only wheels. On Linux the
         import fails -> BROKER_UNAVAILABLE with an explicit message
         (pip install MetaTrader5 != a working MT5; see RUNTIME.md).
      2. Even with the package present (Wine), the calls below assume a
         real terminal (terminal64.exe) behind them. On non-Windows we
         log a warning rather than fail, because Wine runtimes exist;
         RUNTIME.md documents the supported configurations.
    """
    if mt5 is None:
        raise BrokerError(
            BROKER_UNAVAILABLE,
            "MetaTrader5 is not available on this machine: the MetaTrader5 "
            "package is not installed (it ships Windows-only wheels — "
            "'pip install MetaTrader5' on Linux does NOT create a working "
            "MT5 environment). Real MT5 needs a Windows host / "
            "Wine-compatible runtime with an MT5 terminal, or a remote "
            "gateway host (MT5_GATEWAY_URL). See broker/mt5/RUNTIME.md.",
            detail={"reason": "mt5_package_missing"},
        )
    if sys.platform != "win32" and os.environ.get(NONWINDOWS_RUNTIME_ENV) != "1":
        logger.warning(
            "MetaTrader5 calls on non-Windows platform %r without a declared "
            "compatible runtime. Real MT5 needs a Windows host, a "
            "Wine-compatible runtime (%s=1), or a remote gateway "
            "(MT5_GATEWAY_URL). See broker/mt5/RUNTIME.md.",
            sys.platform, NONWINDOWS_RUNTIME_ENV,
        )
    return mt5


def _timeframe(name: str):
    m = _require_mt5()
    mapping = {
        "M1": m.TIMEFRAME_M1,
        "M5": m.TIMEFRAME_M5,
        "M15": m.TIMEFRAME_M15,
        "M30": m.TIMEFRAME_M30,
        "H1": m.TIMEFRAME_H1,
        "H4": m.TIMEFRAME_H4,
        "D1": m.TIMEFRAME_D1,
    }
    tf = mapping.get(name)
    if tf is None:
        raise BrokerError(CONFIG_INVALID, f"Unsupported timeframe: {name}")
    return tf


class MT5Adapter(BrokerAdapter):
    adapter_name = "mt5"

    def __init__(self, transport=None):
        """transport: an MT5Transport (see broker/mt5/gateway.py). When None
        and the MT5_GATEWAY_URL env var is set, a RemoteMT5GatewayTransport
        is built (token from MT5_GATEWAY_TOKEN, required). Otherwise the
        local direct-MetaTrader5 path is used."""
        self._connected = False
        self._login: Optional[int] = None
        self._server: str = ""
        if transport is None:
            gw_url = (os.environ.get("MT5_GATEWAY_URL") or "").strip()
            if gw_url:
                transport = RemoteMT5GatewayTransport(gw_url)
        self._transport = transport  # None -> local direct-mt5 path

    @property
    def transport_mode(self) -> str:
        """'remote' when routing through the HTTP gateway, else 'local'."""
        return "remote" if self._transport is not None else "local"

    def _ensure_remote(self):
        if self._transport is None:  # pragma: no cover - internal guard
            raise BrokerError(CONFIG_INVALID, "No remote transport configured.")
        if not self._connected:
            raise BrokerError(MT5_NOT_CONNECTED, "Not connected — call connect() first.")
        return self._transport

    # -- lifecycle ------------------------------------------------------
    def connect(self, creds: dict) -> None:
        if self._transport is not None:
            # Remote gateway: the bearer token IS the auth; ping() validates
            # reachability + credentials and raises GATEWAY_UNREACHABLE /
            # GATEWAY_AUTH_FAILED with detail on failure.
            self._transport.check()
            self._connected = True
            logger.info("Connected to remote MT5 gateway via %s.",
                        getattr(self._transport, "base_url",
                                type(self._transport).__name__))
            return
        m = _require_mt5()
        login = creds.get("login")
        password = creds.get("password")
        server = creds.get("server")
        if not login or not password or not server:
            raise BrokerError(
                CREDENTIALS_INVALID,
                "MT5 connect requires login/password/server credentials.",
            )
        init_kwargs = {}
        if creds.get("terminal_path"):
            init_kwargs["path"] = creds["terminal_path"]
        if not m.initialize(**init_kwargs):
            raise BrokerError(MT5_NOT_CONNECTED, f"MT5 initialize() failed: {m.last_error()}")
        if not m.login(login=int(login), password=password, server=server):
            m.shutdown()
            raise BrokerError(CREDENTIALS_INVALID, f"MT5 login failed: {m.last_error()}")
        self._connected = True
        self._login = int(login)
        self._server = server
        info = m.account_info()
        logger.info(
            "Connected to MT5. Account=%s Server=%s Balance=%.2f Equity=%.2f %s",
            info.login, info.server, info.balance, info.equity, info.currency,
        )

    def disconnect(self) -> None:
        if self._connected and mt5 is not None:
            mt5.shutdown()
        self._connected = False

    def _ensure(self):
        m = _require_mt5()
        if not self._connected:
            raise BrokerError(MT5_NOT_CONNECTED, "Not connected — call connect() first.")
        return m

    # -- account ---------------------------------------------------------
    def account_info(self) -> AccountInfo:
        if self._transport is not None:
            return self._ensure_remote().account_info()
        m = self._ensure()
        info = m.account_info()
        if info is None:
            raise BrokerError(MT5_NOT_CONNECTED, f"account_info() failed: {m.last_error()}")
        return AccountInfo(
            balance=float(info.balance),
            equity=float(info.equity),  # the original only exposed balance — fixed
            currency=info.currency or "",
            margin=float(info.margin),
            free_margin=float(info.free_margin),
            leverage=int(info.leverage),
            login=int(info.login),
            server=info.server or "",
        )

    # -- market data ------------------------------------------------------
    def symbols(self, names: Optional[List[str]] = None) -> List[SymbolSpec]:
        if self._transport is not None:
            return self._ensure_remote().symbols(names)
        m = self._ensure()
        if names is None:
            all_syms = m.symbols_get()
            names = [s.name for s in all_syms] if all_syms else []
        specs = []
        for name in names:
            info = m.symbol_info(name)
            if info is None:
                raise BrokerError(INVALID_SYMBOL, f"symbol_info() failed for {name}: {m.last_error()}")
            specs.append(SymbolSpec(
                name=name,
                volume_min=float(info.volume_min),
                volume_max=float(info.volume_max),
                volume_step=float(info.volume_step),
                tick_value=float(info.trade_tick_value),
                tick_size=float(info.trade_tick_size),
                contract_size=float(info.trade_contract_size),
                digits=int(info.digits),
                point=float(info.point),
            ))
        return specs

    def candles(self, symbol: str, timeframe: str, count: int = 200) -> List[Candle]:
        """CLOSED candles only: fetches count+1 and drops the forming one."""
        if self._transport is not None:
            return self._ensure_remote().candles(symbol, timeframe, count)
        m = self._ensure()
        tf = _timeframe(timeframe)
        if not m.symbol_select(symbol, True):
            raise BrokerError(INVALID_SYMBOL, f"Could not select symbol {symbol}: {m.last_error()}")
        rates = m.copy_rates_from_pos(symbol, tf, 0, count + 1)
        if rates is None or len(rates) == 0:
            raise BrokerError(INVALID_SYMBOL, f"No candle data for {symbol} {timeframe}: {m.last_error()}")
        closed_rates = rates[:-1]  # last element is the still-forming candle
        return [
            Candle(
                time=datetime.fromtimestamp(r["time"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=int(r["tick_volume"]),
            )
            for r in closed_rates
        ]

    def quote(self, symbol: str) -> Optional[Quote]:
        if self._transport is not None:
            return self._ensure_remote().quote(symbol)
        m = self._ensure()
        tick = m.symbol_info_tick(symbol)
        if tick is None:
            return None
        return Quote(symbol=symbol, bid=float(tick.bid), ask=float(tick.ask),
                     time=datetime.fromtimestamp(tick.time))

    # -- positions / orders -------------------------------------------------
    def _direction_of(self, m, type_value: int) -> str:
        return "BUY" if type_value == m.ORDER_TYPE_BUY else "SELL"

    def positions(self) -> List[Position]:
        if self._transport is not None:
            return self._ensure_remote().positions()
        m = self._ensure()
        raw = m.positions_get()
        if raw is None:
            return []
        out = []
        for p in raw:
            out.append(Position(
                ticket=int(p.ticket),
                position_id=int(p.identifier),
                symbol=p.symbol,
                direction=self._direction_of(m, p.type),
                volume=float(p.volume),
                price_open=float(p.price_open),
                price_current=float(p.price_current),
                sl=float(p.sl),
                tp=float(p.tp),
                profit=float(p.profit),
                swap=float(p.swap),
                magic=int(p.magic),
                comment=p.comment or "",
                time_open=datetime.fromtimestamp(p.time),
            ))
        return out

    def orders(self) -> List[Order]:
        if self._transport is not None:
            return self._ensure_remote().orders()
        m = self._ensure()
        raw = m.orders_get()
        if raw is None:
            return []
        return [
            Order(
                ticket=int(o.ticket),
                symbol=o.symbol,
                direction=self._direction_of(m, o.type),
                volume=float(o.volume_current),
                price=float(o.price_open),
                sl=float(o.sl),
                tp=float(o.tp),
                magic=int(o.magic),
                comment=o.comment or "",
                time_setup=datetime.fromtimestamp(o.time_setup),
            )
            for o in raw
        ]

    def _find_position(self, m, ticket: int):
        for p in self.positions():
            if p.ticket == ticket:
                return p
        raise BrokerError(INVALID_ORDER, f"No open position with ticket {ticket}.")

    # -- trading ------------------------------------------------------------
    def submit_order(self, req: OrderRequest) -> OrderResult:
        """Broker-confirmed only: returns OrderResult solely on
        TRADE_RETCODE_DONE; every other outcome raises BrokerError."""
        if self._transport is not None:
            t = self._ensure_remote()
            if req.direction not in ("BUY", "SELL"):
                raise BrokerError(INVALID_ORDER, f"direction must be BUY|SELL, got {req.direction!r}")
            if req.volume <= 0:
                raise BrokerError(INVALID_ORDER, f"volume must be positive, got {req.volume}")
            if req.stop_loss is None:
                raise BrokerError(INVALID_ORDER, "stop_loss is required")
            return t.submit_order(req)
        m = self._ensure()
        if req.direction not in ("BUY", "SELL"):
            raise BrokerError(INVALID_ORDER, f"direction must be BUY|SELL, got {req.direction!r}")
        if req.volume <= 0:
            raise BrokerError(INVALID_ORDER, f"volume must be positive, got {req.volume}")
        if req.stop_loss is None:
            raise BrokerError(INVALID_ORDER, "stop_loss is required")

        tick = m.symbol_info_tick(req.symbol)
        if tick is None:
            raise BrokerError(INVALID_SYMBOL, f"Could not get tick for {req.symbol}: {m.last_error()}")

        order_type = m.ORDER_TYPE_BUY if req.direction == "BUY" else m.ORDER_TYPE_SELL
        price = float(tick.ask) if req.direction == "BUY" else float(tick.bid)

        request = {
            "action": m.TRADE_ACTION_DEAL,
            "symbol": req.symbol,
            "volume": req.volume,
            "type": order_type,
            "price": price,
            "sl": req.stop_loss,
            "tp": req.take_profit,
            "deviation": 10,
            "magic": MAGIC_NUMBER,
            "comment": req.order_comment(),
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": m.ORDER_FILLING_IOC,
        }
        result = m.order_send(request)
        if result is None:
            raise BrokerError(MT5_NOT_CONNECTED, f"order_send() returned None: {m.last_error()}")

        if result.retcode == m.TRADE_RETCODE_MARKET_CLOSED:
            raise BrokerError(MARKET_CLOSED, f"Market closed for {req.symbol} (retcode={result.retcode}).")
        if result.retcode != m.TRADE_RETCODE_DONE:
            raise BrokerError(
                INVALID_ORDER,
                f"Order rejected by broker: retcode={result.retcode} comment={result.comment}",
                detail={"retcode": result.retcode, "comment": result.comment},
            )

        logger.info(
            "Order confirmed: %s %s %.2f lots @ %.5f ticket=%s SL=%.5f TP=%.5f",
            req.direction, req.symbol, req.volume, result.price, result.order,
            req.stop_loss, req.take_profit,
        )
        return OrderResult(
            ticket=int(result.order),
            symbol=req.symbol,
            direction=req.direction,
            volume=float(result.volume),
            price=float(result.price),
            retcode=int(result.retcode),
            message=str(result.comment),
            raw=result._asdict(),
        )

    def modify_order(self, ticket: int, sl: float, tp: float) -> None:
        """SLTP modify (absorbs exit_manager._modify_sl's direct mt5 call)."""
        if self._transport is not None:
            self._ensure_remote().modify_position(ticket, sl, tp)
            return
        m = self._ensure()
        pos = self._find_position(m, ticket)
        request = {
            "action": m.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": sl,
            "tp": tp,
        }
        result = m.order_send(request)
        if result is None or result.retcode != m.TRADE_RETCODE_DONE:
            raise BrokerError(
                INVALID_ORDER,
                f"SL/TP modify failed for ticket {ticket}: {m.last_error()}",
            )
        logger.info("Modified SL/TP for ticket %s (%s): SL=%.5f TP=%.5f", ticket, pos.symbol, sl, tp)

    def close_position(self, ticket: int) -> float:
        """Market close (absorbs exit_manager.close_position's direct mt5
        call). Returns the broker-confirmed close price."""
        if self._transport is not None:
            return self._ensure_remote().close_position(ticket)
        m = self._ensure()
        pos = self._find_position(m, ticket)
        close_type = m.ORDER_TYPE_SELL if pos.direction == "BUY" else m.ORDER_TYPE_BUY
        tick = m.symbol_info_tick(pos.symbol)
        if tick is None:
            raise BrokerError(MT5_NOT_CONNECTED, f"Could not get tick for {pos.symbol}: {m.last_error()}")
        price = float(tick.bid) if close_type == m.ORDER_TYPE_SELL else float(tick.ask)
        request = {
            "action": m.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 10,
            "magic": MAGIC_NUMBER,
            "comment": "forex-agent close",
            "type_time": m.ORDER_TIME_GTC,
            "type_filling": m.ORDER_FILLING_IOC,
        }
        result = m.order_send(request)
        if result is None or result.retcode != m.TRADE_RETCODE_DONE:
            raise BrokerError(
                INVALID_ORDER,
                f"Close failed for ticket {ticket}: {m.last_error()}",
            )
        logger.info("Closed ticket %s (%s) @ %.5f", ticket, pos.symbol, price)
        return price

    # -- history --------------------------------------------------------------
    def deal_history(
        self, from_: datetime, to: datetime, position_id: Optional[int] = None
    ) -> List[Deal]:
        if self._transport is not None:
            return self._ensure_remote().deal_history(from_, to, position_id)
        m = self._ensure()
        if position_id is not None:
            raw = m.history_deals_get(position=position_id)
        else:
            raw = m.history_deals_get(from_, to)
        if raw is None:
            return []

        def entry_of(d) -> str:
            if d.entry == m.DEAL_ENTRY_IN:
                return "IN"
            if d.entry == m.DEAL_ENTRY_OUT:
                return "OUT"
            return "INOUT"

        return [
            Deal(
                ticket=int(d.ticket),
                position_id=int(d.position_id),
                symbol=d.symbol,
                direction="BUY" if d.type == m.DEAL_TYPE_BUY else "SELL",
                entry=entry_of(d),
                volume=float(d.volume),
                price=float(d.price),
                profit=float(d.profit),
                commission=float(d.commission),
                swap=float(d.swap),
                magic=int(d.magic),
                comment=d.comment or "",
                time=datetime.fromtimestamp(d.time),
            )
            for d in raw
        ]

    # -- health -----------------------------------------------------------------
    def health(self) -> BrokerHealth:
        if self._transport is not None:
            try:
                up = self._transport.ping()
            except Exception as exc:  # never let health checks raise
                return BrokerHealth(connected=False, adapter=self.adapter_name,
                                    message=f"gateway error: {exc}")
            return BrokerHealth(
                connected=bool(self._connected and up),
                adapter=self.adapter_name,
                server_time=datetime.now(),
                message="" if (self._connected and up) else
                        ("gateway reachable, not connected" if up
                         else "gateway unreachable"),
            )
        if mt5 is None:
            return BrokerHealth(
                connected=False, adapter=self.adapter_name,
                message="MetaTrader5 package not installed on this machine.",
            )
        try:
            info = mt5.account_info() if self._connected else None
            return BrokerHealth(
                connected=self._connected and info is not None,
                adapter=self.adapter_name,
                server_time=datetime.now(),
                message="" if self._connected else "Not connected.",
            )
        except Exception as exc:  # never let health checks raise
            return BrokerHealth(connected=False, adapter=self.adapter_name, message=str(exc))

    # -- structured runtime status ------------------------------------------
    def broker_status(self) -> dict:
        """Transport-aware structured status. Never raises. Field docs:
        broker/mt5/RUNTIME.md."""
        try:
            return {"broker": self._mt5_status()}
        except Exception as exc:  # defensive: status must never raise
            return {"broker": {
                "provider": "mt5", "mode": self.transport_mode,
                "configured": True, "reachable": False, "connected": False,
                "account_available": False, "market_data_available": False,
                "trading_available": False,
                "detail": {"probe_error": str(exc)[:200]},
            }}

    def _mt5_status(self) -> dict:
        status = {
            "provider": "mt5",
            "mode": self.transport_mode,
            "configured": True,
            "reachable": False,
            "connected": False,
            "account_available": False,
            "market_data_available": False,
            "trading_available": False,
            "detail": {},
        }
        if self._transport is not None:
            return self._remote_status(status)
        # -- local path --------------------------------------------------
        if mt5 is None:
            status["detail"]["reason"] = (
                "MetaTrader5 package not installed on this machine "
                "(see broker/mt5/RUNTIME.md)")
            return status
        status["reachable"] = True
        if sys.platform != "win32":
            status["detail"]["platform"] = (
                f"{sys.platform}: no Windows terminal runtime declared "
                "(set MT5_ALLOW_NONWINDOWS_RUNTIME=1 for Wine)")
        if not self._connected:
            status["detail"]["reason"] = "runtime present, not connected (call connect())"
            return status
        try:
            self.account_info()
            status["account_available"] = True
            status["connected"] = True
        except BrokerError as exc:
            status["detail"]["account"] = exc.message
            return status
        try:
            if self.symbols():
                status["market_data_available"] = True
        except BrokerError as exc:
            status["detail"]["market_data"] = exc.message
        try:
            ti = mt5.terminal_info()
            status["trading_available"] = bool(
                ti is not None and getattr(ti, "trade_allowed", False))
            if not status["trading_available"]:
                status["detail"]["trading"] = "terminal reports trade_allowed=false"
        except Exception as exc:
            status["detail"]["trading"] = f"terminal_info failed: {exc}"[:120]
        return status

    def _remote_status(self, status: dict) -> dict:
        t = self._transport
        try:
            reachable = bool(t.ping())
        except BrokerError as exc:
            status["detail"]["reason"] = f"gateway unreachable: {exc.message}"
            return status
        status["reachable"] = reachable
        if not reachable:
            status["detail"]["reason"] = "gateway unreachable (see broker/mt5/RUNTIME.md)"
            return status
        if not self._connected:
            status["detail"]["reason"] = "gateway reachable, not connected (call connect())"
            return status
        status["connected"] = True
        try:
            t.account_info()
            status["account_available"] = True
        except BrokerError as exc:
            status["detail"]["account"] = exc.message
        try:
            if t.symbols():
                status["market_data_available"] = True
        except BrokerError as exc:
            status["detail"]["market_data"] = exc.message
        try:
            term = t.terminal_status()
            status["trading_available"] = bool(term.get("trade_allowed", False))
            if not status["trading_available"]:
                status["detail"]["trading"] = "terminal reports trade_allowed=false"
        except BrokerError as exc:
            status["detail"]["trading"] = exc.message
        return status
