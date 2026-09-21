"""
Execution Gateway — the deterministic choke point for every trade.

Flow: agent -> forex.request_trade -> gateway -> risk checks -> safety
checks -> BrokerAdapter.submit_order -> broker confirmation -> event.

THE AGENT CAN NEVER CALL THE ADAPTER DIRECTLY. There is no path from
any agent/daemon/CLI input to submit_order() that does not pass through
request_trade().

Enforced, in order:
  1. Idempotency — a repeated idempotency_key returns the original
     decision; an already-executed signal_id is rejected as duplicate.
  2. Kill switch engaged -> block (KILL_SWITCH_ENGAGED).
  3. Symbol whitelist (INVALID_SYMBOL).
  4. Risk engine: mandatory SL, max risk/trade, max daily loss on
     EQUITY, max open positions (own), max total exposure + correlation
     limits, volume clamped to broker min/max/step.
  5. Safety: market-open (tick freshness), spread cap, account minimum.
  6. Dry-run -> block BEFORE submit_order (DRY_RUN_BLOCKED). Live
     trading is impossible without an explicit config change to
     mode: live. In dry-run the full check pipeline still runs so the
     audit log shows exactly what would have happened.
  7. BrokerAdapter.submit_order — broker-confirmed only. A rejection or
     transport error becomes a structured rejection, never a silent fill.

Every decision is audit-logged: requested -> risk inputs ->
approved/rejected (+reason code) -> broker result. Audit goes to the
local store (store.audit); when the store is unavailable the decision
is still returned but marked audit_pending.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from broker import (
    BrokerAdapter,
    BrokerError,
    BrokerHealth,
    OrderRequest,
    INVALID_ORDER,
    INVALID_SYMBOL,
    MARKET_CLOSED,
    RISK_LIMIT_EXCEEDED,
)
from config import AppConfig
from core import events as events_mod
from core.execution.kill_switch import KillSwitch
from core.risk import DRY_RUN_BLOCKED, KILL_SWITCH_ENGAGED, RiskManager
from core.signals import Signal

logger = logging.getLogger("execution")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TradeRequest:
    signal_id: str
    symbol: str
    direction: str  # "BUY" or "SELL"
    stop_loss: float
    take_profit: float
    entry_price: Optional[float] = None
    volume: Optional[float] = None  # informational; the risk engine sizes
    idempotency_key: Optional[str] = None
    source: str = "agent"  # agent | daemon | cli
    requested_at: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            self.idempotency_key = str(uuid.uuid4())
        if not self.requested_at:
            self.requested_at = _utcnow()

    @classmethod
    def from_signal(cls, signal: Signal, source: str = "agent") -> "TradeRequest":
        key = f"{signal.id or 'sig'}:{signal.candle_time.isoformat()}"
        return cls(
            signal_id=signal.id or key,
            symbol=signal.symbol,
            direction=signal.direction,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            entry_price=signal.entry_price,
            idempotency_key=f"signal:{key}",
            source=source,
        )


@dataclass
class GatewayDecision:
    approved: bool
    reason: str
    reason_code: str
    idempotency_key: str
    volume: float = 0.0
    ticket: Optional[int] = None
    price: Optional[float] = None
    audit_id: Optional[int] = None
    audit_pending: bool = False
    notes: List[str] = field(default_factory=list)


class ExecutionGateway:
    """Deterministic trade gate. Construct once per process."""

    def __init__(
        self,
        config: AppConfig,
        adapter: BrokerAdapter,
        risk_manager: RiskManager,
        kill_switch: KillSwitch,
        store=None,
    ):
        self.config = config
        self.adapter = adapter
        self.risk = risk_manager
        self.kill_switch = kill_switch
        self._store = store
        self._decisions: Dict[str, GatewayDecision] = {}  # idempotency_key -> decision
        self._executed_signal_ids: set = set()

    # -- audit ------------------------------------------------------------
    def _audit(self, record: dict) -> Optional[int]:
        record = {"ts": _utcnow(), "kind": "trade_decision", **record}
        if self._store is None:
            return None
        try:
            return self._store.audit(record)
        except Exception:
            logger.exception("Audit write failed.")
            return None

    # -- main entry ---------------------------------------------------------
    def request_trade(self, req: TradeRequest) -> GatewayDecision:
        # 1. Idempotency: exact key replay returns the ORIGINAL decision.
        if req.idempotency_key in self._decisions:
            prior = self._decisions[req.idempotency_key]
            logger.info("Duplicate idempotency key %s — returning original decision.",
                        req.idempotency_key)
            return prior
        # Duplicate-trade prevention: one fill per signal, ever.
        if req.signal_id in self._executed_signal_ids:
            return self._reject(req, False, "Signal already executed — duplicate trade prevented",
                                INVALID_ORDER, volume=0.0)

        # 2. Kill switch — below any agent layer, no bypass path.
        if self.kill_switch.is_engaged():
            self._risk_blocked_event(req, KILL_SWITCH_ENGAGED, "kill switch engaged")
            return self._reject(req, False, "Kill switch is engaged — no new entries allowed",
                                KILL_SWITCH_ENGAGED)

        # 3. Symbol whitelist.
        if req.symbol not in self.config.trading.symbols:
            return self._reject(req, False, f"Symbol {req.symbol} not in whitelist",
                                INVALID_SYMBOL)

        # 4. Risk engine (mandatory SL, equity daily-loss, own-position
        #    counts, exposure + correlation, broker volume grid).
        signal = Signal(
            symbol=req.symbol, timeframe=self.config.trading.timeframe,
            direction=req.direction, entry_price=req.entry_price or 0.0,
            stop_loss=req.stop_loss, take_profit=req.take_profit,
            ema_fast=0.0, ema_slow=0.0, rsi_value=0.0, atr_value=0.0,
            candle_time=datetime.now(timezone.utc),
            trigger=f"gateway trade request ({req.source})",
            strategy="gateway",
            id=req.signal_id,
        )
        risk_inputs: dict = {}
        try:
            account = self.adapter.account_info()
            risk_inputs = {"equity": account.equity, "balance": account.balance,
                           "currency": account.currency}
            result = self.risk.check(signal, self.adapter, kill_switch_engaged=False)
        except BrokerError as exc:
            return self._reject(req, False, f"Risk inputs unavailable: {exc.message}",
                                exc.code, risk_inputs=risk_inputs)
        risk_inputs["risk_lot_size"] = result.lot_size
        risk_inputs["risk_notes"] = result.notes
        if not result.allowed:
            self._risk_blocked_event(req, result.reason_code, result.reason)
            return self._reject(req, False, result.reason, result.reason_code,
                                volume=result.lot_size, notes=result.notes,
                                risk_inputs=risk_inputs)

        # trade.requested is emitted once risk + sizing are known, so the
        # event carries the real (schema-required) volume. Earlier blocks
        # are observable via risk.blocked / trade.rejected + the audit log.
        events_mod.emit({"event": "trade.requested",
                         "symbol": req.symbol, "side": req.direction,
                         "volume": result.lot_size,
                         "idempotency_key": req.idempotency_key,
                         "signal_id": req.signal_id,
                         "stop_loss": req.stop_loss,
                         "take_profit": req.take_profit,
                         "requested_by": req.source})

        # 5. Safety checks: market-open, spread cap, account minimum.
        safety = self._safety_checks(req, risk_inputs)
        if safety is not None:
            return safety  # already a rejection

        # 6. Dry-run: the full pipeline ran above; NOTHING live leaves here.
        if self.config.dry_run:
            return self._reject(req, False,
                                "dry_run mode: live orders blocked (set mode: live to trade)",
                                DRY_RUN_BLOCKED, volume=result.lot_size,
                                notes=result.notes, risk_inputs=risk_inputs)

        # 7. Broker submission — confirmed fills only.
        order_req = OrderRequest(
            symbol=req.symbol, direction=req.direction, volume=result.lot_size,
            stop_loss=req.stop_loss, take_profit=req.take_profit,
            signal_id=req.signal_id, idempotency_key=req.idempotency_key,
        )
        try:
            fill = self.adapter.submit_order(order_req)
        except BrokerError as exc:
            logger.warning("Broker rejected order for %s: [%s] %s",
                           req.symbol, exc.code, exc.message)
            return self._reject(req, False, f"Broker rejected order: {exc.message}",
                                exc.code, volume=result.lot_size,
                                notes=result.notes, risk_inputs=risk_inputs)

        self._executed_signal_ids.add(req.signal_id)
        decision = GatewayDecision(
            approved=True, reason="Order confirmed by broker",
            reason_code="", idempotency_key=req.idempotency_key,
            volume=fill.volume, ticket=fill.ticket, price=fill.price,
            notes=result.notes,
        )
        self._decisions[req.idempotency_key] = decision
        decision.audit_id = self._audit({
            "idempotency_key": req.idempotency_key, "signal_id": req.signal_id,
            "symbol": req.symbol, "direction": req.direction,
            "source": req.source, "approved": True, "reason": decision.reason,
            "reason_code": "", "volume": fill.volume,
            "risk_inputs": risk_inputs,
            "broker": {"ticket": fill.ticket, "price": fill.price,
                       "retcode": fill.retcode},
        })
        decision.audit_pending = decision.audit_id is None and self._store is not None
        events_mod.emit({"event": "trade.executed",
                         "symbol": req.symbol, "ticket": fill.ticket,
                         "side": req.direction, "volume": fill.volume,
                         "idempotency_key": req.idempotency_key,
                         "price": fill.price, "signal_id": req.signal_id})
        logger.info("Trade approved+confirmed: %s %s %.2f lots ticket=%s",
                    req.direction, req.symbol, fill.volume, fill.ticket)
        return decision

    # -- position management ------------------------------------------------
    # Closes and SL/TP modifies also pass through the gateway (never direct
    # adapter calls from agent/daemon/CLI). Closing is risk-reducing, so it
    # is allowed even while the kill switch is engaged. Modifies must keep a
    # mandatory stop-loss: an SL can be tightened but never removed.
    def close_position(self, ticket, source: str = "agent") -> dict:
        try:
            ticket = int(ticket)
        except (TypeError, ValueError):
            return self._action_result(False, "INVALID_TICKET",
                                       f"ticket must be an integer, got {ticket!r}")
        symbol = "unknown"
        try:
            for pos in self.adapter.positions():
                if pos.ticket == ticket:
                    symbol = pos.symbol
                    break
        except Exception:
            logger.debug("positions() lookup failed before close", exc_info=True)
        try:
            close_price = self.adapter.close_position(ticket)
        except BrokerError as exc:
            self._audit({"ts": _utcnow(), "kind": "close_position",
                         "ticket": ticket, "symbol": symbol, "source": source,
                         "approved": False, "reason": exc.message,
                         "reason_code": exc.code})
            return self._action_result(False, exc.code,
                                       f"Broker refused close of ticket {ticket}: {exc.message}")
        self._audit({"ts": _utcnow(), "kind": "close_position",
                     "ticket": ticket, "symbol": symbol, "source": source,
                     "approved": True, "reason": "broker-confirmed close",
                     "close_price": close_price})
        events_mod.emit({"event": "position.closed", "ticket": ticket,
                         "symbol": symbol, "reason": f"closed via gateway by {source}",
                         "close_price": close_price})
        logger.info("Position closed: ticket=%s %s @ %s", ticket, symbol, close_price)
        return self._action_result(True, "", "broker-confirmed close",
                                   ticket=ticket, symbol=symbol,
                                   close_price=close_price)

    def modify_position(self, ticket, stop_loss=None, take_profit=None,
                        source: str = "agent") -> dict:
        try:
            ticket = int(ticket)
        except (TypeError, ValueError):
            return self._action_result(False, "INVALID_TICKET",
                                       f"ticket must be an integer, got {ticket!r}")
        if stop_loss is None and take_profit is None:
            return self._action_result(False, "INVALID_ORDER",
                                       "nothing to modify: supply stop_loss and/or take_profit")
        # Mandatory-SL rule: an SL can be moved but never removed.
        if stop_loss is not None and stop_loss <= 0:
            return self._action_result(False, "INVALID_ORDER",
                                       "stop_loss is mandatory and must stay positive")
        symbol = "unknown"
        broker_down = None
        pos_direction = None
        pos_sl = None
        try:
            open_positions = self.adapter.positions()
        except BrokerError as exc:
            broker_down = exc
            open_positions = []
        except Exception:
            logger.debug("positions() lookup failed before modify", exc_info=True)
            open_positions = []
        for pos in open_positions:
            if pos.ticket == ticket:
                symbol = pos.symbol
                pos_direction = pos.direction
                pos_sl = pos.sl
                if stop_loss is None:
                    stop_loss = pos.sl
                if take_profit is None:
                    take_profit = pos.tp
                break
        else:
            if broker_down is not None:
                return self._action_result(False, broker_down.code,
                                           f"Cannot verify ticket {ticket}: {broker_down.message}")
            return self._action_result(False, "INVALID_ORDER",
                                       f"ticket {ticket} not found among open positions")
        if stop_loss <= 0:
            return self._action_result(False, "INVALID_ORDER",
                                       "resulting stop_loss must stay positive (mandatory SL)")
        if pos_sl and pos_sl > 0:
            # Direction-aware tightening: a BUY stop may only move up, a
            # SELL stop may only move down. Loosening is rejected.
            if pos_direction == "BUY" and stop_loss < pos_sl:
                return self._action_result(False, "INVALID_ORDER",
                                           "stop_loss may be tightened but never loosened")
            if pos_direction == "SELL" and stop_loss > pos_sl:
                return self._action_result(False, "INVALID_ORDER",
                                           "stop_loss may be tightened but never loosened")
        try:
            self.adapter.modify_order(ticket, stop_loss, take_profit)
        except BrokerError as exc:
            self._audit({"ts": _utcnow(), "kind": "modify_position",
                         "ticket": ticket, "symbol": symbol, "source": source,
                         "approved": False, "reason": exc.message,
                         "reason_code": exc.code})
            return self._action_result(False, exc.code,
                                       f"Broker refused modify of ticket {ticket}: {exc.message}")
        self._audit({"ts": _utcnow(), "kind": "modify_position",
                     "ticket": ticket, "symbol": symbol, "source": source,
                     "approved": True, "reason": "broker-confirmed modify",
                     "stop_loss": stop_loss, "take_profit": take_profit})
        events_mod.emit({"event": "position.modified", "ticket": ticket,
                         "symbol": symbol, "stop_loss": stop_loss,
                         "take_profit": take_profit})
        logger.info("Position modified: ticket=%s SL=%s TP=%s", ticket, stop_loss, take_profit)
        return self._action_result(True, "", "broker-confirmed modify",
                                   ticket=ticket, symbol=symbol,
                                   stop_loss=stop_loss, take_profit=take_profit)

    def _action_result(self, ok: bool, code: str, message: str, **extra) -> dict:
        result = {"ok": ok, "error_code": code, "message": message}
        result.update(extra)
        return result

    # -- helpers --------------------------------------------------------------
    def _safety_checks(self, req: TradeRequest, risk_inputs: dict) -> Optional[GatewayDecision]:
        # Market-open via tick freshness (the original
        # is_symbol_likely_tradeable_now heuristic, now behind the adapter).
        quote = None
        try:
            quote = self.adapter.quote(req.symbol)
        except Exception:
            logger.debug("quote() unavailable for %s", req.symbol, exc_info=True)
        if quote is not None:
            tick_time = quote.time
            if tick_time.tzinfo is None:
                tick_time = tick_time.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - tick_time).total_seconds()
            risk_inputs["tick_age_seconds"] = age
            if age > 300:
                return self._reject(req, False,
                                    f"Market appears closed for {req.symbol} (tick age {age:.0f}s)",
                                    MARKET_CLOSED, risk_inputs=risk_inputs)
            max_spread = self.config.risk.max_spread_points
            if max_spread > 0 and quote.spread > max_spread:
                return self._reject(req, False,
                                    f"Spread {quote.spread} exceeds max {max_spread} for {req.symbol}",
                                    RISK_LIMIT_EXCEEDED, risk_inputs=risk_inputs)
            risk_inputs["spread"] = quote.spread
        else:
            risk_inputs["quote"] = "unavailable — spread/market-open checks skipped"

        min_equity = self.config.risk.min_account_equity
        equity = risk_inputs.get("equity", 0)
        if min_equity > 0 and equity < min_equity:
            return self._reject(req, False,
                                f"Account equity {equity:.2f} below minimum {min_equity:.2f}",
                                RISK_LIMIT_EXCEEDED, risk_inputs=risk_inputs)
        return None

    def _risk_blocked_event(self, req: TradeRequest, code: str, reason: str) -> None:
        events_mod.emit({"event": "risk.blocked", "reason": code,
                         "symbol": req.symbol, "signal_id": req.signal_id,
                         "detail": {"message": reason,
                                    "idempotency_key": req.idempotency_key}})

    def _reject(self, req: TradeRequest, approved: bool, reason: str, code: str,
                volume: float = 0.0, notes: Optional[list] = None,
                risk_inputs: Optional[dict] = None) -> GatewayDecision:
        events_mod.emit({"event": "trade.rejected", "reason": code,
                         "symbol": req.symbol, "signal_id": req.signal_id,
                         "detail": {"message": reason, "volume": volume,
                                    "idempotency_key": req.idempotency_key,
                                    "source": req.source}})
        decision = GatewayDecision(
            approved=False, reason=reason, reason_code=code,
            idempotency_key=req.idempotency_key, volume=volume,
            notes=notes or [],
        )
        # Cache idempotency rejections too — a replayed key must return the
        # identical decision, approved or not.
        self._decisions[req.idempotency_key] = decision
        decision.audit_id = self._audit({
            "idempotency_key": req.idempotency_key, "signal_id": req.signal_id,
            "symbol": req.symbol, "direction": req.direction,
            "source": req.source, "approved": False, "reason": reason,
            "reason_code": code, "volume": volume,
            "risk_inputs": risk_inputs or {},
        })
        decision.audit_pending = decision.audit_id is None and self._store is not None
        logger.info("Trade rejected [%s]: %s", code, reason)
        return decision
