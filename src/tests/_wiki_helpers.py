"""
_wiki_helpers.py -- Shared helpers for wiki_query, wiki_lint, and wiki_orchestrator tests.

Exports:
  - Schema / date constants (_SCHEMA_TAGS, _MINIMAL_SCHEMA, _TODAY, _FRESH_DATE, _STALE_DATE)
  - make_wiki(tmp_path): build the minimal wiki skeleton used by every test
  - make_entity_page(...): write a properly-formatted entity page to the wiki

Kept out of conftest.py because these are builders, not pytest fixtures.
Leading-underscore module name signals "internal to the tests package".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
_FRESH_DATE = "2026-03-01"   # ~39 days before TODAY -- not stale
_STALE_DATE = "2024-01-01"   # >90 days before TODAY -- stale


def make_wiki(tmp_path: Path) -> Path:
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
