"""ctx.cli — user-facing CLI entrypoints.

Each entrypoint here is a thin wrapper that parses argv and delegates to
the appropriate adapter. No business logic lives here.

Existing CLIs (CC-facing):
  ctx-skill-install
  ctx-agent-install
  ctx-mcp-install / ctx-mcp-uninstall
  ctx-wiki-graphify
  ctx-mcp-enrich
  ctx-mcp-quality

New CLIs (harness-facing, added H7):
  ctx run        - drive any model autonomously against a task
  ctx resume     - continue a previous session
  ctx sessions   - list / inspect sessions
"""
