---
hide:
  - navigation
---

# ctx — Skill, Agent, MCP & Harness Recommendations

[![Repo views](https://hits.sh/github.com/stevesolun/ctx.svg?label=repo%20views)](https://hits.sh/github.com/stevesolun/ctx/)

**ctx is not an Amazon-style catalog of skills, MCPs, agents, tools, or
harnesses. It is a recommendation layer.** Point it at your organization's own
tools, or use the pre-built graph, and ctx recommends the smallest useful bundle
for the current development window. The goal is to load the right skills,
agents, MCP servers, and optional harness at the right moment so hosted LLMs
burn fewer tokens and local models waste less CPU/GPU work.

Watches what you develop, walks a knowledge graph of **68,494 skill pages, 467 agents, 10,790 MCP servers, and 207 cataloged harnesses**, and recommends the
right execution bundle on the fly. The live execution bundle is skills,
agents, and MCP servers only; custom/API/local model users get a separate
harness recommendation based on model choice and task goal. You decide
what to load, install, or adopt. Powered by a Karpathy LLM wiki with persistent
memory that gets smarter every session.

!!! tip "Install"

    ```bash
    pip install claude-ctx
    ctx-init --graph --model-mode skip
    ```

    Optional extras: `pip install "claude-ctx[embeddings]"` for the
    semantic backend, `pip install "claude-ctx[harness]"` for local/API
    model harness runs, `pip install "claude-ctx[dev]"` for the
    pytest/mypy/ruff toolchain. After install the `ctx-scan-repo`,
    `ctx-skill-quality`, `ctx-skill-health`, and `ctx-toolbox` console
    scripts are on PATH. `ctx-init --graph` installs the fast pre-built
    runtime graph that powers recommendations and harness dry-runs; source checkouts use
    `graph/wiki-graph-runtime.tar.gz`, while pip installs download the
    matching GitHub release asset. Use
    `ctx-init --graph --graph-install-mode full` when you want the full
    markdown LLM-wiki expanded locally.

    Custom-model users can run
    `ctx-init --model-mode custom --model <provider/model> --goal "<task>"`
    to record the model profile and surface harness recommendations.

!!! tip "Before pushing"

    ```bash
    python scripts/ci_preflight.py --profile pr
    ```

    The preflight uses the same changed-file classifier as GitHub Actions and
    runs the matching local gates before you open a PR: stats, ruff, mypy, pip
    check, unit coverage, canaries, package build, twine, docs, graph
    validation, browser, and similarity checks as needed. Use `--profile full`
    before release work to force the source/package gates even for docs-only or
    graph-only changes. Docs changes run public docs tracker checks before the
    strict MkDocs build. Public docs surfaces are release-tracked: when
    `mkdocs.yml` adds, removes, or moves a nav `.md` page, or public linked
    assets under `docs/assets/javascripts/`, `docs/services/`, or
    `docs/toolbox/templates/` change, update
    `docs/qa/feature-user-story-status.csv` with the exact path in
    `entrypoint_or_route`.

## Why this exists

Claude Code skills, agents, MCP servers, and model harness profiles are
powerful, but at scale they become unmanageable:

- **Discovery problem** — with 68,494 skill pages, 467 agents, 10,790 MCP servers, and 207 harnesses, how do you know which
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

ctx solves all of these by treating your ctx inventory as a **knowledge
graph with persistent memory**, not a flat directory.

## What this is

ctx is not a collection of scripts. It is an agent with persistent memory
and a knowledge graph.

The core idea comes from Andrej Karpathy's LLM-wiki pattern: instead of
re-loading everything from scratch each session, an LLM maintains a wiki
it can read, write, and query. The wiki becomes the agent's long-term
memory.

ctx applies that pattern to entity management — and extends it with
graph-based discovery:

- A Karpathy 3-layer wiki at `~/.claude/skill-wiki/` is the single source
  of truth.
- **79,958 graph nodes** for the shipped skill/agent/MCP/harness
  inventory, including 68,494 skill pages
  and 207 harness pages under `entities/harnesses/`.
  Each page tracks tags, status, provenance, and usage where it applies.
- A **knowledge graph** (79,958 nodes, 1,778,069 edges) built from a
  12,934-node core plus 67,024 body-backed skill nodes.
  The graph has 52 Louvain communities and blends semantic cosine,
  tag overlap, and slug-token overlap; 67,024 skill bodies are
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
  `ctx-harness-install` use the same graph to recommend harnesses
  above the configured harness match floor.

The result: you always know what skills, agents, and MCP servers are available
for your current task, and which harness fits when you choose your own model.
The graph reveals hidden connections. The wiki learns from your usage. Stale
ones are flagged. New ones self-ingest.

## Explore the docs

<div class="grid cards" markdown>

-   **Knowledge graph**

    ---

    79,958 shipped graph nodes: 12,934 curated skill/agent/MCP/harness nodes plus 67,024 body-backed skill nodes. The graph has
    1,778,069 weighted edges and 52 Louvain communities.
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
    and graph browsing. It is served by stdlib `http.server` and renders
    repo docs with MkDocs-compatible Markdown extensions.

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

-   **Source snapshot**

    ---

    Current main is **v1.0.20** — MIT, CI-matrixed (Ubuntu 3.12 plus Windows/macOS 3.11/3.12),
    4,400 test inventory. Adds enterprise OpenTelemetry-ready telemetry and
    ships console scripts including `ctx-init`,
    `ctx-monitor` (local dashboard with graph + wiki + load/unload for
    skills, agents, and MCP servers, plus Harness Setup for user-owned LLMs),
    `ctx-incremental-attach`, `ctx-incremental-shadow`, `ctx-dedup-check`
    (pre-ship near-duplicate gate), and
    `ctx-tag-backfill` (entity hygiene), plus a fast runtime graph artifact
    and the full ~282 MiB wiki tarball with **79,958 nodes / 1,778,069 edges / 52 Louvain communities**.

    [:octicons-arrow-right-24: CHANGELOG](https://github.com/stevesolun/ctx/blob/main/CHANGELOG.md) ·
    [Repository](https://github.com/stevesolun/ctx)

</div>

## Principles

- **Single source of truth.** The wiki and graph drive Claude Code
  recommendations, custom-model harness recommendations, dashboard views,
  and entity update reviews.
- **Explicit approval.** ctx can recommend, review, install, update, unload,
  or uninstall, but it does not mutate live skills, agents, MCP servers, or
  harness installs without a command or approval path.
- **Configurable gates.** Recommendation floors, semantic edge thresholds,
  micro-skill line limits, and harness match floors live in config so teams
  can tune behavior without forking the code.
- **Evidence over opinion.** Suggestions cite real usage data plus
  knowledge-graph edges. No black-box prompts.
- **Token discipline.** Every council run honors `max_tokens` /
  `max_seconds` budgets.
