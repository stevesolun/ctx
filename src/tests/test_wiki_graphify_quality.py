"""
test_wiki_graphify_quality.py -- Verify the graph writer attaches quality attrs.

``wiki_graphify._attach_quality_attrs`` is the only part of graphify that
participates in the Phase 3 quality pipeline; we test it in isolation
against a small in-memory graph so the test stays fast and doesn't need
a real wiki tree.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import networkx as nx
import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import wiki_graphify as wg  # noqa: E402


def _write_sidecar(
    sidecar_dir: Path, slug: str, subject_type: str, score: float, grade: str
) -> None:
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / f"{slug}.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "subject_type": subject_type,
                "score": score,
                "grade": grade,
            }
        ),
        encoding="utf-8",
    )


def test_attach_quality_decorates_matching_nodes(tmp_path: Path) -> None:
    G = nx.Graph()
    G.add_node("skill:alpha", label="alpha", type="skill", tags=["python"])
    G.add_node("agent:beta", label="beta", type="agent", tags=["python"])
    G.add_node("skill:orphan", label="orphan", type="skill", tags=[])

    sidecar = tmp_path / "quality"
    _write_sidecar(sidecar, "alpha", "skill", 0.85, "A")
    _write_sidecar(sidecar, "beta", "agent", 0.55, "C")

    attached = wg._attach_quality_attrs(G, sidecar)

    assert attached == 2
    assert G.nodes["skill:alpha"]["quality_grade"] == "A"
    assert G.nodes["skill:alpha"]["quality_score"] == pytest.approx(0.85)
    assert G.nodes["agent:beta"]["quality_grade"] == "C"
    # Orphan keeps the default placeholders, not missing keys.
    assert G.nodes["skill:orphan"]["quality_score"] is None
    assert G.nodes["skill:orphan"]["quality_grade"] is None


def test_attach_quality_missing_dir_is_noop(tmp_path: Path) -> None:
    G = nx.Graph()
    G.add_node("skill:alpha", label="alpha", type="skill", tags=[])
    attached = wg._attach_quality_attrs(G, tmp_path / "does-not-exist")
    assert attached == 0
    # Default placeholders still applied so downstream reads are safe.
    assert G.nodes["skill:alpha"]["quality_score"] is None


def test_attach_quality_skips_corrupt_sidecar(tmp_path: Path) -> None:
    sidecar = tmp_path / "quality"
    sidecar.mkdir()
    (sidecar / "bad.json").write_text("{not valid json", encoding="utf-8")
    _write_sidecar(sidecar, "good", "skill", 0.7, "B")

    G = nx.Graph()
    G.add_node("skill:good", label="good", type="skill", tags=[])
    G.add_node("skill:bad", label="bad", type="skill", tags=[])

    attached = wg._attach_quality_attrs(G, sidecar)
    assert attached == 1
    assert G.nodes["skill:good"]["quality_grade"] == "B"
    assert G.nodes["skill:bad"]["quality_grade"] is None


def test_attach_quality_ignores_slug_with_no_node(tmp_path: Path) -> None:
    sidecar = tmp_path / "quality"
    _write_sidecar(sidecar, "ghost", "skill", 0.9, "A")
    G = nx.Graph()
    G.add_node("skill:real", label="real", type="skill", tags=[])
    attached = wg._attach_quality_attrs(G, sidecar)
    assert attached == 0
    assert G.nodes["skill:real"]["quality_score"] is None
