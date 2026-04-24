"""
test_public_api.py -- Pin the ctx library's public API surface.

Goal: anything a third-party harness imports from ``ctx`` or
``ctx.api`` is guaranteed stable across non-major releases. This
suite is the canary — if a future refactor drops a name, moves a
function, or changes a signature, this fails loudly.

Covers:
  * Every name in ``ctx.__all__`` is importable.
  * Every name in ``ctx.api.__all__`` matches a public entry.
  * Function signatures (kwarg shape) don't change silently.
  * Each function works end-to-end against a synthetic wiki/graph.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import networkx as nx
import pytest

import ctx
import ctx.api


# ── Synthetic wiki + graph fixture ─────────────────────────────────────────


@pytest.fixture()
def synthetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a tiny wiki + graph under tmp and point ctx at it.

    Layout mirrors ``~/.claude/skill-wiki`` exactly so
    ``default_wiki_dir()`` (which falls back to that path when
    ctx_config isn't reachable) finds our synthetic corpus.
    """
    wiki = tmp_path / ".claude" / "skill-wiki"
    skills = wiki / "entities" / "skills"
    skills.mkdir(parents=True)
    agents = wiki / "entities" / "agents"
    agents.mkdir(parents=True)
    mcp_a = wiki / "entities" / "mcp-servers" / "f"
    mcp_a.mkdir(parents=True)

    (skills / "python-patterns.md").write_text(
        "---\nname: python-patterns\ntitle: Python Patterns\n"
        "tags: [python, patterns]\nstatus: cataloged\n---\n# body\n",
        encoding="utf-8",
    )
    (skills / "fastapi-pro.md").write_text(
        "---\nname: fastapi-pro\ntitle: FastAPI Pro\n"
        "tags: [python, api, web]\nstatus: cataloged\n---\n"
        "# FastAPI Pro\nBuild production FastAPI services.\n",
        encoding="utf-8",
    )
    (agents / "code-reviewer.md").write_text(
        "---\nname: code-reviewer\ntitle: Code Reviewer\n"
        "tags: [python, review]\nstatus: cataloged\n---\n# body\n",
        encoding="utf-8",
    )
    (mcp_a / "filesystem.md").write_text(
        "---\nname: filesystem\ntitle: Filesystem MCP\n"
        "tags: [filesystem]\nstatus: cataloged\n---\n# body\n",
        encoding="utf-8",
    )

    # Graph
    G = nx.Graph()
    G.add_node("skill:python-patterns", label="python-patterns",
               type="skill", tags=["python", "patterns"])
    G.add_node("skill:fastapi-pro", label="fastapi-pro",
               type="skill", tags=["python", "api", "web"])
    G.add_node("agent:code-reviewer", label="code-reviewer",
               type="agent", tags=["python", "review"])
    G.add_edge("skill:python-patterns", "skill:fastapi-pro",
               weight=0.8, shared_tags=["python"])
    G.add_edge("skill:python-patterns", "agent:code-reviewer",
               weight=0.6, shared_tags=["python"])
    (wiki / "graphify-out").mkdir()
    (wiki / "graphify-out" / "graph.json").write_text(
        json.dumps(nx.node_link_data(G, edges="edges")), encoding="utf-8",
    )

    # Point ctx-config and the module-level toolbox singleton at the
    # synthetic corpus.
    home = tmp_path
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    # Reset the singleton toolbox so it re-resolves paths against the
    # synthetic corpus (rather than caching the real one from a prior
    # test in the same session).
    from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
    monkeypatch.setattr(
        ctx.api, "_default_toolbox",
        CtxCoreToolbox(
            wiki_dir=wiki,
            graph_path=wiki / "graphify-out" / "graph.json",
        ),
    )
    # ``default_wiki_dir`` reads from ``ctx_config.cfg`` which is a
    # cached singleton resolved at first-import time — if a prior
    # test loaded the real wiki, the HOME monkeypatch won't undo that.
    # Patch the function directly so list_all_entities + callers see
    # the synthetic wiki regardless of what ctx_config thinks.
    monkeypatch.setattr(ctx.api, "default_wiki_dir", lambda: wiki)
    return home


# ── __all__ + re-export shape ─────────────────────────────────────────────


class TestPublicApiShape:
    # Names third-party harnesses must keep seeing verbatim.
    _REQUIRED_NAMES = frozenset({
        "CtxCoreToolbox",
        "default_wiki_dir",
        "graph_query",
        "list_all_entities",
        "recommend_bundle",
        "wiki_get",
        "wiki_search",
    })

    def test_top_level_exports(self) -> None:
        assert self._REQUIRED_NAMES <= set(ctx.__all__)

    def test_top_level_names_resolve(self) -> None:
        for name in self._REQUIRED_NAMES:
            assert hasattr(ctx, name), f"ctx.{name} missing"

    def test_ctx_api_all_matches(self) -> None:
        # ctx.api is where the real implementations live; ctx re-exports.
        assert self._REQUIRED_NAMES <= set(ctx.api.__all__)

    def test_version_is_string(self) -> None:
        assert isinstance(ctx.__version__, str)
        assert ctx.__version__

    def test_same_callable_after_reexport(self) -> None:
        """ctx.recommend_bundle must be the SAME object as ctx.api.recommend_bundle."""
        assert ctx.recommend_bundle is ctx.api.recommend_bundle
        assert ctx.graph_query is ctx.api.graph_query
        assert ctx.wiki_search is ctx.api.wiki_search
        assert ctx.wiki_get is ctx.api.wiki_get


# ── Signatures (pinning the kwarg shape) ──────────────────────────────────


class TestSignatures:
    def test_recommend_bundle_signature(self) -> None:
        sig = inspect.signature(ctx.recommend_bundle)
        params = list(sig.parameters.values())
        assert params[0].name == "query"
        # top_k must remain keyword-only with default 5.
        top_k = sig.parameters["top_k"]
        assert top_k.kind == inspect.Parameter.KEYWORD_ONLY
        assert top_k.default == 5

    def test_graph_query_signature(self) -> None:
        sig = inspect.signature(ctx.graph_query)
        assert list(sig.parameters)[0] == "seeds"
        assert sig.parameters["max_hops"].default == 2
        assert sig.parameters["top_n"].default == 10

    def test_wiki_search_signature(self) -> None:
        sig = inspect.signature(ctx.wiki_search)
        assert list(sig.parameters)[0] == "query"
        assert sig.parameters["top_n"].default == 15

    def test_wiki_get_signature(self) -> None:
        sig = inspect.signature(ctx.wiki_get)
        assert list(sig.parameters) == ["slug"]

    def test_list_all_entities_signature(self) -> None:
        sig = inspect.signature(ctx.list_all_entities)
        assert sig.parameters["entity_type"].default is None


# ── End-to-end function behaviour ─────────────────────────────────────────


class TestRecommendBundle:
    def test_happy_path(self, synthetic_home: Path) -> None:
        bundle = ctx.recommend_bundle("python web api")
        assert isinstance(bundle, list)
        names = [row["name"] for row in bundle]
        assert "fastapi-pro" in names

    def test_empty_query_returns_empty_list(
        self, synthetic_home: Path,
    ) -> None:
        assert ctx.recommend_bundle("") == []

    def test_top_k_passed_through(self, synthetic_home: Path) -> None:
        bundle = ctx.recommend_bundle("python", top_k=1)
        assert len(bundle) <= 1


class TestGraphQuery:
    def test_happy_path(self, synthetic_home: Path) -> None:
        results = ctx.graph_query(["python-patterns"])
        names = [r["name"] for r in results]
        assert "fastapi-pro" in names or "code-reviewer" in names

    def test_empty_seeds_returns_empty(self, synthetic_home: Path) -> None:
        assert ctx.graph_query([]) == []

    def test_top_n_clamp_propagates(self, synthetic_home: Path) -> None:
        results = ctx.graph_query(["python-patterns"], top_n=1)
        assert len(results) <= 1


class TestWikiSearch:
    def test_finds_matching_pages(self, synthetic_home: Path) -> None:
        results = ctx.wiki_search("python")
        slugs = [r["slug"] for r in results]
        assert len(slugs) >= 1

    def test_empty_query(self, synthetic_home: Path) -> None:
        assert ctx.wiki_search("") == []


class TestWikiGet:
    def test_hit_returns_frontmatter_and_body(
        self, synthetic_home: Path,
    ) -> None:
        page = ctx.wiki_get("python-patterns")
        assert page is not None
        assert page["slug"] == "python-patterns"
        assert "frontmatter" in page
        assert "body" in page

    def test_miss_returns_none(self, synthetic_home: Path) -> None:
        assert ctx.wiki_get("does-not-exist") is None

    def test_invalid_slug_returns_none(
        self, synthetic_home: Path,
    ) -> None:
        assert ctx.wiki_get("../../etc/passwd") is None


class TestListAllEntities:
    def test_no_filter_returns_all_types(
        self, synthetic_home: Path,
    ) -> None:
        entities = ctx.list_all_entities()
        assert "python-patterns" in entities
        assert "fastapi-pro" in entities
        assert "code-reviewer" in entities
        assert "filesystem" in entities

    def test_skill_filter(self, synthetic_home: Path) -> None:
        entities = ctx.list_all_entities(entity_type="skill")
        assert "python-patterns" in entities
        assert "code-reviewer" not in entities   # it's an agent

    def test_agent_filter(self, synthetic_home: Path) -> None:
        entities = ctx.list_all_entities(entity_type="agent")
        assert entities == ["code-reviewer"]

    def test_mcp_filter(self, synthetic_home: Path) -> None:
        entities = ctx.list_all_entities(entity_type="mcp-server")
        assert entities == ["filesystem"]

    def test_empty_wiki_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Point default_wiki_dir at a nonexistent path.
        monkeypatch.setattr(ctx.api, "default_wiki_dir", lambda: None)
        assert ctx.list_all_entities() == []


class TestDefaultWikiDir:
    def test_returns_path_when_wiki_exists(
        self, synthetic_home: Path,
    ) -> None:
        # Fixture patched default_wiki_dir to return the synthetic
        # corpus — confirm the re-export still points at the patch.
        result = ctx.default_wiki_dir()
        assert result is not None
        assert isinstance(result, Path)

    def test_unpatched_returns_none_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exercise the un-patched implementation (not via the fixture).

        When neither ctx_config nor ~/.claude/skill-wiki is reachable,
        default_wiki_dir must return None — lets a caller distinguish
        'no wiki here' from 'exception'.
        """
        from ctx.api import default_wiki_dir as real_default_wiki_dir

        monkeypatch.setenv("HOME", str(tmp_path / "does-not-exist"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "does-not-exist"))
        # Break the ctx_config singleton so the fallback branch runs.
        import sys
        monkeypatch.setitem(sys.modules, "ctx_config", None)
        result = real_default_wiki_dir()
        assert result is None


class TestCtxCoreToolboxRexport:
    def test_is_class(self) -> None:
        assert isinstance(ctx.CtxCoreToolbox, type)

    def test_instantiable_without_args(self) -> None:
        # Doesn't load anything at construction time.
        toolbox = ctx.CtxCoreToolbox()
        defs = toolbox.tool_definitions()
        assert len(defs) == 4
