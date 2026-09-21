"""forex-agent: agent-installable autonomous Forex subsystem.

External agent = brain. This package = capability. The agent never touches
the broker directly: all trade-affecting calls route through the core
Execution Gateway, and all subsystem->agent delivery flows through the
event bus (agent/events), the CLI (scripts/forex), the localhost API
(scripts/local_api.py), or the MCP server (agent/mcp).
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
