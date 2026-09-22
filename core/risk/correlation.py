"""
Correlation exposure rule — alias to the canonical implementation.

Canonical home: intelligence/correlation/ (port of
forex-signal-worker/src/agent/knowledge.js). core/risk imports the rule
from there; this module re-exports it so there is exactly one import
site inside core.

If the intelligence package is unavailable (standalone checkout), a
vendored fallback with identical logic is used instead. The fallback is
marked clearly and must not be edited independently — sync it from
intelligence/correlation/knowledge.py.
"""

try:
    from intelligence.correlation import (  # canonical
        CORRELATION_REFERENCE,
        check_correlated_exposure,
        correlated_pairs,
    )
except ImportError:  # pragma: no cover — fallback for standalone checkouts
    # --- FALLBACK PORT (identical logic to knowledge.js) — do not edit ---
    CORRELATION_REFERENCE = {
        "EURUSD": {"GBPUSD": "positive", "USDJPY": "negative", "AUDUSD": "positive"},
        "GBPUSD": {"EURUSD": "positive", "USDJPY": "negative", "AUDUSD": "positive"},
        "USDJPY": {"EURUSD": "negative", "GBPUSD": "negative", "AUDUSD": "negative"},
        "AUDUSD": {"EURUSD": "positive", "GBPUSD": "positive", "USDJPY": "negative"},
        "US30": {"US100": "positive", "US500": "positive"},
        "US100": {"US30": "positive", "US500": "positive", "AAPL": "positive"},
        "US500": {"US30": "positive", "US100": "positive"},
        "AAPL": {"US100": "positive"},
        "XAUUSD": {"EURUSD": "positive", "USDJPY": "negative"},
    }

    def correlated_pairs(symbol):
        return dict(CORRELATION_REFERENCE.get(symbol, {}))

    def _field(obj, name, default=None):
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    def check_correlated_exposure(new_signal, open_positions):
        symbol = _field(new_signal, "symbol")
        direction = _field(new_signal, "direction")
        correlations = correlated_pairs(symbol)
        flags = []
        for pos in open_positions:
            if _field(pos, "symbol") == symbol:
                continue
            relation = correlations.get(_field(pos, "symbol"))
            if not relation:
                continue
            same_direction = _field(pos, "direction") == direction
            stacked = (relation == "positive" and same_direction) or (
                relation == "negative" and not same_direction
            )
            if stacked:
                flags.append(
                    f"Correlated exposure: open {_field(pos, 'direction')} "
                    f"{_field(pos, 'symbol')} position is {relation}ly "
                    f"correlated with this {direction} {symbol} signal — this may be "
                    "effectively doubling the same directional bet rather than diversifying."
                )
        return flags

__all__ = [
    "CORRELATION_REFERENCE",
    "check_correlated_exposure",
    "correlated_pairs",
]
