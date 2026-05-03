"""Tests for add/update producers that enqueue wiki maintenance jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from ctx.core.wiki import wiki_queue


class _Decision:
    allow = True
    warnings: tuple[Any, ...] = ()


def _skill_text() -> str:
    return "\n".join(
        [
            "---",
            "name: queue-skill",
            "description: Skill used to verify queue producer integration.",
            "tags:",
            "  - testing",
            "---",
            "# queue-skill",
            "",
            "This skill has enough body content to pass structural checks.",
            "",
            "## Usage",
            "",
            "Use it when testing durable queue producer behavior.",
        ]
    )


def _agent_text() -> str:
    return "\n".join(
        [
            "---",
            "name: queue-agent",
            "description: Agent used to verify queue producer integration.",
            "model: inherit",
            "---",
            "# queue-agent",
            "",
            "This agent has enough body content to pass structural checks.",
            "",
            "## Review Process",
            "",
            "Use it when testing durable queue producer behavior.",
        ]
    )


def _mcp_record() -> Any:
    from mcp_entity import McpRecord

    return McpRecord.from_dict(
        {
            "slug": "queue-mcp",
            "name": "Queue MCP",
            "description": "MCP server used to verify queue producer integration.",
            "sources": ["test"],
            "github_url": "https://github.com/example/queue-mcp",
            "tags": ["testing", "mcp"],
            "transports": ["stdio"],
        }
    )


def _harness_record() -> Any:
    import harness_add

    return harness_add.HarnessRecord.from_dict(
        {
            "slug": "queue-harness",
            "name": "Queue Harness",
            "description": "Harness used to verify queue producer integration.",
            "repo_url": "https://github.com/example/queue-harness",
            "tags": ["testing", "harness"],
            "model_providers": ["openai"],
            "runtimes": ["python"],
            "capabilities": ["Run queue producer tests"],
            "setup_commands": ["pip install -e ."],
            "verify_commands": ["pytest"],
            "sources": ["test"],
        }
    )


def _patch_skill_side_effects(monkeypatch: Any, skill_add: Any) -> None:
    monkeypatch.setattr(skill_add, "check_intake", MagicMock(return_value=_Decision()))
    monkeypatch.setattr(skill_add, "record_embedding", MagicMock())
    monkeypatch.setattr(skill_add, "update_index", MagicMock())
    monkeypatch.setattr(skill_add, "append_log", MagicMock())


def _patch_agent_side_effects(monkeypatch: Any, agent_add: Any) -> None:
    monkeypatch.setattr(agent_add, "check_intake", MagicMock(return_value=_Decision()))
    monkeypatch.setattr(agent_add, "record_embedding", MagicMock())
    monkeypatch.setattr(agent_add, "update_index", MagicMock())
    monkeypatch.setattr(agent_add, "append_log", MagicMock())


def _patch_mcp_side_effects(monkeypatch: Any, mcp_add: Any) -> None:
    monkeypatch.setattr(mcp_add, "check_intake", MagicMock(return_value=_Decision()))
    monkeypatch.setattr(mcp_add, "record_embedding", MagicMock())
    monkeypatch.setattr(mcp_add, "update_index", MagicMock())
    monkeypatch.setattr(mcp_add, "append_log", MagicMock())


def _patch_harness_side_effects(monkeypatch: Any, harness_add: Any) -> None:
    monkeypatch.setattr(harness_add, "update_index", MagicMock())
    monkeypatch.setattr(harness_add, "append_log", MagicMock())


def test_add_skill_enqueues_entity_upsert(tmp_path: Path, monkeypatch: Any) -> None:
    import skill_add

    wiki = tmp_path / "wiki"
    skills_dir = tmp_path / "skills"
    source = tmp_path / "SKILL.md"
    (wiki / "entities" / "skills").mkdir(parents=True)
    (wiki / "converted").mkdir(parents=True)
    source.write_text(_skill_text(), encoding="utf-8")
    _patch_skill_side_effects(monkeypatch, skill_add)

    result = skill_add.add_skill(
        source_path=source,
        name="queue-skill",
        wiki_path=wiki,
        skills_dir=skills_dir,
    )

    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert result["queued_job_id"] == jobs[0].id
    assert len(jobs) == 1
    assert jobs[0].kind == "entity-upsert"
    assert jobs[0].status == "pending"
    assert jobs[0].payload["entity_type"] == "skill"
    assert jobs[0].payload["slug"] == "queue-skill"
    assert jobs[0].payload["entity_path"] == "entities/skills/queue-skill.md"


def test_add_agent_enqueues_entity_upsert(tmp_path: Path, monkeypatch: Any) -> None:
    import agent_add

    wiki = tmp_path / "wiki"
    agents_dir = tmp_path / "agents"
    source = tmp_path / "queue-agent.md"
    (wiki / "entities" / "agents").mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    source.write_text(_agent_text(), encoding="utf-8")
    _patch_agent_side_effects(monkeypatch, agent_add)

    result = agent_add.add_agent(
        source_path=source,
        name="queue-agent",
        wiki_path=wiki,
        agents_dir=agents_dir,
    )

    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert result["queued_job_id"] == jobs[0].id
    assert len(jobs) == 1
    assert jobs[0].kind == "entity-upsert"
    assert jobs[0].status == "pending"
    assert jobs[0].payload["entity_type"] == "agent"
    assert jobs[0].payload["slug"] == "queue-agent"
    assert jobs[0].payload["entity_path"] == "entities/agents/queue-agent.md"


def test_add_mcp_enqueues_entity_upsert(tmp_path: Path, monkeypatch: Any) -> None:
    import mcp_add

    wiki = tmp_path / "wiki"
    (wiki / "entities" / "mcp-servers").mkdir(parents=True)
    _patch_mcp_side_effects(monkeypatch, mcp_add)

    result = mcp_add.add_mcp(record=_mcp_record(), wiki_path=wiki)

    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert result["queued_job_id"] == jobs[0].id
    assert len(jobs) == 1
    assert jobs[0].kind == "entity-upsert"
    assert jobs[0].status == "pending"
    assert jobs[0].payload["entity_type"] == "mcp-server"
    assert jobs[0].payload["slug"] == "queue-mcp"
    assert jobs[0].payload["entity_path"] == "entities/mcp-servers/q/queue-mcp.md"


def test_add_harness_enqueues_entity_upsert(tmp_path: Path, monkeypatch: Any) -> None:
    import harness_add

    wiki = tmp_path / "wiki"
    (wiki / "entities" / "harnesses").mkdir(parents=True)
    _patch_harness_side_effects(monkeypatch, harness_add)

    result = harness_add.add_harness(record=_harness_record(), wiki_path=wiki)

    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert result["queued_job_id"] == jobs[0].id
    assert len(jobs) == 1
    assert jobs[0].kind == "entity-upsert"
    assert jobs[0].status == "pending"
    assert jobs[0].payload["entity_type"] == "harness"
    assert jobs[0].payload["slug"] == "queue-harness"
    assert jobs[0].payload["entity_path"] == "entities/harnesses/queue-harness.md"
