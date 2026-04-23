"""
test_agent_install.py -- Coverage for agent_install (207 LOC).

Agents install as single files (no references dir). Otherwise structurally
similar to skill_install, so the edge matrix mirrors it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_install
import install_utils


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def wiki_dir(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    (root / "entities" / "agents").mkdir(parents=True)
    (root / "converted-agents").mkdir(parents=True)
    return root


@pytest.fixture()
def agents_dir(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    root.mkdir()
    return root


@pytest.fixture()
def isolated_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    manifest = tmp_path / "skill-manifest.json"
    monkeypatch.setattr(install_utils, "MANIFEST_PATH", manifest)
    return manifest


def _seed_agent(wiki_dir: Path, slug: str, *, status: str = "cataloged") -> None:
    (wiki_dir / "converted-agents" / f"{slug}.md").write_text(
        f"agent body for {slug}\n", encoding="utf-8"
    )
    (wiki_dir / "entities" / "agents" / f"{slug}.md").write_text(
        f"---\nname: {slug}\nstatus: {status}\n---\nbody\n",
        encoding="utf-8",
    )


# ── install_agent ────────────────────────────────────────────────────────────


class TestInstallAgent:
    def test_invalid_slug(self, wiki_dir: Path, agents_dir: Path) -> None:
        r = agent_install.install_agent(
            "../evil", wiki_dir=wiki_dir, agents_dir=agents_dir,
        )
        assert r.status == "failed"

    def test_not_in_wiki(self, wiki_dir: Path, agents_dir: Path) -> None:
        r = agent_install.install_agent(
            "ghost", wiki_dir=wiki_dir, agents_dir=agents_dir,
        )
        assert r.status == "not-in-wiki"
        assert "ctx-agent-mirror" in r.message

    def test_happy_path(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_agent(wiki_dir, "architect")
        r = agent_install.install_agent(
            "architect", wiki_dir=wiki_dir, agents_dir=agents_dir,
        )
        assert r.status == "installed"
        assert (agents_dir / "architect.md").read_text(encoding="utf-8").startswith("agent body")
        m = install_utils.load_manifest()
        assert any(
            e["skill"] == "architect" and e["entity_type"] == "agent"
            for e in m["load"]
        )
        entity = wiki_dir / "entities" / "agents" / "architect.md"
        assert "status: installed" in entity.read_text(encoding="utf-8")

    def test_dry_run(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_agent(wiki_dir, "a")
        r = agent_install.install_agent(
            "a", wiki_dir=wiki_dir, agents_dir=agents_dir, dry_run=True,
        )
        assert r.status == "installed"
        assert not (agents_dir / "a.md").exists()
        assert install_utils.load_manifest()["load"] == []

    def test_skipped_existing_reconciles(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_agent(wiki_dir, "a")
        (agents_dir / "a.md").write_text("old\n", encoding="utf-8")
        r = agent_install.install_agent(
            "a", wiki_dir=wiki_dir, agents_dir=agents_dir,
        )
        assert r.status == "skipped-existing"
        # Manifest reconciled.
        assert any(
            e["skill"] == "a" and e["entity_type"] == "agent"
            for e in install_utils.load_manifest()["load"]
        )

    def test_skipped_existing_dry_run_preserves_manifest(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_agent(wiki_dir, "a")
        (agents_dir / "a.md").write_text("old\n", encoding="utf-8")
        r = agent_install.install_agent(
            "a", wiki_dir=wiki_dir, agents_dir=agents_dir, dry_run=True,
        )
        assert r.status == "skipped-existing"
        assert install_utils.load_manifest()["load"] == []

    def test_force_overwrites(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
    ) -> None:
        _seed_agent(wiki_dir, "a")
        (agents_dir / "a.md").write_text("old\n", encoding="utf-8")
        r = agent_install.install_agent(
            "a", wiki_dir=wiki_dir, agents_dir=agents_dir, force=True,
        )
        assert r.status == "installed"
        content = (agents_dir / "a.md").read_text(encoding="utf-8")
        assert content.startswith("agent body")


# ── _split_slugs ─────────────────────────────────────────────────────────────


class TestSplitSlugs:
    def _ns(self, **kwargs: object) -> object:
        import argparse as _a
        ns = _a.Namespace()
        defaults = {"slug": None, "slugs": None, "slugs_positional": []}
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(ns, k, v)
        return ns

    def test_all_three_sources(self) -> None:
        out = agent_install._split_slugs(
            self._ns(slug="a", slugs="b,c", slugs_positional=["d"])
        )
        assert out == ["a", "b", "c", "d"]

    def test_trims_comma_empties(self) -> None:
        assert agent_install._split_slugs(
            self._ns(slugs="x, ,y ,"),
        ) == ["x", "y"]


# ── main / CLI ───────────────────────────────────────────────────────────────


class TestMain:
    def test_no_args_exit_2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.argv", ["ctx-agent-install"])
        with pytest.raises(SystemExit) as ei:
            agent_install.main()
        assert ei.value.code == 2

    def test_happy(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_agent(wiki_dir, "a")
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-agent-install", "--slug", "a",
             "--wiki-dir", str(wiki_dir),
             "--agents-dir", str(agents_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            agent_install.main()
        assert ei.value.code == 0
        assert "[OK]" in capsys.readouterr().out

    def test_json_output(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_agent(wiki_dir, "a")
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-agent-install", "--slug", "a",
             "--wiki-dir", str(wiki_dir),
             "--agents-dir", str(agents_dir),
             "--json"],
        )
        with pytest.raises(SystemExit):
            agent_install.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["slug"] == "a"
        assert payload[0]["status"] == "installed"

    def test_multi_slug_dedup(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_agent(wiki_dir, "a")
        _seed_agent(wiki_dir, "b")
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-agent-install", "--slug", "a", "--slugs", "a,b",
             "--wiki-dir", str(wiki_dir),
             "--agents-dir", str(agents_dir),
             "--json"],
        )
        with pytest.raises(SystemExit):
            agent_install.main()
        payload = json.loads(capsys.readouterr().out)
        assert [r["slug"] for r in payload] == ["a", "b"]

    def test_not_in_wiki_exit_1(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-agent-install", "ghost",
             "--wiki-dir", str(wiki_dir),
             "--agents-dir", str(agents_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            agent_install.main()
        assert ei.value.code == 1

    def test_skipped_existing_exit_0(
        self,
        wiki_dir: Path,
        agents_dir: Path,
        isolated_manifest: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_agent(wiki_dir, "a")
        (agents_dir / "a.md").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(
            "sys.argv",
            ["ctx-agent-install", "--slug", "a",
             "--wiki-dir", str(wiki_dir),
             "--agents-dir", str(agents_dir)],
        )
        with pytest.raises(SystemExit) as ei:
            agent_install.main()
        assert ei.value.code == 0
