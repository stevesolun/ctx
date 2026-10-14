# Context clock and telemetry documentation repair — 2026-10-04

Test-phase starting HEAD: `14045187f85606f0e2e9d725e26243421b74d13c`
(detached gate worktree for `codex/full-feature-audit-delivery`, draft PR #286).
This report records focused evidence for `test-1` and `DOC-1`; it does not
certify the full audit or a new committed gate.

## Reproduction and minimal repair

`test-1` / `AUDIT-20261004-CONTEXT-MONITOR-UTC-ROLLOVER` is a test clock
defect. `test_context_monitor.py` captures `TODAY` at module import, while
`load_recent_unmatched_count()` correctly reads the current UTC day at execution.
A collection-to-execution midnight rollover therefore excludes the fixture data.

Before editing, a temporary pytest driver replaced `datetime.datetime` with a
subclass whose `now()` returned `2026-10-04T23:59:59Z` during collection. Its
`pytest_collection_finish` hook asserted the imported `TODAY == "2026-10-04"`
and advanced the same clock to `2026-10-05T00:00:01Z`. It ran exactly:

```text
src/tests/test_context_monitor.py::TestLoadRecentUnmatchedCount::test_happy_path_counts_distinct
src/tests/test_context_monitor.py::TestLoadRecentUnmatchedCount::test_skips_bad_lines
```

With `-q --no-cov -p no:cacheprovider`, both original assertions failed:
**2 failed in 0.30s**, returning 0 instead of 2 and 1. The same driver after
the repair returned **2 passed in 0.23s**. Raw output is retained in
`.gate/context-midnight-red.log` and `.gate/context-midnight-green.log`.

The only test change adds a class-local autouse fixture that monkeypatches the
consumer clock to the existing fixture date. All test assertions, missing-file,
distinct-counting, malformed-line and other-date cases remain unchanged.
Production UTC today-only filtering is unchanged. No test cases were added or
removed, so this repair does not change the existing 9,452-test inventory.

`DOC-1` is a documentation/schema mismatch. `src/ctx/api.py` records operation
and status in event payloads and latency in `duration_ms`.
`src/ctx/telemetry/__init__.py` exports those as `ctx.payload.ctx.operation`,
`ctx.payload.otel.status_code` and `ctx.duration_ms`; exception dimensions get
the same payload prefix. The corrected dashboards and alert use these exported
keys or `ctx.outcome = error`. Automatic API latency uses log-duration
aggregation. The existing manual `record_histogram("ctx.api.duration", ...)`
example is unchanged; optional operation grouping uses the actual metric key
`ctx.metric.ctx.operation`. No exporter or instrumentation code changed.

## Focused verification

Used the existing trusted interpreter
`/Users/steves/Steves_Files/Work/Research_and_Papers/ctx/.venv/bin/python`
from this worktree, with `PYTHONDONTWRITEBYTECODE=1`. The focused pytest
selection used `CTX_TELEMETRY_HASH_SALT=test-phase-fixture` to avoid ambient
salt-file setup, plus `-q --no-cov -p no:cacheprovider`:

- `src/tests/test_context_monitor.py::TestLoadRecentUnmatchedCount` (4 cases)
- `src/tests/test_public_api.py::TestApiTelemetry` (5 cases)
- `src/tests/test_enterprise_telemetry.py::test_export_events_posts_otlp_http_payload`
- `src/tests/test_enterprise_telemetry.py::test_record_exception_hashes_message_and_stack_for_otlp`
- `src/tests/test_enterprise_telemetry.py::test_export_metrics_posts_otlp_resource_metrics`

Result: **12 passed in 12.48s**, exit 0;
`.gate/context-telemetry-focused.log` retains the terminal output.

A separate isolated probe recorded one exception event and one manual
histogram, then ran the real exporters with only HTTP transport intercepted.
It asserted the documented attribute names and absence of unprefixed log keys.
Observed output included:

```json
{
  "ctx.payload.ctx.operation": "recommend_bundle",
  "ctx.payload.otel.status_code": "ERROR",
  "ctx.payload.ctx.exception.type": "builtins.ValueError",
  "ctx.outcome": "error",
  "ctx.source": "ctx-api",
  "ctx.duration_ms": 12.5
}
```

The fingerprint used `ctx.payload.ctx.exception.fingerprint`; the manual
histogram exported `ctx.metric.ctx.operation = recommend_bundle`. Exit 0,
zero network calls; `.gate/telemetry-doc-output.log`. The initial probe had an
incorrect expected exception type (`ValueError` rather than the existing
qualified `builtins.ValueError`); that probe assertion was corrected, with no
product/test change. Its failure is retained in
`.gate/telemetry-doc-output-initial.log`. Temporary spools were removed.

Independent read-only review traced both changed files against their consumers
and exporters and found no material issue. It did not claim test execution or
outer fix-review approval.

## Frozen source and acceptance boundary

| File | SHA-256 |
| --- | --- |
| `src/tests/test_context_monitor.py` | `ca8ea4977cc3bf43c009f719d91a6f5b498b64723c6452d8463c4c6c32b088a2` |
| `docs/telemetry.md` | `14af0ad600f0bae8dc1d34d059b42e3af738e289a019befac3472d8c9b91cd3d` |
| `src/ctx/adapters/claude_code/hooks/context_monitor.py` (unchanged) | `b8343642be44e82b461ffbf60f5cd947342fc38a52ac51aa2b858388a3f56d9d` |
| `src/ctx/api.py` (unchanged) | `25592e8ba8e238b742b9dc805116e1a1dfe759c56ff5ff7f813fea0b4a04b893` |
| `src/ctx/telemetry/__init__.py` (unchanged) | `3ba4921a2b64de362259fe94d884300da5e1eef5fa8f44111518f53f8954fba1` |

All 318 original tracker contracts, prior defect IDs and historical notes are
preserved. Only B-ADAPT-002, DOC-NAV-007 and LANE-D-036 receive new evidence.
B-ADAPT-002 is reopened as Needs Validation with its former acceptance retained
verbatim in notes; both telemetry documentation rows remain Needs Validation.
Formal acceptance fields stay empty pending the original acceptance checks.
Other owner/external prerequisites and pending rows are unchanged.

The tracker authority, schema/freshness and machine-readable verification
contract tests passed: **3 passed in 0.20s**, exit 0, using the same targeted
pytest options (`.gate/context-telemetry-tracker.log`). A direct comparison
against HEAD also confirmed unchanged original contract fields for all 318
rows, preserved prior defect IDs and notes, and exactly those three changed rows.

No full suite, fast gate, preflight, lint, formatting, type/static analysis,
rendered-page build, pipeline control, dependency installation, archive change,
commit, push, PR mutation, merge, release, or paid call ran in this phase.
Current rendered-page checks, committed gates and delivery remain pending with
the outer executor; the PR remains draft. Earlier gate results apply only to
their recorded source. Source is frozen at the hashes above for handoff.
