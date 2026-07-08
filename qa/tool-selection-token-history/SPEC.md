# Tool Selection And Token History Spec

Date: 2026-07-02

## Objective

Add interactive recommendation selection, related-tool recommendations, activation tracking, and per-tool token usage history across the Python API, MCP/core toolbox, CLI, LoopFlow/agent-loop adapter, and local monitor dashboard.

The target user is a programmer or AI engineer who receives ctx recommendations for skills, agents, MCP servers, and harnesses. They must be able to understand the recommendations quickly, choose what to activate, get related suggestions after partial selection, and inspect token KPIs over time for selected/activated tools.

## Existing Context Suggestion Flow

Observed entry points:

- `src/ctx/api.py` exposes `recommend_bundle(query, top_k=5)` and `recommend_related(selected, rejected=None, max_hops=2, top_n=5)` as the public Python API. They wrap the shared ctx-core tools and return lists of recommendation dicts.
- `src/ctx/adapters/generic/ctx_core_tools.py` exposes `ctx__recommend_bundle`, `ctx__recommend_related`, `ctx__graph_query`, `ctx__wiki_search`, `ctx__wiki_get`, and runtime lifecycle/session tools through `CtxCoreToolbox`.
- `src/ctx/core/resolve/recommendations.py` provides `recommend_by_tags(...)`, which ranks graph entities by slug-token, tag overlap, graph degree, explicit entity match, optional semantic query score, and external catalog fallback.
- `src/ctx/cli/recommend.py` provides `ctx-recommend`; text mode renders enriched rows plus related rows when `--selected` is passed, and `--json` emits `{"query": ..., "results": [...]}` with a `selection` object when selected or rejected IDs are supplied.
- `src/ctx/mcp_server/server.py` exposes the shared toolbox through MCP `tools/list` and `tools/call`.
- `src/ctx/adapters/loopflow.py` emits permission-gated LoopFlow/agent-loop adapter payloads containing grouped `skills`, `agents`, `mcps`, `harnesses`, and ctx MCP metadata.

Current recommendation result shape is list-oriented. Rows may include `id`, `name`, `type`, `score`, `normalized_score`, `matching_tags`, `tldr`, `reason`, `selected`, `selection_state`, `external`, `source_catalog`, `status`, `source`, `skill_id`, `installs`, `detail_url`, `install_command`, `category`, `invoke_command`, and `security_review`.

## Existing Tool Model

Supported recommendable entity types are shared through `ctx.core.entity_types.RECOMMENDABLE_ENTITY_TYPES`; current flows use:

- `skill`
- `agent`
- `mcp-server`
- `harness`

The current `ctx__recommend_bundle` core tool recommends `skill`, `agent`, and `mcp-server`. Harnesses are recommended as companion rows when model/provider context exists, and LoopFlow can request harness recommendations only when explicit user-owned model consent is supplied through `own_llm`; `model_provider` and `model` are ranking metadata.

## Existing Knowledge Graph Model

The recommendation graph is loaded by `CtxCoreToolbox._ensure_graph()` from the llm-wiki graph artifact. `recommend_by_tags(...)` uses:

- query-derived tags from `query_to_tags`
- node labels and slug tokens
- node tags and matching tags
- graph degree
- optional semantic-cache query scoring
- status and `never_load` filtering
- external skill catalog fallback when available

Related-tool recommendations should reuse this graph. For selected rows, the minimal stable path is to call or share `ctx__graph_query` / graph-neighbor logic using selected entity names as seeds, then filter out selected and rejected identities.

## Existing Token Accounting Model

Observed exact usage exists at the generic harness level:

- `src/ctx/adapters/generic/providers/base.py` defines `Usage(input_tokens, output_tokens, cost_usd)`.
- `src/ctx/adapters/generic/loop.py` sums `CompletionResponse.usage` into `LoopResult.usage`.
- `src/ctx/cli/run.py` emits aggregate run usage in JSON and stderr output.
- `src/ctx/adapters/generic/state.py` persists per-model-response usage and reconstructs session totals on resume.

Observed lifecycle/telemetry persistence:

- `src/ctx/adapters/generic/runtime_lifecycle.py` persists host-neutral lifecycle events under `~/.ctx/runtime/events.jsonl` or `CTX_RUNTIME_LIFECYCLE_DIR/events.jsonl`.
- `RuntimeLifecycleStore` already records `load_entity`, `mark_entity_used`, `unload_entity`, `record_validation`, `record_escalation`, and `session_state`.
- `src/ctx/telemetry/__init__.py` provides privacy-safe local event and metric spools, `record_event`, `record_counter`, `record_histogram`, `read_events`, and `read_metrics`.
- Legacy `src/skill_telemetry.py` records load/unload events for skills, agents, and MCP servers under `~/.claude/skill-events.jsonl`.

Implemented pieces and remaining caveats:

- Recommendation rows expose the shared selection-state contract.
- User-selected vs system-selected activation flags persist in the runtime lifecycle ledger.
- Per-tool token attribution is stored when the host can provide exact, estimated, or unavailable evidence.
- Dashboard/API aggregation of per-tool token history is implemented through the Runtime page and `/api/runtime.json`; final-loop validation covers the browser/API privacy contract.
- Exact token counts are available for ctx generic harness runs, but not necessarily for arbitrary external MCP/host tool invocations. Unavailable or estimated states must be explicit.
- Exact token counts are currently provider/session-level, not per-tool. A per-tool usage row may be marked `exact` only when the host/provider supplies usage for that specific tool or when the model/tool boundary is unambiguous and recorded with correlation evidence. Session totals must not be split across tools as if they were exact.

## Required UX Behavior

Dashboard and any browser UI must:

- Show a selectable control for each recommendation.
- Display a short TL;DR description and relevance reason for each row.
- Make selected, rejected, and suggested-related states clear.
- Support select all, select none, and subset selection where the surface allows selection.
- Show related recommendations after partial selection.
- Show per-tool usage KPIs and history with empty, unavailable, and error states.
- Reuse existing monitor page, table, card, and checkbox styling rather than introducing a new frontend framework.

## Required CLI Behavior

CLI must:

- Keep existing `ctx-recommend` behavior compatible by default.
- Add an explicit selection mode for users who want to choose recommendations.
- Offer a non-interactive JSON mode exposing the same selection and related-recommendation semantics.
- Show TL;DR descriptions and relevance reasons.
- Record activation/selection only when the user confirms or when a caller explicitly passes selected identities.
- Clearly label session usage as session-scoped and per-tool attribution as exact, estimated, or unavailable when usage evidence is recorded.

## Required API And Backend Behavior

Shared backend behavior must:

- Add a canonical recommendation row enrichment with `id`, `name`, `type`, `tldr`, `reason`, `selected`, and `selection_state` where appropriate.
- Add a related recommendation operation that accepts selected and rejected identities and returns filtered related rows.
- Add activation tracking that records entity id/name/type, time, privacy-safe session/user/system context, selected-vs-system source, and optional source context.
- Add token usage recording that accepts exact usage when available and unavailable/estimated metadata otherwise.
- Keep run/session token totals distinct from per-tool token rows when attribution is unavailable.
- Preserve old `recommend_bundle` list return semantics for Python callers unless a new explicit API asks for enriched state.

## Required Dashboard Behavior

Dashboard must:

- Surface selectable recommendation rows or a dedicated recommendation-selection view.
- Show per-tool token totals and recent usage.
- Show history grouped by tool and user/session/system context where available.
- Distinguish user-selected and system-selected activations.
- Reuse `/kpi`, `/runtime`, `/sessions`, or a nearby monitor page pattern instead of creating a separate app.
- Expose JSON API endpoints for dashboard data.

## Data Model

Preferred persisted record shape for activation and usage:

- `action`: `load_requested`, `used`, `unload_requested`, `validation`, `escalation`, `session_end`, or compatible lifecycle action.
- `session_id`: safe host-generated id; use existing validation and hashed telemetry identifiers.
- `entity_type`: one of `skill`, `agent`, `mcp-server`, `harness`.
- `slug` or `name`: safe entity identity.
- `selection_source`: `user`, `system`, `host`, or `unknown`.
- `selected`: boolean where relevant.
- `source_context`: bounded sanitized object.
- `token_usage`: object with `input_tokens`, `output_tokens`, `total_tokens`, optional `cost_usd`, `provider`, `model`, `attribution`: `exact`, `estimated`, or `unavailable`, and optional `attribution_reason`.
- `correlation_id`: optional safe id linking model responses, tool calls, and lifecycle records when attribution is exact.
- `created_at` and `created_at_epoch`.

Implementation decision: extend `RuntimeLifecycleStore` with selection and token usage fields because it already owns session/entity history.

Persistence note: `RuntimeLifecycleStore` keeps local lifecycle history independent of telemetry/export disablement. Enterprise privacy keeps exporters opt-in, while local runtime history remains available for dashboard/API aggregation.

## Security And Privacy

- Telemetry must never write raw prompts, raw paths, tool arguments, stdout, stderr, secrets, or tokens.
- Session IDs may be stored in the runtime lifecycle ledger because the existing store already validates bounded safe IDs; exported telemetry should continue to use hashed session IDs.
- Payloads must pass existing telemetry/lifecycle sanitizers.
- Local-only defaults remain unchanged; exporters may carry only sanitized OTel-compatible metrics/events.
- Token history must not fabricate exact values. Use `unavailable` when the host cannot provide usage.
- Dashboard/API token history must aggregate usage metadata only. It must not dump raw session transcripts, raw tool arguments, raw tool results, or raw recommendation queries.

## Accessibility

- Browser controls must use native checkbox/select/button controls where possible.
- Dynamic related-recommendation and dashboard states must have text equivalents.
- Tables and forms must preserve existing monitor layout semantics.
- Keyboard-only operation must work for CLI and dashboard forms.

## Acceptance Criteria

- US-001: UI/CLI/API expose selectable recommendation state for skills, agents, MCP servers, and harnesses where those are recommended.
- US-002: Each recommendation exposes a short TL;DR description and relevance reason.
- US-003: Selecting fewer than all recommendations can return filtered related recommendations that exclude selected and rejected tools.
- US-004: Activations persist with entity identity, timestamp, privacy-safe session context, source, and source context.
- US-005: Per-tool token usage is recorded as exact, estimated, or unavailable and exposed through API/dashboard.
- US-006: Historical usage persists across sessions and can be aggregated by tool.
- US-007: Dashboard renders token KPIs/history with empty, unavailable, error, and populated states.
- US-008: API, CLI, MCP/core toolbox, LoopFlow/agent-loop adapter, and dashboard share semantics.
- Existing recommendation, graph, CLI, MCP, LoopFlow, telemetry, and dashboard tests continue to pass.
- Final no-mistakes gate passes after implementation is committed.

## Non-Goals

- Do not introduce a new web framework or dashboard app.
- Do not change the default local-only privacy posture.
- Do not require paid APIs or external observability backends for local functionality.
- Do not rewrite graph ranking.
- Do not fabricate exact token usage for hosts that do not expose it.
- Do not break existing `recommend_bundle` list-return compatibility.
