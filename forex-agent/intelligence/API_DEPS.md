# API_DEPS — intelligence/

Cross-area imports made by `intelligence/` (owner: intelligence-builder).
`intelligence/` never imports `broker`, `core.execution`, `MetaTrader5`, or any LLM.

| Import | From | Expected signature / contract |
|---|---|---|
| `from storage import Store` | `intelligence/experience/store.py` (`ExperienceStore`) | `Store(path)`; `journal_add(entry: dict) -> int` (entry requires `kind`); `journal_query(limit=100, **filters) -> list[dict]` with filter keys `kind, symbol, direction, outcome, since, until`. Owner: intelligence-builder (storage/). |
| `from intelligence.reflections.outcome import compute_outcome` | `intelligence/experience/store.py::record_trade` | `compute_outcome(direction: str, entry_price: float, closed_price: float) -> "win" \| "loss" \| "breakeven"`. Same package area. |
| `from intelligence.experience.store import ExperienceStore` | `intelligence/reflections/reflect.py::record_reflection` | `ExperienceStore.record_reflection_entry(reflection: dict) -> int`; `ExperienceStore(store)`; `query(...)`. Same package area. |
| `from intelligence.ml import FEATURE_COLUMNS` | `backtesting/engine.py::extract_features` (function-local) | canonical 8-feature schema list (see backtesting/API_DEPS.md). |
| strategy/signal shapes | `intelligence/correlation/knowledge.py::check_correlated_exposure` (called by core/risk + agent/tools) | `new_signal`: `{symbol: str, direction: "BUY"\|"SELL"}` (dict or object). `open_positions`: iterable of `{symbol, direction, status?}` — **must be the broker-reported open list** (`BrokerAdapter.positions()`), not a ledger; entries with non-`"open"` status are defensively skipped. |

Consumers OF intelligence (for their API_DEPS):
- `core/risk` → `intelligence.correlation.check_correlated_exposure` (correlation exposure limits; TARGET §3).
- `agent/tools` → `intelligence.correlation.{current_session_info, check_correlated_exposure, correlated_pairs}`, `intelligence.economic_calendar.get_calendar`, `intelligence.ml.get_ml_prediction` (capability backends).
- `daemon/*` → `intelligence.experience.ExperienceStore`, `intelligence.reflections.reflect.record_reflection` (post-close pipeline).
- `backtesting` → `intelligence.ml.FEATURE_COLUMNS` (label schema).
