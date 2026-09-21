"""Backend seam: every cross-area import used by agent/tools lives here.

Conventions (non-negotiable):
  * agent/tools NEVER imports MetaTrader5 (module level or lazily).
  * Trade-affecting calls go through ``gateway()`` (core.execution.gateway)
    ONLY. Read-only tools may use ``broker_adapter()``; trading tools must
    not touch it — enforced by tests/test_iface_tools.py.
  * Storage goes through ``store()`` -> ``from storage import Store``.

All lazy imports are cross-area dependencies recorded in agent/API_DEPS.md
with their expected signatures. Tests inject fakes via ``set_override()``
so no test needs MT5, a live broker, or the (not yet built) gateway.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("forex_agent.tools.backend")

_overrides: Dict[str, Any] = {}
_adapter_cache: Any = None


def set_override(name: str, obj: Any) -> None:
    """Inject a fake backend (tests). Pass None to clear one entry."""
    if obj is None:
        _overrides.pop(name, None)
    else:
        _overrides[name] = obj


def reset_overrides() -> None:
    global _gateway_cache
    _overrides.clear()
    _gateway_cache = None
    reset_adapter_cache()


def reset_adapter_cache() -> None:
    global _adapter_cache
    _adapter_cache = None


def _dep_error(area: str, exc: Exception) -> RuntimeError:
    return RuntimeError(
        "dependency unavailable: %s (import failed; see agent/API_DEPS.md). "
        "%s: %s" % (area, type(exc).__name__, exc)
    )


def _lazy(name: str, importer):
    if name in _overrides:
        return _overrides[name]
    try:
        return importer()
    except ImportError as exc:
        raise _dep_error(name, exc) from exc


# -- broker ---------------------------------------------------------------
def _build_adapter():
    """Instantiate the configured BrokerAdapter.

    provider "mt5" -> broker.mt5.adapter.MT5Adapter (Linux-safe import;
    every operation raises BrokerError(BROKER_UNAVAILABLE) without a
    terminal). Anything else -> broker.disconnected.DisconnectedAdapter.
    A single connect() attempt is made for MT5; failure is logged and the
    adapter is kept — calls then raise structured BrokerErrors that tools
    translate to BROKER_UNAVAILABLE results.
    """
    try:
        cfg = app_config()
        broker_cfg = getattr(cfg, "broker", None)
        provider = getattr(broker_cfg, "provider", "disconnected") or "disconnected"
    except Exception as exc:
        logger.warning("config unavailable, using DisconnectedAdapter: %s", exc)
        cfg, provider = None, "disconnected"

    if provider == "mt5":
        from broker.mt5.adapter import MT5Adapter  # noqa: PLC0415 (lazy, Linux-safe)
        adapter = MT5Adapter()
        try:
            adapter.connect({
                "login": getattr(cfg.broker, "login", 0),
                "password": getattr(cfg.broker, "password", ""),
                "server": getattr(cfg.broker, "server", ""),
                "terminal_path": getattr(cfg.broker, "terminal_path", ""),
            })
        except Exception as exc:
            logger.warning("broker connect failed (%s); calls will report "
                           "BROKER_UNAVAILABLE", exc)
        return adapter

    from broker.disconnected import DisconnectedAdapter  # noqa: PLC0415
    return DisconnectedAdapter()


def broker_adapter():
    """The configured BrokerAdapter (cached). Read-only tools use this;
    trading tools MUST go through gateway() instead."""
    global _adapter_cache
    if "broker" in _overrides:
        return _overrides["broker"]
    if _adapter_cache is None:
        _adapter_cache = _build_adapter()
    return _adapter_cache


# -- storage --------------------------------------------------------------
def store():
    """storage.Store — local SQLite (event queue, audit log, journal,
    risk state, kill-switch latch)."""
    if "store" in _overrides:
        return _overrides["store"]

    def _import():
        from storage import Store  # noqa: PLC0415
        return Store()
    return _lazy("storage", _import)


# -- execution gateway (the ONLY path for trade-affecting calls) ----------
_gateway_cache: Any = None


def gateway():
    """core.execution.gateway — the ExecutionGateway, constructed once per
    process from config + broker adapter + RiskManager + KillSwitch + store.

    Trade-affecting calls: gateway.request_trade(TradeRequest(...)) ->
    GatewayDecision. Kill switch: gateway.kill_switch.engage/disengage/
    is_engaged()/state(). There is NO close/modify API on the gateway —
    forex.close_position / forex.modify_position therefore degrade to
    DEPENDENCY_UNAVAILABLE until the execution builder exposes them
    (see agent/API_DEPS.md).
    """
    global _gateway_cache
    if "gateway" in _overrides:
        return _overrides["gateway"]
    if _gateway_cache is None:
        cfg = app_config()
        adapter = broker_adapter()
        _gateway_cache = _build_gateway(cfg, adapter, store())
    return _gateway_cache


def _build_gateway(cfg, adapter, store):
    try:
        from core.execution.gateway import ExecutionGateway  # noqa: PLC0415
        from core.execution.kill_switch import KillSwitch  # noqa: PLC0415
        from core.risk.engine import RiskManager  # noqa: PLC0415
    except ImportError as exc:
        raise _dep_error("core.execution", exc) from exc
    kill_switch = KillSwitch(store=store, adapter=adapter)
    risk_manager = RiskManager(cfg.risk, store=store)
    return ExecutionGateway(cfg, adapter, risk_manager, kill_switch, store=store)


# -- market / analysis ----------------------------------------------------
def indicators():
    """core.indicators: ema(values, period), rsi(values, period=14),
    atr(highs, lows, closes, period=14)."""
    def _import():
        import core.indicators as ind  # noqa: PLC0415
        return ind
    return _lazy("indicators", _import)


def market_helpers():
    """core.market: Candle, closed_only, closes/highs/lows helpers."""
    def _import():
        import core.market as mkt  # noqa: PLC0415
        return mkt
    return _lazy("market", _import)


def smc():
    """core.smc: find_swing_points, analyze_market_structure,
    detect_structure_breaks, detect_trend_lines,
    detect_support_resistance_zones, detect_liquidity_zones,
    detect_fair_value_gaps, detect_order_blocks (duck-typed candles)."""
    def _import():
        import core.smc as smc_mod  # noqa: PLC0415
        return smc_mod
    return _lazy("smc", _import)


def strategies():
    """core.strategies: Strategy ABC, EmaRsiStrategy(cfg, timeframe)."""
    def _import():
        import core.strategies as strat  # noqa: PLC0415
        return strat
    return _lazy("strategies", _import)


def signals_mod():
    """core.signals: Signal dataclass, SignalStatus."""
    def _import():
        import core.signals as sig  # noqa: PLC0415
        return sig
    return _lazy("signals", _import)


def performance_mod():
    """core.performance (built by the core builder; may not exist yet)."""
    def _import():
        import core.performance as perf  # noqa: PLC0415
        return perf
    return _lazy("performance", _import)


def risk_engine():
    """core.risk (built by the core builder; may not exist yet).

    Standing risk state is ALSO persisted in storage (get_risk_state) —
    prefer that for reads that must work today.
    """
    def _import():
        import core.risk as risk_mod  # noqa: PLC0415
        return risk_mod
    return _lazy("risk", _import)


# -- intelligence ---------------------------------------------------------
def correlation():
    """intelligence.correlation.knowledge: current_session_info(now=None),
    correlated_pairs(symbol), check_correlated_exposure(new_signal,
    open_positions)."""
    def _import():
        import intelligence.correlation.knowledge as corr  # noqa: PLC0415
        return corr
    return _lazy("correlation", _import)


def experience():
    """intelligence.experience.store.ExperienceStore(storage.Store)."""
    def _import():
        from intelligence.experience.store import ExperienceStore  # noqa: PLC0415
        return ExperienceStore(store())
    return _lazy("experience", _import)


# -- config ---------------------------------------------------------------
def app_config():
    """config.config.load_config() -> AppConfig (.mode, .broker.provider,
    .trading.symbols/.timeframes, .worker, .events, .kill_switch, ...)."""
    def _import():
        from config.config import load_config  # noqa: PLC0415
        return load_config()
    return _lazy("config", _import)


# -- shared result helpers --------------------------------------------------
def err(code: str, message: str, **extra) -> dict:
    """Structured error result shared by all tools."""
    result = {"ok": False, "error_code": code, "message": message}
    result.update(extra)
    return result


def broker_error_result(exc: Exception, action: str) -> dict:
    """Translate broker.BrokerError (or any failure) into a tool result."""
    code = getattr(exc, "code", None) or "BROKER_UNAVAILABLE"
    message = getattr(exc, "message", None) or str(exc)
    logger.warning("%s failed: [%s] %s", action, code, message)
    return err(code, "%s: %s" % (action, message))


def dep_unavailable(area: str, exc: Exception) -> dict:
    logger.warning("backend dependency unavailable: %s: %s", area, exc)
    return err("DEPENDENCY_UNAVAILABLE",
               "Required subsystem component is not available: %s" % area,
               component=area)
