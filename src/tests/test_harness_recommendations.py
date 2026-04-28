"""Regression coverage for harness recommendation surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import ctx.api
import scan_repo
from ctx.core.resolve import resolve_skills


def _minimal_profile() -> dict[str, Any]:
    return {
        "repo_path": "/tmp/repo",
        "languages": [],
        "frameworks": [
            {"name": "react", "confidence": 0.95, "evidence": ["package.json"]}
        ],
        "infrastructure": [],
        "data_stores": [],
        "testing": [],
        "ai_tooling": [],
        "build_system": [],
        "docs": [],
    }


class _FakeGraph:
    def number_of_nodes(self) -> int:
        return 1


def test_resolver_routes_harness_graph_hits_to_harness_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolve_skills, "_GRAPH_AVAILABLE", True)
    monkeypatch.setattr(resolve_skills, "_load_graph", lambda: _FakeGraph())

    def fake_resolve_by_seeds(graph: _FakeGraph, seeds: list[str], **kwargs: Any):
        return [
            {
                "name": "text-to-cad",
                "type": "harness",
                "score": 3.0,
                "normalized_score": 0.72,
                "shared_tags": ["cad", "automation"],
                "via": ["react"],
            }
        ]

    monkeypatch.setattr(resolve_skills, "_resolve_by_seeds", fake_resolve_by_seeds)

    available = {
        "react": {
            "path": str(tmp_path / "react" / "SKILL.md"),
            "name": "react",
        }
    }
    manifest = resolve_skills.resolve(_minimal_profile(), available, {})

    assert [h["name"] for h in manifest["harnesses"]] == ["text-to-cad"]
    assert [m["name"] for m in manifest["mcp_servers"]] == []
    assert "text-to-cad" not in [entry["skill"] for entry in manifest["load"]]


def test_scan_recommendations_print_harness_bucket(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolve_skills, "discover_available_skills", lambda _: {})
    monkeypatch.setattr(resolve_skills, "read_wiki_overrides", lambda _: {})
    monkeypatch.setattr(
        resolve_skills,
        "resolve",
        lambda *args, **kwargs: {
            "load": [],
            "mcp_servers": [],
            "harnesses": [
                {
                    "name": "text-to-cad",
                    "score": 4.0,
                    "normalized_score": 0.8,
                    "shared_tags": ["cad", "3d"],
                    "reason": "graph neighbor of react",
                }
            ],
            "warnings": [],
        },
    )

    scan_repo._print_recommendations(str(tmp_path), {"repo_path": str(tmp_path)})
    output = capsys.readouterr().out

    assert "-- Harnesses (1) --" in output
    assert "text-to-cad" in output
    assert "norm=0.80" in output


def test_public_api_lists_harness_entities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "skill-wiki"
    skills = wiki / "entities" / "skills"
    harnesses = wiki / "entities" / "harnesses"
    skills.mkdir(parents=True)
    harnesses.mkdir(parents=True)
    (skills / "python-patterns.md").write_text("# Python Patterns\n", encoding="utf-8")
    (harnesses / "text-to-cad.md").write_text("# Text to CAD\n", encoding="utf-8")

    monkeypatch.setattr(ctx.api, "default_wiki_dir", lambda: wiki)

    assert ctx.api.list_all_entities("harness") == ["text-to-cad"]
    assert "text-to-cad" in ctx.api.list_all_entities()
    assert ctx.api.list_all_entities("plugin") == []
