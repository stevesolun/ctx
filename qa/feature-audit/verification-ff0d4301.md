# Final repair verification — 2026-10-04

Verified product/documentation commit:
`ff0d4301885f0888dfd8e23104291141538cc4f8`.
Delivery: [PR #286](https://github.com/stevesolun/ctx/pull/286), unmerged and
unreleased. This report records evidence, not a second status ledger;
`qa/feature_status.csv` remains authoritative. Later evidence-only changes do
not change the source snapshot named here.

## Repairs and independent review

- Telemetry checkpoint generation history preserves temporary recovery while
  recognizing deliberate rotation and restoration of older generated salts.
  The final legacy guard also refuses unmatched identity when a configured
  fallback was replaced, even if all current candidates are available.
  Restore the original fallback or explicitly replay with `--all`; removing
  historic fallback configuration cannot reconstruct lost provenance.
  See `telemetry-fallback-repair-20261004.md` for the failing-first regression,
  exact hashes and independent source review.
- Benchmark early-arm rejection uses deterministic package-inventory fixture
  data; real rejection and forbidden-process/workspace assertions remain.
- Safe-example telemetry supplies an explicit fixture salt, and browser
  fixtures isolate lifecycle history. Both behavioral negative controls failed
  before repair. See `isolation-repair-20261004.md`.
- A class-local clock fixture fixes collection-to-execution UTC midnight
  rollover without changing production today-only filtering. Telemetry docs
  now use actual exported attribute names and distinguish log durations from
  optional manually recorded histograms. See
  `context-clock-telemetry-docs-20261004.md`.
- Final documentation review corrected same-invocation Fit apply/PR consent,
  canonical tracker ownership, recommendation-path distinctions and dashboard
  timelines. The three Python changes in ff0d4301 are docstrings only.

The native full-range review and its explicit re-review found no remaining
material findings after the isolation repairs. Bounded independent source,
documentation and acceptance reviewers checked the relevant contracts. Agent
self-reports were corroborated with actual logs, diffs, HTTP outputs, rendered
screenshots and GitHub job/step records.

## Local verification

All checks used the existing trusted repository environment; no dependency
upgrades, skips added for convenience, or paid model runs were used.

- d92ae437 authoritative preflight: all 20 checks passed; unit selection
  **9,442 passed, five skipped, 15 warnings in 574.39s**, **92.09% coverage**.
  This precedes the final documentation-only commit and is historical, not
  the exact-final preflight receipt below.
- ff0d4301 committed fast gate: all **11 lanes passed**, return code 0,
  clean committed selection, two workers, **373.931s** total. Unit selection:
  **9,442 passed, five skipped, 15 warnings in 304.14s**, **92.09% coverage**.
  Browser: **20 passed in 26.67s**. Static, canary, contracts, real clean-host,
  docs, telemetry, similarity and reproducible packaging also passed.
  Receipt: native worktree `.gate/local-fast.json`; raw log
  `/tmp/ctx-feature-audit-fast-final-ff0d4301.log`.
- Exact-final serial preflight: **all 20 checks passed**, terminal exit 0.
  It began only after the final fast gate terminated, on unchanged ff0d4301.
  Unit: **9,442 passed, five skipped, 15 warnings in 288.24s**, **92.09%
  coverage**; browser: **20 passed in 25.93s**. Static, live inventory,
  dependency, canary, clean-host, documentation, telemetry, similarity and
  reproducible wheel/sdist/Twine checks all passed. Raw log:
  `/tmp/ctx-feature-audit-preflight-final-ff0d4301.log`.
  Wheel SHA-256: `c8e164e53d2dcf2ce44659083cecf392d3c5354dd455e69f5459850fe323d1ca`;
  sdist: `1732da831dae7f3a78ebc9f343c10199ba948e54a5b664667bef67b35a39df74`.

Previous failed attempts remain in the earlier reports and operational state:
ambiguous fallback replay, optional-dependency fixture imports, unsafe fixture
storage, an archive symlink rejected by regular-file validation, and the UTC
rollover. They were not erased or treated as passes through repetition.

## Actual user-facing checks

The native test phase ran a real loopback HTTP collector, not a transport mock.
For events, metrics and traces, both recovery routes were exercised:

- Ambiguous preview/export returned an actionable identity error, made zero
  HTTP requests and preserved the legacy checkpoint; preview was read-only.
- Restoring the original fallback migrated with no replay, then exported one
  subsequent record. Explicit `--all` replayed the backlog once, then exported
  the next record once. No pending records remained after acknowledgment.
- CLI subprocesses proved preview, export, explicit replay and subsequent
  no-op behavior. Actual exported attributes match the corrected docs.

Retained raw evidence directory:
`/var/folders/cj/j956f9v920b8wk3wvd8ms2nh0000gn/T/no-mistakes-evidence/01M4229GT0BD5FAWR71CFQFHNX/`.
Relevant files: `telemetry-http-results.json`, `telemetry-cli-transcript.txt`,
`telemetry_http_probe.py`, and the dashboard screenshot/replay artifacts.
These apply to unchanged executable behavior between d92ae437 and ff0d4301.

On exact ff0d4301, strict MkDocs succeeded and isolated headless Chromium
inspected the knowledge-graph and telemetry pages. Both returned HTTP 200,
required content was visible, **431 local links/anchors** resolved (132 graph,
299 telemetry), and neither page raised a script error. The coordinator
visually inspected all three screenshots. External API/font requests were
blocked; the temporary loopback server and browser exited normally. This is
local-build evidence, not deployed Pages acceptance.

Final script, result and screenshots: `/tmp/ctx-final-telemetry.S1DCM6/`.

| Page | Source SHA-256 | Rendered SHA-256 |
| --- | --- | --- |
| Knowledge graph | `3a0c42e67ccdc3f4b82f198183829b9a5b25753784473c63611dd31e808a2546` | `c2ffd71af339a3d58820ad8dfac95074f57c03cbb708ea5a1d8bf806619a44cc` |
| Telemetry | `dc07e7645f491863fc13713486f716600949f4b979f6041d9b56a0d5ded0a8b8` | `369aa1b546780e60a35fdbc08095bac43d4a879be15e67087361e49a510a9f3b` |

Earlier source-backed safe graph/telemetry examples remain applicable where
their source hashes are unchanged. No additional graph promotion, paid model,
production collector deployment or remote publication is implied.

## Hosted delivery

No-mistakes run `01M4229GT0BD5FAWR71CFQFHNX` returned **checks-passed**, with
intent, rebase, review, test, document, lint, push and PR phases completed and
CI green. Its background human-merge monitor is not an unfinished repair.

[Tests run 37165295905](https://github.com/stevesolun/ctx/actions/runs/37165295905)
ran on exact ff0d4301. Across all PR workflows, **22 checks succeeded**, with
two intentional classifier skips (broad post-merge matrix and unchanged graph
artifact lane). The actual Linux unit job passed **9,339 tests, 51 skipped in
424.15s**, **91.26% coverage**. Its dependency/platform selection differs from
the local selection; these counts are not interchangeable.

Actual successful jobs and steps, not merely aggregate badges:

- Clean-host contract: `111326886514`.
- Wheel build and upload: `111326886497`.
- macOS wheel install/console smoke: `111326949287`.
- Ubuntu wheel install/console smoke: `111326949341`.

Artifact `11289561576`, `package-smoke-wheel`, **1,541,024 bytes**, exists for
this head and was unexpired when checked; retained October 4–11, 2026.
It is a short-lived CI artifact, not a published release.

## Remaining authorization boundaries

The six publication-dependent rows remain Needs Validation:
`DOC-002`, `DOC-NAV-002`, `DIST-003` (deployed Pages/catalog), `DIST-004`
(authorized Hugging Face sync and remote proof), and `DIST-006` /
`LANE-D-023` (authorized release upload, attestation and publication).

`MAINT-017` remains Blocked/Human Decision for an approved OCR endpoint,
credentials and actual probe/review. `DIST-013` remains Blocked/Human Decision
for the owner/independent-reviewer governance decision and live required-review
ruleset evidence. No release, merge, paid run, credential rotation, governance
change or LFS/Support action was taken to manufacture acceptance.

GitHub traffic read October 4 still reports the September 19–October 2 window:
**335 views, 112 unique visitors; 664 clones, 131 unique cloners**. These are
rolling-window counts, not lifetime totals. Issue #283 has the already-verified
AI-labeled draft-PR update; it remains open and unreleased.
