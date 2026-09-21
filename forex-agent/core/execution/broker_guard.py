"""Broker write-guard — the architectural enforcement that the agent can
never reach the broker adapter's trade-affecting methods directly.

The ONLY code paths allowed to call ``submit_order`` / ``modify_order`` /
``close_position`` on a guarded adapter are the ones that enter
``execution_scope()``:

  * core.execution.gateway  (request_trade / modify_position / close_position)
  * core.execution.kill_switch (close_all sweep — the daemon-driven,
    risk-reducing bulk close; it IS the kill switch, not a bypass of it)
  * daemon.position_monitor (the standing automated exit policy —
    breakeven/trailing/time-based closes; risk-reducing by construction,
    classified as local safety rather than discretionary trading)

Everything else — agent tools, other daemons, CLI, read-only surfaces —
gets ``BrokerError(GATEWAY_BYPASS_ATTEMPTED)`` on any direct write attempt.
Read methods (account_info, positions, quote, ...) pass through untouched,
so read-only tools keep working on the same object.

Wiring: agent.tools.backend.broker_adapter() returns the adapter wrapped
in GatewayOnlyAdapter, so no caller downstream of backend ever holds the
raw adapter. The raw adapter object never escapes _build_adapter().
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from broker import BrokerError

GATEWAY_BYPASS_ATTEMPTED = "GATEWAY_BYPASS_ATTEMPTED"

# Trade-affecting methods. Everything else on the adapter is read-only.
_WRITE_METHODS = frozenset({"submit_order", "modify_order", "close_position"})

_tls = threading.local()


def _depth() -> int:
    return getattr(_tls, "gateway_write_depth", 0)


class execution_scope:
    """Re-entrant context manager granting broker-write permission.

    Entered ONLY by core.execution.gateway (around its adapter write
    calls), core.execution.kill_switch (around its close-all sweep), and
    daemon.position_monitor (around the standing automated exit policy).
    """

    def __enter__(self) -> "execution_scope":
        _tls.gateway_write_depth = _depth() + 1
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        _tls.gateway_write_depth = max(0, _depth() - 1)
        return False


def in_execution_scope() -> bool:
    """True when the current thread is inside execution_scope()."""
    return _depth() > 0


class GatewayOnlyAdapter:
    """Write-guarded proxy around a BrokerAdapter.

    Attribute reads delegate to the wrapped adapter. Write methods raise
    BrokerError(GATEWAY_BYPASS_ATTEMPTED) unless the calling thread is
    inside execution_scope(). The guard is deliberately dumb — it knows
    nothing about callers, only about the scope token the gateway sets.
    """

    def __init__(self, inner: Any):
        object.__setattr__(self, "_inner", inner)

    @property
    def _wrapped(self) -> Any:
        return object.__getattribute__(self, "_inner")

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)
        if name in _WRITE_METHODS and callable(attr):
            return self._guarded(name, attr)
        return attr

    def _guarded(self, name: str, fn: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            if not in_execution_scope():
                raise BrokerError(
                    GATEWAY_BYPASS_ATTEMPTED,
                    f"Direct {name}() call blocked: trade-affecting broker "
                    "calls must go through core.execution.gateway",
                )
            return fn(*args, **kwargs)

        wrapper.__name__ = name
        return wrapper

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"GatewayOnlyAdapter({self._wrapped!r})"
