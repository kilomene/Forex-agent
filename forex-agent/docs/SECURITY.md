# SECURITY.md — forex-agent

**Date:** 2026-09-21 · Scope: the whole subsystem (A: Linux forex-agent
below) plus the Cloudflare Worker (B: cloud/sync layer).

**Dated security review:** `docs/SECURITY_REVIEW_2026-09-21.md` (2026-09-21,
phase 8). It found and fixed 6 issues: (HIGH) `forex config --json`
leaked a token-embedded `NOTIFY_WEBHOOK_URL` — `_redact` now masks
`webhook` substrings and `login`/`server` keys; (MEDIUM)
`AppConfig.redacted()` now covers all `SECRET_KEYS` including
`MT5_LOGIN`/`MT5_SERVER`; (MEDIUM) bearer credentials
(`MT5_GATEWAY_TOKEN`, `NOTIFY_WEBHOOK_URL`, `NEWS_CALENDAR_API_KEY`)
added to the secrets contract and the installer's 0600 secrets file;
(LOW) plain-HTTP gateway URLs now warn; (LOW) `/events` limits clamped
to [1, 1000]; (LOW) installer/supervisor home dirs chmod 700. 16
regression tests in `tests/test_security.py`. Residual accepted risks:
account `login` shown on localhost-only operator surfaces; installer
`sed` on operator-supplied paths; umask-inherited log DB perms. See the
dated report for full detail.

---

# Part A — the forex-agent subsystem (Linux)

## 1. Localhost-only API

- `scripts/local_api.py` binds **127.0.0.1 only** — never `0.0.0.0`.
  The loopback bind IS the access control; there is no auth on the API
  or the SSE stream, by design.
- **Never expose this port beyond the host.** Anyone who can reach it
  can read events, account/positions data, and submit trade requests
  (still gated by the execution gateway, §5–§7, but readable and
  submittable).
- `FOREX_API_PORT` (default 8765) only changes the port, never the
  bind address.
- The SSE stream holds one server thread per connection (stdlib
  `ThreadingHTTPServer`) — sized for a handful of local agent
  subscribers, not for many concurrent streams.

## 2. Secret handling and redaction

- Secrets live in **exactly two places**: environment variables, or
  `$FOREX_AGENT_HOME/secrets.env` at **mode 0600**. The installer
  creates it at 0600, never overwrites it on re-runs, never prints its
  contents; `config/config.py` logs a warning if the file is readable
  by group/other.
- `AppConfig.redacted()` is the **only printable form** of the config.
  `scripts/forex config --json` redacts secret fields; `scripts/forex
  notify --status` masks `webhook_url`; webhook channel logs carry the
  host only, never the full URL (the URL may embed a token).
- Event payloads carry market/trade/risk facts — **never credentials**.
  Nothing in the event journal, logs, or `install-result.json`
  contains a secret.
- The MT5 gateway bearer token (`MT5_GATEWAY_TOKEN`) is mandatory:
  `RemoteMT5GatewayTransport` refuses to construct without it, so an
  unauthenticated gateway connection cannot exist by accident.

## 3. MT5 gateway: auth + allowlist

- The remote MT5 gateway (`broker/mt5/gateway.py`) authenticates every
  request with `Authorization: Bearer <MT5_GATEWAY_TOKEN>`. Missing
  token → construction refused. Wrong token → `GATEWAY_AUTH_FAILED`;
  unreachable host → `GATEWAY_UNREACHABLE`. All surface as structured
  errors, never exceptions leaking the token.
- The transport exposes exactly the 12 contracted operations in
  `broker/mt5/gateway_contract.md` (`OPERATIONS` in `gateway.py`).
  Any other operation name raises `GATEWAY_REJECTED` **before any
  network I/O** — there is no "run arbitrary command" op, so a
  compromised or confused caller cannot be talked into raw terminal
  access.
- `import MetaTrader5` occurs only under `broker/mt5/`; everything else
  talks to `BrokerAdapter`.

## 4. Input validation

- The event bus (`agent/events/bus.py`) schema-validates every event
  before journaling; malformed events are rejected, not stored.
- Trade requests are validated in the gateway: symbol whitelist
  (`INVALID_SYMBOL`), direction, volume step clamp, stop-loss/take-profit
  presence and sanity, modify rules (stops may tighten only —
  direction-aware: BUY stop up, SELL stop down), close requires a known
  ticket.
- SSE/HTTP: unknown `resume_from` / `ack` / `since` ids → HTTP 400
  (`INVALID_ARGUMENT`), never silent misbehavior.
- Installer: empty or whitespace-only `MT5_*` values count as missing →
  `needs_credentials` with a `required` list, never a silent proceed.

## 5. File permissions

| Path | Permissions | Notes |
|---|---|---|
| `$FOREX_AGENT_HOME/secrets.env` | 0600 | never overwritten, never printed |
| `$FOREX_AGENT_HOME/storage/local.db` | default umask of the installing user; keep the agent home private | holds kill-switch latch, risk state, idempotency keys, audit log |
| `run/*.pid`, `log/*.log`, `run/notify.cursor` | agent home, same ownership | no secrets |

For `--system` installs the `forex` user owns `/var/lib/forex-agent`;
systemd unit `WorkingDirectory` must be readable only by that user.

## 6. Kill-switch supremacy

- The latch lives in local SQLite (`kill_switch` table), **persists
  across restarts**, and is fail-closed: an unreadable latch is treated
  as engaged.
- It sits **below the agent layer**: natural-language instructions,
  agent policy, Worker outage, and model output can never bypass it.
  `health_monitor` re-checks the latch every 10 s and engages on any
  read failure.
- While engaged, the execution gateway rejects `request_trade` and
  `modify_position`; `close_position` stays allowed because closing
  reduces risk (blocking a close would trap the agent in a losing
  position).
- The Worker's kill-switch mirror is best-effort sync for the mobile
  app; on any disagreement the **local latch always wins**.

## 7. Dry-run default

- `mode: dry_run` is the config default (`MODE` env / `agent.env`
  overrides). In dry-run, the gateway's terminal lifecycle state is
  `dry_run_simulated` — `executed` is reported only after real broker
  confirmation.
- Live trading requires a deliberate two-step act: `MODE=live` **and**
  valid broker credentials. There is no casual path to live orders.
- `core/execution/broker_guard.py`: any broker write outside the
  gateway's `execution_scope()` raises
  `BrokerError(GATEWAY_BYPASS_ATTEMPTED)`. Trading tools call the
  gateway, never the adapter.

## 8. Honest failure (what is never faked)

No fake market data, no fake fills, no fake predictions. Disconnected
or unreachable broker ⇒ `BROKER_UNAVAILABLE` with an explanatory
message. Economic calendar and ML prediction report `available: false`.
Real MT5 trading has not been validated in this environment (no
terminal/credentials) — the docs say so wherever it matters.

---

# Part B — Cloudflare Worker (cloud/sync layer)

Scope: the Worker only. Subsystem-side secret handling is covered in
Part A above.

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
