# Legacy fallback recovery follow-up — 2026-10-04

This is bounded repair evidence, not another status tracker. Current story
statuses remain in `qa/feature_status.csv`; the original 318 contracts and
historical findings must remain intact.

## Starting point and defect

Base: `591b26c4595a5eb403bb2a4362c466989da99775`, draft PR
[#286](https://github.com/stevesolun/ctx/pull/286). The previous delivery run
returned `checks-passed`; no merge or release occurred. Its original review
still identified the following legacy telemetry defect, so passing CI did not
establish whole-audit acceptance.

A legacy checkpoint can be hashed with fallback B while the primary manual
key A has an unusable lock. If A recovers and the configured fallback now holds
C, both candidates are available but neither proves the old identity. The old
guard considered unavailable candidates only, allowing acknowledged records
to be recounted or exported again. CLI-043 and LANE-D-004 were reopened.

The shared recovery boundary must refuse this ambiguity without changing the
checkpoint or contacting the sink. It must still accept a matching historical
key, preserve recognizable source/destination changes, support deliberate
selected-key rotation, and allow restoring B or explicit `--all` recovery.
Removed legacy configuration cannot be reconstructed from absent provenance.

## Failing-first evidence

- Coordinator external absent/B/C × preview/export × three-signal matrix:
  **6 failed, 12 passed in 0.65s** on unchanged `627bb525` telemetry source;
  the identical source persisted through `591b26c4`.
- Permanent expanded regression selection, before production edits:
  **24 failed, 44 passed, 294 deselected in 2.60s**. The coordinator read
  `.gate/review-r3-shared-red.txt` directly. Failures cover replacement-C
  environment, inline and manual-file alternatives across all three signals,
  plus continuous event/metric checkpoint mutation. Existing absent-env,
  matching-B and recognizable-scope controls passed.

## Delivered baseline evidence

These results apply to the base, not the new repair:

- [Hosted test run](https://github.com/stevesolun/ctx/actions/runs/37160902911):
  **9,272 passed, 51 skipped in 565.44s**, **91.27% coverage**. All required
  checks succeeded; two classifier-selected jobs were intentionally skipped.
- Actual Ubuntu clean-host job and Linux/macOS wheel smoke jobs succeeded.
  The `package-smoke-wheel` artifact (ID `11287855430`) was uploaded with
  seven-day retention; it was present and unexpired when checked.
- The preceding hosted failure was four direct optional-LiteLLM imports,
  fixed in base commit `591b26c4` using controlled catalog stubs. Production
  routing and assertions were unchanged. Coordinator pytest with LiteLLM
  imports deliberately blocked: **37 passed in 3.93s**.

## Separate benchmark fixture isolation

The earlier fast gate failed before an arm-mismatch assertion while preparing
unrelated installed-package inventory. Its original process failure remains
unproven; one later successful replay did not erase that failure.

The targeted test now installs its existing process/workspace guards before
fixture construction and supplies a deterministic valid dependency digest.
Real arm rejection, output-absence assertions, production containment, and
dedicated identity/drift tests remain intact. An initially missing local graph
archive was an environment prerequisite, not regression evidence; an ignored
link reuses an existing archive matching the exact release manifest.

## Repair acceptance

The coordinator directly read the permanent regression logs: **92 passed**
for the targeted selection, then **522 passed in 26.82s** for the identity and
enterprise telemetry modules. The separate benchmark six-case selection was
coordinator-replayed: **6 passed in 4.46s**. Those cases retain real arm rejection
and dependency-identity/drift assertions. These are focused results, not new
full-gate results.

Frozen SHA-256:

| File | SHA-256 |
| --- | --- |
| `src/ctx/telemetry/__init__.py` | `3ba4921a2b64de362259fe94d884300da5e1eef5fa8f44111518f53f8954fba1` |
| `src/tests/test_telemetry_checkpoint_identity.py` | `f0869c915f42e75657ecc75ca3977eec312f406185d130be0766d45d33a22d40` |
| `src/tests/test_ctx_ab_benchmark.py` | `03615af304167dbbafd4b2008ec33ff0eae235eddbeb580aed3de20b0c3829f8` |

The repository's live inventory updater found exactly two stale public values:
README and the homepage. Both now identify **9,450 test inventory**, not passes.
Full Ruff and three-file format checks passed; mypy reports no issues in
**596 source files**. The coordinator tracker/surface selection passed
**53 tests in 5.04s**. A fresh independent reviewer found no material standards
or specification defect after tracing real callers, checkpoint persistence,
capture, selected-key rotation and the migration limits; all three hashes
matched before and after that read-only review. CLI-043/LANE-D-004 therefore
move from Needs Fix to **Needs Validation**, pending new committed gates and
delivery, not directly to an aggregate pass.

New committed gates remain pending. Deployed Pages, external publication, live OCR, and governance
prerequisites remain separate; none is proved by the local repair.

## Coordinator handoff record

The original unattended driver accepted a fix-review while R3 remained open.
The coordinator inspected the exact installed tool behavior, disconnected only
that client, and reattached without automatic approvals. The same run preserved
every commit and later returned `checks-passed`; it was never aborted or reset.
Passing phases offered no additional repair gate before publication, so the PR
was explicitly kept draft. Guarded synchronization returned the delivered
history for this supported follow-up. Phase completion never substituted for
semantic acceptance of the known defect.

The preceding local fast/preflight failures are retained as historical evidence
in `test-phase-91cb979f-20261004.md` and `STATE.md`; passing retries were not
represented as production fixes. Two disjoint repair lanes handled telemetry
and benchmark isolation; the coordinator owns metadata and final integration,
with a fresh reviewer who did not author either patch.

The [MCP issue update](https://github.com/stevesolun/ctx/issues/283#issuecomment-5974481007)
links the draft PR, leaves the issue open, and makes no release/full-conformance
claim. The October 4 read-only traffic snapshot covered September 19–October 2:
335 views / 112 unique visitors; 664 clones / 131 unique cloners. These are
rolling-window totals, not lifetime usage. Repository About/homepage remained
aligned with CTX Fit. No merge, new release, paid evaluation, credential
rotation, repository-governance change, or LFS action was performed.
