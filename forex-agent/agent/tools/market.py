"""Market-data and analysis capabilities.

Ports of the Worker tools get_recent_ohlc / get_indicator_snapshot /
get_smc_patterns, rehomed to local implementations: candles come from the
BrokerAdapter (closed candles only, per the adapter contract), indicators
from core.indicators, structure from core.smc, signal evaluation from
core.strategies.EmaRsiStrategy. No LLM anywhere; ML prediction is an honest
available:false until intelligence/ml/ serves a real model.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import Any, List

from . import backend

logger = logging.getLogger("forex_agent.tools.market")


def _jsonable(value: Any) -> Any:
    """Convert dataclasses/datetimes/nested structures to plain JSON types."""
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _candles_to_dicts(candles: List[Any]) -> List[dict]:
    rows = []
    for c in candles or []:
        to_dict = getattr(c, "to_dict", None)
        if callable(to_dict):
            rows.append(to_dict())
        elif isinstance(c, dict):
            rows.append(dict(c))
        else:
            rows.append(_jsonable(c))
    return rows


def _adapter():
    try:
        return backend.broker_adapter(), None
    except RuntimeError as exc:
        return None, backend.dep_unavailable("broker", exc)


def _fetch_candles(adapter, symbol: str, timeframe: str, count: int):
    try:
        return adapter.candles(symbol, timeframe, count), None
    except Exception as exc:  # BrokerError or transport failure
        return None, backend.broker_error_result(
            exc, "Could not fetch candles for %s %s" % (symbol, timeframe))


def _smc_summary(smc_mod, candles: List[Any]) -> dict:
    """Compose the SMC summary from core.smc's deterministic functions."""
    swings = smc_mod.find_swing_points(candles)
    structure = smc_mod.analyze_market_structure(swings)
    breaks = smc_mod.detect_structure_breaks(candles, swings)
    fvgs = smc_mod.detect_fair_value_gaps(candles)
    order_blocks = smc_mod.detect_order_blocks(candles, breaks)
    return {
        "available": True,
        "market_structure": _jsonable(structure),
        "swing_points": _jsonable(swings)[-20:],
        "structure_breaks": _jsonable(breaks)[-10:],
        "trend_lines": _jsonable(smc_mod.detect_trend_lines(swings)),
        "support_resistance_zones": _jsonable(
            smc_mod.detect_support_resistance_zones(swings))[-10:],
        "liquidity_zones": _jsonable(smc_mod.detect_liquidity_zones(swings)),
        "fair_value_gaps": _jsonable(fvgs)[-20:],
        "order_blocks": _jsonable(order_blocks)[-10:],
    }


def forex_market_data(symbol: str, timeframe: str = "M15", count: int = 30) -> dict:
    """Recent closed candles for a symbol/timeframe, oldest first.

    Port of the Worker get_recent_ohlc tool: real OHLC from the broker
    adapter instead of bridge-forwarded D1 rows.
    """
    adapter, error = _adapter()
    if error:
        return error
    if not symbol or not isinstance(symbol, str):
        return backend.err("INVALID_SYMBOL", "symbol must be a non-empty string")
    try:
        count = max(1, min(int(count), 500))
    except (TypeError, ValueError):
        return backend.err("INVALID_ARGUMENT", "count must be an integer")
    candles, error = _fetch_candles(adapter, symbol, timeframe, count)
    if error:
        return error
    rows = _candles_to_dicts(candles)
    return {
        "ok": True,
        "available": bool(rows),
        "symbol": symbol,
        "timeframe": timeframe,
        "count": len(rows),
        "candles": rows,
        "note": None if rows else "No candles returned (broker disconnected or symbol unknown).",
    }


def forex_analyze(symbol: str, timeframe: str = "M15") -> dict:
    """Full deterministic analysis: indicators + SMC + strategy evaluation.

    Port of get_indicator_snapshot (+ the evidence half of the old agent
    loop). Returns the exact EMA/RSI/ATR values, the SMC structure
    summary, and whether the deterministic strategy fires on closed
    candles. ml_prediction is honestly unavailable until a real model
    is served.
    """
    adapter, error = _adapter()
    if error:
        return error
    if not symbol or not isinstance(symbol, str):
        return backend.err("INVALID_SYMBOL", "symbol must be a non-empty string")
    candles, error = _fetch_candles(adapter, symbol, timeframe, 200)
    if error:
        return error
    if not candles:
        return backend.err("BROKER_UNAVAILABLE",
                           "No candles returned for %s %s" % (symbol, timeframe))

    result: dict = {"ok": True, "symbol": symbol, "timeframe": timeframe,
                    "candle_count": len(candles)}

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    # Indicators (core.indicators) --------------------------------------
    try:
        ind = backend.indicators()
        ema_fast_s = ind.ema(closes, 20)
        ema_slow_s = ind.ema(closes, 50)
        rsi_s = ind.rsi(closes, 14)
        atr_s = ind.atr(highs, lows, closes, 14)
        ema_fast, ema_slow = ema_fast_s[-1], ema_slow_s[-1]
        # Cross trigger on the last two CLOSED candles (no repaint).
        trigger = None
        if (len(closes) >= 52 and ema_fast is not None and ema_slow is not None
                and ema_fast_s[-2] is not None and ema_slow_s[-2] is not None):
            if ema_fast_s[-2] <= ema_slow_s[-2] and ema_fast > ema_slow:
                trigger = "bullish_cross"
            elif ema_fast_s[-2] >= ema_slow_s[-2] and ema_fast < ema_slow:
                trigger = "bearish_cross"
        result["indicators"] = {
            "ema_fast": ema_fast, "ema_slow": ema_slow,
            "rsi": rsi_s[-1], "atr": atr_s[-1], "trigger": trigger,
        }
    except RuntimeError as exc:
        result["indicators"] = {"available": False, "note": str(exc)}

    # SMC structure (core.smc, duck-typed candles) ------------------------
    try:
        result["smc"] = _smc_summary(backend.smc(), candles)
    except RuntimeError as exc:
        result["smc"] = {"available": False, "note": str(exc)}
    except Exception as exc:
        result["smc"] = {"available": False, "note": "smc analysis failed: %s" % exc}

    # Strategy evaluation (core.strategies.EmaRsiStrategy) ----------------
    try:
        cfg = backend.app_config()
        strat_mod = backend.strategies()
        strategy = strat_mod.EmaRsiStrategy(cfg.trading.ema_rsi, timeframe=timeframe)
        signal = strategy.evaluate(symbol, candles)
        result["strategy_signal"] = _jsonable(signal) if signal is not None else None
        result["strategy"] = getattr(strategy, "name", "ema_rsi")
    except RuntimeError as exc:
        result["strategy_signal"] = {"available": False, "note": str(exc)}
    except Exception as exc:
        result["strategy_signal"] = {"available": False,
                                     "note": "strategy evaluation failed: %s" % exc}

    # ML: honest stub via the real intelligence.ml module (serving PARKED:
    # no labeled dataset, no trained model — see intelligence/ml/predict.py).
    try:
        from intelligence.ml.predict import get_ml_prediction  # noqa: PLC0415
        ml = get_ml_prediction()
    except ImportError:
        ml = {"available": False, "reason": "intelligence.ml not importable"}
    result["ml_prediction"] = ml if isinstance(ml, dict) else {
        "available": False, "reason": "unexpected ml module shape"}
    return result


def forex_get_smc(symbol: str, timeframe: str = "M15") -> dict:
    """Smart Money Concepts structure analysis for a symbol.

    Port of the Worker get_smc_patterns tool: deterministic swing/BOS/FVG/
    order-block analysis from core.smc, computed locally against real
    closed candles.
    """
    adapter, error = _adapter()
    if error:
        return error
    if not symbol or not isinstance(symbol, str):
        return backend.err("INVALID_SYMBOL", "symbol must be a non-empty string")
    candles, error = _fetch_candles(adapter, symbol, timeframe, 200)
    if error:
        return error
    if not candles:
        return {"ok": True, "available": False,
                "note": "No candles returned for %s %s." % (symbol, timeframe)}
    try:
        summary = _smc_summary(backend.smc(), candles)
    except RuntimeError as exc:
        return backend.dep_unavailable("core.smc", exc)
    except Exception as exc:
        return {"ok": True, "available": False,
                "note": "SMC analysis failed: %s" % exc}
    summary["ok"] = True
    summary["symbol"] = symbol
    summary["timeframe"] = timeframe
    return summary
