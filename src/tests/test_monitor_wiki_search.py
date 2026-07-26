"""Focused contracts for the monitor's indexed wiki search."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ctx.monitor.services.wiki import search_entities_from_index


def _write_index(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE nodes("
            "id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.executemany(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    "skill:lower-degree",
                    "lower-degree",
                    "skill",
                    '["testing"]',
                    "Lower degree skill",
                    0.8,
                    0.2,
                    3,
                ),
                (
                    "agent:top-reviewer",
                    "top-reviewer",
                    "agent",
                    '["review"]',
                    "Higher degree agent",
                    0.9,
                    0.3,
                    9,
                ),
            ],
        )


def test_empty_query_uses_bounded_indexed_browse_results(tmp_path: Path) -> None:
    index = tmp_path / "dashboard.sqlite3"
    _write_index(index)

    rows = search_entities_from_index(
        index,
        "",
        limit=1,
        index_matches_manifest=lambda path: path == index,
    )
    skill_rows = search_entities_from_index(
        index,
        "",
        "skill",
        limit=5,
        index_matches_manifest=lambda path: path == index,
    )

    assert rows is not None
    assert [(row["slug"], row["type"]) for row in rows] == [("top-reviewer", "agent")]
    assert skill_rows is not None
    assert [(row["slug"], row["type"]) for row in skill_rows] == [("lower-degree", "skill")]
