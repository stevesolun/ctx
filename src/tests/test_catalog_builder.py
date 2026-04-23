"""
test_catalog_builder.py -- Coverage for catalog_builder.py (226 LOC).

catalog_builder scans skills and agents directories, builds catalog.md,
and optionally updates index.md and log.md inside the wiki. A regression
in any of the four public functions silently corrupts the master index
used by every downstream router call, so each branch is explicitly covered.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import catalog_builder
import ctx_config


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_skill(base: Path, name: str, line_count: int) -> Path:
    """Create skills_dir/<name>/SKILL.md with the requested number of lines."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"# line {i}" for i in range(line_count))
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return d


def _make_agent(base: Path, name: str, line_count: int) -> Path:
    """Create agents_dir/<name>.md with the requested number of lines."""
    base.mkdir(parents=True, exist_ok=True)
    content = "\n".join(f"# line {i}" for i in range(line_count))
    p = base / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


def _patched_cfg(monkeypatch: pytest.MonkeyPatch, threshold: int = 180) -> MagicMock:
    """Return a mock cfg with a configurable line_threshold."""
    fake = MagicMock()
    fake.line_threshold = threshold
    monkeypatch.setattr(catalog_builder, "cfg", fake)
    return fake


# ── scan_skills_dir ───────────────────────────────────────────────────────────


class TestScanSkillsDir:
    def test_nonexistent_dir_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch)
        result = catalog_builder.scan_skills_dir(tmp_path / "does-not-exist")
        assert result == []

    def test_empty_dir_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        assert catalog_builder.scan_skills_dir(skills_dir) == []

    def test_dir_without_skill_md_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch)
        skills_dir = tmp_path / "skills"
        (skills_dir / "no-skill-md").mkdir(parents=True)
        # directory exists but has no SKILL.md
        assert catalog_builder.scan_skills_dir(skills_dir) == []

    def test_flat_file_in_skills_dir_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regular .md files at the top level of skills_dir are not skills."""
        _patched_cfg(monkeypatch)
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "flat.md").write_text("# flat", encoding="utf-8")
        assert catalog_builder.scan_skills_dir(skills_dir) == []

    def test_single_skill_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "my-skill", 10)
        result = catalog_builder.scan_skills_dir(skills_dir)
        assert len(result) == 1
        r = result[0]
        assert r["name"] == "my-skill"
        assert r["type"] == "skill"
        assert r["lines"] == 10
        assert r["over_180"] is False
        assert "SKILL.md" in r["path"]

    def test_over_threshold_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "fat-skill", 200)
        result = catalog_builder.scan_skills_dir(skills_dir)
        assert result[0]["over_180"] is True

    def test_exactly_at_threshold_not_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "boundary-skill", 180)
        result = catalog_builder.scan_skills_dir(skills_dir)
        assert result[0]["over_180"] is False

    def test_one_above_threshold_is_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "just-over", 181)
        result = catalog_builder.scan_skills_dir(skills_dir)
        assert result[0]["over_180"] is True

    def test_multiple_skills_sorted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "zebra", 5)
        _make_skill(skills_dir, "alpha", 5)
        _make_skill(skills_dir, "middle", 5)
        result = catalog_builder.scan_skills_dir(skills_dir)
        names = [r["name"] for r in result]
        assert names == sorted(names)

    def test_empty_skill_md_yields_zero_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        d = skills_dir / "empty-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("", encoding="utf-8")
        result = catalog_builder.scan_skills_dir(skills_dir)
        assert result[0]["lines"] == 0

    def test_unicode_content_handled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        d = skills_dir / "unicode-skill"
        d.mkdir(parents=True)
        content = "# 日本語\n# Ärger\n# emoji 🐍\n"
        (d / "SKILL.md").write_text(content, encoding="utf-8")
        result = catalog_builder.scan_skills_dir(skills_dir)
        assert result[0]["lines"] == 3

    def test_unreadable_skill_warns_and_uses_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When read_text raises, lines falls back to 0 and a warning goes to stderr."""
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        d = skills_dir / "bad-skill"
        d.mkdir(parents=True)
        skill_md = d / "SKILL.md"
        skill_md.write_text("content", encoding="utf-8")

        original_read = Path.read_text

        def _boom(self: Path, **kwargs: Any) -> str:  # type: ignore[override]
            if self.name == "SKILL.md":
                raise OSError("permission denied")
            return original_read(self, **kwargs)

        monkeypatch.setattr(Path, "read_text", _boom)
        result = catalog_builder.scan_skills_dir(skills_dir)
        assert result[0]["lines"] == 0
        err = capsys.readouterr().err
        assert "Warning" in err


# ── scan_agents_dir ───────────────────────────────────────────────────────────


class TestScanAgentsDir:
    def test_nonexistent_dir_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch)
        result = catalog_builder.scan_agents_dir(tmp_path / "no-agents")
        assert result == []

    def test_empty_dir_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        assert catalog_builder.scan_agents_dir(agents_dir) == []

    def test_non_md_files_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "script.py").write_text("x=1", encoding="utf-8")
        (agents_dir / "data.json").write_text("{}", encoding="utf-8")
        assert catalog_builder.scan_agents_dir(agents_dir) == []

    def test_single_agent_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "my-agent", 50)
        result = catalog_builder.scan_agents_dir(agents_dir)
        assert len(result) == 1
        r = result[0]
        assert r["name"] == "my-agent"
        assert r["type"] == "agent"
        assert r["lines"] == 50
        assert r["over_180"] is False
        assert r["path"].endswith("my-agent.md")

    def test_over_threshold_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "fat-agent", 200)
        result = catalog_builder.scan_agents_dir(agents_dir)
        assert result[0]["over_180"] is True

    def test_exactly_at_threshold_not_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "boundary", 180)
        result = catalog_builder.scan_agents_dir(agents_dir)
        assert result[0]["over_180"] is False

    def test_stem_used_as_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent name should be the stem (filename without .md extension)."""
        _patched_cfg(monkeypatch, threshold=180)
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "code-reviewer", 10)
        result = catalog_builder.scan_agents_dir(agents_dir)
        assert result[0]["name"] == "code-reviewer"

    def test_multiple_agents_sorted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        agents_dir = tmp_path / "agents"
        for name in ("zzz", "aaa", "mmm"):
            _make_agent(agents_dir, name, 5)
        result = catalog_builder.scan_agents_dir(agents_dir)
        names = [r["name"] for r in result]
        assert names == sorted(names)

    def test_unreadable_agent_warns_and_uses_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "broken-agent", 10)

        original_read = Path.read_text

        def _boom(self: Path, **kwargs: Any) -> str:  # type: ignore[override]
            if self.suffix == ".md":
                raise OSError("permission denied")
            return original_read(self, **kwargs)

        monkeypatch.setattr(Path, "read_text", _boom)
        result = catalog_builder.scan_agents_dir(agents_dir)
        assert result[0]["lines"] == 0
        err = capsys.readouterr().err
        assert "Warning" in err

    def test_unicode_agent_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        content = "# αβγδ\n# 中文\n# emoji 🤖\n"
        (agents_dir / "unicode-agent.md").write_text(content, encoding="utf-8")
        result = catalog_builder.scan_agents_dir(agents_dir)
        assert result[0]["lines"] == 3


# ── build_catalog ─────────────────────────────────────────────────────────────


class TestBuildCatalog:
    def test_empty_dirs_catalog_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        agents_dir = tmp_path / "agents"

        stats = catalog_builder.build_catalog(wiki_dir, skills_dir, agents_dir, [])

        catalog_path = wiki_dir / "catalog.md"
        assert catalog_path.exists()
        assert stats["total"] == 0
        assert stats["skills"] == 0
        assert stats["agents"] == 0
        assert stats["over_180"] == 0
        assert stats["catalog_path"] == str(catalog_path)

    def test_catalog_md_header_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        catalog_builder.build_catalog(wiki_dir, tmp_path / "s", tmp_path / "a", [])

        content = (wiki_dir / "catalog.md").read_text(encoding="utf-8")
        assert "# Skill Catalog" in content
        assert "## Summary" in content
        assert "## All Skills" in content

    def test_stats_counts_skills_and_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        agents_dir = tmp_path / "agents"
        _make_skill(skills_dir, "skill-a", 10)
        _make_skill(skills_dir, "skill-b", 10)
        _make_agent(agents_dir, "agent-x", 5)

        stats = catalog_builder.build_catalog(wiki_dir, skills_dir, agents_dir, [])
        assert stats["total"] == 3
        assert stats["skills"] == 2
        assert stats["agents"] == 1

    def test_over_180_count_correct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        agents_dir = tmp_path / "agents"
        _make_skill(skills_dir, "short-skill", 10)
        _make_skill(skills_dir, "long-skill", 200)
        _make_agent(agents_dir, "fat-agent", 250)

        stats = catalog_builder.build_catalog(wiki_dir, skills_dir, agents_dir, [])
        assert stats["over_180"] == 2

    def test_catalog_table_rows_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        agents_dir = tmp_path / "agents"
        _make_skill(skills_dir, "my-skill", 10)
        _make_agent(agents_dir, "my-agent", 5)

        catalog_builder.build_catalog(wiki_dir, skills_dir, agents_dir, [])
        content = (wiki_dir / "catalog.md").read_text(encoding="utf-8")
        assert "my-skill" in content
        assert "my-agent" in content

    def test_over_180_flag_in_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning character should appear for over-threshold items."""
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "fat-skill", 200)

        catalog_builder.build_catalog(wiki_dir, skills_dir, tmp_path / "a", [])
        content = (wiki_dir / "catalog.md").read_text(encoding="utf-8")
        # The warning flag character should be present somewhere in the row
        assert "⚠" in content  # ⚠

    def test_under_threshold_no_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "short", 10)

        catalog_builder.build_catalog(wiki_dir, skills_dir, tmp_path / "a", [])
        content = (wiki_dir / "catalog.md").read_text(encoding="utf-8")
        assert "⚠" not in content

    def test_extra_dirs_skills_pattern(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extra dir with SKILL.md subdirs should be treated as skills."""
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        extra = tmp_path / "extra-skills"
        _make_skill(extra, "extra-skill", 10)

        stats = catalog_builder.build_catalog(
            wiki_dir, tmp_path / "s", tmp_path / "a", [extra]
        )
        assert stats["total"] == 1
        assert stats["skills"] == 1

    def test_extra_dirs_agents_pattern(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Extra dir with flat .md files (no SKILL.md subdirs) treated as agents."""
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        extra = tmp_path / "extra-agents"
        _make_agent(extra, "extra-agent", 10)

        stats = catalog_builder.build_catalog(
            wiki_dir, tmp_path / "s", tmp_path / "a", [extra]
        )
        assert stats["total"] == 1
        assert stats["agents"] == 1

    def test_extra_dir_nonexistent_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        stats = catalog_builder.build_catalog(
            wiki_dir, tmp_path / "s", tmp_path / "a",
            [tmp_path / "ghost-dir"]
        )
        assert stats["total"] == 0

    def test_catalog_overwrites_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        catalog_path = wiki_dir / "catalog.md"
        catalog_path.write_text("OLD CONTENT", encoding="utf-8")

        catalog_builder.build_catalog(wiki_dir, tmp_path / "s", tmp_path / "a", [])
        content = catalog_path.read_text(encoding="utf-8")
        assert "OLD CONTENT" not in content
        assert "# Skill Catalog" in content

    def test_summary_table_total_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "sk", 10)

        catalog_builder.build_catalog(wiki_dir, skills_dir, tmp_path / "a", [])
        content = (wiki_dir / "catalog.md").read_text(encoding="utf-8")
        assert "Total items" in content
        assert "| 1 |" in content

    def test_catalog_path_in_stats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        stats = catalog_builder.build_catalog(wiki_dir, tmp_path / "s", tmp_path / "a", [])
        assert stats["catalog_path"] == str(wiki_dir / "catalog.md")

    def test_many_items_all_appear_in_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        agents_dir = tmp_path / "agents"
        for i in range(5):
            _make_skill(skills_dir, f"skill-{i}", 10 + i)
        for i in range(3):
            _make_agent(agents_dir, f"agent-{i}", 5 + i)

        stats = catalog_builder.build_catalog(wiki_dir, skills_dir, agents_dir, [])
        assert stats["total"] == 8
        content = (wiki_dir / "catalog.md").read_text(encoding="utf-8")
        for i in range(5):
            assert f"skill-{i}" in content
        for i in range(3):
            assert f"agent-{i}" in content


# ── update_wiki_index ─────────────────────────────────────────────────────────


class TestUpdateWikiIndex:
    def _stats(self, total: int = 5) -> dict:
        return {
            "total": total,
            "skills": 3,
            "agents": 2,
            "over_180": 1,
            "catalog_path": "/tmp/catalog.md",
        }

    def test_no_index_md_is_noop(
        self, tmp_path: Path
    ) -> None:
        """If index.md doesn't exist, function returns without error."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        # no index.md created — should be a silent no-op
        catalog_builder.update_wiki_index(wiki_dir, self._stats())
        assert not (wiki_dir / "index.md").exists()

    def test_catalog_ref_inserted_once(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        index_path = wiki_dir / "index.md"
        index_path.write_text("# Index\n\n## Skills\n\nSome content\n", encoding="utf-8")

        catalog_builder.update_wiki_index(wiki_dir, self._stats())
        content = index_path.read_text(encoding="utf-8")
        assert content.count("[[catalog]]") == 1

    def test_catalog_ref_not_duplicated_on_second_call(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        index_path = wiki_dir / "index.md"
        index_path.write_text("# Index\n\n## Skills\n\n", encoding="utf-8")

        catalog_builder.update_wiki_index(wiki_dir, self._stats())
        catalog_builder.update_wiki_index(wiki_dir, self._stats())
        content = index_path.read_text(encoding="utf-8")
        assert content.count("[[catalog]]") == 1

    def test_catalog_ref_inserted_under_skills_section(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        index_path = wiki_dir / "index.md"
        index_path.write_text("# Index\n\n## Skills\n\n## Other\n", encoding="utf-8")

        catalog_builder.update_wiki_index(wiki_dir, self._stats())
        content = index_path.read_text(encoding="utf-8")
        lines = content.split("\n")
        skills_idx = next(i for i, l in enumerate(lines) if l.strip() == "## Skills")
        catalog_idx = next(i for i, l in enumerate(lines) if "[[catalog]]" in l)
        assert catalog_idx == skills_idx + 1

    def test_catalog_ref_appended_when_no_skills_section(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        index_path = wiki_dir / "index.md"
        index_path.write_text("# Index\n\nNo skills section here.\n", encoding="utf-8")

        catalog_builder.update_wiki_index(wiki_dir, self._stats())
        content = index_path.read_text(encoding="utf-8")
        assert "[[catalog]]" in content

    def test_total_pages_updated(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        index_path = wiki_dir / "index.md"
        index_path.write_text(
            "# Index\n\nTotal pages: 0\n\nLast updated: 2020-01-01\n",
            encoding="utf-8",
        )

        catalog_builder.update_wiki_index(wiki_dir, self._stats(total=42))
        content = index_path.read_text(encoding="utf-8")
        assert "Total pages: 42" in content
        assert "Total pages: 0" not in content

    def test_last_updated_replaced(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        index_path = wiki_dir / "index.md"
        index_path.write_text(
            "# Index\n\nLast updated: 1999-12-31\n",
            encoding="utf-8",
        )

        catalog_builder.update_wiki_index(wiki_dir, self._stats())
        content = index_path.read_text(encoding="utf-8")
        assert "Last updated: 1999-12-31" not in content
        # The new date should match YYYY-MM-DD pattern
        assert re.search(r"Last updated: \d{4}-\d{2}-\d{2}", content)

    def test_index_with_existing_catalog_ref_updates_counts(
        self, tmp_path: Path
    ) -> None:
        """Already-present [[catalog]] should not be duplicated; total should update."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        index_path = wiki_dir / "index.md"
        index_path.write_text(
            "# Index\n\n[[catalog]]\n\nTotal pages: 1\n",
            encoding="utf-8",
        )

        catalog_builder.update_wiki_index(wiki_dir, self._stats(total=99))
        content = index_path.read_text(encoding="utf-8")
        assert content.count("[[catalog]]") == 1
        assert "Total pages: 99" in content

    def test_empty_index_md(
        self, tmp_path: Path
    ) -> None:
        """Empty index.md should not raise; catalog ref is appended."""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        index_path = wiki_dir / "index.md"
        index_path.write_text("", encoding="utf-8")

        catalog_builder.update_wiki_index(wiki_dir, self._stats())
        content = index_path.read_text(encoding="utf-8")
        assert "[[catalog]]" in content


# ── append_log ────────────────────────────────────────────────────────────────


class TestAppendLog:
    def _stats(self) -> dict:
        return {
            "total": 7,
            "skills": 4,
            "agents": 3,
            "over_180": 2,
            "catalog_path": "/tmp/wiki/catalog.md",
        }

    def test_no_log_md_is_noop(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        # log.md does not exist — must not raise
        catalog_builder.append_log(wiki_dir, self._stats())
        assert not (wiki_dir / "log.md").exists()

    def test_entry_appended_to_existing_log(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        log_path = wiki_dir / "log.md"
        log_path.write_text("# Log\n\nOld entry.\n", encoding="utf-8")

        catalog_builder.append_log(wiki_dir, self._stats())
        content = log_path.read_text(encoding="utf-8")
        assert "Old entry." in content
        assert "catalog-build" in content

    def test_log_contains_counts(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        log_path = wiki_dir / "log.md"
        log_path.write_text("", encoding="utf-8")

        catalog_builder.append_log(wiki_dir, self._stats())
        content = log_path.read_text(encoding="utf-8")
        assert "7" in content   # total
        assert "4" in content   # skills
        assert "3" in content   # agents
        assert "2" in content   # over_180

    def test_log_contains_catalog_path(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "log.md").write_text("", encoding="utf-8")

        catalog_builder.append_log(wiki_dir, self._stats())
        content = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert "/tmp/wiki/catalog.md" in content

    def test_log_contains_date(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "log.md").write_text("", encoding="utf-8")

        catalog_builder.append_log(wiki_dir, self._stats())
        content = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert re.search(r"\d{4}-\d{2}-\d{2}", content)

    def test_multiple_appends_accumulate(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        log_path = wiki_dir / "log.md"
        log_path.write_text("", encoding="utf-8")

        catalog_builder.append_log(wiki_dir, self._stats())
        catalog_builder.append_log(wiki_dir, self._stats())
        content = log_path.read_text(encoding="utf-8")
        assert content.count("catalog-build") == 2

    def test_empty_log_md_gets_entry(
        self, tmp_path: Path
    ) -> None:
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        (wiki_dir / "log.md").write_text("", encoding="utf-8")

        catalog_builder.append_log(wiki_dir, self._stats())
        content = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert len(content) > 0


# ── Parametric edge-case coverage ────────────────────────────────────────────


class TestScanSkillsDirParametric:
    @pytest.mark.parametrize("line_count", [0, 1, 179, 180, 181, 500])
    def test_over_threshold_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line_count: int
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        skills_dir = tmp_path / "skills"
        _make_skill(skills_dir, "skill", line_count)
        result = catalog_builder.scan_skills_dir(skills_dir)
        assert result[0]["over_180"] == (line_count > 180)


class TestScanAgentsDirParametric:
    @pytest.mark.parametrize("line_count", [0, 1, 179, 180, 181, 500])
    def test_over_threshold_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line_count: int
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        agents_dir = tmp_path / "agents"
        _make_agent(agents_dir, "agent", line_count)
        result = catalog_builder.scan_agents_dir(agents_dir)
        assert result[0]["over_180"] == (line_count > 180)


class TestBuildCatalogParametric:
    @pytest.mark.parametrize(
        "n_skills,n_agents,expected_total",
        [
            (0, 0, 0),
            (1, 0, 1),
            (0, 1, 1),
            (3, 3, 6),
            (10, 5, 15),
        ],
    )
    def test_total_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        n_skills: int,
        n_agents: int,
        expected_total: int,
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        agents_dir = tmp_path / "agents"
        for i in range(n_skills):
            _make_skill(skills_dir, f"skill-{i}", 10)
        for i in range(n_agents):
            _make_agent(agents_dir, f"agent-{i}", 10)
        stats = catalog_builder.build_catalog(wiki_dir, skills_dir, agents_dir, [])
        assert stats["total"] == expected_total


# ── main() CLI entrypoint ────────────────────────────────────────────────────


class TestMain:
    """Exercise the argparse entrypoint without touching real ~/.claude paths."""

    def test_missing_wiki_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki-missing"
        # wiki_dir intentionally not created
        monkeypatch.setattr(
            "sys.argv",
            [
                "catalog_builder",
                "--wiki", str(wiki_dir),
                "--skills-dir", str(tmp_path / "skills"),
                "--agents-dir", str(tmp_path / "agents"),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            catalog_builder.main()
        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "Wiki not initialized" in err

    def test_happy_path_prints_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        skills_dir = tmp_path / "skills"
        agents_dir = tmp_path / "agents"
        _make_skill(skills_dir, "demo-skill", 10)
        _make_agent(agents_dir, "demo-agent", 5)

        monkeypatch.setattr(
            "sys.argv",
            [
                "catalog_builder",
                "--wiki", str(wiki_dir),
                "--skills-dir", str(skills_dir),
                "--agents-dir", str(agents_dir),
            ],
        )
        catalog_builder.main()
        out = capsys.readouterr().out
        assert "Catalog built:" in out
        assert "Written to:" in out

    def test_extra_dirs_passed_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        extra = tmp_path / "extra"
        _make_skill(extra, "extra-skill", 10)

        monkeypatch.setattr(
            "sys.argv",
            [
                "catalog_builder",
                "--wiki", str(wiki_dir),
                "--skills-dir", str(tmp_path / "skills"),
                "--agents-dir", str(tmp_path / "agents"),
                "--extra-dirs", str(extra),
            ],
        )
        catalog_builder.main()
        # catalog.md should exist and reference the extra skill
        content = (wiki_dir / "catalog.md").read_text(encoding="utf-8")
        assert "extra-skill" in content

    def test_catalog_and_log_written_by_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patched_cfg(monkeypatch, threshold=180)
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        # Pre-create log.md and index.md so update_wiki_index/append_log have targets
        (wiki_dir / "log.md").write_text("", encoding="utf-8")
        (wiki_dir / "index.md").write_text("# Index\n\nTotal pages: 0\n", encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv",
            [
                "catalog_builder",
                "--wiki", str(wiki_dir),
                "--skills-dir", str(tmp_path / "skills"),
                "--agents-dir", str(tmp_path / "agents"),
            ],
        )
        catalog_builder.main()
        assert (wiki_dir / "catalog.md").exists()
        log_content = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert "catalog-build" in log_content
