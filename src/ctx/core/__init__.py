"""ctx.core — provider-agnostic business logic.

Sub-packages:
  graph     - knowledge graph walk + semantic edges
  quality   - heuristic scoring (skill / agent / MCP)
  wiki      - llm-wiki generation, parsing, sync
  resolve   - skill / bundle resolution from signals
  bundle    - top-K cross-type bundle orchestration

None of this layer may import from ctx.adapters or rely on any host-
specific path, hook format, or session convention. Everything here
works identically whether the caller is Claude Code, the generic
harness, or an external consumer using ctx.core as a library.
"""
