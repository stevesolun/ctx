# CTX seven-document semantic audit evidence

Date: 2026-09-30 (Europe/Prague)

Base HEAD at audit start/end: `8c6ecc1c6a48ebe088d55d363dc2a0dc7ec1750a`.
Results exercised the dirty working tree, not that unmodified commit. This is
retained historical evidence, not final-tree story acceptance.

Owned files:

- `docs/catalog.md`
- `docs/knowledge-graph.md`
- `docs/huggingface-publish.md`
- `docs/entity-onboarding.md`
- `docs/dashboard.md`
- `docs/telemetry.md`
- `docs/backup-hook-install.md`

## Source-backed corrections

1. Incremental attach repair (`knowledge-graph.md`, `entity-onboarding.md`)
   - `src/ctx/core/wiki/wiki_queue_worker.py:355-368` returns a normal
     `_AttachOutcome` when no vector index exists.
   - `process_next` then calls `mark_succeeded` at line 130.
   - Therefore a skipped attach is not a pending job. The docs now say the
     graphify/index build reconciles the current entity set and a later attach
     requires a fresh entity update (or reviewed manual attach), while actual
     failed jobs still follow the retry policy.
   - Direct regression evidence:
     `src/tests/test_wiki_queue_worker.py:597-599` asserts both succeeded state
     and `incremental attach skipped (no vector index)`.

2. Dashboard behavior (`dashboard.md`)
   - `src/ctx/monitor/pages/manage.py:35-81` has search and manual form-based
     add/update/delete only; there is no file upload/import input or endpoint.
   - `src/ctx/monitor/pages/skillspector.py:89-120` renders grade, raw score,
     type, optional floor, a 4,000-character JSON preview, and at most the last
     100 audit rows with timestamp/event/actor. It does not render a dedicated
     full four-signal table or session ids.
   - `/skills` is paginated; wiki body/frontmatter displays are bounded; an
     unknown or sparse session can contain any subset of lifecycle events.
   - Corrected all corresponding route and KPI prose.

3. Direct telemetry API paths and local export sensitivity (`telemetry.md`)
   - `export_traces` and `preview_traces_export` accept the provided `Path`
     directly (`src/ctx/telemetry/__init__.py:1204` and `:1372`), so a literal
     `Path("~/<...>")` is not expanded by those APIs. Examples now call
     `.expanduser()`.
   - Incremental local export contains records after its checkpoint; only the
     `--all` replay is expected to mirror all well-formed spool event ids.
   - Local JSONL retains raw session ids by design, so the verification prose
     now labels output local-sensitive and owner-only.

4. Backup naming and manifest structure (`backup-hook-install.md`)
   - `src/backup_mirror.py:294-314` appends an underscore plus sanitized
     reason to the microsecond timestamp under the default `{timestamp}` name
     format. Direct value check:
     `_new_snapshot_id(0.125, "Edit:CLAUDE.md") ==
     "19700101T000000.125000Z_edit-claude.md"`.
   - `src/backup_mirror.py:372-377` stores `reason` once at manifest top level
     and SHA-256 inside each captured entry.
   - Corrected the old double-underscore/dot-stripped examples.

5. Hugging Face workflow (`huggingface-publish.md`)
   - `.github/workflows/huggingface-sync.yml:32-45` fails closed for a missing
     canonical token and excludes every non-canonical repository from all
     publish steps, regardless of whether a fork has a token.
   - `scripts/sync_huggingface.py::_assert_hydrated_artifacts` checks every
     manifest record, not only the two archives and skill index. Manual-sync
     prose now says all five byte identities.

6. Public catalog visibility (`catalog.md`)
   - `.ctx-catalog-card { display:grid }` overrode native `[hidden]` rendering
     in the production stylesheet. `.ctx-catalog-card[hidden] { display:none; }`
     now makes URL/query/type filters remove cards from layout.
   - The real-stylesheet browser regression asserts computed display and zero
     layout height, not only the `hidden` attribute.

7. Graph/current counts (`knowledge-graph.md`, `catalog.md`)
   - `graph/wiki-graph-stats.json` records 79,958 nodes, 1,778,069 edges,
     68,494 skills, 467 agents, 10,790 MCPs, 207 harnesses, 52 communities,
     67,024 body-backed entries, and the documented edge-source/cross-type
     totals.
   - `graph/release-artifacts.json` has five records. Four locally present
     assets (communities, overlays, skill index, runtime archive) matched both
     exact size and SHA-256 during this audit. The full archive was not present
     locally and was not downloaded.

## Safe command and behavior execution

All pytest executions used local fixtures/mocks and `--no-cov`; no model call,
remote publication, real user-data mutation, service install, or network
artifact hydration occurred.

1. Public docs, catalog browser, Hugging Face sync contract, release manifest:

   `.venv/bin/python -m pytest -q --no-cov src/tests/test_docs_catalog_page.py src/tests/test_public_docs_browser.py src/tests/test_huggingface_sync.py src/tests/test_graph_release_manifest.py`

   Result: **42 passed in 2.14s**.

2. Graph pack/store/queue behavior:

   `.venv/bin/python -m pytest -q --no-cov src/tests/test_pack_compaction.py src/tests/test_graph_store.py src/tests/test_wiki_queue_worker.py src/tests/test_wiki_queue_worker_cli.py`

   Result: **74 passed in 2.09s**.

3. Init consent and entity onboarding fixture behavior:

   `.venv/bin/python -m pytest -q --no-cov src/tests/test_ctx_init.py src/tests/test_entity_update.py src/tests/test_entity_write_safety.py src/tests/test_agent_add.py src/tests/test_mcp_add.py src/tests/test_harness_add.py`

   Result: **137 passed in 2.22s**, one Python 3.14 tar-extraction deprecation
   warning from a fixture.

4. Live local HTTP/browser dashboard behavior:

   `.venv/bin/python -m pytest -q --no-cov src/tests/test_ctx_monitor_acceptance.py src/tests/test_ctx_monitor_browser.py`

   Result: **21 passed in 26.84s**.

5. Telemetry privacy/export/retention fixture behavior:

   `.venv/bin/python -m pytest -q --no-cov src/tests/test_enterprise_telemetry.py`

   Result: **94 passed in 1.34s**.

6. Backup config/snapshot/retention/watchdog fixture behavior:

   `.venv/bin/python -m pytest -q --no-cov src/tests/test_backup_config.py src/tests/test_backup_mirror.py src/tests/test_backup_retention.py src/tests/test_backup_watchdog.py`

   Result: **102 passed in 1.33s**.

7. Cross-surface truth/threat/count documents:

   `.venv/bin/python -m pytest -q --no-cov src/tests/test_surface_truth.py src/tests/test_threat_model_docs.py src/tests/test_release_graph_count_sync.py`

   Result: **35 passed in 2.19s**.

8. Exact option/parser smoke (all exit 0):

   - `.venv/bin/ctx-init --help`
   - `.venv/bin/ctx-scan-repo --help`
   - `.venv/bin/python -m ctx_monitor --help`
   - `.venv/bin/python -m ctx.core.wiki.wiki_graphify --help`
   - `.venv/bin/python -m ctx.core.wiki.pack_compaction --help`
   - `.venv/bin/python -m ctx.core.graph.graph_store --help`
   - `.venv/bin/python -m ctx.core.wiki.wiki_queue_worker --help`
   - `.venv/bin/python -m ctx.core.graph.incremental_attach --help`
   - `.venv/bin/python -m ctx.core.graph.incremental_shadow --help`
   - `.venv/bin/python -m skill_add --help`
   - `.venv/bin/python -m agent_add --help`
   - `.venv/bin/python -m mcp_add --help`
   - `.venv/bin/python -m mcp_fetch --help`
   - `.venv/bin/python -m harness_add --help`
   - `.venv/bin/python -m harness_install --help`
   - `.venv/bin/python scripts/sync_huggingface.py --help`
   - `.venv/bin/python scripts/dashboard_smoke.py --help`
   - `.venv/bin/python scripts/graph_release_manifest.py --help`
   - `.venv/bin/python src/import_skills_sh_catalog.py --help`

9. Formatting/scope check:

   `git diff --check -- docs/catalog.md docs/knowledge-graph.md docs/huggingface-publish.md docs/entity-onboarding.md docs/dashboard.md docs/telemetry.md docs/backup-hook-install.md`

   Result: exit 0.

10. Isolated `--graph-only` quality projection (DOC026):

    A fixture home at `<isolated-graph-fixture>/home` supplied a user config whose
    graph blend was `semantic=0.0`, `tags=0.5`, `slug_tokens=0.5`. This is an
    explicit no-network profile: graphify does not construct or call an
    embedding backend when semantic weight is zero. The fixture wiki contained
    two skills; only `graded` had a quality sidecar (`grade=A`, `score=0.875`).

    `HOME=<isolated-graph-fixture>/home .venv/bin/python -m ctx.core.wiki.wiki_graphify --wiki-dir <isolated-graph-fixture>/wiki --graph-only --semantic-vector-index off`

    Result: exit 0; graph export reported 2 nodes, 1 tag edge, and zero semantic
    pairs. Reading the emitted `graphify-out/graph.json` asserted:

    - `skill:graded quality_grade=A quality_score=0.875`
    - `skill:ungraded quality_grade=None quality_score=None`

    The command wrote only below `<isolated-graph-fixture>`; it did not read or mutate
    the real user home and could not download an embedding model under the
    zero-semantic profile.

## Explicit unexecuted/external gaps

- No Hugging Face API lookup or publication was performed; the remote dataset
  HEAD/card/license/tags remain external verification.
- The full `graph/wiki-graph.tar.gz` was absent locally. It was not hydrated
  from GitHub because this lane prohibited network artifact changes. Its
  recorded identity/counts are source-backed by the tracked manifest/stats and
  prior release evidence, not freshly rehashed here.
- No graph archive was installed into the real `~/.claude`, and no manual tar
  extraction, full optional-model graph rebuild, pack promotion, entity write, harness setup,
  approved harness command, or provider validation call was executed.
- No OTLP collector or remote endpoint was contacted. Network retry, TLS,
  allow-list, partial-success, and no-redirect behavior was exercised through
  local mocked tests only.
- No systemd or launchd unit was installed/loaded and no persistent watchdog
  was started. Backup commands were exercised through isolated pytest homes.
- The historical ~50-minute CNM observation was not reproduced; doing so is
  intentionally outside a safe bounded docs audit.
- The catalog stylesheet fix is local and tested. The GitHub Pages deployment
  will remain unchanged until the repository is published; deployed visibility
  must be rechecked after release.
- `python src/update_repo_stats.py --check` currently reports only the global
  concurrent test-inventory drift (8,835 -> 8,963) in README/docs index. It did
  not identify a seven-file graph-count drift, and this lane did not modify the
  shared stats files while the tree was still changing.
