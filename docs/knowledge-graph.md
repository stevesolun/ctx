# Knowledge graph

A pre-built weighted graph of skills, agents, MCP servers, and
harnesses in the ctx ecosystem, shipped as `graph/wiki-graph.tar.gz`.
The on-disk JSON and `resolve_graph` Python API are harness-aware, including
plain-slug graph walks from `harness:<slug>` nodes. `ctx-monitor`
exposes skill/agent/MCP/harness wiki and graph views. Harness installation,
update, and uninstall are handled by `ctx-harness-install`; dashboard
load/unload POSTs deliberately reject harnesses and return the dry-run CLI
command to use instead. Quality scoring is exposed for sidecar-backed skills,
agents, and MCP servers.

## What's in it

Authoritative numbers from the shipped tarball. The curated-core snapshot
is **13,460 nodes** (1,998 curated skills + 467 agents + 10,788 MCP servers
+ 207 harnesses). Harness pages under `entities/harnesses/` are ingested into
local rebuilds and the separate harness recommendation path. The
tarball also carries **91,463 skill pages**; **89,465**
skill bodies are hydrated as installable `SKILL.md` files under
`converted/`; the **28,612** entries over the configured line
limit were converted to gated micro-skill orchestrators. Full original bodies
are used during graph rebuilds for semantic similarity, but
`SKILL.md.original` backups, transient `.lock` files, and `.ctx/` queue state
are omitted from the shipped tarball.

| | Count |
|---|---:|
| Total nodes | **102,925** |
| Curated core nodes | **13,460** (1,998 skills + 467 agents + 10,788 MCP servers + 207 harnesses) |
| Body-backed skill nodes | **89,465** hydrated installable skill entries |
| Total edges | **2,913,930** |
| Hydrated skill incident edges | **2,605,721** |
| Hydrated skill semantic incident edges | **1,500,648** |
| Communities | **52** (Louvain) |
| Edge sources (overlap-deduped) | semantic 1,683,163 - tag 897,754 - token 433,245 |
| Cross-type edges (skill <-> agent) | ~67K |
| Cross-type edges (skill <-> MCP) | ~41K |
| Cross-type edges (agent <-> MCP) | ~223 |
| Harness edges | **6,571** |
| Shipped skill index | **89,465** observed body-backed skill entries |

## Install

Use `ctx-init --graph` to install the fast runtime graph. Source checkouts use
`graph/wiki-graph-runtime.tar.gz`; pip installs download the matching GitHub
release asset for the installed package version. This installs
`graphify-out/*`, the skill index used by recommendations, and
the harness pages used by `ctx-harness-install`:

```bash
ctx-init --graph
```

To expand every shipped skill/agent/MCP entity page, harness page,
skill page, concept page, converted micro-skill pipeline,
and Obsidian vault metadata, request the full wiki artifact explicitly:

```bash
ctx-init --graph --graph-install-mode full
```

Manual extraction is still supported for offline/source installs. Extract the
full tarball into your `~/.claude/skill-wiki/` when you want local markdown
wiki browsing:

```bash
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

On Windows PowerShell, create the target and use the built-in `tar.exe`
without `--force-local`:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skill-wiki" | Out-Null
tar -xzf graph\wiki-graph.tar.gz -C "$env:USERPROFILE\.claude\skill-wiki"
```

The extracted tree also opens directly as an Obsidian vault — the
`.obsidian/` config ships inside the tarball — so you can use
Obsidian's native graph view if you prefer it to the web dashboard.

## How edges are built

Edges are built and explained by the `ctx-wiki-graphify` console script
(`ctx.core.wiki.wiki_graphify`). A pair must first have at least one base
signal:

1. **Semantic cosine** — when the embedding backend is available, entity
   text is embedded and semantic neighbors above the configured build floor
   contribute weighted edges.
2. **Explicit frontmatter tags** — each entity page's YAML `tags:`
   list contributes edges between every pair of entities that share
   a tag. Popular tags capped at 500 nodes to avoid noise-floor
   "everything connects to everything" mega-buckets like `typescript`
   or `frontend`.
3. **Slug-token pseudo-tags** — each hyphenated slug contributes its
   tokens as implicit tags. `fastapi-pro` contributes `fastapi`;
   `python-patterns` contributes `python` and `patterns`. A stop-word
   filter drops generic tokens like `skill`, `agent`, `pro`, `expert`,
   `core` so they don't over-connect the graph.
4. **Source overlap** — pages with the same high-specificity source URL,
   repository URL, homepage, detail URL, or package URL can connect even
   when their tags differ. Dense source buckets are skipped.
5. **Direct wikilinks** — explicit entity links such as
   `[[entities/agents/code-reviewer]]` create a direct graph edge.

Edge `weight` is the final blended strength. Semantic, tag, and token
weights form the base blend from `config.json`; source overlap and direct
links add configured boosts. Existing edges can also receive explainable
ranking boosts from Adamic-Adar shared-neighbor structure, type affinity,
usage telemetry, and quality scores. Those boost-only signals do not create
edges by themselves. The shipped default `graph.min_edge_weight` is `0.03`;
calibration against the 2026-05 shipped graph showed this is the highest
floor with zero edge loss, while `0.05` would remove roughly 29.7% of edges.

Edge metadata keeps the ingredients explainable: `semantic_sim`,
`shared_tags`, `shared_tokens`, `shared_sources`, `direct_link`,
`adamic_adar`, `type_affinity`, `usage_score`, `quality_score`,
`edge_reasons`, and `score_components`. Hydrated skill records use their
full source bodies during graph rebuilds, so long converted entries keep
full-body similarity even though the shipped installable `SKILL.md` files are
short gated loaders. The raw `SKILL.md.original` backups are build inputs, not
tarball members.

## Communities

After edges are built, `wiki_graphify` runs NetworkX's Louvain
community detection (`resolution=1.2`, `seed=42` for determinism).
The result is **52 communities** ranging from single-member isolated
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

Then open `/graph?slug=<entity-slug>&type=<entity-type>` for a
cytoscape neighborhood view, or
`/api/graph/<slug>.json?type=<entity-type>&hops=1&limit=40` for the
dashboard-shaped JSON. The `type` query is optional for unique slugs and
recommended for duplicate slugs such as `langgraph`. See the
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

# 102,925 nodes, 2,913,930 edges
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

- Execution recommendation surfaces (`ctx.recommend_bundle`, MCP
  `ctx__recommend_bundle`, generic harness tools, Claude Code hook
  suggestions, and repo-scan advisory output) share
  `ctx.core.resolve.recommendations.recommend_by_tags` for skills,
  agents, and MCP servers. That engine ranks candidates by
  slug-token matches, tag overlap, graph degree, and semantic-cache
  signals when available. Imported skill results are normal `skill` nodes with
  detail URLs, install commands, duplicate
  hints, gated micro-skill loaders when over the line threshold, and
  quality/security metadata. If an older
  extracted wiki has the skill index JSON but no graph nodes for
  those records, the same recommender falls back to the index file.
- Harness recommendations are a separate path for custom/API/local
  model onboarding (`ctx-init --model-mode custom ...`) and
  `ctx-harness-install`. They use the same graph filtered to
  `harness` nodes and the higher harness match floor from `config.json`.
- Repository scans still start from stack detections, then turn that profile
  into the same tag/query bundle used by the execution recommender. If a
  shipped graph is unavailable, scan output falls back to the legacy installed
  skill resolver so a plain profile scan remains useful. Harnesses are
  intentionally not emitted from repo scans or Claude Code hook bundles.

This split is intentional: execution surfaces need identical ranking and a
small top-K, while harness choice changes the model runtime itself and belongs
in an explicit onboarding/install flow.

### LLM-wiki design references

ctx follows Karpathy's LLM-wiki pattern. We also reviewed
[`nashsu/llm_wiki`](https://github.com/nashsu/llm_wiki) as a design reference
for source traceability, persistent ingest queues, graph insights, and
budgeted token/vector/graph retrieval. That repository is GPLv3, while ctx is
MIT, so ctx can use those ideas as product inspiration but must not copy or
vendor its code or assets.

## Rebuilding

After you add a skill, agent, MCP server, or harness entity page:

```bash
ctx-wiki-worker --wiki ~/.claude/skill-wiki --limit 1
```

The `entity-upsert` worker path validates the queued page hash, updates the
wiki index, and, when a persisted semantic vector index exists, runs a
best-effort ANN attach into `graphify-out/entity-overlays.jsonl`. That overlay
lets the runtime resolver connect a new or updated entity to existing graph
neighbors without recomputing global all-pairs similarity. The worker still
queues the normal incremental `graph-export` job, and the entity markdown page
remains the source of truth.

For manual review or debugging:

```bash
ctx-incremental-attach calibrate \
  --graph ~/.claude/skill-wiki/graphify-out/graph.json

ctx-incremental-attach attach \
  --index-dir ~/.claude/skill-wiki/.embedding-cache/graph/vector-index \
  --overlay ~/.claude/skill-wiki/graphify-out/entity-overlays.jsonl \
  --node-id skill:fastapi-review \
  --type skill \
  --label fastapi-review \
  --text-file ~/.claude/skill-wiki/entities/skills/fastapi-review.md \
  --dry-run
```

Shadow-gate a persisted index before trusting a new ANN backend, changed
thresholds, or a large attach workflow:

```bash
ctx-incremental-shadow \
  --index-dir ~/.claude/skill-wiki/.embedding-cache/graph/vector-index \
  --graph ~/.claude/skill-wiki/graphify-out/graph.json \
  --sample-size 100 \
  --min-overlap 0.85
```

The shadow command pretends sampled existing nodes are new, compares the
incremental attach result to batch graph semantic neighbors, and reports
precision, recall, top-5/top-10/top-20 agreement, score deltas, and bad
examples. A failing gate means either tune thresholds or use a full graph
rebuild before shipping.

If the vector index is missing, rebuild it without repacking artifacts:

```bash
ctx-wiki-graphify \
  --wiki-dir ~/.claude/skill-wiki \
  --incremental \
  --graph-only \
  --semantic-vector-index numpy-flat
```

Then drain pending entity-upsert work with `ctx-wiki-worker --wiki
~/.claude/skill-wiki`. This is the current repair path for "build index" and
"attach pending" without adding another command surface.

Before publishing graph artifacts, run the full rebuild/export path:

```bash
ctx-wiki-graphify          # rebuild entity graph + communities
```

The pre-commit hook (`.githooks/pre-commit`) does **not** rebuild or
repack graph artifacts from `~/.claude/skill-wiki/`; that local wiki can
contain private entities. It refreshes cheap README stats when relevant
checked-in files are staged and warns when entity sources changed. Run
`ctx-wiki-graphify`, validate, repack, and stage the artifacts explicitly
for skill, agent, MCP server, or harness releases.

Graphify exports stage and validate each generated artifact before atomic
promotion. `graph.json`, `graph-delta.json`, `communities.json`,
`graph-report.md`, and `graph-export-manifest.json` each get a sibling
`*.promotion.json` file with candidate, current, and `last_good` hashes plus
rollback metadata. The manifest is promoted last, so a crash between artifact
promotion and manifest promotion is detected as an incomplete export and the
next run rebuilds instead of trusting mixed graph files.

## Edge-count history

| Version | Edges | Note |
|---|---|---|
| v0.5.x | 642K (stale) / 861 (live) | Bundle had stale 642K; live rebuild silently produced 861 because `DENSE_TAG_THRESHOLD=20` dropped every popular tag. |
| v0.6.0 | 454,719 | Threshold raised to 500, multi-line YAML lists parsed, slug-token pseudo-tags added. |
| v0.7.x | 847,207 | Pulsemcp ingest added 10,786 MCP server nodes; sentence-embedding semantic edges added. |
| 2026-04-27 graph rebuild pass | **963,068** | +21 mattpocock skills, +156 designdotmd designs (+106,702 edges); patch-path bug fixed (graphify now forces full rebuild when prior graph has 0 semantic edges but current run computed semantic pairs); community detection switched from CNM to Louvain. |
| 2026-04-29 bulk skill import pass | **1,030,831** | +90,846 first-class `skill` nodes, +90,846 skill pages, and +67,519 sparse duplicate/tag metadata edges to the core graph. Full-body semantic edges are intentionally deferred to the hydration pass. |
| 2026-04-29 text-to-cad harness pass | **1,031,011** | +1 first-class `harness` node, +1 harness page, and +224 explainable harness edges, including 44 skill edges. |
| 2026-04-29 harness inventory pass | **1,033,253** | +12 first-class `harness` nodes/pages for LangGraph, CrewAI, AutoGen, Google ADK, Semantic Kernel, Mastra, Pydantic AI, Haystack, OpenAI Agents SDK, LiteLLM, Langfuse, and AgentOps; harness incident edges now total 2,700. |
| 2026-04-30 skill semantic hydration pass | **2,881,027** | +full-body semantic edges for hydrated skill records; semantic top-K became the dominant large-scale signal. |
| 2026-05-01 micro-skill pass | **2,960,189** | Enforced the <=180-line loader threshold across 89,461 hydrated `SKILL.md` files, converted 28,611 long bodies into gated micro-skill orchestrators, used full originals for semantic graphing, excluded `.original` backups from the shipped tarball, bounded generated stage/reference files to 40 lines, and rebuilt the graph. |
| 2026-05-02 GitNexus MCP pass | **2,960,215** | Added GitNexus as an MCP server entity with 26 cross-type edges to related skill pages and architecture/refactoring agents; semantic edge count unchanged. |
| 2026-05-04 v0.7.3 artifact refresh | **2,960,215** | Hydrated one recoverable command-injection-testing body, raising hydrated `SKILL.md` files to 89,463; generated micro-skill markdown now defangs high-risk command-injection payloads before packaging. Graph topology unchanged. |
| 2026-05-04 body-backed skill prune | **2,900,834** | Removed 1,383 skill records that had no packaged `SKILL.md` body and no parseable prose body. Remaining skill records, graph nodes, entity pages, and converted `SKILL.md` bodies are all **89,463**. |
| 2026-05-05 artifact hygiene refresh | **2,900,834** | Repacked `graph/wiki-graph.tar.gz` to remove transient `.lock` files from the shipped LLM-wiki. Topology unchanged. |
| 2026-05-10 v1.0.0 release prep | **2,900,834** | Refreshed shipped HTML previews from the current export, validated their export IDs in CI, and removed stale PNG previews. Topology unchanged. |
| 2026-05-12 book-to-skill + queue hygiene | **2,900,910** | Added `book-to-skill` as a skill entity (+1 node, +76 edges), restored a missing converted skill body, and repacked `graph/wiki-graph.tar.gz` to omit `.ctx/` queue state. Tar members: **598,154**. |
| 2026-05-13 overlay pass | **2,911,220** | Added AGENTS.md, lat.md, OptiLLM, Matt Pocock refresh deltas, and Julius caveman entities through the safe overlay path (+21 nodes, +10,310 edges) while preserving the saturated skill topology. Tar members at that pass: **598,192**. |
| 2026-05-14 Matt Pocock upstream refresh | **2,911,126** | Pinned `mattpocock/skills` to `e74f0061bb67222181640effa98c675bdb2fdaa7`, removed three stale legacy alias skill pages/nodes (`mattpocock-domain-model`, `mattpocock-github-triage`, `mattpocock-triage-issue`), refreshed `mattpocock-grill-with-docs`, and pruned 94 incident edges plus stale wiki references. Current tar members: **598,189**. |
| 2026-05-14 Mirage + CodeGraph first-class wiki pass | **2,911,162** | Added Mirage as a shipped harness wiki/runtime page, added the CodeGraph MCP markdown page and `codegraph-agentic-codebase-analysis` skill page/body to the full LLM-wiki, added the compact dashboard neighborhood index, regenerated graph preview HTML from the current export, and refreshed exact validation counts. Current tar members: **598,193**. |
| 2026-05-18 repo refresh | **2,911,575** | Refreshed `addyosmani/agent-skills` and `bytedance/deer-flow` from current upstream SKILL.md bodies, added Addy Osmani's `doubt-driven-development` and `interview-me` skills, replaced DeerFlow's stale `vercel-deploy` entry with `vercel-deploy-claimable`, retained `Imbad0202/academic-research-skills` as existing non-commercial upstream content, and added DeerFlow as a first-class harness page/runtime page. Current tar members: **598,596**. |
| 2026-05-26 upstream repo ingest | **2,913,930** | Added `browsing-skills/browsing-skills` as a root browser skill plus 11 site-specific browser skills, refreshed `awesome-skills/code-review-skill` into `code-review-excellence` with its reference pack, refreshed `book-to-skill` through the micro-skill gate, added Presenton as both a harness and MCP server, refreshed DeerFlow's harness page, imported 190 harness catalog pages from `Picrew/awesome-agent-harness`, regenerated dashboard sqlite and preview HTML from the current export, and refreshed exact validation counts. Current tar members: **598,951**. |
| 2026-05-27 Nango catalog/tooling overlay | **2,913,960** | Added `nango-integration-catalog` as a skill page/body, added Nango hosted action-function MCP and Nango docs MCP as first-class MCP pages, connected them to API/auth/tool-calling skills, common SaaS MCPs, and agent harnesses, regenerated dashboard sqlite and preview HTML, and refreshed exact validation counts. Current tar members: **598,955**. |

The full audit history lives in `CHANGELOG.md`. The current build is
fully reproducible from the wiki content.

## Pre-ship gates

Two advisory gates run before the tarball is repackaged. Both produce
review reports and never auto-modify the inventory.

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
