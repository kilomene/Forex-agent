#!/usr/bin/env python3
"""Generate agent/capabilities.json from the live tool registry.

The manifest is the capability contract for this subsystem: a static
discovery document a host agent reads to decide how to use the forex
agent — over MCP (stdio), the CLI, or the loopback-only local HTTP API.
Every command, path, and URL in it is real and executable in a real
install; the generator is the source of truth.

Regenerate after changing the tool registry or any interface:

    python3 scripts/gen_manifest.py

The file is committed; CI/installer verify it is up to date with
`python3 scripts/gen_manifest.py --check`.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.mcp import server as mcp_server  # noqa: E402
from agent.tools.registry import list_capabilities  # noqa: E402

MANIFEST_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent", "capabilities.json")

# Version of the capability-contract schema itself (independent of the
# subsystem version). Bump when a section is added/renamed/removed.
CONTRACT_VERSION = "1.1.0"

API_PORT_ENV = "FOREX_API_PORT"
API_DEFAULT_PORT = 8765
API_BASE_URL = "http://127.0.0.1:${%s:-%d}" % (API_PORT_ENV, API_DEFAULT_PORT)

DAEMONS = [
    {"name": "market_monitor",
     "description": "Candle-feed watchdog; publishes market.candle_closed / market.data_stale.",
     "interval_seconds": 60},
    {"name": "signal_monitor",
     "description": "Deterministic EMA/RSI signal detection; publishes signal.detected. Never trades.",
     "interval_seconds": 60},
    {"name": "position_monitor",
     "description": "Exit-manager pass over the bot's own positions (breakeven/trailing/time stops).",
     "interval_seconds": 60},
    {"name": "health_monitor",
     "description": "Fail-closed kill-switch poll (10s), Worker sync, 6h performance sync.",
     "interval_seconds": 10},
]

# Managed services beyond the four monitors (scripts/forex-daemons and
# the forex-local-api / forex-agent-bridge systemd units manage these).
SERVICES = [
    {"name": "local_api",
     "description": "Loopback HTTP API + SSE agent event channel "
                    "(python3 scripts/local_api.py --port $FOREX_API_PORT).",
     "command": "python3 scripts/local_api.py [--port PORT]",
     "port_env": API_PORT_ENV,
     "default_port": API_DEFAULT_PORT},
    {"name": "agent_bridge",
     "description": "Persistent agent notification bridge: subscribes to "
                    "the SSE stream and proactively delivers each event to "
                    "the host-agent sink (python3 -m agent.events.bridge).",
     "command": "python3 -m agent.events.bridge",
     "cursor": "$FOREX_AGENT_HOME/run/agent-event.cursor",
     "delivery": "at-least-once (event_id dedup; exactly-once NOT claimed)"},
]


def build_manifest() -> dict:
    tools = []
    for cap in list_capabilities():
        tools.append({
            "name": cap["name"],
            "description": cap["description"],
            "inputSchema": cap["input_schema"],
            "trade_affecting": cap["name"] in (
                "forex.request_trade", "forex.close_position",
                "forex.modify_position", "forex.kill_switch"),
        })
    return {
        "name": mcp_server.SERVER_NAME,
        "version": mcp_server.SERVER_VERSION,
        "contract_version": CONTRACT_VERSION,
        "platform": "linux",
        "description": (
            "Agent-native forex trading subsystem: deterministic market "
            "analysis and signal detection, plus a dry-run-by-default "
            "execution gateway. Exposed over MCP (stdio), a CLI, and a "
            "loopback-only local HTTP API with an SSE agent event channel."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "interfaces": {
            "mcp": {
                "transport": "JSON-RPC 2.0 over stdio",
                "command": "python3 -m agent.mcp.server",
                "working_directory": "<forex-agent root>",
                "methods": ["initialize", "ping", "tools/list", "tools/call"],
                "notes": "stdio transport: the host agent spawns this command "
                         "and speaks JSON-RPC on its stdin/stdout.",
            },
            "cli": {
                "command": "scripts/forex",
                "path": "<forex-agent root>/scripts/forex",
                "working_directory": "<forex-agent root>",
                "global_flags": ["--json"],
                "exit_codes": {"0": "ok", "1": "command failed", "2": "usage error"},
                "commands": [
                    "status", "health", "broker-status", "signals", "signal",
                    "positions", "account", "analyze", "risk", "performance",
                    "events", "config", "kill-switch",
                ],
                "examples": [
                    "scripts/forex status --json",
                    "scripts/forex health --json",
                    "scripts/forex broker-status --json",
                    "scripts/forex signals --json --limit 5",
                    "scripts/forex events --json --since-id <event_id>",
                    "scripts/forex events --since-id <event_id> --follow",
                    "scripts/forex config --json",
                    "scripts/forex kill-switch --json",
                ],
                "notes": "13 commands; --json on every command for "
                         "machine-readable output. config output is redacted "
                         "(secrets never printed).",
            },
            "local_api": {
                "bind": "127.0.0.1 only (loopback; this IS the access control)",
                "command": "python3 scripts/local_api.py [--port PORT]",
                "port_env": API_PORT_ENV,
                "default_port": API_DEFAULT_PORT,
                "base_url": API_BASE_URL,
                "endpoints": {
                    "GET": [
                        "/health",
                        "/status",
                        "/signals",
                        "/positions",
                        "/account",
                        "/performance",
                        "/events",
                        "/events/latest",
                    ],
                    "POST": [
                        "/analyze",
                        "/trade/request",
                        "/position/close",
                        "/position/modify",
                    ],
                },
                "notes": "POST routes dispatch to the same tool registry as "
                         "MCP/CLI; trade-affecting routes go through "
                         "core.execution.gateway with dry-run and kill-switch "
                         "enforcement.",
            },
            "events": {
                "sse_endpoint": API_BASE_URL + "/events",
                "accept_header": "text/event-stream",
                "params": {
                    "resume_from": "<event_id> — replay every event journaled "
                                  "after it (oldest first), then stream live",
                    "ack": "<event_id> — cumulative acknowledgement "
                           "(this event and everything journaled at/before it)",
                },
                "resume_semantics": (
                    "Unknown resume_from/ack event_id → HTTP 400. No "
                    "resume_from → only events from connect time are "
                    "streamed. Frames carry `id:` = event_id and `data:` = one "
                    "JSON line {event_id, event, severity, timestamp, payload}."
                ),
                "poll_fallback": {
                    "endpoint": API_BASE_URL + "/events/latest",
                    "params": "since=<event_id> (optional)",
                    "response": "{ok, count, events[], last_event_id}",
                },
                "cli": "scripts/forex events [--since-id <event_id>] [--follow] [--limit N]",
            },
        },
        "lifecycle": {
            "install": {
                "command": "installer/install.sh",
                "flags": ["--prefix DIR", "--non-interactive", "--skip-daemons",
                          "--system", "--source DIR", "--agent", "--json"],
                "notes": "--agent/--json prints a machine-readable install "
                         "result (see docs/CAPABILITY.md); the installer is "
                         "idempotent and never overwrites secrets.",
            },
            "start": "scripts/forex-daemons start  # daemons + local_api + agent_bridge",
            "stop": "scripts/forex-daemons stop",
            "restart": "scripts/forex-daemons restart",
            "status": [
                "scripts/forex-daemons status",
                "scripts/forex status --json",
            ],
            "health": "scripts/forex health --json",
            "logs": "$FOREX_AGENT_HOME/log/<service>.log",
            "states": [
                "installed",
                "configured",
                "operational",
                "broker_disconnected",
                "broker_connected",
                "trading_disabled",
                "trading_ready",
                "needs_credentials",
                "error",
            ],
            "state_notes": (
                "Reported by installer/install.sh --agent from real probes: "
                "trading_ready only when the broker probe reports connected "
                "+ trading_available with mode=live and the kill switch "
                "clear; broker_disconnected when the subsystem is up but the "
                "broker probe fails (analysis-only mode, honest errors)."
            ),
        },
        "broker": {
            "status_command": "scripts/forex broker-status --json",
            "http": "GET /status → broker_status",
            "fields": {
                "provider": "adapter name: mt5 | disconnected",
                "configured": "a broker provider is selected",
                "reachable": "the broker runtime answers",
                "connected": "logged in to the broker",
                "account_available": "account snapshot readable",
                "market_data_available": "price/candle feed readable",
                "trading_available": "terminal allows order submission",
                "detail": "reason dict when any flag is degraded",
            },
            "notes": "connected=true is 'logged in', not 'trading works'. "
                     "Disconnected broker → honest errors, never invented data.",
        },
        "mode": {
            "values": ["dry_run", "live"],
            "default": "dry_run",
            "source": "DRY_RUN env (default true) or config 'mode'",
            "command": "scripts/forex config --json  # -> config.mode",
            "notes": "Dry-run is the default and blocks real order "
                     "submission at the gateway. Live mode additionally "
                     "requires broker credentials and a connected broker.",
        },
        "agent_notification": {
            "mechanism": "persistent notification bridge -> host-provided sink",
            "bridge": {
                "command": "python3 -m agent.events.bridge",
                "managed_by": [
                    "scripts/forex-daemons (service: agent_bridge)",
                    "systemd: forex-agent-bridge.service",
                ],
                "source": "SSE GET /events with resume_from=<event_id>",
                "cursor": "$FOREX_AGENT_HOME/run/agent-event.cursor",
                "delivery": "at-least-once; event_id dedup; exactly-once "
                            "is NOT claimed",
                "reconnect": "automatic, exponential backoff 1s..60s "
                             "(API restart, connection reset, daemon restart)",
            },
            "sink_contract": {
                "class": "agent.events.bridge.AgentNotificationSink",
                "operations": ["deliver(event)  # raise when not accepted",
                               "health()  # secret-free status dict"],
                "note": "Forex provides the bridge and this contract; the "
                        "HOST provides the sink implementation. No "
                        "vendor-specific code and no assumed host HTTP API "
                        "(there is no POST /agent/notify).",
            },
            "generic_sink": {
                "env": "FOREX_AGENT_NOTIFICATION_COMMAND",
                "transport": "argv-based subprocess (never shell=True); "
                             "NDJSON event envelopes on stdin",
                "envelope": "{event_id, event, severity, timestamp, payload}",
                "executable_validation": "absolute paths must exist and be "
                                         "executable; bare names resolve via "
                                         "PATH",
                "security": "no shell=True; payloads never carry secrets; "
                            "the child inherits the bridge environment — do "
                            "not put secrets in the command line",
            },
            "guarantees": {
                "forex_guarantees": [
                    "event generated",
                    "event journaled",
                    "event stream available",
                    "bridge running",
                ],
                "cannot_guarantee": [
                    "that the host model woke up — that depends on the "
                    "host-provided sink integration",
                ],
            },
            "installer_states": {
                "configured": "bridge running and a sink command is set",
                "unconfigured": "bridge running, no sink (cursor tracked, "
                                "nothing proactively delivered)",
                "unavailable": "bridge not running",
            },
        },
        "registration": {
            "mechanism": "manifest",
            "manifest_path": "agent/capabilities.json",
            "note": (
                "This manifest IS the registration. The host agent reads it "
                "and wires whichever interface fits its runtime — MCP server "
                "config, CLI wrapper, or local-API client. There are no "
                "vendor-specific installer files and none are needed."
            ),
        },
        "tools": tools,
        "daemons": DAEMONS,
        "services": SERVICES,
        "safety_invariants": [
            "Dry-run by default; live trading cannot be casually enabled.",
            "Trade-affecting tools route only through core.execution.gateway.",
            "Kill switch is fail-closed and checked before every trade.",
            "Worker (cloud) outage never disables local safety.",
            "No market data is ever invented; disconnected broker => honest errors.",
            "Local API binds only to 127.0.0.1; no secrets in logs or events.",
        ],
    }


def main(argv) -> int:
    manifest = build_manifest()
    text = json.dumps(manifest, indent=2) + "\n"
    if "--check" in argv:
        try:
            with open(MANIFEST_PATH) as fh:
                current = fh.read()
        except OSError:
            print("capabilities.json missing")
            return 1
        # Compare structurally, ignoring the generated_at timestamp.
        old = json.loads(current)
        old.pop("generated_at", None)
        new = dict(manifest)
        new.pop("generated_at", None)
        if old != new:
            print("capabilities.json is stale — run scripts/gen_manifest.py")
            return 1
        print("capabilities.json is up to date")
        return 0
    with open(MANIFEST_PATH, "w") as fh:
        fh.write(text)
    print("wrote %s (%d tools)" % (MANIFEST_PATH, len(manifest["tools"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
