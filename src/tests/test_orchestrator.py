"""
test_orchestrator.py -- Tests for wiki_orchestrator (health score, deductions, counts).

Every test builds its own minimal wiki structure via tmp_path so the real
~/.claude/skill-wiki is never touched.
"""

from __future__ import annotations

import sys
import unittest.mock as mock
from pathlib import Path

import pytest

# Ensure the project root is importable regardless of working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki_orchestrator as wo  # noqa: E402

from ._wiki_helpers import _FRESH_DATE, make_entity_page, make_wiki  # noqa: E402


def _minimal_wiki_for_orchestrator(tmp_path: Path) -> Path:
    """Build a wiki that satisfies run_check without external modules."""
    wiki = make_wiki(tmp_path)
    # Write all required SCHEMA sections so no points are deducted for them.
    schema_text = "\n".join(
        [
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
        ]
    )
    (wiki / "SCHEMA.md").write_text(schema_text, encoding="utf-8")
    return wiki


def _complete_sync_modules() -> dict[str, mock.Mock]:
    wiki_sync = mock.Mock(spec=["ensure_wiki", "upsert_skill_page", "update_index", "append_log"])
    batch_convert = mock.Mock(spec=["convert_skill"])
    link_conversions = mock.Mock(spec=["run"])
    link_conversions.run.return_value = mock.Mock(errors=[])
    catalog_builder = mock.Mock(spec=["build_catalog", "update_wiki_index"])
    catalog_builder.build_catalog.return_value = {"total": 0}
    versions_catalog = mock.Mock(spec=["find_dual_version_skills", "build_versions_catalog"])
    versions_catalog.find_dual_version_skills.return_value = []
    wiki_lint = mock.Mock(spec=["run_lint"])
    wiki_lint.run_lint.return_value = []
    return {
        "wiki_sync": wiki_sync,
        "batch_convert": batch_convert,
        "link_conversions": link_conversions,
        "catalog_builder": catalog_builder,
        "versions_catalog": versions_catalog,
        "wiki_lint": wiki_lint,
    }


def _run_sync_with_modules(
    wiki: Path,
    modules: dict[str, mock.Mock],
) -> wo.HealthReport:
    with (
        mock.patch.object(wo, "_try_import", side_effect=lambda name, _report: modules[name]),
        mock.patch.object(wo.cfg, "all_skill_dirs", return_value=[]),
        mock.patch.object(wo, "_skill_names_on_disk", return_value=[]),
        mock.patch.object(wo, "_actual_page_count", return_value=0),
        mock.patch.object(wo, "run_check", return_value=wo.HealthReport()),
    ):
        return wo.run_sync(wiki)


class TestOrchestratorHealthScorePerfect:
    """test_orchestrator_health_score_perfect -- wiki with all valid pages scores 100."""

    def test_orchestrator_health_score_perfect(self, tmp_path: Path) -> None:
        wiki = _minimal_wiki_for_orchestrator(tmp_path)

        # Two pages cross-linking each other -- no orphans, no broken links.
        make_entity_page(
            wiki,
            "alpha",
            ["python"],
            body="See [[entities/skills/beta]].",
            updated=_FRESH_DATE,
            wikilinks=["entities/skills/beta"],
        )
        make_entity_page(
            wiki,
            "beta",
            ["python"],
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
        make_entity_page(
            wiki, "island-two", ["python"], body="Also standalone.", updated=_FRESH_DATE
        )

        report = wo.run_check(wiki)

        assert len(report.orphan_pages) >= 2, f"Expected 2+ orphan pages, got {report.orphan_pages}"
        # Each orphan costs 1 point; 2 orphans -> score <= 98.
        assert report.score <= 98, f"Score should have dropped for orphans but got {report.score}"


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
        make_entity_page(
            wiki, "skill-two", ["python"], body="Two.", updated=_FRESH_DATE, has_pipeline=True
        )

        # Create a converted directory for skill-two to simulate a pipeline.
        (wiki / "converted" / "skill-two").mkdir(parents=True)

        entity_pages = wo._entity_pages(wiki)
        converted_names = wo._converted_names(wiki)

        assert len(entity_pages) == 2, f"Expected 2 entity pages, got {len(entity_pages)}"
        assert len(converted_names) == 1, f"Expected 1 converted skill, got {converted_names}"
        assert "skill-two" in converted_names

    def test_entity_pages_include_all_recommendable_entity_types(self, tmp_path: Path) -> None:
        wiki = _minimal_wiki_for_orchestrator(tmp_path)

        make_entity_page(wiki, "skill-one", ["python"], body="One.", updated=_FRESH_DATE)
        (wiki / "entities" / "agents").mkdir(parents=True)
        (wiki / "entities" / "agents" / "reviewer.md").write_text(
            "---\ntitle: reviewer\ncreated: 2025-01-01\nupdated: 2026-03-01\ntype: agent\ntags: [testing]\n---\n",
            encoding="utf-8",
        )
        (wiki / "entities" / "mcp-servers" / "g").mkdir(parents=True)
        (wiki / "entities" / "mcp-servers" / "g" / "github.md").write_text(
            "---\ntitle: github\ncreated: 2025-01-01\nupdated: 2026-03-01\ntype: mcp-server\ntags: [testing]\n---\n",
            encoding="utf-8",
        )
        (wiki / "entities" / "harnesses").mkdir(parents=True)
        (wiki / "entities" / "harnesses" / "openhands.md").write_text(
            "---\ntitle: openhands\ncreated: 2025-01-01\nupdated: 2026-03-01\ntype: harness\ntags: [testing]\n---\n",
            encoding="utf-8",
        )

        entity_pages = [p.relative_to(wiki).as_posix() for p in wo._entity_pages(wiki)]

        assert entity_pages == [
            "entities/agents/reviewer.md",
            "entities/harnesses/openhands.md",
            "entities/mcp-servers/g/github.md",
            "entities/skills/skill-one.md",
        ]

    def test_run_check_uses_package_linter_for_non_entity_pages(self, tmp_path: Path) -> None:
        wiki = _minimal_wiki_for_orchestrator(tmp_path)
        (wiki / "concepts").mkdir()
        (wiki / "concepts" / "no-frontmatter.md").write_text(
            "# No frontmatter\n\nThis should be audited by wiki_lint.\n",
            encoding="utf-8",
        )

        with mock.patch.object(wo, "_skill_names_on_disk", return_value=[]):
            report = wo.run_check(wiki)

        assert any(
            "[lint]" in warning and "no-frontmatter" in warning for warning in report.warnings
        )


class TestOrchestratorAddFallback:
    def test_canonical_runtime_failure_exits_without_legacy_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        canonical = mock.Mock()
        canonical.add_skill.side_effect = RuntimeError("canonical boom")

        with (
            mock.patch.object(
                wo,
                "_load_module",
                return_value=wo.ModuleLoadResult(canonical),
            ) as load_module,
            mock.patch.object(wo, "_try_import") as try_import,
        ):
            with pytest.raises(SystemExit) as exc_info:
                wo.run_add(tmp_path / "wiki", "valid-skill")

        assert exc_info.value.code == 1
        load_module.assert_called_once()
        try_import.assert_not_called()
        canonical.add_skill.assert_called_once_with(
            source_path=Path("valid-skill"),
            name="valid-skill",
            wiki_path=tmp_path / "wiki",
            skills_dir=wo.cfg.skills_dir,
        )
        captured = capsys.readouterr()
        assert "skill_add.add_skill raised: canonical boom" in captured.err
        assert "Entity page" not in captured.out

    def test_absent_canonical_module_uses_legacy_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wiki = tmp_path / "wiki"
        legacy = mock.Mock()
        legacy.upsert_skill_page.return_value = True

        with (
            mock.patch.object(wo, "SCRIPT_DIR", tmp_path),
            mock.patch.object(wo, "_try_import", return_value=legacy) as try_import,
        ):
            wo.run_add(wiki, "valid-skill")

        try_import.assert_called_once()
        legacy.upsert_skill_page.assert_called_once_with(
            str(wiki),
            "valid-skill",
            {"path": "valid-skill", "reason": "manually added via orchestrator"},
        )
        legacy.update_index.assert_called_once_with(str(wiki), ["valid-skill"])
        legacy.append_log.assert_called_once_with(
            str(wiki), "add-skill", "valid-skill", ["Path: valid-skill"]
        )
        assert "Entity page created: valid-skill" in capsys.readouterr().out

    def test_canonical_import_failure_exits_without_legacy_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "skill_add.py").write_text(
            'raise RuntimeError("loader boom")\n', encoding="utf-8"
        )

        with (
            mock.patch.object(wo, "SCRIPT_DIR", tmp_path),
            mock.patch.object(wo, "_try_import") as try_import,
        ):
            with pytest.raises(SystemExit) as exc_info:
                wo.run_add(tmp_path / "wiki", "valid-skill")

        assert exc_info.value.code == 1
        try_import.assert_not_called()
        assert "skill_add load failed: import error: loader boom" in capsys.readouterr().err


class TestOrchestratorSyncFailures:
    def test_try_import_uses_canonical_wiki_sync_package(self) -> None:
        from ctx.core.wiki import wiki_sync

        report = wo.HealthReport()

        assert wo._try_import("wiki_sync", report) is wiki_sync
        assert report.skipped_modules == []

    def test_run_sync_uses_link_conversions_run(self, tmp_path: Path) -> None:
        modules = _complete_sync_modules()

        report = _run_sync_with_modules(tmp_path / "wiki", modules)

        modules["link_conversions"].run.assert_called_once_with(
            tmp_path / "wiki", wo.cfg.skills_dir
        )
        assert report.sync_failures == []

    def test_run_sync_reports_missing_step_capability(self, tmp_path: Path) -> None:
        modules = _complete_sync_modules()
        modules["wiki_sync"] = mock.Mock(spec=["upsert_skill_page", "update_index", "append_log"])

        report = _run_sync_with_modules(tmp_path / "wiki", modules)

        expected = "[wiki_sync.ensure_wiki] unavailable; step skipped"
        assert report.sync_failures == [expected]
        assert report.score == 99
        assert any(expected in warning for warning in report.warnings)

    def test_run_sync_records_ensure_wiki_exception_and_continues(self, tmp_path: Path) -> None:
        modules = _complete_sync_modules()
        modules["wiki_sync"].ensure_wiki.side_effect = RuntimeError("init boom")

        report = _run_sync_with_modules(tmp_path / "wiki", modules)

        expected = "[wiki_sync.ensure_wiki] init boom"
        assert report.sync_failures == [expected]
        assert report.score == 99
        assert any(expected in warning for warning in report.warnings)
        modules["link_conversions"].run.assert_called_once()
        modules["wiki_sync"].append_log.assert_called_once()

    def test_run_sync_surfaces_link_conversion_result_errors(self, tmp_path: Path) -> None:
        modules = _complete_sync_modules()
        modules["link_conversions"].run.return_value = mock.Mock(
            errors=["demo-skill: pipeline boom"]
        )

        report = _run_sync_with_modules(tmp_path / "wiki", modules)

        expected = "[link_conversions] demo-skill: pipeline boom"
        assert report.sync_failures == [expected]
        assert report.score == 99
        assert any(expected in warning for warning in report.warnings)

    def test_sync_cli_exits_nonzero_for_partial_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capfd: pytest.CaptureFixture[str],
    ) -> None:
        report = wo.HealthReport(
            score=100,
            warnings=["  [link_conversions] pipeline boom"],
            sync_failures=["[link_conversions] pipeline boom"],
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["wiki_orchestrator.py", "--wiki", str(tmp_path / "wiki"), "--sync"],
        )

        with mock.patch.object(wo, "run_sync", return_value=report):
            with pytest.raises(SystemExit) as exc_info:
                wo.main()

        assert exc_info.value.code == 1
        captured = capfd.readouterr()
        assert "Health Score: 99/100" in captured.out
        assert "Health Score: 100/100" not in captured.out
        assert "Sync Status: FAILED (1 failure)" in captured.out


def test_run_status_missing_wiki_exits_nonzero_without_creating_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_wiki = tmp_path / "missing-wiki"

    with pytest.raises(SystemExit) as exc_info:
        wo.run_status(missing_wiki)

    assert exc_info.value.code == 1
    assert not missing_wiki.exists()
    assert f"Wiki not found at {missing_wiki}" in capsys.readouterr().out
