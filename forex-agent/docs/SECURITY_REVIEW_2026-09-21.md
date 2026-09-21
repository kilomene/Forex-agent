# Security Review — Forex agent-native migration (Phase 8)

**Date:** 2026-09-21
**Scope:** whole `forex-agent/` subsystem on branch `agent-native-migration` —
credential handling, shell/command injection, path traversal, tool-argument
validation, unauthenticated endpoints, and filesystem permissions.
**Interfaces audited:** CLI (`scripts/forex`), localhost API
(`scripts/local_api.py`), MCP server (`agent/mcp/server.py`), MT5 gateway
transport (`broker/mt5/gateway.py`), installer (`installer/install.sh`),
daemon launcher (`scripts/forex-daemons`), event bus (`agent/events/bus.py`),
notification channels (`agent/notifications/`), storage (`storage/store.py`).

## Findings

### Fixed

1. **[HIGH] `forex config --json` printed the raw `NOTIFY_WEBHOOK_URL`.**
   The CLI's private `_redact()` masked key substrings
   `password|secret|api_key|apikey|token` — the field name `webhook_url`
   matches none of them, so a token-embedded webhook URL (declared
   "may embed a token" in `config.py`) was emitted in plaintext.
   Verified live before the fix. **Fixed:** `scripts/forex` now also masks
   keys containing `webhook`, plus exact `login`/`server`/`terminal_path`
   (declared SECRET_KEYS in `config/config.py`).

2. **[MEDIUM] `AppConfig.redacted()` under-covered its own contract.**
   `SECRET_KEYS` documents that `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`,
   `MT5_TERMINAL_PATH` "must never appear in logs / events / error
   strings", but `redacted()` masked only `password`/`api_key`/`webhook_url`
   — `login`, `server`, `terminal_path` passed through in plaintext.
   **Fixed:** new `REDACTED_FIELDS` set in `config/config.py` covers all
   six; `redacted()` masks them.

3. **[MEDIUM] `SECRET_KEYS` contract missing bearer credentials.**
   `MT5_GATEWAY_TOKEN` (MT5 gateway bearer auth) was never in the
   never-log contract, nor in the installer's secret-file flow.
   **Fixed:** added `MT5_GATEWAY_TOKEN`, `NOTIFY_WEBHOOK_URL`, and
   `NEWS_CALENDAR_API_KEY` to `config/config.py` `SECRET_KEYS`; added
   `MT5_GATEWAY_TOKEN` and `NOTIFY_WEBHOOK_URL` to the installer's
   secrets-file population list (still 0600, never printed). The
   installer's credential-*detection* logic (MT5_LOGIN/PASSWORD/SERVER)
   was intentionally left unchanged — installer-output tests assert it.

4. **[LOW] Remote MT5 gateway bearer token could ride over plain HTTP.**
   Nothing warned when `MT5_GATEWAY_URL` used `http://`.
   **Fixed:** `RemoteMT5GatewayTransport` now logs a cleartext-token
   warning for non-`https://` URLs (warn, don't refuse — LAN/test setups).

5. **[LOW] Uncapped `limit` on `/events` / `/events/latest` JSON polls.**
   A single localhost client could request an unbounded journal slice.
   **Fixed:** `scripts/local_api.py` clamps `limit` to `[1, 1000]`
   (`_EVENTS_LIMIT_MAX`), consistent with the SSE replay cap.

6. **[LOW] Home-tree directories created world-listable.**
   The installer and `daemon/common.py` created `$PREFIX`-style trees at
   default umask (0755), exposing directory listings around the 0600
   secrets file. **Fixed:** installer `chmod 700`s `$PREFIX`, `run`,
   `log`, `storage`; `daemon/common.py::get_home()` creates/chmods
   `~/.forex-agent` to 0700.

### Audited, no fix needed

- **Local API bind:** `ThreadingHTTPServer(("127.0.0.1", port))` only; no
  `0.0.0.0` anywhere in the tree (verified by grep + `test_binds_loopback_only`).
  No auth, but the loopback bind is the documented access control.
- **MT5 gateway allowlist:** `_request()` rejects any op outside the fixed
  12-entry `OPERATIONS` map with `GATEWAY_REJECTED` *before* network I/O;
  there is no generic execute/command RPC. Bearer token is mandatory at
  construction (`CREDENTIALS_INVALID` when missing).
- **Trade path:** every trade-affecting entry (CLI has no trade command;
  MCP `tools/call`; API `POST /trade/request`, `/position/close`,
  `/position/modify`) funnels through `registry` → tools →
  `ExecutionGateway`; `GatewayOnlyAdapter` raises `GATEWAY_BYPASS_ATTEMPTED`
  outside `execution_scope()`.
- **Shell injection:** zero `subprocess`/`shell=True`/`os.system` in Python;
  `forex-daemons` validates daemon names against a fixed list before using
  them in paths/module names (`resolve_targets`); pid/log files are not
  user-addressable.
- **Installer:** secrets written with Python single-quote escaping before
  the file is sourced (safe against `$(...)`/quotes in values); secrets
  never printed; `eval "v=\${$key:-}"` iterates a fixed key list.
- **Path traversal:** no file path is built from tool arguments — event
  ids/signal ids are SQLite query parameters (all queries parameterized);
  CLI `events --since-id` is an id lookup, not a path.
- **Argument validation:** symbol non-empty at tools + whitelist at
  gateway; direction enum; volume>0 and mandatory SL>0 at tools, clamped
  to broker grid by the risk engine; ticket `int()` with `INVALID_TICKET`;
  timeframe mapped via `_timeframe()` (rejects unknown); `count`/`limit`/
  `lookback_days`/`hours_ahead` clamped; API `int()` parse errors → 400.
- **Notification channels:** log URL *host* only; `health()` is secret-free
  (verified in `channels.py`); webhook payload logging disabled.
- **Systemd units:** no secrets baked into unit files.

## Residual risks (not fixed, and why)

- **`forex.get_account` / `/account` expose the broker account `login`
  (account number) in plaintext.** This is operator-facing data on a
  localhost-only API, useful for identifying the connected account, and
  not a credential that authenticates anything. Left as-is; flag if the
  threat model changes.
- **Webhook failure log lines in `agent/events/bus.py::_deliver_webhook`
  stringify the caught exception.** urllib exceptions do not embed the
  request URL, so the token-embedded URL cannot leak this way today; the
  dedicated `WebhookChannel` already logs host-only. Left as-is.
- **`sed` substitution in the installer's systemd step** interpolates
  `$SOURCE` into the pattern: a source path containing `|`/`&`/backslashes
  would break the generated unit (a broken install, not an exploit —
  the path is the operator's own checkout). Left as-is.
- **SQLite DB (`local.db`) and log files** inherit the process umask;
  they contain event/audit payloads (no secrets by design). Hardening the
  umask for every writer was judged out of scope for this pass.

## Tests

New `tests/test_security.py` — 16 tests, all passing, each tied to a
finding above (webhook/credential redaction in CLI + `redacted()`,
SECRET_KEYS contract, loopback bind, limit clamping, gateway allowlist +
token refusal + cleartext warning, argument validation at registry/MCP,
CLI metacharacters inert).

Full suite: **384 passed, 8 subtests passed** (baseline was 368; +16 new).
No existing test modified; execution-safety assertions untouched.
