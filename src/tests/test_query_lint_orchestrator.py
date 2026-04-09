"""
test_query_lint_orchestrator.py -- Tests for wiki_query, wiki_lint, and wiki_orchestrator.

Covers keyword search, tag filtering, stats, related-skill lookup, lint checks,
and orchestrator health-score accounting. Every test builds its own minimal wiki
structure via tmp_path so the real ~/.claude/skill-wiki is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import unittest.mock as mock

import pytest

# Ensure the project root is importable regardless of working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki_query as wq
import wiki_lint as wl
import wiki_orchestrator as wo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA_TAGS = (
    "python", "fastapi", "docker", "testing", "architecture",
    "patterns", "cli", "database", "async", "security",
)

_MINIMAL_SCHEMA = """\
# Skill Wiki Schema

## Domain
Skills are reusable knowledge units.

## Conventions
Follow the naming convention: kebab-case slugs.

## Tag Taxonomy
- python: python, fastapi, testing, async
- infra: docker, kubernetes, ci-cd
- design: architecture, patterns, cli, database, security

## Page Thresholds
MAX_PAGE_LINES: 200

## Update Policy
Pages older than 90 days are considered stale.
"""

_TODAY = "2026-04-09"
_FRESH_DATE = "2026-03-01"   # ~39 days before TODAY — not stale
_STALE_DATE = "2024-01-01"   # >90 days before TODAY — stale


def _make_wiki(tmp_path: Path) -> Path:
    """Create the minimal wiki skeleton (SCHEMA.md, index.md, log.md, entities/skills/)."""
    wiki = tmp_path / "skill-wiki"
    (wiki / "entities" / "skills").mkdir(parents=True)
    (wiki / "SCHEMA.md").write_text(_MINIMAL_SCHEMA, encoding="utf-8")
    (wiki / "index.md").write_text("# Index\n\n## Skills\n", encoding="utf-8")
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    return wiki


def make_entity_page(
    wiki_dir: Path,
    name: str,
    tags: list[str],
    *,
    body: str = "",
    updated: str = _FRESH_DATE,
    created: str = "2025-01-01",
    status: str = "installed",
    has_pipeline: bool = False,
    wikilinks: list[str] | None = None,
    extra_fm: dict[str, Any] | None = None,
) -> Path:
    """Write a properly-formatted entity page to wiki_dir/entities/skills/<name>.md.

    Args:
        wiki_dir: Root of the temporary wiki.
        name: Slug (no extension) for the page file.
        tags: Tag list to include in frontmatter.
        body: Markdown body text appended after the frontmatter block.
        updated: ISO date string for the ``updated`` frontmatter key.
        created: ISO date string for the ``created`` frontmatter key.
        status: ``status`` frontmatter value.
        has_pipeline: Whether to write ``has_pipeline: true``.
        wikilinks: Additional ``[[wikilink]]`` tokens injected into the body.
        extra_fm: Extra key/value pairs merged into frontmatter.

    Returns:
        Path to the written file.
    """
    tags_str = "[" + ", ".join(tags) + "]"
    fm_lines = [
        "---",
        f"title: {name}",
        f"created: {created}",
        f"updated: {updated}",
        "type: skill",
        f"tags: {tags_str}",
        f"status: {status}",
    ]
    if has_pipeline:
        # wiki_query reads `has_transformed` to populate SkillPage.has_transformed;
        # wiki_orchestrator reads `has_pipeline` for its frontmatter checks.
        # Write both so both modules see the flag correctly.
        fm_lines.append("has_pipeline: true")
        fm_lines.append("has_transformed: true")
    if extra_fm:
        for k, v in extra_fm.items():
            fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")

    link_block = ""
    if wikilinks:
        link_block = "\n" + "\n".join(f"[[{lnk}]]" for lnk in wikilinks) + "\n"

    content = "\n".join(fm_lines) + "\n\n" + body + link_block
    dest = wiki_dir / "entities" / "skills" / f"{name}.md"
    dest.write_text(content, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# wiki_query tests
# ---------------------------------------------------------------------------


class TestQueryKeywordMatch:
    """test_query_keyword_match -- searching 'docker' finds skills with docker in name/body."""

    def test_query_keyword_match(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        make_entity_page(wiki, "docker-compose-pro", ["docker"], body="Use docker-compose for local dev.")
        # status="" ensures this page scores exactly 0 for a "docker" query (no installed bonus).
        make_entity_page(wiki, "python-basics", ["python"], body="Learn Python fundamentals.", status="")

        pages = wq.load_all_pages(wiki)
        results = wq.search_by_query(pages, "docker")

        names = [p.name for p in results]
        assert "docker-compose-pro" in names, "docker skill must be returned for 'docker' query"
        assert "python-basics" not in names, "unrelated skill must not appear"


class TestQueryTagFilter:
    """test_query_tag_filter -- --tag python returns only python-tagged skills."""

    def test_query_tag_filter(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        make_entity_page(wiki, "fastapi-service", ["python", "fastapi"], body="FastAPI patterns.")
        make_entity_page(wiki, "docker-network", ["docker"], body="Docker networking tips.")
        make_entity_page(wiki, "pytest-patterns", ["python", "testing"], body="Pytest best practices.")

        pages = wq.load_all_pages(wiki)
        results = wq.filter_by_tag(pages, "python")

        names = {p.name for p in results}
        assert "fastapi-service" in names
        assert "pytest-patterns" in names
        assert "docker-network" not in names, "non-python skill must be excluded"


class TestQueryStats:
    """test_query_stats -- compute_stats returns total_entity_pages, top_tags, with_pipeline."""

    def test_query_stats(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        make_entity_page(wiki, "skill-a", ["python"], body="A skill.")
        make_entity_page(wiki, "skill-b", ["python", "fastapi"], body="B skill.", has_pipeline=True)
        make_entity_page(wiki, "skill-c", ["docker"], body="C skill.")
        # Create the converted dir so has_pipeline resolves
        (wiki / "converted" / "skill-b").mkdir(parents=True)

        pages = wq.load_all_pages(wiki)
        stats = wq.compute_stats(wiki, pages)

        assert stats["total_entity_pages"] == 3
        assert stats["with_pipeline"] == 1
        tag_keys = [t for t, _ in stats["top_tags"]]
        assert "python" in tag_keys
        assert "docker" in tag_keys


class TestQueryRelated:
    """test_query_related -- --related fastapi-pro finds python-tagged skills."""

    def test_query_related(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        make_entity_page(wiki, "fastapi-pro", ["python", "fastapi"], body="The target skill.")
        make_entity_page(wiki, "pydantic-models", ["python", "fastapi"], body="Pydantic usage.")
        make_entity_page(wiki, "docker-compose-pro", ["docker"], body="Docker only.")

        pages = wq.load_all_pages(wiki)
        related = wq.find_related(pages, "fastapi-pro")

        names = [p.name for p in related]
        assert "pydantic-models" in names, "pydantic-models shares tags so must appear as related"
        assert "docker-compose-pro" not in names, "no shared tags with fastapi-pro"


class TestQueryNoResults:
    """test_query_no_results -- searching 'xyznonexistent' returns empty list."""

    def test_query_no_results(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        # Use status="" so the installed bonus (+0.5) does not leak into an
        # otherwise zero-scoring page when the query has no keyword match.
        make_entity_page(wiki, "python-basics", ["python"], body="Learn Python.", status="")

        pages = wq.load_all_pages(wiki)
        results = wq.search_by_query(pages, "xyznonexistent")

        assert results == [], f"Expected no results, got {[p.name for p in results]}"


# ---------------------------------------------------------------------------
# wiki_lint tests
# ---------------------------------------------------------------------------


def _collect(wiki: Path) -> dict[str, Path]:
    """Thin wrapper around wiki_lint's internal page collector."""
    return wl._collect_pages(wiki)


class TestLintMissingFrontmatter:
    """test_lint_detects_missing_frontmatter -- page without --- frontmatter is flagged."""

    def test_lint_detects_missing_frontmatter(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        bad = wiki / "entities" / "skills" / "no-frontmatter.md"
        bad.write_text("# No frontmatter here\n\nJust body text.\n", encoding="utf-8")

        pages = _collect(wiki)
        findings = wl.check_missing_frontmatter(pages)

        checks = [f.check for f in findings]
        pages_hit = [f.page for f in findings]
        assert "missing_frontmatter" in checks
        assert any("no-frontmatter" in p for p in pages_hit)


class TestLintBrokenWikilink:
    """test_lint_detects_broken_wikilink -- [[nonexistent-page]] is flagged as broken."""

    def test_lint_detects_broken_wikilink(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        # Page that references a target that does not exist in the wiki
        content = """\
---
title: linker
created: 2025-01-01
updated: 2026-03-01
type: skill
tags: [python]
status: installed
---

See [[nonexistent-ghost-page]] for details.
Also [[another-missing-page]] is broken.
"""
        (wiki / "entities" / "skills" / "linker.md").write_text(content, encoding="utf-8")

        pages = _collect(wiki)
        findings = wl.check_broken_wikilinks(pages)

        messages = " ".join(f.message for f in findings)
        assert "nonexistent-ghost-page" in messages or "another-missing-page" in messages
        assert all(f.check == "broken_wikilink" for f in findings)
        assert all(f.severity == "error" for f in findings)


class TestLintStalePage:
    """test_lint_detects_stale_page -- page with updated: 2024-01-01 (>90 days) is flagged."""

    def test_lint_detects_stale_page(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        make_entity_page(
            wiki, "old-skill", ["python"],
            body="Old content.",
            updated=_STALE_DATE,
        )

        pages = _collect(wiki)
        findings = wl.check_stale_content(pages)

        assert findings, "Stale page must produce at least one finding"
        assert all(f.check == "stale_content" for f in findings)
        assert all(f.severity == "warn" for f in findings)
        assert any("old-skill" in f.page for f in findings)


class TestLintOrphanPage:
    """test_lint_detects_orphan -- page with zero inbound wikilinks is flagged."""

    def test_lint_detects_orphan(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        # Island page: no other page links to it
        make_entity_page(wiki, "lonely-skill", ["python"], body="Nobody links here.")
        # A second page that does NOT link to lonely-skill
        make_entity_page(
            wiki, "social-skill", ["python"],
            body="I link to [[entities/skills/social-skill]] (self-loop excluded).",
        )

        pages = _collect(wiki)
        findings = wl.check_orphan_pages(pages)

        orphaned_pages = [f.page for f in findings]
        assert any("lonely-skill" in p for p in orphaned_pages), (
            "lonely-skill has no inbound links so must be flagged as orphan"
        )
        assert all(f.check == "orphan_page" for f in findings)
        assert all(f.severity == "warn" for f in findings)


class TestLintCleanPagePasses:
    """test_lint_clean_page_passes -- well-formed page with frontmatter and 2+ wikilinks passes."""

    def test_lint_clean_page_passes(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        # Create two pages that cross-link each other so neither is orphaned.
        make_entity_page(
            wiki, "clean-a", ["python"],
            body="See also [[entities/skills/clean-b]] for companion content.",
            updated=_FRESH_DATE,
            wikilinks=["entities/skills/clean-b"],
        )
        make_entity_page(
            wiki, "clean-b", ["python"],
            body="Paired with [[entities/skills/clean-a]].",
            updated=_FRESH_DATE,
            wikilinks=["entities/skills/clean-a"],
        )

        pages = _collect(wiki)

        fm_findings = [
            f for f in wl.check_missing_frontmatter(pages)
            if "clean-a" in f.page or "clean-b" in f.page
        ]
        stale_findings = [
            f for f in wl.check_stale_content(pages)
            if "clean-a" in f.page or "clean-b" in f.page
        ]
        broken_findings = [
            f for f in wl.check_broken_wikilinks(pages)
            if "clean-a" in f.page or "clean-b" in f.page
        ]

        assert not fm_findings, f"No frontmatter errors expected; got {fm_findings}"
        assert not stale_findings, f"No stale findings expected; got {stale_findings}"
        assert not broken_findings, f"No broken-link findings expected; got {broken_findings}"


class TestLintOversizedPage:
    """test_lint_oversized_page -- page >200 lines is flagged as info."""

    def test_lint_oversized_page(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        # Build a page that exceeds MAX_PAGE_LINES (200)
        padding = "\n".join(f"line {i}" for i in range(210))
        make_entity_page(wiki, "huge-skill", ["python"], body=padding)

        pages = _collect(wiki)
        findings = wl.check_oversized_pages(pages)

        assert findings, "Oversized page must produce at least one finding"
        assert all(f.check == "oversized_page" for f in findings)
        assert all(f.severity == "info" for f in findings)
        assert any("huge-skill" in f.page for f in findings)


class TestLintTagHygiene:
    """test_lint_tag_hygiene -- tag not in SCHEMA taxonomy is flagged."""

    def test_lint_tag_hygiene(self, tmp_path: Path) -> None:
        wiki = _make_wiki(tmp_path)
        # Write a page with a tag that is definitely not in _MINIMAL_SCHEMA
        make_entity_page(wiki, "exotic-skill", ["notarealtag"], body="Content.")

        pages = _collect(wiki)
        findings = wl.check_tag_hygiene(pages, wiki)

        assert findings, "Unknown tag must produce a tag_hygiene finding"
        assert all(f.check == "tag_hygiene" for f in findings)
        assert all(f.severity == "warn" for f in findings)
        messages = " ".join(f.message for f in findings)
        assert "notarealtag" in messages


# ---------------------------------------------------------------------------
# wiki_orchestrator tests
# ---------------------------------------------------------------------------


def _minimal_wiki_for_orchestrator(tmp_path: Path) -> Path:
    """Build a wiki that satisfies run_check without external modules."""
    wiki = _make_wiki(tmp_path)
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

        # Two pages cross-linking each other — no orphans, no broken links.
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
        # Each orphan costs 1 point; 2 orphans → score <= 98.
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

        report_before = wo.HealthReport()
        report_before.score = 100

        report = wo.run_check(wiki)

        assert report.broken_wikilinks, (
            f"Expected broken wikilinks to be recorded; got {report.broken_wikilinks}"
        )
        # Each broken link deducts 2 points.
        expected_max = 100 - (2 * len(report.broken_wikilinks))
        assert report.score <= expected_max, (
            f"Score {report.score} does not reflect -2 per broken link "
            f"({len(report.broken_wikilinks)} broken links → max {expected_max})"
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
