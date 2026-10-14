# Committed local verification — 2026-09-30

Tested commit: `53986b36a1c44d38d7ad174fef479fc99375f10a`.
Canonical status authority: `qa/feature_status.csv`.

```sh
scripts/no_mistakes_run.sh fast --allow-dirty --jobs 4
```

Terminal exit: **0**. All **11 lanes** passed in **341.916 seconds**, with
four isolated lanes running concurrently. The only untracked input was the
preserved user-owned `.scratch/`; `committed_head_only=true` means its contents
were not executed. The selection inventory can include untracked paths; that
does not put them in the temporary committed checkouts.

The CI-shaped unit selection passed **8,979 tests**, five documented skips and
15 warnings, with **92.03% coverage**, in 294.66 seconds. The remaining lanes
covered static/dependency integrity, generated facts and policy, 24 canaries,
58 compatibility contracts, fresh installed-wheel clean-host execution
(including real dashboard HTTP), 31 docs/tracker tests and strict MkDocs,
103 telemetry cases, similarity, 19 browser cases, reproducible packages and
Twine. Every lane returned zero. Skips remain the previously recorded optional
real-user wiki check and four unsupported native-Windows cases.

Reproducible SHA-256:

- Wheel: `caff6469521d7c4f40be73f42d1c474dff0a56f0539b255eed66c9311a6acebb`
- Sdist: `11bab73f6ae631256ec63a368ad3633bfd30f93a44fe4393f5f1efa11472d3e0`

Local raw evidence: `/tmp/ctx-feature-audit-fast-53986b36.log` and the
`head_sha=53986b36…` entry in `.gate/local-fast.json` (a later run may replace
that rolling summary). The coordinator additionally matched all 29 retained
documentation/source hashes and the 18-file evidence bundle before this report.

This closes the actual clean-host script/dashboard local contract and the
host-guide explanatory/rendering contracts, with their declared external-host
limits preserved. It does not replace authoritative serial preflight, the full
no-mistakes delivery sequence, or hosted CI. Those are next. Deployed Pages,
Hugging Face/release publication, live host/approved OCR execution, and owner
review-policy changes are not inferred from these local passes. No merge, new
release, paid model call, Support submission, or remote LFS purge occurred.
