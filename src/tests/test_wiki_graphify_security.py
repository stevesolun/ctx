"""
test_wiki_graphify_security.py -- Security regression tests for wiki_graphify.

Covers the CRITICAL security-auditor finding (C-1): pickle.loads on
graphify-out/graph.pickle is an RCE primitive. Any process that can write
to graphify-out/ gets code execution under the user's UID on the next
regraphify.

The fix removes pickle entirely — load_prior_graph reads the existing
graph.json artifact via nx.node_link_graph. JSON loading has no code-
execution path. These tests pin that behavior so a future revert to
pickle fails loudly.
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import networkx as nx
import pytest


# ────────────────────────────────────────────────────────────────────
# Shared fixtures — a graphify-out dir with a known good graph.json
# ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def graphify_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point wiki_graphify.GRAPH_OUT at a temp dir.

    Every test that touches the load/export paths needs the module-level
    constant overridden so tests don't clobber the real wiki.
    """
    from ctx.core.wiki import wiki_graphify

    out = tmp_path / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wiki_graphify, "GRAPH_OUT", out)
    return out


def _make_sample_graph() -> nx.Graph:
    """A small networkx graph with the same shape wiki_graphify produces."""
    G = nx.Graph()
    G.graph["semantic_build_floor"] = 0.5
    G.graph["semantic_min_cosine_default"] = 0.8
    G.add_node("skill:a", label="a", type="skill", tags=["python"])
    G.add_node("skill:b", label="b", type="skill", tags=["python"])
    G.add_node("mcp-server:c", label="c", type="mcp-server", tags=["official"])
    G.add_edge(
        "skill:a",
        "skill:b",
        semantic_sim=0.9,
        tag_sim=0.4,
        token_sim=0.0,
        final_weight=0.72,
        weight=0.72,
        shared_tags=["python"],
        shared_tokens=[],
    )
    G.add_edge(
        "skill:a",
        "mcp-server:c",
        semantic_sim=0.6,
        tag_sim=0.0,
        token_sim=0.0,
        final_weight=0.42,
        weight=0.42,
        shared_tags=[],
        shared_tokens=[],
    )
    return G


def _write_graph_json(out: Path, G: nx.Graph) -> Path:
    """Serialise *G* the same way export_graph does."""
    data = nx.node_link_data(G, edges="edges")
    path = out / "graph.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


# ────────────────────────────────────────────────────────────────────
# CRITICAL-C1: the pickle RCE primitive must be gone
# ────────────────────────────────────────────────────────────────────


class _PickleRCESentinel:
    """A pickle payload that would run arbitrary code during loads()."""

    def __reduce__(self):
        # On unpickle, this touches a sentinel file. A safe load MUST
        # NOT invoke __reduce__ — which means the pickle path must not
        # be read at all.
        marker = Path(_PickleRCESentinel._marker_path())  # type: ignore[attr-defined]
        return (_touch_file, (str(marker),))

    @classmethod
    def _marker_path(cls) -> str:
        return cls._marker  # type: ignore[attr-defined]


def _touch_file(path: str) -> bool:
    Path(path).write_text("pwned", encoding="utf-8")
    return True


def test_load_prior_graph_does_not_execute_pickle_payload(
    graphify_out: Path,
    tmp_path: Path,
) -> None:
    """Writing a malicious pickle next to graph.json must NOT execute it.

    This is the pinned regression for security-auditor finding C-1.
    If load_prior_graph ever starts reading graph.pickle again, this
    sentinel file will be created and the test fails.
    """
    from ctx.core.wiki import wiki_graphify

    marker = tmp_path / "rce-executed.sentinel"
    _PickleRCESentinel._marker = str(marker)  # type: ignore[attr-defined]

    # Write the RCE payload where the old code used to look.
    payload = pickle.dumps(_PickleRCESentinel(), protocol=pickle.HIGHEST_PROTOCOL)
    (graphify_out / "graph.pickle").write_bytes(payload)

    # Legitimate graph.json alongside — load_prior_graph must prefer it
    # (or return None if the JSON is missing; either way, no RCE).
    _write_graph_json(graphify_out, _make_sample_graph())

    result = wiki_graphify.load_prior_graph()

    assert not marker.is_file(), (
        "PICKLE RCE REGRESSED: load_prior_graph executed a pickle __reduce__"
    )
    # Valid JSON alongside → result should be the JSON-derived graph.
    assert result is not None
    assert result.number_of_nodes() == 3


def test_load_prior_graph_ignores_pickle_when_only_pickle_exists(
    graphify_out: Path,
    tmp_path: Path,
) -> None:
    """Even without a graph.json, a stray pickle must not be loaded.

    Before the fix, load_prior_graph fell back to graph.pickle directly.
    After the fix, graph.pickle is never read — the absence of graph.json
    yields None (triggers a full rebuild) rather than executing pickle.
    """
    from ctx.core.wiki import wiki_graphify

    marker = tmp_path / "rce-pickle-only.sentinel"
    _PickleRCESentinel._marker = str(marker)  # type: ignore[attr-defined]

    (graphify_out / "graph.pickle").write_bytes(
        pickle.dumps(_PickleRCESentinel(), protocol=pickle.HIGHEST_PROTOCOL)
    )

    result = wiki_graphify.load_prior_graph()

    assert not marker.is_file(), "pickle was loaded despite being the only artifact"
    assert result is None


# ────────────────────────────────────────────────────────────────────
# Happy-path: JSON round-trip preserves nodes, edges, attrs, graph-meta
# ────────────────────────────────────────────────────────────────────


def test_load_prior_graph_roundtrip_preserves_nodes_and_edges(
    graphify_out: Path,
) -> None:
    """A graph exported to JSON and loaded back must be structurally equal."""
    from ctx.core.wiki import wiki_graphify

    original = _make_sample_graph()
    _write_graph_json(graphify_out, original)

    loaded = wiki_graphify.load_prior_graph()
    assert loaded is not None
    assert loaded.number_of_nodes() == original.number_of_nodes()
    assert loaded.number_of_edges() == original.number_of_edges()
    assert set(loaded.nodes()) == set(original.nodes())


def test_load_prior_graph_roundtrip_preserves_edge_attrs(
    graphify_out: Path,
) -> None:
    """Edge attributes (semantic_sim, tag_sim, etc.) survive JSON round-trip."""
    from ctx.core.wiki import wiki_graphify

    original = _make_sample_graph()
    _write_graph_json(graphify_out, original)

    loaded = wiki_graphify.load_prior_graph()
    assert loaded is not None

    attrs = loaded["skill:a"]["skill:b"]
    assert attrs["semantic_sim"] == pytest.approx(0.9)
    assert attrs["tag_sim"] == pytest.approx(0.4)
    assert attrs["token_sim"] == pytest.approx(0.0)
    assert attrs["final_weight"] == pytest.approx(0.72)
    assert attrs["shared_tags"] == ["python"]


def test_load_prior_graph_roundtrip_preserves_graph_level_metadata(
    graphify_out: Path,
) -> None:
    """Graph-level attrs (build_floor, min_cosine_default) survive the round-trip.

    The semantic_build_floor is used by filter_graph_by_min_cosine to refuse
    below-floor requests; if it got dropped during serialisation the filter
    would silently accept invalid thresholds.
    """
    from ctx.core.wiki import wiki_graphify

    original = _make_sample_graph()
    _write_graph_json(graphify_out, original)

    loaded = wiki_graphify.load_prior_graph()
    assert loaded is not None
    assert loaded.graph.get("semantic_build_floor") == pytest.approx(0.5)
    assert loaded.graph.get("semantic_min_cosine_default") == pytest.approx(0.8)


def test_load_prior_graph_supports_networkx_without_edges_kwarg(
    graphify_out: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.core.wiki import wiki_graphify

    original = _make_sample_graph()
    _write_graph_json(graphify_out, original)
    real_node_link_graph = wiki_graphify.nx.node_link_graph

    def old_node_link_graph(data: dict, *args, **kwargs) -> nx.Graph:
        if "edges" in kwargs:
            raise TypeError("node_link_graph() got an unexpected keyword argument 'edges'")
        assert "links" in data
        assert "edges" not in data
        return real_node_link_graph(data, *args, edges="links", **kwargs)

    monkeypatch.setattr(wiki_graphify.nx, "node_link_graph", old_node_link_graph)

    loaded = wiki_graphify.load_prior_graph()

    assert loaded is not None
    assert loaded.number_of_nodes() == original.number_of_nodes()
    assert loaded.number_of_edges() == original.number_of_edges()


# ────────────────────────────────────────────────────────────────────
# Robustness: corrupt / malformed / missing JSON must not crash
# ────────────────────────────────────────────────────────────────────


def test_load_prior_graph_returns_none_on_missing_json(graphify_out: Path) -> None:
    from ctx.core.wiki import wiki_graphify

    # graphify_out is empty — no graph.json
    assert wiki_graphify.load_prior_graph() is None


def test_load_prior_graph_uses_active_packs_when_graph_json_is_absent(
    graphify_out: Path,
) -> None:
    from ctx.core.graph.graph_packs import write_base_pack
    from ctx.core.wiki import wiki_graphify

    graph = _make_sample_graph()
    write_base_pack(
        pack_dir=graphify_out / "packs" / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-1",
        model_id="model-1",
        graph=graph,
    )

    loaded = wiki_graphify.load_prior_graph()

    assert loaded is not None
    assert loaded.number_of_nodes() == graph.number_of_nodes()
    assert loaded.number_of_edges() == graph.number_of_edges()
    assert loaded.graph["ctx_pack_base_export_id"] == "export-1"


def test_load_prior_graph_returns_none_on_malformed_json(graphify_out: Path) -> None:
    from ctx.core.wiki import wiki_graphify

    (graphify_out / "graph.json").write_text("not { valid json", encoding="utf-8")
    assert wiki_graphify.load_prior_graph() is None


def test_load_prior_graph_returns_none_on_wrong_schema(graphify_out: Path) -> None:
    """Valid JSON that doesn't match the node-link schema returns None.

    Pre-fix, an attacker could craft a JSON file that passed json.loads
    but confused a downstream consumer. Post-fix, the shape check rejects
    anything that isn't a networkx node-link document.
    """
    from ctx.core.wiki import wiki_graphify

    (graphify_out / "graph.json").write_text(
        json.dumps({"not_a_graph": "nope"}),
        encoding="utf-8",
    )
    assert wiki_graphify.load_prior_graph() is None


# ────────────────────────────────────────────────────────────────────
# The export path no longer writes graph.pickle
# ────────────────────────────────────────────────────────────────────


def test_export_graph_does_not_write_pickle(graphify_out: Path) -> None:
    """export_graph must remove stale graph.pickle files."""
    from ctx.core.wiki import wiki_graphify

    (graphify_out / "graph.pickle").write_bytes(b"stale")
    G = _make_sample_graph()
    wiki_graphify.export_graph(G, communities={})

    assert (graphify_out / "graph.json").is_file()
    assert not (graphify_out / "graph.pickle").exists(), (
        "export_graph left graph.pickle behind, preserving the old RCE artifact"
    )


def test_export_graph_writes_active_base_pack(graphify_out: Path) -> None:
    """export_graph writes a modular base pack beside graph.json."""
    from ctx.core.graph.graph_packs import discover_pack_manifests, load_merged_pack_graph
    from ctx.core.wiki import wiki_graphify

    first = _make_sample_graph()
    first.graph[wiki_graphify.GRAPH_SCORING_SIGNATURE_KEY] = {
        "intake_backend": "local",
        "intake_model": "test-model",
    }
    wiki_graphify.export_graph(first, communities={})

    packs_dir = graphify_out / "packs"
    entries = discover_pack_manifests(packs_dir)
    graph_json = json.loads((graphify_out / "graph.json").read_text(encoding="utf-8"))
    export_id = graph_json["graph"]["export_id"]
    assert [entry.manifest.pack_id for entry in entries] == [f"base-{export_id}"]
    assert entries[0].manifest.base_export_id == export_id
    assert entries[0].manifest.model_id == "local:test-model"
    assert load_merged_pack_graph(packs_dir).number_of_nodes() == first.number_of_nodes()

    second = _make_sample_graph()
    second.add_node("skill:new", label="new", type="skill", tags=["python"])
    wiki_graphify.export_graph(second, communities={})

    entries = discover_pack_manifests(packs_dir)
    graph_json = json.loads((graphify_out / "graph.json").read_text(encoding="utf-8"))
    export_id = graph_json["graph"]["export_id"]
    assert [entry.manifest.pack_id for entry in entries] == [f"base-{export_id}"]
    assert load_merged_pack_graph(packs_dir).number_of_nodes() == second.number_of_nodes()


def test_export_graph_supports_networkx_without_edges_kwarg(
    graphify_out: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.core.wiki import wiki_graphify

    real_node_link_data = wiki_graphify.nx.node_link_data

    def old_node_link_data(graph: nx.Graph, *args, **kwargs) -> dict:
        if kwargs.get("edges") == "edges":
            raise TypeError("node_link_data() got an unexpected keyword argument 'edges'")
        return real_node_link_data(graph, *args, edges="links", **kwargs)

    monkeypatch.setattr(wiki_graphify.nx, "node_link_data", old_node_link_data)

    wiki_graphify.export_graph(_make_sample_graph(), communities={})

    data = json.loads((graphify_out / "graph.json").read_text(encoding="utf-8"))
    assert "edges" in data
    assert "links" not in data
    assert len(data["edges"]) == 2


def test_export_graph_uses_atomic_writer_for_artifacts(
    graphify_out: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graphify artifacts are staged, validated, promoted, and metadata-backed."""
    from ctx.core.wiki import wiki_graphify

    calls: list[str] = []

    def fake_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
        calls.append(path.name)
        path.write_text(text, encoding=encoding)

    monkeypatch.setattr(
        wiki_graphify,
        "safe_atomic_write_text",
        fake_atomic,
        raising=False,
    )

    wiki_graphify.export_graph(_make_sample_graph(), communities={})

    assert {name for name in calls if name.endswith(".staged")} == {
        "graph.json.staged",
        "graph-delta.json.staged",
        "communities.json.staged",
        "graph-report.md.staged",
        "graph-export-manifest.json.staged",
    }
    assert calls[-1] == "graph-export-manifest.json.staged"
    for name in (
        "graph.json",
        "graph-delta.json",
        "communities.json",
        "graph-report.md",
        "graph-export-manifest.json",
    ):
        metadata = json.loads((graphify_out / f"{name}.promotion.json").read_text(encoding="utf-8"))
        assert metadata["status"] == "promoted"
        assert metadata["target"].endswith(name)
        assert "last_good" in metadata


def test_load_prior_graph_rejects_post_graph_replace_crash(
    graphify_out: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new graph.json without a matching manifest is an incomplete export."""
    from ctx.core.wiki import wiki_graphify

    old_graph = _make_sample_graph()
    wiki_graphify.export_graph(old_graph, communities={})
    old_manifest = json.loads(
        (graphify_out / "graph-export-manifest.json").read_text(encoding="utf-8")
    )

    new_graph = _make_sample_graph()
    new_graph.add_node("skill:new", label="new", type="skill", tags=["python"])

    real_promote = wiki_graphify.promote_staged_artifact

    def crash_after_graph_promotion(*args, **kwargs):
        result = real_promote(*args, **kwargs)
        if Path(args[1]).name == "graph.json":
            raise RuntimeError("simulated crash after graph replacement")
        return result

    monkeypatch.setattr(
        wiki_graphify,
        "promote_staged_artifact",
        crash_after_graph_promotion,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        wiki_graphify.export_graph(new_graph, communities={})

    assert (
        json.loads((graphify_out / "graph-export-manifest.json").read_text(encoding="utf-8"))
        == old_manifest
    )
    assert wiki_graphify.load_prior_graph() is None


def test_inject_community_links_refreshes_generated_block(
    tmp_path: Path,
) -> None:
    """Regraphify must replace stale graph-generated links, not append forever."""
    from ctx.core.wiki import wiki_graphify

    wiki_graphify.configure_wiki_dir(tmp_path / "wiki")
    page = tmp_path / "wiki" / "entities" / "skills" / "a.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "\n".join(
            [
                "# a",
                "",
                "## Related Skills",
                "<!-- ctx-graph-related:start -->",
                "- [[entities/skills/old]]",
                "<!-- ctx-graph-related:end -->",
                "- [[entities/skills/manual]]",
                "",
            ]
        ),
        encoding="utf-8",
    )

    G = nx.Graph()
    G.add_node("skill:a", label="a", type="skill", tags=["python"])
    G.add_node("skill:b", label="b", type="skill", tags=["python"])
    G.add_edge("skill:a", "skill:b", weight=1.0)

    updated = wiki_graphify.inject_community_links(G, {0: ["skill:a", "skill:b"]})

    content = page.read_text(encoding="utf-8")
    assert updated == 1
    assert "[[entities/skills/b]]" in content
    assert "[[entities/skills/manual]]" in content
    assert "[[entities/skills/old]]" not in content


def test_main_dry_run_does_not_write_graph_or_concepts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.core.wiki import wiki_graphify

    G = _make_sample_graph()
    G.add_node("skill:d", label="d", type="skill", tags=["python"])
    G.add_edge("skill:b", "skill:d", weight=0.5)
    original_wiki = wiki_graphify.WIKI_DIR

    def fail_export(*_args: object, **_kwargs: object) -> None:
        pytest.fail("dry-run must not export graph artifacts")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ctx-wiki-graphify",
            "--wiki-dir",
            str(tmp_path / "wiki"),
            "--dry-run",
        ],
    )
    monkeypatch.setattr(wiki_graphify, "build_graph", lambda **_kwargs: (G, {}))
    monkeypatch.setattr(
        wiki_graphify,
        "detect_communities",
        lambda _graph: {
            0: ["skill:a", "skill:b", "skill:d"],
        },
    )
    monkeypatch.setattr(wiki_graphify, "export_graph", fail_export)

    try:
        wiki_graphify.main()
    finally:
        wiki_graphify.configure_wiki_dir(original_wiki)

    assert not (tmp_path / "wiki" / "graphify-out").exists()
    assert not (tmp_path / "wiki" / "concepts").exists()


def test_main_writes_wiki_base_pack_after_page_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.core.wiki import wiki_graphify
    from ctx.core.wiki.wiki_packs import discover_wiki_pack_manifests, load_merged_wiki_pages

    wiki = tmp_path / "wiki"
    skill_pages = wiki / "entities" / "skills"
    skill_pages.mkdir(parents=True)
    (skill_pages / "a.md").write_text("# a\n", encoding="utf-8")
    (skill_pages / "b.md").write_text("# b\n", encoding="utf-8")
    original_wiki = wiki_graphify.WIKI_DIR
    graph = _make_sample_graph()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ctx-wiki-graphify",
            "--wiki-dir",
            str(wiki),
        ],
    )
    monkeypatch.setattr(wiki_graphify, "build_graph", lambda **_kwargs: (graph, {}))
    monkeypatch.setattr(
        wiki_graphify,
        "detect_communities",
        lambda _graph: {
            0: ["skill:a", "skill:b", "mcp-server:c"],
        },
    )

    try:
        wiki_graphify.main()
        graph_manifest = json.loads(
            (wiki / "graphify-out" / "graph-export-manifest.json").read_text(encoding="utf-8")
        )
        entries = discover_wiki_pack_manifests(wiki / "wiki-packs")
        assert [entry.manifest.pack_id for entry in entries] == [
            f"base-{graph_manifest['export_id']}"
        ]
        assert entries[0].manifest.base_export_id == graph_manifest["export_id"]

        pages = load_merged_wiki_pages(wiki / "wiki-packs")
        assert "[[entities/skills/b]]" in pages["entities/skills/a.md"]
        assert any(path.startswith("concepts/community-") for path in pages)

        (skill_pages / "a.md").write_text("# a v2\n", encoding="utf-8")
        wiki_graphify.main()
        pages = load_merged_wiki_pages(wiki / "wiki-packs")
    finally:
        wiki_graphify.configure_wiki_dir(original_wiki)

    entries = discover_wiki_pack_manifests(wiki / "wiki-packs")
    assert len(entries) == 1
    assert "# a v2" in pages["entities/skills/a.md"]
    assert (wiki / "wiki-packs.rollback").is_dir()


def test_generate_concept_pages_reconciles_generated_pages(
    tmp_path: Path,
) -> None:
    from ctx.core.wiki import wiki_graphify

    original_wiki = wiki_graphify.WIKI_DIR
    try:
        wiki_graphify.configure_wiki_dir(tmp_path / "wiki")
        concepts = tmp_path / "wiki" / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "community-old.md").write_text(
            "*Generated by wiki_graphify.py via community detection.*\n",
            encoding="utf-8",
        )
        (concepts / "community-manual.md").write_text(
            "# manually maintained\n",
            encoding="utf-8",
        )

        G = nx.Graph()
        G.add_node("skill:a", label="a", type="skill", tags=["python"])
        G.add_node("skill:b", label="b", type="skill", tags=["python"])
        G.add_node("skill:c", label="c", type="skill", tags=["testing"])
        G.add_edges_from([("skill:a", "skill:b"), ("skill:b", "skill:c")])

        created = wiki_graphify.generate_concept_pages(
            G,
            {0: ["skill:a", "skill:b", "skill:c"]},
        )
    finally:
        wiki_graphify.configure_wiki_dir(original_wiki)

    assert created == ["community-python-testing.md"]
    new_page = concepts / "community-python-testing.md"
    assert new_page.is_file()
    content = new_page.read_text(encoding="utf-8")
    assert wiki_graphify.CONCEPT_GENERATED_MARKER in content
    assert "tags: [python, testing]" in content
    assert not (concepts / "community-old.md").exists()
    assert (concepts / "community-manual.md").is_file()
