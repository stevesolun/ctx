"""
test_loop_tools.py -- opt-in loop skill provisioning.

The helper recommends skills for a harness-loop goal and installs only
loadable local wiki rows. The MCP/toolbox surface is intentionally hidden by
default because it writes into the configured skills directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctx.adapters.claude_code.install import install_utils
from ctx.adapters.generic import loop_tools
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall


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
    manifest = tmp_path / "skill-manifest.json"
    monkeypatch.setattr(install_utils, "MANIFEST_PATH", manifest)
    return manifest


def _seed_skill(wiki_dir: Path, slug: str) -> None:
    converted = wiki_dir / "converted" / slug
    converted.mkdir(parents=True, exist_ok=True)
    (converted / "SKILL.md").write_text(
        f"---\nname: {slug}\nstatus: cataloged\n---\nbody\n",
        encoding="utf-8",
    )
    (wiki_dir / "entities" / "skills" / f"{slug}.md").write_text(
        f"---\nname: {slug}\nstatus: cataloged\n---\nbody\n",
        encoding="utf-8",
    )


def _local_row(slug: str) -> dict[str, str]:
    return {"name": slug, "skill_id": slug, "type": "skill", "status": "cataloged"}


def _external_row(slug: str, install_command: str) -> dict[str, str]:
    return {
        "name": slug,
        "skill_id": slug,
        "type": "skill",
        "install_command": install_command,
    }


def _fake_recommend(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, str]]) -> None:
    monkeypatch.setattr(
        loop_tools,
        "recommend_for_loop",
        lambda **kwargs: {"capabilities": {"skills": rows}},
    )


def test_provision_installs_and_returns_use_skills(
    wiki_dir: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_skill(wiki_dir, "stripe-webhooks")
    _seed_skill(wiki_dir, "idempotency")
    _fake_recommend(monkeypatch, [_local_row("stripe-webhooks"), _local_row("idempotency")])

    out = loop_tools.provision_skills(
        wiki_dir=wiki_dir,
        skills_dir=skills_dir,
        goal="harden stripe webhook",
    )

    assert set(out["use_skills"]) == {"stripe-webhooks", "idempotency"}
    assert set(out["installed"]) == {"stripe-webhooks", "idempotency"}
    assert out["skipped"] == []
    assert (skills_dir / "stripe-webhooks" / "SKILL.md").exists()
    assert (skills_dir / "idempotency" / "SKILL.md").exists()


def test_reprovision_reports_skipped(
    wiki_dir: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_skill(wiki_dir, "stripe-webhooks")
    _fake_recommend(monkeypatch, [_local_row("stripe-webhooks")])

    loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g")
    out = loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g")

    assert out["installed"] == []
    assert out["skipped"] == ["stripe-webhooks"]
    assert out["use_skills"] == ["stripe-webhooks"]


def test_topup_excludes_loaded(
    wiki_dir: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_skill(wiki_dir, "a")
    _seed_skill(wiki_dir, "b")
    _fake_recommend(monkeypatch, [_local_row("a"), _local_row("b")])

    out = loop_tools.provision_skills(
        wiki_dir=wiki_dir,
        skills_dir=skills_dir,
        goal="g",
        exclude=["a"],
    )

    assert out["use_skills"] == ["b"]
    assert not (skills_dir / "a").exists()
    assert (skills_dir / "b" / "SKILL.md").exists()


def test_dry_run_installs_nothing(
    wiki_dir: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_skill(wiki_dir, "a")
    _fake_recommend(monkeypatch, [_local_row("a")])

    out = loop_tools.provision_skills(
        wiki_dir=wiki_dir,
        skills_dir=skills_dir,
        goal="g",
        dry_run=True,
    )

    assert out["dry_run"] is True
    assert out["would_install"] == ["a"]
    assert out["installed"] == []
    assert out["use_skills"] == ["a"]
    assert not (skills_dir / "a").exists()


def test_missing_skill_goes_to_failed(
    wiki_dir: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_recommend(monkeypatch, [_local_row("ghost")])

    out = loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g")

    assert out["use_skills"] == []
    assert [failure["slug"] for failure in out["failed"]] == ["ghost"]


def test_external_row_goes_to_manual(
    wiki_dir: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_skill(wiki_dir, "local")
    _fake_recommend(monkeypatch, [_local_row("local"), _external_row("remote", "npx remote")])

    out = loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="g")

    assert out["use_skills"] == ["local"]
    assert [manual["name"] for manual in out["manual"]] == ["remote"]


def test_empty_goal_returns_empty(wiki_dir: Path, skills_dir: Path) -> None:
    out = loop_tools.provision_skills(wiki_dir=wiki_dir, skills_dir=skills_dir, goal="   ")

    assert out["use_skills"] == []
    assert "note" in out


def test_toolbox_hides_loop_write_tools_by_default() -> None:
    box = CtxCoreToolbox()
    names = {definition.name for definition in box.tool_definitions()}

    assert "ctx__loop_provision" not in names
    assert "ctx__loop_topup" not in names
    with pytest.raises(ValueError, match="not allowed"):
        box.dispatch(ToolCall(id="t", name="ctx__loop_provision", arguments={"goal": "g"}))


def test_toolbox_advertises_loop_tools_when_explicitly_allowed() -> None:
    allowed = {"ctx__loop_provision", "ctx__loop_topup"}
    names = {
        definition.name
        for definition in CtxCoreToolbox(allowed_tool_names=allowed).tool_definitions()
    }

    assert names == allowed


def test_toolbox_dispatches_allowed_dry_run_provision(
    wiki_dir: Path,
    skills_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctx_config

    _seed_skill(wiki_dir, "a")
    _fake_recommend(monkeypatch, [_local_row("a")])
    monkeypatch.setattr(ctx_config.cfg, "skills_dir", skills_dir)
    box = CtxCoreToolbox(
        wiki_dir=wiki_dir,
        allowed_tool_names={"ctx__loop_provision"},
    )

    payload = json.loads(
        box.dispatch(
            ToolCall(
                id="t",
                name="ctx__loop_provision",
                arguments={"goal": "g", "dry_run": True},
            )
        )
    )

    assert payload["would_install"] == ["a"]
    assert payload["installed"] == []
    assert not (skills_dir / "a").exists()
