"""ctx.api — blessed public Python API for third-party harness authors.

The ``ctx/`` package has lots of internal modules (``ctx.core.graph``,
``ctx.core.wiki``, ``ctx.adapters.generic.*``) that were stable for
the MCP server and the first-party ``ctx run`` CLI, but are not
great entrypoints for someone building their own loop. This module
is the one stable, flat namespace such callers should target.

Four delivery paths, in increasing order of coupling to ctx:

1. **Attach the MCP server.** Zero Python dependency on ctx — your
   harness just spawns ``ctx-mcp-server`` and speaks JSON-RPC. The
   right choice for anything already MCP-aware (Claude Agent SDK,
   Cline, Goose, OpenHands). See ``docs/harness/attaching-to-hosts.md``.

2. **Import this module.** Use the functions below from Python —
   each one wraps a single ctx.core query with safe defaults. The
   right choice when you have your own agent loop and want the
   recommendations inline without subprocess overhead.

3. **Use ``ctx run`` directly.** The full harness-over-LiteLLM
   experience, no host-side code required. Good if you don't already
   have a loop.

4. **Use the LoopFlow adapter.** If another runner already owns
   plan/act/observe, call ``python -m ctx.adapters.loopflow`` or
   ``ctx.adapters.loopflow.recommend_for_loop`` before planning to get
   a permissioned JSON contract for the current loop.

Public functions:

    recommend_bundle(query, *, top_k=5)
        Free-text → ranked skill/agent/MCP execution bundle.

    graph_query(seeds, *, max_hops=2, top_n=10)
        Walk the knowledge graph from seed entity names.

    wiki_search(query, *, top_n=15)
        Keyword search wiki entity pages.

    wiki_get(slug, *, entity_type=None)
        Fetch one wiki entity by slug — frontmatter + body.

    list_all_entities(entity_type=None)
        Enumerate every slug in the wiki (filterable by type).

    default_wiki_dir()
        Resolve the configured wiki directory (``~/.claude/skill-wiki``
        by default) — lets callers pre-build a custom CtxCoreToolbox
        pointed at a non-default location.

Adapter support helpers:

    ctx_core_tool_names()
        Return ctx-core tool names exposed by the shared toolbox.

    recommendation_graph()
        Return the shared recommendation graph for adapter-side ranking.

Plan 001 Phase H9.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall
from ctx.core.entity_types import RECOMMENDABLE_ENTITY_TYPES
from ctx.telemetry import hash_identifier, record_event, record_exception, telemetry_span


__all__ = [
    "recommend_bundle",
    "graph_query",
    "wiki_search",
    "wiki_get",
    "list_all_entities",
    "default_wiki_dir",
    "CtxCoreToolbox",
]


# Module-level singleton toolbox — lazy, shared across calls. Saves
# loading the 13k-node graph on every function call.
_default_toolbox: CtxCoreToolbox | None = None
_TOOL_EVENT_NAMES = {
    "ctx__recommend_bundle": "ctx.api.recommend_bundle",
    "ctx__graph_query": "ctx.api.graph_query",
    "ctx__wiki_search": "ctx.api.wiki_search",
    "ctx__wiki_get": "ctx.api.wiki_get",
}


def _get_toolbox() -> CtxCoreToolbox:
    global _default_toolbox
    if _default_toolbox is None:
        _default_toolbox = CtxCoreToolbox()
    return _default_toolbox


def _duration_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _hash_json_value(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hash_identifier(encoded)


def _safe_argument_payload(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ctx.operation": tool_name.removeprefix("ctx__"),
        "ctx.tool.name": tool_name,
        "ctx.arguments.keys": sorted(arguments),
    }
    for key in ("top_k", "top_n", "max_hops"):
        value = arguments.get(key)
        if isinstance(value, (int, float)):
            payload[f"ctx.arguments.{key}"] = value
    query = arguments.get("query")
    if isinstance(query, str):
        payload["ctx.query.hash"] = hash_identifier(query)
        payload["ctx.query.length"] = len(query)
    seeds = arguments.get("seeds")
    if isinstance(seeds, (list, tuple)):
        payload["ctx.seeds.count"] = len(seeds)
        payload["ctx.seeds.hash"] = _hash_json_value(list(seeds))
    slug = arguments.get("slug")
    if isinstance(slug, str):
        payload["ctx.slug.hash"] = hash_identifier(slug)
    entity_type = arguments.get("entity_type")
    if isinstance(entity_type, str):
        payload["ctx.entity.type"] = entity_type
    return payload


def _result_payload(result: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"ctx.result.has_error_payload": "error" in result}
    results = result.get("results")
    if isinstance(results, list):
        payload["ctx.result.count"] = len(results)
    elif "error" not in result and result:
        payload["ctx.result.count"] = 1
    return payload


def _record_api_event(
    event_name: str,
    *,
    payload: dict[str, Any],
    outcome: str,
    duration_ms: float,
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
                source="ctx-api",
                exc=exc,
                transport="python-api",
                outcome=outcome,
                duration_ms=duration_ms,
                error_kind=error_kind,
                payload=payload,
            )
        else:
            record_event(
                event_name,
                source="ctx-api",
                transport="python-api",
                outcome=outcome,
                duration_ms=duration_ms,
                error_kind=error_kind,
                payload=payload,
            )
    except Exception:  # noqa: BLE001 - telemetry must never break the API.
        pass


def _call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke one CtxCoreToolbox tool, return the parsed JSON result."""
    started = time.perf_counter()
    event_name = _TOOL_EVENT_NAMES.get(tool_name, "ctx.api.tool_call")
    event_payload = _safe_argument_payload(tool_name, arguments)
    toolbox = _get_toolbox()
    with telemetry_span():
        try:
            raw = toolbox.dispatch(ToolCall(id="api", name=tool_name, arguments=arguments))
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 - preserve existing propagation.
            _record_api_event(
                event_name,
                payload=event_payload,
                outcome="error",
                duration_ms=_duration_ms(started),
                error_kind=type(exc).__name__,
                exc=exc,
            )
            raise

        outcome = "error" if isinstance(payload, dict) and "error" in payload else "ok"
        if isinstance(payload, dict):
            event_payload.update(_result_payload(payload))
        _record_api_event(
            event_name,
            payload=event_payload,
            outcome=outcome,
            duration_ms=_duration_ms(started),
            error_kind="structured_error" if outcome == "error" else None,
        )
        return payload


# ── Public API ─────────────────────────────────────────────────────────────


def recommend_bundle(
    query: str,
    *,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Return a top-K ranked recommendation bundle for a free-text query.

    Each entry is a dict with: ``name``, ``type``, ``score``,
    ``matching_tags``. Empty list on any error (missing graph,
    empty query, etc.); the CLI/MCP versions surface errors as
    structured payload, but library callers usually just want a list.

    Example:

        from ctx import recommend_bundle

        bundle = recommend_bundle("build a FastAPI service with auth", top_k=5)
        for entry in bundle:
            print(f"{entry['type']:>11}  {entry['name']}  (score {entry['score']:.1f})")
    """
    payload = _call(
        "ctx__recommend_bundle",
        {"query": query, "top_k": top_k},
    )
    return payload.get("results", []) if "error" not in payload else []


def graph_query(
    seeds: list[str],
    *,
    max_hops: int = 2,
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """Walk the knowledge graph from seed entity names. See ``recommend_bundle``.

    Each entry carries ``name``, ``type``, ``score``,
    ``normalized_score``, ``shared_tags``, ``via`` (which seeds it
    was reached from).
    """
    payload = _call(
        "ctx__graph_query",
        {"seeds": seeds, "max_hops": max_hops, "top_n": top_n},
    )
    return payload.get("results", []) if "error" not in payload else []


def wiki_search(
    query: str,
    *,
    top_n: int = 15,
) -> list[dict[str, Any]]:
    """Keyword search across wiki entity pages.

    Each entry: ``slug``, ``title``, ``excerpt``, ``tags``, ``status``,
    ``score``.
    """
    payload = _call(
        "ctx__wiki_search",
        {"query": query, "top_n": top_n},
    )
    return payload.get("results", []) if "error" not in payload else []


def wiki_get(
    slug: str,
    *,
    entity_type: str | None = None,
) -> dict[str, Any] | None:
    """Fetch one entity page by slug. Returns None if not found.

    ``entity_type`` optionally disambiguates duplicate slugs across
    skills, agents, MCP servers, and harnesses.

    Result dict on hit: ``slug``, ``path``, ``frontmatter``, ``body``.
    Errors (invalid slug, traversal attempt, file missing) all map to
    ``None`` — library callers get a simple "exists or not" contract.
    """
    args: dict[str, Any] = {"slug": slug}
    if entity_type is not None:
        args["entity_type"] = entity_type
    payload = _call("ctx__wiki_get", args)
    if "error" in payload:
        return None
    return payload


def list_all_entities(
    entity_type: str | None = None,
) -> list[str]:
    """Return every entity slug in the wiki.

    ``entity_type`` filters by type when given; valid values:
    ``'skill'``, ``'agent'``, ``'mcp-server'``, ``'harness'``. Pass
    None (default) to get every entity across all recommendable types.
    """
    started = time.perf_counter()
    event_payload: dict[str, Any] = {
        "ctx.operation": "list_all_entities",
        "ctx.arguments.keys": ["entity_type"] if entity_type is not None else [],
    }
    if entity_type is not None:
        event_payload["ctx.entity.type"] = entity_type
    try:
        wiki = default_wiki_dir()
        event_payload["ctx.wiki.available"] = bool(wiki is not None and wiki.is_dir())
        if wiki is None or not wiki.is_dir():
            result: list[str] = []
            outcome = "error"
            error_kind = "wiki_unavailable"
        elif entity_type is not None and entity_type not in RECOMMENDABLE_ENTITY_TYPES:
            result = []
            outcome = "error"
            error_kind = "invalid_entity_type"
        else:
            from ctx.core.wiki.wiki_query import load_all_pages  # noqa: PLC0415

            result = sorted(
                {
                    page.name
                    for page in load_all_pages(wiki)
                    if entity_type is None or page.entity_type == entity_type
                }
            )
            outcome = "ok"
            error_kind = None
    except Exception as exc:  # noqa: BLE001 - preserve existing propagation.
        _record_api_event(
            "ctx.api.list_all_entities",
            payload=event_payload,
            outcome="error",
            duration_ms=_duration_ms(started),
            error_kind=type(exc).__name__,
        )
        raise

    event_payload["ctx.result.count"] = len(result)
    _record_api_event(
        "ctx.api.list_all_entities",
        payload=event_payload,
        outcome=outcome,
        duration_ms=_duration_ms(started),
        error_kind=error_kind,
    )
    return result


def ctx_core_tool_names() -> list[str]:
    """Return ctx-core tool names exposed by the shared toolbox for adapter payloads."""
    return [definition.name for definition in _get_toolbox().tool_definitions()]


def recommendation_graph() -> Any:
    """Return the shared recommendation graph for adapter-side ranking."""
    return _get_toolbox()._ensure_graph()


def default_wiki_dir() -> Path | None:
    """Resolve the configured wiki directory. None when no config is reachable.

    Falls through to ``~/.claude/skill-wiki`` when the config module
    isn't importable (e.g. a harness that has ctx.core but no ctx_config
    setup). Returns None if even that fallback doesn't exist on disk
    so callers can give a clean error instead of walking a nonexistent
    tree.
    """
    try:
        from ctx_config import cfg  # noqa: PLC0415

        wiki = Path(cfg.wiki_dir)
    except Exception:  # noqa: BLE001
        import os

        wiki = Path(os.path.expanduser("~/.claude/skill-wiki"))
    return wiki if wiki.exists() else None
