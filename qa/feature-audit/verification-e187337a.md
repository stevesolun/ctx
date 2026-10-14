# Final local audit verification — 2026-09-30

Tested checkpoint: `e187337a95ef337a777484a21391a3a19144d89e`.
This report is supporting evidence; `qa/feature_status.csv` is the sole status
authority. At that checkpoint, production source was unchanged since the
successful `05d16854` fast gate and the only untracked tree was the preserved
user-owned `.scratch/`. Later changes are distinguished below.

## Authoritative PR preflight

`PYTEST_ADDOPTS=-ra .venv/bin/python scripts/ci_preflight.py --profile pr`
completed with exit 0. All 20 selected checks passed, following successful
proportional cheap/docs lanes on this checkpoint and all 11 fast lanes on
`05d16854`. The complete preflight log is
`/tmp/ctx-feature-audit-preflight-e187337a.log`.

Checks covered whitespace, generated statistics, no-test policy, Ruff format
and lint, mypy, dependency integrity, exact-manifest runtime hydration, the
unit selection, A–Z canaries, package compatibility, actual isolated clean-host
installation, public documentation trackers, strict MkDocs, telemetry,
similarity, browser security, reproducible distributions, and Twine.

The unit selection passed **8,977 tests**, with five skips and 15 deprecation
warnings, in 283.02 seconds. Coverage was **92.02%**. Skips were one opt-in
real-user wiki check and four unsupported native-Windows contracts. The
separate integration, deep release-archive, documentation example, and rendered
link evidence remains in the preceding reports; local macOS success is not
substituted for hosted Linux execution.

Reproducible package SHA-256:

- Wheel: `d28e48bf20cf61863047b28caaf39f2ed4f0c0f30d512259b7ee37a7015482af`
- Sdist: `92f09422038a396fa597de15ceb256cc595a80802bd7bffa3a41ee5d08fbe739`

## Installed-wheel dashboard supplement

The coordinator independently replayed the installed-wheel monitor probe,
after reading its reproducer. A fresh temporary virtual environment installed
a wheel built from the tested checkpoint, with temporary HOME/XDG/Codex
directories and no source-tree `PYTHONPATH`. Both `ctx` and `ctx_monitor`
resolved inside the temporary environment's site-packages, not the checkout.

The probe reused the already-validated local dependency pool through a `.pth`
file; it does not claim a new clean dependency closure. The separate real
clean-host gate proves installation, init, scan, run/resume, denied tools, and
the context-monitor hook. This supplemental probe specifically exercises the
dashboard from the installed wheel rather than substituting source-tree tests.

| Route | Result | Content |
| --- | --- | --- |
| `/` | HTTP 200 | HTML, 38,113 bytes |
| `/status` | HTTP 200 | HTML, 38,700 bytes; status and telemetry-health markers |
| `/api/status.json` | HTTP 200 | JSON object, 4,285 bytes |
| `/api/sessions.json` | HTTP 200 | JSON list, 2 bytes; isolated empty sessions |

Every response excluded actual-home and checkout paths. The loopback server
was stopped and the disposable fixture removed. Source identity remained
unchanged before/after. The ad-hoc smoke wheel was 1,556,170 bytes with SHA-256
`6257765215395aa1c103d8d0e3aeb182a598a1f24d9d726bf367ff285783462d`;
this is distinct from the reproducible package build above.

Local reproducer/result: `/tmp/ctx-maint007-installed-monitor.py` and
`/tmp/ctx-maint007-root-result.json`. Raw local paths are deliberately not
copied into this public evidence report.

## Tracker reconciliation and remaining boundaries

Independent review found a metadata contradiction: 41 newly passed rows still
carried their initial mapping status and obsolete work requests. The
coordinator retained the initial mapping as explicitly historical context and
made each current review agree with its accepted result and limitations. The
ledger-integrity row no longer embeds totals that become stale whenever another
row advances. No user story, expected behavior, setup, or verification contract
was weakened.

## Real optional static-scanner check

The coordinator independently executed CTX `audit-tar` with official NVIDIA
SkillSpector 2.12.0 installed only in a disposable Python 3.12 environment.
The wheel's 665,509-byte size and published SHA-256 were verified before
installation:
`62973f6254d30c871480246869f88a01e17dff6f12e9d43010962eb0d7e305f4`.

The actual scan used macOS network denial, an empty temporary home, and an
explicit credential-free environment. One synthetic benign skill completed
with zero errors, `mode=static-no-llm`, `llm_requested=false`, the expected
content identity, and a non-error normalized record. The five audit/stamping
unit tests also passed. The CTX scanner source hash was unchanged before and
after the real scan. Exact command, fixture text, dependency versions,
provenance, results, hashes, and limits are retained with local paths redacted
in `skillspector-static-smoke-evidence.json`.

This corrects the earlier assumed blocker: `audit-tar` imports a Python
package, not the service executable, and ordinary isolated dependency setup
does not require a model, credentials, or scanning the user's skill corpus.
It proves this actual CTX integration path, not full-catalog security quality
or live online vulnerability coverage.

## Remaining boundaries

Hosted CI, deployed Pages, real third-party host handshakes, OCR credentials,
reviewer-policy changes, and a new release/publication remain distinct
requirements. The clean-host script integration repair and final metadata
changes postdate the checkpoint's successful full gates. No external
requirement is implied passed by these local gates. The LFS Support request
remains unsent pending the user's action-time confirmation.
