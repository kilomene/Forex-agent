# forex-agent Worker — cloud/sync layer

Cloudflare Worker (ESM JavaScript, **zero npm runtime deps** — fetch,
crypto/Web Crypto only). This is the cloud half of the system: a
persistent ledger and sync point. It contains **no AI reasoning, no LLM
calls, and no trading decisions**.

## What it owns (per TARGET ARCHITECTURE §6)

- Signal lifecycle persistence: `pending → approved → executed → closed`
  (plus `rejected_by_user`, `rejected_by_risk`, `failed`)
- Kill-switch state + emergency close-all coordination
- Settings (trading mode), device tokens, chart data, performance
  snapshots, reflection *records*
- FCM push notifications to registered devices

What it does NOT do: reason about signals, approve anything by itself,
generate reflection text, or call any model provider. The external agent
is the brain; the subsystem core is the deterministic engine; this Worker
is the cloud notebook they both write in.

## Layout

```
worker/
├── src/
│   ├── index.js      # router: all routes (see CONTRACT.md)
│   ├── db.js         # all D1 access
│   ├── auth.js       # per-client credentials (client_id + secret)
│   ├── fcm.js        # FCM v1 push, OAuth token cached until near-expiry
│   ├── reflect.js    # computeOutcome math + reflection record persistence
│   └── ratelimit.js  # per-client rate limiting (best-effort, per isolate)
├── tests/
│   └── contract.test.mjs   # node:test suite, in-memory FakeD1
├── migrations/       # numbered, append-only, non-destructive D1 migrations
├── scripts/
│   └── provision-client.mjs  # generate client_id + secret, print D1 SQL
├── schema.sql        # canonical full schema for FRESH deploys
├── wrangler.toml     # deploy config (database_id = DEPLOY-TIME placeholder)
├── CONTRACT.md       # machine-readable route contract (sync layer tests against this)
├── API_DEPS.md       # what the Python sync layer must implement
└── CORRELATION_RULE_NOTE.md  # deterministic correlation rule (moved to subsystem core)
```

## Deploy checklist

1. **Set the database id.** `wrangler.toml` ships with
   `database_id = "REPLACE_WITH_REAL_DATABASE_ID"` — a DEPLOY-TIME
   placeholder, clearly marked. Create the DB
   (`wrangler d1 create forex-signals-db`) and paste the real id.
   The Worker cannot serve requests without it.
2. **Apply the schema.** Fresh DB: `wrangler d1 execute forex-signals-db
   --file=./schema.sql`. Existing DB from the old worker: apply
   `migrations/*.sql` in numeric order instead (never destructive).
3. **Set secrets** (never in the toml, never in git):
   `wrangler secret put FCM_SERVICE_ACCOUNT_JSON`,
   `wrangler secret put FCM_PROJECT_ID`.
   Do NOT set the old `WORKER_API_KEY` / `ANTHROPIC_API_KEY` /
   `OPENROUTER_*` / `CUSTOM_AI_*` — the Worker no longer reads them.
4. **Provision API clients** — at least one (e.g. `bridge`) before any
   client can connect:
   ```bash
   node scripts/provision-client.mjs bridge "Windows MT5 bridge"
   wrangler d1 execute forex-signals-db --command="<printed SQL>"
   ```
   Hand the printed secret to the client operator once; store it in the
   client's secret store. Rotation/revocation: `docs/SECURITY.md`.
5. **(Recommended)** Add a Cloudflare dashboard rate-limiting rule on the
   Worker route for a hard global cap — the in-code limiter
   (`src/ratelimit.js`) is per-isolate and best-effort.
6. `wrangler deploy`.

## Local verification (no wrangler needed)

```bash
node --test 'worker/tests/*.test.mjs'   # 20 contract tests, in-memory FakeD1
node --check src/index.js                # syntax check any src file you touch
```

## Key behavior notes

- `POST /signals` stores the signal as `pending` and pushes — nothing
  else. There is no auto-approve; approval comes from the subsystem's
  signal monitor or a human via `POST /signals/:id/approve`.
- `POST /signals/:id/failed` (new) lets the bridge report MT5
  `order_send` failures so the ledger reflects reality.
- Auth failures are indistinguishable (unknown/revoked/bad → same 401)
  and throttled per source IP.
