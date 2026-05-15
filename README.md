# ctx — Skill, Agent, MCP & Harness Recommendations

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-green.svg)](https://python.org)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)
[![Tests](https://img.shields.io/badge/Tests-3823_collected-brightgreen.svg)](#)
[![Graph](https://img.shields.io/badge/Graph-102%2C717_nodes_/_2.9M_edges-red.svg)](graph/)
[![Docs](https://img.shields.io/badge/docs-MkDocs_Material-blue.svg)](https://stevesolun.github.io/ctx/)

ctx watches what you are building, walks a **102,717-node** graph, and
recommends a small, top-scored bundle of skills, agents, and MCP servers for
the current task. If you use your own local/API model instead of Claude Code,
ctx has a separate harness catalog flow: tell it the model and goal, review the
recommended harness, then install with dry-run/update/uninstall controls.

Current shipped snapshot:

- **91,448 skills**: 1,985 curated/imported skills plus **89,463 body-backed Skills.sh skills**.
- **467 agents**, **10,787 MCP servers**, and **15 cataloged harnesses**.
- **2.9M graph edges** across semantic similarity, tags, slug tokens, source overlap, direct links, quality, usage, type affinity, and graph structure.
- **89,463 hydrated `SKILL.md` bodies** in the shipped LLM-wiki; long entries are converted through the micro-skill gate instead of loading raw long prompts.
- Entity updates for skills, agents, MCPs, and harnesses print benefits/risks and skip replacement unless you explicitly approve the update.

## Why it exists

- **Discovery** — with 91K+ skill nodes, 460+ agents, 10K+ MCP servers, and 15 cataloged harnesses, you can't possibly know which exist or which apply to your current work.
- **Context budget** — loading everything wastes tokens and degrades quality. You need the right 10–15 per session.
- **Skill rot** — skills you installed months ago and never used are cluttering context. Stale ones should be flagged automatically.

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
It contains `graphify-out/*` plus the external Skills.sh catalog needed for
recommendations and the 15 cataloged harness pages needed by
`ctx-harness-install`:

```bash
ctx-init --graph
```

The full LLM-wiki artifact remains available for local browsing, Obsidian, and
expanded markdown pages:

```bash
ctx-init --graph --graph-install-mode full
```

The full `wiki-graph.tar.gz` includes `external-catalogs/skills-sh/catalog.json`,
89,463 body-backed Skills.sh skill pages under `entities/skills/skills-sh-*.md`,
89,463 hydrated installable Skills.sh `SKILL.md` files under
`converted/skills-sh-*/`, and 15 cataloged harness pages under
`entities/harnesses/`.

> **Windows:** PowerShell's built-in `tar.exe` does not support
> `--force-local`; use `tar -xzf graph\wiki-graph.tar.gz -C "$env:USERPROFILE\.claude\skill-wiki"`.
> In Git Bash or MSYS, use `--force-local` only when your `-C` target is a
> drive-letter path such as `C:/Users/...`.

## Use

After `ctx-init --hooks` or the wizard hook step, ctx observes Claude Code's
`PostToolUse` + `Stop` events. Typical flow:

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

The **`ctx-monitor`** dashboard shows currently loaded skills, agents, MCP servers, installed harness records, and generic-harness validation/escalation state. It provides load/unload buttons where ctx owns the live action, a cytoscape graph view (`/graph?slug=…`), the LLM-wiki entity browser (`/wiki/<slug>`), a filterable skills grid, a session timeline, audit/runtime log views, and a live SSE event stream. Installed harness records appear in `/loaded`; cataloged harnesses appear in `/wiki` and `/graph`. Harness install/update/uninstall actions stay in `ctx-harness-install`.

When `ctx-skill-add`, `ctx-agent-add`, `ctx-mcp-add`, or `ctx-harness-add`
finds an existing entity, ctx prints a benefits/risks update review and skips
replacement by default. Re-run with `--update-existing` to apply the catalog or
local asset update after review.

Step-by-step entity onboarding:
**<https://stevesolun.github.io/ctx/entity-onboarding/>**

Full docs, architecture, and every module: **<https://stevesolun.github.io/ctx/>**

## License

MIT — see [LICENSE](LICENSE).
