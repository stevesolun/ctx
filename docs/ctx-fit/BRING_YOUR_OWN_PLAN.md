# CTX — Bring your own knowledge

## Standing product contract

Owner direction, 2026-10-12: distribute CTX's infrastructure, recommendation
engine, graph schema and wiki structure, not a maintained catalog of downloaded
skills, agents, MCP servers or their executable bodies. Users and organizations
populate and control their own knowledge. The primary experience is relevant,
bounded suggestions during work in Codex, Claude Code or another connected
host. CTX Fit remains a secondary, explicitly invoked evaluation utility.

The normal installation is empty, local and offline. Empty knowledge and no
useful match are successful states, not reasons to download a fallback corpus.
One shared graph/wiki recommendation path serves CLI, MCP and native hooks.
Host events trigger work; no additional paid LLM or polling daemon is required.
Recommendations contain at most five tools in total, can contain none, and
state their relevance and availability without claiming measured savings.

Similarity and graph content never authorize execution. Preserve per-kind
installation preferences, explicit trust boundaries and approval requirements.
Do not equate an advisory load/unload request with an actual host action.
Automatic all-kind provisioning remains a distinct acceptance condition until
it has real end-to-end proof. Unload requires separate permission.

## Scope and deletion boundary

- Remove the version-controlled downloaded corpus, populated graph outputs and
  bundled runtime capabilities from the distributed product.
- Retain graph/wiki schemas, indexing/query/import machinery, safe installation
  infrastructure and minimal synthetic test fixtures excluded from artifacts.
- Prevent bootstrap, defaults, CI and release jobs from silently restoring the
  historical CTX-owned corpus. Retire automatic owned-dataset publication.
- Preserve all audit fixes through base commit
  `0afdac155083db28935da2c4a9d4ef64bb21b621`.
- Do not delete mixed user data under personal tool homes, credentials,
  `.scratch/`, Git history, historical releases or the repository itself.
  Local archive cleanup requires exact ownership and dependency checks.

## Ordered tasks and ownership

| ID | Task | Owner | Dependency | Acceptance |
| --- | --- | --- | --- | --- |
| BYO-1 | Record product decision and safe removal inventory | Coordinator | None | New mandate explicit; prior evidence retained |
| BYO-2 | Empty/offline setup and package; explicit user imports | Data/bootstrap lane | BYO-1 | Fresh home has zero tools; reinit preserves data; no download fallback |
| BYO-3 | Shared user-catalog recommendations through CLI/MCP/native hooks | Engine lane | BYO-1 | Same inputs produce same bounded suggestions; empty/no-match abstains |
| BYO-4 | Corpus-free CI/release; retire automatic dataset publication | Delivery lane | BYO-1 | Checks and build do not hydrate old data; software provenance gates retained |
| BYO-5 | Remove tracked corpus; align docs, metadata and acceptance board | Coordinator | BYO-2/3/4 | No public bundled-tool promise; changed contracts explicitly recorded |
| BYO-6 | Verify clean artifact, user knowledge and consent boundaries | Coordinator + independent reviewer | BYO-2/3/4/5 | Focused checks, security review, committed fast gate and PR preflight |
| BYO-7 | Ship reviewable GitHub change; verify hosted result | Coordinator | BYO-6 | Exact-head checks green; publication status reported honestly |

## Required behavior checks

1. Isolated clean install/init creates the empty graph/wiki structure with no
   external catalog or tool installation, network request or model call.
2. Author/import synthetic user-owned skill, agent and MCP records, build the
   graph, and obtain relevant mixed recommendations from the same engine.
3. No match returns none; the mixed list never exceeds five; selected/rejected
   or active tools are suppressed as supported by the host contract.
4. Reinitialization and custom imports preserve existing user data and do not
   inject CTX-owned capabilities. Malformed/untrusted inputs fail safely.
5. Real installed CLI and MCP child processes work without repository-relative
   files. Hook subprocess tests are distinguished from actual host acceptance.
6. No tool metadata, prompt or similarity score grants installation, credential
   access, activation or unload authority. Test the selected consent behavior.
7. Wheel/sdist and release jobs contain no downloaded tool corpus. CI uses
   synthetic fixtures, never silently downloads the retired release catalog.

## Execution loop

Read this contract and STATE.md, reconcile the actual tree, dispatch only
disjoint bounded work, inspect evidence rather than agent self-reports, run
focused checks first, freeze writers before broad gates, and record exact
results and remaining blockers. No repeated full suites without a relevant
change. Do not claim publication, host interoperability or token/time savings
from tests that did not measure them.

## Initial evidence

At the preserved base, native hooks route to an exact bundled-skill catalog;
MCP already queries user graph/wiki. `ensure_wiki()` creates empty structure.
Initialization can download the old graph and inject nine packaged tools even
into custom imports. CI/release/Hugging Face jobs hydrate or publish old data.
The tracked downloaded skill corpus contains 477 files; graph outputs occupy
about 21 MB. Personal wiki data may be mixed and is outside bulk deletion.

## Open boundary

Owner was asked whether this release requires automatic provisioning under
per-kind preapproval in addition to suggestions and explicit installation.
Work on empty infrastructure, shared recommendations and corpus removal does
not depend on that answer; no automatic execution is enabled by inference.
