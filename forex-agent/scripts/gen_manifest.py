#!/usr/bin/env python3
"""Generate agent/capabilities.json from the live tool registry.

The manifest is the static discovery document for external agents: it
lists every capability with its JSON Schema, the interfaces that expose
it (MCP/CLI/HTTP), the daemons, and the safety invariants. Regenerate
after changing the tool registry:

    python3 scripts/gen_manifest.py

The file is committed; CI/installer can verify it is up to date with
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
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "interfaces": {
            "mcp": {
                "transport": "JSON-RPC 2.0 over stdio",
                "command": "python3 -m agent.mcp.server",
                "methods": ["initialize", "ping", "tools/list", "tools/call"],
            },
            "cli": {
                "command": "scripts/forex",
                "notes": "11 commands; --json on every command; exit 0/1/2.",
            },
            "http": {
                "bind": "127.0.0.1 only (loopback; this IS the access control)",
                "command": "python3 scripts/local_api.py [--port PORT]",
                "routes": ["GET /health /status /signals /positions /account "
                           "/performance /events",
                           "POST /analyze /trade/request /position/close "
                           "/position/modify"],
            },
        },
        "tools": tools,
        "daemons": DAEMONS,
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
