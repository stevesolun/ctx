"""
test_skill_category.py -- Tests for category inference + backfill.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import skill_category as sc  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# infer_category
# ────────────────────────────────────────────────────────────────────


class TestInferCategory:
    def test_language_wins_over_pattern(self) -> None:
        # A skill tagged [python, pattern] should land in 'language'.
        assert sc.infer_category(["python", "pattern"]) == "language"

    def test_language_wins_over_framework(self) -> None:
        # [python, django] -> language (language precedes framework in taxonomy).
        assert sc.infer_category(["python", "django"]) == "language"

    def test_framework_match(self) -> None:
        assert sc.infer_category(["react", "state-management"]) == "framework"

    def test_tool_match(self) -> None:
        assert sc.infer_category(["docker", "ops"]) == "tool"

    def test_pattern_match(self) -> None:
        assert sc.infer_category(["refactoring", "design-patterns"]) == "pattern"

    def test_workflow_match(self) -> None:
        assert sc.infer_category(["rag", "embeddings"]) == "workflow"

    def test_meta_match(self) -> None:
        assert sc.infer_category(["skill-router", "taxonomy"]) == "meta"

    def test_no_match_returns_none(self) -> None:
        assert sc.infer_category(["zzz-unknown-tag"]) is None

    def test_empty_list_returns_none(self) -> None:
        assert sc.infer_category([]) is None

    def test_non_string_tags_ignored(self) -> None:
        assert sc.infer_category([None, 42, "python"]) == "language"  # type: ignore[list-item]

    def test_case_insensitive(self) -> None:
        assert sc.infer_category(["PYTHON"]) == "language"
        assert sc.infer_category(["React"]) == "framework"


# ────────────────────────────────────────────────────────────────────
# set_category + read_existing_category
# ────────────────────────────────────────────────────────────────────


class TestSetCategory:
    def test_appends_when_missing(self) -> None:
        raw = "---\nname: foo\ntags: [python]\n---\n\nBody.\n"
        new, changed = sc.set_category(raw, "language")
        assert changed is True
        assert "category: language" in new
        # Preserve existing keys.
        assert "name: foo" in new
        assert "tags: [python]" in new

    def test_fills_empty_value(self) -> None:
        raw = "---\nname: foo\ncategory:  \ntags: [python]\n---\n\nBody.\n"
        new, changed = sc.set_category(raw, "language")
        assert changed is True
        assert "category: language" in new
        # There should be exactly one category line after filling.
        assert new.count("category:") == 1

    def test_keeps_existing_non_empty_value(self) -> None:
        raw = "---\nname: foo\ncategory: meta\ntags: [python]\n---\n\nBody.\n"
        new, changed = sc.set_category(raw, "language")
        assert changed is False
        assert "category: meta" in new
        assert "category: language" not in new

    def test_no_frontmatter_returns_unchanged(self) -> None:
        raw = "Just a body with no frontmatter.\n"
        new, changed = sc.set_category(raw, "language")
        assert changed is False
        assert new == raw

    def test_invalid_category_raises(self) -> None:
        raw = "---\nname: foo\n---\n\nBody.\n"
        with pytest.raises(ValueError):
            sc.set_category(raw, "nonsense")

    def test_preserves_body_content(self) -> None:
        raw = "---\nname: foo\n---\n\n# Body\n\n- bullet 1\n- bullet 2\n"
        new, changed = sc.set_category(raw, "meta")
        assert changed is True
        assert "# Body" in new
        assert "- bullet 1" in new


class TestReadExistingCategory:
    def test_returns_value_when_present(self) -> None:
        raw = "---\nname: foo\ncategory: language\n---\n\nBody.\n"
        assert sc.read_existing_category(raw) == "language"

    def test_returns_none_when_empty(self) -> None:
        raw = "---\nname: foo\ncategory:   \n---\n\nBody.\n"
        assert sc.read_existing_category(raw) is None

    def test_returns_none_when_missing(self) -> None:
        raw = "---\nname: foo\n---\n\nBody.\n"
        assert sc.read_existing_category(raw) is None

    def test_returns_none_when_no_frontmatter(self) -> None:
        raw = "No frontmatter at all.\n"
        assert sc.read_existing_category(raw) is None


# ────────────────────────────────────────────────────────────────────
# backfill_file + backfill_corpus
# ────────────────────────────────────────────────────────────────────


def _make_skill(root: Path, slug: str, tags: list[str], category: str | None = None) -> Path:
    d = root / slug
    d.mkdir(parents=True)
    fm_lines = [f"name: {slug}"]
    if tags:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    if category is not None:
        fm_lines.append(f"category: {category}")
    body = "---\n" + "\n".join(fm_lines) + "\n---\n\n# " + slug + "\n"
    path = d / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def _make_agent(root: Path, slug: str, tags: list[str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{slug}.md"
    fm_lines = [f"name: {slug}"]
    if tags:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    body = "---\n" + "\n".join(fm_lines) + "\n---\n\n# " + slug + "\n"
    path.write_text(body, encoding="utf-8")
    return path


class TestBackfillFile:
    def test_fills_missing_category(self, tmp_path: Path) -> None:
        p = _make_skill(tmp_path, "py-testing", ["python", "testing"])
        result = sc.backfill_file(p, dry_run=False)
        assert result == "filled:language"
        raw = p.read_text(encoding="utf-8")
        assert "category: language" in raw

    def test_respects_existing_category(self, tmp_path: Path) -> None:
        p = _make_skill(
            tmp_path, "py-testing", ["python", "testing"], category="meta",
        )
        result = sc.backfill_file(p, dry_run=False)
        assert result == "already-set"
        assert "category: meta" in p.read_text(encoding="utf-8")

    def test_unresolved_on_no_match(self, tmp_path: Path) -> None:
        p = _make_skill(tmp_path, "mystery", ["zzz-nope"])
        result = sc.backfill_file(p, dry_run=False)
        assert result == "unresolved"
        # File must not be modified on unresolved.
        assert "category:" not in p.read_text(encoding="utf-8")

    def test_dry_run_does_not_write(self, tmp_path: Path) -> None:
        p = _make_skill(tmp_path, "py", ["python"])
        before = p.read_text(encoding="utf-8")
        result = sc.backfill_file(p, dry_run=True)
        assert result == "filled:language"
        assert p.read_text(encoding="utf-8") == before


class TestBackfillCorpus:
    def test_summary_shape(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        agents = tmp_path / "agents"
        _make_skill(skills, "py-testing", ["python", "testing"])
        _make_skill(skills, "react-state", ["react"])
        _make_skill(skills, "mystery", ["zzz-unknown"])
        _make_skill(skills, "pinned", ["python"], category="meta")
        _make_agent(agents, "devops", ["docker", "kubernetes"])

        summary = sc.backfill_corpus(
            skills_dir=skills, agents_dir=agents, dry_run=False,
        )
        c = summary["counts"]
        assert c["filled"] == 3         # py-testing, react-state, devops agent
        assert c["already-set"] == 1    # pinned
        assert c["unresolved"] == 1
        cat = summary["category_counts"]
        assert cat.get("language") == 1
        assert cat.get("framework") == 1
        assert cat.get("tool") == 1
        assert summary["total_files"] == 5

    def test_skips_underscore_dirs(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _make_skill(skills, "_demoted", ["python"])
        _make_skill(skills, "real", ["python"])
        summary = sc.backfill_corpus(
            skills_dir=skills, agents_dir=tmp_path / "agents", dry_run=False,
        )
        assert summary["total_files"] == 1


# ────────────────────────────────────────────────────────────────────
# CLI smoke
# ────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_infer_prints_category(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        rc = sc.main(["infer", "python,testing"])
        out = capsys.readouterr().out.strip()
        assert rc == 0
        assert out == "language"

    def test_infer_unresolved_returns_nonzero(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        rc = sc.main(["infer", "zzz-nope"])
        assert rc == 1
        assert "unresolved" in capsys.readouterr().out

    def test_backfill_cli_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        skills = tmp_path / "skills"
        agents = tmp_path / "agents"
        _make_skill(skills, "py", ["python"])

        class _FakeCfg:
            skills_dir = skills
            agents_dir = agents

        import ctx_config
        monkeypatch.setattr(ctx_config, "cfg", _FakeCfg(), raising=True)

        rc = sc.main(["backfill", "--dry-run", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["filled"] == 1
        assert payload["category_counts"]["language"] == 1
