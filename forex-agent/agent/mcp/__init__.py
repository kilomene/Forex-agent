"""MCP server package: hand-rolled JSON-RPC 2.0 over stdio.

No MCP SDK (none is installed and none may be added — Python 3.12,
no new runtime dependencies). Any MCP-compatible client can spawn
``python3 -m agent.mcp.server`` and speak JSON-RPC 2.0 with
``tools/list`` / ``tools/call``.
"""

from .server import main, serve

__all__ = ["main", "serve"]
