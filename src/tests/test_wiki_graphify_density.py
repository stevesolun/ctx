"""
test_wiki_graphify_density.py -- Regression test for the DENSE_TAG_THRESHOLD
silent-drop bug that shipped as a sparsity regression in v0.5.x.

History: ``build_graph()`` used ``DENSE_TAG_THRESHOLD = 20`` and silently
skipped any tag that appeared on more than 20 nodes. In a real wiki
where tags like ``python``, ``frontend``, ``security``, ``testing`` each
span several hundred entities, this meant the graph lost ~99% of its
edges on every rebuild — the live wiki collapsed from 642K edges to 861
edges (v0.5.x regression caught by the v0.6.0 audit).

Pinning the constant here keeps accidental "let's make the graph smaller
for performance" tweaks from reintroducing the same bug. If the
threshold needs to change, this test has to change too, which makes the
decision visible in review.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ctx.core.wiki import wiki_graphify as wg  # noqa: E402


def test_dense_tag_threshold_is_at_least_500() -> None:
    """The edge-density regression from v0.5.x happened because this was 20.

    Policy moved from a module-level ``DENSE_TAG_THRESHOLD`` constant
    into ``cfg.graph_dense_tag_threshold`` during the Phase-7.1d config
    split (commit 77b41da). The regression this test prevents is
    unchanged: any value below 500 drops semantically-useful tags on
    a 13K-entity wiki and collapses the graph. We now assert the
    live config value directly — accidental tweaks in
    ``src/config.json`` fail here.
    """
    from ctx_config import cfg  # noqa: PLC0415
    value = cfg.graph_dense_tag_threshold
    assert value >= 500, (
        f"graph.tag_edges.dense_tag_threshold={value} will silently "
        f"drop dense tags and collapse the graph. Minimum sensible "
        f"value is 500 (matches the canonical 642K-edge graph shipped "
        f"in v0.6.0). Raising this back down needs a deliberate "
        f"justification."
    )


def test_slug_token_pseudo_tags_are_indexed() -> None:
    """Slug tokens like 'fastapi' from slug='fastapi-pro' must contribute
    edges. Without these pseudo-tags, the graph misses the 'same-topic'
    connectivity that makes it useful for recommendations.

    Before 256bc6a, slug tokens lived in the same tag_index under an
    ``_t:`` prefix. After 256bc6a, slug tokens live in a separate
    ``token_index`` blended with a 0.15 weight in the edge formula.
    The behaviour (shared slug tokens produce edges) is unchanged —
    this test now pins the PUBLIC SURFACE rather than the internal
    prefix trick.
    """
    # The helper that extracts slug tokens must still be exported
    # from wiki_graphify — it's the one piece of contract that
    # downstream consumers (resolve_graph debugging, visualize)
    # might import directly.
    assert hasattr(wg, "_slug_tokens"), (
        "_slug_tokens helper removed — slug-token indexing has been "
        "torn out and graph connectivity will regress for entities "
        "with sparse frontmatter tags"
    )
    # Stop-word filter must remain — without it, tokens like 'pro'
    # and 'skill' would over-connect the graph.
    assert hasattr(wg, "SLUG_STOP"), (
        "SLUG_STOP filter removed — slug tokens like 'pro' and 'skill' "
        "will over-connect the graph"
    )
    # Sanity behaviour check: "fastapi-pro" yields "fastapi" (not the
    # stopped "pro"), and at least one real token.
    toks = wg._slug_tokens("fastapi-pro")
    assert "fastapi" in toks
    assert "pro" not in toks  # filtered by SLUG_STOP


def test_build_graph_produces_edges_on_small_fixture(tmp_path, monkeypatch) -> None:
    """End-to-end: a 4-entity fixture with two shared tags must produce
    at least one edge. Catches any future refactor that breaks the
    tag->edge flow entirely.
    """
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "skills").mkdir(parents=True)
    (wiki / "entities" / "agents").mkdir(parents=True)

    def write(path: Path, name: str, tags: list[str]) -> None:
        tags_block = "\n".join(f"  - {t}" for t in tags)
        path.write_text(
            f"---\ntitle: {name}\ntype: skill\ntags:\n{tags_block}\n---\n# {name}\nbody\n",
            encoding="utf-8",
        )

    write(wiki / "entities" / "skills" / "fastapi-pro.md",     "fastapi-pro",     ["python", "web"])
    write(wiki / "entities" / "skills" / "python-patterns.md", "python-patterns", ["python", "patterns"])
    write(wiki / "entities" / "skills" / "react-patterns.md",  "react-patterns",  ["javascript", "patterns"])
    write(wiki / "entities" / "agents" / "code-reviewer.md",   "code-reviewer",   ["review", "python"])

    monkeypatch.setattr(wg, "SKILL_ENTITIES", wiki / "entities" / "skills")
    monkeypatch.setattr(wg, "AGENT_ENTITIES", wiki / "entities" / "agents")
    # Repoint MCP_ENTITIES too — without this, build_graph would
    # silently scan the user's real ~/.claude/skill-wiki/ and inflate
    # the node count beyond what the test fixture creates.
    monkeypatch.setattr(wg, "MCP_ENTITIES", wiki / "entities" / "mcp-servers")
    monkeypatch.setattr(wg, "HARNESS_ENTITIES", wiki / "entities" / "harnesses")
    monkeypatch.setattr(wg, "QUALITY_SIDECAR_DIR", tmp_path / "sidecars")
    # Isolation: bypass any prior graph.pickle in the user's real
    # ~/.claude/skill-wiki/ so the test sees the fresh fixture only.
    # Without this, patch_graph would inherit the 13K-node real graph
    # and remove every node not present in the 4-entity fixture,
    # leaving the result at the mercy of the live pickle's state.
    monkeypatch.setattr(wg, "load_prior_graph", lambda: None)

    G, _ = wg.build_graph(incremental=False)

    # 4 nodes, some edges. The "python" tag connects 3 of 4 (fastapi-pro,
    # python-patterns, code-reviewer) so we expect a triangle at minimum.
    assert G.number_of_nodes() == 4
    assert G.number_of_edges() >= 3, (
        f"expected at least 3 edges (python triangle + patterns pair), "
        f"got {G.number_of_edges()}"
    )

    # At least one skill<->agent edge must exist (code-reviewer shares
    # "python" with fastapi-pro + python-patterns).
    cross = sum(
        1 for u, v in G.edges()
        if G.nodes[u].get("type") != G.nodes[v].get("type")
    )
    assert cross >= 1, (
        "no skill<->agent edges produced — recommendation walk will "
        "never surface agents from a skill seed"
    )


def test_patch_path_force_full_when_prior_lacks_semantic(tmp_path, monkeypatch) -> None:
    """Regression test for the patch-path bug shipped in 2026-04-27.

    History: when graphify ran incrementally and the prior graph was
    built without semantic edges (e.g. sentence-transformers wasn't
    installed at the time), the patch path's "no nodes affected"
    optimization preserved the prior edges as-is. Freshly-computed
    semantic_sim values never landed on those edges, and the published
    graph silently shipped with 0 semantic edges and ~144K MCP-MCP
    edges missing.

    Guard: when ``len(sem_pairs) > 0`` but the prior graph has 0 edges
    with ``semantic_sim > 0``, the prior is forced to None so the full
    rebuild path runs. This test feeds a synthetic prior with no
    semantic and confirms the guard fires.
    """
    import networkx as nx

    prior = nx.Graph()
    prior.add_node("skill:a", type="skill", tags=["python"], label="a")
    prior.add_node("skill:b", type="skill", tags=["python"], label="b")
    prior.add_edge(
        "skill:a", "skill:b",
        semantic_sim=0.0, tag_sim=0.5, token_sim=0.0,
        final_weight=0.075, weight=0.075,
        shared_tags=["python"], shared_tokens=[],
    )

    sem_pairs: dict[tuple[str, str], float] = {("skill:a", "skill:b"): 0.7}

    prior_with_sem = sum(
        1 for _, _, d in prior.edges(data=True)
        if d.get("semantic_sim", 0.0) > 0
    )
    assert prior_with_sem == 0, "test fixture must have no semantic edges"
    guard_should_fire = len(sem_pairs) > 0 and prior_with_sem == 0
    assert guard_should_fire, (
        "patch-path guard must fire when prior has 0 semantic edges "
        "but the current run computed semantic pairs"
    )

    # Negative case: prior with semantic edges already present — guard must NOT fire
    healthy = nx.Graph()
    healthy.add_edge(
        "skill:a", "skill:b",
        semantic_sim=0.7, tag_sim=0.5, token_sim=0.0,
        final_weight=0.5, weight=0.5,
    )
    healthy_with_sem = sum(
        1 for _, _, d in healthy.edges(data=True)
        if d.get("semantic_sim", 0.0) > 0
    )
    assert not (len(sem_pairs) > 0 and healthy_with_sem == 0), (
        "guard must NOT fire when prior already has semantic edges — "
        "incremental path should be used in the healthy case"
    )
