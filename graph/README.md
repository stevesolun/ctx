# Knowledge Graph

Pre-built knowledge graph of **2,211 nodes** (1,768 skills + 443 agents) with **642,468 edges** across **865 communities**.

## Files

| File | Size | Contents |
|------|------|----------|
| `wiki-graph.tar.gz` | 11.7 MB | **Full wiki** (entity pages, concept pages, 952 converted micro-skill pipelines, knowledge graph, catalog) |
| `communities.json` | 153 KB | 865 detected communities with labels and member lists |
| `graph-report.md` | 32 KB | God nodes (most connected skills/agents) + community summary |
| `viz-overview.html` / `.png` | — | Plotly-rendered overview of the full graph |
| `viz-python.html` | — | Python-skills sub-view |
| `viz-security.html` / `.png` | — | Security-skills sub-view |
| `viz-ai-agents.html` | — | AI agents sub-view |
| `sample-top60.html` | — | Top-60-by-degree nodes, interactive |

### What's inside `wiki-graph.tar.gz`

- `entities/skills/` — **1,768** skill entity pages with YAML frontmatter
- `entities/agents/` — **443** agent entity pages
- `concepts/` — **61** auto-generated community concept pages
- `converted/` — **952** micro-skill pipelines (5-stage gated format)
- `graphify-out/graph.json` — full knowledge graph (2,211 nodes, 642,468 edges)
- `graphify-out/communities.json` — community detection results (865 communities)
- `catalog.md` — bulk listing of all skills and agents
- `SCHEMA.md`, `index.md`, `log.md` — wiki infrastructure
- `.obsidian/` — Obsidian vault config, so the extracted tree opens as a graph directly in Obsidian

## Usage

### Extract the wiki

```bash
# Extract to ~/.claude/skill-wiki/ (or any directory)
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

This gives you:
- `entities/skills/*.md` — 1,768 skill entity pages with frontmatter (tags, description, use_count)
- `entities/agents/*.md` — 443 agent entity pages
- `concepts/*.md` — 61 auto-generated community pages grouping related skills
- `graphify-out/graph.json` — full networkx graph (load with `networkx.readwrite.node_link_graph`)
- `graphify-out/communities.json` — 865 community detection results

### Load the graph in Python

```python
import json
from pathlib import Path
from networkx.readwrite import node_link_graph

raw = json.loads(Path("~/.claude/skill-wiki/graphify-out/graph.json").expanduser().read_text())
# Auto-detect the NetworkX 2.x "links" vs 3.x "edges" schema.
# Graphs built by older ctx releases used "links".
edges_key = "links" if "links" in raw else "edges"
G = node_link_graph(raw, edges=edges_key)

# 2,211 nodes, 642,468 edges
print(G.number_of_nodes(), G.number_of_edges())

# Find skills related to "fastapi"
fastapi = "skill:fastapi-pro"
neighbors = sorted(G.neighbors(fastapi),
                   key=lambda n: G[fastapi][n]["weight"], reverse=True)[:10]
for n in neighbors:
    print(f"  {G.nodes[n]['label']} (weight={G[fastapi][n]['weight']})")
```

Or just use the dashboard:

```bash
ctx-monitor serve
# then open http://127.0.0.1:8765/graph?slug=fastapi-pro
```

### Open in Obsidian

The extracted wiki is an Obsidian-compatible vault. Entity pages use `[[wikilinks]]` for cross-references. Open the directory in Obsidian and use the graph view to explore visually.

## Rebuild

After adding new skills or changing the wiki:

```bash
python -m wiki_batch_entities --all
python -m wiki_graphify
```

Then re-archive:

```bash
cd ~/.claude/skill-wiki
tar czf /path/to/ctx/graph/wiki-graph.tar.gz \
    graphify-out/ entities/ concepts/ converted/ catalog.md \
    SCHEMA.md index.md log.md .obsidian/
```
