# Repaired audit verification — 2026-09-30

Source checkpoint: `05d16854e04cd1dcf1f7f3c5e9f5c4cdebae0939`.
Canonical story status remains in `qa/feature_status.csv`; this is supporting
execution evidence, not another tracker. No new release or paid model run is
claimed.

## Committed fast gate

`PYTEST_ADDOPTS=-ra scripts/no_mistakes_run.sh fast --allow-dirty --jobs 4`
completed successfully in isolated checkouts of the exact checkpoint. All 11
lanes passed. The unit selection passed **8,977 tests**, skipped five, and
reached **92.02% coverage** in 296.59 seconds. The five skips are the explicitly
opt-in real-user wiki check and four unsupported native-Windows contracts.

Other lanes passed policy/stats, Ruff/format/mypy/dependency integrity, 24
canaries, 57 compatibility tests, real isolated wheel installation, 31 public
documentation contracts plus strict MkDocs, 103 selected telemetry tests,
real similarity evaluation, 19 browser tests, reproducible packaging, and Twine.

Reproducible package SHA-256:

- Wheel: `0186d19ff33fb72605619cc21ff43d294c475ccfc88711bd718b270a076ef0a9`
- Sdist: `544698fafd063553539027923a3fca0c4a874c084eb2ccb6ceff0a4bb7260e16`

The previous failure and its independent diagnosis remain recorded in
`verification-2f7a6a23.md`; this successful run does not erase that evidence.
Operational log: `/tmp/ctx-feature-audit-fast-05d16854.log`.

## Additional coordinator checks

- The final homepage was rebuilt strictly and opened in the browser. Its
  bundled-MCP prerequisite, host-owned observations, and host-owned budget
  enforcement are rendered correctly; all homepage anchors resolve.
- All 3,407 local links and anchors across 29 final navigation pages resolve.
  This is local evidence; the deployed catalog still needs the CSS correction.
- The three portable documentation scripts executed all 79 examples again.
  Two deliberately retained old broken examples reproduce their historical
  defects; they are not failures of the corrected examples. Lifecycle dry-run
  changed no fixture files. All 29 retained source/prose hashes still match.
- The safe graph/telemetry reproducer passed actual graphify, vector-index
  persistence, attach, staged compaction, preview, incremental export and replay.
  No external call, user-home change, or active-pack promotion occurred.
- The bounded dashboard rerun passed 24 HTTP checks. Graph cold/warm:
  1.138677/0.001799 seconds; 10,000-sidecar KPI cold/warm:
  0.878468/0.029550 seconds; KPI page: 0.029878 seconds. All recorded limits
  passed. This is not a full-catalog KPI latency guarantee.
- Tracker/surface tests passed 37 checks; after 39 evidence-backed checklist
  dispositions, both tracker suites passed 16 checks. Expected behaviors were
  unchanged, and partial external evidence did not receive pass status.
- Live GitHub readback: strict `CI required` protection exists without required
  reviewer approval. About matches CTX Fit. September 15–28 traffic is 303 views
  from 102 unique visitors and 698 clones from 139 unique cloners. The API did
  not yet include September 29–30. No new reporter replies were present.

## Remaining verification boundary

Authoritative PR preflight, hosted CI, deployed Pages, and the final issue/PR
handoff remain. Optional external host handshakes, SkillSpector, approved OCR
credentials, reviewer-rule changes, and a new release/publication are not
implied by the successful local gate. The LFS Support request remains unsent.
