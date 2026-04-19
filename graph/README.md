# Knowledge Graph

Pre-built knowledge graph of **2,253 nodes** (1,789 skills + 464 agents) with **454,719 edges** across **93 communities**. Average degree 416.6, max degree 1,152, **195,226 skill↔agent cross-type edges**. Every node is reproducibly derived from the YAML `tags:` list plus slug-token pseudo-tags (stop-word-filtered). Rebuild with `ctx-wiki-graphify`.

> v0.5.x shipped a stale `graph.json` with 642K edges from a build path that no longer exists. When the live build ran it silently produced only 861 edges because of a `DENSE_TAG_THRESHOLD=20` regression that dropped every tag with more than 20 nodes. v0.6.0 fixed the threshold, added slug-token semantic edges, taught `parse_frontmatter` to read multi-line YAML lists, and shipped a reproducible 454K-edge graph (v0.6.1 harvest expanded to 454,719 edges / 2,253 nodes after the Council of High Intelligence agents landed).

## Files

| File | Size | Contents |
|------|------|----------|
| `wiki-graph.tar.gz` | 11.7 MB | **Full wiki** (entity pages, concept pages, 956 converted micro-skill pipelines, knowledge graph, catalog) |
| `communities.json` | 153 KB | 865 detected communities with labels and member lists |
| `graph-report.md` | 32 KB | God nodes (most connected skills/agents) + community summary |
| `viz-overview.html` / `.png` | — | Plotly-rendered overview of the full graph |
| `viz-python.html` | — | Python-skills sub-view |
| `viz-security.html` / `.png` | — | Security-skills sub-view |
| `viz-ai-agents.html` | — | AI agents sub-view |
| `sample-top60.html` | — | Top-60-by-degree nodes, interactive |

### What's inside `wiki-graph.tar.gz`

- `entities/skills/` — **1,789** skill entity pages with YAML frontmatter
- `entities/agents/` — **446** agent entity pages
- `concepts/` — **74** auto-generated community concept pages
- `converted/` — **956** micro-skill pipelines (5-stage gated format)
- `graphify-out/graph.json` — full knowledge graph (2,253 nodes, 454,719 edges)
- `graphify-out/communities.json` — community detection results (93 communities)
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
- `entities/skills/*.md` — 1,789 skill entity pages with frontmatter (tags, description, use_count)
- `entities/agents/*.md` — 464 agent entity pages
- `concepts/*.md` — 74 auto-generated community pages grouping related skills
- `graphify-out/graph.json` — full networkx graph (load with `networkx.readwrite.node_link_graph`)
- `graphify-out/communities.json` — 93 community detection results

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

# 2,253 nodes, 454,719 edges
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
