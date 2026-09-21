"""Experience store: the subsystem's local memory of trades, setups, regimes,
strategies, symbols, failures, successful patterns, and reflections.

Backed by the local journal (`storage.Store.journal_add` / `journal_query`),
so experience survives restarts and Worker outage. This is the local half of
the memory.js story: the Worker kept trade history in D1 (cloud); the
subsystem keeps its own retrievable experience locally.

What this is NOT: learning in any deep sense. Like the original reflect.js,
this is retrieval-augmented experience — future agent reasoning can be
handed past reflections ("you took a similar EURUSD long against a bearish
order block and it lost"). Probabilistic, bounded value, honestly framed.

RISK BOUNDARY (non-negotiable): experience and reflections are ADVISORY
data for the external agent. They can never change risk parameters, engage
or disengage the kill switch, or bypass any deterministic control in
core/risk or core/execution. Those layers read only their own config and
the local risk-state store — never the journal.
"""

from storage import Store

from intelligence.reflections.outcome import compute_outcome

# The kinds of experience this store records. Kept as a closed set so
# queries stay meaningful; add a kind deliberately, not casually.
EXPERIENCE_KINDS = frozenset(
    {
        "trade",       # a closed trade with its outcome
        "setup",       # a signal setup (fired or observed)
        "regime",      # a market-regime observation (session/volatility/structure)
        "strategy",    # notes about a named strategy's behavior
        "symbol",      # per-symbol observations
        "failure",     # something that went wrong (missed exit, data gap, ...)
        "pattern",     # a successful pattern worth repeating
        "reflection",  # a post-trade reflection record (see reflections/)
    }
)


class ExperienceStore:
    """Local experience memory backed by the storage journal."""

    def __init__(self, store: Store):
        self._store = store

    def record(
        self,
        kind: str,
        *,
        symbol=None,
        direction=None,
        outcome=None,
        payload=None,
        tags=None,
    ) -> int:
        """Record one experience entry. Returns the journal row id."""
        if kind not in EXPERIENCE_KINDS:
            raise ValueError(
                f"unknown experience kind {kind!r}; expected one of {sorted(EXPERIENCE_KINDS)}"
            )
        entry = {
            "kind": kind,
            "symbol": symbol,
            "direction": direction,
            "outcome": outcome,
            "tags": list(tags or []),
            "detail": dict(payload or {}),
        }
        return self._store.journal_add(entry)

    def query(
        self,
        kind=None,
        symbol=None,
        direction=None,
        outcome=None,
        since=None,
        limit: int = 100,
    ) -> list:
        """Query experience, newest first. Filters mirror journal_query."""
        filters = {}
        if kind is not None:
            filters["kind"] = kind
        if symbol is not None:
            filters["symbol"] = symbol
        if direction is not None:
            filters["direction"] = direction
        if outcome is not None:
            filters["outcome"] = outcome
        if since is not None:
            filters["since"] = since
        return self._store.journal_query(limit=limit, **filters)

    # -- conveniences -------------------------------------------------
    def record_trade(self, *, symbol, direction, entry_price, closed_price,
                     outcome=None, payload=None) -> int:
        """Record a closed trade; outcome is computed if not given."""
        return self.record(
            "trade",
            symbol=symbol,
            direction=direction,
            outcome=outcome or compute_outcome(direction, entry_price, closed_price),
            payload={"entry_price": entry_price, "closed_price": closed_price,
                     **(payload or {})},
        )

    def record_reflection_entry(self, reflection: dict) -> int:
        """Persist a reflection record built by reflections.record_reflection."""
        return self.record(
            "reflection",
            symbol=reflection.get("symbol"),
            direction=reflection.get("direction"),
            outcome=reflection.get("outcome"),
            payload=reflection,
        )
