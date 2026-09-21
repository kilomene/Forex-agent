"""Static forex domain knowledge — the CANONICAL Python port of the Worker's
`src/agent/knowledge.js` (which is being removed). Other areas import from
here; nothing here is duplicated elsewhere.

What this is: static session UTC windows, a static long-run correlation
reference table, and the deterministic correlated-exposure rule. What this
is NOT: live-computed data. Session times are fixed UTC windows (they don't
track DST edge cases perfectly); correlations are commonly-cited long-run
approximations, not a rolling statistic off the price feed. A future upgrade
path is computing correlation from actual candle data — this static table
must never be presented as equivalent to that.

Consumers: core/risk (correlation exposure limits), agent/tools
(get_correlation_exposure / get_session_info capabilities).
"""

from datetime import datetime, timezone

# Static session windows in UTC. Fixed reference data, not live-computed.
SESSIONS_UTC = {
    "tokyo": {"start": 0, "end": 9},
    "london": {"start": 8, "end": 17},
    "new_york": {"start": 13, "end": 22},
}


def current_session_info(now=None) -> dict:
    """Session/liquidity snapshot for `now` (UTC). Mirrors knowledge.js."""
    now = now or datetime.now(timezone.utc)
    hour = now.hour if now.tzinfo else now.hour  # naive datetimes treated as UTC
    active = [
        name
        for name, window in SESSIONS_UTC.items()
        if window["start"] <= hour < window["end"]
    ]

    overlaps = []
    if "tokyo" in active and "london" in active:
        overlaps.append("tokyo/london")
    if "london" in active and "new_york" in active:
        overlaps.append("london/new_york")

    if overlaps:
        note = (
            f"{', '.join(overlaps)} overlap — typically the highest liquidity "
            "and volatility window"
        )
    elif not active:
        note = (
            "Outside major session hours — typically thin liquidity, wider "
            "spreads, less reliable moves"
        )
    else:
        note = f"Only {', '.join(active)} session active — moderate liquidity"

    return {
        "utc_hour": hour,
        "active_sessions": active,
        "overlaps": overlaps,
        "liquidity_note": note,
    }


# Commonly-cited long-run correlation direction between majors.
# "positive" = pairs tend to move the same direction, "negative" = opposite.
# A static reference heuristic, NOT a computed statistic.
CORRELATION_REFERENCE = {
    "EURUSD": {"GBPUSD": "positive", "USDJPY": "negative", "AUDUSD": "positive"},
    "GBPUSD": {"EURUSD": "positive", "USDJPY": "negative", "AUDUSD": "positive"},
    "USDJPY": {"EURUSD": "negative", "GBPUSD": "negative", "AUDUSD": "negative"},
    "AUDUSD": {"EURUSD": "positive", "GBPUSD": "positive", "USDJPY": "negative"},
    # Indices: broadly move together (risk-on/risk-off).
    "US30": {"US100": "positive", "US500": "positive"},
    "US100": {"US30": "positive", "US500": "positive", "AAPL": "positive"},
    "US500": {"US30": "positive", "US100": "positive"},
    # A stock correlates with an index it's a major component of.
    "AAPL": {"US100": "positive"},
    # Gold commonly moves inversely to USD strength.
    "XAUUSD": {"EURUSD": "positive", "USDJPY": "negative"},
}


def correlated_pairs(symbol: str) -> dict:
    """Return {other_symbol: 'positive'|'negative'} for `symbol` ({} if unknown)."""
    return dict(CORRELATION_REFERENCE.get(symbol, {}))


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def check_correlated_exposure(new_signal, open_positions) -> list:
    """Flag stacked directional exposure for a prospective signal.

    Deterministic rule (ported verbatim from knowledge.js): for each open
    position on a DIFFERENT symbol, relation = table lookup
    ('positive'/'negative'); stacked = (positive and same direction) or
    (negative and opposite direction). Flag text is kept byte-identical to
    the original so downstream consumers/tests don't drift.

    `new_signal`: dict or object with `symbol`, `direction` ("BUY"/"SELL").
    `open_positions`: iterable of dicts/objects with `symbol`, `direction`.

    Double-counting guard: ONLY positions the broker reports as open count.
    The caller MUST pass the current open-position list (e.g. from
    `BrokerAdapter.positions()`), not a ledger of "executed, assumed open"
    signals — that assumption was a known weakness of the original
    `memory.js` (`getActiveExposure`) and is fixed by construction here.
    Defensively, any position explicitly marked with a non-"open" status
    is skipped rather than counted.
    """
    symbol = _field(new_signal, "symbol")
    direction = _field(new_signal, "direction")
    correlations = correlated_pairs(symbol)
    flags = []

    for pos in open_positions:
        if _field(pos, "symbol") == symbol:
            continue  # same symbol is not "correlated exposure"
        status = _field(pos, "status", "open")
        if status is not None and str(status).lower() not in ("open",):
            continue  # closed/pending positions must not double-count
        relation = correlations.get(_field(pos, "symbol"))
        if not relation:
            continue

        same_direction = _field(pos, "direction") == direction
        stacked = (relation == "positive" and same_direction) or (
            relation == "negative" and not same_direction
        )
        if stacked:
            pos_dir = _field(pos, "direction")
            pos_sym = _field(pos, "symbol")
            flags.append(
                f"Correlated exposure: open {pos_dir} {pos_sym} position is "
                f"{relation}ly correlated with this {direction} {symbol} signal — "
                "this may be effectively doubling the same directional bet "
                "rather than diversifying."
            )

    return flags
