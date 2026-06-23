# ctx — Skill, Agent, MCP & Harness Recommendations

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-green.svg)](https://python.org)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)
[![Tests](https://img.shields.io/badge/Tests-4308_collected-brightgreen.svg)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![Graph](https://img.shields.io/badge/Graph-79%2C958_nodes_/_1%2C778%2C069_edges-red.svg)](https://stevesolun.github.io/ctx/knowledge-graph/)
[![Skills](https://img.shields.io/badge/Skills-68%2C494-blue.svg)](https://stevesolun.github.io/ctx/catalog/?type=skill)
[![Agents](https://img.shields.io/badge/Agents-467-purple.svg)](https://stevesolun.github.io/ctx/catalog/?type=agent)
[![MCPs](https://img.shields.io/badge/MCPs-10%2C790-pink.svg)](https://stevesolun.github.io/ctx/catalog/?type=mcp-server)
[![Harnesses](https://img.shields.io/badge/Harnesses-207-orange.svg)](https://stevesolun.github.io/ctx/catalog/?type=harness)
[![Docs](https://img.shields.io/badge/docs-MkDocs_Material-blue.svg)](https://stevesolun.github.io/ctx/)
[![Repo views](https://hits.sh/github.com/stevesolun/ctx.svg?label=repo%20views)](https://hits.sh/github.com/stevesolun/ctx/)

**ctx is not an Amazon-style catalog of skills, MCPs, agents, tools, or
harnesses. It is a recommendation layer.** Point it at your organization's own
tools, or use the pre-built graph, and ctx recommends the smallest useful bundle
for the current development window. The goal is to load the right skills,
agents, MCP servers, and optional harness at the right moment so hosted LLMs
burn fewer tokens and local models waste less CPU/GPU work.

ctx watches what you are building, walks a **79,958-node** graph, and
recommends a small, top-scored bundle of skills, agents, and MCP servers for
the current task. If you use your own local/API model instead of Claude Code,
ctx has a separate harness setup flow: tell it the model and goal, review the
recommended harness, then install with dry-run/update/uninstall controls.

Current shipped snapshot:

- **68,494 skill entity pages**, with **67,024** hydrated installable `SKILL.md` bodies.
- **467 agents**, **10,790 MCP servers**, and **207 harnesses**.
- **1,778,069 graph edges** across semantic similarity, tags, slug tokens, source overlap, direct links, quality, usage, type affinity, and graph structure.
- **28,612 long skill bodies** converted through the micro-skill gate instead of shipping raw long prompts.
- Entity updates for skills, agents, MCPs, and harnesses print benefits/risks and skip replacement unless you explicitly approve the update.

## Why it exists

- **Discovery** — with 68,494 skill pages, 467 agents, 10,790 MCP servers, and 207 harnesses, you can't possibly know which exist or which apply to your current work.
- **Context budget** — loading everything wastes tokens and degrades quality. You need the right 10–15 per session.
- **Skill rot** — skills you installed months ago and never used are cluttering context. Stale ones should be flagged automatically.

## Example user stories

The canonical QA tracker is
[`docs/qa/feature-user-story-status.csv`](docs/qa/feature-user-story-status.csv).
Examples from that tracker:

| Tracker row | User story | Expected ctx behavior |
| --- | --- | --- |
| `CLI-002` | As a user I can ask ctx for current repo recommendations. | `ctx-recommend` returns a capped, graph-scored bundle of relevant skills, agents, and MCP servers from the shared recommendation engine. |
| `CLI-026` | As a local/API model user I can get harness recommendations and install one. | `ctx-harness-install --dry-run` interviews model/goals/tools/privacy, recommends a fitting harness above threshold, or emits a no-fit custom harness PRD. |
| `API-011` | As a dashboard user I can manually add, edit, or delete entities. | `/api/entity/upsert` and `/api/entity/delete` validate type, slug, and body, then queue safe graph/wiki updates instead of mutating blindly. |

## Install

```bash
pip install claude-ctx
ctx-init                    # terminal wizard: hooks, graph, model, harness goal
ctx-init --graph --hooks --model-mode skip  # fast runtime graph + Claude Code hooks
ctx-init --graph --graph-install-mode full  # expand the full markdown wiki locally
ctx-init --wizard           # force the same wizard from scripts/tests
ctx-init --model-mode custom --model openai/gpt-5.5 --goal "build a CAD agent"
```

Optional extras: `pip install "claude-ctx[embeddings]"` for the semantic backend, `pip install "claude-ctx[harness]"` for local/API model harness runs, `pip install "claude-ctx[dev]"` for the test toolchain.

### Pre-built knowledge graph

Graph-backed recommendations need the pre-built graph. By default, `ctx-init
--graph` installs the fast runtime artifact: `graph/wiki-graph-runtime.tar.gz`
in source checkouts, or the matching GitHub release asset from pip installs.
It contains `graphify-out/*`, the shipped skill index needed for
recommendations, and the 207 harness pages needed by
`ctx-harness-install`:

```bash
ctx-init --graph
```

The full LLM-wiki artifact remains available for local browsing, Obsidian, and
expanded markdown pages:

```bash
ctx-init --graph --graph-install-mode full
```

The full `wiki-graph.tar.gz` includes the shipped skill index,
68,494 skill entity pages under `entities/skills/`, 67,024 hydrated
installable `SKILL.md` files under `converted/`,
and 207 harness pages under
`entities/harnesses/`.

> **Windows:** PowerShell's built-in `tar.exe` does not support
> `--force-local`; use `tar -xzf graph\wiki-graph.tar.gz -C "$env:USERPROFILE\.claude\skill-wiki"`.
> In Git Bash or MSYS, use `--force-local` only when your `-C` target is a
> drive-letter path such as `C:/Users/...`.

## Use

After `ctx-init --hooks` or the wizard hook step, ctx observes Claude Code's
`PostToolUse` and `Stop` events. Typical flow:

```bash
ctx-scan-repo --repo .     # scan current repo and stack signals
ctx-scan-repo --repo . --recommend  # include skill/agent/MCP recommendations
ctx-agent-add --agent-path ./code-reviewer.md --name code-reviewer
ctx-harness-add --repo https://github.com/earthtojake/text-to-cad --tag cad
ctx-harness-install text-to-cad --dry-run   # inspect before cloning/running anything
ctx-harness-install text-to-cad             # install after reviewing the plan
ctx-harness-install text-to-cad --update --dry-run
ctx-harness-install text-to-cad --uninstall --dry-run
ctx-skill-quality list     # four-signal quality score for every skill
ctx-skill-quality explain python-patterns   # drill into a single skill
ctx-skill-health dashboard # structural health + drift detection
ctx-toolbox run --event pre-commit          # run a council on the current diff
ctx-monitor serve          # local dashboard: http://127.0.0.1:8765/
```

Before pushing, run the local PR gate:

```bash
python scripts/ci_preflight.py --profile pr
```

It uses the same changed-file classifier as GitHub Actions, then runs the
matching local checks: stats, ruff, mypy, pip check, unit coverage, canaries,
package build, twine, docs, graph validation, browser, and similarity gates as
needed. Use `--profile full` before release work to force the source/package
gates even for docs-only or graph-only changes.

The **`ctx-monitor`** dashboard shows currently loaded skills, agents, MCP servers, installed harness records, and generic-harness validation/escalation state. It provides load/unload buttons where ctx owns the live action, a graph view (`/graph?slug=...`), the LLM-wiki entity browser (`/wiki/<slug>`), a filterable skills grid, a session timeline, audit/runtime log views, and a live SSE event stream. Installed harness records appear in `/loaded`; harness pages appear in `/wiki` and `/graph`. Harness install/update/uninstall actions stay in `ctx-harness-install`.

When `ctx-skill-add`, `ctx-agent-add`, `ctx-mcp-add`, or `ctx-harness-add`
finds an existing entity, ctx prints a benefits/risks update review and skips
replacement by default. Re-run with `--update-existing` to apply the catalog or
local asset update after review.

Step-by-step entity onboarding:
**<https://stevesolun.github.io/ctx/entity-onboarding/>**

Full docs, architecture, and every module: **<https://stevesolun.github.io/ctx/>**

## License

MIT — see [LICENSE](LICENSE).
