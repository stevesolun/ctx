"""Tests for ctx_audit_log — append-only JSONL audit log."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import ctx_audit_log as cal


def test_log_writes_single_line(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    cal.log(
        "skill.added", subject_type="skill", subject="python-patterns",
        actor="cli", session_id="s1", meta={"source": "test"},
        path=target,
    )
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "skill.added"
    assert record["subject_type"] == "skill"
    assert record["subject"] == "python-patterns"
    assert record["actor"] == "cli"
    assert record["session_id"] == "s1"
    assert record["meta"] == {"source": "test"}
    assert record["ts"].endswith("Z")


def test_log_is_append_only(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    for i in range(5):
        cal.log(
            "skill.loaded", subject_type="skill", subject=f"skill-{i}",
            path=target,
        )
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    # Earliest write is first line — append-only.
    subjects = [json.loads(ln)["subject"] for ln in lines]
    assert subjects == [f"skill-{i}" for i in range(5)]


def test_log_concurrent_writers_no_corruption(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"

    def writer(i: int) -> None:
        cal.log(
            "skill.score_updated", subject_type="skill",
            subject=f"t-{i}", meta={"idx": i}, path=target,
        )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    # Every line must parse as valid JSON — no torn writes.
    for line in lines:
        record = json.loads(line)
        assert record["event"] == "skill.score_updated"


def test_log_unknown_event_still_writes_but_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    target = tmp_path / "audit.jsonl"
    cal.log(
        "skill.mystery_verb", subject_type="skill", subject="x", path=target,
    )
    err = capsys.readouterr().err
    assert "unknown event" in err
    assert "skill.mystery_verb" in err
    # Write still happened — the warning is advisory.
    assert target.read_text().count("mystery_verb") == 1


def test_log_never_raises_on_unwritable_path(tmp_path: Path) -> None:
    # Point at a path inside a non-existent parent with no create
    # permission — mkdir(parents=True) handles that; this mainly asserts
    # that the exception swallow works.
    tmp_path / "audit.jsonl"
    # Passing an open directory as the target — should not raise.
    (tmp_path / "dir").mkdir()
    # Append to the dir path; OSError swallowed.
    cal.log(
        "skill.loaded", subject_type="skill", subject="x",
        path=tmp_path / "dir",
    )


def test_log_never_raises_on_unserializable_meta(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    cal.log(
        "skill.loaded",
        subject_type="skill",
        subject="x",
        meta={"path": tmp_path, "items": {"a", "b"}},
        path=target,
    )

    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["meta"]["path"] == str(tmp_path)
    assert sorted(record["meta"]["items"]) == ["a", "b"]


def test_log_never_raises_on_circular_meta(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    meta: dict[str, object] = {}
    meta["self"] = meta

    cal.log(
        "skill.loaded",
        subject_type="skill",
        subject="x",
        meta=meta,
        path=target,
    )

    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["meta"]["self"] == "<circular>"


def test_log_skill_event_wrapper(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "audit.jsonl"
    monkeypatch.setattr(cal, "audit_log_path", lambda: target)
    cal.log_skill_event("skill.loaded", "python-patterns",
                        actor="hook", session_id="abc", meta={"via": "test"})
    record = json.loads(target.read_text().splitlines()[0])
    assert record["event"] == "skill.loaded"
    assert record["subject"] == "python-patterns"
    assert record["session_id"] == "abc"
    assert record["meta"] == {"via": "test"}


def test_log_agent_event_wrapper(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "audit.jsonl"
    monkeypatch.setattr(cal, "audit_log_path", lambda: target)
    cal.log_agent_event("agent.used", "code-reviewer", actor="cli")
    record = json.loads(target.read_text().splitlines()[0])
    assert record["event"] == "agent.used"
    assert record["subject_type"] == "agent"
    assert record["subject"] == "code-reviewer"


def test_rotate_if_needed_skips_small_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "audit.jsonl"
    target.write_text("one\n", encoding="utf-8")
    monkeypatch.setattr(cal, "audit_log_path", lambda: target)
    rotated = cal.rotate_if_needed(max_bytes=1_000_000)
    assert rotated is None
    assert target.exists()


def test_rotate_if_needed_rotates_big_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "audit.jsonl"
    target.write_text("x" * 1000, encoding="utf-8")
    monkeypatch.setattr(cal, "audit_log_path", lambda: target)
    rotated = cal.rotate_if_needed(max_bytes=100)
    assert rotated is not None
    assert rotated.exists()
    # Original path should be free for the next append.
    assert not target.exists()
    assert rotated.name.startswith("audit-")
    assert rotated.suffix == ".jsonl"


def test_audit_log_path_honors_config(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "custom-audit.jsonl"

    class FakeCfg:
        def get(self, key, default=None):
            if key == "paths":
                return {"audit_log": str(custom)}
            return default

    fake_cfg_module = type("M", (), {"cfg": FakeCfg()})()
    monkeypatch.setitem(__import__("sys").modules, "ctx_config", fake_cfg_module)
    assert cal.audit_log_path() == custom


def test_all_event_types_are_dotted() -> None:
    """Every canonical event name uses ``subject_type.verb`` form."""
    for event in cal.EVENT_TYPES:
        assert "." in event, f"event {event!r} lacks dotted namespace"
        parts = event.split(".", 1)
        assert len(parts) == 2
        assert parts[0] in {"skill", "agent", "session", "backup", "toolbox"}
        assert parts[1]  # non-empty verb
