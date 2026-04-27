"""
test_skill_install.py -- Coverage for skill_install (342 LOC).

Skills install from ``<wiki>/converted/<slug>/SKILL.md`` (or .original
fallback) into ``~/.claude/skills/<slug>/``. Tests cover the source
selection logic, multi-slug dedup, references mirror, and the CLI.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from ctx.adapters.claude_code.install import agent_install
from ctx.adapters.claude_code.install import install_utils
from ctx.adapters.claude_code.install import skill_install
# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def wiki_dir(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    (root / "entities" / "skills").mkdir(parents=True)
    (root / "converted").mkdir(parents=True)
    return root


@pytest.fixture()
def skills_dir(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    return root


@pytest.fixture()
def isolated_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    manifest = tmp_path / "skill-manifest.json"
    monkeypatch.setattr(install_utils, "MANIFEST_PATH", manifest)
    return manifest


def _seed_skill(
    wiki_dir: Path,
    slug: str,
    *,
    with_transformed: bool = True,
    with_original: bool = False,
    refs: list[str] | None = None,
) -> Path:
    d = wiki_dir / "converted" / slug
    d.mkdir(parents=True, exist_ok=True)
    if with_transformed:
        (d / "SKILL.md").write_text(
            f"---\nname: {slug}\nstatus: cataloged\n---\nbody\n",
            encoding="utf-8",
        )
    if with_original:
        (d / "SKILL.md.original").write_text(
            "original body\n", encoding="utf-8"
        )
    if refs:
        r = d / "references"
        r.mkdir(parents=True, exist_ok=True)
        for name in refs:
            (r / f"{name}.md").write_text(f"ref {name}\n", encoding="utf-8")
    # Entity card for status bumps.
    (wiki_dir / "entities" / "skills" / f"{slug}.md").write_text(
        f"---\nname: {slug}\nstatus: cataloged\n---\nbody\n",
        encoding="utf-8",
    )
    return d


def _symlink_to(target: Path, link: Path, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")


# ── _pick_source ─────────────────────────────────────────────────────────────


class TestPickSource:
    def test_prefer_transformed_finds_transformed(
        self, wiki_dir: Path
    ) -> None:
        d = _seed_skill(wiki_dir, "s", with_transformed=True, with_original=True)
        path, variant = skill_install._pick_source(d, "transformed")
        assert variant == "transformed"
        assert path is not None and path.name == "SKILL.md"

    def test_prefer_original_finds_original(self, wiki_dir: Path) -> None:
        d = _seed_skill(wiki_dir, "s", with_transformed=True, with_original=True)
        path, variant = skill_install._pick_source(d, "original")
        assert variant == "original"
        assert path is not None and path.name == "SKILL.md.original"

    def test_prefer_original_falls_back_to_transformed(
        self, wiki_dir: Path
    ) -> None:
        d = _seed_skill(wiki_dir, "s", with_transformed=True, with_original=False)
        path, variant = skill_install._pick_source(d, "original")
        assert variant == "transformed"

    def test_prefer_transformed_falls_back_to_original(
        self, wiki_dir: Path
    ) -> None:
        d = _seed_skill(wiki_dir, "s", with_transformed=False, with_original=True)
        path, variant = skill_install._pick_source(d, "transformed")
        assert variant == "original"

    def test_neither_present(self, wiki_dir: Path) -> None:
        d = wiki_dir / "converted" / "bare"
        d.mkdir(parents=True)
        assert skill_install._pick_source(d, "transformed") == (None, None)


# ── _copy_references ─────────────────────────────────────────────────────────


class TestCopyReferences:
    def test_no_references_dir(self, wiki_dir: Path, tmp_path: Path) -> None:
        d = wiki_dir / "converted" / "s"
        d.mkdir(parents=True)
        assert skill_install._copy_references(d, tmp_path / "out") == 0

    def test_copies_multiple_md_files(
        self, wiki_dir: Path, tmp_path: Path
    ) -> None:
        src = _seed_skill(
            wiki_dir, "s", refs=["one", "two", "three"],
        )
        dest = tmp_path / "dest"
        n = skill_install._copy_references(src, dest)
        assert n == 3
        out = dest / "references"
        assert (out / "one.md").read_text(encoding="utf-8") == "ref one\n"
        assert sorted(p.name for p in out.glob("*.md")) == [
            "one.md", "three.md", "two.md"
        ]

    def test_skips_non_md_files(
        self, wiki_dir: Path, tmp_path: Path
    ) -> None:
        d = _seed_skill(wiki_dir, "s", refs=["foo"])
        (d / "references" / "ignore.txt").write_text("nope", encoding="utf-8")
        dest = tmp_path / "dest"
        n = skill_install._copy_references(d, dest)
        assert n == 1


# ── install_skill ────────────────────────────────────────────────────────────


class TestInstallSkill:
    def test_invalid_slug(self, wiki_dir: Path, skills_dir: Path) -> None:
        r = skill_install.install_skill(
            "../evil", wiki_dir=wiki_dir, skills_dir=skills_dir,
        )
        assert r.status == "failed"
        assert "invalid slug" in r.message

    def test_not_in_wiki_missing_converted(
        self, wiki_dir: Path, skills_dir: Path
    ) -> None:
        r = skill_install.install_skill(
            "ghost", wiki_dir=wiki_dir, skills_dir=skills_dir,
        )
        assert r.status == "not-in-wiki"

    def test_not_in_wiki_empty_converted(
        self, wiki_dir: Path, skills_dir: Path
    ) -> None:
        (wiki_dir / "converted" / "shell").mkdir()
        r = skill_install.install_skill(
            "shell", wiki_dir=wiki_dir, skills_dir=skills_dir,
        )
        assert r.status == "not-in-wiki"
        assert "no SKILL.md" in r.message

    def test_happy_path_with_references(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_skill(wiki_dir, "s", refs=["a", "b"])
        r = skill_install.install_skill(
            "s", wiki_dir=wiki_dir, skills_dir=skills_dir,
        )
        assert r.status == "installed"
        assert r.references_copied == 2
        assert (skills_dir / "s" / "SKILL.md").is_file()
        assert (skills_dir / "s" / "references" / "a.md").is_file()
        # Manifest entry tagged skill.
        m = install_utils.load_manifest()
        assert any(
            e["skill"] == "s" and e["entity_type"] == "skill" for e in m["load"]
        )
        # Entity status flipped.
        entity = wiki_dir / "entities" / "skills" / "s.md"
        assert "status: installed" in entity.read_text(encoding="utf-8")

    def test_dry_run_skips_writes(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_skill(wiki_dir, "s", refs=["a"])
        r = skill_install.install_skill(
            "s", wiki_dir=wiki_dir, skills_dir=skills_dir, dry_run=True,
        )
        assert r.status == "installed"
        assert r.references_copied == 1
        assert not (skills_dir / "s").exists()
        # Manifest untouched.
        assert install_utils.load_manifest()["load"] == []

    def test_skipped_existing_reconciles_manifest(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        """Existing install without manifest entry — rerun reconciles it."""
        _seed_skill(wiki_dir, "s")
        # Pre-create the dest so we hit the skipped-existing path.
        (skills_dir / "s").mkdir()
        (skills_dir / "s" / "SKILL.md").write_text("existing\n", encoding="utf-8")
        r = skill_install.install_skill(
            "s", wiki_dir=wiki_dir, skills_dir=skills_dir,
        )
        assert r.status == "skipped-existing"
        # Manifest reconciled even though the copy was skipped.
        assert any(
            e["skill"] == "s" and e["entity_type"] == "skill"
            for e in install_utils.load_manifest()["load"]
        )

    def test_skipped_existing_dry_run_leaves_manifest_alone(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_skill(wiki_dir, "s")
        (skills_dir / "s").mkdir()
        (skills_dir / "s" / "SKILL.md").write_text("x\n", encoding="utf-8")
        r = skill_install.install_skill(
            "s", wiki_dir=wiki_dir, skills_dir=skills_dir, dry_run=True,
        )
        assert r.status == "skipped-existing"
        assert install_utils.load_manifest()["load"] == []

    def test_force_overwrites(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_skill(wiki_dir, "s")
        (skills_dir / "s").mkdir()
        (skills_dir / "s" / "SKILL.md").write_text("old\n", encoding="utf-8")
        r = skill_install.install_skill(
            "s", wiki_dir=wiki_dir, skills_dir=skills_dir, force=True,
        )
        assert r.status == "installed"
        content = (skills_dir / "s" / "SKILL.md").read_text(encoding="utf-8")
        assert "body" in content
        assert content != "old\n"

    def test_rejects_symlinked_skill_destination_parent(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
        tmp_path: Path,
    ) -> None:
        _seed_skill(wiki_dir, "s")
        outside = tmp_path / "outside"
        outside.mkdir()
        _symlink_to(outside, skills_dir / "s", target_is_directory=True)
        r = skill_install.install_skill(
            "s", wiki_dir=wiki_dir, skills_dir=skills_dir, force=True,
        )
        assert r.status == "failed"
        assert "symlinked destination parent" in r.message
        assert not (outside / "SKILL.md").exists()

    def test_rejects_symlinked_agent_destination_file(
        self, tmp_path: Path, isolated_manifest: Path,
    ) -> None:
        wiki = tmp_path / "agent-wiki"
        agents_dir = tmp_path / "agents"
        outside = tmp_path / "outside.md"
        (wiki / "converted-agents").mkdir(parents=True)
        (wiki / "entities" / "agents").mkdir(parents=True)
        agents_dir.mkdir()
        outside.write_text("outside\n", encoding="utf-8")
        (wiki / "converted-agents" / "architect.md").write_text(
            "---\nname: architect\nstatus: cataloged\n---\nbody\n",
            encoding="utf-8",
        )
        (wiki / "entities" / "agents" / "architect.md").write_text(
            "---\nname: architect\nstatus: cataloged\n---\nbody\n",
            encoding="utf-8",
        )
        _symlink_to(outside, agents_dir / "architect.md", target_is_directory=False)
        r = agent_install.install_agent(
            "architect", wiki_dir=wiki, agents_dir=agents_dir, force=True,
        )
        assert r.status == "failed"
        assert "symlinked destination file" in r.message
        assert outside.read_text(encoding="utf-8") == "outside\n"

    def test_prefer_original_only_source(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_skill(wiki_dir, "s", with_transformed=False, with_original=True)
        r = skill_install.install_skill(
            "s", wiki_dir=wiki_dir, skills_dir=skills_dir, prefer="original",
        )
        assert r.status == "installed"
        assert r.source_variant == "original"
        content = (skills_dir / "s" / "SKILL.md").read_text(encoding="utf-8")
        assert "original body" in content


# ── _split_slugs ─────────────────────────────────────────────────────────────


class TestSplitSlugs:
    def _ns(self, **kwargs: object) -> argparse.Namespace:
        ns = argparse.Namespace()
        defaults: dict[str, object] = {
            "slug": None,
            "slugs": None,
            "slugs_positional": [],
        }
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(ns, k, v)
        return ns

    def test_single_slug(self) -> None:
        out = skill_install._split_slugs(self._ns(slug="a"))
        assert out == ["a"]

    def test_comma_separated(self) -> None:
        out = skill_install._split_slugs(self._ns(slugs="a,b,c"))
        assert out == ["a", "b", "c"]

    def test_comma_strips_whitespace_and_empties(self) -> None:
        out = skill_install._split_slugs(self._ns(slugs=" a , , b ,"))
        assert out == ["a", "b"]

    def test_positional(self) -> None:
        out = skill_install._split_slugs(self._ns(slugs_positional=["x", "y"]))
        assert out == ["x", "y"]

    def test_all_three_sources_combined(self) -> None:
        out = skill_install._split_slugs(
            self._ns(slug="a", slugs="b,c", slugs_positional=["d"])
        )
        assert out == ["a", "b", "c", "d"]

    def test_empty(self) -> None:
        assert skill_install._split_slugs(self._ns()) == []


# ── main / CLI ───────────────────────────────────────────────────────────────


class TestMain:
    def test_no_slugs_exits_2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["ctx-skill-install"])
        with pytest.raises(SystemExit) as ei:
            skill_install.main()
        assert ei.value.code == 2

    def test_happy_path_exit_0(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_skill(wiki_dir, "s")
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-skill-install", "--slug", "s",
             "--wiki-dir", str(wiki_dir),
             "--skills-dir", str(skills_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            skill_install.main()
        assert ei.value.code == 0
        assert "[OK]" in capsys.readouterr().out

    def test_not_in_wiki_exit_1(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-skill-install", "ghost",
             "--wiki-dir", str(wiki_dir),
             "--skills-dir", str(skills_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            skill_install.main()
        assert ei.value.code == 1

    def test_dedup_across_sources(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--slug s --slugs 's,t' should install s once + t."""
        _seed_skill(wiki_dir, "s")
        _seed_skill(wiki_dir, "t")
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-skill-install", "--slug", "s", "--slugs", "s,t",
             "--wiki-dir", str(wiki_dir),
             "--skills-dir", str(skills_dir),
             "--json"],
        )
        with pytest.raises(SystemExit):
            skill_install.main()
        payload = json.loads(capsys.readouterr().out)
        slugs = [r["slug"] for r in payload]
        assert slugs == ["s", "t"]  # dedup preserved order, s not duplicated

    def test_json_output_shape(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_skill(wiki_dir, "s", refs=["r1"])
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-skill-install", "--slug", "s",
             "--wiki-dir", str(wiki_dir),
             "--skills-dir", str(skills_dir),
             "--json"],
        )
        with pytest.raises(SystemExit):
            skill_install.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["slug"] == "s"
        assert payload[0]["status"] == "installed"
        assert payload[0]["references_copied"] == 1
        assert payload[0]["source_variant"] == "transformed"

    def test_skipped_existing_exit_0(
        self,
        wiki_dir: Path,
        skills_dir: Path,
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Idempotent rerun should exit 0, not 1."""
        _seed_skill(wiki_dir, "s")
        (skills_dir / "s").mkdir()
        (skills_dir / "s" / "SKILL.md").write_text("existing\n", encoding="utf-8")
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-skill-install", "--slug", "s",
             "--wiki-dir", str(wiki_dir),
             "--skills-dir", str(skills_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            skill_install.main()
        assert ei.value.code == 0
