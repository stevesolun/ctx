"""
ctx — Alive skill system + model-agnostic harness for Claude Code and other LLMs.

Three delivery surfaces (pick what fits your integration):

  1. **MCP server** — attach from any MCP host (Claude Code, Claude
     Agent SDK, Cline, Goose, OpenHands, custom):
         claude mcp add ctx-wiki -- ctx-mcp-server

  2. **Generic harness CLI** — drive any LLM against a task:
         ctx run --model openrouter/anthropic/claude-opus-4.7 \\
                 --task "fix the failing tests"

  3. **Python library** — use from your own harness / tool:
         from ctx import recommend_bundle, graph_query, wiki_search
         bundle = recommend_bundle("python web api", top_k=5)

See docs/harness/attaching-to-hosts.md for host-specific recipes.

Package layout:
    ctx.core       - provider-agnostic business logic (graph, quality,
                     wiki, resolve, bundle)
    ctx.adapters   - host-specific integration code
      .claude_code   - Claude Code hooks + install CLIs
      .generic       - model-agnostic harness (LiteLLM, MCP, loop,
                       session, compaction, ctx-core tools)
    ctx.cli        - user-facing CLI entrypoints (`ctx run` et al.)
    ctx.mcp_server - the standalone MCP server (H8)
    ctx.api        - blessed public Python API for custom harnesses
    ctx.utils      - low-level primitives (safe names, atomic IO)

See docs/plans/001-model-agnostic-harness.md for the roadmap + phase
status.
"""

__version__ = "0.6.4"


# Public library surface — anything listed here is safe for third-
# party harnesses to import. Underlying module paths can change; these
# names stay stable across non-major releases.
from ctx.api import (
    CtxCoreToolbox,
    default_wiki_dir,
    graph_query,
    list_all_entities,
    recommend_bundle,
    wiki_get,
    wiki_search,
)


__all__ = [
    "CtxCoreToolbox",
    "default_wiki_dir",
    "graph_query",
    "list_all_entities",
    "recommend_bundle",
    "wiki_get",
    "wiki_search",
    "__version__",
]
