# Entity Source Surfaces

ctx keeps discovery metadata separate from install state, but it does **not**
currently expose one ordered, federated marketplace registry. In particular,
`ctx.core.source_registry` is an ingestion provenance and license gate; it is
not the recommendation engine and it does not register the runtime graph,
local assets, GitHub repositories, or MCP catalogs by priority.

## What currently ships

- The release graph/wiki artifacts and the packaged runtime overlay are local,
  offline recommendation inputs. `ctx.core.resolve.recommendations` reads the
  graph and the locally packaged skills.sh snapshot; it does not perform a live
  marketplace search for each recommendation.
- `src/import_skills_sh_catalog.py`, the PulseMCP and awesome-list MCP
  importers, and the graph build pipeline create local catalog data. Refreshing
  those sources is an explicit import/release operation.
- `python -m skill_add`, `agent_add`, `mcp_add`, and `harness_add` are explicit
  entity intake commands. `mcp_fetch` fetches configured MCP sources, and
  `harness_install` installs a reviewed harness definition.
- Host adapters and installers discover applicable user-local assets during
  their own workflows. There is no general `user-local` registry entry that
  automatically overrides every shipped candidate.
- `ctx-source-registry` validates built-in or supplied external-source records.
  Its provenance, license, and ingestion boundaries are described in the
  [threat model](threat-model.md#graph-and-catalog-metadata).

Supply a registry with `ctx-source-registry --registry PATH`. The JSON must be
a list of record objects or an object containing a `sources` list of records.
Unreadable files, invalid JSON or record structures, and policy failures exit
with code `1`. Human output reports the error on stderr; `--json` instead emits
`{"error": "...", "failed": 1}` on stdout. Successful validation exits `0`.

The old names `ctx-shipped-graph`, `user-local`, `shipped-skills`,
`github-entity-repos`, and `mcp-and-harness-sources` were documentation labels,
not configuration accepted by ctx. Do not put those YAML examples in a config
file.

## Recommendation behavior

1. The active recommender searches the graph/wiki and any local catalog
   snapshot already loaded into that runtime.
2. It normalizes and deduplicates candidates according to the recommendation
   implementation, applies its quality/availability gates, and returns the
   configured bounded result with reasons.
3. An entity such as `find-skills` can be recommended like any other skill, but
   ctx does not silently execute it as a live remote-search step.
4. Remote refresh, entity intake, update review, and installation remain
   separate explicit workflows.

This is the implemented boundary. A live federated registry with source
precedence, remote freshness queries, and cross-source semantic deduplication
would be new product work, not behavior provided by this page.

## Update Rules

Entity updates are intentionally explicit:

- `python -m skill_add`, `python -m agent_add`, `python -m mcp_add`, and `python -m harness_add` create
  new entities when no duplicate exists.
- If a duplicate exists, the command emits an update review and refuses to
  replace content unless the user passes `--update-existing`.
- New or updated skills go through the micro-skill line-count gate from config.
- Security/cyber checks run before entity content is promoted.
- Graph/wiki artifacts are rebuilt, validated, packed, and atomically promoted.

## Security Notes

- Never install without current per-kind approval or persisted preapproval from
  `ctx-init`; preapproval for installation never authorizes unload or uninstall.
- Always show the source URL, entity type, install command, and permissions.
- Reject missing `SKILL.md` bodies for skill imports unless the source is only a
  catalog pointer.
- Never execute repository scripts during cataloging without explicit user
  approval.
- Warn on network, filesystem, shell, credential, or system-level permissions.
- Preserve last-good graph/wiki artifacts so a failed refresh cannot ship a
  corrupt runtime.
