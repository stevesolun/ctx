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

Install semantics are DELIBERATELY out of scope for v1:
    A generic-harness install would mean "stage this skill's body
    into the next turn's context" (no filesystem auto-load like
    Claude Code has). That is more opinionated than ctx.core should
    be, so it lives in the host adapter layer (H7 ctx-run CLI
    decides how to surface the recommendation; H9 per-host adapters
    like Aider get their own install path). For now, the model
    *recommends* and *reads*; the user or a higher-level adapter
    chooses whether to install.

Plan 001 Phase H6.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ctx.adapters.generic.providers import ToolCall, ToolDefinition
from ctx.adapters.generic.tools import TOOL_SEPARATOR


_logger = logging.getLogger(__name__)


# Tool names all live under the "ctx" namespace, consistent with the
# MCP router's <server>__<tool> convention. The harness dispatches
# calls with names starting "ctx{TOOL_SEPARATOR}" to CtxCoreToolbox,
# anything else falls back to its normal tool_executor.
_NAMESPACE = f"ctx{TOOL_SEPARATOR}"


@dataclass(frozen=True)
class BundleEntry:
    """One row of a recommendation result.

    ``score`` is the raw graph-walk weight; ``normalized_score`` is
    that value divided by the top score in the same result set (so
    the highest-ranked entry is always 1.0 — lets a caller apply a
    0.0-1.0 cutoff without knowing the absolute graph scale).
    """

    name: str
    entity_type: str   # 'skill' | 'agent' | 'mcp-server'
    score: float
    normalized_score: float
    shared_tags: tuple[str, ...]
    via: tuple[str, ...]


class CtxCoreToolbox:
    """Read-only ctx-core surface, packaged as harness tools.

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
    ) -> None:
        self._wiki_dir = wiki_dir
        self._graph_path = graph_path
        self._graph: Any | None = None       # networkx.Graph
        self._pages: list[Any] | None = None  # list[SkillPage]

    # ── Public Protocol surface ─────────────────────────────────────────

    def tool_definitions(self) -> list[ToolDefinition]:
        """Return the list of tools this toolbox exposes to the model."""
        return [
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
                            "description": "How many entries to return. Default 5.",
                            "minimum": 1,
                            "maximum": 50,
                        },
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
                    "already know a specific skill or MCP and want "
                    "to find its close neighbours."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "seeds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Seed entity names (skill / agent / "
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
                    },
                    "required": ["seeds"],
                },
            ),
            ToolDefinition(
                name=f"{_NAMESPACE}wiki_search",
                description=(
                    "Keyword search across the llm-wiki entity pages "
                    "(skills + agents + mcp-servers). Matches against "
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
                    },
                    "required": ["slug"],
                },
            ),
        ]

    def dispatch(self, call: ToolCall) -> str:
        """Execute one ctx-core tool call. Returns a JSON string.

        Returning JSON (not a bare string) keeps the model's
        mental model of tool output consistent — every ctx-core
        tool produces structured data, and the model can parse it
        back on the next turn to reason about specific fields.
        """
        if not call.name.startswith(_NAMESPACE):
            raise ValueError(
                f"CtxCoreToolbox got a non-ctx call {call.name!r}"
            )
        local_name = call.name[len(_NAMESPACE):]
        args = call.arguments or {}

        if local_name == "recommend_bundle":
            return self._dispatch_recommend(args)
        if local_name == "graph_query":
            return self._dispatch_graph_query(args)
        if local_name == "wiki_search":
            return self._dispatch_wiki_search(args)
        if local_name == "wiki_get":
            return self._dispatch_wiki_get(args)

        raise ValueError(f"unknown ctx-core tool {local_name!r}")

    def owns(self, tool_name: str) -> bool:
        """True when this toolbox is the dispatcher for the given name."""
        return tool_name.startswith(_NAMESPACE)

    # ── Individual dispatchers ──────────────────────────────────────────

    def _dispatch_recommend(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query must be non-empty", "results": []})
        top_k = _clamp_int(args.get("top_k"), default=5, lo=1, hi=50)

        tags = _query_to_tags(query)
        if not tags:
            return json.dumps({
                "error": "query produced no usable tags",
                "results": [],
            })

        graph = self._ensure_graph()
        if graph.number_of_nodes() == 0:
            return json.dumps({
                "error": "knowledge graph not available; run ctx-wiki-graphify",
                "results": [],
            })

        from ctx.core.resolve.recommendations import recommend_by_tags  # noqa: PLC0415

        # Pass the original query through so the recommender can apply
        # semantic-similarity scoring (in addition to tag/slug-token).
        # Falls through silently if the embedding cache is missing.
        raw = recommend_by_tags(graph, tags, top_n=top_k, query=query)
        results = [
            {
                "name": r["name"],
                "type": r["type"],
                "score": r["score"],
                "matching_tags": r.get("matching_tags", []),
            }
            for r in raw
        ]
        return json.dumps({"query": query, "tags": tags, "results": results})

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
            return json.dumps({
                "error": "knowledge graph not available; run ctx-wiki-graphify",
                "results": [],
            })

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
        return json.dumps({"seeds": seeds, "results": results})

    def _dispatch_wiki_search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query must be non-empty", "results": []})
        top_n = _clamp_int(args.get("top_n"), default=15, lo=1, hi=100)

        pages = self._ensure_pages()
        if not pages:
            return json.dumps({
                "error": "wiki has no pages",
                "results": [],
            })

        from ctx.core.wiki.wiki_query import search_by_query  # noqa: PLC0415

        hits = search_by_query(pages, query, top_n=top_n)
        results = [
            {
                "slug": p.name,
                "title": p.title or p.name,
                # SkillPage doesn't have a description field; expose a
                # short body excerpt instead so the model can rank by
                # content snippet rather than an empty string.
                "excerpt": _excerpt(p.body, 160),
                "tags": list(p.tags),
                "status": p.status,
                "score": p.score,
            }
            for p in hits
        ]
        return json.dumps({"query": query, "results": results})

    def _dispatch_wiki_get(self, args: dict[str, Any]) -> str:
        slug = str(args.get("slug", "")).strip()
        if not slug:
            return json.dumps({"error": "slug must be non-empty"})

        # Validate — ctx-core's validator rejects traversal shapes.
        from ctx.core.wiki.wiki_utils import validate_skill_name  # noqa: PLC0415

        try:
            validate_skill_name(slug)
        except ValueError as exc:
            return json.dumps({"error": f"invalid slug: {exc}"})

        wiki = self._wiki_dir_resolved()
        if wiki is None:
            return json.dumps({"error": "wiki_dir not configured"})

        # Try each known entity layout.
        candidates = [
            wiki / "entities" / "skills" / f"{slug}.md",
            wiki / "entities" / "agents" / f"{slug}.md",
        ]
        # MCP pages are sharded.
        first = slug[0] if slug and slug[0].isalpha() else "0-9"
        candidates.append(
            wiki / "entities" / "mcp-servers" / first / f"{slug}.md"
        )

        for path in candidates:
            if path.is_file():
                return self._serialise_page(path)

        return json.dumps({
            "error": f"no entity page found for slug {slug!r}",
            "looked_in": [str(p) for p in candidates],
        })

    def _serialise_page(self, path: Path) -> str:
        from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body  # noqa: PLC0415

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return json.dumps({"error": f"could not read {path}: {exc}"})
        fm, body = parse_frontmatter_and_body(text)
        return json.dumps({
            "slug": path.stem,
            "path": str(path),
            "frontmatter": fm,
            "body": body,
        })

    # ── Lazy caches ─────────────────────────────────────────────────────

    def _ensure_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        from ctx.core.graph.resolve_graph import load_graph  # noqa: PLC0415

        self._graph = load_graph(self._graph_path)
        return self._graph

    def _ensure_pages(self) -> list[Any]:
        if self._pages is not None:
            return self._pages
        wiki = self._wiki_dir_resolved()
        if wiki is None:
            self._pages = []
            return self._pages
        from ctx.core.wiki.wiki_query import load_all_pages  # noqa: PLC0415

        self._pages = load_all_pages(wiki)
        return self._pages

    def _wiki_dir_resolved(self) -> Path | None:
        if self._wiki_dir is not None:
            return self._wiki_dir
        try:
            from ctx_config import cfg  # noqa: PLC0415
            return Path(cfg.wiki_dir)
        except Exception:  # noqa: BLE001
            return None


# ── Helpers ────────────────────────────────────────────────────────────────


_TAG_STOPWORDS: frozenset[str] = frozenset({
    # Tiny stoplist for query→tags tokenisation. Not a real NLP
    # pipeline — callers who need precision should use graph_query
    # with explicit seed names instead.
    "the", "a", "an", "and", "or", "but", "for", "with", "of", "to",
    "on", "in", "at", "by", "as", "is", "are", "was", "were", "be",
    "how", "what", "when", "where", "why", "which", "who", "can",
    "i", "you", "me", "my", "your", "our", "we", "they", "their",
    "help", "please", "need", "want", "use", "using", "find",
    "looking", "looking-for", "task",
})


def _query_to_tags(query: str) -> list[str]:
    """Extract tag-shaped tokens from a free-text query.

    Lowercases, strips non-alnum chars except '-' and '_', drops
    stopwords and tokens < 3 chars. Deduplicates while preserving
    order (first occurrence wins). The resulting list is what
    ``resolve_by_tags`` treats as candidate match tags.
    """
    tokens = re.findall(r"[A-Za-z0-9_\-]+", query.lower())
    seen: dict[str, None] = {}
    for t in tokens:
        if len(t) < 3 or t in _TAG_STOPWORDS:
            continue
        seen.setdefault(t, None)
    return list(seen.keys())


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
