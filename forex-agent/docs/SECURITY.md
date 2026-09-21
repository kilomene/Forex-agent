# SECURITY.md — forex-agent Worker (cloud/sync layer)

**Date:** 2026-09-21 · Scope: the Cloudflare Worker only. Subsystem-side
secret handling (env/`0600` files, never in code/logs) is covered in the
top-level docs; the Worker side is specified here.

## 1. Authentication: per-client credentials

The old single shared secret (`WORKER_API_KEY`, one bearer token for the
bridge and the app alike) is **gone**. Every client now has its own
provisioned record in the `api_clients` D1 table:

| Column | Meaning |
|---|---|
| `client_id` | unique, e.g. `bridge`, `app`, `agent` |
| `secret_hash` | SHA-256 hex digest of the secret — the raw secret is NEVER stored |
| `label` | human note ("Windows MT5 bridge") |
| `revoked` | `1` = rejected |
| `created_at` / `last_used_at` | provisioning + last successful use (best-effort) |

Wire format (both accepted):

```
Authorization: Basic base64(client_id:secret)      # preferred
Authorization: Bearer client_id:secret
```

Verification (`src/auth.js`): parse → look up `client_id` → reject if
unknown or `revoked` → SHA-256 the presented secret → constant-time
compare against `secret_hash`. All failure modes return an identical
`401 {"error":"Unauthorized","code":"AUTH_REQUIRED"}` — no oracle for
"client exists but secret wrong".

### Provisioning

```bash
node scripts/provision-client.mjs <client_id> [label]
# prints the raw secret ONCE + the SQL to register the hash in D1
wrangler d1 execute forex-signals-db --command="<the printed SQL>"
```

- Generate one client per consumer (`bridge`, `app`, `agent`, …) — never
  share one credential across consumers; the `label` column should say
  what each one is.
- The raw secret is shown once. Hand it to the client operator and store
  it in the client's secret store. If it is lost, rotate (below) — there
  is no "recover the secret" path, by design.

### Rotation

Run the provision script again with the **same** `client_id` and apply the
SQL. The `ON CONFLICT(client_id)` clause replaces the stored hash and
un-revokes the client, immediately invalidating the old secret. Steps:

1. Provision new secret, apply SQL.
2. Update the client to the new secret.
3. Confirm the client authenticates (one `GET /kill-switch`).
4. Delete the old secret everywhere it was stored.

Rotate on a schedule (e.g. quarterly) and immediately if a secret may
have leaked (log exposure, chat paste, device loss).

### Revocation

```bash
wrangler d1 execute forex-signals-db \
  --command="UPDATE api_clients SET revoked = 1 WHERE client_id = '<client_id>';"
```

Takes effect on the next request. Revoke before decommissioning any
client, and revoke-then-rotate if a device with a stored secret is lost.

## 2. Secret handling (Worker side)

- Worker secrets are set via `wrangler secret put <NAME>` — never in
  `wrangler.toml`, never in code, never in git. Current secrets:
  `FCM_SERVICE_ACCOUNT_JSON`, `FCM_PROJECT_ID`. (Removed: `WORKER_API_KEY`,
  `ANTHROPIC_API_KEY`, `OPENROUTER_*`, `CUSTOM_AI_*` — the Worker no longer
  reads them; do not set them.)
- Secrets are **never logged**. `src/auth.js` and `src/fcm.js` contain no
  logging of credentials; error paths log status codes only. If you add
  logging anywhere in this Worker, grep for `Authorization`, `secret`,
  `api_key`, `private_key` before committing.
- Client secrets are SHA-256-hashed at rest in D1. SHA-256 is a
  fast hash, not a password KDF — this is acceptable because secrets are
  256-bit random tokens (see provision script), not human passwords.
  Do not let a human-chosen weak secret into this table.

## 3. Rate limiting

Enforced in-code (`src/ratelimit.js`), per `client_id`, fixed 60s windows:

| Bucket | Default | Scope |
|---|---|---|
| ingest | 30/min | `POST /signals`, per client |
| general | 120/min | all routes, per client |
| auth-failure | 20/min | failed auth attempts, per source IP |

Exceeded → `429 {"code":"RATE_LIMITED"}`. Tunables are Worker vars
(`RATE_LIMIT_*_PER_MIN`) in `wrangler.toml`; setting one to `0`/empty
disables that bucket. This is **best-effort per isolate** — isolates keep
their own counters, so it bounds abusive clients rather than enforcing a
hard global cap. For strict global limits, add a Cloudflare dashboard
rate-limiting rule on the Worker route as well.

## 4. FCM

- The OAuth access token is cached in module memory until within 5
  minutes of expiry (`src/fcm.js`) — no more per-device-per-push minting.
- `FCM_SERVICE_ACCOUNT_JSON` is parsed per send from env; the private key
  never leaves the Worker and is never logged. Push failures are logged
  (status code only) and never thrown — a push outage must not break
  signal reporting.

## 5. D1 considerations

- **Migrations are append-only and additive.** `schema.sql` is the
  canonical schema for fresh deploys; existing databases are upgraded via
  `migrations/*.sql` in numeric order. Never drop/rename a column on a
  live database. (D1/SQLite has no `ADD COLUMN IF NOT EXISTS` — retrying
  an applied migration errors on the duplicate column, which is harmless
  but check the column exists.)
- `api_clients` rows are security state: include D1 in your backup story
  (`wrangler d1 export`). Losing the table locks every client out until
  re-provisioned.
- The `settings` table may contain legacy `custom_ai_*` rows from the old
  Worker — they are ignored and harmless; no cleanup migration deletes
  user data.

## 6. Threat model notes (what this does and doesn't cover)

- Covered: credential separation per consumer, revocation, rotation,
  brute-force throttling, no secret leakage in logs/responses, kill-switch
  defense in depth (Worker approve-gate + bridge risk check).
- Not covered (out of scope for this layer): D1 encryption at rest is
  Cloudflare's; transport security is TLS (never call the Worker over
  plain HTTP); the Worker's push path trusts FCM with notification
  content (no prices are sensitive, but be aware notification bodies
  traverse Google's infrastructure).
