"""Outcome arithmetic for closed trades.

Canonical Python port of `computeOutcome` from the Worker's
`src/agent/reflect.js`. Pure math — win/loss/breakeven from entry vs close
price and direction — not opinion. No LLM, no I/O.
"""


def compute_outcome(direction: str, entry_price: float, closed_price: float) -> str:
    """Return 'win' | 'loss' | 'breakeven'.

    direction: "BUY" or "SELL" (case-insensitive).
    """
    direction = (direction or "").upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"direction must be BUY or SELL, got {direction!r}")
    diff = closed_price - entry_price if direction == "BUY" else entry_price - closed_price
    if diff > 0:
        return "win"
    if diff < 0:
        return "loss"
    return "breakeven"
