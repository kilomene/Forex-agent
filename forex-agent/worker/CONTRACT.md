# Worker API Contract

**Version:** 2.0 (agent-native migration) · **Date:** 2026-09-21

Machine-readable contract for the forex-agent cloud/sync Worker. The Python
sync layer (bridge `worker_client`) contract-tests against this document.
Every route is authenticated (per-client credentials, §0). All request and
response bodies are JSON. Timestamps are ISO 8601 strings. Prices are raw
broker price units (not pips).

## 0. Auth

Every request MUST carry one of:

```
Authorization: Basic base64(client_id:secret)      # preferred
Authorization: Bearer client_id:secret
```

Failure → `401 {"error":"Unauthorized","code":"AUTH_REQUIRED"}` with a
`WWW-Authenticate` header. All failure reasons (unknown client, revoked
client, bad secret, malformed header) are indistinguishable by design.

## 1. Conventions

- `Content-Type: application/json` on all responses.
- Error shape: `{"error": "<human message>", "code": "<MACHINE_CODE>"}`.
- Error codes: `AUTH_REQUIRED` (401) · `VALIDATION_ERROR` (400) ·
  `NOT_FOUND` (404) · `CONFLICT` (409) · `RATE_LIMITED` (429) ·
  `INTERNAL` (500).
- `409 CONFLICT` means "the resource exists but is not in a state that
  allows this transition" — never retry blindly; re-fetch and reconcile.
- Rate limits (per client_id, per 60s window, best-effort per isolate):
  `POST /signals` ≤ 30/min · all routes ≤ 120/min · failed auth ≤ 20/min
  per source IP. Exceeded → `429 {"error":...,"code":"RATE_LIMITED"}`.
- `POST /signals` is NOT idempotent — the reporter must dedupe (e.g. by
  `candle_time` per symbol) before reporting. State-transition routes are
  guarded: repeating a completed transition returns `409`, which the
  client should treat as "already applied, re-fetch to confirm".

## 2. Signal object (as returned by GET)

```jsonc
{
  "id": "uuid",
  "symbol": "EURUSD", "timeframe": "M15", "direction": "BUY|SELL",
  "entry_price": 1.085, "stop_loss": 1.08, "take_profit": 1.095,
  "ema_fast": 1.084, "ema_slow": 1.082, "rsi_value": 58.2, "atr_value": 0.0012,
  "candle_time": "2026-09-21T12:00:00.000Z",
  "trigger_text": "EMA20 crossed above EMA50, RSI=58.2",
  "ai_reasoning": null, "ai_confidence": null, "ai_risk_notes": null,
  "ai_tool_calls": null, "ai_decision": null,   // written by the subsystem/agent, never the Worker
  "spread_at_entry": 0.0002, "lot_size": 0.1, "commission": -1.5, "swap": 0.0,
  "recent_candles": "[...]", "smc_summary": "{...}", "symbol_specs": "{...}",  // JSON strings or null
  "status": "pending|approved|executed|closed|rejected_by_user|rejected_by_risk|failed|skipped_by_agent",
  "mt5_result": "{...}",            // JSON string, set on executed
  "rejection_reason": "...",        // set on rejected_by_risk
  "failure_reason": "...",          // set on failed
  "closed_reason": "trailing_stop", "closed_price": 1.09, "closed_at": "2026-09-21T14:00:00.000Z",
  "created_at": "...", "updated_at": "..."
}
```

`skipped_by_agent` is legacy (old Worker auto-flow) — the new Worker never
writes it, but old rows may exist.

## 3. Routes

### 3.1 `POST /signals` — report a newly detected signal

Stores the signal as `pending` and fires a push notification. Performs no
reasoning and never auto-approves.

Request:
```jsonc
{
  "symbol": "EURUSD",            // required
  "timeframe": "M15",            // required
  "direction": "BUY",            // required: BUY | SELL
  "entry_price": 1.085,          // required
  "stop_loss": 1.08,             // required
  "take_profit": 1.095,          // required
  "candle_time": "2026-09-21T12:00:00.000Z",  // required: closed candle that fired
  "trigger": "EMA20 crossed above EMA50",    // optional -> trigger_text
  "ema_fast": 1.084, "ema_slow": 1.082, "rsi_value": 58.2, "atr_value": 0.0012, // optional
  "recent_candles": [ {"time":"...","open":1,"high":2,"low":0.5,"close":1.5} ], // optional
  "smc_summary": {},             // optional
  "symbol_specs": {}             // optional
}
```
Responses: `201 {"signal_id": "uuid"}` ·
`400 VALIDATION_ERROR` (missing required field) · `401` · `429`.

### 3.2 `GET /signals/pending-approved` — poll approved-but-unexecuted

Response: `200 {"signals": [<signal>, ...]}` ordered oldest-first. Empty
array (not 404) when none.

### 3.3 `POST /signals/:id/executed` — report execution result

Request: `{"mt5_result": {...}, "spread_at_entry": 0.0002, "lot_size": 0.1}`
(all optional). Guards: `404` unknown id · `409` unless status is
`approved`. Response: `200 {"ok": true}`. Sends push.

### 3.4 `POST /signals/:id/rejected` — report risk-check rejection

Request: `{"reason": "..."}` (optional). Guards: `404` · `409` unless
`approved`. Sets `rejected_by_risk` + `rejection_reason`. Response:
`200 {"ok": true}`. Sends push.

### 3.5 `POST /signals/:id/failed` — report broker order failure (NEW)

Request: `{"reason": "...", "mt5_error": "..."}` (at least one
recommended; stored truncated to 500 chars). Guards: `404` ·
`409` if status is `closed` or `executed`. Sets `failed` +
`failure_reason`. Response: `200 {"ok": true}`. Sends push.

### 3.6 `GET /signals?status=<s>&limit=<n>` — list signals

Query: `status` optional (exact status string) · `limit` optional,
default 50. Response: `200 {"signals": [...]}` newest-first.

### 3.7 `GET /signals/:id` — one signal

Response: `200 {"signal": <signal>}` · `404`.

### 3.8 `POST /signals/:id/approve` — approve a pending signal

No body. Guards: `404` · `409` unless `pending` · `409` while the kill
switch is engaged. Response: `200 {"ok": true}`. The approver is the
external agent (subsystem signal monitor) or a human — the Worker never
approves on its own.

### 3.9 `POST /signals/:id/reject` — reject a pending signal

No body. Guards: `404` · `409` unless `pending`. Sets
`rejected_by_user`. Response: `200 {"ok": true}`.

### 3.10 `POST /signals/:id/closed` — report a closed position

Request:
```jsonc
{
  "reason": "trailing_stop",   // optional free text
  "price": 1.09,               // close price
  "commission": -1.5,          // optional, real MT5 deal-history cost
  "swap": 0.0,                 // optional, real MT5 deal-history cost
  "reflection_text": "..."     // optional: external agent's reflection text
}
```
Guards: `404` · `409` if already `closed`. The Worker computes the
deterministic outcome (`win|loss|breakeven`) and persists a reflection
RECORD (see §4); it never generates reflection text. If
`reflection_text` is absent, the record is stored with a placeholder.
Response: `200 {"ok": true, "outcome": "win"}`. Sends push.

### 3.11 `POST /devices/register` — register an FCM token

Request: `{"token": "..."}` → `400 VALIDATION_ERROR` if missing.
Response: `200 {"ok": true}`. Idempotent (upsert on token).

### 3.12 `POST /devices/test-push` — test push to all devices

Response: `200 {"ok": bool, "sent": n, "total": m}` ·
`400` when no devices are registered.

### 3.13 `GET /settings/trading-mode`

Response: `200 {"trading_mode": "manual"|"autonomous"}` (default
`manual`).

### 3.14 `POST /settings/trading-mode`

Request: `{"mode": "manual"|"autonomous"}` → `400` otherwise. Response:
`200 {"ok": true, "trading_mode": "<mode>"}`. Meaning: in `autonomous`
the subsystem's signal monitor may approve without a human tap; the
Worker itself never approves either way.

### 3.15 `GET /kill-switch`

Response: `200 {"engaged": bool, "close_all_requested_at": "iso|null"}`.

### 3.16 `POST /kill-switch`

Request: `{"engaged": true|false}` → `400` otherwise. While engaged, no
new entry can happen (`/approve` returns `409`; the bridge checks
independently). Response: `200 {"ok": true, "engaged": bool}`. Sends push.

### 3.17 `POST /kill-switch/close-all`

One-shot emergency request (not a toggle). Response: `200 {"ok": true}`.
Sends push.

### 3.18 `POST /kill-switch/close-all/complete`

Request: `{"closed_count": n}` (optional). Clears the one-shot request.
Response: `200 {"ok": true}`. Sends push.

### 3.19 `POST /performance/snapshot`

Request (fields optional except the counts/totals the reporter sends):
```jsonc
{
  "period_start": "...", "period_end": "...",
  "total_closed_trades": 10, "wins": 6, "losses": 4, "breakeven": 0,
  "win_rate_pct": 60.0, "total_profit": 120.5,
  "average_win": 25.0, "average_loss": -12.0, "profit_factor": 2.1,
  "largest_win": 40.0, "largest_loss": -20.0,
  "by_symbol": {}, "total_commission": -5.0, "total_swap": -0.5,
  "net_profit": 115.0
}
```
Response: `201 {"ok": true, "id": "uuid"}`.

### 3.20 `GET /performance/latest`

Response: `200 {"available": true, "snapshot": {...}}` or
`200 {"available": false}`.

### 3.21 `GET /reflections?symbol=<s>&limit=<n>`

Response: `200 {"reflections": [...]}` newest-first. Reflection record:
```jsonc
{
  "id": "uuid", "signal_id": "uuid", "symbol": "EURUSD",
  "direction": "BUY", "outcome": "win|loss|breakeven",
  "entry_price": 1.085, "closed_price": 1.09,
  "original_reasoning": null, "original_confidence": null,
  "reflection_text": "...", "created_at": "..."
}
```

### 3.22 `POST /chart-data`

Request:
```jsonc
{
  "symbol": "EURUSD", "timeframe": "H1",   // required
  "candles": [ {"time":"...","open":1,"high":2,"low":0.5,"close":1.5} ],
  "ema_fast": [null, 1.2], "ema_slow": [null, 1.1],
  "ema_fast_period": 20, "ema_slow_period": 50
}
```
Upserts per (symbol, timeframe). Response: `200 {"ok": true}` ·
`400` when symbol/timeframe missing.

### 3.23 `GET /chart-data/available`

Response: `200 {"charts": [{"symbol","timeframe","updated_at"}, ...]}`.

### 3.24 `GET /chart-data?symbol=<s>&timeframe=<tf>`

Response: `200 {"available": true, "symbol", "timeframe", "candles": [...],
"ema_fast": [...]|null, "ema_slow": [...]|null, "ema_fast_period",
"ema_slow_period", "updated_at"}` or `200 {"available": false}` ·
`400` when params missing.

## 4. Reflection persistence (no generation)

`POST /signals/:id/closed` persists a `reflections` row with the
deterministic outcome from `computeOutcome(direction, entry, close)`:
`BUY`: close > entry → win, < → loss, = → breakeven (mirrored for SELL).
The Worker never produces reflection text; the external agent supplies it
via `reflection_text`, or the row keeps a placeholder noting the text is
pending.

## 5. Removed in v2 (do not implement against)

`GET /settings/ai-provider`, `GET|POST /settings/custom-ai`,
`POST /settings/custom-ai/clear`, `POST /settings/custom-ai/test-vm`,
`POST /settings/custom-ai/test-model` — provider configuration belongs
to the external agent. `env.WORKER_API_KEY` bearer auth — replaced by
per-client credentials (§0).
