# Knowledge graph

A pre-built weighted graph of skills, agents, MCP servers, and cataloged
harnesses in the ctx ecosystem, shipped as `graph/wiki-graph.tar.gz`.
The on-disk JSON and `resolve_graph` Python API are harness-aware;
`ctx-monitor` currently exposes skill/agent/MCP graph and wiki views
only. Dashboard harness exposure is not yet present.

## What's in it

Authoritative numbers from the shipped tarball. The curated-core snapshot
is **13,219 nodes** (1,969 curated skills + agents + MCP servers); harness
pages under `entities/harnesses/` are ingested into local rebuilds and
recommendation output when cataloged. The tarball also carries **90,846
remote-cataloged Skills.sh `skill` nodes**, matching skill pages under
`entities/skills/skills-sh-*.md`, and **67,519 sparse metadata edges** back
to curated entities. These records are first-class skills by graph type,
but remain metadata-only until their upstream SKILL.md bodies are hydrated
and reviewed.

| | Count |
|---|---:|
| Total nodes | **104,065** |
| Curated core nodes | **13,219** (1,969 skills + 464 agents + 10,786 MCP servers) |
| Remote-cataloged Skills.sh skill nodes | **90,846** (`skill`, `status=remote-cataloged`) |
| Total edges | **1,030,831** |
| Curated core edges | **963,312** |
| Skills.sh metadata edges | **67,519** |
| Communities | **22** (Louvain over the curated core) |
| Edge sources (overlap-deduped) | semantic 210,227 - tag 543,938 - token 300,345 |
| Cross-type edges (skill <-> agent) | ~222K |
| Cross-type edges (skill <-> MCP) | ~62K |
| Cross-type edges (agent <-> MCP) | ~13K |
| Skills.sh catalog | **90,846** observed entries (`external-catalogs/skills-sh/catalog.json` + `entities/skills/skills-sh-*.md`) |

## Install

Extract the tarball into your `~/.claude/skill-wiki/` to get a
ready-to-query graph plus every shipped skill/agent/MCP entity page,
cataloged harness pages when present, remote-cataloged Skills.sh skill
pages, concept pages, and converted micro-skill pipelines. The extracted
tree also includes the Skills.sh catalog JSON used by the shared
recommender:

```bash
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

The extracted tree also opens directly as an Obsidian vault — the
`.obsidian/` config ships inside the tarball — so you can use
Obsidian's native graph view if you prefer it to the web dashboard.

## How edges are built

Two sources of connectivity, combined at build time by the
`ctx-wiki-graphify` console script (`ctx.core.wiki.wiki_graphify`):

1. **Explicit frontmatter tags** — each entity page's YAML `tags:`
   list contributes edges between every pair of entities that share
   a tag. Popular tags capped at 500 nodes to avoid noise-floor
   "everything connects to everything" mega-buckets like `typescript`
   or `frontend`.
2. **Slug-token pseudo-tags** — each hyphenated slug contributes its
   tokens as implicit tags. `fastapi-pro` contributes `fastapi`;
   `python-patterns` contributes `python` and `patterns`. A stop-word
   filter drops generic tokens like `skill`, `agent`, `pro`, `expert`,
   `core` so they don't over-connect the graph.

Edge `weight` is the count of shared tags between two nodes. Edge
`shared_tags` is the list of the actual tags that produced the edge,
so any single edge is explainable (e.g. `cloud-architect ↔ terraform-
engineer` has `weight=6` with `shared_tags=[automation, azure,
security, _t:architect, ...]`).

## Communities

After edges are built, `wiki_graphify` runs NetworkX's Louvain
community detection (`resolution=1.2`, `seed=42` for determinism).
The result is **22 communities** ranging from single-member isolated
specialists to several thousand members in broad clusters like
`Community + Official + AI`. Each community also gets an auto-generated
`concepts/<community>.md` wiki page summarizing its members and top
shared tags.

The legacy CNM ("greedy modularity") algorithm is still available
behind `CTX_GRAPH_COMMUNITY=cnm` — it's deterministic but O(n²) on
dense graphs and hangs on the live 13K-node dataset (~50min run was
killed on 2026-04-27 inside the priority-queue siftup). Louvain is
the default because it finishes in seconds and produces equivalent
quality clusters for the recommendation use case.

## Querying the graph

### Via the dashboard

```bash
ctx-monitor serve              # http://127.0.0.1:8765
```

Then open `/graph?slug=<skill-agent-or-mcp-slug>` for a cytoscape
neighborhood view, or `/api/graph/<slug>.json?hops=1&limit=40` for the
dashboard-shaped JSON. `ctx-monitor` does not yet offer harness filters,
styling, or wiki routes; use the Python/API recommendation surfaces for
harness-aware graph results. See the [dashboard reference](dashboard.md)
for the full route catalogue.

### Via Python

```python
import json
from pathlib import Path
from networkx.readwrite import node_link_graph

raw = json.loads(
    Path("~/.claude/skill-wiki/graphify-out/graph.json").expanduser().read_text()
)
edges_key = "links" if "links" in raw else "edges"
G = node_link_graph(raw, edges=edges_key)

# 104,065 nodes, 1,030,831 edges
print(G.number_of_nodes(), G.number_of_edges())

# Find entities related to 'fastapi-pro' by edge weight
seed = "skill:fastapi-pro"
neighbors = sorted(
    G.neighbors(seed),
    key=lambda n: G[seed][n]["weight"],
    reverse=True,
)[:10]
for n in neighbors:
    shared = G[seed][n].get("shared_tags", [])
    print(f"  w={G[seed][n]['weight']:>2}  {G.nodes[n]['label']:<40}  {shared[:3]}")
```

The node-link JSON schema's edges key is auto-detected (legacy
NetworkX 2.x used `"links"`; current versions default to `"edges"`).
The helper `resolve_graph.load_graph()` does this for you.

### Via recommendation paths

The graph backs two recommendation paths:

- Free-text recommendation surfaces (`ctx.recommend_bundle`, MCP
  `ctx__recommend_bundle`, generic harness tools, and Claude Code hook
  suggestions) share `ctx.core.resolve.recommendations.recommend_by_tags`.
  That engine ranks skills, agents, MCP servers, and harnesses by
  slug-token matches, tag overlap, graph degree, and semantic-cache
  signals when available. Skills.sh results are `skill` nodes with
  `source_catalog=skills.sh`, `detail_url`, `install_command`, duplicate
  hints, and metadata-only quality/security signals. If an older
  extracted wiki has the Skills.sh catalog JSON but no graph nodes for
  those records, the same recommender falls back to the catalog file.
- Repository scans still start from stack detections and installed-entity
  availability. `resolve_skills.resolve()` maps detected languages,
  frameworks, infrastructure, and tools through the shared stack matrix, then
  uses the graph as an advisory augmentation source for additional installed
  skills, agents, and MCP server suggestions, plus catalog-only harness
  recommendations where the scan includes them.

This split is intentional: free-text query surfaces need identical ranking,
while scan resolution also has to respect local installation state and the
manifest cap.

## Rebuilding

After you add a skill, agent, MCP server, or harness entity page:

```bash
ctx-wiki-graphify          # rebuild entity graph + communities
```

The pre-commit hook (`.githooks/pre-commit`) re-runs this
automatically when `skills/` or `agents/` are staged, and repacks
the tarball on disk so `README.md` numbers never drift. Run
`ctx-wiki-graphify` directly for MCP server or harness catalog changes
if your hook config does not include those paths.

## Edge-count history

| Version | Edges | Note |
|---|---|---|
| v0.5.x | 642K (stale) / 861 (live) | Bundle had stale 642K; live rebuild silently produced 861 because `DENSE_TAG_THRESHOLD=20` dropped every popular tag. |
| v0.6.0 | 454,719 | Threshold raised to 500, multi-line YAML lists parsed, slug-token pseudo-tags added. |
| v0.7.x | 847,207 | Pulsemcp ingest added 10,786 MCP server nodes; sentence-embedding semantic edges added. |
| 2026-04-27 (this release) | **963,068** | +21 mattpocock skills, +156 designdotmd designs (+106,702 edges); patch-path bug fixed (graphify now forces full rebuild when prior graph has 0 semantic edges but current run computed semantic pairs); community detection switched from CNM to Louvain. |
| 2026-04-29 Skills.sh remote-cataloged pass | **1,030,831** | +90,846 first-class `skill` nodes, +90,846 skill pages, and +67,519 sparse duplicate/tag metadata edges to the curated graph. Full-body semantic edges are intentionally deferred to the hydration pass. |

The full audit history lives in `CHANGELOG.md`. The current build is
fully reproducible from the wiki content.

## Pre-ship gates

Two advisory gates run before the tarball is repackaged. Both produce
review reports and never auto-modify the catalog.

- **`ctx-dedup-check`** — flags entity pairs (skill ↔ skill, skill ↔
  agent, skill ↔ MCP, agent ↔ agent, agent ↔ MCP, MCP ↔ MCP) at or
  above 0.85 cosine similarity. Incremental: keeps a `dedup-state.json`
  next to the embedding cache, so follow-up runs only re-check pairs
  involving entities whose content changed. Allowlist support via
  `.dedup-allowlist.txt`. The current snapshot has 15,976 findings,
  most of which are within-MCP near-duplicates (multiple wrappers
  around the same upstream service).
- **`ctx-tag-backfill`** — finds skills/agents with empty `tags:`
  frontmatter and proposes a backfill drawn from slug tokens, body
  keywords, and the existing tag vocabulary. Report-only by default;
  pass `--apply` to write. Backfills are additive only.
