"""
test_lint.py -- Tests for wiki_lint (frontmatter, wikilinks, staleness, orphans, size, tags).

Every test builds its own minimal wiki structure via tmp_path so the real
~/.claude/skill-wiki is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is importable regardless of working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki_lint as wl  # noqa: E402

from ._wiki_helpers import _FRESH_DATE, _STALE_DATE, make_entity_page, make_wiki  # noqa: E402


def _collect(wiki: Path) -> dict[str, Path]:
    """Thin wrapper around wiki_lint's internal page collector."""
    return wl._collect_pages(wiki)


class TestLintMissingFrontmatter:
    """test_lint_detects_missing_frontmatter -- page without --- frontmatter is flagged."""

    def test_lint_detects_missing_frontmatter(self, tmp_path: Path) -> None:
        wiki = make_wiki(tmp_path)
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
        wiki = make_wiki(tmp_path)
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
        wiki = make_wiki(tmp_path)
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
        wiki = make_wiki(tmp_path)
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
        wiki = make_wiki(tmp_path)
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
        wiki = make_wiki(tmp_path)
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
        wiki = make_wiki(tmp_path)
        # Write a page with a tag that is definitely not in _MINIMAL_SCHEMA
        make_entity_page(wiki, "exotic-skill", ["notarealtag"], body="Content.")

        pages = _collect(wiki)
        findings = wl.check_tag_hygiene(pages, wiki)

        assert findings, "Unknown tag must produce a tag_hygiene finding"
        assert all(f.check == "tag_hygiene" for f in findings)
        assert all(f.severity == "warn" for f in findings)
        messages = " ".join(f.message for f in findings)
        assert "notarealtag" in messages
