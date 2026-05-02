"""Regression tests for symlink-safe entity writes."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ctx.utils._fs_utils import safe_atomic_write_text  # noqa: E402
from mcp_entity import McpRecord  # noqa: E402


def _fresh_module(name: str) -> Any:
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def _symlink_to(target: Path, link: Path, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")


def _allow_intake(*args: Any, **kwargs: Any) -> Any:
    from intake_gate import IntakeDecision

    return IntakeDecision(allow=True)


def test_safe_atomic_write_text_rejects_symlinked_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    link = tmp_path / "target.md"
    _symlink_to(outside, link, target_is_directory=False)

    with pytest.raises(ValueError, match="symlinked path"):
        safe_atomic_write_text(link, "owned\n")

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_skill_and_agent_entity_writers_reject_symlinked_targets(
    tmp_path: Path,
) -> None:
    skill_add = _fresh_module("skill_add")
    agent_add = _fresh_module("agent_add")
    try:
        wiki = tmp_path / "wiki"
        skill_dir = wiki / "entities" / "skills"
        agent_dir = wiki / "entities" / "agents"
        skill_dir.mkdir(parents=True)
        agent_dir.mkdir(parents=True)
        outside_skill = tmp_path / "outside-skill.md"
        outside_agent = tmp_path / "outside-agent.md"
        outside_skill.write_text("skill\n", encoding="utf-8")
        outside_agent.write_text("agent\n", encoding="utf-8")
        _symlink_to(outside_skill, skill_dir / "react.md", target_is_directory=False)
        _symlink_to(outside_agent, agent_dir / "reviewer.md", target_is_directory=False)

        with pytest.raises(ValueError, match="symlinked path"):
            skill_add.write_entity_page(wiki, "react", "# new\n")
        with pytest.raises(ValueError, match="symlinked path"):
            agent_add.write_entity_page(wiki, "reviewer", "# new\n")

        assert outside_skill.read_text(encoding="utf-8") == "skill\n"
        assert outside_agent.read_text(encoding="utf-8") == "agent\n"
    finally:
        sys.modules.pop("skill_add", None)
        sys.modules.pop("agent_add", None)


def test_mcp_add_rejects_symlinked_entity_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_add = _fresh_module("mcp_add")
    try:
        wiki = tmp_path / "wiki"
        mcp_root = wiki / "entities" / "mcp-servers"
        mcp_root.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        _symlink_to(outside, mcp_root / "g", target_is_directory=True)

        monkeypatch.setattr(mcp_add, "check_intake", _allow_intake)
        monkeypatch.setattr(mcp_add, "record_embedding", lambda **kwargs: None)
        monkeypatch.setattr(mcp_add, "update_index", lambda *args, **kwargs: None)
        monkeypatch.setattr(mcp_add, "append_log", lambda *args, **kwargs: None)
        record = McpRecord.from_dict({
            "name": "github-mcp",
            "description": "A GitHub MCP server",
            "sources": ["test"],
            "github_url": "https://github.com/org/github-mcp",
            "tags": ["github"],
            "transports": ["stdio"],
        })

        with pytest.raises(ValueError, match="symlinked path"):
            mcp_add.add_mcp(record=record, wiki_path=wiki)

        assert not (outside / "github-mcp.md").exists()
    finally:
        sys.modules.pop("mcp_add", None)
