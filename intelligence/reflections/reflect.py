"""Post-trade reflection pipeline: outcome → performance → reflection → experience.

What was ported from the Worker's `src/agent/reflect.js`:
  - `computeOutcome` (pure outcome math) → `intelligence/reflections/outcome.py`
  - the "store a structured reflection of every close" discipline → here.

What was deliberately NOT ported: the LLM prompt (`reflectPrompt.js`) and
the provider call (`askProvider`). Reflection *text generation* is
external-agent reasoning in the new architecture (the agent reads the
structured record below and writes its own review). This module produces
only deterministic, auditable facts.

RISK BOUNDARY (non-negotiable, restated from experience/store.py):
a reflection record is advisory. It never modifies risk parameters, never
touches the kill switch, never approves or blocks a trade. Deterministic
risk controls live in core/risk + core/execution and do not read the
journal. Nothing in this module can weaken them — there is no code path
from here to there.
"""

from datetime import datetime, timezone

from intelligence.experience.store import ExperienceStore
from intelligence.reflections.outcome import compute_outcome


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def build_performance(*, direction, entry_price, stop_loss, take_profit, closed_price) -> dict:
    """Deterministic performance facts for a closed trade. No judgement, just math."""
    outcome = compute_outcome(direction, entry_price, closed_price)
    diff = closed_price - entry_price if direction.upper() == "BUY" else entry_price - closed_price
    planned_risk = abs(entry_price - stop_loss) if stop_loss else None
    planned_reward = abs(take_profit - entry_price) if take_profit else None
    r_multiple = (diff / planned_risk) if planned_risk else None
    return {
        "outcome": outcome,
        "price_diff": diff,
        "r_multiple": r_multiple,
        "planned_risk": planned_risk,
        "planned_reward": planned_reward,
        "planned_rr": (planned_reward / planned_risk) if planned_risk and planned_reward else None,
    }


def record_reflection(
    experience: ExperienceStore,
    signal,
    *,
    closed_price: float,
    closed_at=None,
    close_reason: str = None,
    reflection_text: str = None,
) -> dict:
    """Build and persist the deterministic reflection record for a closed signal.

    `signal`: dict/object with symbol, direction, entry_price, stop_loss,
      take_profit, candle_time, trigger, strategy (any subset ok).
    Returns the stored reflection dict. `reflection_text`, when supplied
    (e.g. written by the external agent), is stored verbatim as the agent's
    own review; when absent the record notes the review is pending — the
    subsystem never invents one.
    """
    direction = _field(signal, "direction")
    entry_price = _field(signal, "entry_price")
    performance = build_performance(
        direction=direction,
        entry_price=entry_price,
        stop_loss=_field(signal, "stop_loss"),
        take_profit=_field(signal, "take_profit"),
        closed_price=closed_price,
    )
    record = {
        "symbol": _field(signal, "symbol"),
        "direction": direction,
        "strategy": _field(signal, "strategy"),
        "trigger": _field(signal, "trigger"),
        "entry_price": entry_price,
        "stop_loss": _field(signal, "stop_loss"),
        "take_profit": _field(signal, "take_profit"),
        "signal_time": str(_field(signal, "candle_time")),
        "closed_price": closed_price,
        "closed_at": closed_at or datetime.now(timezone.utc).isoformat(),
        "close_reason": close_reason,
        "outcome": performance["outcome"],
        "performance": performance,
        "reflection_text": reflection_text
        if reflection_text is not None
        else "No agent review recorded yet — pending external-agent reflection.",
    }
    experience.record_reflection_entry(record)
    return record
