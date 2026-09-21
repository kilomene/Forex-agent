"""
Execution Gateway — the deterministic choke point for every trade.

Flow: agent -> forex.request_trade -> gateway -> risk checks -> safety
checks -> BrokerAdapter.submit_order -> broker confirmation -> event.

THE AGENT CAN NEVER CALL THE ADAPTER DIRECTLY. There is no path from
any agent/daemon/CLI input to submit_order()/modify_order()/close_position()
that does not pass through the gateway. This is enforced at runtime by
core.execution.broker_guard.GatewayOnlyAdapter: agent/tools/backend.py
hands every consumer the write-guarded proxy, and the proxy's
trade-affecting methods raise GATEWAY_BYPASS_ATTEMPTED unless the calling
thread is inside execution_scope() — a scope only the gateway (and the
kill-switch close-all sweep) enters.

Enforced, in order:
  1. Idempotency — every request carries a request_id (uuid). A repeated
     request_id returns the ORIGINAL decision/result from the durable
     idempotency store (SQLite: execution_idempotency) — no double
     execution, no double modify, no double close. The legacy
     idempotency_key replay path is kept for request_trade.
     An already-executed signal_id is rejected as duplicate.
  2. Kill switch engaged -> block (KILL_SWITCH_ENGAGED). Applies to
     request_trade and modify_position. close_position stays allowed
     while engaged: closing reduces risk, never increases it.
  3. Symbol whitelist (INVALID_SYMBOL).
  4. Risk engine: mandatory SL, max risk/trade, max daily loss on
     EQUITY, max open positions (own), max total exposure + correlation
     limits, volume clamped to broker min/max/step.
  5. Safety: market-open (tick freshness), spread cap, account minimum.
  6. Dry-run -> block BEFORE submit_order (DRY_RUN_BLOCKED). Live
     trading is impossible without an explicit config change to
     mode: live. In dry-run the full check pipeline still runs so the
     audit log shows exactly what would have happened; the reported
     state is dry_run_simulated — never "executed".
  7. BrokerAdapter.submit_order — broker-confirmed only. A rejection or
     transport error becomes a structured rejection, never a silent fill.

Trade-request lifecycle (tracked per request_id, every transition
audit-logged):
    requested -> validated -> risk_checked -> approved -> submitted
        -> broker_acknowledged -> executed
Terminal states: rejected, failed, dry_run_simulated,
duplicate_suppressed. A trade is reported "executed" ONLY after the
broker confirms the fill; dry-run or unconfirmed submissions report
their true state.

modify_position / close_position follow the same lifecycle
(requested -> validated -> approved -> submitted -> broker_acknowledged
-> executed) and the same idempotency rule. Validation failures return
structured errors (INVALID_TICKET, POSITION_NOT_FOUND, INVALID_SL_TP,
INVALID_ORDER, KILL_SWITCH_ENGAGED, BROKER_UNAVAILABLE), never silent.

Every decision and every state transition is audit-logged: the audit
trail is the durable record of the lifecycle. Audit goes to the local
store (store.audit); when the store is unavailable the decision is
still returned but marked audit_pending.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from broker import (
    BROKER_UNAVAILABLE,
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
from core.execution.broker_guard import execution_scope
from core.execution.kill_switch import KillSwitch
from core.risk import DRY_RUN_BLOCKED, KILL_SWITCH_ENGAGED, RiskManager
from core.signals import Signal

logger = logging.getLogger("execution")

# Structured error codes raised/returned by the gateway itself.
POSITION_NOT_FOUND = "POSITION_NOT_FOUND"
INVALID_SL_TP = "INVALID_SL_TP"

# Lifecycle states -----------------------------------------------------------
ST_REQUESTED = "requested"
ST_VALIDATED = "validated"
ST_RISK_CHECKED = "risk_checked"
ST_APPROVED = "approved"
ST_SUBMITTED = "submitted"
ST_BROKER_ACK = "broker_acknowledged"
ST_EXECUTED = "executed"
# Terminal non-success states:
ST_REJECTED = "rejected"
ST_FAILED = "failed"
ST_DRY_RUN = "dry_run_simulated"
ST_DUPLICATE = "duplicate_suppressed"


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
    request_id: Optional[str] = None  # uuid; idempotency identity for ALL ops
    source: str = "agent"  # agent | daemon | cli
    requested_at: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            self.idempotency_key = str(uuid.uuid4())
        if not self.request_id:
            self.request_id = uuid.uuid4().hex
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
    request_id: str = ""
    state: str = ST_REQUESTED  # lifecycle state; "executed" ONLY after broker confirmation
    volume: float = 0.0
    ticket: Optional[int] = None
    price: Optional[float] = None
    audit_id: Optional[int] = None
    audit_pending: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved, "reason": self.reason,
            "reason_code": self.reason_code,
            "idempotency_key": self.idempotency_key,
            "request_id": self.request_id, "state": self.state,
            "volume": self.volume, "ticket": self.ticket,
            "price": self.price, "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GatewayDecision":
        return cls(
            approved=bool(data.get("approved", False)),
            reason=str(data.get("reason", "")),
            reason_code=str(data.get("reason_code", "")),
            idempotency_key=str(data.get("idempotency_key", "")),
            request_id=str(data.get("request_id", "")),
            state=str(data.get("state", ST_REQUESTED)),
            volume=float(data.get("volume", 0.0) or 0.0),
            ticket=data.get("ticket"), price=data.get("price"),
            notes=list(data.get("notes", []) or []),
        )


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
        # request_id -> durable idempotency record (memory cache; the
        # store is the source of truth across restarts).
        self._idem: Dict[str, dict] = {}
        # request_id -> ordered lifecycle transition list.
        self._lifecycles: Dict[str, List[dict]] = {}

    # -- audit ----------------------------------------------------------------
    def _audit(self, record: dict) -> Optional[int]:
        record = {"ts": _utcnow(), **record}
        if self._store is None:
            return None
        try:
            return self._store.audit(record)
        except Exception:
            logger.exception("Audit write failed.")
            return None

    # -- lifecycle --------------------------------------------------------------
    def _transition(self, request_id: str, operation: str, state: str,
                    symbol: Optional[str] = None, detail: Optional[dict] = None) -> None:
        """Record one lifecycle transition: in-memory + audit log."""
        entry = {"ts": _utcnow(), "state": state,
                 "detail": detail or {}}
        self._lifecycles.setdefault(request_id, []).append(entry)
        self._audit({"kind": "trade_state", "request_id": request_id,
                     "operation": operation, "state": state,
                     "symbol": symbol, "detail": detail or {}})

    def lifecycle(self, request_id: str) -> List[dict]:
        """Ordered state transitions recorded for a request_id (this process)."""
        return list(self._lifecycles.get(request_id, []))

    # -- durable idempotency ----------------------------------------------------
    def _idem_lookup(self, request_id: str) -> Optional[dict]:
        if request_id in self._idem:
            return self._idem[request_id]
        store = self._store
        if store is not None and hasattr(store, "idem_get"):
            try:
                rec = store.idem_get(request_id)
            except Exception:
                logger.exception("Idempotency lookup failed.")
                rec = None
            if rec:
                self._idem[request_id] = rec
                return rec
        return None

    def _idem_store(self, request_id: str, operation: str, payload: dict) -> None:
        record = {"request_id": request_id, "operation": operation,
                  "decision": payload, "created_at": _utcnow()}
        self._idem[request_id] = record
        store = self._store
        if store is not None and hasattr(store, "idem_put"):
            try:
                store.idem_put(request_id, operation, payload)
            except Exception:
                logger.exception("Idempotency persist failed.")

    # -- main entry ---------------------------------------------------------------
    def request_trade(self, req: TradeRequest) -> GatewayDecision:
        op = "request_trade"
        if not req.request_id:
            req.request_id = uuid.uuid4().hex
        rid = req.request_id
        self._transition(rid, op, ST_REQUESTED, symbol=req.symbol,
                         detail={"signal_id": req.signal_id, "source": req.source,
                                 "idempotency_key": req.idempotency_key})

        # 1a. Idempotency on request_id (durable): a repeated request_id
        # returns the ORIGINAL decision — never a second execution.
        prior = self._idem_lookup(rid)
        if prior is not None and prior.get("operation") == op:
            self._transition(rid, op, ST_DUPLICATE, symbol=req.symbol,
                             detail={"replayed": True})
            logger.info("Duplicate request_id %s — returning original decision.", rid)
            return GatewayDecision.from_dict(prior["decision"])

        # 1b. Legacy idempotency-key replay returns the ORIGINAL decision.
        if req.idempotency_key in self._decisions:
            original = self._decisions[req.idempotency_key]
            self._idem_store(rid, op, original.to_dict())
            self._transition(rid, op, ST_DUPLICATE, symbol=req.symbol,
                             detail={"idempotency_key": req.idempotency_key})
            logger.info("Duplicate idempotency key %s — returning original decision.",
                        req.idempotency_key)
            return original

        # Duplicate-trade prevention: one fill per signal, ever.
        if req.signal_id in self._executed_signal_ids:
            return self._reject(req, op, "Signal already executed — duplicate trade prevented",
                                INVALID_ORDER, volume=0.0)

        # 2. Kill switch — below any agent layer, no bypass path.
        if self.kill_switch.is_engaged():
            self._risk_blocked_event(req, KILL_SWITCH_ENGAGED, "kill switch engaged")
            return self._reject(req, op, "Kill switch is engaged — no new entries allowed",
                                KILL_SWITCH_ENGAGED)
        self._transition(rid, op, ST_VALIDATED, symbol=req.symbol)

        # 3. Symbol whitelist.
        if req.symbol not in self.config.trading.symbols:
            return self._reject(req, op, f"Symbol {req.symbol} not in whitelist",
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
            return self._reject(req, op, f"Risk inputs unavailable: {exc.message}",
                                exc.code, risk_inputs=risk_inputs)
        risk_inputs["risk_lot_size"] = result.lot_size
        risk_inputs["risk_notes"] = result.notes
        if not result.allowed:
            self._risk_blocked_event(req, result.reason_code, result.reason)
            return self._reject(req, op, result.reason, result.reason_code,
                                volume=result.lot_size, notes=result.notes,
                                risk_inputs=risk_inputs)
        self._transition(rid, op, ST_RISK_CHECKED, symbol=req.symbol,
                         detail={"lot_size": result.lot_size})

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
        # The reported state is dry_run_simulated — never "executed".
        if self.config.dry_run:
            return self._reject(req, op,
                                "dry_run mode: live orders blocked (set mode: live to trade)",
                                DRY_RUN_BLOCKED, volume=result.lot_size,
                                notes=result.notes, risk_inputs=risk_inputs,
                                state=ST_DRY_RUN)

        # 7. Broker submission — confirmed fills only.
        self._transition(rid, op, ST_APPROVED, symbol=req.symbol,
                         detail={"lot_size": result.lot_size})
        order_req = OrderRequest(
            symbol=req.symbol, direction=req.direction, volume=result.lot_size,
            stop_loss=req.stop_loss, take_profit=req.take_profit,
            signal_id=req.signal_id, idempotency_key=req.idempotency_key,
        )
        self._transition(rid, op, ST_SUBMITTED, symbol=req.symbol)
        try:
            # execution_scope: the gateway is the only writer the guard allows.
            with execution_scope():
                fill = self.adapter.submit_order(order_req)
        except BrokerError as exc:
            logger.warning("Broker rejected order for %s: [%s] %s",
                           req.symbol, exc.code, exc.message)
            return self._reject(req, op, f"Broker rejected order: {exc.message}",
                                exc.code, volume=result.lot_size,
                                notes=result.notes, risk_inputs=risk_inputs,
                                state=ST_FAILED)

        self._transition(rid, op, ST_BROKER_ACK, symbol=req.symbol,
                         detail={"ticket": fill.ticket, "price": fill.price})
        self._executed_signal_ids.add(req.signal_id)
        decision = GatewayDecision(
            approved=True, reason="Order confirmed by broker",
            reason_code="", idempotency_key=req.idempotency_key,
            request_id=rid, state=ST_EXECUTED,
            volume=fill.volume, ticket=fill.ticket, price=fill.price,
            notes=result.notes,
        )
        self._decisions[req.idempotency_key] = decision
        self._idem_store(rid, op, decision.to_dict())
        self._transition(rid, op, ST_EXECUTED, symbol=req.symbol,
                         detail={"ticket": fill.ticket, "price": fill.price})
        decision.audit_id = self._audit({
            "kind": "trade_decision",
            "idempotency_key": req.idempotency_key, "request_id": rid,
            "signal_id": req.signal_id,
            "symbol": req.symbol, "direction": req.direction,
            "source": req.source, "approved": True, "reason": decision.reason,
            "reason_code": "", "state": ST_EXECUTED, "volume": fill.volume,
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

    # -- position management ------------------------------------------------------
    # Closes and SL/TP modifies also pass through the gateway (never direct
    # adapter calls from agent/daemon/CLI). Closing is risk-reducing, so it
    # is allowed even while the kill switch is engaged. Modifies are blocked
    # while the kill switch is engaged. Every modify/close carries a
    # request_id: a repeated request_id returns the ORIGINAL result — no
    # double modify, no double close.
    def close_position(self, ticket, source: str = "agent",
                       request_id: Optional[str] = None) -> dict:
        op = "close_position"
        rid = request_id or uuid.uuid4().hex
        self._transition(rid, op, ST_REQUESTED, detail={"ticket": ticket,
                                                        "source": source})
        prior = self._idem_lookup(rid)
        if prior is not None and prior.get("operation") == op:
            self._transition(rid, op, ST_DUPLICATE,
                             detail={"replayed": True})
            logger.info("Duplicate close request_id %s — returning original result.", rid)
            return dict(prior["decision"])

        try:
            ticket = int(ticket)
        except (TypeError, ValueError):
            return self._action_reject(rid, op, "INVALID_TICKET",
                                       f"ticket must be an integer, got {ticket!r}")
        # Closing reduces risk — allowed even while the kill switch is engaged.
        symbol = "unknown"
        try:
            for pos in self.adapter.positions():
                if pos.ticket == ticket:
                    symbol = pos.symbol
                    break
            else:
                return self._action_reject(
                    rid, op, INVALID_ORDER,
                    f"ticket {ticket} not found among open positions",
                    symbol=symbol)
        except BrokerError as exc:
            return self._action_reject(rid, op, exc.code,
                                       f"Cannot list positions: {exc.message}")
        except Exception:
            logger.debug("positions() lookup failed before close", exc_info=True)
        self._transition(rid, op, ST_VALIDATED, symbol=symbol,
                         detail={"ticket": ticket})
        self._transition(rid, op, ST_APPROVED, symbol=symbol,
                         detail={"ticket": ticket})
        self._transition(rid, op, ST_SUBMITTED, symbol=symbol,
                         detail={"ticket": ticket})
        try:
            with execution_scope():
                close_price = self.adapter.close_position(ticket)
        except BrokerError as exc:
            self._transition(rid, op, ST_FAILED, symbol=symbol,
                             detail={"ticket": ticket, "code": exc.code})
            result = self._action_result(False, exc.code,
                                         f"Broker refused close of ticket {ticket}: {exc.message}",
                                         request_id=rid, state=ST_FAILED,
                                         ticket=ticket, symbol=symbol)
            self._idem_store(rid, op, result)
            self._audit({"kind": "close_position",
                         "request_id": rid, "ticket": ticket, "symbol": symbol,
                         "source": source, "approved": False,
                         "reason": exc.message, "reason_code": exc.code,
                         "state": ST_FAILED})
            return result
        self._transition(rid, op, ST_BROKER_ACK, symbol=symbol,
                         detail={"ticket": ticket, "close_price": close_price})
        self._transition(rid, op, ST_EXECUTED, symbol=symbol,
                         detail={"ticket": ticket, "close_price": close_price})
        result = self._action_result(True, "", "broker-confirmed close",
                                     request_id=rid, state=ST_EXECUTED,
                                     ticket=ticket, symbol=symbol,
                                     close_price=close_price)
        self._idem_store(rid, op, result)
        result["audit_id"] = self._audit({"kind": "close_position",
                                          "request_id": rid,
                                          "ticket": ticket, "symbol": symbol,
                                          "source": source,
                                          "approved": True,
                                          "reason": "broker-confirmed close",
                                          "state": ST_EXECUTED,
                                          "close_price": close_price})
        events_mod.emit({"event": "position.closed", "ticket": ticket,
                         "symbol": symbol, "reason": f"closed via gateway by {source}",
                         "close_price": close_price})
        logger.info("Position closed: ticket=%s %s @ %s", ticket, symbol, close_price)
        return result

    def modify_position(self, ticket, stop_loss=None, take_profit=None,
                        source: str = "agent",
                        request_id: Optional[str] = None) -> dict:
        op = "modify_position"
        rid = request_id or uuid.uuid4().hex
        self._transition(rid, op, ST_REQUESTED,
                         detail={"ticket": ticket, "source": source,
                                 "stop_loss": stop_loss, "take_profit": take_profit})
        prior = self._idem_lookup(rid)
        if prior is not None and prior.get("operation") == op:
            self._transition(rid, op, ST_DUPLICATE,
                             detail={"replayed": True})
            logger.info("Duplicate modify request_id %s — returning original result.", rid)
            return dict(prior["decision"])

        try:
            ticket = int(ticket)
        except (TypeError, ValueError):
            return self._action_reject(rid, op, "INVALID_TICKET",
                                       f"ticket must be an integer, got {ticket!r}")
        # Kill switch blocks modifies (only risk-reducing closes stay allowed).
        if self.kill_switch.is_engaged():
            return self._action_reject(
                rid, op, KILL_SWITCH_ENGAGED,
                "Kill switch is engaged — position modifies are blocked")
        if stop_loss is None and take_profit is None:
            return self._action_reject(rid, op, INVALID_ORDER,
                                       "nothing to modify: supply stop_loss and/or take_profit")
        # Mandatory-SL rule: an SL can be moved but never removed.
        if stop_loss is not None and stop_loss <= 0:
            return self._action_reject(rid, op, INVALID_ORDER,
                                       "stop_loss is mandatory and must stay positive")
        if take_profit is not None and take_profit <= 0:
            return self._action_reject(rid, op, INVALID_SL_TP,
                                       "take_profit must be positive when supplied")
        symbol = "unknown"
        pos_direction = None
        pos_sl = None
        try:
            open_positions = self.adapter.positions()
        except BrokerError as exc:
            return self._action_reject(rid, op, exc.code,
                                       f"Cannot list positions: {exc.message}")
        except Exception:
            logger.debug("positions() lookup failed before modify", exc_info=True)
            open_positions = []
        explicit_tp = take_profit is not None
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
            return self._action_reject(rid, op, POSITION_NOT_FOUND,
                                       f"ticket {ticket} not found among open positions",
                                       symbol=symbol)
        self._transition(rid, op, ST_VALIDATED, symbol=symbol,
                         detail={"ticket": ticket})
        if stop_loss <= 0:
            return self._action_reject(rid, op, INVALID_ORDER,
                                       "resulting stop_loss must stay positive (mandatory SL)",
                                       symbol=symbol)
        if explicit_tp and (take_profit is None or take_profit <= 0):
            return self._action_reject(rid, op, INVALID_SL_TP,
                                       "take_profit must stay positive",
                                       symbol=symbol)
        if pos_sl and pos_sl > 0:
            # Direction-aware tightening: a BUY stop may only move up, a
            # SELL stop may only move down. Loosening is rejected.
            if pos_direction == "BUY" and stop_loss < pos_sl:
                return self._action_reject(rid, op, INVALID_ORDER,
                                           "stop_loss may be tightened but never loosened",
                                           symbol=symbol)
            if pos_direction == "SELL" and stop_loss > pos_sl:
                return self._action_reject(rid, op, INVALID_ORDER,
                                           "stop_loss may be tightened but never loosened",
                                           symbol=symbol)
        self._transition(rid, op, ST_APPROVED, symbol=symbol,
                         detail={"ticket": ticket, "stop_loss": stop_loss,
                                 "take_profit": take_profit})
        self._transition(rid, op, ST_SUBMITTED, symbol=symbol,
                         detail={"ticket": ticket})
        try:
            with execution_scope():
                self.adapter.modify_order(ticket, stop_loss, take_profit)
        except BrokerError as exc:
            self._transition(rid, op, ST_FAILED, symbol=symbol,
                             detail={"ticket": ticket, "code": exc.code})
            result = self._action_result(
                False, exc.code,
                f"Broker refused modify of ticket {ticket}: {exc.message}",
                request_id=rid, state=ST_FAILED, ticket=ticket, symbol=symbol)
            self._idem_store(rid, op, result)
            self._audit({"kind": "modify_position",
                         "request_id": rid, "ticket": ticket, "symbol": symbol,
                         "source": source, "approved": False,
                         "reason": exc.message, "reason_code": exc.code,
                         "state": ST_FAILED})
            return result
        self._transition(rid, op, ST_BROKER_ACK, symbol=symbol,
                         detail={"ticket": ticket})
        self._transition(rid, op, ST_EXECUTED, symbol=symbol,
                         detail={"ticket": ticket, "stop_loss": stop_loss,
                                 "take_profit": take_profit})
        result = self._action_result(True, "", "broker-confirmed modify",
                                     request_id=rid, state=ST_EXECUTED,
                                     ticket=ticket, symbol=symbol,
                                     stop_loss=stop_loss, take_profit=take_profit)
        self._idem_store(rid, op, result)
        result["audit_id"] = self._audit({"kind": "modify_position",
                                          "request_id": rid,
                                          "ticket": ticket, "symbol": symbol,
                                          "source": source,
                                          "approved": True,
                                          "reason": "broker-confirmed modify",
                                          "state": ST_EXECUTED,
                                          "stop_loss": stop_loss,
                                          "take_profit": take_profit})
        events_mod.emit({"event": "position.modified", "ticket": ticket,
                         "symbol": symbol, "stop_loss": stop_loss,
                         "take_profit": take_profit})
        logger.info("Position modified: ticket=%s SL=%s TP=%s", ticket, stop_loss, take_profit)
        return result

    def _action_result(self, ok: bool, code: str, message: str, **extra) -> dict:
        result = {"ok": ok, "error_code": code, "message": message}
        result.update(extra)
        return result

    def _action_reject(self, request_id: str, operation: str, code: str,
                       message: str, symbol: Optional[str] = None, **extra) -> dict:
        """Structured rejection for modify/close: terminal 'rejected' state,
        audit-logged, and stored under the request_id for idempotency."""
        self._transition(request_id, operation, ST_REJECTED, symbol=symbol,
                         detail={"code": code, "message": message})
        result = self._action_result(False, code, message,
                                     request_id=request_id, state=ST_REJECTED,
                                     symbol=symbol, **extra)
        self._idem_store(request_id, operation, result)
        self._audit({"kind": operation, "request_id": request_id,
                     "symbol": symbol, "approved": False,
                     "reason": message, "reason_code": code,
                     "state": ST_REJECTED})
        events_mod.emit({"event": "trade.rejected", "reason": code,
                         "symbol": symbol or "unknown",
                         "detail": {"message": message,
                                    "operation": operation,
                                    "request_id": request_id}})
        logger.info("%s rejected [%s]: %s", operation, code, message)
        return result

    # -- helpers ----------------------------------------------------------------
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
                return self._reject(req, "request_trade",
                                    f"Market appears closed for {req.symbol} (tick age {age:.0f}s)",
                                    MARKET_CLOSED, risk_inputs=risk_inputs)
            max_spread = self.config.risk.max_spread_points
            if max_spread > 0 and quote.spread > max_spread:
                return self._reject(req, "request_trade",
                                    f"Spread {quote.spread} exceeds max {max_spread} for {req.symbol}",
                                    RISK_LIMIT_EXCEEDED, risk_inputs=risk_inputs)
            risk_inputs["spread"] = quote.spread
        else:
            risk_inputs["quote"] = "unavailable — spread/market-open checks skipped"

        min_equity = self.config.risk.min_account_equity
        equity = risk_inputs.get("equity", 0)
        if min_equity > 0 and equity < min_equity:
            return self._reject(req, "request_trade",
                                f"Account equity {equity:.2f} below minimum {min_equity:.2f}",
                                RISK_LIMIT_EXCEEDED, risk_inputs=risk_inputs)
        return None

    def _risk_blocked_event(self, req: TradeRequest, code: str, reason: str) -> None:
        events_mod.emit({"event": "risk.blocked", "reason": code,
                         "symbol": req.symbol, "signal_id": req.signal_id,
                         "detail": {"message": reason,
                                    "idempotency_key": req.idempotency_key}})

    def _reject(self, req: TradeRequest, operation: str, reason: str, code: str,
                volume: float = 0.0, notes: Optional[list] = None,
                risk_inputs: Optional[dict] = None,
                state: str = ST_REJECTED) -> GatewayDecision:
        events_mod.emit({"event": "trade.rejected", "reason": code,
                         "symbol": req.symbol, "signal_id": req.signal_id,
                         "detail": {"message": reason, "volume": volume,
                                    "idempotency_key": req.idempotency_key,
                                    "source": req.source}})
        decision = GatewayDecision(
            approved=False, reason=reason, reason_code=code,
            idempotency_key=req.idempotency_key, request_id=req.request_id or "",
            state=state, volume=volume, notes=notes or [],
        )
        # Cache idempotency rejections too — a replayed key must return the
        # identical decision, approved or not — and persist for restarts.
        self._decisions[req.idempotency_key] = decision
        self._idem_store(decision.request_id, operation, decision.to_dict())
        self._transition(decision.request_id, operation, state, symbol=req.symbol,
                         detail={"code": code, "message": reason})
        decision.audit_id = self._audit({
            "kind": "trade_decision",
            "idempotency_key": req.idempotency_key, "request_id": decision.request_id,
            "signal_id": req.signal_id,
            "symbol": req.symbol, "direction": req.direction,
            "source": req.source, "approved": False, "reason": reason,
            "reason_code": code, "state": state, "volume": volume,
            "risk_inputs": risk_inputs or {},
        })
        decision.audit_pending = decision.audit_id is None and self._store is not None
        logger.info("Trade rejected [%s]: %s", code, reason)
        return decision
