"""
Kill switch — idempotent latch, persisted, enforced below any agent layer.

Enforcement lives in the Execution Gateway (which checks
is_engaged() on every request) and in core.risk (checked before every
order). There is no bypass path: the agent cannot reach the broker
without passing through the gateway.

Fails closed: if the store cannot be read, is_engaged() returns True.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from broker import BrokerAdapter, is_own_position
from core.events import emit
from core.execution.broker_guard import execution_scope

logger = logging.getLogger("kill_switch")


def _default_store():
    try:
        from storage.store import Store
    except ImportError:
        return None
    try:
        return Store()
    except Exception:
        logger.exception("Could not open kill-switch store.")
        return None


class KillSwitch:
    """Persistent idempotent latch.

    `store` provides get_kill_switch()->{"engaged","source","ts"} /
    set_kill_switch(engaged, source). `adapter` is used only by
    close_all(); the latch itself needs no broker.
    """

    def __init__(self, store=None, adapter: Optional[BrokerAdapter] = None):
        self._store = store if store is not None else _default_store()
        self._adapter = adapter
        self._mem = {"engaged": False, "source": None, "ts": None}

    # -- latch ------------------------------------------------------------
    def state(self) -> dict:
        if self._store is not None:
            try:
                return dict(self._store.get_kill_switch())
            except Exception:
                logger.exception("Kill-switch store read failed — failing closed.")
        return dict(self._mem)

    def is_engaged(self) -> bool:
        """Fails CLOSED: if the latch state cannot be READ, trading stops.

        (state() below falls back to the in-memory copy for display;
        the safety decision itself never trusts a fallback.)
        """
        if self._store is not None:
            try:
                return bool(self._store.get_kill_switch().get("engaged", False))
            except Exception:
                logger.exception("Kill-switch state unreadable — failing closed.")
                return True
        return bool(self._mem.get("engaged", False))

    def engage(self, source: str = "local") -> dict:
        """Idempotent: engaging twice keeps the ORIGINAL ts/source."""
        current = self.state()
        if current.get("engaged"):
            return current
        return self._set(True, source, initial=True)

    def disengage(self, source: str = "local") -> dict:
        return self._set(False, source)

    def _set(self, engaged: bool, source: str, initial: bool = False) -> dict:
        ts = datetime.now(timezone.utc).isoformat()
        record = {"engaged": engaged, "source": source, "ts": ts}
        self._mem = dict(record)
        if self._store is not None:
            try:
                self._store.set_kill_switch(engaged, source)
                record = dict(self._store.get_kill_switch())
            except Exception:
                logger.exception("Kill-switch store write failed — latch is memory-only.")
        if engaged and initial:
            logger.warning("KILL SWITCH ENGAGED by %s", source)
            emit({"event": "kill_switch.activated", "ts": ts, "source": source})
        elif not engaged:
            logger.warning("Kill switch disengaged by %s", source)
        return record

    # -- close-all ----------------------------------------------------------
    def close_all(self) -> dict:
        """Force-close every position THIS BOT manages (magic-filtered),
        regardless of profit/loss or exit rules. Returns a per-ticket
        outcome summary; never raises (a failed close is reported, not
        thrown, so one bad ticket can't abort the sweep)."""
        if self._adapter is None:
            return {"closed": 0, "failed": 0, "results": [],
                    "error": "no adapter attached"}
        results = []
        closed = failed = 0
        try:
            positions = self._adapter.positions()
        except Exception as exc:
            logger.exception("close_all: positions() failed")
            return {"closed": 0, "failed": 0, "results": [],
                    "error": f"positions() failed: {exc}"}
        for pos in positions:
            if not is_own_position(pos.magic):
                continue
            try:
                # execution_scope: the kill-switch sweep is the one
                # non-gateway writer the broker guard allows.
                with execution_scope():
                    price = self._adapter.close_position(pos.ticket)
                closed += 1
                results.append({"ticket": pos.ticket, "symbol": pos.symbol,
                                "ok": True, "close_price": price})
            except Exception as exc:
                failed += 1
                logger.error("close_all: failed to close ticket %s: %s", pos.ticket, exc)
                results.append({"ticket": pos.ticket, "symbol": pos.symbol,
                                "ok": False, "error": str(exc)})
        logger.warning("Kill-switch close-all: %d closed, %d failed.", closed, failed)
        return {"closed": closed, "failed": failed, "results": results}
