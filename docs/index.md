---
hide:
  - navigation
---

# ctx — Skill, Agent, MCP & Harness Catalog

Watches what you develop, walks a knowledge graph of **91,432 skills, 464
agents, 10,787 MCP servers, and 13 cataloged harnesses**, and recommends the
right execution bundle on the fly. The live execution bundle is skills,
agents, and MCP servers only; custom/API/local model users get a separate
harness-catalog recommendation based on model choice and task goal. You decide
what to load, install, or adopt. Powered by a Karpathy LLM wiki with persistent
memory that gets smarter every session.

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

- **Discovery problem** — with 91K+ skills, 460+ agents, 10,000+
  MCP servers, and 13 cataloged harnesses, how do you know which
  ones exist and which are relevant to your current project?
- **Context budget** — loading every installable entity wastes tokens and
  degrades quality. You need exactly the right skills, agents, and MCP
  servers per session, plus a harness recommendation only when you choose
  a custom/API/local model path.
- **Hidden connections** — a FastAPI skill is useful, but you also need
  the Pydantic skill, the async Python patterns skill, and the Docker
  skill, plus possibly a matching MCP server. If you are not using Claude
  Code, ctx separately suggests the model harness most likely to fit your
  goal.
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
- **102,696 entity pages/nodes** for the shipped skill/agent/MCP/harness
  inventory, including 89,463 body-backed Skills.sh skill pages
  and 13 cataloged harness pages under `entities/harnesses/`.
  Each page tracks tags, status, provenance, and usage where it applies.
- A **knowledge graph** (102,696 nodes, 2,900,834 edges) built from a
  13,233-node curated core plus 89,463 body-backed Skills.sh `skill`
  nodes. The graph has 52 Louvain communities and blends semantic cosine,
  tag overlap, and slug-token overlap; 89,463 hydrated Skills.sh bodies are
  shipped as installable `SKILL.md` files. Entries over the configured line
  threshold are converted to gated micro-skill orchestrators. Full source
  bodies were used for semantic graphing before packaging; `SKILL.md.original`
  backups are not shipped in the tarball.
- **52 Louvain communities** group related entities into named
  communities (e.g., *AI + Devops + Frontend*, *Python + API*).
- PostToolUse and Stop hooks update the wiki automatically during each
  Claude Code session.
- Hydrated skills over 180 lines are converted to gated micro-skill
  pipelines so the router can load them incrementally.
- At session start, the skill-router scans your project and
  **recommends** the best-matching skills, agents, and MCP servers.
- Mid-session, the context monitor watches every tool call, detects new
  stack signals, walks the graph, and **recommends** relevant skills,
  agents, and MCP servers in real time — **nothing loads or
  installs without your approval**.
- During custom/API/local model onboarding, `ctx-init` and
  `ctx-harness-install` use the same graph catalog to recommend harnesses
  above the configured harness match floor.

The result: you always know what skills, agents, and MCP servers are available
for your current task, and which harness fits when you choose your own model.
The graph reveals hidden connections. The wiki learns from your usage. Stale
ones are flagged. New ones self-ingest.

## Explore the docs

<div class="grid cards" markdown>

-   **Knowledge graph**

    ---

    102,696 shipped graph nodes: 13,233 curated skill/agent/MCP/harness
    nodes plus 89,463 body-backed Skills.sh skill nodes. The graph has
    2,900,834 weighted edges and 52 Louvain communities.
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
    skill grades + four-signal scores, session timelines, one-click
    load/unload for skills, agents, and MCP servers, plus harness wiki
    and graph browsing. Zero dependencies beyond stdlib.

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
    recommend supporting agents and MCP servers.

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
    3,614 tests collected. Ships console scripts including `ctx-init`,
    `ctx-monitor` (local dashboard with graph + wiki + load/unload for
    skills, agents, and MCP servers, plus harness wiki/graph browsing),
    `ctx-dedup-check` (pre-ship near-duplicate gate), and
    `ctx-tag-backfill` (catalog hygiene), plus the ~336 MiB pre-built
    wiki tarball with **102,696 nodes / 2,900,834 edges / 52 Louvain
    communities**.

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
