# Feature audit evidence — 2026-09-30

`qa/feature_status.csv` is the only canonical tracker. This directory retains
bounded execution evidence and reproductions, not another status ledger.
Results apply to the dirty working tree and the source hashes in each artifact;
the base HEAD alone does not identify the code tested. No final-tree story pass
is asserted by this handoff.

At the mapper freeze, the canonical CSV contained 318 rows: 254 executable
contracts, 60 explicit acceptance checklists, and four deprecated contracts.
All 314 active contracts received behavior-level source/assertion review. The
CSV SHA256 was `a3617aee4afb9c1b5304ddbe2937dffc3f9cbb18b00600b0c542b749d935c73b`.
Later coordinator results may update that file; these are historical counts.

## Retained examples

- `documentation-evidence.md`: source-backed review, corrections, and explicit
  unexecuted host/provider/service boundaries for the main documentation lane.
- `seven-docs-evidence.md`: catalog, graph, publishing, onboarding, dashboard,
  telemetry, and backup evidence, including isolated no-network graph-only
  quality projection.
- `doc-toolbox-example-evidence.json`: 34 isolated CLI examples.
- `doc-lifecycle-example-evidence.json`: 37 isolated health/quality/lifecycle
  examples, including unchanged files under dry-run after its repair.
- `doc-host-example-evidence.json`: eight real local API/adapter examples over
  a synthetic graph; no ranking function mocks or provider calls.
- `dashboard-performance-evidence.json`: shipped-runtime cold/warm graph HTTP
  and exactly 10,000 synthetic sidecars for bounded KPI timing. This is not a
  full-catalog KPI performance claim.

The three portable documentation scripts reran successfully at handoff:

```sh
.venv/bin/python qa/feature-audit/doc_toolbox_examples.py
.venv/bin/python qa/feature-audit/doc_lifecycle_examples.py
.venv/bin/python qa/feature-audit/doc_host_examples.py
PYTHONPATH=. .venv/bin/python qa/feature-audit/dashboard_performance_repro.py
```

The first three write fresh local JSON under `/tmp/ctx-feature-audit-*`, not
over this evidence. The performance reproducer prints JSON and requires the
already-hydrated runtime archive. Inspect its cold/warm measurements against
the recorded thresholds; its exit status alone checks the smoke result.
Its retained timings precede the mapper freeze and need a frozen-tree rerun.
All paths in retained example output are synthetic or redacted (`<fixture>`,
`<repo>`). No credentials, real user data, or provider execution are retained.

The final resolver/tracker/surface check at handoff was:

```sh
.venv/bin/python -m pytest -q --no-cov src/tests/test_resolve_skills.py src/tests/test_feature_user_story_tracker.py src/tests/test_dashboard_user_story_tracker.py src/tests/test_surface_truth.py
```

Result: **88 passed in 1.95s**. This validates the small final legacy-router
documentation correction and tracker structure, not all 314 story outcomes.
The latest mapped session privacy, status payload, protected logs/events/SSE,
archive failure, threshold sweep, and wizard-command assertions were inspected
directly; their coordinator final-tree execution remains authoritative.

## True remaining acceptance groups

1. Frozen-tree aggregate evidence: full non-integration tests, browser tests,
   static checks, committed fast gate, PR preflight, real wheel/clean install,
   release-pair deep validation, and synchronized repository stats.
2. Final documentation rendering/links and source-hash reconciliation. Safe
   graph/store/attach/compaction, scanner, and local telemetry code blocks not
   individually repeated remain distinguished from behavior-suite coverage.
   Deployed Pages/catalog fixes are not proved by local browser results.
3. One real timing rerun for DASH-013/API-004/API-009 with the retained bounded
   fixture and explicit 5s cold graph / 1s warm graph / 3s KPI thresholds.
4. Current external facts and hosted evidence: About, rulesets, release upload
   and attestation, supported-host package smoke, Pages, and Hugging Face.
   Existing releases or a workflow's wiring do not prove a new-tree publish.
5. Explicit unavailable prerequisites: CLI-039 needs an already approved local
   SkillSpector runtime (no `audit-directory` subcommand exists); MAINT-017
   needs an approved OCR LLM endpoint/credentials; DIST-013 has no required
   independent-approval rule. These are not test failures or silent passes.
6. Real third-party SDK/host handshakes, paid/model-backed runs, service
   installation/activation, and remote collectors/publication remain external.
   Do not run them merely to manufacture an all-green audit.

The coordinator owns final dispositions and exact-tree/commit evidence. The
temporary detailed execution map is an execution aid only; this bundle does
not duplicate its large per-row inventory.
