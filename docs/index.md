---
hide:
  - navigation
---

# ctx — Skill, Agent, MCP & Harness Recommendation and Management

Watches what you develop, walks a knowledge graph of **1,968 skills, 464
agents, 10,786 MCP servers, and cataloged harnesses**, and recommends the
right ones on the fly — you decide what to load, install, or adopt. Powered
by a Karpathy LLM wiki with persistent memory that gets smarter every session.

!!! tip "Install"

    ```bash
    pip install claude-ctx
    ```

    Optional extras: `pip install "claude-ctx[embeddings]"` for the
    semantic backend, `pip install "claude-ctx[dev]"` for the
    pytest/mypy/ruff toolchain. After install the `ctx-scan-repo`,
    `ctx-skill-quality`, `ctx-skill-health`, and `ctx-toolbox` console
    scripts are on PATH.

    Custom-model users can run
    `ctx-init --model-mode custom --model <provider/model> --goal "<task>"`
    to record the model profile and surface harness recommendations.

## Why this exists

Claude Code skills, agents, MCP servers, and model harness profiles are
powerful, but at scale they become unmanageable:

- **Discovery problem** — with 1,900+ skills, 460+ agents, 10,000+
  MCP servers, and an expanding harness catalog, how do you know which
  ones exist and which are relevant to your current project?
- **Context budget** — loading every installable entity wastes tokens and
  degrades quality. You need exactly the right skills, agents, MCP
  servers, and harness recommendations per session.
- **Hidden connections** — a FastAPI skill is useful, but you also need
  the Pydantic skill, the async Python patterns skill, and the Docker
  skill, plus possibly a matching MCP server or model harness profile.
  Nobody tells you that.
- **Entity rot** — skills, agents, MCP servers, and harness records you
  added months ago and never used are cluttering your context. Stale ones
  should be flagged and archived.

ctx solves all of these by treating your ctx catalog as a **knowledge
graph with persistent memory**, not a flat directory.

## What this is

ctx is not a collection of scripts. It is an agent with persistent memory
and a knowledge graph.

The core idea comes from Andrej Karpathy's LLM-wiki pattern: instead of
re-loading everything from scratch each session, an LLM maintains a wiki
it can read, write, and query. The wiki becomes the agent's long-term
memory.

ctx applies that pattern to catalog management — and extends it with
graph-based discovery:

- A Karpathy 3-layer wiki at `~/.claude/skill-wiki/` is the single source
  of truth.
- **13,218+ entity pages** for the shipped skill/agent/MCP inventory, plus
  harness pages under `entities/harnesses/` when you catalog them. Each
  page tracks tags, status, provenance, and usage where it applies.
- A **knowledge graph** (13,218 nodes, 963K edges, 24 Louvain
  communities) blending semantic cosine + tag overlap + slug-token
  overlap connects skills, agents, MCP servers, and cataloged harnesses,
  enabling context-aware recommendations across installable and
  catalog-only entity types.
- **24 auto-generated concept pages** group related entities into named
  communities (e.g., *AI + Devops + Frontend*, *Python + API*).
- PostToolUse and Stop hooks update the wiki automatically during each
  Claude Code session.
- Skills over 180 lines are converted to a gated 5-stage micro-skill
  pipeline so the router can load them incrementally.
- At session start, the skill-router scans your project and
  **recommends** the best-matching skills, agents, MCP servers, and
  harnesses.
- Mid-session, the context monitor watches every tool call, detects new
  stack signals, walks the graph, and **recommends** relevant skills,
  agents, MCP servers, and harnesses in real time — **nothing loads or
  installs without your approval**.

The result: you always know what skills, agents, MCP servers, and harnesses are
available for your current task. The graph reveals hidden connections. The wiki
learns from your usage. Stale ones are flagged. New ones self-ingest.

## Explore the docs

<div class="grid cards" markdown>

-   **Knowledge graph**

    ---

    13,218 shipped skill/agent/MCP nodes, plus cataloged harnesses when
    present, connected by 963,068 weighted edges across 24 Louvain
    communities.
    Ships pre-built in `graph/wiki-graph.tar.gz` and powers the
    graph-aware recommendations + the pre-ship `ctx-dedup-check` gate.

    [:octicons-arrow-right-24: Knowledge graph](knowledge-graph.md)

-   **Entity onboarding**

    ---

    Step-by-step commands for adding a skill, agent, MCP server, or
    harness to the wiki and graph. Includes the `text-to-cad` harness
    pattern for custom-model users.

    [:octicons-arrow-right-24: Entity onboarding](entity-onboarding.md)

-   **Dashboard**

    ---

    `ctx-monitor serve` opens a local HTTP dashboard with live graph,
    skill grades + four-signal scores, session timelines, and one-click
    load/unload for skills, agents, and MCP servers. Dashboard harness
    exposure is not yet present. Zero dependencies beyond stdlib.

    [:octicons-arrow-right-24: Dashboard reference](dashboard.md)

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
    the stack matrix, loads exactly the skills that apply, and can
    recommend supporting agents, MCP servers, and harnesses.

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

    **v0.7.x** — MIT, CI-matrixed (Ubuntu + Windows × Python 3.11/3.12),
    3,287+ tests passing. Ships console scripts including `ctx-init`,
    `ctx-monitor` (local dashboard with graph + wiki + load/unload for
    skills, agents, and MCP servers; harness exposure not yet present),
    `ctx-dedup-check` (pre-ship near-duplicate gate), and
    `ctx-tag-backfill` (catalog hygiene), plus the ~25 MB pre-built
    wiki tarball with **13,218 nodes / 963,068 edges / 24 Louvain
    communities**. Hardened across the Strix audit + a 12-finding codex
    review.

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
