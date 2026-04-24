"""ctx.mcp_server — expose ctx-core over MCP so any MCP-speaking
harness can consume its recommendations.

Tools exposed (built in phase H8):
  recommend_bundle(query, top_k=5)   - top-K skill/agent/MCP recommendations
  graph_query(seeds, max_hops=2)     - graph walk from seed entities
  install(slug, entity_type)         - install a skill/agent/MCP
  wiki_search(query, limit=10)       - search the llm-wiki

Host examples that can attach this server:
  Claude Agent SDK, Cline, Goose, OpenHands, any custom harness.
"""
