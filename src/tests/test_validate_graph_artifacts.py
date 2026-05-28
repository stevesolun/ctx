from __future__ import annotations

import gzip
import json
import sqlite3
import tarfile
import zlib
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from validate_graph_artifacts import (
    DEFAULT_HARNESSES,
    GraphArtifactError,
    _validate_root_entity_overlay,
    _safe_tar_name,
    _scan_graph_json,
    validate_graph_artifacts,
)

_PREVIEW_HTML_FILES = (
    "sample-top60.html",
    "viz-ai-agents.html",
    "viz-overview.html",
    "viz-python.html",
    "viz-security.html",
)


def _add_text(tf: tarfile.TarFile, name: str, text: str) -> None:
    payload = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    tf.addfile(info, BytesIO(payload))


def _add_bytes(tf: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    tf.addfile(info, BytesIO(payload))


def _dashboard_index_bytes(graph_dir: Path, *, export_id: str) -> bytes:
    path = graph_dir / f"dashboard-index-{export_id}.sqlite3"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [
                ("version", "1"),
                ("export_id", json.dumps(export_id)),
                ("nodes_count", "2"),
                ("edges_count", "1"),
                ("max_degree", "1"),
                ("top_k", "40"),
            ],
        )
        conn.executemany(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            [
                ("skill:skills-sh-example-skill", "skills-sh-example-skill", "skill", "[]", "", None, None, 1),
                ("harness:langgraph", "langgraph", "harness", "[]", "", None, None, 1),
            ],
        )
        conn.executemany(
            "INSERT INTO slug_index VALUES(?,?,?)",
            [
                ("skills-sh-example-skill", "skill", "skill:skills-sh-example-skill"),
                ("langgraph", "harness", "harness:langgraph"),
            ],
        )
        conn.executemany(
            "INSERT INTO neighbors VALUES(?,?)",
            [
                ("skill:skills-sh-example-skill", zlib.compress(json.dumps([{"target": "harness:langgraph"}]).encode("utf-8"))),
                ("harness:langgraph", zlib.compress(json.dumps([{"target": "skill:skills-sh-example-skill"}]).encode("utf-8"))),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    payload = path.read_bytes()
    path.unlink()
    return payload


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


def _write_entity_overlay(graph_dir: Path) -> None:
    payload = {
        "overlay_id": "test-overlay",
        "nodes": [
            {
                "id": "harness:langgraph",
                "label": "langgraph",
                "type": "harness",
            }
        ],
        "edges": [
            {
                "source": "skill:skills-sh-example-skill",
                "target": "harness:langgraph",
                "weight": 0.5,
                "final_weight": 0.5,
                "similarity_score": 0.5,
            }
        ],
    }
    (graph_dir / "entity-overlays.jsonl").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )


def _write_runtime_archive(
    graph_dir: Path,
    *,
    graph: dict[str, Any],
    include_queue: bool = False,
    include_delta: bool = True,
    include_report: bool = True,
    include_manifest: bool = True,
    delta_export_id: str = "export-test",
    communities_export_id: str = "export-test",
    report_export_id: str = "export-test",
    manifest_export_id: str = "export-test",
) -> None:
    with tarfile.open(graph_dir / "wiki-graph-runtime.tar.gz", "w:gz") as tf:
        _add_text(tf, "index.md", "# Wiki\n")
        _add_text(tf, "graphify-out/graph.json", json.dumps(graph, separators=(",", ":")))
        if include_delta:
            _add_text(
                tf,
                "graphify-out/graph-delta.json",
                json.dumps({"export_id": delta_export_id, "nodes": [], "edges": []}),
            )
        _add_text(
            tf,
            "graphify-out/communities.json",
            json.dumps({"export_id": communities_export_id, "total_communities": 1}),
        )
        if include_report:
            _add_text(
                tf,
                "graphify-out/graph-report.md",
                f"# Graph Report\n\n> Export ID: {report_export_id}\n",
            )
        if include_manifest:
            _add_text(
                tf,
                "graphify-out/graph-export-manifest.json",
                json.dumps({
                    "version": 1,
                    "export_id": manifest_export_id,
                    "artifacts": {
                        "graph": "graph.json",
                        "delta": "graph-delta.json",
                        "communities": "communities.json",
                        "report": "graph-report.md",
                    },
                    "counts": {"nodes": 2, "edges": 1, "communities": 1},
                }),
            )
        _add_bytes(
            tf,
            "graphify-out/dashboard-neighborhoods.sqlite3",
            _dashboard_index_bytes(graph_dir, export_id=manifest_export_id),
        )
        _add_text(tf, "external-catalogs/skills-sh/catalog.json", "{}")
        if include_queue:
            _add_text(tf, ".ctx/wiki-queue.sqlite3", "not a shipped artifact\n")
        for slug in sorted(DEFAULT_HARNESSES):
            _add_text(tf, f"entities/harnesses/{slug}.md", f"# {slug}\n")


def _write_archive(
    graph_dir: Path,
    *,
    include_converted: bool = True,
    converted_skill_text: str = "# Example\n",
    include_original: bool = False,
    include_lock: bool = False,
    include_queue: bool = False,
    include_delta: bool = True,
    include_report: bool = True,
    include_manifest: bool = True,
    graph_export_id: str = "export-test",
    delta_export_id: str = "export-test",
    communities_export_id: str | None = None,
    report_export_id: str | None = None,
    manifest_export_id: str | None = None,
) -> None:
    _write_entity_overlay(graph_dir)
    communities_export_id = communities_export_id or graph_export_id
    report_export_id = report_export_id or graph_export_id
    manifest_export_id = manifest_export_id or graph_export_id
    graph = {
        "graph": {"export_id": graph_export_id},
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
        if include_delta:
            _add_text(
                tf,
                "./graphify-out/graph-delta.json",
                json.dumps({"export_id": delta_export_id, "nodes": [], "edges": []}),
            )
        _add_text(
            tf,
            "./graphify-out/communities.json",
            json.dumps({"export_id": communities_export_id, "total_communities": 1}),
        )
        if include_report:
            _add_text(
                tf,
                "./graphify-out/graph-report.md",
                f"# Graph Report\n\n> Export ID: {report_export_id}\n",
            )
        if include_manifest:
            _add_text(
                tf,
                "./graphify-out/graph-export-manifest.json",
                json.dumps({
                    "version": 1,
                    "export_id": manifest_export_id,
                    "artifacts": {
                        "graph": "graph.json",
                        "delta": "graph-delta.json",
                        "communities": "communities.json",
                        "report": "graph-report.md",
                    },
                    "counts": {"nodes": 2, "edges": 1, "communities": 1},
                }),
            )
        _add_bytes(
            tf,
            "./graphify-out/dashboard-neighborhoods.sqlite3",
            _dashboard_index_bytes(graph_dir, export_id=manifest_export_id),
        )
        _add_text(tf, "./external-catalogs/skills-sh/catalog.json", "{}")
        _add_text(tf, "./entities/skills/skills-sh-example-skill.md", "# Example\n")
        _add_text(tf, "./entities/harnesses/langgraph.md", "# LangGraph\n")
        if include_converted:
            _add_text(tf, "./converted/skills-sh-example-skill/SKILL.md", converted_skill_text)
            _add_text(tf, "./converted/skills-sh-example-skill/references/01-scope.md", "# Scope\n")
        if include_original:
            _add_text(tf, "./converted/skills-sh-example-skill/SKILL.md.original", "# Raw\n")
        if include_lock:
            _add_text(tf, "./index.md.lock", "")
        if include_queue:
            _add_text(tf, "./.ctx/wiki-queue.sqlite3", "not a shipped artifact\n")
    for preview in _PREVIEW_HTML_FILES:
        (graph_dir / preview).write_text(
            "\n".join([
                "<!DOCTYPE html>",
                "<html><head>",
                f'<meta name="ctx-graph-export-id" content="{manifest_export_id}">',
                "</head><body>",
                "const CTX_GRAPH_METADATA = "
                + json.dumps({
                    "export_id": manifest_export_id,
                    "source_graph_nodes": 2,
                    "source_graph_edges": 1,
                }),
                "</body></html>",
            ]),
            encoding="utf-8",
        )
    _write_runtime_archive(
        graph_dir,
        graph=graph,
        include_delta=include_delta,
        include_report=include_report,
        include_manifest=include_manifest,
        delta_export_id=delta_export_id,
        communities_export_id=communities_export_id,
        report_export_id=report_export_id,
        manifest_export_id=manifest_export_id,
    )
    (graph_dir / "communities.json").write_text(
        json.dumps({"export_id": communities_export_id, "total_communities": 1}),
        encoding="utf-8",
    )


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


def test_validate_graph_artifacts_rejects_mixed_export_generation(
    tmp_path: Path,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(tmp_path, graph_export_id="new-export", delta_export_id="old-export")

    with pytest.raises(GraphArtifactError, match="export_id mismatch"):
        validate_graph_artifacts(
            tmp_path,
            deep=True,
            min_nodes=2,
            min_edges=1,
            min_skills_sh_nodes=1,
            min_semantic_edges=1,
            expected_harnesses={"langgraph"},
        )


def test_validate_graph_artifacts_rejects_runtime_full_export_split(
    tmp_path: Path,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    _write_archive(tmp_path, graph_export_id="full-export")
    runtime_graph = {
        "graph": {"export_id": "runtime-export"},
        "nodes": [
            {"id": "skill:skills-sh-example-skill", "type": "skill"},
            {"id": "harness:langgraph", "type": "harness"},
        ],
        "edges": [
            {"source": "skill:skills-sh-example-skill", "target": "harness:langgraph"},
        ],
    }
    _write_runtime_archive(
        tmp_path,
        graph=runtime_graph,
        delta_export_id="runtime-export",
        communities_export_id="runtime-export",
        report_export_id="runtime-export",
        manifest_export_id="runtime-export",
    )

    with pytest.raises(GraphArtifactError, match="runtime graph archive export_id mismatch"):
        validate_graph_artifacts(
            tmp_path,
            deep=True,
            min_nodes=2,
            min_edges=1,
            min_skills_sh_nodes=1,
            min_semantic_edges=1,
            expected_harnesses={"langgraph"},
        )


def test_validate_graph_artifacts_rejects_corrupt_dashboard_index(
    tmp_path: Path,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    _write_archive(tmp_path)
    with tarfile.open(tmp_path / "wiki-graph-runtime.tar.gz", "w:gz") as tf:
        graph = {
            "graph": {"export_id": "export-test"},
            "nodes": [
                {"id": "skill:skills-sh-example-skill", "type": "skill"},
                {"id": "harness:langgraph", "type": "harness"},
            ],
            "edges": [
                {"source": "skill:skills-sh-example-skill", "target": "harness:langgraph"},
            ],
        }
        _add_text(tf, "index.md", "# Wiki\n")
        _add_text(tf, "graphify-out/graph.json", json.dumps(graph, separators=(",", ":")))
        _add_text(tf, "graphify-out/graph-delta.json", json.dumps({"export_id": "export-test"}))
        _add_text(tf, "graphify-out/communities.json", json.dumps({"export_id": "export-test"}))
        _add_text(tf, "graphify-out/graph-report.md", "# Graph Report\n\n> Export ID: export-test\n")
        _add_text(
            tf,
            "graphify-out/graph-export-manifest.json",
            json.dumps({
                "version": 1,
                "export_id": "export-test",
                "artifacts": {
                    "graph": "graph.json",
                    "delta": "graph-delta.json",
                    "communities": "communities.json",
                    "report": "graph-report.md",
                },
                "counts": {"nodes": 2, "edges": 1, "communities": 1},
            }),
        )
        _add_text(tf, "graphify-out/dashboard-neighborhoods.sqlite3", "not sqlite\n")
        _add_text(tf, "external-catalogs/skills-sh/catalog.json", "{}")
        for slug in sorted(DEFAULT_HARNESSES):
            _add_text(tf, f"entities/harnesses/{slug}.md", f"# {slug}\n")

    with pytest.raises(GraphArtifactError, match="dashboard index"):
        validate_graph_artifacts(
            tmp_path,
            deep=True,
            min_nodes=2,
            min_edges=1,
            min_skills_sh_nodes=1,
            min_semantic_edges=1,
            expected_harnesses={"langgraph"},
        )


def test_validate_graph_artifacts_rejects_stale_dashboard_index(
    tmp_path: Path,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    _write_archive(tmp_path, graph_export_id="new-export", manifest_export_id="new-export")
    with tarfile.open(tmp_path / "wiki-graph-runtime.tar.gz", "w:gz") as tf:
        graph = {
            "graph": {"export_id": "new-export"},
            "nodes": [
                {"id": "skill:skills-sh-example-skill", "type": "skill"},
                {"id": "harness:langgraph", "type": "harness"},
            ],
            "edges": [
                {"source": "skill:skills-sh-example-skill", "target": "harness:langgraph"},
            ],
        }
        _add_text(tf, "index.md", "# Wiki\n")
        _add_text(tf, "graphify-out/graph.json", json.dumps(graph, separators=(",", ":")))
        _add_text(tf, "graphify-out/graph-delta.json", json.dumps({"export_id": "new-export"}))
        _add_text(tf, "graphify-out/communities.json", json.dumps({"export_id": "new-export"}))
        _add_text(tf, "graphify-out/graph-report.md", "# Graph Report\n\n> Export ID: new-export\n")
        _add_text(
            tf,
            "graphify-out/graph-export-manifest.json",
            json.dumps({
                "version": 1,
                "export_id": "new-export",
                "artifacts": {
                    "graph": "graph.json",
                    "delta": "graph-delta.json",
                    "communities": "communities.json",
                    "report": "graph-report.md",
                },
                "counts": {"nodes": 2, "edges": 1, "communities": 1},
            }),
        )
        _add_bytes(
            tf,
            "graphify-out/dashboard-neighborhoods.sqlite3",
            _dashboard_index_bytes(tmp_path, export_id="old-export"),
        )
        _add_text(tf, "external-catalogs/skills-sh/catalog.json", "{}")
        for slug in sorted(DEFAULT_HARNESSES):
            _add_text(tf, f"entities/harnesses/{slug}.md", f"# {slug}\n")

    with pytest.raises(GraphArtifactError, match="dashboard index export_id mismatch"):
        validate_graph_artifacts(
            tmp_path,
            deep=True,
            min_nodes=2,
            min_edges=1,
            min_skills_sh_nodes=1,
            min_semantic_edges=1,
            expected_harnesses={"langgraph"},
        )


def test_validate_graph_artifacts_rejects_stale_preview_html(
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
    (tmp_path / "viz-overview.html").write_text(
        '<meta name="ctx-graph-export-id" content="old-export">',
        encoding="utf-8",
    )

    with pytest.raises(GraphArtifactError, match="stale graph preview"):
        validate_graph_artifacts(
            tmp_path,
            deep=True,
            min_nodes=2,
            min_edges=1,
            min_skills_sh_nodes=1,
            min_semantic_edges=1,
            expected_harnesses={"langgraph"},
        )


def test_validate_graph_artifacts_rejects_stale_root_communities(
    tmp_path: Path,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    _write_archive(tmp_path)
    (tmp_path / "communities.json").write_text(
        json.dumps({"export_id": "old-export", "total_communities": 1}),
        encoding="utf-8",
    )

    with pytest.raises(GraphArtifactError, match="stale graph/communities.json"):
        validate_graph_artifacts(
            tmp_path,
            expected_harnesses={"langgraph"},
        )


@pytest.mark.parametrize(
    ("archive_kwargs", "missing_name"),
    [
        ({"include_delta": False}, "graph-delta.json"),
        ({"include_report": False}, "graph-report.md"),
        ({"include_manifest": False}, "graph-export-manifest.json"),
    ],
)
def test_validate_graph_artifacts_rejects_missing_graph_export_files(
    tmp_path: Path,
    archive_kwargs: dict[str, Any],
    missing_name: str,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(tmp_path, **archive_kwargs)

    with pytest.raises(GraphArtifactError, match=missing_name):
        validate_graph_artifacts(tmp_path, expected_harnesses={"langgraph"})


@pytest.mark.parametrize(
    "archive_kwargs",
    [
        {"graph_export_id": "artifact-export", "manifest_export_id": "manifest-export"},
        {"delta_export_id": "artifact-export", "manifest_export_id": "manifest-export"},
        {
            "communities_export_id": "artifact-export",
            "manifest_export_id": "manifest-export",
        },
        {"report_export_id": "artifact-export", "manifest_export_id": "manifest-export"},
    ],
)
def test_validate_graph_artifacts_rejects_artifact_export_id_mismatch(
    tmp_path: Path,
    archive_kwargs: dict[str, Any],
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(tmp_path, **archive_kwargs)

    with pytest.raises(GraphArtifactError, match="export_id mismatch"):
        validate_graph_artifacts(tmp_path, expected_harnesses={"langgraph"})


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


def test_validate_graph_artifacts_rejects_missing_skill_bundle_reference(
    tmp_path: Path,
) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(
        tmp_path,
        converted_skill_text=(
            "# Example\n\n"
            "Use `resources/implementation-playbook.md` for the implementation flow.\n"
        ),
    )

    with pytest.raises(GraphArtifactError, match="missing bundled skill file"):
        validate_graph_artifacts(tmp_path, expected_harnesses={"langgraph"})


def test_validate_graph_artifacts_rejects_body_unavailable_catalog_records(
    tmp_path: Path,
) -> None:
    _write_catalog(tmp_path, converted_path=None)
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(tmp_path, include_converted=False)

    with pytest.raises(GraphArtifactError, match="body-unavailable records"):
        validate_graph_artifacts(tmp_path)


def test_validate_graph_artifacts_rejects_missing_entity_overlay(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    _write_archive(tmp_path)
    (tmp_path / "entity-overlays.jsonl").unlink()

    with pytest.raises(GraphArtifactError, match="entity-overlays.jsonl"):
        validate_graph_artifacts(tmp_path)


def test_validate_graph_artifacts_rejects_invalid_entity_overlay(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    _write_archive(tmp_path)
    (tmp_path / "entity-overlays.jsonl").write_text(
        json.dumps({
            "overlay_id": "bad-overlay",
            "nodes": [{"id": "skill:bad"}],
            "edges": [{"source": "skill:bad", "target": "skill:other", "weight": 2}],
        })
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GraphArtifactError, match="weight must be 0..1"):
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


def test_validate_graph_artifacts_rejects_lock_members(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    (tmp_path / "communities.json").write_text("{}", encoding="utf-8")
    _write_archive(tmp_path, include_lock=True)

    with pytest.raises(GraphArtifactError, match="lock member"):
        validate_graph_artifacts(tmp_path)


def test_validate_graph_artifacts_rejects_transient_queue_state(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    _write_archive(tmp_path, include_queue=True)

    with pytest.raises(GraphArtifactError, match="transient queue state"):
        validate_graph_artifacts(tmp_path)


def test_validate_graph_artifacts_rejects_runtime_queue_state(tmp_path: Path) -> None:
    _write_catalog(
        tmp_path,
        converted_path="converted/skills-sh-example-skill/SKILL.md",
    )
    _write_archive(tmp_path)
    graph = {
        "graph": {"export_id": "export-test"},
        "nodes": [
            {"id": "skill:skills-sh-example-skill", "type": "skill"},
            {"id": "harness:langgraph", "type": "harness"},
        ],
        "edges": [],
    }
    _write_runtime_archive(tmp_path, graph=graph, include_queue=True)

    with pytest.raises(GraphArtifactError, match="transient queue state"):
        validate_graph_artifacts(tmp_path, expected_harnesses={"langgraph"})


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

    assert _scan_graph_json(BytesIO(payload)) == (2, 2, 1, 1, 1, None)


def test_scan_graph_json_rejects_out_of_range_edge_scores() -> None:
    graph = {
        "nodes": [{"id": "skill:a", "type": "skill"}],
        "edges": [
            {
                "source": "skill:a",
                "target": "skill:b",
                "semantic_sim": 2.0,
                "tag_sim": 0.0,
                "token_sim": 0.0,
                "weight": 0.5,
                "final_weight": 0.5,
            },
        ],
    }
    payload = json.dumps(graph).encode("utf-8")

    with pytest.raises(GraphArtifactError, match="semantic_sim must be 0..1"):
        _scan_graph_json(BytesIO(payload))


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_scan_graph_json_rejects_non_finite_edge_scores(raw: str) -> None:
    payload = (
        b'{"nodes":[{"id":"skill:a","type":"skill"}],"edges":['
        b'{"source":"skill:a","target":"skill:b","semantic_sim":'
        + raw.encode("ascii")
        + b',"tag_sim":0.0,"token_sim":0.0,"weight":0.5,"final_weight":0.5}'
        b"]}"
    )

    with pytest.raises(GraphArtifactError, match="semantic_sim must be finite"):
        _scan_graph_json(BytesIO(payload))


def test_scan_graph_json_rejects_weight_final_weight_drift() -> None:
    graph = {
        "nodes": [{"id": "skill:a", "type": "skill"}],
        "edges": [
            {
                "source": "skill:a",
                "target": "skill:b",
                "semantic_sim": 0.8,
                "tag_sim": 0.0,
                "token_sim": 0.0,
                "weight": 0.7,
                "final_weight": 0.5,
            },
        ],
    }
    payload = json.dumps(graph).encode("utf-8")

    with pytest.raises(GraphArtifactError, match="weight must equal final_weight"):
        _scan_graph_json(BytesIO(payload))


def test_scan_graph_json_rejects_weight_drift_with_nested_score_components() -> None:
    graph = {
        "nodes": [{"id": "skill:a", "type": "skill"}],
        "edges": [
            {
                "source": "skill:a",
                "target": "skill:b",
                "semantic_sim": 0.8,
                "tag_sim": 0.0,
                "token_sim": 0.0,
                "weight": 0.7,
                "final_weight": 0.5,
                "score_components": {
                    "semantic": 0.8,
                    "tag": 0.0,
                    "token": 0.0,
                },
            },
        ],
    }
    payload = json.dumps(graph).encode("utf-8")

    with pytest.raises(GraphArtifactError, match="weight must equal final_weight"):
        _scan_graph_json(BytesIO(payload))


def test_scan_graph_json_rejects_score_component_drift() -> None:
    graph = {
        "nodes": [{"id": "skill:a", "type": "skill"}],
        "edges": [
            {
                "source": "skill:a",
                "target": "skill:b",
                "semantic_sim": 0.8,
                "tag_sim": 0.0,
                "token_sim": 0.0,
                "weight": 0.8,
                "final_weight": 0.8,
                "score_components": {
                    "semantic": 0.4,
                    "type_affinity": 0.1,
                },
            },
        ],
    }
    payload = json.dumps(graph).encode("utf-8")

    with pytest.raises(GraphArtifactError, match="score_components must sum"):
        _scan_graph_json(BytesIO(payload))


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_scan_graph_json_rejects_out_of_range_score_components(value: float) -> None:
    graph = {
        "nodes": [{"id": "skill:a", "type": "skill"}],
        "edges": [
            {
                "source": "skill:a",
                "target": "skill:b",
                "weight": 0.5,
                "final_weight": 0.5,
                "score_components": {
                    "semantic": value,
                    "tag": 0.5 - value,
                },
            },
        ],
    }
    payload = json.dumps(graph).encode("utf-8")

    with pytest.raises(GraphArtifactError, match="score_components must be 0..1"):
        _scan_graph_json(BytesIO(payload))


@pytest.mark.parametrize("field", ["semantic_sim", "tag_sim", "token_sim"])
def test_overlay_validation_rejects_out_of_range_similarity_fields(
    tmp_path: Path,
    field: str,
) -> None:
    (tmp_path / "entity-overlays.jsonl").write_text(
        json.dumps({
            "nodes": [{"id": "skill:a"}],
            "edges": [
                {
                    "source": "skill:a",
                    "target": "skill:b",
                    "weight": 0.5,
                    "final_weight": 0.5,
                    field: 2.0,
                },
            ],
        })
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GraphArtifactError, match=f"{field} must be 0..1"):
        _validate_root_entity_overlay(tmp_path / "entity-overlays.jsonl")


def test_overlay_validation_rejects_weight_final_weight_drift(tmp_path: Path) -> None:
    (tmp_path / "entity-overlays.jsonl").write_text(
        json.dumps({
            "nodes": [{"id": "skill:a"}],
            "edges": [
                {
                    "source": "skill:a",
                    "target": "skill:b",
                    "weight": 0.7,
                    "final_weight": 0.5,
                },
            ],
        })
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GraphArtifactError, match="weight must equal final_weight"):
        _validate_root_entity_overlay(tmp_path / "entity-overlays.jsonl")


def test_overlay_validation_rejects_score_component_drift(tmp_path: Path) -> None:
    (tmp_path / "entity-overlays.jsonl").write_text(
        json.dumps({
            "nodes": [{"id": "skill:a"}],
            "edges": [
                {
                    "source": "skill:a",
                    "target": "skill:b",
                    "weight": 0.8,
                    "final_weight": 0.8,
                    "score_components": {
                        "semantic": 0.4,
                        "type_affinity": 0.1,
                    },
                },
            ],
        })
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GraphArtifactError, match="score_components must sum"):
        _validate_root_entity_overlay(tmp_path / "entity-overlays.jsonl")


@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_overlay_validation_rejects_out_of_range_score_components(
    tmp_path: Path,
    value: float,
) -> None:
    (tmp_path / "entity-overlays.jsonl").write_text(
        json.dumps({
            "nodes": [{"id": "skill:a"}],
            "edges": [
                {
                    "source": "skill:a",
                    "target": "skill:b",
                    "weight": 0.5,
                    "final_weight": 0.5,
                    "score_components": {
                        "semantic": value,
                        "tag": 0.5 - value,
                    },
                },
            ],
        })
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(GraphArtifactError, match="score_components must be 0..1"):
        _validate_root_entity_overlay(tmp_path / "entity-overlays.jsonl")


def test_scan_graph_json_extracts_top_level_graph_export_id() -> None:
    graph = {
        "directed": False,
        "graph": {
            "source_catalog_nodes": {"skills.sh": 1},
            "source_catalog_edges": {"skills.sh": 0},
            "export_id": "graph-export",
        },
        "nodes": [
            {
                "id": "skill:skills-sh-example-skill",
                "type": "skill",
                "source_catalog": "skills.sh",
            },
        ],
        "edges": [],
    }
    payload = json.dumps(graph, separators=(",", ":")).encode("utf-8")

    assert _scan_graph_json(BytesIO(payload)) == (1, 0, 0, 1, 0, "graph-export")


def test_scan_graph_json_ignores_node_level_export_id() -> None:
    graph = {
        "nodes": [
            {
                "id": "skill:skills-sh-example-skill",
                "type": "skill",
                "source_catalog": "skills.sh",
                "export_id": "node-export",
            },
        ],
        "edges": [],
    }
    payload = json.dumps(graph, separators=(",", ":")).encode("utf-8")

    assert _scan_graph_json(BytesIO(payload)) == (1, 0, 0, 1, 0, None)


def test_graph_only_workflow_uses_exact_release_counts() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text(
        encoding="utf-8"
    ))
    steps = workflow["jobs"]["graph-check"]["steps"]
    validate_step = next(
        step for step in steps if step.get("name") == "Validate shipped graph artifacts"
    )
    command = " ".join(
        line.rstrip("\\").strip()
        for line in validate_step["run"].splitlines()
        if line.strip()
    )
    argv = command.split()

    script_index = argv.index("src/validate_graph_artifacts.py")
    args = argv[script_index + 1:]
    parsed: dict[str, str | bool] = {}
    i = 0
    while i < len(args):
        flag = args[i]
        if i + 1 >= len(args) or args[i + 1].startswith("--"):
            parsed[flag] = True
            i += 1
        else:
            parsed[flag] = args[i + 1]
            i += 2

    assert argv[:script_index + 1] == ["python", "src/validate_graph_artifacts.py"]
    assert parsed == {
        "--graph-dir": "graph",
        "--deep": True,
        "--min-nodes": "100000",
        "--min-edges": "2000000",
        "--min-skills-sh-nodes": "89000",
        "--min-semantic-edges": "1000000",
        "--expected-nodes": "102928",
        "--expected-edges": "2913960",
        "--expected-semantic-edges": "1683193",
        "--expected-harness-nodes": "207",
        "--expected-skills-sh-nodes": "89471",
        "--expected-skills-sh-catalog-entries": "89465",
        "--expected-skills-sh-converted": "89465",
        "--expected-skill-pages": "91464",
        "--expected-agent-pages": "467",
        "--expected-mcp-pages": "10790",
        "--expected-harness-pages": "207",
        "--line-threshold": "180",
        "--max-stage-lines": "40",
    }


def test_graph_only_workflow_waits_for_release_asset_upload() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text(
        encoding="utf-8"
    ))
    steps = workflow["jobs"]["graph-check"]["steps"]
    resolve_step = next(
        step for step in steps if step.get("name") == "Resolve graph LFS artifacts"
    )
    script = resolve_step["run"]

    assert "release_asset_wait_seconds = 300" in script
    assert "while True:" in script
    assert "Waiting for matching release asset" in script
    assert "time.sleep(release_asset_poll_seconds)" in script
