"""CTX: relevant tool suggestions over user-owned graph and wiki knowledge.

CTX ships the engine and infrastructure, not a populated tool catalog.
Initialize an empty local wiki, add your own records, and ask about a task::

    ctx-init --model-mode skip
    ctx recommend "review the API changes" --json

Native prompt hooks, CLI, MCP, and Python use the shared recommendation engine.
Suggestions do not grant installation, activation, or unload permission.
CTX Fit remains an optional evaluator invoked explicitly with ``ctx fit``.
The integration surfaces below remain available:

  1. **MCP server** — attach from any MCP host (Claude Code, Claude
     Agent SDK, Cline, Goose, OpenHands, custom):
         claude mcp add ctx-wiki -- ctx-mcp-server

  2. **Generic harness CLI** — drive any LLM against a task:
         ctx run --model openrouter/anthropic/claude-opus-4.7 \\
                 --task "fix the failing tests"
     The same CLI is available through ``python -m ctx``.

  3. **Python library** — use from your own harness / tool:
         from ctx import recommend_bundle, graph_query, wiki_search
         bundle = recommend_bundle("python web api", top_k=5)

  4. **Loop adapter** — emit permissioned pre-plan context for an
     external loop that already owns plan/act/observe:
         python -m ctx.adapters.loopflow --goal "fix checkout" \\
                 --permissions skills,agents,mcps

See docs/harness/attaching-to-hosts.md for host-specific recipes.

Package layout:
    ctx.fit        - CTX Fit profiling, experiments, recommendation, and apply
    ctx.core       - provider-agnostic business logic (graph, quality,
                     wiki, resolve, bundle)
    ctx.adapters   - host-specific integration code
      .claude_code   - Claude Code hooks + install CLIs
      .generic       - model-agnostic harness (LiteLLM, MCP, loop,
                       session, compaction, ctx-core tools)
      .loopflow      - LoopFlow/custom agent-loop recommendation adapter
    ctx.cli        - recommendation, optional Fit, and harness CLI entrypoints
    ctx.mcp_server - the standalone MCP server (H8)
    ctx.api        - blessed public Python API for custom harnesses
    ctx.utils      - low-level primitives (safe names, atomic IO)
"""

__version__ = "1.0.21"


# Public library surface — anything listed here is safe for third-
# party harnesses to import. Underlying module paths can change; these
# names stay stable across non-major releases.
from ctx.api import (
    CtxCoreToolbox,
    default_wiki_dir,
    graph_query,
    list_all_entities,
    recommend_bundle,
    recommend_related,
    wiki_get,
    wiki_search,
)


__all__ = [
    "CtxCoreToolbox",
    "default_wiki_dir",
    "graph_query",
    "list_all_entities",
    "recommend_bundle",
    "recommend_related",
    "wiki_get",
    "wiki_search",
    "__version__",
]
