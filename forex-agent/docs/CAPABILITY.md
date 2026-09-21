# Capability contract

The forex-agent subsystem exposes one machine-readable capability
contract: `agent/capabilities.json`. It is generated from the live tool
registry by `scripts/gen_manifest.py` (the generator is the source of
truth) and verified with `scripts/gen_manifest.py --check`.

## What the contract contains

- **Identity**: name, subsystem version, `contract_version` (bumped when a
  section is added/renamed/removed), platform (`linux`), description.
- **Interfaces** — every command, path, and URL is real and executable in
  a real install:
  - `mcp`: `python3 -m agent.mcp.server` (JSON-RPC 2.0 over stdio).
  - `cli`: `scripts/forex` with example invocations (`status --json`,
    `broker-status --json`, `events --since-id <id> --follow`, …).
  - `local_api`: `http://127.0.0.1:${FOREX_API_PORT:-8765}` with the real
    GET/POST route list (managed service `local_api`).
  - `events`: the SSE channel (`GET /events`, `Accept: text/event-stream`),
    `resume_from`/`ack` semantics, and the `GET /events/latest` poll
    fallback.
- **Agent notification** — the persistent bridge
  (`python3 -m agent.events.bridge`, managed service `agent_bridge`):
  SSE → host-provided sink, durable cursor, reconnect backoff,
  at-least-once delivery, the generic subprocess/stdin NDJSON sink
  contract (`FOREX_AGENT_NOTIFICATION_COMMAND`), installer states, and
  the honest guarantee boundary (Forex guarantees journaled/streamed/
  bridged; it cannot guarantee the host model woke up).
- **Services** — the managed services beyond the four monitors
  (`local_api`, `agent_bridge`) with their commands.
- **Lifecycle**: the real commands for install/start/stop/restart/
  status/health/logs, plus the status vocabulary the installer reports.
- **Broker**: the fields of `broker_status()` and what each flag means
  (`connected` = logged in, `trading_available` = terminal allows orders).
- **Mode**: `dry_run` (default) vs `live`, and where it comes from.

## Installer machine-readable output

`installer/install.sh --agent` (alias `--json`) prints a structured
install result on stdout and always writes
`$FOREX_AGENT_HOME/install-result.json`:

```json
{
  "schema": "forex-agent.install-result/1",
  "status": "broker_disconnected",
  "mode": "dry_run",
  "broker": "disconnected",
  "broker_detail": "MetaTrader5 package not installed on this machine (see broker/mt5/RUNTIME.md)",
  "trading": "disabled",
  "signals": "available",
  "events": "running",
  "agent_notification": "unconfigured",
  "mcp": "available",
  "daemons": true,
  "manifest": "/opt/forex-agent/agent/capabilities.json"
}
```

`status` is derived from real probes (`broker_status()`, config mode,
kill-switch state, daemon supervisor, and — for `events` — a real
TCP/`/health`/SSE handshake probe of the local API) — never invented.
`agent_notification` is `configured` / `unconfigured` / `unavailable`
(see `docs/INSTALL.md`). Missing or
empty/whitespace-only broker credentials yield
`{"status": "needs_credentials", "required": ["MT5_LOGIN", ...]}`.

## Registration (generic — no vendor-specific files)

The manifest **is** the registration mechanism. There are deliberately no
vendor-specific installer files (no `grok_install.py`, no
`instinct_install.py`, nothing of the sort). The host agent consumes
`agent/capabilities.json` and decides how to register the capability in
its own runtime — e.g. adding the MCP command to its server list,
wrapping the CLI, or pointing an HTTP client at the local API base URL.
New hosts are supported by reading the contract, not by adding files here.
