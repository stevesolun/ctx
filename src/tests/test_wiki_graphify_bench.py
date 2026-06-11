"""Deterministic source-path coverage for wiki_graphify hot paths.

The old tests in this file copied implementation loops and asserted they
finished under a wall-clock threshold. These tests call the production paths
instead, so they pin behavior without making CI depend on machine speed.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx

from ctx.core.wiki import wiki_graphify as wg


def _write_skill(path: Path, slug: str, tags: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_lines = "\n".join(f"  - {tag}" for tag in tags)
    path.write_text(
        "\n".join([
            "---",
            f"title: {slug}",
            "type: skill",
            "tags:",
            tag_lines,
            "---",
            f"# {slug}",
            "Body text.",
        ])
        + "\n",
        encoding="utf-8",
    )


def test_build_graph_applies_dense_tag_threshold_from_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import ctx_config

    original_wiki = wg.WIKI_DIR
    wiki = tmp_path / "skill-wiki"
    try:
        wg.configure_wiki_dir(wiki)
        monkeypatch.setattr(wg, "QUALITY_SIDECAR_DIR", tmp_path / "quality")
        monkeypatch.setattr(wg, "load_prior_graph", lambda: None)
        monkeypatch.setattr(ctx_config.cfg, "graph_edge_weight_semantic", 0.0)
        monkeypatch.setattr(ctx_config.cfg, "graph_edge_weight_tokens", 0.0)
        monkeypatch.setattr(ctx_config.cfg, "graph_dense_tag_threshold", 2)

        _write_skill(
            wiki / "entities" / "skills" / "alpha-one.md",
            "alpha-one",
            ["dense", "kept"],
        )
        _write_skill(
            wiki / "entities" / "skills" / "beta-two.md",
            "beta-two",
            ["dense", "kept"],
        )
        _write_skill(
            wiki / "entities" / "skills" / "gamma-three.md",
            "gamma-three",
            ["dense"],
        )

        graph, _entities = wg.build_graph(incremental=False)
    finally:
        wg.configure_wiki_dir(original_wiki)

    assert graph.has_edge("skill:alpha-one", "skill:beta-two")
    assert not graph.has_edge("skill:alpha-one", "skill:gamma-three")
    assert not graph.has_edge("skill:beta-two", "skill:gamma-three")
    edge = graph["skill:alpha-one"]["skill:beta-two"]
    assert edge["shared_tags"] == ["kept"]


def test_generate_concept_pages_writes_cross_community_connections(
    tmp_path: Path,
) -> None:
    original_wiki = wg.WIKI_DIR
    wiki = tmp_path / "skill-wiki"
    graph = nx.Graph()
    alpha_members = [f"skill:alpha-{idx}" for idx in range(3)]
    beta_members = [f"agent:beta-{idx}" for idx in range(3)]
    for node_id in alpha_members:
        graph.add_node(node_id, label=node_id.rsplit(":", 1)[1], type="skill", tags=["alpha"])
    for node_id in beta_members:
        graph.add_node(node_id, label=node_id.rsplit(":", 1)[1], type="agent", tags=["beta"])
    graph.add_edge(alpha_members[0], beta_members[0], weight=1.0)

    try:
        wg.configure_wiki_dir(wiki)
        created = wg.generate_concept_pages(
            graph,
            {0: alpha_members, 1: beta_members},
            dry_run=False,
        )
    finally:
        wg.configure_wiki_dir(original_wiki)

    assert sorted(created) == ["community-alpha.md", "community-beta.md"]
    alpha_page = wiki / "concepts" / "community-alpha.md"
    assert alpha_page.is_file()
    text = alpha_page.read_text(encoding="utf-8")
    assert "## Cross-Community Connections" in text
    assert "- Beta (1 connections)" in text
