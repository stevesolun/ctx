"""ctx.adapters.generic.tools — MCP-first tool routing for the harness.

Exposes:
  McpServerConfig   - how to spawn one MCP server
  McpClient         - single-server sync JSON-RPC client
  McpRouter         - multi-server client with namespaced tool dispatch
  McpServerError    - raised on protocol / execution errors
  running_router    - context-manager helper
  TOOL_SEPARATOR    - '__' — separates server name from tool name

Qualified tool names the harness exposes to the model are
``<server_name><TOOL_SEPARATOR><tool_name>`` (e.g.
``filesystem__read_file``). The router splits on the separator to
dispatch each call back to the right MCP server.

Plan 001 Phase H2.
"""

from __future__ import annotations

from ctx.adapters.generic.tools.mcp_router import (
    TOOL_SEPARATOR,
    McpClient,
    McpRouter,
    McpServerConfig,
    McpServerError,
    running_router,
)


__all__ = [
    "McpClient",
    "McpRouter",
    "McpServerConfig",
    "McpServerError",
    "TOOL_SEPARATOR",
    "running_router",
]
