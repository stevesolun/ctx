from __future__ import annotations

import gzip
import json
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

from validate_graph_artifacts import (
    GraphArtifactError,
    _safe_tar_name,
    _scan_graph_json,
    validate_graph_artifacts,
)


def _add_text(tf: tarfile.TarFile, name: str, text: str) -> None:
    payload = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    tf.addfile(info, BytesIO(payload))


def _write_catalog(graph_dir: Path, *, converted_path: str | None = None) -> None:
    skill = {
        "ctx_slug": "skills-sh-example-skill",
        "graph_node_id": "skill:skills-sh-example-skill",
        "entity_path": "entities/skills/skills-sh-example-skill.md",
        "body_available": converted_path is not None,
        "converted_path": converted_path,
    }
    catalog = {
        "observed_unique_skills": 1,
        "body_available_count": 1 if converted_path else 0,
        "skills": [skill],
    }
    with gzip.open(graph_dir / "skills-sh-catalog.json.gz", "wt", encoding="utf-8") as f:
        json.dump(catalog, f)


def _write_archive(
    graph_dir: Path,
    *,
    include_converted: bool = True,
    include_original: bool = False,
) -> None:
    graph = {
        "nodes": [
            {
                "id": "skill:skills-sh-example-skill",
                "type": "skill",
                "source_catalog": "skills.sh",
            },
            {"id": "harness:langgraph", "type": "harness"},
        ],
        "edges": [
            {
                "source": "skill:skills-sh-example-skill",
                "target": "harness:langgraph",
                "semantic_sim": 0.91,
            },
        ],
    }
    with tarfile.open(graph_dir / "wiki-graph.tar.gz", "w:gz") as tf:
        _add_text(tf, "./index.md", "# Wiki\n")
        _add_text(tf, "./graphify-out/graph.json", json.dumps(graph, separators=(",", ":")))
        _add_text(tf, "./graphify-out/communities.json", json.dumps({"total_communities": 1}))
        _add_text(tf, "./external-catalogs/skills-sh/catalog.json", "{}")
        _add_text(tf, "./entities/skills/skills-sh-example-skill.md", "# Example\n")
        _add_text(tf, "./entities/harnesses/langgraph.md", "# LangGraph\n")
        if include_converted:
            _add_text(tf, "./converted/skills-sh-example-skill/SKILL.md", "# Example\n")
            _add_text(tf, "./converted/skills-sh-example-skill/references/01-scope.md", "# Scope\n")
        if include_original:
            _add_text(tf, "./converted/skills-sh-example-skill/SKILL.md.original", "# Raw\n")


def test_validate_graph_artifacts_checks_catalog_paths_and_deep_graph_stats(
    tmp_path: Path,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    (tmp_path / "communities.json").write_text(
        json.dumps({"total_communities": 1}),
        encoding="utf-8",
    )
    _write_archive(tmp_path)

    stats = validate_graph_artifacts(
        tmp_path,
        deep=True,
        min_nodes=2,
        min_edges=1,
        min_skills_sh_nodes=1,
        min_semantic_edges=1,
        expected_harnesses={"langgraph"},
        line_threshold=180,
        max_stage_lines=40,
        expected_nodes=2,
        expected_edges=1,
        expected_semantic_edges=1,
        expected_harness_nodes=1,
        expected_skills_sh_nodes=1,
        expected_skills_sh_catalog_entries=1,
        expected_skills_sh_converted=1,
        expected_skill_pages=1,
        expected_agent_pages=0,
        expected_mcp_pages=0,
        expected_harness_pages=1,
    )

    assert stats.graph_nodes == 2
    assert stats.graph_edges == 1
    assert stats.harness_nodes == 1
    assert stats.skills_sh_catalog_entries == 1
    assert stats.skills_sh_converted == 1
    assert stats.harness_pages == 1

    with pytest.raises(GraphArtifactError, match="graph_edges exact count mismatch"):
        validate_graph_artifacts(
            tmp_path,
            deep=True,
            min_nodes=2,
            min_edges=1,
            min_skills_sh_nodes=1,
            min_semantic_edges=1,
            expected_harnesses={"langgraph"},
            expected_edges=2,
        )

    with pytest.raises(GraphArtifactError, match="deep=True is required"):
        validate_graph_artifacts(
            tmp_path,
            expected_harnesses={"langgraph"},
            expected_nodes=2,
        )


def test_validate_graph_artifacts_rejects_missing_converted_catalog_path(
    tmp_path: Path,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(tmp_path, include_converted=False)

    with pytest.raises(GraphArtifactError, match="missing converted Skills.sh body"):
        validate_graph_artifacts(tmp_path)


def test_validate_graph_artifacts_rejects_body_unavailable_catalog_records(
    tmp_path: Path,
) -> None:
    _write_catalog(tmp_path, converted_path=None)
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(tmp_path, include_converted=False)

    with pytest.raises(GraphArtifactError, match="body-unavailable records"):
        validate_graph_artifacts(tmp_path)


def test_validate_graph_artifacts_rejects_original_backup_members(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(tmp_path, include_original=True)

    with pytest.raises(GraphArtifactError, match="raw backup"):
        validate_graph_artifacts(tmp_path)


@pytest.mark.parametrize(
    "raw_name",
    [
        "../graphify-out/graph.json",
        "./../graphify-out/graph.json",
        "entities/../graphify-out/graph.json",
        "/graphify-out/graph.json",
        r"C:\tmp\graph.json",
        "entities//skills/example.md",
    ],
)
def test_safe_tar_name_rejects_unsafe_members(raw_name: str) -> None:
    with pytest.raises(GraphArtifactError, match="unsafe archive member path"):
        _safe_tar_name(raw_name)


def test_safe_tar_name_strips_only_exact_current_dir_prefix() -> None:
    assert _safe_tar_name("./graphify-out/graph.json") == "graphify-out/graph.json"


def test_scan_graph_json_handles_pretty_printed_graph() -> None:
    graph = {
        "nodes": [
            {
                "id": "skill:skills-sh-example-skill",
                "type": "skill",
                "source_catalog": "skills.sh",
            },
            {
                "id": "harness:text-to-cad",
                "type": "harness",
            },
        ],
        "edges": [
            {
                "source": "skill:skills-sh-example-skill",
                "target": "harness:text-to-cad",
                "semantic_sim": 0.0,
            },
            {
                "source": "skill:skills-sh-example-skill",
                "target": "harness:text-to-cad",
                "semantic_sim": 0.82,
            },
        ],
    }
    payload = json.dumps(graph, indent=2).encode("utf-8")

    assert _scan_graph_json(BytesIO(payload)) == (2, 2, 1, 1, 1)


def test_graph_only_workflow_uses_exact_release_counts() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    for flag in (
        "--expected-nodes 102696",
        "--expected-edges 2900834",
        "--expected-semantic-edges 1682825",
        "--expected-harness-nodes 13",
        "--expected-skills-sh-nodes 89463",
        "--expected-skills-sh-catalog-entries 89463",
        "--expected-skills-sh-converted 89463",
        "--expected-skill-pages 91432",
        "--expected-agent-pages 464",
        "--expected-mcp-pages 10787",
        "--expected-harness-pages 13",
    ):
        assert flag in workflow
