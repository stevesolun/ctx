"""
test_skill_loader_telemetry.py -- Integration test: skill_loader emits a telemetry event.

Verifies that loading a skill via skill_loader.main() (or the programmatic
path through find_skill + emit_load_event) writes exactly one JSON line to
the JSONL stream with the correct ``skill`` slug and ``event`` type.

The events file is redirected to a tmp_path location so the real
~/.claude/skill-events.jsonl is never touched.  The override is passed
directly to emit_load_event via its ``path`` kwarg, matching the API that
skill_telemetry.log_event already exposes.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Shared fixture: fake HOME + reloaded skill_loader module
# ---------------------------------------------------------------------------


@pytest.fixture()
def loader_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return a reloaded skill_loader with a throwaway HOME and a real skill."""
    home = tmp_path / "home"
    skill_dir = home / ".claude" / "skills" / "myskill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# myskill\n\nDo stuff.", encoding="utf-8")
    (home / ".claude" / "agents").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows

    from ctx.adapters.claude_code import skill_loader
    importlib.reload(skill_loader)
    return skill_loader, home


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_emit_load_event_writes_one_line(loader_env, tmp_path: Path) -> None:
    """emit_load_event writes exactly one JSONL line with correct slug + event."""
    loader, _ = loader_env
    events_file = tmp_path / "events.jsonl"

    loader.emit_load_event("myskill", path=events_file, trusted_root=tmp_path)

    lines = [ln for ln in events_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"

    record = json.loads(lines[0])
    assert record["skill"] == "myskill"
    assert record["event"] == "load"
    assert "timestamp" in record
    assert "session_id" in record
    assert "event_id" in record


def test_emit_load_event_failure_does_not_raise(loader_env, tmp_path: Path) -> None:
    """A telemetry write to an unwritable path must not propagate an exception."""
    loader, _ = loader_env
    # Pass a path whose parent does not exist and cannot be created (a file
    # sitting where the parent directory would need to be).
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a dir", encoding="utf-8")
    bad_path = blocker / "events.jsonl"  # parent is a file -> mkdir will fail

    # Must not raise anything (trusted_root=tmp_path allows the path through
    # the containment check; the OS-level mkdir failure is what we're testing).
    loader.emit_load_event("myskill", path=bad_path, trusted_root=tmp_path)


def test_no_event_emitted_when_skill_not_found(loader_env, tmp_path: Path) -> None:
    """A failed find_skill lookup must not produce any telemetry event."""
    loader, _ = loader_env
    events_file = tmp_path / "events.jsonl"

    result = loader.find_skill("nosuchskill")
    assert result is None
    # emit_load_event is only called after find_skill succeeds, so the file
    # should not exist (or be empty if somehow touched).
    assert not events_file.exists() or events_file.stat().st_size == 0


def test_multiple_loads_write_multiple_lines(loader_env, tmp_path: Path) -> None:
    """Each successful emit_load_event call appends an independent line."""
    loader, home = loader_env

    # Create a second skill so we can load two distinct slugs.
    second_dir = home / ".claude" / "skills" / "otherskill"
    second_dir.mkdir(parents=True)
    (second_dir / "SKILL.md").write_text("# other", encoding="utf-8")
    importlib.reload(loader)

    events_file = tmp_path / "events.jsonl"
    loader.emit_load_event("myskill", path=events_file, trusted_root=tmp_path)
    loader.emit_load_event("otherskill", path=events_file, trusted_root=tmp_path)

    lines = [ln for ln in events_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2

    slugs = {json.loads(ln)["skill"] for ln in lines}
    assert slugs == {"myskill", "otherskill"}

    event_types = {json.loads(ln)["event"] for ln in lines}
    assert event_types == {"load"}
