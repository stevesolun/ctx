"""
ctx — Alive skill system + model-agnostic harness for Claude Code and other LLMs.

This package is the TARGET structure being assembled by Plan 001
(docs/plans/001-model-agnostic-harness.md). During phases R0-R6 the
codebase migrates from the flat src/ layout into this tree. Each phase
touches <=5 files; legacy imports from src/ stay working via shims
until the migration completes.

Layout:
    ctx.core       - provider-agnostic business logic (graph, quality,
                     wiki, resolve, bundle)
    ctx.adapters   - host-specific integration code
      .claude_code   - Claude Code hooks + install CLIs (today's shape)
      .generic       - new model-agnostic harness (v1 solo agent,
                       v2 adds Planner+Evaluator per Anthropic pattern)
    ctx.cli        - user-facing CLI entrypoints (thin wrappers)
    ctx.mcp_server - ctx-core exposed as an MCP server so ANY
                     MCP-speaking harness (Cline, Goose, Claude Agent
                     SDK, custom) can consume its recommendations
    ctx.utils      - low-level primitives (safe name validation, atomic
                     file write, file locks)

Status: R0 scaffolding only — see docs/plans/001-model-agnostic-harness.md.
"""

__version__ = "0.1.0-alpha"
