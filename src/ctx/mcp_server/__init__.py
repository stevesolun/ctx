"""ctx.mcp_server — expose ctx-core over MCP so any MCP-speaking
harness can consume its recommendations.

Tools exposed:
  ctx__recommend_bundle(query, top_k=5)
      top-K skill/agent/MCP recommendations with selection metadata
  ctx__recommend_related(selected, rejected=None, max_hops=2, top_n=5)
      graph-backed suggestions after a partial selection
  ctx__graph_query(seeds, max_hops=2, top_n=10)
      graph walk from seed entities
  ctx__wiki_search(query, top_n=15)
      search the llm-wiki
  ctx__wiki_get(slug)
      fetch a specific entity card
  ctx__observe_dev_event / ctx__load_entity / ctx__mark_entity_used /
  ctx__record_validation / ctx__record_escalation / ctx__unload_entity /
  ctx__session_end / ctx__session_state
      runtime lifecycle records and session state

Console script: ``ctx-mcp-server``.
Use ``--allow-tools`` and ``--entity-types`` to expose a scoped subset for
permissioned adapters.

Host examples that can attach this server:
  Claude Code:        claude mcp add ctx-wiki -- ctx-mcp-server
  Claude Agent SDK:   McpServerConfig(command='ctx-mcp-server')
  Cline / Goose:      their MCP server config form
  Custom harness:     subprocess.Popen(['ctx-mcp-server'], ...)

Plan 001 Phase H8.
"""

from ctx.mcp_server.server import main, run_server

__all__ = ["main", "run_server"]
