"""
Risk guardrails. These run BEFORE any order reaches the broker, no
exceptions. A signal passing the strategy just means the math fired —
it says nothing about whether it's safe to execute right now.

This module (via the Execution Gateway) is the ONLY place that decides
whether a trade actually executes. Not the agent, not any model —
this code, and nothing upstream of it gets to skip these checks.

Three genuine holes from the original risk.py are fixed here:
  (a) Daily-loss accounting is computed on EQUITY, not balance — open
      floating losses count. The original compared start-of-day balance
      to current balance, so the account could be deeply underwater on
      open positions while the "3% daily loss" gate still read 0%.
  (b) Volume is validated and clamped to the broker's real
      volume_min/max/step (from adapter.symbols()). The original rounded
      to 2 decimals and floored at 0.01, which brokers reject (or
      worse) for instruments with different steps.
  (c) Position counts and exposure math filter to the bot's OWN
      positions via the magic number. The original counted every
      position on the account, so manual trades ate the bot's quota.

Risk state (daily-loss baseline, loss-streak outcomes) is persisted to
the local store — fixing the restart hole where both died with the
process. The store is injected; tests use a fake in-memory store.
"""

import logging
import math
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Deque, List, Optional

from broker import (
    BrokerAdapter,
    BrokerError,
    INVALID_ORDER,
    MAX_EXPOSURE,
    RISK_LIMIT_EXCEEDED,
    DAILY_LOSS_LIMIT,
    is_own_position,
)
from config import RiskConfig
from core.risk.correlation import check_correlated_exposure
from core.signals import Signal

logger = logging.getLogger("risk")

# Gateway-level reason codes beyond the shared broker vocabulary.
KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
DRY_RUN_BLOCKED = "DRY_RUN_BLOCKED"


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str
    reason_code: str = ""  # structured code when rejected
    lot_size: float = 0.0
    notes: List[str] = field(default_factory=list)  # correlation flags, clamps applied


def _default_store():
    """Local SQLite store via the storage contract, or None if the
    storage package isn't installed (in-memory fallback)."""
    try:
        from storage.store import Store
    except ImportError:
        logger.warning("storage.store unavailable — risk state is in-memory only (restart hole open).")
        return None
    path = os.environ.get("FOREX_AGENT_DB")
    try:
        return Store(path) if path else Store()
    except Exception:
        logger.exception("Could not open risk store — falling back to in-memory state.")
        return None


def calculate_position_size(
    equity: float,
    risk_pct: float,
    entry: float,
    stop_loss: float,
    symbol: str,
    specs: dict,
) -> float:
    """
    Risk a fixed % of account EQUITY per trade, sized off the actual
    stop distance so every trade risks roughly the same account-currency
    amount regardless of how wide its stop is.

    Uses REAL broker-specified contract values (tick_value/tick_size from
    adapter.symbols()) rather than a hardcoded forex pip formula — the
    same sizing code stays correct across forex, metals, indices, and
    stocks. Ported from the original; the only change is taking a specs
    dict instead of the MT5 client, so it stays broker-agnostic and
    testable.

    `risk_pct` is a percent number (1.0 = 1%).
    """
    tick_value = specs.get("tick_value", 0)
    tick_size = specs.get("tick_size", 0)

    if tick_size <= 0 or tick_value <= 0:
        return 0.0

    stop_distance = abs(entry - stop_loss)
    ticks_at_risk = stop_distance / tick_size
    dollar_risk_per_lot = ticks_at_risk * tick_value
    if dollar_risk_per_lot <= 0:
        return 0.0

    dollar_risk = equity * (risk_pct / 100)
    lot_size = dollar_risk / dollar_risk_per_lot
    return max(lot_size, 0.0)


def clamp_volume(volume: float, spec) -> float:
    """
    Clamp a computed volume to the broker's volume_min/max/step.
    Floors to the step (never rounds UP past the risk-sized amount).
    Raises BrokerError(INVALID_ORDER) if even the minimum is unreachable.
    """
    step = spec.volume_step
    if step <= 0:
        raise BrokerError(INVALID_ORDER, f"No valid volume step for {spec.name}.")
    steps = math.floor(volume / step + 1e-9)
    v = round(steps * step, 8)
    v = min(v, spec.volume_max)
    if v < spec.volume_min - 1e-9:
        raise BrokerError(
            INVALID_ORDER,
            f"Computed volume {volume:.4f} is below broker minimum "
            f"{spec.volume_min} for {spec.name} — order rejected.",
        )
    return max(v, spec.volume_min)


class RiskManager:
    """Pre-trade guardrails with persisted state.

    `store` must provide get_risk_state()->dict / set_risk_state(dict)
    (the storage contract). When omitted, the real local SQLite store is
    used if importable; otherwise state is in-memory only.
    """

    _DAY_KEY = "day"
    _EQUITY_KEY = "start_of_day_equity"
    _OUTCOMES_KEY = "recent_outcomes"

    def __init__(self, config: RiskConfig, store=None):
        self.config = config
        self._store = store if store is not None else _default_store()
        self._mem_state: dict = {}

    # -- persisted state -------------------------------------------------
    def _get_state(self) -> dict:
        if self._store is not None:
            try:
                return dict(self._store.get_risk_state())
            except Exception:
                logger.exception("Risk store read failed — using in-memory state.")
        return dict(self._mem_state)

    def _set_state(self, patch: dict) -> None:
        self._mem_state.update(patch)
        if self._store is not None:
            try:
                self._store.set_risk_state(patch)
            except Exception:
                logger.exception("Risk store write failed — state is memory-only this run.")

    def _roll_day_if_needed(self, current_equity: float) -> float:
        """Returns the start-of-day equity, rolling the day when needed."""
        state = self._get_state()
        today = date.today().isoformat()
        if state.get(self._DAY_KEY) != today or self._EQUITY_KEY not in state:
            self._set_state({self._DAY_KEY: today, self._EQUITY_KEY: current_equity})
            logger.info("Risk day rolled. Start-of-day equity=%.2f", current_equity)
            return current_equity
        return float(state[self._EQUITY_KEY])

    def sync_outcomes(self, outcomes: list) -> None:
        """Replaces the tracked outcome history with ground truth from
        deal history (see core.performance) — called periodically, not
        incrementally, so this reflects reality rather than accumulating
        drift. Persisted, so the circuit breaker survives restarts."""
        self._set_state({self._OUTCOMES_KEY: list(outcomes[-20:])})

    def _recent_outcomes(self) -> Deque[bool]:
        return deque(self._get_state().get(self._OUTCOMES_KEY, []), maxlen=20)

    def _consecutive_losses(self) -> int:
        count = 0
        for outcome in reversed(self._recent_outcomes()):
            if outcome:
                break
            count += 1
        return count

    # -- the check ----------------------------------------------------------
    def check(
        self,
        signal: Signal,
        adapter: BrokerAdapter,
        kill_switch_engaged: bool = False,
    ) -> RiskCheckResult:
        # Checked first, before anything else — this is the subsystem's
        # OWN independent confirmation that entries are allowed, not a
        # trust of whatever any agent or Worker already decided.
        if kill_switch_engaged:
            return RiskCheckResult(False, "Kill switch is engaged — no new entries allowed",
                                   reason_code=KILL_SWITCH_ENGAGED)

        if self.config.require_stop_loss and not signal.stop_loss:
            return RiskCheckResult(False, "Signal has no stop loss — rejected by policy",
                                   reason_code=INVALID_ORDER)

        account = adapter.account_info()
        equity = account.equity
        start_equity = self._roll_day_if_needed(equity)

        # HOLE (c) FIX: count only the bot's OWN positions (magic filter).
        own_positions = [p for p in adapter.positions() if is_own_position(p.magic)]
        if len(own_positions) >= self.config.max_open_positions:
            return RiskCheckResult(
                False,
                f"Max open positions reached ({len(own_positions)}/{self.config.max_open_positions})",
                reason_code=RISK_LIMIT_EXCEEDED,
            )

        # HOLE (a) FIX: daily loss on EQUITY — floating losses count.
        daily_loss_pct = 0.0
        if start_equity > 0:
            daily_loss_pct = (start_equity - equity) / start_equity
        if daily_loss_pct >= self.config.max_daily_loss:
            return RiskCheckResult(
                False,
                f"Daily loss limit hit ({daily_loss_pct * 100:.2f}% >= "
                f"{self.config.max_daily_loss * 100:.2f}%)",
                reason_code=DAILY_LOSS_LIMIT,
            )

        # Circuit breaker: N consecutive losses halts new entries. Plain
        # code, checked before every order — no agent or model overrides it.
        consecutive_losses = self._consecutive_losses()
        if consecutive_losses >= self.config.max_consecutive_losses:
            return RiskCheckResult(
                False,
                f"Circuit breaker: {consecutive_losses} consecutive losses "
                f"(limit {self.config.max_consecutive_losses}) — halting new entries",
                reason_code=RISK_LIMIT_EXCEEDED,
            )

        # Exposure: own open volume + this request vs the cap.
        specs = {s.name: s for s in adapter.symbols([signal.symbol])}
        spec = specs.get(signal.symbol)
        if spec is None:
            return RiskCheckResult(False, f"Unknown symbol: {signal.symbol}",
                                   reason_code=INVALID_ORDER)
        if self.config.use_percent_risk_sizing:
            raw_volume = calculate_position_size(
                equity, self.config.max_risk_per_trade * 100,
                signal.entry_price, signal.stop_loss, signal.symbol,
                {"tick_value": spec.tick_value, "tick_size": spec.tick_size},
            )
        else:
            raw_volume = self.config.fixed_lot_size

        # HOLE (b) FIX: validate/clamp to the broker's real volume grid.
        try:
            volume = clamp_volume(raw_volume, spec)
        except BrokerError as exc:
            return RiskCheckResult(False, exc.message, reason_code=exc.code)

        open_volume = sum(p.volume for p in own_positions)
        if open_volume + volume > self.config.max_total_exposure_lots:
            return RiskCheckResult(
                False,
                f"Max total exposure exceeded ({open_volume + volume:.2f} > "
                f"{self.config.max_total_exposure_lots:.2f} lots)",
                reason_code=MAX_EXPOSURE,
            )

        # Correlation: flag stacked directional bets as ground truth in
        # the notes; block when the stack would exceed the limit.
        notes: List[str] = []
        flags = check_correlated_exposure(signal, own_positions)
        notes.extend(flags)
        if len(flags) >= self.config.max_correlated_positions:
            return RiskCheckResult(
                False,
                f"Correlated exposure limit: {len(flags)} already-stacked "
                f"correlated position(s) (limit {self.config.max_correlated_positions})",
                reason_code=MAX_EXPOSURE,
                notes=notes,
            )

        return RiskCheckResult(True, "OK", lot_size=volume, notes=notes)
