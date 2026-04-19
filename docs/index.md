---
hide:
  - navigation
---

# ctx — Skill & Agent Recommendation and Management for Claude Code

Watches what you develop, walks a knowledge graph of **1,789 skills and
464 agents**, and recommends the right ones on the fly — you decide what
to load and unload. Powered by a Karpathy LLM wiki with persistent memory
that gets smarter every session.

!!! tip "Install"

    ```bash
    pip install claude-ctx
    ```

    Optional extras: `pip install "claude-ctx[embeddings]"` for the
    semantic backend, `pip install "claude-ctx[dev]"` for the
    pytest/mypy/ruff toolchain. After install the `ctx-scan-repo`,
    `ctx-skill-quality`, `ctx-skill-health`, and `ctx-toolbox` console
    scripts are on PATH.

## Why this exists

Claude Code skills and agents are powerful, but at scale they become
unmanageable:

- **Discovery problem** — with 1,700+ skills, how do you know which ones
  exist and which are relevant to your current project?
- **Context budget** — loading all skills wastes tokens and degrades
  quality. You need exactly the right 10–15 skills and agents per
  session.
- **Hidden connections** — a FastAPI skill is useful, but you also need
  the Pydantic skill, the async Python patterns skill, and the Docker
  skill. Nobody tells you that.
- **Skill rot** — skills you installed three months ago and never used
  are cluttering your context. Stale skills should be flagged and
  archived.

ctx solves all of these by treating your skill library as a **knowledge
graph with persistent memory**, not a flat directory.

## What this is

ctx is not a collection of scripts. It is an agent with persistent memory
and a knowledge graph.

The core idea comes from Andrej Karpathy's LLM-wiki pattern: instead of
re-loading everything from scratch each session, an LLM maintains a wiki
it can read, write, and query. The wiki becomes the agent's long-term
memory.

ctx applies that pattern to skill management — and extends it with
graph-based discovery:

- A Karpathy 3-layer wiki at `~/.claude/skill-wiki/` is the single source
  of truth.
- **2,253 entity pages** (1,789 skills + 464 agents) with frontmatter
  tracking use count, last used date, tags, and status.
- A **knowledge graph** (2,253 nodes, 454K edges, 93 communities)
  connects skills and agents by shared tags, enabling context-aware
  recommendations.
- **74 auto-generated concept pages** group related skills into named
  communities (e.g., *Security + Testing*, *Python + API + Database*).
- PostToolUse and Stop hooks update the wiki automatically during each
  Claude Code session.
- Skills over 180 lines are converted to a gated 5-stage micro-skill
  pipeline (956 converted) so the router can load them incrementally.
- At session start, the skill-router scans your project and
  **recommends** the best-matching skills and agents.
- Mid-session, the context monitor watches every tool call, detects new
  stack signals, walks the graph, and **recommends** relevant skills and
  agents in real time — **nothing loads without your approval**.

The result: you always know what skills and agents are available for
your current task. The graph reveals hidden connections. The wiki learns
from your usage. Stale ones are flagged. New ones self-ingest.

## Explore the docs

<div class="grid cards" markdown>

-   **Toolbox**

    ---

    Curated councils of skills and agents that fire at session-start,
    file-save, pre-commit, and session-end. Blocks `git commit` on
    HIGH/CRITICAL findings. Five starter toolboxes ship out of the box.

    [:octicons-arrow-right-24: Toolbox overview](toolbox/index.md) ·
    [Starter toolboxes](toolbox/starters.md) ·
    [Verdicts & guardrails](toolbox/verdicts.md)

-   **Skill router**

    ---

    Scans the active repo, detects the stack from file signatures, walks
    the skill-stack matrix, and loads exactly the skills that apply — no
    more, no fewer.

    [:octicons-arrow-right-24: Router overview](skill-router/index.md) ·
    [Stack signatures](stack-signatures.md) ·
    [Skill-stack matrix](skill-stack-matrix.md)

-   **Health & quality**

    ---

    Structural health checks (missing frontmatter, orphan manifest
    entries, line-count drift) plus the four-signal quality score
    (telemetry · intake · graph · routing) that grades every skill
    A/B/C/D/F.

    [:octicons-arrow-right-24: Skill health](skills-health.md) ·
    [Memory anchoring](memory-anchor.md) ·
    [Lifecycle dashboard](skill-lifecycle-and-dashboard.md)

-   **Releases**

    ---

    **v0.6.1** — MIT, CI-matrixed (Ubuntu + Windows × Python 3.11/3.12),
    1,363 tests passing. Ships 10 console scripts including `ctx-init`
    and `ctx-monitor` (local dashboard with graph + wiki + load/unload)
    plus the 9.6 MB pre-built wiki tarball with **2,253 nodes /
    454,719 edges / 93 communities**. Hardened across four Strix-audited
    security findings.

    [:octicons-arrow-right-24: CHANGELOG](https://github.com/stevesolun/ctx/blob/main/CHANGELOG.md) ·
    [Repository](https://github.com/stevesolun/ctx)

</div>

## Principles

- **Foundation first.** Data model, CLI, and starter bundles ship before
  any hook integration. Each phase is independently usable.
- **User-configurable everything.** Dedup policy, suggestion loudness,
  trigger set, council composition.
- **Evidence over opinion.** Suggestions cite real usage data plus
  knowledge-graph edges. No black-box prompts.
- **Token discipline.** Every council run honors `max_tokens` /
  `max_seconds` budgets.
