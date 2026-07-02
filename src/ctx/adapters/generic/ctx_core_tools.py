"""ctx.adapters.generic.ctx_core_tools — expose ctx-core as harness tools.

This is the integration point that makes the alive skill system
available to ANY LLM running through the generic harness. The
toolbox wraps the read-only query surface of ctx.core — graph
walks, skill/agent/MCP recommendations, wiki search — as
``ToolDefinition``/dispatcher pairs that slot into ``run_loop``.

Tools exposed (all namespaced under the ``ctx__`` prefix, matching
the MCP router's separator convention so the harness can route to
the toolbox via the same tool-dispatch path it already uses for
MCP servers):

    ctx__recommend_bundle(query, top_k=5)
        Free-text → top-K cross-type bundle (skill + agent + MCP).
        Tokenizes the query into tags, walks the graph.

    ctx__graph_query(seeds, max_hops=2, top_n=10)
        Direct graph walk from a list of seed entity names.
        Exposed for advanced agentic flows that already know
        which entities are relevant.

    ctx__wiki_search(query, top_n=15)
        Keyword search across wiki entity pages — title, description,
        tags. Returns the top matches with their slugs + descriptions.

    ctx__wiki_get(slug)
        Fetch a single entity page by slug — returns its full
        frontmatter + body for the model to reason about.

Load/unload tools are explicit lifecycle records, not filesystem
auto-installs. The host remains responsible for asking the user and
deciding how to place selected entities into context.

Plan 001 Phase H6.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ctx.adapters.generic.providers import ToolCall, ToolDefinition
from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore
from ctx.adapters.generic.tools import TOOL_SEPARATOR
from ctx.core.entity_types import (
    RECOMMENDABLE_ENTITY_TYPES,
    entity_relpath,
    entity_page_path,
    entity_wikilink,
)
from ctx.telemetry import hash_identifier, record_event, record_exception, telemetry_span


_logger = logging.getLogger(__name__)


# Tool names all live under the "ctx" namespace, consistent with the
# MCP router's <server>__<tool> convention. The harness dispatches
# calls with names starting "ctx{TOOL_SEPARATOR}" to CtxCoreToolbox,
# anything else falls back to its normal tool_executor.
_NAMESPACE = f"ctx{TOOL_SEPARATOR}"
_FILE_SIGNATURE_SAMPLE_BYTES = 64 * 1024
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
    "graph_query": "ctx.core.graph_query",
    "wiki_search": "ctx.core.wiki_search",
    "wiki_get": "ctx.core.wiki_get",
}
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

    The toolbox is stateless after initialisation — calls are
    independent and safe to parallelise (the MCP router already
    serialises per-server anyway).
    """

    def __init__(
        self,
        *,
        wiki_dir: Path | None = None,
        graph_path: Path | None = None,
        lifecycle_dir: Path | None = None,
        bound_session_id: str | None = None,
    ) -> None:
        self._wiki_dir = wiki_dir
        self._graph_path = graph_path
        self._lifecycle = RuntimeLifecycleStore(lifecycle_dir)
        self._bound_session_id = str(bound_session_id or "").strip() or None
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
                    "the full frontmatter (as a dict) and body text. "
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
        ]
        definitions.extend(_lifecycle_tool_definitions(self._bound_session_id))
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
        local_name = call.name[len(_NAMESPACE) :]
        args = call.arguments or {}
        started = time.perf_counter()
        event_name = _CORE_EVENT_NAMES.get(
            local_name,
            "ctx.core.lifecycle" if local_name in _LIFECYCLE_LOCAL_NAMES else "ctx.core.tool_call",
        )
        event_payload = _safe_tool_payload(local_name, args)
        session_id = str(args.get("session_id") or "").strip() or self._bound_session_id

        with telemetry_span():
            try:
                if local_name == "recommend_bundle":
                    result = self._dispatch_recommend(args)
                elif local_name == "graph_query":
                    result = self._dispatch_graph_query(args)
                elif local_name == "wiki_search":
                    result = self._dispatch_wiki_search(args)
                elif local_name == "wiki_get":
                    result = self._dispatch_wiki_get(args)
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
        raw = recommend_by_tags(
            graph,
            tags,
            top_n=top_k,
            query=query,
            entity_types=("skill", "agent", "mcp-server"),
            min_normalized_score=cfg.recommendation_min_normalized_score,
            use_semantic_query=use_semantic_query,
            semantic_cache_dir=semantic_cache_dir,
        )
        results = [
            {
                "name": r["name"],
                "type": r["type"],
                "score": r["score"],
                "normalized_score": r.get("normalized_score"),
                "matching_tags": r.get("matching_tags", []),
                "external": r.get("external", False),
                "external_catalog": r.get("external_catalog"),
                "source_catalog": r.get("source_catalog"),
                "status": r.get("status"),
                "source": r.get("source"),
                "skill_id": r.get("skill_id"),
                "installs": r.get("installs"),
                "detail_url": r.get("detail_url"),
                "install_command": r.get("install_command"),
                "category": r.get("category"),
                "invoke_command": r.get("invoke_command"),
                "security_review": r.get("security_review"),
            }
            for r in raw
        ]
        model_provider = _optional_str(args.get("model_provider"))
        model = _optional_str(args.get("model"))
        companion_harnesses = (
            _recommend_companion_harnesses(
                query,
                top_k=top_k,
                model_provider=model_provider,
                model=model,
            )
            if model_provider or model
            else []
        )
        return _encode_response(
            {
                "query": query,
                "tags": tags,
                "results": results,
                "companion_harnesses": companion_harnesses,
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

        pages = self._ensure_pages()
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

        # Validate — ctx-core's validator rejects traversal shapes.
        from ctx.core.wiki.wiki_utils import validate_skill_name  # noqa: PLC0415

        try:
            validate_skill_name(slug)
        except ValueError as exc:
            return json.dumps({"error": f"invalid slug: {exc}"})

        wiki = self._wiki_dir_resolved()
        if wiki is None:
            return json.dumps({"error": "wiki_dir not configured"})

        candidates = _wiki_get_candidates(wiki, slug, entity_type or None)
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
                "looked_in": [str(p) for _, p, _ in candidates],
            }
        )

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
                )
            elif name == "mark_entity_used":
                result = self._lifecycle.mark_entity_used(
                    session_id=session_id,
                    entity_type=str(args.get("entity_type") or ""),
                    slug=str(args.get("slug") or ""),
                    evidence=str(args.get("evidence") or "") or None,
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
        return _encode_response(
            {
                "slug": path.stem,
                "entity_type": entity_type,
                "wikilink": wikilink,
                "path": str(path),
                "frontmatter": fm,
                "body": body,
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
    entity_type: str | None,
) -> list[tuple[str, Path, str]]:
    entity_types = [entity_type] if entity_type else list(RECOMMENDABLE_ENTITY_TYPES)
    return [
        (typ, _wiki_entity_path(wiki, slug, typ), _wiki_entity_link(slug, typ))
        for typ in entity_types
    ]


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


def _optional_str(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value or None


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
                "Record that the host/user chose to load a recommended skill, "
                "agent, MCP server, or harness into the current session."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": session,
                    "entity_type": entity_type,
                    "slug": slug,
                    "reason": {"type": "string"},
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
