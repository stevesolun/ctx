# Knowledge Graph Artifacts

This directory ships the pre-built ctx LLM-wiki and knowledge graph.

Current snapshot:

- **79,958 graph nodes**
- **1,778,069 graph edges**
- **52 Louvain communities**
- **68,494 skill entity pages**; **67,024** have hydrated catalog bodies
- **467 agent pages**
- **10,790 MCP server pages**
- **207 harness pages**
- **67,024 hydrated imported `SKILL.md` bodies**
- Long skill bodies are kept behind the configured micro-skill line gate; the
  shipped tarball excludes raw `SKILL.md.original` backups.

The runtime recommendation paths use this graph in two ways:

- Development recommendations return skills, agents, and MCP servers only.
- Custom/API/local model onboarding recommends harnesses using the higher harness fit floor in `src/config.json`.

## Files

| File | Contents |
|---|---|
| `wiki-graph-runtime.tar.gz` | Fast install artifact used by default `ctx-init --graph`: `graphify-out/*`, the skill index, 207 harness pages, wiki index files, and Obsidian metadata needed for recommendations and harness dry-runs without expanding every entity page |
| `wiki-graph.tar.gz` | Full LLM-wiki: entity pages, converted skill bodies, mirrored agent bodies, concept pages, `graphify-out/graph.json`, `graph-delta.json`, export manifest, communities, skill indexes, SkillSpector stamps, and Obsidian metadata |
| `skillspector-audit.jsonl.gz` | Compact per-skill audit records produced by a ctx-run static `--no-llm` pass with [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector). This is not NVIDIA endorsement or certification. The same gzip is embedded in `wiki-graph.tar.gz` as `security/skillspector-audit.jsonl.gz`. |
| Skill catalog gzip | Compressed skill index for the 67,024 body-backed skill entries shipped in the wiki |
| `communities.json` | Current Louvain community export |
| `entity-overlays.jsonl` | Release overlay for first-class entities added after the base graph export; installed beside `graphify-out/graph.json` by `ctx-init --graph` |
| `graphify-out/dashboard-neighborhoods.sqlite3` inside both tarballs | Compact top-neighbor index used by `ctx-monitor` so `/api/graph/<slug>.json` does not cold-parse the 818 MiB graph JSON |
| `viz-overview.html` | Plotly overview of the graph |
| `viz-python.html` | Python-focused graph view |
| `viz-security.html` | Security-focused graph view |
| `viz-ai-agents.html` | AI-agent-focused graph view |
| `sample-top60.html` | Interactive top-degree sample |

Preview HTML files are generated from the shipped `graphify-out/graph.json`
and embed the graph export ID in `<meta name="ctx-graph-export-id">`. Static
PNG snapshots are intentionally not shipped because they can drift from the
current tarball without an executable freshness check.

## Runtime vs Full Wiki

`ctx-init --graph` installs `wiki-graph-runtime.tar.gz` by default. That is the
right path for recommendations and first-time installs because it avoids
expanding hundreds of thousands of markdown files while still shipping the
harness pages needed by `ctx-harness-install --dry-run`. Use
`ctx-init --graph --graph-install-mode full` or manual full extraction when you
want local wiki browsing, Obsidian, or the converted skill body tree.

## Modular Pack Model

The installed graph/wiki can be maintained as immutable base packs plus small
overlay packs. The merged view is intended to behave like the old single graph:
dashboard, search, recommendations, and harness setup still see one catalog,
but local adds, updates, and deletes no longer need to rewrite the full graph
and full wiki tarball.

Active pack layout:

```text
~/.claude/skill-wiki/
  graphify-out/packs/base-<export-id>/
  graphify-out/packs/overlay-<id>/
  wiki-packs/base-<export-id>/
  wiki-packs/overlay-<id>/
```

Overlay packs carry changed nodes, edges, wiki page upserts, and tombstones.
Periodic compaction merges the active base plus overlays into a new staged base
pack set. The full release artifact is still rebuilt explicitly for published
snapshots, but normal local updates should use overlays and the SQLite graph
store refresh path.

## What Is Inside `wiki-graph.tar.gz`

- `entities/skills/` - all skill entity pages
- `entities/agents/` - agent entity pages
- `entities/mcp-servers/<shard>/` - sharded MCP server entity pages
- `entities/harnesses/` - harness entity pages
- `converted/` - installable skill bodies
- `converted-agents/` - mirrored agent bodies
- `concepts/` - community concept pages
- `external-catalogs/` - machine-readable skill index, summary, and coverage metadata
- `security/skillspector-audit.jsonl.gz` - per-skill SkillSpector audit records
- `graphify-out/graph.json` - NetworkX node-link graph
- `graphify-out/graph-delta.json` - delta export for the latest graph generation
- `graphify-out/graph-export-manifest.json` - export manifest tying graph, delta, communities, and report to one generation
- `graphify-out/communities.json` - community export
- `SCHEMA.md`, `index.md`, `log.md`, `catalog.md` - wiki contract and indexes
- `.obsidian/` - vault metadata for local graph browsing

`SKILL.md.original` backups, transient `.lock` files, and `.ctx/` queue state
are not shipped. Local micro-skill conversion may keep `.original` files for
traceability, but the packaged tarball excludes them so users do not ingest raw
long bodies after conversion.

## Extract

Default runtime install:

```bash
ctx-init --graph
```

Full wiki extraction:

```bash
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

On Windows PowerShell, use the built-in `tar.exe` without `--force-local`:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skill-wiki"
tar -xzf graph\wiki-graph.tar.gz -C "$env:USERPROFILE\.claude\skill-wiki"
```

With Git Bash or MSYS tar, use `--force-local` only when the `-C` target is a
drive-letter path:

```bash
tar --force-local xzf graph/wiki-graph.tar.gz -C C:/Users/<you>/.claude/skill-wiki/
```

## Validate

```bash
python src/validate_graph_artifacts.py --deep
python src/update_repo_stats.py --check
```

For release-count validation, pin the exact snapshot numbers:

```bash
python src/validate_graph_artifacts.py --deep \
  --expected-nodes 79958 \
  --expected-edges 1778069 \
  --expected-semantic-edges 1088763 \
  --expected-harness-nodes 207 \
  --expected-skills-sh-nodes 67028 \
  --expected-skills-sh-catalog-entries 67024 \
  --expected-skills-sh-converted 67024 \
  --expected-skill-pages 68494 \
  --expected-agent-pages 467 \
  --expected-mcp-pages 10790 \
  --expected-harness-pages 207
```

Manual sanity checks:

```bash
tar -tzf graph/wiki-graph.tar.gz | grep 'graphify-out/graph.json'
tar -tzf graph/wiki-graph.tar.gz | grep 'external-catalogs/.*/catalog.json'
tar -tzf graph/wiki-graph.tar.gz | grep 'SKILL.md.original' && exit 1 || true
tar -tzf graph/wiki-graph.tar.gz | grep '\.lock$' && exit 1 || true
tar -tzf graph/wiki-graph.tar.gz | grep '^\./\.ctx/' && exit 1 || true
```

Windows PowerShell equivalent for the exclusion checks:

```powershell
tar -tzf graph/wiki-graph.tar.gz | Select-String 'SKILL.md.original'
tar -tzf graph/wiki-graph.tar.gz | Select-String '\.lock$'
tar -tzf graph/wiki-graph.tar.gz | Select-String '^\./\.ctx/'
```

The PowerShell commands should print nothing.

## Rebuild

After adding or updating skills, agents, MCP servers, or harnesses:

```bash
ctx-wiki-worker --wiki ~/.claude/skill-wiki --limit 1
ctx-scan-repo --repo . --recommend
```

The worker path is the fast local update path. It validates the queued entity
page, updates the wiki index, and attempts incremental ANN attach into
`graphify-out/entity-overlays.jsonl` when the semantic vector index exists. It
also queues the normal incremental graph export job, so a full rebuild remains
the reconciliation path for release artifacts.

If the worker reports that incremental attach was skipped because no vector
index exists, build the exact portable index:

```bash
ctx-wiki-graphify \
  --wiki-dir ~/.claude/skill-wiki \
  --incremental \
  --graph-only \
  --semantic-vector-index numpy-flat
```

Then drain pending queue work again:

```bash
ctx-wiki-worker --wiki ~/.claude/skill-wiki
```

Before promoting an ANN backend or changed thresholds, run the shadow gate:

```bash
ctx-incremental-shadow \
  --index-dir ~/.claude/skill-wiki/.embedding-cache/graph/vector-index \
  --graph ~/.claude/skill-wiki/graphify-out/graph.json \
  --sample-size 100 \
  --min-overlap 0.85
```

It reports precision/recall, top-k agreement, score deltas, and bad examples;
the release gate fails when recall at the largest requested top-k is below the
overlap floor.

To compact local graph/wiki overlays into a new coordinated base pack set:

```bash
ctx-pack-compact compact \
  --wiki-path ~/.claude/skill-wiki \
  --base-export-id <new-export-id> \
  --staging-dir /tmp/ctx-pack-stage \
  --json
```

Then validate and promote the staged graph and wiki packs together:

```bash
ctx-pack-compact promote \
  --wiki-path ~/.claude/skill-wiki \
  --staged-graph-packs-dir /tmp/ctx-pack-stage/graph-packs \
  --staged-wiki-packs-dir /tmp/ctx-pack-stage/wiki-packs \
  --json
```

Promotion requires the top-level `pack-compaction-manifest.json`, matching graph
and wiki export IDs, valid checksums, non-empty merged graph/wiki contents, and
entity consistency between known graph nodes and wiki pages. It refreshes
`graphify-out/graph-store.sqlite3` by default so dashboard graph APIs do not
fall back to cold-parsing `graph.json`. If you disable that refresh, rebuild and
validate the store explicitly:

```bash
ctx-graph-store build \
  --graph-dir ~/.claude/skill-wiki/graphify-out \
  --db ~/.claude/skill-wiki/graphify-out/graph-store.sqlite3

ctx-graph-store validate \
  --db ~/.claude/skill-wiki/graphify-out/graph-store.sqlite3
```

For release artifact rebuilds:

```bash
python scripts/graph_artifact_guard.py park
ctx-wiki-graphify
python src/validate_graph_artifacts.py --deep
python src/update_repo_stats.py --check
```

`park` sets Git's local `skip-worktree` bit for the heavyweight generated
archives: `graph/wiki-graph.tar.gz`, `graph/wiki-graph-runtime.tar.gz`, and
the compressed skill index. Keep them parked while graph/wiki generation,
validation, dashboard smoke, and stats checks are still in progress. This
prevents background Git integrations from repeatedly staging hundreds of
megabytes through the Git LFS clean filter. When the release candidate is final,
unpark and stage the artifacts exactly once:

```bash
python scripts/graph_artifact_guard.py unpark
git add graph/wiki-graph.tar.gz graph/wiki-graph-runtime.tar.gz \
  graph/skills-sh-catalog.json.gz graph/communities.json graph/entity-overlays.jsonl
python scripts/graph_artifact_guard.py prune
```

If a local Git integration gets interrupted while artifacts are dirty,
`python scripts/graph_artifact_guard.py prune` removes prunable local LFS cache
entries. It does not delete tracked graph files, rewrite history, or change the
remote LFS store. Repo-wide `git prune --expire=now` is intentionally opt-in via
`--include-git-prune` because it can discard unrelated dangling recovery objects.

For a bulk skill refresh, update the existing shipped tarball through the
release refresh path:

```bash
python src/import_skills_sh_catalog.py \
  --from-catalog <skill-catalog.json.gz> \
  --catalog-out <skill-catalog.json.gz> \
  --wiki-tar graph/wiki-graph.tar.gz \
  --update-wiki-tar
```

For a full local wiki repack from an existing artifact, use the packed-page
repacker. It writes `wiki-packs/base-<export-id>/`, removes expanded
skill/agent/MCP entity pages from the tar member list, and leaves harness pages
directly available for fast runtime setup:

```bash
python scripts/pack_full_wiki_tar.py \
  --source graph/wiki-graph.tar.gz \
  --target graph/wiki-graph.tar.gz.staged
python -c "from pathlib import Path; from ctx.core.wiki.artifact_promotion import promote_staged_artifact; from import_skills_sh_catalog import _validate_wiki_tarball_candidate; promote_staged_artifact(Path('graph/wiki-graph.tar.gz.staged'), Path('graph/wiki-graph.tar.gz'), validate=_validate_wiki_tarball_candidate)"
```

For a full local wiki repack from an expanded `~/.claude/skill-wiki` tree, write
the tarball to the sibling staged path, then promote that staged candidate after
validation:

```bash
cd ~/.claude/skill-wiki
tar --force-local -czf /path/to/ctx/graph/wiki-graph.tar.gz.staged \
    --exclude='.trash' \
    --exclude='__pycache__' \
    --exclude='./raw' \
    --exclude='./.embedding-cache' \
    --exclude='./.ingest-checkpoint' \
    --exclude='./.enrich-checkpoint' \
    --exclude='./.ctx' \
    --exclude='./graphify-out/graph.pickle' \
    --exclude='*.original' \
    --exclude='*.lock' \
    .
cd /path/to/ctx
python -c "from pathlib import Path; from ctx.core.wiki.artifact_promotion import promote_staged_artifact; from import_skills_sh_catalog import _validate_wiki_tarball_candidate; promote_staged_artifact(Path('graph/wiki-graph.tar.gz.staged'), Path('graph/wiki-graph.tar.gz'), validate=_validate_wiki_tarball_candidate)"
```

The repack command above is for Git Bash/MSYS. In Linux/macOS shells omit
`--force-local`; in PowerShell use `tar -czf` without `--force-local`.

Both flows validate candidates before atomic promotion. Each promoted artifact
gets a sibling `*.promotion.json` file with current, candidate, and `last_good`
hashes for review or rollback. The graph, delta, communities, report, and
export manifest are shipped together and carry the same export ID so validation
can reject mixed or partially refreshed graph generations. Raw `.original`
backups, transient `.lock` files, and `.ctx/` queue state must not appear in
the shipped tarball.

## Implementation Notes

The graph is built by `ctx.core.wiki.wiki_graphify` and the `ctx-wiki-graphify`
console script. Edges blend semantic similarity, explicit tag overlap,
slug-token overlap, source overlap, direct links, quality, usage, type affinity,
and graph-structure signals where available. The shipped default
`graph.min_edge_weight` is `0.03`, chosen from artifact calibration because it
keeps the current topology intact while recording the real shipped floor.

`nashsu/llm_wiki` was reviewed for design ideas around persistent wiki
contracts, queues, retrieval, and graph maintenance. ctx does not vendor that
code in this MIT repository.
