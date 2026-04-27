# ctx — Skill, Agent & MCP Recommendation for Claude Code

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-green.svg)](https://python.org)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)
[![Tests](https://img.shields.io/badge/Tests-3190_passing-brightgreen.svg)](#)
[![Graph](https://img.shields.io/badge/Graph-13%2C218_nodes_/_963K_edges-red.svg)](graph/)
[![Docs](https://img.shields.io/badge/docs-MkDocs_Material-blue.svg)](https://stevesolun.github.io/ctx/)

Watches what you develop, walks a knowledge graph of **1,968 skills, 464 agents, and 10,786 MCP servers**, and recommends the right bundle on the fly. You approve, it installs; you stop using, it unloads. Powered by a Karpathy LLM wiki with persistent memory that gets smarter every session.

> **2026-04-27 updates.**
> - Imported [mattpocock/skills](https://github.com/mattpocock/skills) — 21 opinionated skills (TDD, domain-model, ubiquitous-language, github-triage, caveman compression mode, write-a-skill, plus 15 more) deployed under the `mattpocock-` prefix. See [`imported-skills/mattpocock/ATTRIBUTION.md`](imported-skills/mattpocock/ATTRIBUTION.md).
> - Imported [designdotmd.directory](https://designdotmd.directory) — 156 DESIGN.md files (visual identities: color tokens, typography, spacing, components + rationale) deployed under the `designdotmd-` prefix. These are reference designs an agent can read when asked to build a UI. See [`imported-skills/designdotmd/ATTRIBUTION.md`](imported-skills/designdotmd/ATTRIBUTION.md).
> - Skill total: 1,791 → **1,968** (+177).

## Why it exists

- **Discovery** — with 1,700+ skills, 400+ agents, and 10K+ MCPs, you can't possibly know which exist or which apply to your current work.
- **Context budget** — loading everything wastes tokens and degrades quality. You need the right 10–15 per session.
- **Skill rot** — skills you installed months ago and never used are cluttering context. Stale ones should be flagged automatically.

## Install

```bash
pip install claude-ctx
ctx-init --hooks            # one-shot setup: directories, hooks, starter toolboxes
```

Optional extras: `pip install "claude-ctx[embeddings]"` for the semantic backend, `pip install "claude-ctx[dev]"` for the test toolchain.

### Pre-built knowledge graph (optional)

A pre-built knowledge graph of 13,218 nodes and 963K edges ships as a tarball. Extract to get a ready-to-use `~/.claude/skill-wiki/`:

```bash
# after `git clone` — or download graph/wiki-graph.tar.gz from the GitHub release
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

> **Windows / Git-Bash / MSYS:** pass `--force-local` so `tar` doesn't read the `c:` in the path as a remote host: `tar --force-local xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/`. Linux/macOS users can ignore.

## Use

After install, the `ctx` hooks integrate automatically with Claude Code's `PostToolUse` + `Stop` events. Typical flow:

```bash
ctx-scan-repo --repo .     # scan current repo, surface recommended skills/agents
ctx-skill-quality list     # four-signal quality score for every skill
ctx-skill-quality explain python-patterns   # drill into a single skill
ctx-skill-health dashboard # structural health + drift detection
ctx-toolbox run --event pre-commit          # run a council on the current diff
ctx-monitor serve          # local dashboard: http://127.0.0.1:8765/
```

The **`ctx-monitor`** dashboard shows currently loaded skills with load/unload buttons, a cytoscape graph view (`/graph?slug=…`), the LLM-wiki entity browser (`/wiki/<slug>`), a filterable skills grid, a session timeline, an audit log viewer, and a live SSE event stream.

Full docs, architecture, and every module: **<https://stevesolun.github.io/ctx/>**

## License

MIT — see [LICENSE](LICENSE).
