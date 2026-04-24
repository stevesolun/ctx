"""ctx.adapters.generic — model-agnostic harness.

This adapter wraps ANY LLM and drives it autonomously. Works with:

  - OpenRouter (tier-1 remote aggregator: GPT-5.5, MiniMax, Claude,
    Gemini, Llama, ...)
  - Ollama (tier-1 local inference)
  - Direct provider SDKs (optional, added when OpenRouter falls short)

Paths live under ~/.ctx/ so the generic harness can be installed
alongside Claude Code without conflict.

Built across phases H1-H12 of Plan 001:
  H1  - provider adapter (LiteLLM-backed)
  H2  - MCP router (tool dispatch)
  H3  - while-loop (solo agent)
  H4  - state (JSONL append-only)
  H5  - context compaction
  H6  - ctx-core integration
  H7  - CLI (ctx run)
  H8  - MCP server (ctx-core as MCP)
  H9  - per-host adapters (Aider / Goose / Cline / custom)
  H10 - Planner agent (evidence-driven, deferred until solo fails)
  H11 - Evaluator agent (evidence-driven)
  H12 - Sprint contracts (evidence-driven)
"""
