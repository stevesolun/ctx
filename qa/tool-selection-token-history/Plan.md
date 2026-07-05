# Tool Selection And Token History Plan

Date: 2026-07-02

## Phase 0: Planning And Architecture Map

Scope: planning artifacts only.

Files:

- `qa/tool-selection-token-history/SPEC.md`
- `qa/tool-selection-token-history/Plan.md`
- `qa/tool-selection-token-history/tracker.csv`

Validation:

```sh
/tmp/ctx-verify-venv/bin/python - <<'PY'
import csv
from pathlib import Path
path = Path("qa/tool-selection-token-history/tracker.csv")
rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
required = {
    "ID", "Workstream", "Feature", "User Story", "Expected Behavior",
    "Files/Modules", "Test Command", "Status", "Severity", "Attempts",
    "Last Checked", "Notes",
}
assert rows
assert set(rows[0]) == required
assert {row["ID"] for row in rows} >= {f"US-{i:03d}" for i in range(1, 9)}
print(f"tracker rows: {len(rows)}")
PY
```

Success criteria:

- Spec names observed code paths and missing pieces.
- Plan breaks implementation into bounded milestones.
- Tracker has all core user stories.

## Phase 1: Shared Recommendation Selection Contract

Scope: backend/core only, no dashboard changes.

Candidate files:

- `src/ctx/core/resolve/recommendations.py`
- `src/ctx/adapters/generic/ctx_core_tools.py`
- `src/ctx/api.py`
- `src/tests/test_recommendations.py`
- `src/tests/test_public_api.py`

Work:

- Add stable row enrichment helpers for recommendation id, TL;DR, reason, selected/default state, and entity identity.
- Preserve old `recommend_bundle()` list-return compatibility.
- Add explicit enriched API/toolbox operation or opt-in argument rather than changing all callers silently.

Validation:

```sh
/tmp/ctx-verify-venv/bin/python -m pytest -q -p no:cacheprovider src/tests/test_recommendations.py src/tests/test_public_api.py src/tests/test_recommendation_surfaces_golden.py
/tmp/ctx-verify-venv/bin/python -m ruff check src/ctx/core/resolve/recommendations.py src/ctx/adapters/generic/ctx_core_tools.py src/ctx/api.py
/tmp/ctx-verify-venv/bin/python -m mypy src
```

## Phase 2: Related Recommendations

Scope: graph/backend, then API/LoopFlow exposure.

Candidate files:

- `src/ctx/core/resolve/recommendations.py`
- `src/ctx/adapters/generic/ctx_core_tools.py`
- `src/ctx/api.py`
- `src/ctx/adapters/loopflow.py`
- `src/tests/test_recommendations.py`
- `src/tests/test_loopflow_adapter.py`

Work:

- Add a related recommendation function based on selected seeds, graph neighbors, categories/tags, or existing `graph_query`.
- Exclude selected and rejected identities.
- Explain reasons with shared tags/graph-neighbor evidence.
- Expose through API/core toolbox and LoopFlow/agent-loop payloads.

Validation:

```sh
/tmp/ctx-verify-venv/bin/python -m pytest -q -p no:cacheprovider src/tests/test_recommendations.py src/tests/test_loopflow_adapter.py src/tests/test_harness_recommendations.py
/tmp/ctx-verify-venv/bin/python -m ruff check src/ctx/core/resolve/recommendations.py src/ctx/adapters/generic/ctx_core_tools.py src/ctx/api.py src/ctx/adapters/loopflow.py
/tmp/ctx-verify-venv/bin/python -m mypy src
```

## Phase 3: Activation And Token Usage Persistence

Scope: runtime lifecycle store plus telemetry metrics.

Candidate files:

- `src/ctx/adapters/generic/runtime_lifecycle.py`
- `src/ctx/adapters/generic/ctx_core_tools.py`
- `src/ctx/telemetry/__init__.py`
- `src/tests/test_harness_ctx_core.py`
- `src/tests/test_enterprise_telemetry.py`

Work:

- Add selection/activation metadata to lifecycle records.
- Add token usage record support with exact/estimated/unavailable measurement.
- Preserve local runtime history even when telemetry export is disabled; do not make required activation history depend on exporters.
- Emit OTel-style counters/histograms for usage when metrics are enabled.
- Reuse existing sanitization and safe session/entity validation.

Validation:

```sh
/tmp/ctx-verify-venv/bin/python -m pytest -q -p no:cacheprovider src/tests/test_harness_ctx_core.py src/tests/test_enterprise_telemetry.py
/tmp/ctx-verify-venv/bin/python -m ruff check src/ctx/adapters/generic/runtime_lifecycle.py src/ctx/adapters/generic/ctx_core_tools.py src/ctx/telemetry/__init__.py
/tmp/ctx-verify-venv/bin/python -m mypy src
```

## Phase 4: CLI And Loop Runtime Integration

Scope: user-facing CLI and ctx run attribution where exact usage exists.

Candidate files:

- `src/ctx/cli/recommend.py`
- `src/ctx/cli/run.py`
- `src/ctx/adapters/generic/state.py`
- `src/tests/test_recommend_cli.py`
- `src/tests/test_harness_cli_run.py`

Work:

- Add explicit selection mode to `ctx-recommend`.
- Add JSON contract for selected/rejected/related recommendations.
- Attribute exact usage to activated ctx entities only when the host/provider or session log provides unambiguous per-tool correlation.
- Keep aggregate run usage as session-level history and label unavailable per-tool attribution honestly.

Validation:

```sh
/tmp/ctx-verify-venv/bin/python -m pytest -q -p no:cacheprovider src/tests/test_recommend_cli.py src/tests/test_harness_cli_run.py src/tests/test_harness_state.py
/tmp/ctx-verify-venv/bin/python -m ruff check src/ctx/cli/recommend.py src/ctx/cli/run.py src/ctx/adapters/generic/state.py
/tmp/ctx-verify-venv/bin/python -m mypy src
```

## Phase 5: Dashboard And API Views

Scope: monitor pages/services/API only.

Candidate files:

- `src/ctx/monitor/services/runtime.py`
- `src/ctx/monitor/pages/activity.py`
- `src/ctx/monitor/pages/ops.py`
- `src/ctx/monitor/api/readonly.py`
- `src/ctx/monitor/routes.py`
- `src/tests/test_ctx_monitor.py`
- `src/tests/test_monitor_testing_api.py`
- `src/tests/test_dashboard_smoke.py`

Work:

- Add aggregation service for per-tool usage history.
- Add JSON API for usage KPIs/history.
- Add dashboard view or embed in Runtime/KPIs with populated, empty, unavailable, and error states.
- Aggregate token metadata only; never render raw transcripts, raw tool arguments, raw tool outputs, or raw recommendation queries.
- Keep monitor UI consistent with existing cards/tables/native controls.

Validation:

```sh
/tmp/ctx-verify-venv/bin/python -m pytest -q -p no:cacheprovider src/tests/test_ctx_monitor.py src/tests/test_monitor_testing_api.py src/tests/test_dashboard_smoke.py
/tmp/ctx-verify-venv/bin/python -m ruff check src/ctx/monitor/services/runtime.py src/ctx/monitor/pages/activity.py src/ctx/monitor/pages/ops.py src/ctx/monitor/api/readonly.py src/ctx/monitor/routes.py
/tmp/ctx-verify-venv/bin/python -m mypy src
```

## Phase 6: Docs And Tracker Sync

Scope: docs and QA status.

Candidate files:

- `docs/dashboard.md`
- `docs/telemetry.md`
- `docs/harness/attaching-to-hosts.md`
- `docs/harness/loopflow-adapter-demo.md`
- `qa/tool-selection-token-history/tracker.csv`
- `qa/feature_status.csv`

Validation:

```sh
/tmp/ctx-verify-venv/bin/python -m pytest -q -p no:cacheprovider \
  src/tests/test_bug_smoke_tracker.py \
  src/tests/test_feature_user_story_tracker.py \
  src/tests/test_dashboard_user_story_tracker.py \
  src/tests/test_toolbox_cli.py
/tmp/ctx-verify-venv/bin/python -m mkdocs build --strict
```

## Phase 7: Final Quality Gate

Validation:

```sh
/tmp/ctx-verify-venv/bin/python -m ruff format --check src hooks scripts
/tmp/ctx-verify-venv/bin/python -m ruff check src hooks scripts
/tmp/ctx-verify-venv/bin/python -m mypy src
/tmp/ctx-verify-venv/bin/python -m pytest -q
/tmp/ctx-verify-venv/bin/python scripts/ci_preflight.py --profile pr
no-mistakes axi run --intent "Implement interactive tool selection, related-tool recommendations, activation tracking, and per-tool token usage history across ctx API, CLI, MCP/core toolbox, LoopFlow adapter, and dashboard while preserving enterprise privacy and existing recommendation compatibility."
```

Success criteria:

- All tracker rows are `Pass` or explicitly `Blocked`.
- Focused tests pass for each milestone.
- Full pytest and CI preflight pass.
- no-mistakes reaches `checks-passed` or `passed`.

## CTO Parallelization Rules

- Keep planning and orchestration in the main branch workspace.
- Use read-only explorer agents for module maps.
- Use worker agents only after Phase 0 when write sets are disjoint.
- Do not let workers edit the same file in parallel.
- Commit after each milestone that passes its validation.
