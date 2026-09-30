# Independent repair refreeze — 2026-09-30

This is evidence, not a second tracker. `qa/feature_status.csv` owns every
current story, defect, reproduction, disposition, and remaining requirement.
The implementation checkpoint is `e187337a`; the installed-dashboard helper,
host-guide clarification, and two tests described below are later changes.
Older green gates do not certify that later delta.

## Code and privacy review

The independent repair reviewer accepted seven additional families with no
P0–P3 findings: catalog hidden-card layout, systemd rate-limit section, mobile
long-content containment, telemetry preview writes, telemetry CLI path errors,
scanner guidance/output errors, and untrusted MCP frames. The reviewer ran
40 focused cases and three Chromium cases; every scoped implementation/test
file was byte-identical to `e187337a`. That checkpoint's coordinator-observed
20-check preflight also passed, including the complete unit and browser lanes.

The reviews traced the relevant behavior, not only test names:

- Hidden cards compute `display:none` and zero height under the real stylesheet;
  search/type filtering leaves one/four visible cards respectively.
- Restart-rate directives belong to `[Unit]`, without changing user scope or
  service hardening. No service was installed or activated.
- Long metadata fits the 390px home, session, and wiki browser views.
- Every telemetry preview path avoids creating a persistent salt/lock; explicit
  and existing salts still work. A fresh isolated HOME remained empty.
- Expected telemetry and scanner filesystem errors produce bounded nonzero
  CLI results instead of tracebacks.
- MCP framing validates envelopes, IDs, initialization and tool-result shapes;
  startup failures reap children, and diagnostics omit/redact untrusted data.

An earlier fresh independent registry/store pass also accepted malformed input
handling and SQLite lifecycle/file boundaries: six CLI error cases, 77 registry
tests, 25 store tests, and a real FIFO rejection passed. SQLite disappearance
remains normal, while FIFO and hardlink targets are rejected before mutation.
Coordinator review separately accepted the budget/strict-JSON guidance,
credential identity, apply recovery advice, and C-family detector scoping.
The coordinator ran 94 Fit CLI/surface/doctor/provider tests; the C-family
regression also passed in the `e187337a` aggregate.

Source SHA-256 identities retained for the independent passes:

| Source | SHA-256 |
| --- | --- |
| `docs/catalog.md` | `a40dd53a276085eebea726ee2f4ff98c90fc99695dfdb7a30afcd08d39371153` |
| `src/ctx/cli/telemetry.py` | `f200942df1c27fe728ff54807d1a324b35567b3b9f1d9a7528fe24e51e3e0f86` |
| `src/ctx/core/quality/skillspector_audit.py` | `1cb40dde18a7762a8dbef873516a4359d98a6772252619485f7329667a2aa78b` |

## Documentation review

An independent source-backed semantic pass accepted all eight families:
About positioning, toolbox contracts, host snippets, lifecycle arguments,
quality/memory claims, operations, router/registry boundaries, and front-door
safety guidance. A separate reviewer cross-checked lifecycle/quality/memory.
All 29 retained documentation/source hashes matched at this review; unchanged
examples were not repeatedly executed merely to generate another receipt.

The host guide now explicitly labels named-host recipes version-dependent and
unverified, and states configuration, process-start, initialize/list, and
read-only-call checks. Its explanatory contract is not a claim of live host
interoperability. The coordinator replayed all eight local host examples and
38 surface/tracker tests after the last prose correction. Retained host-guide
SHA-256: `1b457a79071b16284256585a0c315e12e4f57cab09ad9b1f186025bfc18fcc22`.

The generated README/docs inventory was refreshed to 8,988 tests. Full Ruff
lint and formatting, mypy (595 files), statistics freshness, and strict MkDocs
passed on the later delta. This inventory count is not the unit-lane pass
count. Final committed gates remain separately required.

## Actual installed-dashboard integration

The supplemental installed-wheel probe exposed a real omission: the production
clean-host script itself did not exercise dashboard HTTP. The repair adds
bounded loopback checks to the actual script immediately after wheel install,
using its isolated HOME and installed interpreter without a source-tree
`PYTHONPATH`. It checks `/`, `/status`, `/api/status.json`, and
`/api/sessions.json` for status, content type, content/shape, and host-path privacy.
Cleanup terminates and reaps the child on success or failure.

The writer's failing-first tests cover actual invocation and failure cleanup.
The coordinator independently ran all 18 clean-host tests and the real command:

```sh
.venv/bin/python -m pytest -q --no-cov src/tests/test_clean_host_contract.py
.venv/bin/python scripts/clean_host_contract.py --fast
```

Both passed. The second invocation built a wheel and installed fresh dependencies;
it did not reuse the earlier ad-hoc `.pth` dependency shortcut. Its local log is
`/tmp/ctx-clean-host-dashboard-root.log`. Script SHA-256:
`e3da253e652f42b4e933870fdac650943390ef429d4f48067fcdd059a211ab11`.
This is real installed HTTP evidence, not JavaScript/browser or live-Claude proof.

Independent final semantic review accepted this addition with no P0–P3 findings
after tracing environment isolation, installed imports, loopback bounds,
response privacy, and unconditional cleanup. The reviewer separately passed
the invocation and failure-cleanup tests, Ruff, mypy, and diff hygiene, without
repeating the coordinator's dependency installation or aggregate gates.

## Canonical record integrity and remaining requirements

Thirty-five current defect/record families are now attached to 65 canonical
rows with reproduction, fix, exact regression, reviewer, and limit fields.
Application checked SHA-256 of all original story, expected-behavior, setup,
verification command, and mode fields before editing. Earlier IDs and evidence
remain explicitly historical rather than being erased. Independent review
accepted the applied delta with no blocking findings: all 35 families mapped,
all 65 guarded contracts and historical records preserved, and every external
non-pass retained. Reviewed CSV SHA-256:
`f253c12f58bb8ce37efdea4facf84f2333e4ac3e166e9f270e1123eb110a6175`
(before recording this acceptance receipt). CLI-039's separate checklist
correction is supported by the real optional-scanner evidence; the preservation
claim is specifically for the 65 guarded contracts. Final committed gates remain.

Deployed catalog/Pages, hosted CI and package smoke, actual publication and
attestation, approved OCR/model execution, and independent-approval governance
are not proved by local checks. The named-host guide's honest explanatory
acceptance does not certify any external host. No release, paid model call,
repository setting change, or Support submission was made by these reviews.
