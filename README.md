# ctx — Skill & Agent Recommendation for Claude Code

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-green.svg)](https://python.org)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)
[![Tests](https://img.shields.io/badge/Tests-1363_passing-brightgreen.svg)](#)
[![Graph](https://img.shields.io/badge/Graph-2,211_nodes_/_642K_edges-red.svg)](graph/)
[![Docs](https://img.shields.io/badge/docs-MkDocs_Material-blue.svg)](https://stevesolun.github.io/ctx/)

Watches what you develop, walks a knowledge graph of **1,769 skills and 443 agents** (2,212 nodes, 885 edges, 865 communities), and recommends the right ones on the fly — you decide what to load and unload. Powered by a Karpathy LLM wiki with persistent memory that gets smarter every session.

## Why it exists

- **Discovery** — with 1,700+ skills and 400+ agents, you can't possibly know which exist or which apply to your current repo.
- **Context budget** — loading everything wastes tokens and degrades quality. You need the right 10–15 per session.
- **Skill rot** — skills you installed months ago and never used are cluttering context. Stale ones should be flagged automatically.

## Install

```bash
pip install claude-ctx
ctx-init --hooks            # one-shot setup: directories, hooks, starter toolboxes
```

Optional extras: `pip install "claude-ctx[embeddings]"` for the semantic backend, `pip install "claude-ctx[dev]"` for the test toolchain.

### Pre-built knowledge graph (optional)

A pre-built knowledge graph of 2,211 nodes and 642K edges ships as a tarball. Extract to get a ready-to-use `~/.claude/skill-wiki/`:

```bash
# after `git clone` — or download graph/wiki-graph.tar.gz from the GitHub release
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

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
