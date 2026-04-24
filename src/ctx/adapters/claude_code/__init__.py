"""ctx.adapters.claude_code — Claude Code integration.

This is the adapter for today's shipping UX: a PostToolUse hook pipeline
that detects unmatched signals, writes pending-skills.json, and emits
bundle recommendations via the hookSpecificOutput JSON shape.

Paths live under ~/.claude/ by Claude Code convention.

Populated in phase R4 (moves from src/bundle_orchestrator.py,
context_monitor.py, skill_suggest.py, inject_hooks.py, skill_install.py,
agent_install.py, mcp_install.py).
"""
