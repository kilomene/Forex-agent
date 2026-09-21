# MIGRATION.worker.md — what changed in the Worker

**Date:** 2026-09-21 · Old → new: `forex-signal-worker` → `forex-agent/worker/`
(cloud/sync layer). Companion: [CONTRACT.md](../worker/CONTRACT.md).

## 1. Headline behavior changes

1. **Autonomous auto-approve is GONE.** The old Worker moved a signal
   `pending → approved` by itself when the agent's decision was `enter`.
   The new Worker never moves a signal out of `pending` on its own. A
   signal stays `pending` until the subsystem's signal monitor (external
   agent) or a human approves it via `POST /signals/:id/approve`. Nothing
   can reach the broker without an explicit approval + the bridge's own
   risk checks.
2. **No AI in the Worker, ever.** No reasoning text, no enter/skip
   gating, no reflection text, no provider configuration. `POST /signals`
   stores + pushes. `POST /signals/:id/closed` stores the close + a
   reflection *record*. Reasoning and reflection text are the external
   agent's job; the deterministic correlation-flag rule moved to the
   subsystem core ([CORRELATION_RULE_NOTE.md](../worker/CORRELATION_RULE_NOTE.md)).
3. **New route: `POST /signals/:id/failed`.** The old schema documented
   the `failed` status but no route ever set it, so MT5 `order_send`
   failures left signals dangling in `approved`. The bridge can now
   report them (stored in `failure_reason`, migration `0002`).
4. **Auth replaced.** Single shared `WORKER_API_KEY` bearer →
   per-client `client_id` + secret (hashed, revocable, rotatable).
   Old-style bearer tokens are rejected with 401.

## 2. Route-by-route

| Route | Verdict | Change |
|---|---|---|
| `POST /signals` | CHANGED | stores signal as `pending` + push only; no `runAgent`, no auto-approve, no `skipped_by_agent`; `ai_*` columns stay NULL (subsystem writes them) |
| `GET /signals/pending-approved` | KEPT | unchanged semantics |
| `POST /signals/:id/executed` | CHANGED | now requires status `approved` (409 otherwise); was unguarded |
| `POST /signals/:id/rejected` | CHANGED | same `approved`-only guard added |
| `POST /signals/:id/failed` | NEW | see §1.3 |
| `GET /signals`, `GET /signals/:id` | KEPT | unchanged |
| `POST /signals/:id/approve` | CHANGED | kill-switch 409 guard kept; the Worker itself never approves anymore |
| `POST /signals/:id/reject` | KEPT | unchanged |
| `POST /signals/:id/closed` | CHANGED | keeps outcome math + reflection *record*; LLM reflection generation removed; accepts optional `reflection_text` from the agent; 409 on double-close |
| `POST /devices/register`, `POST /devices/test-push` | KEPT | unchanged |
| `GET/POST /settings/trading-mode` | CHANGED | kept for the app toggle; `autonomous` now only signals the *subsystem* it may self-approve — the Worker never approves either way |
| `GET /kill-switch`, `POST /kill-switch`, `POST /kill-switch/close-all`, `POST /kill-switch/close-all/complete` | KEPT | unchanged semantics |
| `POST /performance/snapshot`, `GET /performance/latest` | KEPT | unchanged |
| `GET /reflections` | KEPT | unchanged (records only; text comes from the agent) |
| `POST /chart-data`, `GET /chart-data/available`, `GET /chart-data` | KEPT | unchanged |
| `GET /settings/ai-provider` | REMOVED | provider config belongs to the external agent |
| `GET/POST /settings/custom-ai`, `/clear`, `/test-vm`, `/test-model` | REMOVED | same; legacy `custom_ai_*` settings rows are ignored if present |

## 3. Modules removed (with dependency-trace justification)

Per AUDIT §2.4, the entire AI surface had exactly **two** call sites
(`reasoning.js`, `reflect.js` → `ai/index.js`), and deleting all of
`src/ai/` kills only: reasoning text, enter/skip gating, reflection text.
Signal lifecycle, D1, kill switch, FCM all survive — verified by the
contract test suite (`worker/tests/contract.test.mjs`, 20 tests green).

- REMOVED: `src/ai/*` (7 files: index, prompt, anthropic,
  anthropic_tools, openrouter, openrouter_tools, custom)
- REMOVED: `src/agent/agentLoop.js`, `openrouterAgentLoop.js`
  (multi-turn tool-calling harnesses), `agentPrompt.js`, `parse.js`
  (model-output handling), `reflectPrompt.js` (LLM reflection prompt),
  `dynamicSettings.js` (app-configured provider override)
- REHOMED (out of the Worker, into the subsystem — not deleted from the
  project): `src/agent/tools.js` → agent capabilities;
  `src/agent/knowledge.js` → `intelligence/correlation/` (static tables +
  the correlation rule, see CORRELATION_RULE_NOTE.md);
  `src/agent/memory.js`, `src/agent/context.js` → subsystem
  data-assembly; `src/agent/reflect.js` → `src/reflect.js` (persistence
  only: `computeOutcome` + record insert, no prompt, no provider call)

## 4. Other changes

- **FCM** (`src/fcm.js`): OAuth token cached until near-expiry (was
  re-minted per device per push).
- **Rate limiting** (`src/ratelimit.js`, new): per-client buckets —
  30/min ingest, 120/min general, 20/min auth-failures per IP;
  env-overridable; 429 with `RATE_LIMITED` code.
- **Structured errors**: every error now carries a machine `code`
  (`AUTH_REQUIRED`, `VALIDATION_ERROR`, `NOT_FOUND`, `CONFLICT`,
  `RATE_LIMITED`, `INTERNAL`).
- **Schema/D1**: `schema.sql` is the canonical fresh-deploy schema;
  `migrations/` is append-only and non-destructive (`0001` api_clients,
  `0002` signals.failure_reason). Existing D1 data survives upgrades.
- **`wrangler.toml`**: `database_id` kept as a clearly-marked
  DEPLOY-TIME placeholder; AI secrets removed from the comments;
  rate-limit tunables as `[vars]`.

## 5. For the app (legacy mobile client)

The app's AI-provider settings screens (`/settings/ai-provider`,
`/settings/custom-ai*`) will 404 — those screens should be removed or
hidden in any future dashboard. Everything else the app used (signals,
approve/reject, kill switch, trading mode, devices, performance,
reflections, chart data) is unchanged.
