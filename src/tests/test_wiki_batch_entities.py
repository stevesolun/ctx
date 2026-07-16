"""Behavior tests for batch-generating skill and agent wiki pages."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

import wiki_batch_entities as batch


@dataclass(frozen=True)
class EntityCase:
    name: str
    page: Path
    entity_type: str
    generate: Callable[[bool], int]


@pytest.fixture()
def entity_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, EntityCase]:
    skills_dir = tmp_path / "installed-skills"
    agents_dir = tmp_path / "installed-agents"
    wiki_dir = tmp_path / "wiki"
    skill_entities = wiki_dir / "entities" / "skills"
    agent_entities = wiki_dir / "entities" / "agents"

    skill_name = "demo-skill"
    skill_source = skills_dir / skill_name / "SKILL.md"
    skill_source.parent.mkdir(parents=True)
    skill_source.write_text(
        "---\ndescription: Demo skill.\n---\n\n# Demo Skill\n",
        encoding="utf-8",
    )

    agent_name = "demo-agent"
    agents_dir.mkdir(parents=True)
    (agents_dir / f"{agent_name}.md").write_text(
        "---\ndescription: Demo agent.\nmodel: sonnet\n---\n\n# Demo Agent\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(batch, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(batch, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(batch, "WIKI_DIR", wiki_dir)
    monkeypatch.setattr(batch, "SKILL_ENTITIES", skill_entities)
    monkeypatch.setattr(batch, "AGENT_ENTITIES", agent_entities)

    return {
        "skills": EntityCase(
            name=skill_name,
            page=skill_entities / f"{skill_name}.md",
            entity_type="skill",
            generate=batch.generate_missing_skills,
        ),
        "agents": EntityCase(
            name=agent_name,
            page=agent_entities / f"{agent_name}.md",
            entity_type="agent",
            generate=batch.generate_missing_agents,
        ),
    }


@pytest.mark.parametrize("kind", ["skills", "agents"])
def test_dry_run_reports_without_writing(
    kind: str,
    entity_cases: dict[str, EntityCase],
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = entity_cases[kind]
    assert not case.page.parent.exists()

    assert case.generate(True) == 0

    assert not case.page.parent.exists()
    assert not case.page.exists()
    assert f"[DRY RUN] Would create: {case.name}.md" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["skills", "agents"])
def test_apply_creates_missing_page(
    kind: str,
    entity_cases: dict[str, EntityCase],
) -> None:
    case = entity_cases[kind]

    assert case.generate(False) == 1

    content = case.page.read_text(encoding="utf-8")
    assert f"type: {case.entity_type}" in content
    assert f"# {case.name}" in content


@pytest.mark.parametrize("kind", ["skills", "agents"])
def test_apply_uses_safe_atomic_writer(
    kind: str,
    entity_cases: dict[str, EntityCase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = entity_cases[kind]
    writes: list[tuple[Path, str, str]] = []

    def record_write(path: Path, text: str, encoding: str = "utf-8") -> None:
        writes.append((path, text, encoding))

    monkeypatch.setattr(batch, "safe_atomic_write_text", record_write)

    assert case.generate(False) == 1
    assert len(writes) == 1
    path, content, encoding = writes[0]
    assert path == case.page
    assert f"type: {case.entity_type}" in content
    assert encoding == "utf-8"


@pytest.mark.parametrize("kind", ["skills", "agents"])
def test_existing_page_is_skipped(
    kind: str,
    entity_cases: dict[str, EntityCase],
) -> None:
    case = entity_cases[kind]
    original = "# Existing page\n\nPreserve this content.\n"
    case.page.parent.mkdir(parents=True)
    case.page.write_text(original, encoding="utf-8")

    assert case.generate(False) == 0
    assert case.page.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("kind", ["skills", "agents"])
def test_rerun_is_idempotent(
    kind: str,
    entity_cases: dict[str, EntityCase],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = entity_cases[kind]
    assert case.generate(False) == 1
    original = case.page.read_bytes()

    def unexpected_write(*args: object, **kwargs: object) -> None:
        pytest.fail("rerun attempted to rewrite an existing entity page")

    monkeypatch.setattr(batch, "safe_atomic_write_text", unexpected_write)

    assert case.generate(False) == 0
    assert case.page.read_bytes() == original


def test_cli_all_generates_skills_and_agents(
    entity_cases: dict[str, EntityCase],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["wiki_batch_entities.py", "--all"])

    batch.main()

    assert entity_cases["skills"].page.exists()
    assert entity_cases["agents"].page.exists()
    assert "Total: 2 entity pages generated" in capsys.readouterr().out


def test_cli_without_mode_exits_one(
    entity_cases: dict[str, EntityCase],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["wiki_batch_entities.py"])

    with pytest.raises(SystemExit) as exit_info:
        batch.main()

    assert exit_info.value.code == 1
    assert "usage:" in capsys.readouterr().out
    assert not entity_cases["skills"].page.parent.exists()
    assert not entity_cases["agents"].page.parent.exists()
