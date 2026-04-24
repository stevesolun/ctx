"""ctx.adapters — per-host integration code.

An adapter translates between a host's conventions (file layout, event
format, session identity) and ctx.core's provider-agnostic API. Each
adapter owns:

  - Its paths (e.g. Claude Code lives at ~/.claude/, generic harness
    at ~/.ctx/)
  - Its event / hook / message format
  - Its install + load semantics
  - Its session identity model

Adapters may import from ctx.core freely. They must NOT import from
each other — cross-adapter coupling is a refactor smell. Two adapters
that need the same helper put it in ctx.core or ctx.utils.

Current adapters:
  claude_code - Claude Code hooks + skill/agent/MCP install CLIs
  generic     - model-agnostic harness (OpenRouter, Ollama, direct SDKs)
"""
