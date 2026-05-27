from __future__ import annotations

import json

import networkx as nx
import numpy as np
import pytest
from networkx.readwrite import node_link_data

from ctx.core.graph.incremental_attach import (
    calibrate_attach_defaults,
    main,
    render_calibration_markdown,
)
from ctx.core.graph.resolve_graph import load_graph
from ctx.core.graph.vector_index import build_vector_index


def test_calibrate_attach_defaults_uses_existing_graph_distributions() -> None:
    G = nx.Graph()
    G.add_node("skill:a", type="skill")
    G.add_node("skill:b", type="skill")
    G.add_node("agent:c", type="agent")
    G.add_node("mcp-server:d", type="mcp-server")
    G.add_edge("skill:a", "skill:b", semantic_sim=0.9, final_weight=0.7, weight=0.7)
    G.add_edge("skill:a", "agent:c", semantic_sim=0.8, final_weight=0.5, weight=0.5)
    G.add_edge("skill:a", "mcp-server:d", semantic_sim=0.6, final_weight=0.3, weight=0.3)
    G.add_edge("skill:b", "agent:c", semantic_sim=0.4, final_weight=0.2, weight=0.2)

    summary = calibrate_attach_defaults(G)

    assert summary.node_count == 4
    assert summary.edge_count == 4
    assert summary.semantic_score_percentiles[50] == pytest.approx(0.7)
    assert summary.final_weight_percentiles[75] == pytest.approx(0.55)
    assert summary.degree_percentiles_by_type["skill"][75] == pytest.approx(2.75)
    assert summary.recommended_min_semantic_score == pytest.approx(0.76)
    assert summary.recommended_max_edges_per_node == 3
    assert summary.recommended_min_final_weight == pytest.approx(0.03)


def test_calibrate_attach_defaults_ignores_missing_semantic_scores() -> None:
    G = nx.Graph()
    G.add_node("skill:a", type="skill")
    G.add_node("skill:b", type="skill")
    G.add_edge("skill:a", "skill:b", weight=0.4)

    summary = calibrate_attach_defaults(G)

    assert summary.semantic_score_percentiles == {}
    assert summary.final_weight_percentiles[50] == pytest.approx(0.4)
    assert summary.recommended_min_semantic_score == pytest.approx(0.80)
    assert summary.recommended_max_edges_per_node == 1


def test_calibrate_attach_defaults_ignores_zero_semantic_edges_and_caps_degree() -> None:
    G = nx.Graph()
    for index in range(25):
        G.add_node(f"skill:{index}", type="skill")
    for source in range(25):
        for target in range(source + 1, 25):
            G.add_edge(
                f"skill:{source}",
                f"skill:{target}",
                semantic_sim=0.82,
                final_weight=0.1,
            )

    summary = calibrate_attach_defaults(G)

    assert summary.semantic_score_percentiles[50] == pytest.approx(0.82)
    assert summary.recommended_max_edges_per_node == 20


def test_render_calibration_markdown_includes_recommended_defaults() -> None:
    G = nx.Graph()
    G.add_node("skill:a", type="skill")
    summary = calibrate_attach_defaults(G)

    report = render_calibration_markdown(summary)

    assert "# Incremental Attach Calibration" in report
    assert "recommended_min_semantic_score" in report
    assert "recommended_max_edges_per_node" in report


def test_main_calibrate_outputs_json(tmp_path, capsys) -> None:
    G = nx.Graph()
    G.add_node("skill:a", type="skill")
    G.add_node("skill:b", type="skill")
    G.add_edge("skill:a", "skill:b", semantic_sim=0.8, final_weight=0.4)
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        __import__("json").dumps(node_link_data(G, edges="edges")),
        encoding="utf-8",
    )

    assert main(["calibrate", "--graph", str(graph_path), "--json"]) == 0

    output = capsys.readouterr().out
    assert '"node_count": 2' in output
    assert '"recommended_min_semantic_score": 0.8' in output


def test_main_attach_dry_run_outputs_overlay_without_writing(tmp_path, capsys) -> None:
    index_dir = tmp_path / "vector-index"
    build_vector_index(
        kind="numpy-flat",
        model_id="model-a",
        node_ids=["skill:python-testing", "skill:ruby-testing"],
        content_hashes=["ha", "hb"],
        vectors=np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype="float32"),
    ).save(index_dir)
    overlay = tmp_path / "entity-overlays.jsonl"
    text_file = tmp_path / "new-python.md"
    text_file.write_text("new python testing helper", encoding="utf-8")

    rc = main([
        "attach",
        "--index-dir", str(index_dir),
        "--overlay", str(overlay),
        "--node-id", "skill:new-python",
        "--label", "new-python",
        "--type", "skill",
        "--tag", "python",
        "--text-file", str(text_file),
        "--model-id", "model-a",
        "--vector-json", "[1.0, 0.0]",
        "--top-k", "1",
        "--min-score", "0.5",
        "--dry-run",
    ])

    assert rc == 0
    assert not overlay.exists()
    output = capsys.readouterr().out
    assert '"attach_key": "ann:v1:model-a:skill:new-python:' in output
    assert '"target": "skill:python-testing"' in output


def test_main_attach_writes_idempotent_overlay_used_by_resolver(tmp_path) -> None:
    index_dir = tmp_path / "vector-index"
    build_vector_index(
        kind="numpy-flat",
        model_id="model-a",
        node_ids=["skill:python-testing", "skill:ruby-testing"],
        content_hashes=["ha", "hb"],
        vectors=np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype="float32"),
    ).save(index_dir)
    graph = nx.Graph()
    graph.add_node("skill:python-testing", type="skill", label="python-testing")
    graph.add_node("skill:ruby-testing", type="skill", label="ruby-testing")
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(__import__("json").dumps(node_link_data(graph, edges="edges")))
    overlay = tmp_path / "entity-overlays.jsonl"
    args = [
        "attach",
        "--index-dir", str(index_dir),
        "--overlay", str(overlay),
        "--node-id", "skill:new-python",
        "--label", "new-python",
        "--type", "skill",
        "--tag", "python",
        "--text", "new python testing helper",
        "--model-id", "model-a",
        "--vector-json", "[1.0, 0.0]",
        "--top-k", "1",
        "--min-score", "0.5",
    ]

    assert main(args) == 0
    assert main(args) == 0
    loaded = load_graph(graph_path)

    assert overlay.read_text(encoding="utf-8").count("\n") == 1
    assert loaded.has_edge("skill:new-python", "skill:python-testing")
    edge = loaded.edges["skill:new-python", "skill:python-testing"]
    assert edge["semantic_sim"] == pytest.approx(1.0)
    assert edge["type_affinity"] == pytest.approx(0.35)
    assert edge["score_components"] == {
        "semantic": pytest.approx(0.7),
        "type_affinity": pytest.approx(0.0105),
    }
    assert edge["final_weight"] == pytest.approx(0.7105)

    changed_args = [
        "attach",
        "--index-dir", str(index_dir),
        "--overlay", str(overlay),
        "--node-id", "skill:new-python",
        "--label", "new-python",
        "--type", "skill",
        "--tag", "python",
        "--text", "updated ruby testing helper",
        "--model-id", "model-a",
        "--vector-json", "[0.0, 1.0]",
        "--top-k", "1",
        "--min-score", "0.5",
    ]
    assert main(changed_args) == 0
    loaded_after_change = load_graph(graph_path)

    assert overlay.read_text(encoding="utf-8").count("\n") == 2
    assert loaded_after_change.has_edge("skill:new-python", "skill:ruby-testing")
    assert not loaded_after_change.has_edge("skill:new-python", "skill:python-testing")


def test_main_attach_default_min_score_matches_runtime_semantic_floor(tmp_path) -> None:
    index_dir = tmp_path / "vector-index"
    build_vector_index(
        kind="numpy-flat",
        model_id="model-a",
        node_ids=["skill:almost-python"],
        content_hashes=["ha"],
        vectors=np.asarray([[0.76, 0.65]], dtype="float32"),
    ).save(index_dir)
    overlay = tmp_path / "entity-overlays.jsonl"

    assert main([
        "attach",
        "--index-dir", str(index_dir),
        "--overlay", str(overlay),
        "--node-id", "skill:new-python",
        "--label", "new-python",
        "--type", "skill",
        "--text", "new python testing helper",
        "--model-id", "model-a",
        "--vector-json", "[1.0, 0.0]",
        "--top-k", "1",
    ]) == 0

    payload = json.loads(overlay.read_text(encoding="utf-8"))
    assert payload["edges"] == []
