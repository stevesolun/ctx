"""Behavior tests for the ctx-tag-backfill user story."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from ctx.core.quality import tag_backfill


def _entity_text(
    name: str,
    *,
    tags: str = "tags: []",
    body: str = "Build a FastAPI service with reliable testing.",
    attribution: str = "",
) -> str:
    return (
        f"{attribution}"
        "---\n"
        f"name: {name}\n"
        "description: A focused development helper.\n"
        f"{tags}\n"
        "---\n"
        f"# {name}\n\n"
        f"{body}\n"
    )


def _write_skill(home: Path, slug: str, text: str) -> Path:
    path = home / ".claude" / "skills" / slug / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_agent(home: Path, slug: str, text: str) -> Path:
    path = home / ".claude" / "agents" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_split_frontmatter_supports_import_attribution_header() -> None:
    attribution = (
        "<!-- strix-import: upstream=https://example.test rev=0123456789ab "
        "license=Apache-2.0 category=coordination -->\n"
    )
    text = _entity_text(
        "imported-skill",
        attribution=attribution,
    )

    prefix, body, frontmatter = tag_backfill._split_frontmatter(text)

    assert prefix == attribution
    assert "name: imported-skill" in frontmatter
    assert body.startswith("# imported-skill")


def test_split_frontmatter_leaves_malformed_sources_untouched() -> None:
    for text in (
        "# No frontmatter\n",
        "---\nname: unclosed\n",
        "# Body\n\n---\ntags: []\n---\nTrailing body\n",
        "<!-- unknown-import: upstream=https://example.test -->\n"
        "---\nname: helper\n---\n",
        "<!-- strix-import: upstream=https://example.test -->\n"
        "---\nname: helper\n---\n",
        "----\nname: helper\n----\n",
    ):
        assert tag_backfill._split_frontmatter(text) == (text, "", "")


def test_apply_does_not_treat_body_thematic_breaks_as_frontmatter(tmp_path: Path) -> None:
    source = tmp_path / "SKILL.md"
    original = "# Helper\n\nIntro\n\n---\ntags: []\n---\nClosing text\n"
    source.write_text(original, encoding="utf-8")
    proposal = tag_backfill.TagProposal(
        entity_type="skill",
        slug="helper",
        path=source,
        current_tags=[],
        proposed_add=["python"],
    )

    assert tag_backfill.apply_proposals([proposal]) == 0
    assert source.read_text(encoding="utf-8") == original


def test_render_frontmatter_merges_tags_without_removing_curated_values() -> None:
    frontmatter = "name: helper\ntags:\n  - curated\ndescription: Keep this field"

    rendered = tag_backfill._render_frontmatter_with_added_tags(
        frontmatter,
        ["python", "curated"],
    )
    tags, present = tag_backfill._parse_frontmatter_tags(rendered)

    assert present is True
    assert tags == ["curated", "python"]
    assert "description: Keep this field" in rendered


def test_proposal_prioritizes_slug_tokens_then_known_body_keywords(tmp_path: Path) -> None:
    source = tmp_path / "service-helper.md"
    source.write_text(_entity_text("service-helper"), encoding="utf-8")

    proposal = tag_backfill._propose(
        source,
        "skill",
        "service-helper",
        vocab=Counter({"python": 10, "fastapi": 8, "testing": 3}),
        max_tags=4,
    )

    assert proposal.proposed_add[:2] == ["service", "helper"]
    assert proposal.proposed_add[2:] == ["python", "fastapi"]
    assert proposal.sources["body_keyword"]


def test_discovery_finds_empty_skills_and_agents_but_skips_curated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    empty_skill = _write_skill(tmp_path, "empty-skill", _entity_text("empty-skill"))
    _write_skill(
        tmp_path,
        "curated-skill",
        _entity_text("curated-skill", tags="tags: [curated]"),
    )
    empty_agent = _write_agent(tmp_path, "empty-agent", _entity_text("empty-agent"))

    discovered = tag_backfill.discover_empty_tag_entities(tmp_path / "wiki")

    assert discovered == [
        ("skill", "empty-skill", empty_skill),
        ("agent", "empty-agent", empty_agent),
    ]


def test_run_and_apply_backfill_preserves_body_and_becomes_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    source = _write_skill(
        tmp_path,
        "python-fastapi",
        _entity_text("python-fastapi", body="Build a FastAPI service."),
    )

    report = tag_backfill.run_backfill(wiki_dir=tmp_path / "wiki", max_tags_per_entity=3)
    assert report.total_entities_scanned == 1
    assert report.entities_with_empty_tags == 1
    assert report.proposals[0].proposed_add[:2] == ["python", "fastapi"]

    assert tag_backfill.apply_proposals(report.proposals) == 1
    updated = source.read_text(encoding="utf-8")
    assert "# python-fastapi\n\nBuild a FastAPI service." in updated
    assert tag_backfill.discover_empty_tag_entities(tmp_path / "wiki") == []


def test_report_renderers_expose_proposals_without_losing_sources(tmp_path: Path) -> None:
    proposal = tag_backfill.TagProposal(
        entity_type="skill",
        slug="python-helper",
        path=tmp_path / "SKILL.md",
        current_tags=[],
        proposed_add=["python"],
        sources={"slug_token": ["python"]},
    )
    report = tag_backfill.TagReport(1, 1, [proposal])

    markdown = tag_backfill.render_markdown(report)
    payload = json.loads(tag_backfill.render_json(report))

    assert "skill: `python-helper`" in markdown
    assert payload["proposals"][0]["sources"] == {"slug_token": ["python"]}


def test_main_report_only_writes_reports_without_mutating_entities(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    source = _write_skill(tmp_path, "python-helper", _entity_text("python-helper"))
    before = source.read_text(encoding="utf-8")
    markdown_report = tmp_path / "reports" / "tags.md"
    json_report = tmp_path / "reports" / "tags.json"

    result = tag_backfill.main(
        [
            "--report",
            str(markdown_report),
            "--report-json",
            str(json_report),
        ]
    )

    assert result == 0
    assert "report-only" in capsys.readouterr().out
    assert markdown_report.is_file()
    assert json_report.is_file()
    assert source.read_text(encoding="utf-8") == before


def test_main_apply_updates_empty_tags(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    source = _write_agent(tmp_path, "security-agent", _entity_text("security-agent"))

    result = tag_backfill.main(
        [
            "--apply",
            "--report",
            str(tmp_path / "tags.md"),
            "--report-json",
            str(tmp_path / "tags.json"),
        ]
    )

    assert result == 0
    assert "APPLIED 1 files" in capsys.readouterr().out
    assert "  - security" in source.read_text(encoding="utf-8")
