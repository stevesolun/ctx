"""ctx.mcp_server — expose ctx-core over MCP so any MCP-speaking
harness can consume its recommendations.

Tools exposed:
  recommend_bundle(query, top_k=5)   - top-K skill/agent/MCP recommendations
  graph_query(seeds, max_hops=2)     - graph walk from seed entities
  wiki_search(query, top_n=15)       - search the llm-wiki
  wiki_get(slug)                     - fetch a specific entity card

Console script: ``ctx-mcp-server``.

Host examples that can attach this server:
  Claude Code:        claude mcp add ctx-wiki -- ctx-mcp-server
  Claude Agent SDK:   McpServerConfig(command='ctx-mcp-server')
  Cline / Goose:      their MCP server config form
  Custom harness:     subprocess.Popen(['ctx-mcp-server'], ...)

Plan 001 Phase H8.
"""

from ctx.mcp_server.server import main, run_server

__all__ = ["main", "run_server"]
