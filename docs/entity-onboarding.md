# Entity Onboarding

ctx treats skills, agents, MCP servers, and harnesses as wiki entities that can
be indexed, linked in the knowledge graph, and recommended from the same
surface. The important distinction is install behavior:

- Skills and agents are local Claude Code assets.
- MCP servers are recorded first, then installed only when the user opts in.
- Harnesses are recorded first. A harness describes the machinery around the
  model: runtime, tools, access boundaries, memory, verification, and approval
  policy. Adding one never executes upstream setup commands.

After adding any entity, drain the durable wiki queue when you want the local
runtime graph to see it immediately:

```bash
python -m ctx.core.wiki.wiki_queue_worker --wiki ~/.claude/skill-wiki --limit 1
ctx-scan-repo --repo . --recommend
```

If a persisted semantic vector index exists, that worker pass also runs a
best-effort incremental attach into `graphify-out/entity-overlays.jsonl` and,
when active graph packs exist, writes a small graph overlay pack for the
changed entity. If the vector index is missing, the worker can still write a
node-only graph overlay pack and refresh `graph-store.sqlite3`, so local
dashboard/search/recommendation reads see the merged base+overlay graph without
a full all-pairs semantic rebuild. Wiki readers also merge active pack pages
with safe local-only entity files, while pack pages and tombstones shadow local
files at the same relative path. Only legacy installs without active graph
packs fall back to the normal incremental graph export job.

## Updating the Graph and LLM Wiki

Use this sequence for every accepted skill, agent, MCP server, or harness
change. The graph and LLM-wiki are shippable artifacts, not scratch output, so
the update is treated like a release step.

1. Add or update the entity through the matching command:
   `python -m skill_add`, `python -m agent_add`, `python -m mcp_add`, or `python -m harness_add`.
2. If the entity already exists, read the update review. It lists changed
   fields, likely benefits, regressions, and security findings. Do not pass
   `--update-existing` until those findings are acceptable.
3. Run the security/cyber check below.
4. Park heavyweight graph artifacts locally before rebuilds:
   `python scripts/graph_artifact_guard.py park`. This keeps background Git
   integrations from repeatedly LFS-cleaning the full wiki tarball while it is
   still changing.
5. Drain the wiki queue for local runtime use:
   `python -m ctx.core.wiki.wiki_queue_worker --wiki ~/.claude/skill-wiki --limit 1`. This updates the
   wiki index, writes wiki overlay packs, attempts incremental ANN graph attach
   when a vector index exists, writes graph overlay packs when active packs are
   present, and queues a graph-store refresh so local reads see the merged
   graph/wiki view.
6. Rebuild the curated wiki graph with `python -m ctx.core.wiki.wiki_graphify` before shipping
   release artifacts or when you need a full graph/export reconciliation.
7. Repack `graph/wiki-graph.tar.gz` through the artifact promotion path:
   write a staged tarball, validate it, atomically promote it, and keep the
   generated `*.promotion.json` metadata with the previous/current hashes.
   Never commit local review reports or raw caches.
8. Refresh the bulk skill index when shipping large skill updates.
   This adds first-class `skill` nodes, skill pages under `entities/skills/`, install
   commands, duplicate hints, and metadata-only quality/security signals:

   ```bash
   python src/import_skills_sh_catalog.py --from-api-union <raw.json> \
     --catalog-out graph/skills-sh-catalog.json.gz \
     --wiki-tar graph/wiki-graph.tar.gz \
     --update-wiki-tar
   ```
9. Refresh published counts with `python src/update_repo_stats.py`.
10. Verify the changed entity can be recommended through
   `ctx-scan-repo --repo . --recommend` or `ctx__recommend_bundle`.
11. Unpark and stage the graph artifacts once the release candidate is final:
    `python scripts/graph_artifact_guard.py unpark`, then `git add` the graph
    artifacts intentionally. Run `python scripts/graph_artifact_guard.py prune`
    after interrupted Git/LFS runs or after release staging to clean prunable
    local LFS cache entries. Add `--include-git-prune` only when you explicitly
    want repo-wide dangling Git objects removed too.

The durable wiki worker drains `entity-upsert`, `graph-export`,
`skill-index-refresh`, `tar-refresh`, and `artifact-promotion` jobs. Use
`python -m ctx.core.wiki.wiki_queue_worker --wiki ~/.claude/skill-wiki --limit 1` for a controlled
single-job drain, or omit `--limit` to drain the ready queue.
Artifact-promotion jobs must target known `<wiki>/graphify-out/` graph outputs
or allowlisted repo `graph/` release artifacts, and their staged path must be
the target sibling named `<target>.staged`. The worker rejects staged/target
symlinks and selects the JSON/JSONL/gzip/tar validator from the target file
type before promotion.

For a manual attach dry-run against an existing vector index:

```bash
python -m ctx.core.graph.incremental_attach attach \
  --index-dir ~/.claude/skill-wiki/.embedding-cache/graph/vector-index \
  --overlay ~/.claude/skill-wiki/graphify-out/entity-overlays.jsonl \
  --node-id skill:fastapi-review \
  --type skill \
  --label fastapi-review \
  --text-file ~/.claude/skill-wiki/entities/skills/fastapi-review.md \
  --dry-run
```

Use `python -m ctx.core.graph.incremental_attach calibrate --graph ~/.claude/skill-wiki/graphify-out/graph.json`
to inspect the current graph's semantic and degree distributions before
changing attach thresholds.

Validate the attach quality before relying on a new ANN backend or changed
threshold:

```bash
python -m ctx.core.graph.incremental_shadow \
  --index-dir ~/.claude/skill-wiki/.embedding-cache/graph/vector-index \
  --graph ~/.claude/skill-wiki/graphify-out/graph.json \
  --sample-size 100 \
  --min-overlap 0.85
```

The shadow gate pretends sampled existing nodes are new, compares incremental
attach neighbors to batch graph semantic neighbors, reports precision/recall
for top 5/10/20, score deltas, and bad examples, then exits non-zero if
recall at the largest top-k is below the overlap floor.

## Repair Incremental Attach

If the worker says `incremental attach skipped (no vector index)`, build the
persisted semantic index once:

```bash
python -m ctx.core.wiki.wiki_graphify \
  --wiki-dir ~/.claude/skill-wiki \
  --incremental \
  --graph-only \
  --semantic-vector-index numpy-flat
```

`numpy-flat` is exact and portable. `--semantic-vector-index auto` keeps the
portable exact backend at this graph size and can switch to the optional ANN
backend only above the configured node threshold. `hnswlib` is optional and
should be shadow-gated before release use.

Then process pending entity updates:

```bash
python -m ctx.core.wiki.wiki_queue_worker --wiki ~/.claude/skill-wiki
```

That is the supported "attach pending" flow today: the queue is durable, so
failed or skipped entity-upsert jobs remain visible to the worker and can be
retried after the index exists. Use manual `python -m ctx.core.graph.incremental_attach attach
--dry-run` for one-off debugging, not as the normal bulk path.

## Security and Cyber Check

Run this before applying `--update-existing`, before installing a harness with
approved commands, and before shipping a refreshed graph tarball.

- Inspect changed entity markdown and frontmatter for shell commands, setup
  commands, install commands, URLs, requested permissions, and model/provider
  access.
- Treat these as manual-review blockers: `curl | sh`, `wget | bash`,
  `Invoke-Expression`, broad `rm -rf`, `git reset --hard`, `chmod 777`, secret
  upload, disabled auth/TLS/sandboxing/audit/tests, or unpinned package sources.
- For MCP and harness updates, check network access, filesystem scope, auth
  material, command transports, and whether setup or verify commands execute
  remote code.
- Prefer dry-run first: `python -m harness_install <slug> --dry-run` and
  `python -m harness_install <slug> --update --dry-run`.
- If a candidate is useful but risky, document the safer install path or keep it
  as metadata instead of shipping it as an installed skill.

## Updating an Existing Entity

The add commands are non-destructive by default when the target skill, agent,
MCP server, or harness already exists. The first add attempt prints an update
review instead of replacing files. That review lists changed fields, expected
benefits, possible regressions, security findings, and a recommendation.

Use this flow for every entity type:

1. Run the normal add command.
2. If ctx prints `Existing <type> already exists`, read the benefits and risks.
3. Keep the current entity by doing nothing, or re-run with `--skip-existing`
   in batch jobs where you do not want reviews.
4. Apply the replacement only after review with `--update-existing`.
5. Drain the queue with `python -m ctx.core.wiki.wiki_queue_worker --wiki ~/.claude/skill-wiki --limit 1`
   for immediate local recommendation use, or rebuild with `python -m ctx.core.wiki.wiki_graphify`
   when the update should be reconciled into shipped graph artifacts.

Examples:

```bash
python -m skill_add --skill-path ./SKILL.md --name fastapi-review
python -m skill_add --skill-path ./SKILL.md --name fastapi-review --update-existing

python -m agent_add --agent-path ./code-reviewer.md --name code-reviewer
python -m agent_add --agent-path ./code-reviewer.md --name code-reviewer --update-existing

python -m mcp_add --from-json ./github-mcp.json
python -m mcp_fetch --source pulsemcp --limit 50 | python -m mcp_add --from-stdin
python -m mcp_add --from-json ./github-mcp.json --update-existing

python -m harness_add --from-json ./text-to-cad-harness.json
python -m harness_add --from-json ./text-to-cad-harness.json --update-existing
```

`python -m harness_install --update` is different: it refreshes an installed harness
checkout under `~/.claude/harnesses/<slug>`. Catalog entity replacement uses
`python -m harness_add --update-existing`.

## Removing or Retiring an Entity

Removal has three separate meanings. First decide which one you need.

- **Catalog removal** stops ctx from showing the entity in the wiki, graph, and
  recommendations. Use the dashboard: `python -m ctx_monitor serve`, open **Manage**,
  search for the slug and type, then choose **Delete selected**. If the entity
  is currently loaded, Manage first attempts to unload it; if unloading fails,
  deletion stops and the wiki page is kept. Otherwise, Manage unlinks the page
  and queues an `entity-upsert` delete plus graph refresh. When active wiki
  packs exist, draining the worker records a wiki tombstone so the removed
  relative path stays hidden even if it still exists in a base pack or stale
  local file.
- **Runtime unload** removes a currently loaded entity from the live manifest
  while keeping its catalog entry. Use the dashboard **Loaded** page. MCP
  unloads call the Claude MCP removal path when available; skill and agent
  unloads remove the manifest row.
- **Installed-file removal** is type-specific. Use
  `python -m harness_install <slug> --uninstall` for harness checkouts,
  `python -m ctx.adapters.claude_code.install.mcp_install uninstall <slug>` for installed MCPs, and `python -m ctx_lifecycle archive`
  then `python -m ctx_lifecycle purge` for stale local skills that should be deleted
  after the configured grace period.

After deleting an entity page, drain or rebuild before trusting recommendation
results:

```bash
python -m ctx.core.wiki.wiki_queue_worker --wiki ~/.claude/skill-wiki --limit 1
ctx-scan-repo --repo . --recommend
```

If you need an auditable manual flow without the browser, use the dashboard
local API only from loopback with the per-process monitor token printed into
the served page. The API route is `POST /api/entity/delete` with
`{"slug": "...", "entity_type": "skill|agent|mcp-server|harness"}`.

## Add a Skill

Use this when you have a local `SKILL.md` that should be installed under
`~/.claude/skills/<name>/SKILL.md` and mirrored into the wiki.

```bash
python -m skill_add \
  --skill-path ./SKILL.md \
  --name fastapi-review
```

What happens:

1. The name is validated.
2. NVIDIA SkillSpector runs in static `--no-llm` mode by default; the add
   fails unless the scan passes. Use `--no-security-scan` only for a deliberate
   local exception.
3. Intake checks run against the markdown.
4. The skill is copied into `~/.claude/skills/`.
5. A wiki page is created under `entities/skills/`.
6. The wiki index and log are updated.

## Add an Agent

Use this when you have a local Claude Code agent markdown file.

```bash
python -m agent_add \
  --agent-path ./code-reviewer.md \
  --name code-reviewer
```

Batch-add every top-level `.md` file in a directory:

```bash
python -m agent_add --scan-dir ./agents --skip-existing
```

Agents are copied into `~/.claude/agents/` and mirrored into
`entities/agents/`. Re-run `python -m ctx.core.wiki.wiki_graphify` after adding agents if you want
graph recommendations to include them.

## Add an MCP Server

Use this when you want the MCP server available as a recommendation before
installing it into a host.

Create `github-mcp.json`:

```json
{
  "name": "GitHub MCP",
  "slug": "github-mcp",
  "description": "MCP server for GitHub repository and issue workflows.",
  "github_url": "https://github.com/modelcontextprotocol/servers",
  "sources": ["manual"],
  "tags": ["github", "automation", "repository"],
  "transports": ["stdio"]
}
```

Add it:

```bash
python -m mcp_add --from-json ./github-mcp.json
```

MCP pages live under `entities/mcp-servers/<shard>/<slug>.md`. The add command
accepts a single JSON object with `--from-json` or streams JSONL records from
`--from-jsonl`/`--from-stdin` without preloading the whole batch. It detects
existing pages by slug and, when possible, canonical GitHub URL. If a match
exists, ctx prints the update review and skips replacement unless
`--update-existing` is passed.

Runtime install is separate from catalog add. Use
`python -m ctx.adapters.claude_code.install.mcp_install <slug> --dry-run` to inspect the Claude MCP command without
mutating local state. If the server is already installed, dry-run reports
`skipped-existing` and does not reconcile `skill-manifest.json`; run without
`--dry-run` to refresh the manifest record, or add `--force` when you need to
reinstall the Claude MCP entry. Inline secret-looking arguments are rejected; use
environment variables, secret-file/path flags, or `--cmd-json` placeholders
instead of literal tokens.

## Add a Harness

Use this when a repo provides the runtime around a model rather than just a
tool. Harness examples include coding-agent loops, CAD-generation runtimes,
browser-automation runners, evaluation loops, and local-model workbenches.

Example: add `earthtojake/text-to-cad` as a harness recommendation.

```bash
python -m harness_add \
  --repo https://github.com/earthtojake/text-to-cad \
  --name "Text to CAD" \
  --description "Harness for turning text prompts into CAD artifacts." \
  --tag cad --tag 3d --tag automation \
  --model-provider openai \
  --runtime python \
  --capability "Generate CAD artifacts from natural language" \
  --setup-command "pip install -e ." \
  --verify-command "pytest"
```

Or load one JSON record:

```json
{
  "repo_url": "https://github.com/earthtojake/text-to-cad",
  "name": "Text to CAD",
  "description": "Harness for turning text prompts into CAD artifacts.",
  "tags": ["cad", "3d", "automation"],
  "model_providers": ["openai"],
  "runtimes": ["python"],
  "capabilities": ["Generate CAD artifacts from natural language"],
  "setup_commands": ["pip install -e ."],
  "verify_commands": ["pytest"],
  "sources": ["manual"]
}
```

```bash
python -m harness_add --from-json ./text-to-cad-harness.json
```

Harness pages live under `entities/harnesses/<slug>.md`. Setup and verification
commands are documentation only; ctx records them so the user can inspect and
decide before running anything.

To inspect and install a harness:

```bash
python -m harness_install text-to-cad --dry-run
python -m harness_install text-to-cad
python -m harness_install text-to-cad --update --dry-run
python -m harness_install text-to-cad --uninstall --dry-run
```

The installer clones or copies the harness into `~/.claude/harnesses/<slug>` and
writes `~/.claude/harness-installs/<slug>.json`. It does not run setup commands
unless you pass `--approve-commands`, and it does not run verification commands
unless you also pass `--run-verify`.

```bash
python -m harness_install text-to-cad --approve-commands --run-verify
python -m harness_install text-to-cad --update --approve-commands --run-verify
python -m harness_install text-to-cad --uninstall
python -m harness_install text-to-cad --uninstall --keep-files
```

## Initialize Model Choice

During setup, record whether you use Claude Code or your own model. Plain
`ctx-init` starts a small wizard when it is attached to an interactive
terminal; use `ctx-init --wizard` to force the prompts, or pass explicit flags
such as `--model-mode skip` for non-interactive automation.

```bash
ctx-init
ctx-init --wizard
ctx-init --model-mode skip
```

For Claude Code:

```bash
ctx-init --model-mode claude-code --goal "maintain a FastAPI service"
```

For a custom model:

```bash
ctx-init \
  --model-mode custom \
  --model openai/gpt-5.5 \
  --goal "build CAD artifacts from text prompts"
```

Add `--validate-model` only when you want `ctx-init` to make one small provider
call. Without that flag, setup writes `~/.claude/ctx-model-profile.json` and
prints harness recommendations without calling the model.
