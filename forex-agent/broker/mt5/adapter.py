"""
MT5Adapter — implements BrokerAdapter against a local MT5 terminal.

This is the ONLY place `import MetaTrader5` may appear in the whole
project. It absorbs BOTH original dialects:
  * MT5Client methods (connect, candles, balance, positions, specs,
    market orders) from mt5_client.py
  * the direct mt5.* calls from exit_manager.py (SL modify, close),
    reconciliation.py (positions_get, history_deals_get) and
    performance_review.py / cost_tracking.py (deal history)

Graceful degradation: if the MetaTrader5 package is missing (Linux),
importing this module still works; every broker operation raises
BrokerError(BROKER_UNAVAILABLE) via _require_mt5(). Never raises at
import time.
"""

import logging
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
    INVALID_ORDER,
    INVALID_SYMBOL,
    MARKET_CLOSED,
    MT5_NOT_CONNECTED,
    CONFIG_INVALID,
)
from broker.mt5.shared import extract_signal_id_from_comment  # noqa: F401  (kept for adapter-internal use)
from core.market import Candle

logger = logging.getLogger("broker.mt5")


def _require_mt5():
    if mt5 is None:
        raise BrokerError(
            BROKER_UNAVAILABLE,
            "The MetaTrader5 package is not installed on this machine "
            "(Windows + MT5 terminal required). Core runs brokerless; "
            "use DisconnectedAdapter or connect a broker host.",
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

    def __init__(self):
        self._connected = False
        self._login: Optional[int] = None
        self._server: str = ""

    # -- lifecycle ------------------------------------------------------
    def connect(self, creds: dict) -> None:
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
