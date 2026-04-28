"""Tests for agent_add existing-update review behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import agent_add  # noqa: E402


class _Decision:
    allow = True
    warnings: tuple[Any, ...] = ()


def _agent_text(
    *,
    description: str = "Agent that reviews code changes with clear findings.",
    model: str = "inherit",
    body: str | None = None,
) -> str:
    body = body or (
        "This agent reviews code and reports concrete risks.\n\n"
        "## Review Process\n\n"
        "Read the diff, identify regressions, and return prioritized findings."
    )
    return "\n".join(
        [
            "---",
            "name: reviewer-agent",
            f"description: {description}",
            f"model: {model}",
            "---",
            "# reviewer-agent",
            "",
            body,
        ]
    )


def _setup_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    wiki = tmp_path / "wiki"
    agents_dir = tmp_path / "agents"
    source = tmp_path / "reviewer-agent.md"
    (wiki / "entities" / "agents").mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    return wiki, agents_dir, source


def _patch_side_effects(monkeypatch: Any) -> MagicMock:
    check = MagicMock(return_value=_Decision())
    monkeypatch.setattr(agent_add, "check_intake", check)
    monkeypatch.setattr(agent_add, "record_embedding", MagicMock())
    monkeypatch.setattr(agent_add, "update_index", MagicMock())
    monkeypatch.setattr(agent_add, "append_log", MagicMock())
    monkeypatch.setattr(agent_add, "ensure_wiki", MagicMock())
    return check


def test_existing_agent_review_skips_without_mutating_files(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki, agents_dir, source = _setup_paths(tmp_path)
    installed = agents_dir / "reviewer-agent.md"
    existing_text = _agent_text(
        description="Detailed agent with a conservative review process.",
        model="sonnet",
    )
    installed.write_text(existing_text, encoding="utf-8")
    entity = wiki / "entities" / "agents" / "reviewer-agent.md"
    entity.write_text("# existing entity\n", encoding="utf-8")
    entity_text = entity.read_text(encoding="utf-8")
    source.write_text(
        _agent_text(description="Short agent.", model="haiku"),
        encoding="utf-8",
    )
    check = _patch_side_effects(monkeypatch)

    result = agent_add.add_agent(
        source_path=source,
        name="reviewer-agent",
        wiki_path=wiki,
        agents_dir=agents_dir,
        review_existing=True,
    )

    assert result["skipped"] is True
    assert result["update_required"] is True
    assert "Existing agent already exists: reviewer-agent" in result["update_review"]
    assert installed.read_text(encoding="utf-8") == existing_text
    assert entity.read_text(encoding="utf-8") == entity_text
    check.assert_not_called()


def test_existing_agent_update_existing_applies_change(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki, agents_dir, source = _setup_paths(tmp_path)
    installed = agents_dir / "reviewer-agent.md"
    installed.write_text(_agent_text(), encoding="utf-8")
    entity = wiki / "entities" / "agents" / "reviewer-agent.md"
    entity.write_text("# existing entity\n", encoding="utf-8")
    updated_text = _agent_text(
        description="Updated agent with stronger review coverage.",
        model="opus",
    )
    source.write_text(updated_text, encoding="utf-8")
    _patch_side_effects(monkeypatch)

    result = agent_add.add_agent(
        source_path=source,
        name="reviewer-agent",
        wiki_path=wiki,
        agents_dir=agents_dir,
        review_existing=True,
        update_existing=True,
    )

    assert result["skipped"] is False
    assert result["update_required"] is False
    assert result["is_new_page"] is False
    assert installed.read_text(encoding="utf-8") == updated_text
    assert "Updated agent with stronger review coverage" in entity.read_text(
        encoding="utf-8"
    )


def test_main_existing_agent_prints_update_review(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    wiki, agents_dir, source = _setup_paths(tmp_path)
    installed = agents_dir / "reviewer-agent.md"
    installed.write_text(_agent_text(), encoding="utf-8")
    source.write_text(
        _agent_text(description="Replacement agent."),
        encoding="utf-8",
    )
    _patch_side_effects(monkeypatch)
    monkeypatch.setattr(sys, "argv", [
        "agent_add.py",
        "--agent-path", str(source),
        "--name", "reviewer-agent",
        "--wiki", str(wiki),
        "--agents-dir", str(agents_dir),
    ])

    agent_add.main()

    out = capsys.readouterr().out
    assert "Existing agent already exists: reviewer-agent" in out
    assert "Use the explicit update flag" in out
    assert "Replacement agent." not in installed.read_text(encoding="utf-8")
