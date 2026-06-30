"""
test_loop_tools.py -- Coverage for loop_provision / loop_topup.

The new tools recommend skills for a harness-loop goal (via the existing
``recommend_for_loop``) and install them through the real ``install_skill``
path so the names resolve in ~/.claude/skills. ``recommend_for_loop`` (the
graph-scoring half) is monkeypatched to keep tests hermetic; the install half
runs for real against a temp wiki + skills dir, so these tests prove the
orchestration AND that slugs actually land on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctx.adapters.generic import loop_tools
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.claude_code.install import install_utils


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


@pytest.fixture(autouse=True)
def isolated_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep install bookkeeping off the real ~/.claude/skill-manifest.json."""
    manifest = tmp_path / "skill-manifest.json"
    monkeypatch.setattr(install_utils, "MANIFEST_PATH", manifest)
    return manifest


def _seed_skill(wiki_dir: Path, slug: str) -> None:
    """Create an installable skill in the temp wiki (matches skill_install's layout)."""
    d = wiki_dir / "converted" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug}\nstatus: cataloged\n---\nbody\n", encoding="utf-8"
    )
    (wiki_dir / "entities" / "skills" / f"{slug}.md").write_text(
        f"---\nname: {slug}\nstatus: cataloged\n---\nbody\n", encoding="utf-8"
    )


def _local_row(slug: str) -> dict:
    """A skill row that _is_loadable_skill_row() accepts (local wiki, installable)."""
    return {"name": slug, "skill_id": slug, "type": "skill", "status": "cataloged"}


def _external_row(slug: str, install_command: str) -> dict:
    """A skill row that is recommended but installed from an external catalog."""
    return {"name": slug, "skill_id": slug, "type": "skill", "install_command": install_command}


def _fake_recommend(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    """Patch recommend_for_loop to return a fixed skill bundle."""
    monkeypatch.setattr(
        loop_tools,
        "recommend_for_loop",
        lambda **kw: {"capabilities": {"skills": rows}},
    )


# ── provision_skills ─────────────────────────────────────────────────────────


def test_provision_installs_and_returns_use_skills(wiki_dir, skills_dir, monkeypatch):
    _seed_skill(wiki_dir, "stripe-webhooks")
    _seed_skill(wiki_dir, "idempotency")
    _fake_recommend(monkeypatch, [_local_row("stripe-webhooks"), _local_row("idempotency")])

    out = loop_tools.provision_skills(
        wiki_dir=wiki_dir, skills_dir=skills_dir, goal="harden stripe webhook"
    )

    assert set(out["use_skills"]) == {"stripe-webhooks", "idempotency"}
    assert set(out["installed"]) == {"stripe-webhooks", "idempotency"}
    assert out["skipped"] == []
    assert (skills_dir / "stripe-webhooks" / "SKILL.md").exists()
    assert (skills_dir / "idempotency" / "SKILL.md").exists()


def test_reprovision_reports_skipped(wiki_dir, skills_dir, monkeypatch):
    _seed_skill(wiki_dir, "stripe-webhooks")
    _fake_recommend(monkeypatch, [_local_row("stripe-webhooks")])

    loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g")
    out = loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g")

    assert out["installed"] == []
    assert out["skipped"] == ["stripe-webhooks"]
    assert out["use_skills"] == ["stripe-webhooks"]  # still resolvable


def test_topup_excludes_loaded(wiki_dir, skills_dir, monkeypatch):
    _seed_skill(wiki_dir, "a")
    _seed_skill(wiki_dir, "b")
    _fake_recommend(monkeypatch, [_local_row("a"), _local_row("b")])

    out = loop_tools.provision_skills(
        wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g", exclude=["a"]
    )

    assert out["use_skills"] == ["b"]
    assert not (skills_dir / "a").exists()
    assert (skills_dir / "b" / "SKILL.md").exists()


def test_dry_run_installs_nothing(wiki_dir, skills_dir, monkeypatch):
    _seed_skill(wiki_dir, "a")
    _fake_recommend(monkeypatch, [_local_row("a")])

    out = loop_tools.provision_skills(
        wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g", dry_run=True
    )

    assert out["dry_run"] is True
    assert out["would_install"] == ["a"]
    assert out["installed"] == []
    assert out["use_skills"] == ["a"]  # would resolve
    assert not (skills_dir / "a").exists()  # nothing written


def test_missing_skill_goes_to_failed(wiki_dir, skills_dir, monkeypatch):
    # Loadable row, but the slug isn't seeded into the wiki → install can't find it.
    _fake_recommend(monkeypatch, [_local_row("ghost")])

    out = loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g")

    assert out["use_skills"] == []
    assert [f["slug"] for f in out["failed"]] == ["ghost"]


def test_external_row_goes_to_manual(wiki_dir, skills_dir, monkeypatch):
    _seed_skill(wiki_dir, "local")
    _fake_recommend(monkeypatch, [_local_row("local"), _external_row("remote", "npx remote")])

    out = loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g")

    assert out["use_skills"] == ["local"]  # external one not installed locally
    assert [m["name"] for m in out["manual"]] == ["remote"]


def test_empty_goal_returns_empty(wiki_dir, skills_dir):
    out = loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="   ")

    assert out["use_skills"] == []
    assert "note" in out


# ── toolbox registration (no graph needed) ───────────────────────────────────


def test_toolbox_advertises_loop_tools():
    names = {td.name for td in CtxCoreToolbox().tool_definitions()}
    assert "ctx__loop_provision" in names
    assert "ctx__loop_topup" in names


def test_toolbox_owns_loop_tools():
    box = CtxCoreToolbox()
    assert box.owns("ctx__loop_provision")
    assert box.owns("ctx__loop_topup")
