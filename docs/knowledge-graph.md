# Your knowledge graph and LLM wiki

CTX provides the graph/wiki infrastructure. **You provide the knowledge.**
A fresh installation has no skill, agent, MCP, or harness records and downloads
no CTX corpus. Existing local records are not deleted or replaced during setup.
The optional `ctx fit` evaluator consumes configuration evidence separately;
it is not required to build or query your graph.

## Structure

The compatibility default is `~/.claude/skill-wiki/`. Select a private location
with an absolute, non-symlink `CTX_WIKI_DIR` for `ctx recommend`, the Python API,
MCP, and native hook reads.
Keep that environment variable consistent in your shell and host configuration.
`ctx-init` and the legacy dashboard still use the compatibility default;
`CTX_WIKI_DIR` does not redirect their wiki paths. For a private wiki, pass the
same absolute path to the authoring commands' `--wiki` and the graph builder's
`--wiki-dir` options.

```text
skill-wiki/
  entities/
    skills/
    agents/
    mcp-servers/
    harnesses/
  concepts/
  graphify-out/
```

This is a structure, not a shipped dataset. Entity Markdown frontmatter carries
a stable name/type, description, tags, source/provenance, and reviewed
installation metadata where relevant. Bodies contain your explanation or
instructions; wikilinks can describe relationships. Generated graph/index
files are derived views of your records. Do not put credentials into either.

Tools can be known without being installed. CTX distinguishes a catalog
candidate from local availability; a useful similarity match is not permission
to fetch or execute the candidate.

## Initialize and populate

```bash
ctx-init --model-mode skip
```

Initialization creates the empty structure locally and offline. `--graph`
remains a compatibility setup option; it does not download an old release
catalog. Only import an archive when you explicitly choose a user-owned source.

For an explicit, digest-pinned archive import into an empty or untouched
initialized wiki:

```bash
ctx-init --graph --graph-url https://your-organization.example/wiki.tar.gz \
  --graph-sha256 <64-character-sha256> --graph-install-mode full --model-mode skip
```

Replace the example URL and digest with the exact source you reviewed.
This explicit import can use the network; normal initialization does not.

Use [entity onboarding](entity-onboarding.md) to author or add a record.
Reinitializing an existing wiki preserves its content. Legacy downloaded content
already in your personal wiki is your data: upgrading CTX does not bulk-delete it.

## How edges are built

`python -m ctx.core.wiki.wiki_graphify` derives relationships from the records
you supplied:

- Semantic cosine similarity when you explicitly use an available embedding backend.
- Shared frontmatter tags and meaningful slug tokens.
- Shared specific source URLs.
- Direct wikilinks between entities.

Configured weights and thresholds combine those signals. Usage, quality,
type affinity, and shared-neighbor information can adjust existing edges.
They do not turn a graph connection into installation authority. Community
pages summarize clusters in your own graph; their number depends on your data.

Optional embedding backends can require model downloads or a running service.
Those are not part of default initialization. Review your embedding configuration
before starting a graph build; the vector-index option does not disable embedding.

## Rebuilding

```bash
python -m ctx.core.wiki.wiki_graphify --help
python -m ctx.core.wiki.wiki_graphify --wiki-dir /absolute/path/to/wiki --graph-only
```

Select the wiki explicitly with `--wiki-dir` when building a non-default
location. The graph builder's help lists embedding and output options;
review them before enabling model-backed work. Indexing writes derived graph
files; it does not install the tools described by the input pages.

The entity add commands enqueue local wiki work. To drain one queued job:

```bash
python -m ctx.core.wiki.wiki_queue_worker --wiki /absolute/path/to/wiki --limit 1
```

The worker can refresh wiki indexes and graph overlays. If no semantic vector
index exists, incremental semantic attachment is skipped; that is not an
instruction to download the retired CTX catalog. A full local rebuild can
reconcile your records after you configure the desired backend.

When active wiki packs exist, rebuilds use their validated merged view,
including overlays and deletions. Stale loose Markdown files do not override
that view. Use [pack-aware authoring](entity-onboarding.md#add-records-you-own)
to update metadata and reviewed skill/agent bodies; corrupt or incomplete active
packs stop the rebuild rather than replacing your knowledge with an empty graph.

## Querying

```bash
ctx recommend "review a Python API" --json
ctx-scan-repo --repo . --recommend
python -m ctx_monitor serve
```

The CLI, `ctx__recommend_bundle` MCP tool, and native prompt adapters share
the recommendation engine. Results are capped at five across skills, agents,
and MCPs—not five of each. An empty wiki or an unrelated task can return no
recommendations. Hosts may provide selected/rejected/active context to suppress
items already considered.

For direct Python use, see
[the library path](harness/attaching-to-hosts.md#2-python-library-path).

## Import and trust boundaries

Use sources you control or have explicitly reviewed. Archive extraction rejects
unsafe members; it is still your responsibility to review content and licenses.
Never follow an installation string solely because it appears in a wiki page.
The current product suggests tools; installing a selected tool requires explicit
approval. Unload and uninstall are separate actions with separate approval.

Graph outputs, embeddings, wiki packs, and backups remain user-owned artifacts.
CTX no longer publishes or hydrates its old corpus by default. Historical GitHub
releases and dataset snapshots are not the current installation contract.

## Sharing within an organization

Keep private records in a repository or storage location your team controls.
Review changes to descriptions, source URLs, installation instructions, and
permissions before importing them. Retain provenance and versions so a
recommendation can be traced back to the record that produced it.

Do not infer measured savings from similarity. The graph ranks potentially useful
tools; real task quality, time, and cost require independent measurements.
