"""Hand-rolled MCP server: JSON-RPC 2.0 over stdio.

Run: ``python3 -m agent.mcp.server`` (from the forex-agent directory).

Protocol (minimal MCP-compatible surface):
  -> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
  <- {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05",
      "serverInfo":{"name":"forex-agent","version":"0.1.0"},
      "capabilities":{"tools":{}}}}
  -> {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
  <- {"jsonrpc":"2.0","id":2,"result":{"tools":[{...18 forex.* tools...}]}}
  -> {"jsonrpc":"2.0","id":3,"method":"tools/call",
      "params":{"name":"forex.get_health","arguments":{}}}
  <- {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text",
      "text":"{...tool result JSON...}"}],"isError":false}}

Notes:
- One JSON-RPC message per line on stdin; one response line on stdout.
- Notifications (no "id") get no response.
- All logging goes to stderr — stdout is the protocol and must stay clean.
- Tool results are ALWAYS returned as result.content text (even when the
  tool itself reports ok:false); isError=true only when the CALL failed
  (unknown tool, bad arguments, internal crash).
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger("forex_agent.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "forex-agent"
SERVER_VERSION = "0.1.0"


def _ok(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str,
         data: Any = None) -> dict:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _registry():
    from agent.tools import registry  # noqa: PLC0415 (lazy: keep import light)
    return registry


def handle_request(message: dict) -> Optional[dict]:
    """Process one decoded JSON-RPC message; return the response dict,
    or None for notifications / unparseable input."""
    if not isinstance(message, dict):
        return None
    if message.get("jsonrpc") != "2.0":
        return _err(message.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _err(request_id, -32602, "Invalid params: must be an object")

    # Notification: no response, but still act on initialized.
    if request_id is None:
        return None

    try:
        registry = _registry()
    except Exception as exc:  # noqa: BLE001
        logger.exception("registry import failed")
        return _err(request_id, -32603, "Internal error: %s" % exc)

    if method == "initialize":
        return _ok(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"tools": {}},
        })
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": [
            {"name": c["name"], "description": c["description"],
             "inputSchema": c["input_schema"]}
            for c in registry.list_capabilities()
        ]})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name or not isinstance(name, str):
            return _err(request_id, -32602,
                        "Invalid params: 'name' (string) is required")
        if not isinstance(arguments, dict):
            return _err(request_id, -32602,
                        "Invalid params: 'arguments' must be an object")
        try:
            result = registry.call(name, arguments)
        except KeyError:
            return _err(request_id, -32601, "Unknown tool: %r" % name)
        except Exception as exc:  # noqa: BLE001 — a crashing tool is a server error
            logger.exception("tool %s crashed", name)
            return _err(request_id, -32603,
                        "Tool %s failed: %s" % (name, exc))
        return _ok(request_id, {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": not result.get("ok", True),
        })
    return _err(request_id, -32601, "Method not found: %r" % (method,))


def serve(stdin=None, stdout=None) -> None:
    """Read JSON-RPC lines from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            stdout.write(json.dumps(
                _err(None, -32700, "Parse error: %s" % exc)) + "\n")
            stdout.flush()
            continue
        try:
            response = handle_request(message)
        except Exception as exc:  # noqa: BLE001 — never die on one message
            logger.exception("request handling crashed")
            response = _err(message.get("id") if isinstance(message, dict) else None,
                            -32603, "Internal error: %s" % exc)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    serve()


if __name__ == "__main__":
    main()
