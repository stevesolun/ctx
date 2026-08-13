---
hide:
  - navigation
---

# CTX Fit — the cheapest AI coding setup that works on your repo

[![Repo views](https://hits.sh/github.com/stevesolun/ctx.svg?label=repo%20views)](https://hits.sh/github.com/stevesolun/ctx/)

**CTX Fit profiles your repository, evaluates candidate AI coding
configurations against real tasks taken from the repository's own history, and
produces the winning configuration as a reviewable change.** It picks the
*cheapest* setup that *reliably* works — reliability is a requirement, not a
tie-break — and "keep what you already have" is a valid and expected answer.

The winner is chosen by a fixed rule, not a score: discard every candidate
below the reliability floor, then minimize attributable cost, then break ties
toward the simpler configuration. An LLM may explain a result; it never decides
one.

!!! warning "CTX Fit ships from source, not yet from PyPI"

    The published `claude-ctx` package is release **1.0.20**, and it contains
    none of CTX Fit — `pip install claude-ctx` gets you the recommendation
    surface documented further down, whose `ctx` command is the agent-loop
    harness (`ctx run`, `ctx resume`, `ctx sessions`). This source tree
    declares `1.0.21` for unreleased work. To use `ctx fit` today you need a
    source checkout.

    ```bash
    git clone https://github.com/stevesolun/ctx && cd ctx
    pip install -e .
    cd /path/to/my-project
    ctx fit
    ```

    Requires CPython 3.11 or newer on Linux or macOS. Native Windows and
    PowerShell are not supported; run ctx inside WSL2.

## What each command actually does

Bare `ctx fit` is free, local and read-only. It invokes no model, spends
nothing, and on a plain run issues no `git` commands at all. It prints the
repository profile: detected languages, the AI coding setup already in place,
the verification commands the repository declares for itself, an agent-readiness
score with its component breakdown, and the highest-impact improvements.

| Command | What it costs | What it touches |
| --- | --- | --- |
| `ctx fit` | nothing; no model call | reads the working tree |
| `ctx fit --json` | nothing; no model call | reads the working tree, prints the profile as JSON |
| `ctx fit --dry-run` | nothing; no model call | additionally runs read-only git queries (`log`, `show --name-only`, `ls-tree`, `rev-parse`) to derive representative tasks, then prints the experiment plan and a cost estimate |
| `ctx fit --test --budget N` | up to `N` dollars | runs candidates and verifies each trial with the repository's own test command |
| `ctx fit --apply` | nothing beyond the evaluation | writes the winning configuration into your working tree; the write runs no git command, though the evaluation it needs first does |
| `ctx fit --pr` | nothing beyond the evaluation | creates a branch, commits, **pushes to your remote**, and opens a pull request through `gh` |

`--dry-run` reads history to derive tasks and writes nothing — not to the
repository, not to the index, not to any ref.

Spending requires two explicit flags. `--test` alone will not spend: without
`--budget` CTX Fit only plans. Run `ctx doctor` to see whether a real evaluation
can run where you are; without provider credentials `--test` runs in
simulation, which proves the pipeline but never your repository, and a
simulated result is refused as evidence for `--apply` and `--pr`.

### `--apply` and `--pr` are different writes

`--apply` writes files into your working tree, on whatever branch you are
standing on. It prints every proposed change first and, unless you pass
`--yes`, stops there so you can look. The write itself runs no git command:
nothing is staged, committed, or pushed. Getting to it does run git — `--apply`
is refused without evidence from `ctx fit --test --budget N`, and deriving the
tasks for that evaluation uses the same read-only queries `--dry-run` uses.

Each proposed change names the file and whether CTX Fit is *creating* or
*modifying* it, and that word decides how you review and undo it. Today every
plan contains exactly one artifact, `AGENTS.md`:

| It printed | State after the write | Review with | Undo with |
| --- | --- | --- | --- |
| `modify: AGENTS.md` | tracked, modified | `git diff` | `git checkout -- AGENTS.md` |
| `create: AGENTS.md` | new and **untracked** | `git status --short` shows `?? AGENTS.md` | delete the file |

!!! warning "`git checkout` cannot undo a file git has never seen"

    The `create` row is the common one, because a repository with no agent
    instruction file is exactly the repository CTX Fit's own scorer flags first
    (`Add an AGENTS.md describing the project, conventions, and how to verify a
    change. (+12)`). A newly created file is not in the index, so `git diff`
    prints nothing for it and `git checkout -- AGENTS.md` fails with `error:
    pathspec 'AGENTS.md' did not match any file(s) known to git`, leaving the
    file in place. CTX Fit's own closing line after a write recommends that
    pair without qualifying it; on a `create` follow the table instead.

**`--pr` writes to a remote.** It creates a branch, commits the winning
configuration, pushes it to `origin`, and opens a pull request through the
GitHub CLI. Before running anything it prints the pull-request body, the files
it will write, and the exact command sequence:

```text
git checkout -b ctx-fit/<timestamp>
git add -- <paths>
git commit -m "<pull request title>"
git push --set-upstream origin ctx-fit/<timestamp>
gh pr create --title "<pull request title>" --body-file -
```

Without `--yes` it stops there and changes nothing. With `--yes` it writes those
files into the working tree and then runs those five commands, in that order and
no others. Before any of them runs, the gate described below runs read-only
probes — `git rev-parse`, `git status`, `git remote get-url`, and `gh auth
status` — which is what lets every refusal leave the repository exactly as it
found it. CTX Fit never merges.

`--pr` refuses before touching anything if you are not inside a git repository,
if the working tree has changes CTX Fit did not write (untracked files
included — they would be carried onto the new branch), if `gh` is not installed
or not logged in, if the branch already exists, or if there is no remote to
push to. Each refusal names which one it was, exits non-zero, and leaves the
tree untouched. If a command fails partway, CTX Fit reports which one and how
many ran, and how to get back to the branch you were on; the files it had
already written stay in your working tree.

## The recommendation surface (existing, and what PyPI ships)

The rest of this site documents the graph-backed recommendation layer that
predates CTX Fit. It still ships, it still works, and it is what release
`1.0.20` installs. It is not the product; it is the inventory and routing
machinery underneath, useful on its own.

!!! tip "Install the recommendation surface"

    ```bash
    pip install claude-ctx
    ctx-init --graph --model-mode skip
    ```

    Optional extras: `pip install "claude-ctx[embeddings]"` for the
    semantic backend, `pip install "claude-ctx[harness]"` for local/API
    model harness runs, `pip install "claude-ctx[dev]"` for the
    pytest/mypy/ruff toolchain. After install the `ctx`, `ctx-init`,
    `ctx-scan-repo`, `ctx-mcp-server`, `ctx-source-registry`,
    `ctx-telemetry-export`, and `ctx-telemetry-retention` console scripts are
    on PATH; `python -m ctx --help` reaches the same CLI as `ctx`. Skill
    quality and health tooling moved off PATH and is reached with
    `python -m skill_quality` and
    `python -m ctx.adapters.claude_code.skill_health`.
    `ctx-init --graph` installs the fast
    pre-built runtime graph that powers recommendations and harness dry-runs;
    source checkouts use `graph/wiki-graph-runtime.tar.gz`, while pip installs
    download the matching GitHub release asset. Use
    `ctx-init --graph --graph-install-mode full` when you want the full
    packed LLM-wiki installed locally.

    Custom-model users can run
    `ctx-init --model-mode custom --model <provider/model> --goal "<task>"`
    to record the model profile and surface harness recommendations.

Point it at your organization's own tools, or use the pre-built graph, and ctx
recommends the smallest useful bundle for the current development window: the
right skills, agents, MCP servers, and optional harness at the right moment, so
hosted LLMs burn fewer tokens and local models waste less CPU/GPU work.

It walks a knowledge graph of **68,494 skill pages, 467 agents, 10,790 MCP servers, and 207 cataloged harnesses**.
The live execution bundle is skills, agents, and MCP servers only; custom/API/local
model users and external loop adapters get separate harness recommendations
after explicit user-owned model consent, ranked by model choice and task goal.
You decide what to load, install, or adopt.

### Why this surface exists

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

ctx treats your inventory as a **knowledge graph with persistent memory**, not
a flat directory.

The core idea comes from Andrej Karpathy's LLM-wiki pattern: instead of
re-loading everything from scratch each session, an LLM maintains a wiki
it can read, write, and query. The wiki becomes the agent's long-term
memory. ctx applies that pattern to entity management and extends it with
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
- Recommendation calls can suppress already selected, rejected, active, or
  baseline context and can filter local/no-key or language-mismatched rows
  before they enter a plan.
- During custom/API/local model onboarding and LoopFlow/agent-loop adapter
  calls with explicit user-owned model consent, `ctx-init`,
  `python -m harness_install`, and `python -m ctx.adapters.loopflow` use the same
  graph to recommend harnesses above the configured harness match floor.

## Explore the docs

<div class="grid cards" markdown>

-   **Knowledge graph**

    ---

    79,958 shipped graph nodes: 12,934 curated skill/agent/MCP/harness nodes plus 67,024 body-backed skill nodes. The graph has
    1,778,069 weighted edges and 52 Louvain communities.
    Ships pre-built in `graph/wiki-graph.tar.gz` and powers the
    graph-aware recommendations + the pre-ship
    `python -m ctx.core.quality.dedup_check` gate.

    [:octicons-arrow-right-24: Knowledge graph](knowledge-graph.md)

-   **Entity onboarding**

    ---

    Step-by-step commands for adding a skill, agent, MCP server, or
    harness to the wiki and graph. Includes the `text-to-cad` harness
    pattern for custom-model users.

    [:octicons-arrow-right-24: Entity onboarding](entity-onboarding.md)

-   **Dashboard**

    ---

    `python -m ctx_monitor serve` opens a local HTTP dashboard over the
    recommendation surface: live graph, skill grades + four-signal scores,
    session timelines, one-click load/unload for skills, agents, and MCP
    servers, selectable recommendations, runtime token history, plus harness
    wiki and graph browsing. It shows no CTX Fit state. It is served by stdlib
    `http.server` and renders repo docs with MkDocs-compatible Markdown
    extensions.

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
    recommend supporting agents and MCP servers. Loop adapters can call
    the same recommender before each plan.

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

    Current main is **v1.0.21** — MIT, tested on CPython 3.11+ for Linux and macOS,
    8,581 test inventory. Ships seven console scripts led by `ctx` and
    `ctx-init`. The maintenance
    tools are still shipped and still work, now via `python -m`:
    `ctx_monitor serve` (local dashboard with graph + wiki + load/unload for
    skills, agents, and MCP servers, plus Harness Setup for user-owned LLMs),
    `ctx.core.graph.incremental_attach`, `ctx.core.graph.incremental_shadow`,
    `ctx.core.quality.dedup_check` (pre-ship near-duplicate gate), and
    `ctx.core.quality.tag_backfill` (entity hygiene), plus a fast runtime graph artifact
    and the full ~281 MiB wiki tarball with **79,958 nodes / 1,778,069 edges / 52 Louvain communities**.

    [:octicons-arrow-right-24: CHANGELOG](https://github.com/stevesolun/ctx/blob/main/CHANGELOG.md) ·
    [Repository](https://github.com/stevesolun/ctx)

</div>

## Principles

- **Reliability is a filter, not a weight.** A configuration that is cheaper
  but less reliable does not win; it is discarded before cost is compared.
- **Verification is the repository's own.** CTX Fit judges a trial with the
  test, typecheck, lint, and build commands your repository already declares —
  never with an agent's self-report.
- **Unknown cost stays unknown.** A cost record carries its completeness state,
  and an incomplete record is never compared as if it were complete.
- **Explicit approval.** ctx can recommend, review, install, update, unload,
  or uninstall, but it does not mutate live skills, agents, MCP servers, or
  harness installs without a command or approval path.
- **Configurable gates.** Recommendation floors, semantic edge thresholds,
  micro-skill line limits, and harness match floors live in config so teams
  can tune behavior without forking the code.
- **Token discipline.** Every council run honors `max_tokens` /
  `max_seconds` budgets.

## Before pushing a change to ctx itself

```bash
scripts/no_mistakes_run.sh fast --profile smoke
scripts/no_mistakes_run.sh fast
scripts/no_mistakes_run.sh gate --intent "narrow task statement for this branch"
```

`scripts/no_mistakes_run.sh fast --profile smoke` is the quick first pass:
it keeps cheap invariants, no-test policy, ruff, and public docs tracker
checks while deferring slow unit, package, graph, browser, similarity,
telemetry, and strict docs lanes. `scripts/no_mistakes_run.sh fast` is the
full fast front door before no-mistakes or PR: it selects the same PR
checks, splits independent work into isolated temporary worktree lanes, and
runs them in parallel against committed branch history. The wrapper writes
lane timing evidence to `.gate/local-fast.json` by default, and lane filters
support fast reruns such as
`scripts/no_mistakes_run.sh fast --lane static --lane unit`. Unit-family
checks run as separate `unit`, `canary`, `contract`, and `clean-host` lanes
so the broad coverage pass no longer serializes the canary and clean-host
smoke checks.
`scripts/no_mistakes_run.sh gate --intent ...` refuses implicit/stale
intent, runs smoke + full local-fast first, then starts no-mistakes with the
explicit branch objective. The serial preflight/no-mistakes path remains
available when you need to inspect individual checks. Preflight uses the
same changed-file classifier as GitHub Actions and runs the matching local
gates before you open a PR: stats, ruff format/check, mypy, pip check, unit
coverage, canaries, package build, twine, docs, graph validation, browser,
and similarity checks as needed.
When graph artifacts are still Git LFS pointers, preflight hydrates only
the required tarballs, verifies their pointer SHA-256 and size caps, then
validates the artifacts.
Use `--profile full` before release work to force the source/package gates
even for docs-only or graph-only changes. Docs changes run public docs
tracker checks before the strict MkDocs build, including bug-smoke,
feature, dashboard, and toolbox coverage. Always pass an explicit narrow
no-mistakes intent so review/test/doc agents validate this branch instead
of inferring a stale broader goal from local transcripts. Public docs
surfaces are release-tracked: when
`mkdocs.yml` adds, removes, or moves a nav `.md` page, or public linked
assets under `docs/assets/javascripts/`, `docs/services/`, or
`docs/toolbox/templates/` change, update the relevant supporting ledger
(`docs/qa/feature-user-story-status.csv` or
`docs/qa/dashboard-user-story-status.csv`) and the canonical
`qa/feature_status.csv` with the exact path in `entrypoint_or_route`.
Bug-smoke audit rows live in `qa/bug_smoke_status.csv` and are validated
by the same public docs tracker; `Retested Pass` rows must include `PASS:`
retest evidence and a closed `next_action` starting with `Closed;`.
