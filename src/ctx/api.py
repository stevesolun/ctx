"""ctx.api — blessed public Python API for third-party harness authors.

The ``ctx/`` package has lots of internal modules (``ctx.core.graph``,
``ctx.core.wiki``, ``ctx.adapters.generic.*``) that were stable for
the MCP server and the first-party ``ctx run`` CLI, but are not
great entrypoints for someone building their own loop. This module
is the one stable, flat namespace such callers should target.

Three delivery paths, in increasing order of coupling to ctx:

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

Plan 001 Phase H9.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall
from ctx.core.entity_types import (
    RECOMMENDABLE_ENTITY_TYPES,
    SUBJECT_TYPE_FOR_ENTITY_TYPE,
)


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


def _get_toolbox() -> CtxCoreToolbox:
    global _default_toolbox
    if _default_toolbox is None:
        _default_toolbox = CtxCoreToolbox()
    return _default_toolbox


def _call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke one CtxCoreToolbox tool, return the parsed JSON result."""
    toolbox = _get_toolbox()
    raw = toolbox.dispatch(
        ToolCall(id="api", name=tool_name, arguments=arguments)
    )
    return json.loads(raw)


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
    wiki = default_wiki_dir()
    if wiki is None or not wiki.is_dir():
        return []
    if entity_type is not None and entity_type not in RECOMMENDABLE_ENTITY_TYPES:
        return []

    slugs: list[str] = []
    for current_type in RECOMMENDABLE_ENTITY_TYPES:
        if entity_type is not None and entity_type != current_type:
            continue
        subject_type = SUBJECT_TYPE_FOR_ENTITY_TYPE[current_type]
        root = wiki / "entities" / subject_type
        if current_type == "mcp-server":
            if root.is_dir():
                for shard in root.iterdir():
                    if shard.is_dir():
                        slugs.extend(p.stem for p in shard.glob("*.md"))
        else:
            slugs.extend(p.stem for p in root.glob("*.md"))
    return sorted(set(slugs))


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
