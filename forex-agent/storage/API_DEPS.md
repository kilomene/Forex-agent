# API_DEPS — storage/

`storage/` is the **local** persistence layer. It has **no cross-area imports**:
it depends only on the Python standard library (`sqlite3`, `threading`, `json`).

Other areas import FROM storage (never the reverse):

| Consumer | Imports | Expected surface |
|---|---|---|
| `intelligence.experience` | `from storage import Store` | `Store(path)`; `journal_add(dict) -> int`; `journal_query(limit=100, **filters) -> list[dict]` with filter keys `kind, symbol, direction, outcome, since, until` |
| `intelligence.reflections` | `from storage import Store` | same as above (writes `kind="reflection"` entries) |
| `core.risk` (core-builder) | `from storage import Store` | `get_risk_state() -> dict` / `set_risk_state(dict)` for daily-loss baseline + loss-streak persistence |
| `core.execution` (core-builder) | `from storage import Store` | `get_kill_switch() -> {"engaged": bool, "source": str\|None, "ts": str\|None}` / `set_kill_switch(engaged, source)` |
| `agent.events` (agent-builder) | `from storage import Store` | `enqueue_event(dict) -> int` / `dequeue_events(limit=100) -> list[dict]` (FIFO; returned dicts carry `_queue_id`, `_ts`) |
| any area | `from storage import Store` | `audit(dict) -> int` / `query_audit(limit=100, since=None) -> list[dict]` newest-first; returned dicts carry `_audit_id`, `_ts` |

DDL is the single source of truth: `storage/schemas/schema.sql` (executed by
`Store.__init__` via `executescript`). This schema is intentionally separate
from the Worker D1 schema (`worker/schema.sql`, cloud layer).
