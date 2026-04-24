"""
test_skill_mirror.py -- pins the short-skill mirror contract.

The mirror exists because ctx-skill-install reads
``<wiki>/converted/<slug>/SKILL.md`` and short skills (< line_threshold
lines) skip the batch_convert pipeline that would create that file.
Without the mirror, 835 of 1,791 skills are un-installable from the
shipped tarball.

Covers:
  - short skill gets mirrored (creates converted/<slug>/SKILL.md)
  - long skill is skipped (line_threshold guard)
  - existing converted/<slug>/ dir is not overwritten without --force
  - unchanged files skipped (idempotent re-run)
  - --force overrides both guards
  - invalid slugs rejected via validate_skill_name
  - missing local source returns not-found
  - prune removes short-skill mirrors whose local source vanished
  - prune LEAVES long-skill pipeline dirs alone
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import skill_mirror as _sm


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Return (skills_dir, wiki_dir) set up as empty roots."""
    skills = tmp_path / "skills"
    wiki = tmp_path / "wiki"
    skills.mkdir()
    wiki.mkdir()
    return skills, wiki


def _write_skill(skills: Path, slug: str, lines: int, content: str | None = None) -> Path:
    """Create ~/.claude/skills/<slug>/SKILL.md with *lines* lines of body."""
    body = content if content is not None else "\n".join(f"line {i}" for i in range(lines))
    if content is None and not body.endswith("\n"):
        body += "\n"
    path = skills / slug / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ────────────────────────────────────────────────────────────────────
# mirror_one
# ────────────────────────────────────────────────────────────────────


class TestMirrorOne:
    def test_short_skill_gets_mirrored(self, dirs):
        skills, wiki = dirs
        _write_skill(skills, "short-skill", lines=30)

        r = _sm.mirror_one(
            "short-skill", skills_dir=skills, wiki_dir=wiki,
            line_threshold=180,
        )

        assert r.status == "mirrored"
        assert (wiki / "converted" / "short-skill" / "SKILL.md").is_file()
        assert r.body_lines == 30

    def test_long_skill_skipped(self, dirs):
        """Skills above line_threshold belong in the batch_convert
        pipeline, not the mirror. Refusing here prevents a short-mirror
        call from bypassing the pipeline's post-processing."""
        skills, wiki = dirs
        _write_skill(skills, "long-skill", lines=250)

        r = _sm.mirror_one(
            "long-skill", skills_dir=skills, wiki_dir=wiki,
            line_threshold=180,
        )

        assert r.status == "skipped-too-long"
        assert not (wiki / "converted" / "long-skill").exists()

    def test_existing_pipeline_dir_not_overwritten(self, dirs):
        """An existing converted/<slug>/ with SKILL.md + references/
        is a pipeline artifact. Mirror must NOT overwrite it."""
        skills, wiki = dirs
        _write_skill(skills, "had-pipeline", lines=30)
        pipeline_dir = wiki / "converted" / "had-pipeline"
        pipeline_dir.mkdir(parents=True)
        pipeline_file = pipeline_dir / "SKILL.md"
        pipeline_file.write_text("PIPELINE-CONVERTED BODY\n", encoding="utf-8")
        (pipeline_dir / "references").mkdir()

        r = _sm.mirror_one(
            "had-pipeline", skills_dir=skills, wiki_dir=wiki,
            line_threshold=180,
        )

        assert r.status == "skipped-existing-pipeline"
        assert pipeline_file.read_text(encoding="utf-8") == "PIPELINE-CONVERTED BODY\n"

    def test_force_overwrites_existing(self, dirs):
        """--force re-syncs after a local edit."""
        skills, wiki = dirs
        _write_skill(skills, "edited", lines=30, content="FRESH LOCAL BODY\n")
        stale = wiki / "converted" / "edited" / "SKILL.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("STALE WIKI BODY\n", encoding="utf-8")

        r = _sm.mirror_one(
            "edited", skills_dir=skills, wiki_dir=wiki,
            line_threshold=180, force=True,
        )

        assert r.status == "mirrored"
        assert stale.read_text(encoding="utf-8") == "FRESH LOCAL BODY\n"

    def test_unchanged_is_idempotent(self, dirs):
        """Second run on already-mirrored content reports `unchanged`,
        not a rewrite."""
        skills, wiki = dirs
        _write_skill(skills, "same", lines=30, content="BODY\n")
        # Pre-create the mirror with identical content. Because dest_dir
        # already exists AND content matches, path returns `unchanged`.
        mirror_path = wiki / "converted" / "same" / "SKILL.md"
        mirror_path.parent.mkdir(parents=True)
        mirror_path.write_text("BODY\n", encoding="utf-8")

        r = _sm.mirror_one(
            "same", skills_dir=skills, wiki_dir=wiki, line_threshold=180,
        )
        assert r.status == "unchanged"

    def test_invalid_slug_rejected(self, dirs):
        skills, wiki = dirs
        r = _sm.mirror_one(
            "../etc/passwd", skills_dir=skills, wiki_dir=wiki,
            line_threshold=180,
        )
        assert r.status == "skipped-invalid"

    def test_missing_local_returns_not_found(self, dirs):
        skills, wiki = dirs
        r = _sm.mirror_one(
            "does-not-exist", skills_dir=skills, wiki_dir=wiki,
            line_threshold=180,
        )
        assert r.status == "not-found"

    def test_dry_run_does_not_write(self, dirs):
        skills, wiki = dirs
        _write_skill(skills, "dry", lines=30)

        r = _sm.mirror_one(
            "dry", skills_dir=skills, wiki_dir=wiki,
            line_threshold=180, dry_run=True,
        )

        assert r.status == "mirrored"
        assert "dry-run" in r.message
        assert not (wiki / "converted" / "dry").exists()


# ────────────────────────────────────────────────────────────────────
# mirror_all
# ────────────────────────────────────────────────────────────────────


class TestMirrorAll:
    def test_mixed_corpus(self, dirs):
        """Bulk run: short mirrors, long skips, existing-pipeline skips."""
        skills, wiki = dirs
        _write_skill(skills, "short-a", lines=20)
        _write_skill(skills, "short-b", lines=50)
        _write_skill(skills, "long", lines=300)
        _write_skill(skills, "had-pipeline", lines=30)
        # Pre-existing pipeline for had-pipeline:
        (wiki / "converted" / "had-pipeline").mkdir(parents=True)
        (wiki / "converted" / "had-pipeline" / "SKILL.md").write_text("X\n", encoding="utf-8")
        (wiki / "converted" / "had-pipeline" / "references").mkdir()

        results = _sm.mirror_all(
            skills_dir=skills, wiki_dir=wiki, line_threshold=180,
        )

        by_status = {r.slug: r.status for r in results}
        assert by_status["short-a"] == "mirrored"
        assert by_status["short-b"] == "mirrored"
        assert by_status["long"] == "skipped-too-long"
        assert by_status["had-pipeline"] == "skipped-existing-pipeline"

    def test_sort_is_stable(self, dirs):
        """Order is deterministic across runs so log diffs are readable."""
        skills, wiki = dirs
        for slug in ("b-skill", "a-skill", "c-skill"):
            _write_skill(skills, slug, lines=10)

        results = _sm.mirror_all(
            skills_dir=skills, wiki_dir=wiki, line_threshold=180,
        )
        assert [r.slug for r in results] == ["a-skill", "b-skill", "c-skill"]


# ────────────────────────────────────────────────────────────────────
# prune_orphans
# ────────────────────────────────────────────────────────────────────


class TestPruneOrphans:
    def test_prune_short_mirror_when_local_gone(self, dirs):
        """Short mirror = converted/<slug>/ containing only SKILL.md.
        If local source vanishes, prune drops the mirror."""
        skills, wiki = dirs
        mirror_dir = wiki / "converted" / "vanished"
        mirror_dir.mkdir(parents=True)
        (mirror_dir / "SKILL.md").write_text("orphan\n", encoding="utf-8")

        results = _sm.prune_orphans(
            skills_dir=skills, wiki_dir=wiki,
        )

        assert any(r.slug == "vanished" and r.status == "pruned" for r in results)
        assert not mirror_dir.exists()

    def test_prune_leaves_pipeline_dirs_alone(self, dirs):
        """Long-skill pipeline dirs (SKILL.md + references/ + siblings)
        must not be pruned — they hold converted content the local
        source no longer needs to generate from."""
        skills, wiki = dirs
        pipeline_dir = wiki / "converted" / "pipelined"
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "SKILL.md").write_text("body\n", encoding="utf-8")
        (pipeline_dir / "references").mkdir()
        (pipeline_dir / "check-gates.md").write_text("gates\n", encoding="utf-8")
        # No local source for "pipelined" — but it has siblings, so keep it.

        results = _sm.prune_orphans(skills_dir=skills, wiki_dir=wiki)

        assert not any(r.slug == "pipelined" for r in results)
        assert pipeline_dir.exists()

    def test_prune_skips_present_sources(self, dirs):
        """Mirror whose local source STILL exists is not pruned."""
        skills, wiki = dirs
        _write_skill(skills, "still-here", lines=20)
        mirror_dir = wiki / "converted" / "still-here"
        mirror_dir.mkdir(parents=True)
        (mirror_dir / "SKILL.md").write_text("body\n", encoding="utf-8")

        results = _sm.prune_orphans(skills_dir=skills, wiki_dir=wiki)

        assert not any(r.slug == "still-here" for r in results)
        assert mirror_dir.exists()

    def test_prune_dry_run_reports_but_keeps(self, dirs):
        skills, wiki = dirs
        mirror_dir = wiki / "converted" / "to-prune"
        mirror_dir.mkdir(parents=True)
        (mirror_dir / "SKILL.md").write_text("x\n", encoding="utf-8")

        results = _sm.prune_orphans(
            skills_dir=skills, wiki_dir=wiki, dry_run=True,
        )

        assert any(r.status == "pruned" and "dry-run" in r.message for r in results)
        assert mirror_dir.exists()


# ────────────────────────────────────────────────────────────────────
# Integration with skill_install
# ────────────────────────────────────────────────────────────────────


class TestIntegrationWithInstall:
    def test_installed_after_mirror(self, dirs, tmp_path):
        """End-to-end: a short local skill has no wiki content -> install
        would fail with not-in-wiki -> run mirror -> install now works.

        This is the exact regression the mirror exists to fix.
        """
        from ctx.adapters.claude_code.install.skill_install import install_skill

        skills, wiki = dirs
        _write_skill(
            skills, "short-e2e", lines=10,
            content="# short-e2e\n\nBody for a short skill.\n",
        )
        # Before mirror: install fails.
        install_target = tmp_path / "install-target"
        before = install_skill(
            "short-e2e", wiki_dir=wiki, skills_dir=install_target,
        )
        assert before.status == "not-in-wiki"

        # Run the mirror.
        r = _sm.mirror_one(
            "short-e2e", skills_dir=skills, wiki_dir=wiki,
            line_threshold=180,
        )
        assert r.status == "mirrored"

        # Install now succeeds with the mirrored body.
        after = install_skill(
            "short-e2e", wiki_dir=wiki, skills_dir=install_target,
        )
        assert after.status == "installed"
        assert (install_target / "short-e2e" / "SKILL.md").is_file()
        # Content matches the local body verbatim.
        assert "Body for a short skill." in (
            install_target / "short-e2e" / "SKILL.md"
        ).read_text(encoding="utf-8")
