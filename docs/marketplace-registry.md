# Entity Source Surfaces

ctx keeps discovery metadata separate from install state, but it does **not**
currently expose one ordered, federated marketplace registry. In particular,
`ctx.core.source_registry` is an ingestion provenance and license gate; it is
not the recommendation engine and it does not register the runtime graph,
local assets, GitHub repositories, or MCP catalogs by priority.

## User-supplied inputs

- CTX ships the engine and empty graph/wiki structure. The shared recommender
  in `ctx.adapters.generic.ctx_core_tools` reads your local graph/wiki records;
  it does not ship a catalog, download a fallback corpus, or perform a live
  marketplace search for each recommendation.
- `src/import_skills_sh_catalog.py`, the PulseMCP and awesome-list MCP
  importers, and the graph build pipeline remain optional authoring tools.
  Importing or refreshing sources you select is explicit work, separate from
  initialization and recommendation. CTX does not publish an owned dataset.
- `python -m skill_add`, `agent_add`, `mcp_add`, and `harness_add` are explicit
  entity intake commands. `mcp_fetch` fetches configured MCP sources, and
  `harness_install` installs a reviewed harness definition.
- Host adapters and installers discover applicable user-local assets during
  their own workflows. There is no general `user-local` registry entry that
  automatically overrides every candidate.
- `ctx-source-registry` validates supplied external-source records; its default
  source list is empty.
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

1. The active recommender searches user-supplied graph/wiki knowledge.
2. It normalizes and deduplicates candidates according to the recommendation
   implementation, applies its quality/availability gates, and returns the
   bounded result with reasons and availability: at most five tools total
   across skills, agents, and MCP servers. Tools need not already be installed
   to be suggested. Empty knowledge and no useful match return no suggestions.
3. An entity such as `find-skills` can be recommended like any other skill, but
   ctx does not silently execute it as a live remote-search step.
4. Remote refresh, entity intake, update review, and installation remain
   separate explicit workflows.

This is the implemented boundary. A live federated registry with source
precedence, remote freshness queries, and cross-source semantic deduplication
would be new product work, not behavior provided by this page.

## Update Rules

See [entity onboarding](entity-onboarding.md#updates-and-removal) for explicit
update review, pack-aware authoring, and removal boundaries. Intake queues wiki
work; follow its [indexing steps](entity-onboarding.md#index-and-check) to
refresh the derived graph after approved changes.

## Security Notes

- Install only after explicit approval through a separate user/host workflow.
  Recommendations do not install, execute, activate, or unload anything.
  Saved legacy preapproval preferences cannot authorize the current
  recommendation path. Unload and uninstall require separate permission.
- Always show the source URL, entity type, install command, and permissions.
- Reject missing `SKILL.md` bodies for skill imports unless the source is only a
  catalog pointer.
- Never execute repository scripts during cataloging without explicit user
  approval.
- Warn on network, filesystem, shell, credential, or system-level permissions.
- Preserve last-good user graph/wiki artifacts so a failed refresh cannot
  replace valid knowledge with corrupt data.
