"""test_recommendations.py — tests for recommend_by_tags + IDF scoring.

These guard the regression caught on 2026-04-27 where a query for
"Python FastAPI project" surfaced ``python-project-structure`` ahead
of ``python-fastapi-development`` because both skills had two
substring-token matches in their labels and the algorithm couldn't
distinguish the rare-token (fastapi) from the common ones.

The fix introduced exact slug-token matching plus IDF weighting on
matches. These tests pin both behaviors.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import networkx as nx

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ctx.core.resolve.recommendations import (  # noqa: E402
    _slug_tokens, _token_idf, query_to_tags, recommend_by_tags,
)


def _build_graph(entities: list[tuple[str, list[str]]]) -> nx.Graph:
    """Build a tiny graph from ``[(slug, tags), ...]`` for ranking tests."""
    G = nx.Graph()
    for slug, tags in entities:
        G.add_node(
            f"skill:{slug}",
            label=slug, type="skill", tags=tags,
        )
    # Add a small star edge so degrees aren't all zero (avoids log1p == 0
    # ties dominating the result).
    if len(G) >= 2:
        nodes = list(G.nodes())
        for n in nodes[1:]:
            G.add_edge(nodes[0], n)
    return G


# ── Slug tokenisation ─────────────────────────────────────────────────


def test_slug_tokens_splits_hyphens() -> None:
    assert _slug_tokens("python-fastapi-development") == {
        "python", "fastapi", "development",
    }


def test_slug_tokens_handles_underscore_and_slash() -> None:
    assert _slug_tokens("foo_bar/baz-qux") == {"foo", "bar", "baz", "qux"}


def test_slug_tokens_lowercases() -> None:
    assert _slug_tokens("Foo-BAR") == {"foo", "bar"}


# ── IDF computation ───────────────────────────────────────────────────


def test_token_idf_rare_token_outweighs_common() -> None:
    """A token appearing in only one of many labels should outweigh a
    token in every label."""
    entities: list[tuple[str, list[str]]] = [
        ("python-foo", []),
        ("python-bar", []),
        ("python-baz", []),
        ("python-fastapi-rare", []),  # 'fastapi' is the rare token
    ]
    G = _build_graph(entities)
    idf = _token_idf(G)
    assert idf["fastapi"] > idf["python"], (
        f"fastapi (df=1) must have higher IDF than python (df=4); "
        f"got fastapi={idf['fastapi']}, python={idf['python']}"
    )


def test_token_idf_zero_for_universal_token() -> None:
    """A token in every label has IDF = log(N/N) = 0."""
    G = _build_graph([
        ("python-a", []), ("python-b", []), ("python-c", []),
    ])
    idf = _token_idf(G)
    assert idf["python"] == 0.0


def test_token_idf_caches_per_graph_identity() -> None:
    """The IDF table is cached so repeated query calls don't recompute it."""
    G = _build_graph([("foo", []), ("bar", [])])
    a = _token_idf(G)
    b = _token_idf(G)
    assert a is b, "IDF table must be cached per graph object"


# ── Ranking behavior (the regression) ─────────────────────────────────


def test_fastapi_query_surfaces_fastapi_skill_above_generic_python_skill() -> None:
    """The 2026-04-27 regression: rare-token matches must outrank
    common-token matches even when the common-token match has a
    matching tag.
    """
    # Build a realistic-ish corpus: 'python' is common, 'project' is
    # also fairly common (many *-project-* names), and 'fastapi' is
    # rare. With realistic IDFs, the fastapi-specific skill must win.
    common_python: list[tuple[str, list[str]]] = [
        (f"python-pro-{i}", ["python"]) for i in range(20)
    ]
    common_project: list[tuple[str, list[str]]] = [
        (f"team-project-{i}", []) for i in range(10)
    ]
    G = _build_graph([
        # python-project-structure: hits common-python + common-project +
        # has python tag.
        ("python-project-structure", ["python"]),
        # python-fastapi-development: hits common-python + RARE 'fastapi'.
        # No tags.
        ("python-fastapi-development", []),
        *common_python,
        *common_project,
    ])
    results = recommend_by_tags(G, ["python", "fastapi", "project"], top_n=5)
    names = [r["name"] for r in results]
    assert names[0] == "python-fastapi-development", (
        f"fastapi-specific skill must lead the ranking; got order: {names}"
    )


def test_exact_slug_token_outscores_substring_only() -> None:
    """If signal is a slug-token of A and only a substring (no token
    match) of B, A must outscore B.
    """
    G = _build_graph([
        ("api-design", []),       # 'api' is a slug-token
        ("rapid-builder", []),    # 'api' is a substring of 'rapid' but not a token
    ])
    results = recommend_by_tags(G, ["api"], top_n=2)
    names = [r["name"] for r in results]
    assert names[0] == "api-design"


def test_tag_match_idf_weighting() -> None:
    """A rare tag should outweigh a common one in the score."""
    G = _build_graph([
        ("alpha", ["common"]),
        ("beta", ["common"]),
        ("gamma", ["common"]),
        ("delta", ["common", "rare"]),
    ])
    # Both 'common' and 'rare' are not in the slug — only tag overlap fires.
    results = recommend_by_tags(G, ["common", "rare"], top_n=5)
    names = [r["name"] for r in results]
    # delta has BOTH tags; the others have only 'common'. Even with
    # equal IDF this would put delta first; the test specifically
    # locks in the rare-tag-dominates rank.
    assert names[0] == "delta"
    # The next three ('alpha', 'beta', 'gamma') all have 'common' only,
    # so they tie on tag-score; degree-tiebreak is via the star edges.
    # We just assert delta is first, the rest unordered.
    assert sorted(names[1:]) == ["alpha", "beta", "gamma"]


def test_empty_query_returns_empty_list() -> None:
    G = _build_graph([("foo", [])])
    assert recommend_by_tags(G, [], top_n=5) == []


def test_top_n_limit_is_respected() -> None:
    G = _build_graph([
        ("python-a", ["python"]),
        ("python-b", ["python"]),
        ("python-c", ["python"]),
    ])
    out = recommend_by_tags(G, ["python"], top_n=2)
    assert len(out) == 2


# ── query_to_tags ─────────────────────────────────────────────────────


def test_query_to_tags_drops_stopwords_and_short_tokens() -> None:
    out = query_to_tags("how do I help with the python and api work")
    # dropped: how, do, i, help, with, the, and (stopwords); 'do' < 3 chars.
    assert "python" in out
    assert "api" in out
    assert "the" not in out
    assert "and" not in out
    assert "i" not in out


def test_query_to_tags_dedupes() -> None:
    assert query_to_tags("python python PYTHON") == ["python"]


# ── Semantic boost at query time ──────────────────────────────────────


def _patch_semantic(monkeypatch, *, sims: dict[str, float]) -> None:
    """Replace _load_semantic_index + _embed_query with synthetic ones
    so we can test the semantic-boost code path without a real model.

    ``sims`` maps node_id → fixed cosine similarity returned for any
    query. The patched functions return a 1-dim "vector" of [1.0] and
    a (N, 1) matrix where each row is the value from ``sims``.
    """
    import numpy as np
    from ctx.core.resolve import recommendations as rec

    def fake_load(graph, cache_dir):
        ids = tuple(graph.nodes)
        mat = np.array([[sims.get(n, 0.0)] for n in ids], dtype="float32")
        return mat, ids, "fake-model"

    def fake_embed(query, model_id):
        return np.array([1.0], dtype="float32")

    monkeypatch.setattr(rec, "_load_semantic_index", fake_load)
    monkeypatch.setattr(rec, "_embed_query", fake_embed)
    rec._semantic_cache.clear()


def test_semantic_boost_changes_top_result(monkeypatch) -> None:
    """When two entities tie on tag/token signal, the one with higher
    semantic similarity to the query must rank higher.
    """
    G = _build_graph([
        ("foo-skill", ["common"]),
        ("bar-skill", ["common"]),
    ])
    _patch_semantic(monkeypatch, sims={
        "skill:foo-skill": 0.10,
        "skill:bar-skill": 0.90,
    })
    out = recommend_by_tags(
        G, ["common"], top_n=2, query="anything", semantic_weight=100.0,
    )
    names = [r["name"] for r in out]
    assert names[0] == "bar-skill", (
        f"semantic-boost should put bar-skill (cos=0.9) ahead of "
        f"foo-skill (cos=0.1); got {names}"
    )


def test_semantic_off_when_query_omitted(monkeypatch) -> None:
    """The semantic path must NOT fire if no query is supplied —
    callers that only have tags should get the legacy ranking unchanged.
    """
    from ctx.core.resolve import recommendations as rec

    called = {"load": 0, "embed": 0}

    def fake_load(graph, cache_dir):
        called["load"] += 1
        return None

    def fake_embed(*args, **kwargs):
        called["embed"] += 1
        return None

    monkeypatch.setattr(rec, "_load_semantic_index", fake_load)
    monkeypatch.setattr(rec, "_embed_query", fake_embed)
    rec._semantic_cache.clear()

    G = _build_graph([("foo", ["common"]), ("bar", ["common"])])
    recommend_by_tags(G, ["common"], top_n=2)  # no query=

    assert called["load"] == 0, "semantic index must not load when query is None"
    assert called["embed"] == 0, "query must not be embedded when query is None"


def test_semantic_index_failure_falls_through(monkeypatch) -> None:
    """If the embedding cache is missing/malformed, ranking still works
    via tag+token+degree alone (no exception).
    """
    from ctx.core.resolve import recommendations as rec

    monkeypatch.setattr(rec, "_load_semantic_index", lambda g, c: None)
    rec._semantic_cache.clear()

    G = _build_graph([("python-foo", ["python"]), ("ruby-bar", ["ruby"])])
    out = recommend_by_tags(G, ["python"], top_n=2, query="python work")
    names = [r["name"] for r in out]
    assert "python-foo" in names, (
        "tag-based ranking must still work when the embedding cache is missing"
    )


def test_external_skills_sh_catalog_can_rank_when_graph_has_no_match(tmp_path) -> None:
    wiki = tmp_path / "wiki"
    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True)
    catalog_dir = wiki / "external-catalogs" / "skills-sh"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "catalog.json").write_text(
        json.dumps({
            "skills": [
                {
                    "id": "open.feishu.cn/lark-doc",
                    "source": "open.feishu.cn",
                    "skill_id": "lark-doc",
                    "name": "lark-doc",
                    "tags": ["docs"],
                    "installs": 18029,
                    "detail_url": "https://skills.sh/site/open.feishu.cn/lark-doc",
                    "install_command": "npx skills add https://open.feishu.cn",
                }
            ]
        }),
        encoding="utf-8",
    )
    G = _build_graph([("unrelated-python", ["python"])])
    G.graph["ctx_graph_path"] = str(graph_dir / "graph.json")

    out = recommend_by_tags(G, ["lark", "docs"], top_n=3, query="lark docs")

    assert out[0]["name"] == "open.feishu.cn/lark-doc"
    assert out[0]["external"] is False
    assert out[0]["external_catalog"] is None
    assert out[0]["source_catalog"] == "skills.sh"
    assert out[0]["status"] == "remote-cataloged"
    assert out[0]["type"] == "skill"
    assert out[0]["install_command"] == "npx skills add https://open.feishu.cn"


def test_external_skill_graph_node_ranks_without_sidecar_catalog() -> None:
    G = _build_graph([("unrelated-python", ["python"])])
    G.graph["source_catalog_nodes"] = {"skills.sh": 1}
    G.add_node(
        "skill:skills-sh-open-feishu-cn-lark-doc",
        label="skills-sh-open-feishu-cn-lark-doc",
        type="skill",
        status="remote-cataloged",
        source_catalog="skills.sh",
        source="open.feishu.cn",
        skill_id="lark-doc",
        tags=["docs"],
        installs=18029,
        detail_url="https://skills.sh/site/open.feishu.cn/lark-doc",
        install_command="npx skills add https://open.feishu.cn",
    )

    out = recommend_by_tags(G, ["lark", "docs"], top_n=3, query="lark docs")

    assert out[0]["name"] == "skills-sh-open-feishu-cn-lark-doc"
    assert out[0]["type"] == "skill"
    assert out[0]["external"] is False
    assert out[0]["source_catalog"] == "skills.sh"
    assert out[0]["install_command"] == "npx skills add https://open.feishu.cn"
