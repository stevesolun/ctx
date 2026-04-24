"""ctx.adapters.claude_code.install — skill / agent / MCP install CLIs for Claude Code.

The MCP install path shells out to `claude mcp add` etc. — a hard
dependency on the claude CLI, which is why it lives in the CC adapter
and not in a generic install layer.
"""
