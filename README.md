# ctx — Skill, Agent, MCP & Harness Catalog

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-green.svg)](https://python.org)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)
[![Tests](https://img.shields.io/badge/Tests-3358_collected-brightgreen.svg)](#)
[![Graph](https://img.shields.io/badge/Graph-104%2C078_nodes_/_1.0M_edges-red.svg)](graph/)
[![Docs](https://img.shields.io/badge/docs-MkDocs_Material-blue.svg)](https://stevesolun.github.io/ctx/)

Watches what you develop, walks a graph that combines **92,815 skills, 464 agents, 10,786 MCP servers, and 13 cataloged harnesses**, and recommends the right execution bundle on the fly. Execution recommendations are capped to the best skills, agents, and MCP servers for the current task; custom/API/local model users get a separate harness-catalog recommendation during onboarding or `ctx-harness-install`. The skill count includes 1,969 curated ctx skills plus 90,846 remote-cataloged Skills.sh skill nodes with upstream `npx skills` install instructions, duplicate hints, and metadata-only quality/security signals. You approve what loads, installs, or gets adopted. Powered by a Karpathy LLM wiki with persistent memory that gets smarter every session.

> **2026-04-29 updates.**
> - Added the curated `find-skills` workflow, backed by the canonical upstream install command `npx skills add https://github.com/vercel-labs/skills --skill find-skills`.
> - Shipped 90,846 Skills.sh entries as first-class remote-cataloged `skill` nodes inside `graph/wiki-graph.tar.gz` and as `graph/skills-sh-catalog.json.gz`.
> - Added 13 cataloged harnesses, including LangGraph, CrewAI, AutoGen, Google ADK, Semantic Kernel, Mastra, Pydantic AI, Haystack, OpenAI Agents SDK, LiteLLM, Langfuse, AgentOps, and text-to-cad.
> - Added security/cyber review warnings to entity update reviews and documented the graph/wiki update procedure.

> **2026-04-27 updates.**
> - Imported [mattpocock/skills](https://github.com/mattpocock/skills) — 21 opinionated skills (TDD, domain-model, ubiquitous-language, github-triage, caveman compression mode, write-a-skill, plus 15 more) deployed under the `mattpocock-` prefix. See [`imported-skills/mattpocock/ATTRIBUTION.md`](imported-skills/mattpocock/ATTRIBUTION.md).
> - Imported [designdotmd.directory](https://designdotmd.directory) — 156 DESIGN.md files (visual identities: color tokens, typography, spacing, components + rationale) deployed under the `designdotmd-` prefix. These are reference designs an agent can read when asked to build a UI. See [`imported-skills/designdotmd/ATTRIBUTION.md`](imported-skills/designdotmd/ATTRIBUTION.md).
> - Skill total: 1,791 → **1,968** (+177).

## Why it exists

- **Discovery** — with 92K+ skill nodes, 460+ agents, 10K+ MCP servers, and 13 cataloged harnesses, you can't possibly know which exist or which apply to your current work.
- **Context budget** — loading everything wastes tokens and degrades quality. You need the right 10–15 per session.
- **Skill rot** — skills you installed months ago and never used are cluttering context. Stale ones should be flagged automatically.

## Install

```bash
pip install claude-ctx
ctx-init                    # terminal wizard: hooks, graph, model, harness goal
ctx-init --wizard           # force the same wizard from scripts/tests
ctx-init --model-mode skip  # non-interactive setup for automation
ctx-init --model-mode custom --model openai/gpt-5.5 --goal "build a CAD agent"
```

Optional extras: `pip install "claude-ctx[embeddings]"` for the semantic backend, `pip install "claude-ctx[dev]"` for the test toolchain.

### Pre-built knowledge graph (optional)

A pre-built knowledge graph of 104,078 nodes and 1,033,253 edges ships as a tarball. The same tarball includes `external-catalogs/skills-sh/catalog.json`, 90,846 remote-cataloged Skills.sh skill pages under `entities/skills/skills-sh-*.md`, and 13 cataloged harness pages under `entities/harnesses/`. Extract to get a ready-to-use `~/.claude/skill-wiki/`:

```bash
# after `git clone` — or download graph/wiki-graph.tar.gz from the GitHub release
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

> **Windows / Git-Bash / MSYS:** pass `--force-local` so `tar` doesn't read the `c:` in the path as a remote host: `tar --force-local xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/`. Linux/macOS users can ignore.

## Use

After install, the `ctx` hooks integrate automatically with Claude Code's `PostToolUse` + `Stop` events. Typical flow:

```bash
ctx-scan-repo --repo .     # scan current repo and stack signals
ctx-scan-repo --repo . --recommend  # include skill/agent/MCP recommendations
ctx-agent-add --agent-path ./code-reviewer.md --name code-reviewer
ctx-harness-add --repo https://github.com/earthtojake/text-to-cad --tag cad
ctx-harness-install text-to-cad --dry-run   # inspect before cloning/running anything
ctx-harness-install text-to-cad --update --dry-run
ctx-harness-install text-to-cad --uninstall --dry-run
ctx-skill-quality list     # four-signal quality score for every skill
ctx-skill-quality explain python-patterns   # drill into a single skill
ctx-skill-health dashboard # structural health + drift detection
ctx-toolbox run --event pre-commit          # run a council on the current diff
ctx-monitor serve          # local dashboard: http://127.0.0.1:8765/
```

The **`ctx-monitor`** dashboard shows currently loaded skills, agents, MCP servers, and installed harness records. It provides load/unload buttons where ctx owns the live action, a cytoscape graph view (`/graph?slug=…`), the LLM-wiki entity browser (`/wiki/<slug>`), a filterable skills grid, a session timeline, an audit log viewer, and a live SSE event stream. Harnesses are visible in the dashboard loaded/wiki/graph views; harness install/update/uninstall actions stay in `ctx-harness-install`.

When `ctx-skill-add`, `ctx-agent-add`, `ctx-mcp-add`, or `ctx-harness-add`
finds an existing entity, ctx prints a benefits/risks update review and skips
replacement by default. Re-run with `--update-existing` to apply the catalog or
local asset update after review.

Step-by-step entity onboarding:
**<https://stevesolun.github.io/ctx/entity-onboarding/>**

Full docs, architecture, and every module: **<https://stevesolun.github.io/ctx/>**

## License

MIT — see [LICENSE](LICENSE).
