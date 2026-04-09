# Knowledge Graph

Pre-built knowledge graph of 1,851 skills and agents with 472K edges.

## Files

| File | Size | Contents |
|------|------|----------|
| `wiki-graph.tar.gz` | 8.9 MB | **Full wiki** (159 MB uncompressed, 18,230 files): entity pages, concept pages, 844 converted micro-skill pipelines, knowledge graph, catalog, versions catalog |
| `communities.json` | 140 KB | 835 detected communities with labels and member lists |
| `graph-report.md` | 32 KB | God nodes (most connected skills/agents) + community summary |

### What's inside `wiki-graph.tar.gz`

- `entities/skills/` — 1,439 skill entity pages with YAML frontmatter
- `entities/agents/` — 412 agent entity pages
- `concepts/` — 34 auto-generated community concept pages
- `converted/` — 844 micro-skill pipelines (5-stage gated format)
- `graphify-out/graph.json` — full knowledge graph (1,851 nodes, 472K edges)
- `graphify-out/communities.json` — community detection results
- `catalog.md` — bulk listing of all skills and agents
- `versions-catalog.md` — dual-version skill tracking
- `SCHEMA.md`, `index.md`, `log.md` — wiki infrastructure

## Usage

### Extract the wiki

```bash
# Extract to ~/.claude/skill-wiki/ (or any directory)
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

This gives you:
- `entities/skills/*.md` — 1,439 skill entity pages with frontmatter (tags, description, use_count)
- `entities/agents/*.md` — 412 agent entity pages
- `concepts/*.md` — 34 auto-generated community pages grouping related skills
- `graphify-out/graph.json` — full networkx graph (load with `networkx.readwrite.node_link_graph`)
- `graphify-out/communities.json` — community detection results

### Load the graph in Python

```python
import json
import networkx as nx
from networkx.readwrite import node_link_graph

with open("~/.claude/skill-wiki/graphify-out/graph.json") as f:
    G = node_link_graph(json.load(f))

# 1,851 nodes, 472K edges
print(G.number_of_nodes(), G.number_of_edges())

# Find skills related to "fastapi"
fastapi = "skill:fastapi-pro"
neighbors = sorted(G.neighbors(fastapi), key=lambda n: G[fastapi][n]["weight"], reverse=True)[:10]
for n in neighbors:
    print(f"  {G.nodes[n]['label']} (weight={G[fastapi][n]['weight']})")
```

### Open in Obsidian

The extracted wiki is an Obsidian-compatible vault. Entity pages use `[[wikilinks]]` for cross-references. Open the directory in Obsidian and use the graph view to explore visually.

## Rebuild

After adding new skills or changing the wiki:

```bash
python src/wiki_batch_entities.py --all
python src/wiki_graphify.py
```

Then re-archive:

```bash
cd ~/.claude/skill-wiki
tar czf /path/to/ctx/graph/wiki-graph.tar.gz graphify-out/ entities/ concepts/ SCHEMA.md index.md
```
