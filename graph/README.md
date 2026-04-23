# Knowledge Graph

Pre-built knowledge graph of **13,041 nodes** (1,791 skills + 464 agents + 10,786 MCP servers) with **847,207 edges** across **15 communities**. Cross-type connectivity: skill↔skill 211K, agent↔skill 198K, MCP↔MCP 303K, MCP↔skill 60K, agent↔agent 63K, agent↔MCP 13K. Edges are blended from three signals — semantic cosine (default weight 0.70), explicit `tags:` overlap (0.15), and slug-token overlap (0.15). Rebuild with `ctx-wiki-graphify` (incremental by default).

> v0.5.x shipped a stale `graph.json` with 642K edges from a build path that no longer existed; the live rebuild silently produced only 861 edges because `DENSE_TAG_THRESHOLD=20` dropped every tag with more than 20 nodes. v0.6.0 fixed the threshold + added slug-token pseudo-tags → reproducible 454K-edge graph. v0.7 (this release) ingested 10,786 MCP servers from pulsemcp (93% enriched with github_url + stars), added sentence-embedding semantic edges with a configurable `build_floor=0.50` / `min_cosine=0.80` split (filter at query time without a rebuild), wired the alive-loop cumulative-threshold trigger so the suggestion arm actually fires in production, and shipped install/uninstall CLIs for all three entity types.

## Files

| File | Size | Contents |
|------|------|----------|
| `wiki-graph.tar.gz` | 22.5 MB | **Full wiki** — entity cards, 1,772 converted skill bodies, 430 mirrored agent bodies, 13K-node knowledge graph, concept pages, catalog |
| `communities.json` | 153 KB | 15 detected communities with labels + member lists |
| `graph-report.md` | 31 KB | God nodes (most connected skills / agents / MCPs) + community summary |
| `viz-overview.html` / `.png` | — | Plotly-rendered overview of the full graph |
| `viz-python.html` | — | Python-skills sub-view |
| `viz-security.html` / `.png` | — | Security-skills sub-view |
| `viz-ai-agents.html` | — | AI agents sub-view |
| `sample-top60.html` | — | Top-60-by-degree nodes, interactive |

### What's inside `wiki-graph.tar.gz`

- `entities/skills/` — **1,791** skill entity pages with YAML frontmatter
- `entities/agents/` — **464** agent entity pages
- `entities/mcp-servers/<shard>/` — **10,786** MCP entity pages (sharded by first-char to keep dirs scannable)
- `concepts/` — community concept pages
- `converted/` — **1,772** skill bodies ready for `ctx-skill-install` (956 pipeline-converted + 816 short-skill mirrors + 9 existing)
- `converted-agents/` — **430** agent bodies ready for `ctx-agent-install`
- `graphify-out/graph.json` — full knowledge graph (13,041 nodes, 847,207 edges)
- `graphify-out/communities.json` — community detection results (15 communities)
- `catalog.md` — bulk listing of skills / agents / MCPs
- `SCHEMA.md`, `index.md`, `log.md` — wiki infrastructure
- `.obsidian/` — Obsidian vault config, so the extracted tree opens as a graph directly in Obsidian

Excluded to keep the tarball under GitHub's 100MB file limit (all regenerable on first local run): `raw/` (pulsemcp HTML cache, ~700MB), `.embedding-cache/` (sentence-transformer vectors, ~66MB), `.ingest-checkpoint/`, `.enrich-checkpoint/`, `graphify-out/graph-delta.json`.

## Usage

### Extract the wiki

```bash
# Extract to ~/.claude/skill-wiki/
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

> **Windows / Git-Bash / MSYS:** pass `--force-local` so `tar` doesn't parse `c:` as a remote host: `tar --force-local xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/`.

This gives you:
- Every entity (skill / agent / MCP) browsable as a frontmatter-rich markdown card
- Installable content for every short and long skill + every agent (`ctx-skill-install`, `ctx-agent-install`)
- The full knowledge graph (`graphify-out/graph.json`) and community detection (`communities.json`)
- An Obsidian vault — open the extracted dir in Obsidian and the graph view renders directly

### Load the graph in Python

```python
import json
from pathlib import Path
from networkx.readwrite import node_link_graph

raw = json.loads(Path("~/.claude/skill-wiki/graphify-out/graph.json").expanduser().read_text())
# Auto-detect the NetworkX 2.x "links" vs 3.x "edges" schema.
# v0.6.x graphs used "links"; v0.7+ uses "edges".
edges_key = "links" if "links" in raw else "edges"
G = node_link_graph(raw, edges=edges_key)

# 13,041 nodes, 847,207 edges
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

After adding new skills, agents, or MCP entities (or changing the wiki):

```bash
python -m wiki_batch_entities --all     # regenerate entity cards
python -m wiki_graphify                 # incremental by default; --full to force
```

Then re-archive. The pre-commit hook does this automatically when the wiki drifts beyond the tarball; the manual command below mirrors its exclusions:

```bash
cd ~/.claude/skill-wiki
tar --force-local -czf /path/to/ctx/graph/wiki-graph.tar.gz \
    --exclude='.trash' \
    --exclude='__pycache__' \
    --exclude='./raw' \
    --exclude='./.embedding-cache' \
    --exclude='./.ingest-checkpoint' \
    --exclude='./.enrich-checkpoint' \
    --exclude='./graphify-out/graph-delta.json' \
    --exclude='./graphify-out/graph.pickle' \
    .
```

The exclusions keep the tarball under GitHub's 100MB file limit (raw/ alone is ~700MB of regenerable pulsemcp HTML). `--force-local` tells MSYS `tar` on Windows not to parse `c:` as a remote host. Non-Windows users can drop that flag.
