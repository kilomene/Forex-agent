# API_DEPS.md — what the Python sync layer must implement against

**Audience:** the builder/maintainer of the subsystem's Worker sync layer
(the `worker_client` successor in `daemon/`). This is the implementer's
checklist; the normative reference is [CONTRACT.md](CONTRACT.md).

## 1. Connection

- `WORKER_BASE_URL`: e.g. `https://forex-control-plane.ayiijumo.workers.dev`
  (no trailing slash). All paths below are relative to it.
- Auth header on EVERY request (no exceptions, no unsigned health check):
  `Authorization: Basic base64(client_id:secret)` (preferred) or
  `Authorization: Bearer client_id:secret`. Use a dedicated client,
  e.g. `client_id = "bridge"`. Secrets come from env / a `0600` secrets
  file — never hardcoded, never logged (see `docs/SECURITY.md`).
- Timeouts: 10s connect/read for writes, 15s for polls. On timeout or
  5xx: retry with backoff (e.g. 3 attempts: 1s, 5s, 15s), then fail OPEN
  for reporting paths (log + continue) but fail CLOSED for the kill-switch
  poll (see §4).

## 2. Endpoints the sync layer uses

| Sync job | Method + path | Payload notes |
|---|---|---|
| report signal | `POST /signals` | required: symbol, timeframe, direction, entry_price, stop_loss, take_profit, candle_time. Recommended: trigger, ema_fast/slow, rsi_value, atr_value, recent_candles (≤30 closed candles), smc_summary, symbol_specs. Dedupe BEFORE sending (same symbol+timeframe+candle_time) — the route is not idempotent. ≤30 req/min. |
| poll approvals | `GET /signals/pending-approved` | every ~30s. Reconstruct the order from the Worker's stored copy (source of truth), then run risk checks locally. |
| report execution | `POST /signals/:id/executed` | `{mt5_result, spread_at_entry, lot_size}`. Only valid from `approved`; a `409` means it already transitioned — re-fetch `GET /signals/:id` to reconcile, do not resend the order. |
| report risk rejection | `POST /signals/:id/rejected` | `{reason}`. Only valid from `approved`. |
| report broker failure | `POST /signals/:id/failed` | `{reason, mt5_error}`. NEW — use this when `order_send` fails/returns an error retcode so the ledger doesn't dangle in `approved` forever. |
| report close | `POST /signals/:id/closed` | `{reason, price, commission, swap}` — commission/swap from real MT5 deal history, not estimates. Optional `reflection_text` if the agent already produced one. |
| performance sync | `POST /performance/snapshot` | every ~6h, computed from MT5 deal history. |
| chart sync | `POST /chart-data` | every ~120s per symbol/timeframe. Upsert semantics — safe to resend. |
| kill-switch poll | `GET /kill-switch` | every ~10s. On network failure: treat as ENGAGED (fail closed) until a successful poll proves otherwise. |
| close-all ack | `POST /kill-switch/close-all/complete` | after the local close-all sweep finishes. |

## 3. Response handling rules

- `2xx`: apply the response; where the contract returns `{ok: true}`,
  nothing further is needed.
- `401`: credential wrong/revoked — alert loudly, do NOT retry in a loop
  (auth failures are rate-limited per IP; a tight retry loop will 429
  the source IP).
- `409`: the signal already moved past the state you assumed. Re-fetch
  the signal and reconcile locally — never treat as a transient error.
- `429`: back off (honor at least 60s) — you exceeded the per-client
  rate limit.
- `5xx` / timeout on a write: the write may or may not have applied.
  Re-fetch the signal to check state before retrying the write (all
  state-transition routes are guarded, so a duplicate is a safe `409`,
  not a double-apply — except `POST /signals` itself, which is why you
  dedupe before sending).
- Never log the `Authorization` header, the raw secret, or full request
  dumps containing them.

## 4. Safety-critical semantics (do not reinterpret)

- **Signal ≠ trade.** A `pending` signal from the Worker is a candidate.
  The local execution gateway + risk engine make the final call; the
  Worker never approves on its own anymore.
- **Kill switch fails closed locally.** If `GET /kill-switch` errors or
  times out, assume engaged until proven otherwise.
- **Dry-run default** is enforced locally, independent of the Worker.
- **No AI in the Worker.** `ai_*` fields on signals are written by the
  subsystem/external agent (e.g. deterministic correlation flags in
  `ai_risk_notes` — see CORRELATION_RULE_NOTE.md), never by the Worker.

## 5. Removed — do not call

The old `worker_client` talked to AI-provider routes
(`/settings/ai-provider`, `/settings/custom-ai*`, `/test-vm`,
`/test-model`) and used the `WORKER_API_KEY` shared bearer. All are
gone: provider config belongs to the external agent, auth is per-client.
If any code still references them, delete the call sites.
