"""ctx.adapters.generic.ctx_core_tools — expose ctx-core as harness tools.

This is the integration point that makes the alive skill system
available to ANY LLM running through the generic harness. The
toolbox wraps ctx-core recommendation, graph/wiki, and runtime
lifecycle surfaces as ``ToolDefinition``/dispatcher pairs that slot
into ``run_loop``.

Tools exposed (all namespaced under the ``ctx__`` prefix, matching
the MCP router's separator convention so the harness can route to
the toolbox via the same tool-dispatch path it already uses for
MCP servers):

    ctx__recommend_bundle(
        query,
        top_k=5,
        selected=None,
        rejected=None,
        active_context=None,
        baseline_context=None,
        include_baseline_context=False,
        include_unavailable=False,
        local_code_task=None,
        no_api_keys=None,
        language=None,
    )
        Free-text → top-K cross-type bundle (skill + agent + MCP).
        Tokenizes the query into tags, walks the graph, suppresses
        selected/rejected/active/default-baseline context, and returns
        enriched rows with id, tags, availability, tldr, reason, selection
        metadata, baseline_context, and context_policy. Local/no-key/language
        hints can hide unavailable, external-service, generic-planning, or
        wrong-language rows unless include_unavailable opts them back in.

    ctx__recommend_related(selected, rejected=None, max_hops=2, top_n=5)
        Selected/rejected recommendation IDs → graph-related rows.
        Excludes selected, rejected, unavailable, and deprecated nodes.

    ctx__graph_query(seeds, max_hops=2, top_n=10)
        Direct graph walk from a list of seed entity names.
        Exposed for advanced agentic flows that already know
        which entities are relevant.

    ctx__wiki_search(query, top_n=15)
        Keyword search across wiki entity pages — title, description,
        tags. Returns the top matches with their slugs + descriptions.

    ctx__wiki_get(slug)
        Fetch a single entity page by slug — returns its full
        frontmatter plus at most 8,000 UTF-8 bytes of body text.

Load/unload/use tools are explicit lifecycle records, not filesystem
auto-installs. Model-visible load/unload tools record advisory requests
only. The host remains responsible for asking the user, applying verified
activation grants through ``RuntimeLifecycleStore``, and deciding how to
place selected entities into context. Per-entity token usage is recorded
only when the host supplies explicit ``ctx__mark_entity_used.token_usage``
attribution.

Hosts that need a narrower permission surface can construct
``CtxCoreToolbox`` with ``allowed_tool_names`` and ``allowed_entity_types``.
That filters both tool discovery and read/query results before a model sees
them.

Plan 001 Phase H6.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ctx.adapters.generic.providers import ToolCall, ToolDefinition
from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore
from ctx.adapters.generic.tools import TOOL_SEPARATOR
from ctx.core.entity_types import (
    RECOMMENDABLE_ENTITY_TYPES,
    entity_relpath,
    entity_page_path,
    entity_wikilink,
    normalize_entity_type,
)
from ctx.telemetry import hash_identifier, record_event, record_exception, telemetry_span


_logger = logging.getLogger(__name__)


# Tool names all live under the "ctx" namespace, consistent with the
# MCP router's <server>__<tool> convention. The harness dispatches
# calls with names starting "ctx{TOOL_SEPARATOR}" to CtxCoreToolbox,
# anything else falls back to its normal tool_executor.
_NAMESPACE = f"ctx{TOOL_SEPARATOR}"
_FILE_SIGNATURE_SAMPLE_BYTES = 64 * 1024
_WIKI_GET_BODY_MAX_BYTES = 8_000
_RECOMMENDATION_ENTITY_TYPE_ALIASES = {
    "agent": "agent",
    "harness": "harness",
    "mcp": "mcp-server",
    "mcp-server": "mcp-server",
    "mcp-servers": "mcp-server",
    "skill": "skill",
}
_RELATED_BLOCKED_STATUSES = {
    "archived",
    "deleted",
    "deprecated",
    "disabled",
    "removed",
    "stale",
    "unavailable",
}
_REMOTE_SKILL_LOAD_STATUSES = {"available", "remote-cataloged"}
_DEFAULT_BASELINE_CONTEXT = ("mcp-server:codex-cli",)
_REJECTION_MODES = frozenset({"use", "replace", "ignore"})
_LOCAL_CODE_QUERY_MARKERS = (
    "local files",
    "local repo",
    "local repository",
    "feature_implementation",
    "feature implementation",
    "codex cli",
)
_NO_API_KEY_QUERY_MARKERS = (
    "no api keys",
    "no local api keys",
    "without api keys",
    "no external api",
)
_EXTERNAL_SERVICE_TOKENS = {
    "anthropic",
    "aws",
    "azure",
    "deployer",
    "deployment",
    "gemini",
    "google",
    "keyvault",
    "notion",
    "openai",
    "stripe",
    "vercel",
}
_GENERIC_PLANNING_TOKENS = {
    "archiving",
    "planner",
    "planning",
    "prioritization",
    "product",
    "prompt",
    "spec",
}
_LANGUAGE_TOKEN_ALIASES = {
    "c": frozenset({"c"}),
    "cpp": frozenset({"cpp", "c++", "cplusplus"}),
    "csharp": frozenset({"csharp", "c#", "dotnet"}),
    "go": frozenset({"go", "golang"}),
    "java": frozenset({"java"}),
    "javascript": frozenset({"javascript", "js", "node"}),
    "php": frozenset({"php"}),
    "python": frozenset({"python", "py"}),
    "rust": frozenset({"rust", "rs"}),
    "typescript": frozenset({"typescript", "ts"}),
}
_AMBIGUOUS_LANGUAGE_TOKEN_ALIASES = frozenset({"go", "node"})
SUPPORTED_RESPONSE_FORMATS = ("json", "gcf")
_RESPONSE_FORMAT_PROPERTY = {
    "type": "string",
    "enum": list(SUPPORTED_RESPONSE_FORMATS),
    "description": (
        "Optional response codec for large read-only responses. "
        "Default json preserves the stable ctx contract; gcf requires "
        "the optional claude-ctx[gcf] extra."
    ),
}

FileSignature = tuple[int, int, str]
PackSignature = tuple[tuple[str, FileSignature | None], ...]
GraphSignature = tuple[FileSignature | None, FileSignature | None, PackSignature]
PageSignature = tuple[int, int, int, PackSignature]


def _response_format_from_args(args: Mapping[str, Any]) -> str:
    raw = args.get("_response_format", args.get("output_format", "json"))
    requested = str(raw or "json").strip().lower()
    return requested or "json"


def _encode_response(data: Mapping[str, Any], response_format: str) -> str:
    requested = str(response_format or "json").strip().lower()
    if requested == "json":
        return json.dumps(data)
    if requested != "gcf":
        return json.dumps(
            {
                "error": (
                    "unsupported response format "
                    f"{response_format!r}; expected one of "
                    f"{', '.join(SUPPORTED_RESPONSE_FORMATS)}"
                ),
                "response_format": "json",
            }
        )

    try:
        from gcf import encode_generic  # type: ignore[import-not-found]
    except Exception:
        return json.dumps(
            {
                "error": (
                    "GCF response format requires optional dependency "
                    "gcf-python. Install with `pip install "
                    '"claude-ctx[gcf]"`.'
                ),
                "response_format": "json",
            }
        )

    try:
        return str(encode_generic(dict(data)))
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {
                "error": f"GCF response encoding failed: {exc}",
                "response_format": "json",
            }
        )


def _duration_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _hash_json_value(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hash_identifier(encoded)


def _safe_tool_payload(local_name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ctx.operation": local_name,
        "ctx.tool.name": f"{_NAMESPACE}{local_name}",
        "ctx.arguments.keys": sorted(str(key) for key in args),
    }
    for key in ("top_k", "top_n", "max_hops"):
        value = args.get(key)
        if isinstance(value, (int, float)):
            payload[f"ctx.arguments.{key}"] = value
    query = args.get("query")
    if isinstance(query, str):
        payload["ctx.query.hash"] = hash_identifier(query)
        payload["ctx.query.length"] = len(query)
    seeds = args.get("seeds")
    if isinstance(seeds, (list, tuple)):
        payload["ctx.seeds.count"] = len(seeds)
        payload["ctx.seeds.hash"] = _hash_json_value(list(seeds))
    for key in ("selected", "rejected"):
        values = args.get(key)
        if isinstance(values, (list, tuple)):
            payload[f"ctx.selection.{key}.count"] = len(values)
            payload[f"ctx.selection.{key}.hash"] = _hash_json_value(list(values))
    slug = args.get("slug")
    if isinstance(slug, str):
        payload["ctx.slug.hash"] = hash_identifier(slug)
    entity_type = args.get("entity_type")
    if isinstance(entity_type, str):
        payload["ctx.entity.type"] = entity_type
    lifecycle_action = args.get("event_type") or args.get("trigger") or local_name
    if local_name in _LIFECYCLE_LOCAL_NAMES:
        payload["ctx.lifecycle.action"] = str(lifecycle_action)
    return payload


def _result_payload(result_json: str) -> tuple[str, str | None, dict[str, Any]]:
    try:
        parsed = json.loads(result_json)
    except json.JSONDecodeError:
        return "error", "invalid_json", {"ctx.result.has_error_payload": True}
    if not isinstance(parsed, dict):
        return "ok", None, {}
    payload: dict[str, Any] = {"ctx.result.has_error_payload": "error" in parsed}
    results = parsed.get("results")
    if isinstance(results, list):
        payload["ctx.result.count"] = len(results)
    companion_harnesses = parsed.get("companion_harnesses")
    if isinstance(companion_harnesses, list):
        payload["ctx.companion_harness.count"] = len(companion_harnesses)
    if parsed.get("ok") is False or "error" in parsed:
        return "error", "structured_error", payload
    return "ok", None, payload


def _record_core_tool_event(
    event_name: str,
    *,
    payload: dict[str, Any],
    outcome: str,
    duration_ms: float,
    session_id: str | None = None,
    error_kind: str | None = None,
    exc: BaseException | None = None,
) -> None:
    payload["otel.status_code"] = "ERROR" if outcome == "error" else "OK"
    if error_kind:
        payload["error.type"] = error_kind
    try:
        if exc is not None:
            record_exception(
                event_name,
                source="ctx-core",
                exc=exc,
                transport="ctx-core-toolbox",
                session_id=session_id,
                outcome=outcome,
                duration_ms=duration_ms,
                error_kind=error_kind,
                payload=payload,
            )
        else:
            record_event(
                event_name,
                source="ctx-core",
                transport="ctx-core-toolbox",
                session_id=session_id,
                outcome=outcome,
                duration_ms=duration_ms,
                error_kind=error_kind,
                payload=payload,
            )
    except Exception:  # noqa: BLE001 - telemetry must not break tool calls.
        pass


_CORE_EVENT_NAMES = {
    "recommend_bundle": "ctx.core.recommend_bundle",
    "recommend_related": "ctx.core.recommend_related",
    "graph_query": "ctx.core.graph_query",
    "wiki_search": "ctx.core.wiki_search",
    "wiki_get": "ctx.core.wiki_get",
    "loop_provision": "ctx.core.loop_provision",
    "loop_topup": "ctx.core.loop_topup",
}
_LOOP_PROVISION_TOOL_NAMES = frozenset(
    {
        f"{_NAMESPACE}loop_provision",
        f"{_NAMESPACE}loop_topup",
    }
)
_LIFECYCLE_LOCAL_NAMES = frozenset(
    {
        "observe_dev_event",
        "load_entity",
        "mark_entity_used",
        "record_validation",
        "record_escalation",
        "unload_entity",
        "session_end",
        "session_state",
    }
)


class CtxCoreToolbox:
    """ctx-core recommendation and lifecycle surface for harness tools.

    Lazy-initialises heavy deps (networkx graph load, wiki page
    scan) so a harness that never asks for ctx-core tools doesn't
    pay the cost. First call to ``dispatch`` or ``tool_definitions``
    warms the relevant cache.

    Recommendation calls are independent unless a host binds or supplies
    a session id. Session-bound calls persist canonical rejection feedback
    in the lifecycle store so later recommendations do not repeat it.
    """

    def __init__(
        self,
        *,
        wiki_dir: Path | None = None,
        graph_path: Path | None = None,
        lifecycle_dir: Path | None = None,
        bound_session_id: str | None = None,
        recommendation_session_id: str | None = None,
        allowed_tool_names: Iterable[str] | None = None,
        allowed_entity_types: Iterable[str] | None = None,
    ) -> None:
        self._wiki_dir = wiki_dir
        self._graph_path = graph_path
        self._lifecycle = RuntimeLifecycleStore(lifecycle_dir)
        self._bound_session_id = str(bound_session_id or "").strip() or None
        self._recommendation_bound_session_id = str(recommendation_session_id or "").strip() or None
        self._allowed_tool_names = _normalise_allowed_tool_names(allowed_tool_names)
        self._allowed_entity_types = _normalise_allowed_entity_types(allowed_entity_types)
        self._graph: Any | None = None  # networkx.Graph
        self._pages: list[Any] | None = None  # list[SkillPage]
        self._graph_signature: GraphSignature | None = None
        self._pages_signature: PageSignature | None = None
        self._semantic_signature: tuple[FileSignature | None, ...] | None = None

    # ── Public Protocol surface ─────────────────────────────────────────

    def tool_definitions(self) -> list[ToolDefinition]:
        """Return the list of tools this toolbox exposes to the model."""
        definitions = [
            ToolDefinition(
                name=f"{_NAMESPACE}recommend_bundle",
                description=(
                    "Recommend a top-K bundle of skills / agents / MCP "
                    "servers relevant to a free-text query. Returns a "
                    "JSON array of entries with name, type, score, and "
                    "shared tags. Use when the user asks 'what tools "
                    "should I use for X?' or mid-task to find a more "
                    "specialised skill."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Free-text description of the task or stack.",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "How many entries to return. Default/max 5.",
                            "minimum": 1,
                            "maximum": 5,
                        },
                        "model_provider": {
                            "type": "string",
                            "description": (
                                "Optional custom/local model provider, used only "
                                "for companion harness recommendations."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Optional model slug, used only for companion "
                                "harness recommendations."
                            ),
                        },
                        "use_semantic_query": {
                            "type": "boolean",
                            "description": (
                                "Opt in to local embedding-based query scoring. "
                                "Default false keeps recommendations latency-safe."
                            ),
                        },
                        "selected": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Already selected recommendation IDs or names to suppress.",
                        },
                        "rejected": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Rejected recommendation IDs or names to suppress.",
                        },
                        "active_context": {
                            "type": "array",
                            "items": {
                                "anyOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "load_status": {"type": "string"},
                                            "stale": {"type": "boolean"},
                                            "unload_candidate": {"type": "boolean"},
                                        },
                                        "required": ["id"],
                                    },
                                ]
                            },
                            "description": (
                                "Active ctx IDs, optionally with explicit applied/stale "
                                "state used for keep, unload, and replace guidance."
                            ),
                        },
                        "baseline_context": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Host-provided baseline ctx entity IDs. "
                                "Defaults to mcp-server:codex-cli."
                            ),
                        },
                        "include_baseline_context": {
                            "type": "boolean",
                            "description": "Return baseline context as normal recommendations. Default false.",
                        },
                        "include_unavailable": {
                            "type": "boolean",
                            "description": (
                                "Include non-local or non-loadable rows. Default false for "
                                "local/no-key coding contexts."
                            ),
                        },
                        "local_code_task": {
                            "type": "boolean",
                            "description": (
                                "Hint that the task is a local repo/code edit, used to demote "
                                "generic planning/deployment recommendations."
                            ),
                        },
                        "no_api_keys": {
                            "type": "boolean",
                            "description": (
                                "Hint that no API keys are available, used to suppress "
                                "external-service recommendations."
                            ),
                        },
                        "language": {
                            "type": "string",
                            "description": "Optional scenario/programming language hint.",
                        },
                        "output_format": dict(_RESPONSE_FORMAT_PROPERTY),
                        "_response_format": dict(_RESPONSE_FORMAT_PROPERTY),
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name=f"{_NAMESPACE}graph_query",
                description=(
                    "Walk the knowledge graph from a list of seed "
                    "entities and return related entities ranked by "
                    "edge weight over up to max_hops. Use when you "
                    "already know a specific skill, agent, MCP, or harness and want "
                    "to find its close neighbours."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "seeds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Seed entity names (skill / agent / harness / "
                                "mcp-server slugs). No type prefix — "
                                "the walker tries every entity type."
                            ),
                            "minItems": 1,
                        },
                        "max_hops": {
                            "type": "integer",
                            "description": "Walk depth. Default 2.",
                            "minimum": 1,
                            "maximum": 4,
                        },
                        "top_n": {
                            "type": "integer",
                            "description": "How many results. Default 10.",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "output_format": dict(_RESPONSE_FORMAT_PROPERTY),
                        "_response_format": dict(_RESPONSE_FORMAT_PROPERTY),
                    },
                    "required": ["seeds"],
                },
            ),
            ToolDefinition(
                name=f"{_NAMESPACE}recommend_related",
                description=(
                    "Recommend related skills / agents / MCP servers after "
                    "the user selected a subset of an initial bundle. "
                    "Filters out selected and rejected recommendation IDs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "selected": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Selected recommendation IDs or names, such as "
                                "'skill:fastapi-pro' or 'fastapi-pro'."
                            ),
                            "minItems": 1,
                        },
                        "rejected": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Rejected recommendation IDs or names to exclude.",
                        },
                        "max_hops": {
                            "type": "integer",
                            "description": "Graph walk depth. Default 2.",
                            "minimum": 1,
                            "maximum": 4,
                        },
                        "top_n": {
                            "type": "integer",
                            "description": "How many related recommendations. Default 5.",
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "output_format": dict(_RESPONSE_FORMAT_PROPERTY),
                        "_response_format": dict(_RESPONSE_FORMAT_PROPERTY),
                    },
                    "required": ["selected"],
                },
            ),
            ToolDefinition(
                name=f"{_NAMESPACE}wiki_search",
                description=(
                    "Keyword search across the llm-wiki entity pages "
                    "(skills + agents + mcp-servers + harnesses). Matches against "
                    "title, description, and tags. Returns slug + "
                    "description for each hit."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_n": {
                            "type": "integer",
                            "description": "Max results. Default 15.",
                            "minimum": 1,
                            "maximum": 100,
                        },
                        "output_format": dict(_RESPONSE_FORMAT_PROPERTY),
                        "_response_format": dict(_RESPONSE_FORMAT_PROPERTY),
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name=f"{_NAMESPACE}wiki_get",
                description=(
                    "Fetch a single wiki entity page by slug. Returns "
                    "the full frontmatter (as a dict), wiki-relative path, "
                    "and up to 8,000 UTF-8 bytes of body text. Body byte "
                    "counts and truncation status are included in the response. "
                    "Use after recommend_bundle / wiki_search to read "
                    "the detail of a specific candidate."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "entity_type": {
                            "type": "string",
                            "enum": list(RECOMMENDABLE_ENTITY_TYPES),
                            "description": (
                                "Optional entity type from wiki_search. "
                                "Use it to disambiguate duplicate slugs."
                            ),
                        },
                        "output_format": dict(_RESPONSE_FORMAT_PROPERTY),
                        "_response_format": dict(_RESPONSE_FORMAT_PROPERTY),
                    },
                    "required": ["slug"],
                },
            ),
            ToolDefinition(
                name=f"{_NAMESPACE}loop_provision",
                description=(
                    "Opt-in write tool for harness loops. Recommends and installs "
                    "loadable skills for a goal into the configured skills "
                    "directory. Hidden unless the host explicitly allows this "
                    "tool name."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "The loop goal the skills should support.",
                        },
                        "intent": {
                            "type": "string",
                            "description": (
                                "Optional recommendation query override; defaults to goal."
                            ),
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "How many skills to consider. Default/max 5.",
                            "minimum": 1,
                            "maximum": 5,
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Preview without writing files. Default false.",
                        },
                        "security_scan": {
                            "type": "boolean",
                            "description": (
                                "Run SkillSpector before installing each skill. Default false."
                            ),
                        },
                    },
                    "required": ["goal"],
                },
            ),
            ToolDefinition(
                name=f"{_NAMESPACE}loop_topup",
                description=(
                    "Opt-in write tool for harness loops. After a failed loop "
                    "cycle, recommends and installs additional loadable skills "
                    "for the goal plus reflection, excluding already loaded "
                    "skills. Hidden unless the host explicitly allows this tool "
                    "name."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "reflection": {
                            "type": "string",
                            "description": "Why the last loop cycle failed.",
                        },
                        "loaded": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Skill slugs already loaded.",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "How many skills to consider. Default/max 5.",
                            "minimum": 1,
                            "maximum": 5,
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Preview without writing files. Default false.",
                        },
                        "security_scan": {
                            "type": "boolean",
                            "description": (
                                "Run SkillSpector before installing each skill. Default false."
                            ),
                        },
                    },
                    "required": ["goal", "reflection"],
                },
            ),
        ]
        for definition in definitions:
            if definition.name not in {
                f"{_NAMESPACE}recommend_bundle",
                f"{_NAMESPACE}recommend_related",
            }:
                continue
            properties = definition.parameters["properties"]
            properties["rejection_mode"] = {
                "type": "string",
                "enum": sorted(_REJECTION_MODES),
                "description": (
                    "use merges session memory (default); replace overwrites it, "
                    "including clearing with an empty rejected list; ignore is call-local."
                ),
            }
            if self._bound_session_id is None and self._recommendation_bound_session_id is None:
                properties["session_id"] = {
                    "type": "string",
                    "description": (
                        "Optional host session id used only for rejection memory correlation."
                    ),
                }
        definitions.extend(_lifecycle_tool_definitions(self._bound_session_id))
        definitions = [td for td in definitions if self.allows(td.name)]
        return definitions

    def dispatch(self, call: ToolCall) -> str:
        """Execute one ctx-core tool call. Returns a JSON string.

        Returning JSON (not a bare string) keeps the model's
        mental model of tool output consistent — every ctx-core
        tool produces structured data, and the model can parse it
        back on the next turn to reason about specific fields.
        """
        if not call.name.startswith(_NAMESPACE):
            raise ValueError(f"CtxCoreToolbox got a non-ctx call {call.name!r}")
        if not self.allows(call.name):
            raise ValueError(f"ctx-core tool not allowed: {call.name!r}")
        local_name = call.name[len(_NAMESPACE) :]
        args = call.arguments or {}
        started = time.perf_counter()
        event_name = _CORE_EVENT_NAMES.get(
            local_name,
            "ctx.core.lifecycle" if local_name in _LIFECYCLE_LOCAL_NAMES else "ctx.core.tool_call",
        )
        event_payload = _safe_tool_payload(local_name, args)
        session_id = str(args.get("session_id") or "").strip() or self._bound_session_id
        if session_id is None and local_name in {"recommend_bundle", "recommend_related"}:
            session_id = self._recommendation_bound_session_id

        with telemetry_span():
            try:
                if local_name == "recommend_bundle":
                    result = self._dispatch_recommend(args)
                elif local_name == "recommend_related":
                    result = self._dispatch_recommend_related(args)
                elif local_name == "graph_query":
                    result = self._dispatch_graph_query(args)
                elif local_name == "wiki_search":
                    result = self._dispatch_wiki_search(args)
                elif local_name == "wiki_get":
                    result = self._dispatch_wiki_get(args)
                elif local_name == "loop_provision":
                    result = self._dispatch_loop_provision(args)
                elif local_name == "loop_topup":
                    result = self._dispatch_loop_topup(args)
                elif local_name == "observe_dev_event":
                    result = self._dispatch_lifecycle(args, "observe_dev_event")
                elif local_name == "load_entity":
                    result = self._dispatch_lifecycle(args, "load_entity")
                elif local_name == "mark_entity_used":
                    result = self._dispatch_lifecycle(args, "mark_entity_used")
                elif local_name == "record_validation":
                    result = self._dispatch_lifecycle(args, "record_validation")
                elif local_name == "record_escalation":
                    result = self._dispatch_lifecycle(args, "record_escalation")
                elif local_name == "unload_entity":
                    result = self._dispatch_lifecycle(args, "unload_entity")
                elif local_name == "session_end":
                    result = self._dispatch_lifecycle(args, "session_end")
                elif local_name == "session_state":
                    result = self._dispatch_lifecycle(args, "session_state")
                else:
                    raise ValueError(f"unknown ctx-core tool {local_name!r}")
            except Exception as exc:  # noqa: BLE001 - preserve existing propagation.
                _record_core_tool_event(
                    event_name,
                    payload=event_payload,
                    outcome="error",
                    duration_ms=_duration_ms(started),
                    session_id=session_id,
                    error_kind=type(exc).__name__,
                    exc=exc,
                )
                raise

            outcome, error_kind, result_payload = _result_payload(result)
            event_payload.update(result_payload)
            _record_core_tool_event(
                event_name,
                payload=event_payload,
                outcome=outcome,
                duration_ms=_duration_ms(started),
                session_id=session_id,
                error_kind=error_kind,
            )
        return result

    def owns(self, tool_name: str) -> bool:
        """True when this toolbox is the dispatcher for the given name."""
        return tool_name.startswith(_NAMESPACE)

    def allows(self, tool_name: str) -> bool:
        """True when this toolbox is allowed to expose the given ctx tool."""
        if self._allowed_tool_names is not None:
            return tool_name in self._allowed_tool_names
        return tool_name not in _LOOP_PROVISION_TOOL_NAMES

    def recommendation_rejections(
        self,
        rejected: list[str] | None = None,
        *,
        session_id: str | None = None,
        rejection_mode: str = "use",
    ) -> list[str]:
        """Resolve explicit and remembered rejections for first-party adapters."""
        graph = self._ensure_graph()
        explicit = _recommendation_selection_values(rejected or [])
        if graph.number_of_nodes() == 0:
            return explicit
        return self._recommendation_rejections(
            graph,
            {
                "rejected": explicit,
                "session_id": session_id,
                "rejection_mode": rejection_mode,
            },
        )

    # ── Individual dispatchers ──────────────────────────────────────────

    def _dispatch_recommend(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query must be non-empty", "results": []})
        from ctx_config import cfg  # noqa: PLC0415

        top_k = _clamp_int(
            args.get("top_k"),
            default=cfg.recommendation_top_k,
            lo=1,
            hi=cfg.recommendation_top_k,
        )

        tags = _query_to_tags(query)
        use_semantic_query = bool(args.get("use_semantic_query"))
        if not tags and not use_semantic_query:
            return json.dumps(
                {
                    "error": "query produced no usable tags",
                    "results": [],
                }
            )

        graph = self._ensure_graph()
        if graph.number_of_nodes() == 0:
            return json.dumps(
                {
                    "error": "knowledge graph not available; run ctx-wiki-graphify",
                    "results": [],
                }
            )

        from ctx.core.resolve.recommendations import recommend_by_tags  # noqa: PLC0415

        semantic_cache_dir = None
        if use_semantic_query:
            self._refresh_semantic_cache_signature()
            semantic_cache_dir = _semantic_cache_dir(self._wiki_dir_resolved())
        entity_types = tuple(
            entity_type
            for entity_type in ("skill", "agent", "mcp-server")
            if self._entity_type_allowed(entity_type)
        )
        if not entity_types:
            return _encode_response(
                {"query": query, "tags": tags, "results": [], "companion_harnesses": []},
                _response_format_from_args(args),
            )
        selected = _selection_values_from_args(args, "selected")
        rejected = self._recommendation_rejections(graph, args)
        active_context_raw = args.get("active_context") or []
        active_context = (
            _recommendation_selection_values(active_context_raw)
            if isinstance(active_context_raw, list)
            else []
        )
        include_baseline = bool(_optional_bool(args.get("include_baseline_context")) or False)
        baseline_context = _selection_values_from_args(args, "baseline_context")
        if not baseline_context and not include_baseline:
            baseline_context = list(_DEFAULT_BASELINE_CONTEXT)
        excluded = _recommendation_selection_keys(
            selected + rejected + active_context + ([] if include_baseline else baseline_context)
        )
        recommendation_context = _recommendation_context_from_args(query, args)
        raw_top_n = min(50, top_k + len(excluded) + 25)
        raw = recommend_by_tags(
            graph,
            tags,
            top_n=raw_top_n,
            query=query,
            entity_types=entity_types,
            min_normalized_score=cfg.recommendation_min_normalized_score,
            use_semantic_query=use_semantic_query,
            semantic_cache_dir=semantic_cache_dir,
        )
        results: list[dict[str, Any]] = []
        wiki_dir = self._wiki_dir_resolved()
        for r in raw:
            row = _with_recommendation_selection_metadata(
                _base_recommendation_row(r, wiki_dir=wiki_dir)
            )
            candidate_keys = _recommendation_selection_keys(
                [_recommendation_identity(row), str(row.get("name") or "")]
            )
            if candidate_keys & excluded:
                continue
            skip_reason = _recommendation_context_skip_reason(row, recommendation_context)
            if skip_reason is not None:
                continue
            results.append(row)
            if len(results) >= top_k:
                break
        model_provider = _optional_str(args.get("model_provider"))
        model = _optional_str(args.get("model"))
        companion_harnesses = (
            _recommend_companion_harnesses(
                query,
                top_k=top_k,
                model_provider=model_provider,
                model=model,
            )
            if (model_provider or model) and self._entity_type_allowed("harness")
            else []
        )
        return _encode_response(
            {
                "query": query,
                "tags": tags,
                "baseline_context": baseline_context,
                "selection": {
                    "selected": selected,
                    "rejected": rejected,
                    "active_context": active_context,
                },
                "context_policy": _recommendation_context_policy(
                    baseline_context=baseline_context,
                    active_context=(
                        active_context_raw
                        if isinstance(active_context_raw, list)
                        else active_context
                    ),
                    rejected_context=rejected,
                    results=results,
                ),
                "results": results,
                "companion_harnesses": companion_harnesses,
            },
            _response_format_from_args(args),
        )

    def _dispatch_recommend_related(self, args: dict[str, Any]) -> str:
        selected_raw = args.get("selected") or []
        if not isinstance(selected_raw, list) or not selected_raw:
            return json.dumps({"error": "selected must be a non-empty list", "results": []})
        selected = _recommendation_selection_values(selected_raw)
        if not selected:
            return json.dumps(
                {
                    "error": "selected must contain recommendation IDs or names",
                    "results": [],
                }
            )

        max_hops = _clamp_int(args.get("max_hops"), default=2, lo=1, hi=4)
        top_n = _clamp_int(args.get("top_n"), default=5, lo=1, hi=50)

        graph = self._ensure_graph()
        if graph.number_of_nodes() == 0:
            return json.dumps(
                {
                    "error": "knowledge graph not available; run ctx-wiki-graphify",
                    "results": [],
                }
            )

        rejected = self._recommendation_rejections(graph, args)
        excluded = _recommendation_selection_keys(selected + rejected)
        seed_ids = _recommendation_selection_node_ids(graph, selected)
        raw = _resolve_related_recommendation_rows(
            graph,
            seed_ids,
            max_hops=max_hops,
            top_n=min(50, top_n + len(excluded) + 5),
        )
        results: list[dict[str, Any]] = []
        for r in raw:
            if not self._entity_type_allowed(str(r.get("type") or "")):
                continue
            candidate_keys = _recommendation_selection_keys(
                [_recommendation_identity(r), str(r.get("name") or "")]
            )
            if candidate_keys & excluded:
                continue
            shared_tags = r.get("shared_tags", [])
            related_row = dict(r)
            related_row["matching_tags"] = shared_tags
            row = _with_recommendation_selection_metadata(
                _base_recommendation_row(related_row, wiki_dir=self._wiki_dir_resolved())
            )
            row["shared_tags"] = shared_tags
            row["via"] = r.get("via", [])
            row["selection_state"] = "suggested_related"
            row["related_to"] = r.get("via", [])
            row["reason"] = _related_recommendation_reason(row)
            results.append(row)
            if len(results) >= top_n:
                break

        return _encode_response(
            {
                "selected": selected,
                "rejected": rejected,
                "results": results,
            },
            _response_format_from_args(args),
        )

    def _dispatch_graph_query(self, args: dict[str, Any]) -> str:
        seeds_raw = args.get("seeds") or []
        if not isinstance(seeds_raw, list) or not seeds_raw:
            return json.dumps({"error": "seeds must be a non-empty list", "results": []})
        seeds = [str(s) for s in seeds_raw if s]
        if not seeds:
            return json.dumps({"error": "seeds must be non-empty strings", "results": []})
        max_hops = _clamp_int(args.get("max_hops"), default=2, lo=1, hi=4)
        top_n = _clamp_int(args.get("top_n"), default=10, lo=1, hi=50)

        graph = self._ensure_graph()
        if graph.number_of_nodes() == 0:
            return json.dumps(
                {
                    "error": "knowledge graph not available; run ctx-wiki-graphify",
                    "results": [],
                }
            )

        from ctx.core.graph.resolve_graph import resolve_by_seeds  # noqa: PLC0415

        raw = resolve_by_seeds(graph, seeds, max_hops=max_hops, top_n=top_n)
        results = [
            {
                "name": r["name"],
                "type": r["type"],
                "score": r["score"],
                "normalized_score": r.get("normalized_score"),
                "shared_tags": r.get("shared_tags", []),
                "via": r.get("via", []),
            }
            for r in raw
            if self._entity_type_allowed(str(r.get("type") or ""))
        ]
        return _encode_response(
            {"seeds": seeds, "results": results},
            _response_format_from_args(args),
        )

    def _dispatch_wiki_search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query must be non-empty", "results": []})
        top_n = _clamp_int(args.get("top_n"), default=15, lo=1, hi=100)

        pages = [
            page
            for page in self._ensure_pages()
            if self._entity_type_allowed(str(page.entity_type))
        ]
        if not pages:
            return json.dumps(
                {
                    "error": "wiki has no pages",
                    "results": [],
                }
            )

        from ctx.core.wiki.wiki_query import search_by_query  # noqa: PLC0415

        hits = search_by_query(pages, query, top_n=top_n)
        results = [
            {
                "slug": p.name,
                "title": p.title or p.name,
                "entity_type": p.entity_type,
                "wikilink": p.wikilink,
                "description": p.description,
                "excerpt": _excerpt(p.body, 160),
                "tags": list(p.tags),
                "status": p.status,
                "score": p.score,
            }
            for p in hits
        ]
        return _encode_response(
            {"query": query, "results": results},
            _response_format_from_args(args),
        )

    def _dispatch_wiki_get(self, args: dict[str, Any]) -> str:
        slug = str(args.get("slug", "")).strip()
        if not slug:
            return json.dumps({"error": "slug must be non-empty"})
        entity_type = str(args.get("entity_type", "")).strip()
        if entity_type and entity_type not in RECOMMENDABLE_ENTITY_TYPES:
            return json.dumps(
                {
                    "error": (
                        "entity_type must be one of " + ", ".join(RECOMMENDABLE_ENTITY_TYPES)
                    ),
                }
            )
        if entity_type and not self._entity_type_allowed(entity_type):
            return json.dumps({"error": f"entity_type {entity_type!r} is not allowed"})

        # Validate — ctx-core's validator rejects traversal shapes.
        from ctx.core.wiki.wiki_utils import validate_skill_name  # noqa: PLC0415

        try:
            validate_skill_name(slug)
        except ValueError as exc:
            return json.dumps({"error": f"invalid slug: {exc}"})

        wiki = self._wiki_dir_resolved()
        if wiki is None:
            return json.dumps({"error": "wiki_dir not configured"})

        candidate_entity_types = (
            (entity_type,)
            if entity_type
            else tuple(
                allowed_type
                for allowed_type in RECOMMENDABLE_ENTITY_TYPES
                if self._entity_type_allowed(allowed_type)
            )
        )
        if not candidate_entity_types:
            return json.dumps({"error": "no entity types are allowed"})
        candidates = _wiki_get_candidates(wiki, slug, candidate_entity_types)
        try:
            pack_pages = _wiki_pack_pages(wiki)
        except Exception as exc:  # noqa: BLE001 - surface corrupt pack state to callers.
            return json.dumps({"error": f"could not read wiki-packs: {exc}"})

        for candidate_type, path, wikilink in candidates:
            if pack_pages is not None:
                relpath = _wiki_entity_relpath(candidate_type, slug)
                text = pack_pages.get(relpath)
                if text is not None:
                    return self._serialise_page_text(
                        path,
                        text,
                        candidate_type,
                        wikilink,
                        _response_format_from_args(args),
                    )
                continue
            if path.is_file():
                return self._serialise_page(
                    path,
                    candidate_type,
                    wikilink,
                    _response_format_from_args(args),
                )

        return json.dumps(
            {
                "error": f"no entity page found for slug {slug!r}",
                "looked_in": [
                    _wiki_entity_relpath(candidate_type, slug)
                    for candidate_type, _, _ in candidates
                ],
            }
        )

    def _entity_type_allowed(self, entity_type: str) -> bool:
        return self._allowed_entity_types is None or entity_type in self._allowed_entity_types

    def _dispatch_loop_provision(self, args: dict[str, Any]) -> str:
        goal = str(args.get("goal", "")).strip()
        intent = _optional_str(args.get("intent"))
        return self._dispatch_loop_provision_impl(
            goal=(intent or goal).strip(),
            reflection="",
            args=args,
            exclude=[],
        )

    def _dispatch_loop_topup(self, args: dict[str, Any]) -> str:
        goal = str(args.get("goal", "")).strip()
        reflection = str(args.get("reflection", "")).strip()
        loaded_raw = args.get("loaded") or []
        loaded = (
            [str(slug).strip() for slug in loaded_raw if str(slug).strip()]
            if isinstance(loaded_raw, list)
            else []
        )
        return self._dispatch_loop_provision_impl(
            goal=goal,
            reflection=reflection,
            args=args,
            exclude=loaded,
        )

    def _dispatch_loop_provision_impl(
        self,
        *,
        goal: str,
        reflection: str,
        args: dict[str, Any],
        exclude: list[str],
    ) -> str:
        if not goal:
            return json.dumps(
                {
                    "error": "goal (or intent) must be non-empty",
                    "use_skills": [],
                    "installed": [],
                    "skipped": [],
                }
            )
        from ctx_config import cfg  # noqa: PLC0415

        wiki = self._wiki_dir_resolved()
        if wiki is None:
            return json.dumps(
                {
                    "error": "wiki directory not configured; cannot install skills",
                    "use_skills": [],
                    "installed": [],
                    "skipped": [],
                }
            )
        top_k = _clamp_int(
            args.get("top_k"),
            default=cfg.recommendation_top_k,
            lo=1,
            hi=cfg.recommendation_top_k,
        )

        from ctx.adapters.generic.loop_tools import provision_skills  # noqa: PLC0415

        result = provision_skills(
            wiki_dir=wiki,
            skills_dir=cfg.skills_dir,
            goal=goal,
            reflection=reflection,
            top_k=top_k,
            dry_run=bool(args.get("dry_run")),
            exclude=exclude,
            security_scan=bool(args.get("security_scan")),
        )
        return json.dumps(result)

    def _dispatch_lifecycle(self, args: dict[str, Any], name: str) -> str:
        try:
            session_id = self._lifecycle_session_id(args)
            if name == "observe_dev_event":
                result = self._lifecycle.record_dev_event(
                    session_id=session_id,
                    event_type=str(args.get("event_type") or ""),
                    host=str(args.get("host") or "") or None,
                    cwd=str(args.get("cwd") or "") or None,
                    payload=_dict_arg(args.get("payload")),
                )
            elif name == "load_entity":
                result = self._lifecycle.load_entity(
                    session_id=session_id,
                    entity_type=str(args.get("entity_type") or ""),
                    slug=str(args.get("slug") or ""),
                    reason=str(args.get("reason") or "") or None,
                    security_scan=(
                        _dict_arg(args.get("security_scan")) if "security_scan" in args else None
                    ),
                    selected=False,
                    selection_source="unknown",
                    source_context=_dict_arg(args.get("source_context")),
                )
            elif name == "mark_entity_used":
                result = self._lifecycle.mark_entity_used(
                    session_id=session_id,
                    entity_type=str(args.get("entity_type") or ""),
                    slug=str(args.get("slug") or ""),
                    evidence=str(args.get("evidence") or "") or None,
                    token_usage=(
                        _dict_arg(args.get("token_usage")) if "token_usage" in args else None
                    ),
                )
            elif name == "unload_entity":
                result = self._lifecycle.unload_entity(
                    session_id=session_id,
                    entity_type=str(args.get("entity_type") or ""),
                    slug=str(args.get("slug") or ""),
                    reason=str(args.get("reason") or "") or None,
                )
            elif name == "record_validation":
                result = self._lifecycle.record_validation(
                    session_id=session_id,
                    check_name=str(args.get("check_name") or ""),
                    status=str(args.get("status") or ""),
                    command=str(args.get("command") or "") or None,
                    summary=str(args.get("summary") or "") or None,
                    entity_type=str(args.get("entity_type") or "") or None,
                    slug=str(args.get("slug") or "") or None,
                    payload=_dict_arg(args.get("payload")),
                )
            elif name == "record_escalation":
                result = self._lifecycle.record_escalation(
                    session_id=session_id,
                    trigger=str(args.get("trigger") or ""),
                    reason=str(args.get("reason") or ""),
                    severity=str(args.get("severity") or "") or None,
                    status=str(args.get("status") or "") or None,
                    entity_type=str(args.get("entity_type") or "") or None,
                    slug=str(args.get("slug") or "") or None,
                    payload=_dict_arg(args.get("payload")),
                )
            elif name == "session_end":
                result = self._lifecycle.end_session(
                    session_id=session_id,
                    status=str(args.get("status") or "") or None,
                    summary=str(args.get("summary") or "") or None,
                )
            elif name == "session_state":
                result = self._lifecycle.session_state(
                    session_id=session_id,
                    min_unused_seconds=_float_arg(
                        args.get("min_unused_seconds"),
                    ),
                )
            else:
                raise ValueError(f"unknown lifecycle tool {name}")
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps(result)

    def _lifecycle_session_id(self, args: dict[str, Any]) -> str:
        supplied = str(args.get("session_id") or "").strip()
        if self._bound_session_id is not None:
            if supplied and supplied != self._bound_session_id:
                raise ValueError("session_id is host-bound and cannot be overridden")
            return self._bound_session_id
        if not supplied:
            raise ValueError("session_id is required")
        return supplied

    def _recommendation_session_id(self, args: Mapping[str, Any]) -> str | None:
        supplied = str(args.get("session_id") or "").strip()
        bound = self._bound_session_id or self._recommendation_bound_session_id
        if bound is not None:
            if supplied and supplied != bound:
                raise ValueError("session_id is host-bound and cannot be overridden")
            return bound
        return supplied or None

    def _recommendation_rejections(
        self,
        graph: Any,
        args: Mapping[str, Any],
    ) -> list[str]:
        mode = str(args.get("rejection_mode") or "use").strip().lower()
        if mode not in _REJECTION_MODES:
            raise ValueError("rejection_mode must be one of " + ", ".join(sorted(_REJECTION_MODES)))
        raw = args.get("rejected") or []
        explicit = _recommendation_selection_values(raw) if isinstance(raw, list) else []
        session_id = self._recommendation_session_id(args)
        if session_id is None or mode == "ignore":
            return explicit

        stored = self._lifecycle.recommendation_rejections(session_id=session_id)
        canonical = _canonical_graph_recommendation_ids(graph, explicit)
        next_stored = (
            canonical if mode == "replace" else _recommendation_selection_values(stored + canonical)
        )
        self._lifecycle.remember_recommendation_rejections(
            session_id=session_id,
            rejected=next_stored,
        )
        return _recommendation_selection_values(next_stored + explicit)

    def _serialise_page(
        self,
        path: Path,
        entity_type: str,
        wikilink: str,
        response_format: str,
    ) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return json.dumps({"error": f"could not read {path}: {exc}"})
        return self._serialise_page_text(path, text, entity_type, wikilink, response_format)

    def _serialise_page_text(
        self,
        path: Path,
        text: str,
        entity_type: str,
        wikilink: str,
        response_format: str,
    ) -> str:
        from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body  # noqa: PLC0415

        fm, body = parse_frontmatter_and_body(text)
        encoded_body = body.encode("utf-8")
        body_bytes = len(encoded_body)
        body_truncated = body_bytes > _WIKI_GET_BODY_MAX_BYTES
        if body_truncated:
            body = encoded_body[:_WIKI_GET_BODY_MAX_BYTES].decode(
                "utf-8",
                errors="ignore",
            )
        body_returned_bytes = len(body.encode("utf-8"))
        return _encode_response(
            {
                "slug": path.stem,
                "entity_type": entity_type,
                "wikilink": wikilink,
                "path": _wiki_entity_relpath(entity_type, path.stem),
                "frontmatter": fm,
                "body": body,
                "body_truncated": body_truncated,
                "body_bytes": body_bytes,
                "body_returned_bytes": body_returned_bytes,
                "body_limit_bytes": _WIKI_GET_BODY_MAX_BYTES,
            },
            response_format,
        )

    # ── Lazy caches ─────────────────────────────────────────────────────

    def _ensure_graph(self) -> Any:
        graph_path = self._graph_file_path()
        signature = _graph_file_signature(graph_path) if graph_path is not None else None
        if self._graph is not None and signature == self._graph_signature:
            return self._graph
        from ctx.core.graph.resolve_graph import load_graph  # noqa: PLC0415

        self._graph = load_graph(graph_path)
        self._graph_signature = signature
        return self._graph

    def _ensure_pages(self) -> list[Any]:
        wiki = self._wiki_dir_resolved()
        if wiki is None:
            self._pages = []
            self._pages_signature = None
            return self._pages
        signature = _wiki_pages_signature(wiki)
        if self._pages is not None and signature == self._pages_signature:
            return self._pages
        from ctx.core.wiki.wiki_query import load_all_pages  # noqa: PLC0415

        self._pages = load_all_pages(wiki)
        self._pages_signature = signature
        return self._pages

    def _graph_file_path(self) -> Path | None:
        if self._graph_path is not None:
            if _graph_source_available(self._graph_path):
                return self._graph_path
            wiki = self._wiki_dir_resolved()
            if wiki is not None:
                wiki_graph_path = wiki / "graphify-out" / "graph.json"
                if _graph_source_available(wiki_graph_path):
                    return wiki_graph_path
            return self._graph_path
        wiki = self._wiki_dir_resolved()
        if wiki is not None:
            return wiki / "graphify-out" / "graph.json"
        return None

    def _refresh_semantic_cache_signature(self) -> None:
        signature = _semantic_cache_signature(self._wiki_dir_resolved())
        if signature == self._semantic_signature:
            return
        self._semantic_signature = signature
        try:
            from ctx.core.resolve import recommendations as rec  # noqa: PLC0415

            rec._semantic_cache.clear()
        except Exception:  # noqa: BLE001
            return

    def _wiki_dir_resolved(self) -> Path | None:
        if self._wiki_dir is not None:
            return self._wiki_dir
        try:
            from ctx_config import cfg  # noqa: PLC0415

            return Path(cfg.wiki_dir)
        except Exception:  # noqa: BLE001
            return None


# ── Helpers ────────────────────────────────────────────────────────────────


def _wiki_entity_path(wiki: Path, slug: str, entity_type: str) -> Path:
    path = entity_page_path(wiki, entity_type, slug)
    if path is None:
        raise ValueError(f"unknown entity type {entity_type!r}")
    return path


def _wiki_entity_relpath(entity_type: str, slug: str) -> str:
    relpath = entity_relpath(entity_type, slug)
    if relpath is None:
        raise ValueError(f"unknown entity type {entity_type!r}")
    return relpath.as_posix()


def _wiki_entity_link(slug: str, entity_type: str) -> str:
    link = entity_wikilink(entity_type, slug)
    if link is None:
        raise ValueError(f"unknown entity type {entity_type!r}")
    return link


def _wiki_get_candidates(
    wiki: Path,
    slug: str,
    entity_types: Iterable[str],
) -> list[tuple[str, Path, str]]:
    return [
        (typ, _wiki_entity_path(wiki, slug, typ), _wiki_entity_link(slug, typ))
        for typ in entity_types
    ]


def _normalise_allowed_tool_names(
    tool_names: Iterable[str] | None,
) -> frozenset[str] | None:
    if tool_names is None:
        return None
    return frozenset(str(name).strip() for name in tool_names if str(name).strip())


def _normalise_allowed_entity_types(
    entity_types: Iterable[str] | None,
) -> frozenset[str] | None:
    if entity_types is None:
        return None
    normalised: list[str] = []
    for raw in entity_types:
        entity_type = normalize_entity_type(raw, allowed=RECOMMENDABLE_ENTITY_TYPES)
        if entity_type is None:
            raise ValueError(
                "allowed_entity_types must contain only " + ", ".join(RECOMMENDABLE_ENTITY_TYPES)
            )
        if entity_type not in normalised:
            normalised.append(entity_type)
    return frozenset(normalised)


def _wiki_pack_pages(wiki: Path) -> dict[str, str] | None:
    packs_dir = wiki / "wiki-packs"
    if not packs_dir.is_dir():
        return None
    from ctx.core.wiki.wiki_packs import load_merged_wiki_pages  # noqa: PLC0415

    return load_merged_wiki_pages(packs_dir)


def _file_signature(path: Path) -> FileSignature | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        stat.st_mtime_ns,
        stat.st_size,
        _file_content_fingerprint(path, stat.st_size),
    )


def _graph_file_signature(path: Path) -> GraphSignature:
    return (
        _file_signature(path),
        _file_signature(path.with_name("entity-overlays.jsonl")),
        _graph_pack_signature(path),
    )


def _graph_source_available(path: Path) -> bool:
    return path.is_file() or (path.parent / "packs").is_dir()


def _graph_pack_signature(graph_path: Path) -> PackSignature:
    return _pack_dir_signature(graph_path.parent / "packs")


def _pack_dir_signature(packs_dir: Path) -> PackSignature:
    if not packs_dir.is_dir():
        return ()

    rows: list[tuple[str, FileSignature | None]] = []
    try:
        paths = sorted(path for path in packs_dir.rglob("*") if path.is_file())
    except OSError:
        return (("<unreadable>", None),)
    for path in paths:
        try:
            relpath = path.relative_to(packs_dir).as_posix()
        except ValueError:
            relpath = path.name
        rows.append((relpath, _file_signature(path)))
    return tuple(rows)


def _file_content_fingerprint(path: Path, size: int) -> str:
    hasher = hashlib.blake2b(digest_size=8)
    try:
        with path.open("rb") as fh:
            hasher.update(fh.read(_FILE_SIGNATURE_SAMPLE_BYTES))
            if size > _FILE_SIGNATURE_SAMPLE_BYTES * 2:
                fh.seek(-_FILE_SIGNATURE_SAMPLE_BYTES, 2)
                hasher.update(fh.read(_FILE_SIGNATURE_SAMPLE_BYTES))
            elif size > _FILE_SIGNATURE_SAMPLE_BYTES:
                hasher.update(fh.read())
    except OSError:
        return "unreadable"
    return hasher.hexdigest()


def _wiki_pages_signature(wiki: Path) -> PageSignature:
    entity_root = wiki / "entities"
    count = 0
    newest = 0
    total_size = 0
    if entity_root.is_dir():
        for path in entity_root.rglob("*.md"):
            try:
                stat = path.stat()
            except OSError:
                continue
            count += 1
            newest = max(newest, stat.st_mtime_ns)
            total_size += stat.st_size
    return count, newest, total_size, _pack_dir_signature(wiki / "wiki-packs")


def _semantic_cache_signature(
    wiki: Path | None,
) -> tuple[FileSignature | None, ...] | None:
    cache_dir = _semantic_cache_dir(wiki)
    if cache_dir is None:
        return None
    return (
        _file_signature(cache_dir / "embeddings.npz"),
        _file_signature(cache_dir / "topk-state.json"),
    )


def _semantic_cache_dir(wiki: Path | None) -> Path | None:
    try:
        from ctx_config import cfg  # noqa: PLC0415

        cache_dir = Path(cfg.graph_semantic_cache_dir).expanduser()
    except Exception:  # noqa: BLE001
        if wiki is None:
            return None
        cache_dir = wiki / ".embedding-cache" / "graph"
    else:
        default_cache = Path("~/.claude/skill-wiki/.embedding-cache/graph").expanduser()
        if wiki is not None and cache_dir == default_cache:
            cache_dir = wiki / ".embedding-cache" / "graph"
    return cache_dir


def _query_to_tags(query: str) -> list[str]:
    """Compatibility wrapper around the shared recommendation tokenizer."""
    from ctx.core.resolve.recommendations import query_to_tags  # noqa: PLC0415

    return query_to_tags(query)


def _selection_values_from_args(args: Mapping[str, Any], key: str) -> list[str]:
    raw = args.get(key) or []
    return _recommendation_selection_values(raw) if isinstance(raw, list) else []


def _optional_str(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


def _clip_recommendation_text(value: str, *, max_chars: int = 160) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _recommendation_identity(row: Mapping[str, Any]) -> str:
    entity_type = str(row.get("type") or "tool").strip() or "tool"
    name = str(row.get("name") or "unknown").strip() or "unknown"
    return f"{entity_type}:{name}"


def _base_recommendation_row(row: Mapping[str, Any], *, wiki_dir: Path | None) -> dict[str, Any]:
    base = {
        "name": row["name"],
        "type": row["type"],
        "score": row["score"],
        "normalized_score": row.get("normalized_score"),
        "matching_tags": row.get("matching_tags", []),
        "tags": row.get("tags", []),
        "external": row.get("external", False),
        "external_catalog": row.get("external_catalog"),
        "source_catalog": row.get("source_catalog"),
        "status": row.get("status"),
        "source": row.get("source"),
        "skill_id": row.get("skill_id"),
        "installs": row.get("installs"),
        "detail_url": row.get("detail_url"),
        "install_command": row.get("install_command"),
        "category": row.get("category"),
        "invoke_command": row.get("invoke_command"),
        "security_review": row.get("security_review"),
    }
    base.update(_recommendation_availability(base, wiki_dir=wiki_dir))
    return base


def _recommendation_source_ref(path: Path, *, wiki_dir: Path) -> str:
    try:
        return path.relative_to(wiki_dir).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(wiki_dir.resolve()).as_posix()
        except (OSError, ValueError):
            return path.name


def _recommendation_availability(
    row: Mapping[str, Any],
    *,
    wiki_dir: Path | None,
) -> dict[str, Any]:
    entity_type = str(row.get("type") or "").strip()
    slug = str(row.get("name") or "").strip()
    install_command = str(row.get("install_command") or "").strip()
    source_catalog = str(row.get("source_catalog") or "").strip()
    status = str(row.get("status") or "").strip().lower()
    result: dict[str, Any] = {
        "installable": False,
        "load_status": "unknown",
        "source_path": None,
    }
    if not slug:
        result["load_status"] = "missing-slug"
        return result
    if wiki_dir is None:
        result["load_status"] = "wiki-unavailable"
        return result
    if entity_type == "skill":
        converted = wiki_dir / "converted" / slug
        if converted.is_dir() and not converted.is_symlink():
            for candidate in (converted / "SKILL.md", converted / "SKILL.md.original"):
                if candidate.is_file() and not candidate.is_symlink():
                    result.update(
                        {
                            "installable": True,
                            "load_status": "local-wiki",
                            "source_path": _recommendation_source_ref(candidate, wiki_dir=wiki_dir),
                        }
                    )
                    return result
            result.update(
                {
                    "load_status": "wiki-no-loadable-body",
                    "source_path": _recommendation_source_ref(converted, wiki_dir=wiki_dir),
                }
            )
            return result
        entity_path = entity_page_path(wiki_dir, "skill", slug)
        if entity_path is not None and entity_path.exists():
            result.update(
                {
                    "load_status": "wiki-no-loadable-body",
                    "source_path": _recommendation_source_ref(entity_path, wiki_dir=wiki_dir),
                }
            )
            return result
        if source_catalog or install_command or status in {"available", "remote-cataloged"}:
            result.update(
                {
                    "load_status": "external-install-required",
                    "source_path": install_command or None,
                }
            )
            return result
        result["load_status"] = "not-in-wiki"
        return result
    try:
        entity_path = entity_page_path(wiki_dir, entity_type, slug)
    except Exception:  # noqa: BLE001 - metadata only; invalid type stays unknown.
        return result
    if entity_path is not None and entity_path.exists():
        result.update(
            {
                "installable": True,
                "load_status": "local-wiki",
                "source_path": _recommendation_source_ref(entity_path, wiki_dir=wiki_dir),
            }
        )
    elif source_catalog or install_command or status in {"available", "remote-cataloged"}:
        result.update(
            {
                "load_status": "external-install-required",
                "source_path": install_command or None,
            }
        )
    else:
        result["load_status"] = "not-in-wiki"
    return result


def _is_local_loadable_skill_row(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    source_catalog = str(row.get("source_catalog") or "").strip().lower()
    install_command = str(row.get("install_command") or "").strip()
    load_status = str(row.get("load_status") or "").strip().lower()
    if load_status and load_status != "local-wiki":
        return False
    if status in _REMOTE_SKILL_LOAD_STATUSES:
        return False
    if source_catalog == "skill-index" or install_command:
        return False
    return True


def _is_loadable_recommendation_row(row: Mapping[str, Any]) -> bool:
    if row.get("installable") is not True or row.get("external", False) is not False:
        return False
    raw_type = row.get("type")
    if not isinstance(raw_type, str):
        return False
    entity_type = raw_type.strip().lower()
    if entity_type not in RECOMMENDABLE_ENTITY_TYPES:
        return False
    if entity_type == "skill":
        return _is_local_loadable_skill_row(row)
    return True


def _recommendation_context_from_args(query: str, args: Mapping[str, Any]) -> dict[str, Any]:
    query_lower = query.lower()
    no_api_keys = _optional_bool(args.get("no_api_keys"))
    local_code_task = _optional_bool(args.get("local_code_task"))
    language = _normalize_language_hint(_optional_str(args.get("language"))) or (
        _infer_query_language(query_lower)
    )
    include_unavailable = bool(_optional_bool(args.get("include_unavailable")) or False)
    return {
        "no_api_keys": (
            no_api_keys
            if no_api_keys is not None
            else any(marker in query_lower for marker in _NO_API_KEY_QUERY_MARKERS)
        ),
        "local_code_task": (
            local_code_task
            if local_code_task is not None
            else any(marker in query_lower for marker in _LOCAL_CODE_QUERY_MARKERS)
        ),
        "language": language,
        "include_unavailable": include_unavailable,
    }


def _normalize_language_hint(value: str | None) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return _canonical_language_alias(raw) or raw


def _canonical_language_alias(value: str) -> str | None:
    raw = value.strip().lower()
    if not raw:
        return None
    for language, aliases in _LANGUAGE_TOKEN_ALIASES.items():
        if raw == language or raw in aliases:
            return language
    return None


def _infer_query_language(query_lower: str) -> str | None:
    if match := re.search(r"\blocobench\s+([a-z+#]+)\b", query_lower):
        if language := _canonical_language_alias(match.group(1)):
            return language
    tokens = set(_recommendation_text_tokens(query_lower))
    hits = {
        language: matched
        for language, aliases in _LANGUAGE_TOKEN_ALIASES.items()
        if (matched := tokens & aliases)
    }
    if len(hits) == 1:
        language, matched = next(iter(hits.items()))
        if matched - _AMBIGUOUS_LANGUAGE_TOKEN_ALIASES:
            return language
        return None
    strong_hits = {
        language
        for language, matched in hits.items()
        if matched - _AMBIGUOUS_LANGUAGE_TOKEN_ALIASES
    }
    if len(strong_hits) == 1:
        return next(iter(strong_hits))
    return None


def _recommendation_context_skip_reason(
    row: Mapping[str, Any],
    context: Mapping[str, Any],
) -> str | None:
    local_code_task = bool(context.get("local_code_task"))
    no_api_keys = bool(context.get("no_api_keys"))
    include_unavailable = bool(context.get("include_unavailable"))
    text_tokens = set(_recommendation_text_tokens(_recommendation_context_text(row)))
    if (
        (local_code_task or no_api_keys)
        and not include_unavailable
        and not _is_loadable_recommendation_row(row)
    ):
        return "not locally loadable for local/no-key context"
    if no_api_keys and text_tokens & _EXTERNAL_SERVICE_TOKENS:
        return "external service requires credentials"
    if local_code_task and text_tokens & _GENERIC_PLANNING_TOKENS:
        return "generic planning workflow for local code task"
    language = str(context.get("language") or "").strip().lower()
    if language:
        target_aliases = _LANGUAGE_TOKEN_ALIASES.get(language, frozenset({language}))
        other_language_hits = {
            lang
            for lang, aliases in _LANGUAGE_TOKEN_ALIASES.items()
            if lang != language and text_tokens & aliases
        }
        if other_language_hits and not (text_tokens & target_aliases):
            return "wrong language recommendation"
    return None


def _recommendation_context_text(row: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "name",
        "type",
        "source",
        "source_catalog",
        "status",
        "skill_id",
        "detail_url",
        "install_command",
        "category",
        "invoke_command",
    ):
        value = row.get(key)
        if value:
            values.append(str(value))
    for key in ("matching_tags", "shared_tags", "tags"):
        tags = row.get(key) or []
        if isinstance(tags, list):
            values.extend(str(tag) for tag in tags)
    return " ".join(values)


def _recommendation_text_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", value.lower())


def _recommendation_context_policy(
    *,
    baseline_context: list[str],
    active_context: list[Any],
    results: list[dict[str, Any]],
    rejected_context: list[str] | None = None,
) -> dict[str, Any]:
    active = _recommendation_selection_values(active_context)
    action_reasons = _active_context_action_reasons(
        baseline_context=baseline_context,
        active_context=active_context,
        rejected_context=rejected_context or [],
    )
    action_keys = _recommendation_selection_keys(list(action_reasons))
    keep = [
        value
        for value in _recommendation_selection_values(baseline_context + active)
        if _recommendation_selection_key(value) not in action_keys
    ]
    keep_keys = _recommendation_selection_keys(keep)
    loadable = [
        row
        for row in results
        if _is_loadable_recommendation_row(row)
        and not (_recommendation_selection_keys([str(row.get("id") or "")]) & keep_keys)
    ]
    initial = next(
        (row for row in loadable if str(row.get("type") or "").strip().lower() == "skill"),
        None,
    )
    initial_id = str(initial["id"]) if initial is not None else None
    unload_candidates = [
        value for value in active if _recommendation_selection_key(value) in action_keys
    ]
    replacements: list[dict[str, str]] = []
    if initial_id is not None and unload_candidates:
        replaced = unload_candidates.pop(0)
        replacements.append(
            {
                "unload": replaced,
                "load": initial_id,
                "reason": action_reasons[_recommendation_selection_key(replaced)],
            }
        )
    return {
        "baseline": baseline_context,
        "keep": keep,
        "load": [initial_id] if initial_id is not None and not replacements else [],
        "deferred": [row["id"] for row in loadable if row["id"] != initial_id],
        "manual": [row["id"] for row in results if not _is_loadable_recommendation_row(row)],
        "unload": unload_candidates,
        "replace": replacements,
    }


def _active_context_action_reasons(
    *,
    baseline_context: list[str],
    active_context: list[Any],
    rejected_context: list[str],
) -> dict[str, str]:
    baseline_keys = _recommendation_selection_keys(baseline_context)
    rejected_keys = _recommendation_selection_keys(rejected_context)
    reasons: dict[str, str] = {}
    for raw in active_context:
        values = _recommendation_selection_values([raw])
        if not values:
            continue
        value = values[0]
        key = _recommendation_selection_key(value)
        if key in baseline_keys:
            continue
        if key in rejected_keys:
            reasons[key] = "active context was explicitly rejected in this session"
            continue
        if isinstance(raw, Mapping) and _is_stale_applied_context(raw):
            reasons[key] = "host marked applied context as stale"
    return reasons


def _is_stale_applied_context(raw: Mapping[str, Any]) -> bool:
    load_status = str(raw.get("load_status") or "").strip().lower()
    applied = raw.get("applied") is True or load_status in {"active", "applied", "loaded"}
    state = str(raw.get("status") or "").strip().lower()
    stale = raw.get("stale") is True or raw.get("unload_candidate") is True or state == "stale"
    return applied and stale


def _recommendation_tags(row: Mapping[str, Any]) -> list[str]:
    raw = row.get("matching_tags", [])
    if not isinstance(raw, list):
        return []
    return [str(tag).strip() for tag in raw if str(tag).strip()]


def _recommendation_tldr(row: Mapping[str, Any]) -> str:
    description = (
        _optional_str(row.get("description"))
        or _optional_str(row.get("summary"))
        or _optional_str(row.get("title"))
    )
    if description:
        return _clip_recommendation_text(description)

    entity_type = str(row.get("type") or "tool").strip() or "tool"
    category = _optional_str(row.get("category"))
    tags = _recommendation_tags(row)
    if tags:
        prefix = f"{category} {entity_type}" if category else entity_type
        return _clip_recommendation_text(f"{prefix} matching {', '.join(tags[:4])}.")

    catalog = _optional_str(row.get("source_catalog")) or _optional_str(row.get("external_catalog"))
    if bool(row.get("external")) and catalog:
        return _clip_recommendation_text(f"External {entity_type} from {catalog}.")
    return _clip_recommendation_text(f"{entity_type} recommendation.")


def _recommendation_reason(row: Mapping[str, Any]) -> str:
    parts: list[str] = []
    tags = _recommendation_tags(row)
    if tags:
        parts.append(f"matches tags {', '.join(tags[:6])}")
    category = _optional_str(row.get("category"))
    if category:
        parts.append(f"category {category}")
    source = _optional_str(row.get("source_catalog")) or _optional_str(row.get("source"))
    if source:
        parts.append(f"source {source}")
    normalized_score = row.get("normalized_score")
    if isinstance(normalized_score, (int, float)):
        parts.append(f"normalized score {float(normalized_score):.3f}")
    if not parts:
        return "Ranked by ctx recommendation graph."
    return _clip_recommendation_text("; ".join(parts), max_chars=220)


def _with_recommendation_selection_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    enriched["id"] = _recommendation_identity(row)
    enriched["tldr"] = _recommendation_tldr(row)
    enriched["reason"] = _recommendation_reason(row)
    enriched["selected"] = False
    enriched["selection_state"] = "suggested"
    return enriched


def _recommendation_selection_values(values: list[Any]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            raw = value.get("id") or (
                f"{value.get('type')}:{value.get('name')}"
                if value.get("type") and value.get("name")
                else value.get("name")
            )
        else:
            raw = value
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item:
            continue
        key = _recommendation_selection_key(item)
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
    return selected


def _recommendation_selection_keys(values: list[str]) -> set[str]:
    keys: set[str] = set()
    for value in values:
        item = str(value or "").strip().lower()
        if not item:
            continue
        keys.add(_recommendation_selection_key(item))
    return keys


def _recommendation_selection_key(value: str) -> str:
    entity_type, name = _recommendation_selection_parts(value)
    if entity_type is not None:
        return f"{entity_type}:{name.lower()}"
    return name.lower()


def _recommendation_selection_parts(value: str) -> tuple[str | None, str]:
    item = str(value or "").strip()
    if ":" not in item:
        return None, item
    raw_type, raw_name = item.split(":", 1)
    entity_type = _RECOMMENDATION_ENTITY_TYPE_ALIASES.get(raw_type.strip().lower())
    name = raw_name.strip()
    if entity_type is None or not name:
        return None, item
    return entity_type, name


def _canonical_graph_recommendation_ids(graph: Any, values: list[str]) -> list[str]:
    canonical_nodes = {
        str(node_id).lower(): str(node_id)
        for node_id in graph.nodes
        if _recommendation_selection_parts(str(node_id))[0] is not None
    }
    labels: dict[str, list[str]] = {}
    for node_id, data in graph.nodes(data=True):
        canonical = canonical_nodes.get(str(node_id).lower())
        if canonical is None:
            continue
        label = str(data.get("label") or data.get("name") or "").strip().lower()
        if label:
            labels.setdefault(label, []).append(canonical)

    resolved: list[str] = []
    for value in _recommendation_selection_values(values):
        entity_type, name = _recommendation_selection_parts(value)
        if entity_type is not None:
            candidate = canonical_nodes.get(f"{entity_type}:{name}".lower())
        else:
            matches = labels.get(name.lower(), [])
            candidate = matches[0] if len(matches) == 1 else None
        if candidate is not None:
            resolved.append(candidate)
    return _recommendation_selection_values(resolved)


def _recommendation_selection_node_ids(graph: Any, values: list[str]) -> set[str]:
    node_ids: set[str] = set()
    for value in values:
        entity_type, name = _recommendation_selection_parts(value)
        if not name:
            continue
        if entity_type is not None:
            node_id = f"{entity_type}:{name}"
            if node_id in graph:
                node_ids.add(node_id)
            continue
        for candidate_type in RECOMMENDABLE_ENTITY_TYPES:
            node_id = f"{candidate_type}:{name}"
            if node_id in graph:
                node_ids.add(node_id)
    return node_ids


def _resolve_related_recommendation_rows(
    graph: Any,
    seed_ids: set[str],
    *,
    max_hops: int,
    top_n: int,
) -> list[dict[str, Any]]:
    if not seed_ids:
        return []
    scores: dict[str, float] = {}
    via: dict[str, list[str]] = {}
    shared_tags_map: dict[str, list[str]] = {}
    visited = set(seed_ids)
    frontier = list(seed_ids)

    for hop in range(max_hops):
        next_frontier: list[str] = []
        decay = 1.0 / (hop + 1)
        for node_id in frontier:
            for neighbor in graph.neighbors(node_id):
                if neighbor in seed_ids or not _related_node_is_recommendable(graph, neighbor):
                    continue
                edge_data = graph[node_id][neighbor]
                try:
                    weight = float(edge_data.get("weight", 1)) * decay
                except (TypeError, ValueError):
                    weight = decay
                scores[neighbor] = scores.get(neighbor, 0.0) + weight
                seed_label = node_id.split(":", 1)[-1]
                via.setdefault(neighbor, [])
                if seed_label not in via[neighbor]:
                    via[neighbor].append(seed_label)
                shared_tags_map.setdefault(neighbor, [])
                for tag in edge_data.get("shared_tags", []):
                    if tag not in shared_tags_map[neighbor]:
                        shared_tags_map[neighbor].append(tag)
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
        frontier = next_frontier

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_n]
    max_score = max((score for _, score in ranked), default=0.0) or 1.0
    results: list[dict[str, Any]] = []
    for node_id, score in ranked:
        node_data = graph.nodes.get(node_id, {})
        entity_type = str(node_data.get("type") or node_id.split(":", 1)[0])
        name = str(node_data.get("label") or node_id.split(":", 1)[-1])
        result = {
            "name": name,
            "type": entity_type,
            "score": round(score, 2),
            "normalized_score": round(score / max_score, 4),
            "shared_tags": shared_tags_map.get(node_id, [])[:8],
            "tags": [str(tag).lower() for tag in node_data.get("tags", []) if tag],
            "via": via.get(node_id, [])[:4],
        }
        for key in (
            "external",
            "external_catalog",
            "source_catalog",
            "status",
            "source",
            "skill_id",
            "installs",
            "detail_url",
            "install_command",
            "category",
            "invoke_command",
            "security_review",
        ):
            if key in node_data:
                result[key] = node_data.get(key)
        results.append(result)
    return results


def _related_node_is_recommendable(graph: Any, node_id: str) -> bool:
    node_data = graph.nodes.get(node_id, {})
    entity_type = str(node_data.get("type") or node_id.split(":", 1)[0])
    if entity_type not in RECOMMENDABLE_ENTITY_TYPES:
        return False
    status = str(node_data.get("status") or "").strip().lower()
    if status in _RELATED_BLOCKED_STATUSES:
        return False
    return not _truthy_recommendation_flag(node_data.get("never_load"))


def _truthy_recommendation_flag(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _related_recommendation_reason(row: Mapping[str, Any]) -> str:
    parts: list[str] = []
    via = row.get("via", [])
    if isinstance(via, list) and via:
        parts.append(f"related via {', '.join(str(v) for v in via[:4])}")
    tags = _recommendation_tags(row)
    if tags:
        parts.append(f"shares tags {', '.join(tags[:6])}")
    normalized_score = row.get("normalized_score")
    if isinstance(normalized_score, (int, float)):
        parts.append(f"normalized score {float(normalized_score):.3f}")
    if not parts:
        return "Related by ctx recommendation graph."
    return _clip_recommendation_text("; ".join(parts), max_chars=220)


def _recommend_companion_harnesses(
    query: str,
    *,
    top_k: int,
    model_provider: str | None,
    model: str | None,
) -> list[dict[str, Any]]:
    try:
        from ctx_init import recommend_harnesses  # noqa: PLC0415

        raw = recommend_harnesses(
            query,
            top_k=top_k,
            model_provider=model_provider,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("ctx harness companion recommendation failed: %s", exc)
        return []
    return [
        {
            "name": row.get("name"),
            "type": "harness",
            "fit_score": row.get("fit_score"),
            "normalized_score": row.get("normalized_score"),
            "matching_tags": row.get("matching_tags", []),
            "provider_match": row.get("provider_match"),
            "detail_url": row.get("detail_url"),
            "install_command": row.get("install_command"),
        }
        for row in raw
        if row.get("name")
    ]


def _lifecycle_tool_definitions(
    bound_session_id: str | None = None,
) -> list[ToolDefinition]:
    entity_enum = list(RECOMMENDABLE_ENTITY_TYPES)
    session = {
        "type": "string",
        "description": "Host-generated session id for lifecycle correlation.",
    }
    entity_type = {
        "type": "string",
        "enum": entity_enum,
        "description": "Entity type to load/use/unload.",
    }
    slug = {
        "type": "string",
        "description": "Entity slug from ctx recommendations or wiki search.",
    }
    definitions = [
        ToolDefinition(
            name=f"{_NAMESPACE}observe_dev_event",
            description=(
                "Record the current development event for a custom/API/local "
                "harness. Use before recommending when the host has task, "
                "file, error, or verification context to preserve."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "event_type": {"type": "string"},
                    "host": {"type": "string"},
                    "cwd": {"type": "string"},
                    "payload": {"type": "object"},
                },
                "required": ["session_id", "event_type"],
            },
        ),
        ToolDefinition(
            name=f"{_NAMESPACE}load_entity",
            description=(
                "Request that the host consider loading a recommended skill, "
                "agent, MCP server, or harness. This is advisory and does not "
                "prove selection or apply activation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "entity_type": entity_type,
                    "slug": slug,
                    "reason": {"type": "string"},
                    "source_context": {
                        "type": "object",
                        "description": (
                            "Optional privacy-sanitized context such as the "
                            "recommendation surface or workflow that caused activation."
                        ),
                    },
                    "security_scan": {
                        "type": "object",
                        "description": (
                            "Optional SkillSpector scan proof for skill loads. "
                            "Pass the scanner result object returned by the host "
                            "when available. If omitted for a skill, ctx records "
                            "a not_provided warning in session_state."
                        ),
                    },
                },
                "required": ["session_id", "entity_type", "slug"],
            },
        ),
        ToolDefinition(
            name=f"{_NAMESPACE}mark_entity_used",
            description="Record evidence that a loaded ctx entity was useful.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "entity_type": entity_type,
                    "slug": slug,
                    "evidence": {"type": "string"},
                    "token_usage": {
                        "type": "object",
                        "description": (
                            "Optional per-tool usage evidence. Only pass exact "
                            "token counts when the host/provider can attribute "
                            "them to this entity; otherwise set attribution to "
                            "estimated or unavailable."
                        ),
                        "properties": {
                            "attribution": {
                                "type": "string",
                                "enum": ["exact", "estimated", "unavailable"],
                            },
                            "input_tokens": {"type": "integer", "minimum": 0},
                            "cached_input_tokens": {"type": "integer", "minimum": 0},
                            "cache_write_input_tokens": {"type": "integer", "minimum": 0},
                            "uncached_input_tokens": {"type": "integer", "minimum": 0},
                            "output_tokens": {"type": "integer", "minimum": 0},
                            "total_tokens": {"type": "integer", "minimum": 0},
                            "tokens_reported": {"type": "boolean"},
                            "cost_usd": {"type": "number", "minimum": 0},
                            "attribution_reason": {"type": "string"},
                            "provider": {"type": "string"},
                            "model": {"type": "string"},
                        },
                    },
                },
                "required": ["session_id", "entity_type", "slug"],
            },
        ),
        ToolDefinition(
            name=f"{_NAMESPACE}record_validation",
            description=(
                "Record a harness validation check result outside the chat "
                "transcript so state can be inspected and replayed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "check_name": {
                        "type": "string",
                        "description": "Stable name of the check that ran.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["passed", "failed", "skipped", "error"],
                    },
                    "command": {"type": "string"},
                    "summary": {"type": "string"},
                    "entity_type": entity_type,
                    "slug": slug,
                    "payload": {"type": "object"},
                },
                "required": ["session_id", "check_name", "status"],
            },
        ),
        ToolDefinition(
            name=f"{_NAMESPACE}record_escalation",
            description=(
                "Record that a predefined escalation condition was reached "
                "so the host can ask the user instead of hiding state in chat."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "trigger": {
                        "type": "string",
                        "description": "Stable escalation trigger name.",
                    },
                    "reason": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "blocking"],
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "resolved", "ignored"],
                    },
                    "entity_type": entity_type,
                    "slug": slug,
                    "payload": {"type": "object"},
                },
                "required": ["session_id", "trigger", "reason"],
            },
        ),
        ToolDefinition(
            name=f"{_NAMESPACE}unload_entity",
            description=(
                "Record that the host/user chose to unload a ctx entity. "
                "Hosts should ask the user before calling this."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "entity_type": entity_type,
                    "slug": slug,
                    "reason": {"type": "string"},
                },
                "required": ["session_id", "entity_type", "slug"],
            },
        ),
        ToolDefinition(
            name=f"{_NAMESPACE}session_end",
            description="Record that a custom/API/local harness session ended.",
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "status": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["session_id"],
            },
        ),
        ToolDefinition(
            name=f"{_NAMESPACE}session_state",
            description=(
                "Read the current lifecycle state for a session, including "
                "loaded entities, used entities, validation checks, "
                "escalations, and unload candidates that were loaded but "
                "have no usage evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "min_unused_seconds": {
                        "type": "number",
                        "description": (
                            "Minimum age before an unused loaded entity is "
                            "returned as an unload candidate. Default 0."
                        ),
                    },
                },
                "required": ["session_id"],
            },
        ),
    ]
    if bound_session_id is not None:
        for definition in definitions:
            properties = definition.parameters.get("properties")
            if isinstance(properties, dict):
                properties.pop("session_id", None)
            required = definition.parameters.get("required")
            if isinstance(required, list):
                definition.parameters["required"] = [
                    name for name in required if name != "session_id"
                ]
    return definitions


def _dict_arg(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _float_arg(raw: Any) -> float:
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _optional_bool(raw: Any) -> bool | None:
    return raw if isinstance(raw, bool) else None


def _excerpt(body: str, max_chars: int) -> str:
    """Short preview of a page body: first non-empty line, trimmed.

    The wiki body often starts with a markdown heading; take the
    first line that isn't a heading or blank and clip it. No
    markdown rendering — this is for the model's reasoning context,
    not human display.
    """
    if not body:
        return ""
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        if len(s) > max_chars:
            return s[: max_chars - 1].rstrip() + "…"
        return s
    return ""


def _clamp_int(raw: Any, *, default: int, lo: int, hi: int) -> int:
    """Coerce ``raw`` to an int clamped to ``[lo, hi]``. Default on parse fail."""
    try:
        v = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        v = default
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


# ── Tool-executor factory ──────────────────────────────────────────────────


def make_tool_executor(
    toolbox: CtxCoreToolbox,
    fallback: Callable[[ToolCall], str] | None = None,
) -> Callable[[ToolCall], str]:
    """Return a tool_executor that routes ctx__* calls to the toolbox.

    Non-ctx calls fall through to ``fallback`` (or raise if none).
    Lets callers compose the toolbox with their own custom tools.
    """

    def _executor(call: ToolCall) -> str:
        if toolbox.owns(call.name):
            return toolbox.dispatch(call)
        if fallback is not None:
            return fallback(call)
        raise ValueError(f"no executor for tool {call.name!r}")

    return _executor
