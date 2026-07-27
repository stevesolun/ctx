"""Behavior tests for the ctx-agent-mirror user story."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import agent_mirror


def _agent_text(name: str = "reviewer") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: Reviews changes and reports concrete risks.\n"
        "---\n"
        f"# {name}\n\n"
        "Inspect the change and report evidence-backed findings.\n"
    )


@pytest.fixture()
def mirror_dirs(tmp_path: Path) -> tuple[Path, Path]:
    agents_dir = tmp_path / "agents"
    wiki_dir = tmp_path / "wiki"
    agents_dir.mkdir()
    wiki_dir.mkdir()
    return agents_dir, wiki_dir


def test_mirror_one_writes_verbatim_and_then_reports_unchanged(mirror_dirs) -> None:
    agents_dir, wiki_dir = mirror_dirs
    source = agents_dir / "reviewer.md"
    body = _agent_text()
    source.write_text(body, encoding="utf-8")

    first = agent_mirror.mirror_one(
        "reviewer",
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
    )
    destination = wiki_dir / "converted-agents" / "reviewer.md"

    assert first.status == "mirrored"
    assert first.bytes_copied == len(body.encode("utf-8"))
    assert destination.read_text(encoding="utf-8") == body

    second = agent_mirror.mirror_one(
        "reviewer",
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
    )
    assert second.status == "unchanged"
    assert second.bytes_copied == 0


def test_mirror_one_dry_run_and_force_are_explicit(mirror_dirs) -> None:
    agents_dir, wiki_dir = mirror_dirs
    source = agents_dir / "reviewer.md"
    source.write_text(_agent_text(), encoding="utf-8")
    destination = wiki_dir / "converted-agents" / "reviewer.md"

    dry_run = agent_mirror.mirror_one(
        "reviewer",
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
        dry_run=True,
    )
    assert dry_run.status == "mirrored"
    assert dry_run.message == "dry-run: no files written"
    assert not destination.exists()

    destination.parent.mkdir(parents=True)
    destination.write_text("stale\n", encoding="utf-8")
    forced = agent_mirror.mirror_one(
        "reviewer",
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
        force=True,
    )
    assert forced.status == "mirrored"
    assert destination.read_text(encoding="utf-8") == _agent_text()


def test_mirror_all_filters_pipeline_fragments_non_agents_and_nested_files(
    mirror_dirs,
) -> None:
    agents_dir, wiki_dir = mirror_dirs
    (agents_dir / "reviewer.md").write_text(_agent_text(), encoding="utf-8")
    (agents_dir / "BUILDER.md").write_text(_agent_text("BUILDER"), encoding="utf-8")
    (agents_dir / "notes.md").write_text("# not an agent\n", encoding="utf-8")
    nested = agents_dir / "pipeline"
    nested.mkdir()
    (nested / "nested.md").write_text(_agent_text("nested"), encoding="utf-8")

    results = agent_mirror.mirror_all(agents_dir=agents_dir, wiki_dir=wiki_dir)
    statuses = {result.slug: result.status for result in results}

    assert statuses == {
        "BUILDER": "skipped-pipeline-fragment",
        "notes": "skipped-no-frontmatter",
        "reviewer": "mirrored",
    }
    assert not (wiki_dir / "converted-agents" / "nested.md").exists()


def test_prune_orphans_dry_run_then_apply(mirror_dirs) -> None:
    agents_dir, wiki_dir = mirror_dirs
    (agents_dir / "kept.md").write_text(_agent_text("kept"), encoding="utf-8")
    mirror_dir = wiki_dir / "converted-agents"
    mirror_dir.mkdir()
    kept = mirror_dir / "kept.md"
    orphan = mirror_dir / "orphan.md"
    kept.write_text(_agent_text("kept"), encoding="utf-8")
    orphan.write_text(_agent_text("orphan"), encoding="utf-8")

    preview = agent_mirror.prune_orphans(
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
        dry_run=True,
    )
    assert [(result.slug, result.status) for result in preview] == [("orphan", "pruned")]
    assert orphan.exists()

    applied = agent_mirror.prune_orphans(agents_dir=agents_dir, wiki_dir=wiki_dir)
    assert [(result.slug, result.status) for result in applied] == [("orphan", "pruned")]
    assert kept.exists()
    assert not orphan.exists()


def test_mirror_one_reports_invalid_and_missing_sources(mirror_dirs) -> None:
    agents_dir, wiki_dir = mirror_dirs

    invalid = agent_mirror.mirror_one(
        "../escape",
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
    )
    missing = agent_mirror.mirror_one(
        "missing",
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
    )

    assert invalid.status == "skipped-no-frontmatter"
    assert invalid.message.startswith("invalid slug:")
    assert missing.status == "not-found"


def test_cli_json_reports_single_slug_result(mirror_dirs, monkeypatch, capsys) -> None:
    agents_dir, wiki_dir = mirror_dirs
    (agents_dir / "reviewer.md").write_text(_agent_text(), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ctx-agent-mirror",
            "--slug",
            "reviewer",
            "--agents-dir",
            str(agents_dir),
            "--wiki-dir",
            str(wiki_dir),
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        agent_mirror.main()

    assert exit_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["slug"] == "reviewer"
    assert payload[0]["status"] == "mirrored"


def test_cli_read_failure_exits_nonzero(mirror_dirs, monkeypatch, capsys) -> None:
    agents_dir, wiki_dir = mirror_dirs
    source = agents_dir / "broken.md"
    source.write_text(_agent_text("broken"), encoding="utf-8")
    original_read_text = Path.read_text

    def fail_source_read(path: Path, *args, **kwargs):
        if path == source:
            raise OSError("simulated read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_source_read)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ctx-agent-mirror",
            "--slug",
            "broken",
            "--agents-dir",
            str(agents_dir),
            "--wiki-dir",
            str(wiki_dir),
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        agent_mirror.main()

    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["message"] == "read failed: simulated read failure"
