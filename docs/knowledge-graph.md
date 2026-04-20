# Knowledge graph

A pre-built weighted graph of every skill and agent in the ctx
ecosystem, shipped as `graph/wiki-graph.tar.gz` and queryable via
the `ctx-monitor` dashboard, the `resolve_graph` Python API, and
the on-disk JSON.

## What's in it

Authoritative numbers from the shipped tarball:

| | Count |
|---|---:|
| Nodes | **2,253** (1,789 skills + 464 agents) |
| Edges | **454,719** |
| Communities | **93** |
| Avg degree | **416.6** |
| Max degree | **1,152** |
| Skill ↔ agent cross-edges | **195,226** |
| Isolated nodes | 71 (entities with no tags and no slug-token overlap) |

## Install

Extract the tarball into your `~/.claude/skill-wiki/` to get a
ready-to-query graph plus every entity page, concept page, and
converted micro-skill pipeline:

```bash
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

The extracted tree also opens directly as an Obsidian vault — the
`.obsidian/` config ships inside the tarball — so you can use
Obsidian's native graph view if you prefer it to the web dashboard.

## How edges are built

Two sources of connectivity, combined at build time by
`src/wiki_graphify.py`:

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

After edges are built, `wiki_graphify` runs NetworkX's greedy
modularity community detection (`resolution=1.2`). The result is
93 communities ranging from single-member (isolated specialists) to
hundreds of members (broad clusters like `AI + Security + DevOps`).
Each community also gets an auto-generated `concepts/<community>.md`
wiki page summarizing its members and top shared tags.

## Querying the graph

### Via the dashboard

```bash
ctx-monitor serve              # http://127.0.0.1:8765
```

Then open `/graph?slug=<any-slug>` for a cytoscape neighborhood view,
or `/api/graph/<slug>.json?hops=1&limit=40` for the raw JSON. See the
[dashboard reference](dashboard.md) for the full route catalogue.

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

# 2,253 nodes, 454,719 edges
print(G.number_of_nodes(), G.number_of_edges())

# Find skills related to 'fastapi-pro' by edge weight
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

### Via the recommendation path

The graph is load-bearing on `resolve_skills.resolve()`: after the
static stack matrix matches a slug, the resolver seeds
`resolve_by_seeds(G, matched_slugs)` to walk 1-hop neighbors with
edge weight ≥ 1.5 and fold them into the recommendation with
`reason="graph neighbor of <slug> via shared tags [...]"`. This is
what makes a FastAPI repo surface `python-pro`, `test-automator`,
`async-python-patterns` etc. alongside the direct `fastapi-pro`
match.

## Rebuilding

After you add skills or edit entity pages:

```bash
ctx-wiki-graphify          # rebuild entity graph + communities
```

The pre-commit hook (`.githooks/pre-commit`) re-runs this
automatically when `skills/` or `agents/` are staged, and repacks
the tarball on disk so `README.md` numbers never drift.

## Why the edge count is 454K and not 642K

Earlier v0.5.x releases shipped a `graph.json` with 642K edges but
from a build path that no longer exists in the repo. When anyone
actually ran `wiki_graphify`, it silently produced 861 edges because
`DENSE_TAG_THRESHOLD` was 20 and every semantically-useful tag (on
300+ entities each) was being skipped. v0.6.0 fixed the threshold
to 500, added slug-token pseudo-tags, and taught `parse_frontmatter`
to read multi-line YAML lists — producing the 454K-edge graph that
ships today and is **reproducible from the wiki content** rather
than orphaned from a lost code path. See `CHANGELOG.md` v0.6.0 for
the full postmortem.
