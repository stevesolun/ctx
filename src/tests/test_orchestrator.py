"""
test_orchestrator.py -- Tests for wiki_orchestrator (health score, deductions, counts).

Every test builds its own minimal wiki structure via tmp_path so the real
~/.claude/skill-wiki is never touched.
"""

from __future__ import annotations

import sys
import unittest.mock as mock
from pathlib import Path

# Ensure the project root is importable regardless of working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki_orchestrator as wo  # noqa: E402

from ._wiki_helpers import _FRESH_DATE, make_entity_page, make_wiki  # noqa: E402


def _minimal_wiki_for_orchestrator(tmp_path: Path) -> Path:
    """Build a wiki that satisfies run_check without external modules."""
    wiki = make_wiki(tmp_path)
    # Write all required SCHEMA sections so no points are deducted for them.
    schema_text = "\n".join([
        "# Wiki Schema",
        "",
        "## Domain",
        "Core skill domain description.",
        "",
        "## Conventions",
        "Naming conventions here.",
        "",
        "## Tag Taxonomy",
        "- python: python, testing",
        "",
        "## Page Thresholds",
        "MAX_PAGE_LINES: 200",
        "",
        "## Update Policy",
        "90 days.",
    ])
    (wiki / "SCHEMA.md").write_text(schema_text, encoding="utf-8")
    return wiki


class TestOrchestratorHealthScorePerfect:
    """test_orchestrator_health_score_perfect -- wiki with all valid pages scores 100."""

    def test_orchestrator_health_score_perfect(self, tmp_path: Path) -> None:
        wiki = _minimal_wiki_for_orchestrator(tmp_path)

        # Two pages cross-linking each other -- no orphans, no broken links.
        make_entity_page(
            wiki, "alpha", ["python"],
            body="See [[entities/skills/beta]].",
            updated=_FRESH_DATE,
            wikilinks=["entities/skills/beta"],
        )
        make_entity_page(
            wiki, "beta", ["python"],
            body="See [[entities/skills/alpha]].",
            updated=_FRESH_DATE,
            wikilinks=["entities/skills/alpha"],
        )

        # Patch _skill_names_on_disk so run_check does not scan the real
        # user skill directories (which would deduct points for skills that
        # have no entity page in our isolated tmp wiki).
        with mock.patch.object(wo, "_skill_names_on_disk", return_value=[]):
            report = wo.run_check(wiki)

        # Score must be 100 if no deductions were triggered.
        assert report.score == 100, (
            f"Expected score 100 but got {report.score}. Warnings: {report.warnings}"
        )


class TestOrchestratorHealthDeductsForOrphans:
    """test_orchestrator_health_deducts_for_orphans -- each orphan costs 1 point."""

    def test_orchestrator_health_deducts_for_orphans(self, tmp_path: Path) -> None:
        wiki = _minimal_wiki_for_orchestrator(tmp_path)

        # Create two isolated pages with no cross-links.
        make_entity_page(wiki, "island-one", ["python"], body="Standalone.", updated=_FRESH_DATE)
        make_entity_page(wiki, "island-two", ["python"], body="Also standalone.", updated=_FRESH_DATE)

        report = wo.run_check(wiki)

        assert len(report.orphan_pages) >= 2, (
            f"Expected 2+ orphan pages, got {report.orphan_pages}"
        )
        # Each orphan costs 1 point; 2 orphans -> score <= 98.
        assert report.score <= 98, (
            f"Score should have dropped for orphans but got {report.score}"
        )


class TestOrchestratorHealthDeductsForBrokenLinks:
    """test_orchestrator_health_deducts_for_broken_links -- each broken link costs 2 points."""

    def test_orchestrator_health_deducts_for_broken_links(self, tmp_path: Path) -> None:
        wiki = _minimal_wiki_for_orchestrator(tmp_path)

        # Page with two wikilinks pointing at pages that don't exist.
        content = """\
---
title: linker-page
created: 2025-01-01
updated: 2026-03-01
type: skill
tags: [python]
status: installed
---

See [[ghost-page-alpha]] and [[ghost-page-beta]] for info.
"""
        (wiki / "entities" / "skills" / "linker-page.md").write_text(content, encoding="utf-8")

        report = wo.run_check(wiki)

        assert report.broken_wikilinks, (
            f"Expected broken wikilinks to be recorded; got {report.broken_wikilinks}"
        )
        # Each broken link deducts 2 points.
        expected_max = 100 - (2 * len(report.broken_wikilinks))
        assert report.score <= expected_max, (
            f"Score {report.score} does not reflect -2 per broken link "
            f"({len(report.broken_wikilinks)} broken links -> max {expected_max})"
        )


class TestOrchestratorStatusReturnsCounts:
    """test_orchestrator_status_returns_counts -- _entity_pages and _converted_names return counts."""

    def test_orchestrator_status_returns_counts(self, tmp_path: Path) -> None:
        wiki = _minimal_wiki_for_orchestrator(tmp_path)

        make_entity_page(wiki, "skill-one", ["python"], body="One.", updated=_FRESH_DATE)
        make_entity_page(wiki, "skill-two", ["python"], body="Two.", updated=_FRESH_DATE, has_pipeline=True)

        # Create a converted directory for skill-two to simulate a pipeline.
        (wiki / "converted" / "skill-two").mkdir(parents=True)

        entity_pages = wo._entity_pages(wiki)
        converted_names = wo._converted_names(wiki)

        assert len(entity_pages) == 2, f"Expected 2 entity pages, got {len(entity_pages)}"
        assert len(converted_names) == 1, f"Expected 1 converted skill, got {converted_names}"
        assert "skill-two" in converted_names
