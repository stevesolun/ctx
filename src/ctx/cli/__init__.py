"""ctx.cli — user-facing CLI entrypoints.

Each entrypoint here is a thin wrapper that parses argv and delegates to
the appropriate adapter. No business logic lives here.

Existing CLIs (CC-facing):
  python -m ctx.adapters.claude_code.install.skill_install
  python -m ctx.adapters.claude_code.install.agent_install
  python -m ctx.adapters.claude_code.install.mcp_install [uninstall]
  python -m ctx.core.wiki.wiki_graphify
  python -m mcp_enrich
  python -m mcp_quality

New CLIs (harness-facing, added H7):
  ctx run        - drive any model autonomously against a task
  ctx resume     - continue a previous session
  ctx sessions   - list / inspect sessions

The package entrypoint ``python -m ctx`` delegates to the same harness CLI.
"""
