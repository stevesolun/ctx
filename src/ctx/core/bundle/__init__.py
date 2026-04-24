"""ctx.core.bundle — top-K cross-type (skill + agent + MCP) bundle orchestration.

Populated in phase R4 — extracts the adapter-neutral logic from the current
src/bundle_orchestrator.py. Host-specific output formatting (Claude Code
hookSpecificOutput JSON) moves to ctx.adapters.claude_code.hooks.
"""
