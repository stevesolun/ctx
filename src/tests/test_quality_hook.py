"""
test_quality_hook.py -- Regression tests for runtime hooks.

Focuses on the pure helpers in ``quality_on_session_end.py`` plus the
root-script and packaged-entrypoint runtime contracts that must never block
Claude Code tool/session lifecycles. Expensive child processes are mocked;
the scorer and backup mirror have their own dedicated suites.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

import quality_on_session_end as qh  # noqa: E402
import backup_on_change as backup_hook  # noqa: E402
from ctx.adapters.claude_code.hooks import lifecycle_hooks  # noqa: E402


NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _write_events(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_touched_slugs_since_cutoff(tmp_path: Path) -> None:
    events = tmp_path / "skill-events.jsonl"
    _write_events(
        events,
        [
            {"event": "load", "skill": "old-one", "timestamp": _iso(NOW - timedelta(days=3))},
            {"event": "load", "skill": "fresh-one", "timestamp": _iso(NOW - timedelta(hours=1))},
            {"event": "load", "skill": "fresh-two", "timestamp": _iso(NOW - timedelta(minutes=10))},
            {
                "event": "load",
                "skill": "fresh-one",  # dup
                "timestamp": _iso(NOW - timedelta(minutes=5)),
            },
        ],
    )
    cutoff = NOW - timedelta(hours=2)
    slugs = qh._touched_slugs_since(cutoff, events)
    assert slugs == ["fresh-one", "fresh-two"]


def test_touched_slugs_skips_malformed_lines(tmp_path: Path) -> None:
    events = tmp_path / "skill-events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        "not json\n"
        + json.dumps({"event": "load", "skill": "good", "timestamp": _iso(NOW)})
        + "\n"
        + json.dumps({"event": "load", "skill": 42, "timestamp": _iso(NOW)})  # non-string skill
        + "\n",
        encoding="utf-8",
    )
    slugs = qh._touched_slugs_since(NOW - timedelta(hours=1), events)
    assert slugs == ["good"]


def test_touched_slugs_missing_file_returns_empty(tmp_path: Path) -> None:
    slugs = qh._touched_slugs_since(NOW, tmp_path / "does-not-exist.jsonl")
    assert slugs == []


def test_touched_slugs_caps_at_max(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(qh, "_MAX_SLUGS_PER_RUN", 3, raising=True)
    events = tmp_path / "skill-events.jsonl"
    _write_events(
        events,
        [
            {"event": "load", "skill": f"s{i}", "timestamp": _iso(NOW - timedelta(minutes=i))}
            for i in range(10)
        ],
    )
    slugs = qh._touched_slugs_since(NOW - timedelta(hours=1), events)
    assert len(slugs) == 3


def test_read_cutoff_uses_state_file(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"last_run_at": _iso(NOW - timedelta(hours=2))}),
        encoding="utf-8",
    )
    monkeypatch.setattr(qh, "_STATE_PATH", state, raising=True)
    cutoff = qh._read_cutoff()
    assert abs((cutoff - (NOW - timedelta(hours=2))).total_seconds()) < 5


def test_read_cutoff_falls_back_when_state_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(qh, "_STATE_PATH", tmp_path / "absent.json", raising=True)
    cutoff = qh._read_cutoff()
    # Default lookback is 24h; cutoff should be in the past but not ancient.
    delta = datetime.now(timezone.utc) - cutoff
    assert 0 < delta.total_seconds() <= 25 * 3600


def test_write_state_roundtrip(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setattr(qh, "_STATE_PATH", state, raising=True)
    qh._write_state(NOW)
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["last_run_at"].startswith("2026-04-19")


def test_invoke_recompute_noops_on_empty(monkeypatch) -> None:
    called = {"n": 0}

    def _bad_subprocess(*a, **kw):
        called["n"] += 1
        raise AssertionError("should not be called")

    monkeypatch.setattr(qh.subprocess, "run", _bad_subprocess, raising=True)
    assert qh._invoke_recompute([]) == 0
    assert called["n"] == 0


def test_hook_mains_exit_zero_and_dispatch_expected_work(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Point everything at tmp so no real state is touched.
    monkeypatch.setenv("HOME", str(tmp_path))
    events = tmp_path / "skill-events.jsonl"
    _write_events(events, [{"event": "load", "skill": "demo", "timestamp": _iso(NOW)}])
    monkeypatch.setattr(qh, "_EVENTS_PATH", events, raising=True)
    monkeypatch.setattr(qh, "_STATE_PATH", tmp_path / "state.json", raising=True)

    def _boom(*a, **kw):
        raise OSError("pretend the subprocess exploded")

    monkeypatch.setattr(qh.subprocess, "run", _boom, raising=True)
    # Provide empty stdin.
    monkeypatch.setattr("sys.stdin", _StdinStub(""))
    rc = qh.main()
    assert rc == 0  # hook never propagates errors

    current = datetime.now(timezone.utc)
    (tmp_path / "state.json").write_text(
        json.dumps({"last_run_at": _iso(current - timedelta(minutes=3))}),
        encoding="utf-8",
    )
    _write_events(
        events,
        [
            {
                "event": "load",
                "skill": "fresh-one",
                "timestamp": _iso(current - timedelta(minutes=2)),
            },
            {
                "event": "load",
                "skill": "fresh-two",
                "timestamp": _iso(current - timedelta(minutes=1)),
            },
            {
                "event": "load",
                "skill": "fresh-one",
                "timestamp": _iso(current),
            },
        ],
    )
    calls: list[dict[str, Any]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def _capture_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env")})
        return _Result()

    monkeypatch.setattr(qh.subprocess, "run", _capture_run, raising=True)
    monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps({"session_id": "sess-1"})))
    assert qh.main() == 0
    assert calls
    assert calls[0]["cmd"][-3:] == ["recompute", "--slugs", "fresh-one,fresh-two"]
    assert calls[0]["env"]["CTX_SESSION_ID"] == "sess-1"

    snapshot_reasons: list[str] = []
    touched = tmp_path / ".claude" / "settings.json"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(touched)},
    }

    def _record_snapshot(reason: str) -> int:
        snapshot_reasons.append(reason)
        return 2

    monkeypatch.setattr(
        backup_hook,
        "_is_tracked",
        lambda path, claude_home: path == touched and claude_home.name == ".claude",
        raising=True,
    )
    monkeypatch.setattr(
        backup_hook,
        "_invoke_snapshot",
        _record_snapshot,
        raising=True,
    )
    monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps(payload)))
    assert backup_hook.main() == 0
    assert snapshot_reasons == ["Write:settings.json"]

    monkeypatch.setattr(backup_hook, "_is_tracked", lambda path, home: False, raising=True)
    monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps(payload)))
    assert backup_hook.main() == 0
    assert snapshot_reasons == ["Write:settings.json"]

    packaged_events = tmp_path / "packaged-skill-events.jsonl"
    packaged_state = tmp_path / "packaged-state.json"
    packaged_current = datetime.now(timezone.utc)
    packaged_seeded_last_run = _iso(packaged_current - timedelta(minutes=3))
    packaged_state.write_text(
        json.dumps({"last_run_at": packaged_seeded_last_run}),
        encoding="utf-8",
    )
    _write_events(
        packaged_events,
        [
            {
                "event": "load",
                "skill": "packaged-one",
                "timestamp": _iso(packaged_current - timedelta(minutes=2)),
            },
            {
                "event": "load",
                "skill": "packaged-two",
                "timestamp": _iso(packaged_current - timedelta(minutes=1)),
            },
            {
                "event": "load",
                "skill": "packaged-one",
                "timestamp": _iso(packaged_current),
            },
        ],
    )
    monkeypatch.setattr(lifecycle_hooks, "_EVENTS_PATH", packaged_events, raising=True)
    monkeypatch.setattr(lifecycle_hooks, "_STATE_PATH", packaged_state, raising=True)
    monkeypatch.delenv("CTX_SESSION_ID", raising=False)

    quality_calls: list[dict[str, Any]] = []

    def _record_quality_main(argv: list[str]) -> int:
        quality_calls.append(
            {"argv": argv, "session_id": lifecycle_hooks.os.environ.get("CTX_SESSION_ID")}
        )
        return 0

    audit_events: list[dict[str, Any]] = []

    def _record_session_event(
        event: str,
        session_id: str,
        *,
        actor: str,
        meta: dict[str, Any],
    ) -> None:
        audit_events.append(
            {
                "event": event,
                "session_id": session_id,
                "actor": actor,
                "meta": meta,
            }
        )

    def _record_rotate() -> None:
        audit_events.append({"event": "rotated"})

    monkeypatch.setitem(
        sys.modules,
        "skill_quality",
        SimpleNamespace(main=_record_quality_main),
    )
    monkeypatch.setitem(
        sys.modules,
        "ctx_audit_log",
        SimpleNamespace(
            log_session_event=_record_session_event,
            rotate_if_needed=_record_rotate,
        ),
    )
    monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps({"sessionId": "sess-2"})))
    assert lifecycle_hooks.main(["quality-on-session-end"]) == 0
    assert quality_calls == [
        {
            "argv": ["recompute", "--slugs", "packaged-one,packaged-two"],
            "session_id": "sess-2",
        }
    ]
    assert "CTX_SESSION_ID" not in lifecycle_hooks.os.environ
    packaged_last_run = json.loads(packaged_state.read_text(encoding="utf-8"))["last_run_at"]
    assert datetime.fromisoformat(packaged_last_run) > datetime.fromisoformat(
        packaged_seeded_last_run
    )
    assert audit_events[0]["event"] == "session.ended"
    assert audit_events[0]["session_id"] == "sess-2"
    assert audit_events[0]["meta"]["recomputed_slugs"] == 2
    assert audit_events[1] == {"event": "rotated"}

    packaged_snapshot_reasons: list[str] = []
    packaged_touched = tmp_path / ".claude" / "packaged-settings.json"
    packaged_payload = {
        "tool_name": "MultiEdit",
        "tool_input": {"file_path": str(packaged_touched)},
    }

    def _record_packaged_snapshot(*, reason: str) -> None:
        packaged_snapshot_reasons.append(reason)

    monkeypatch.setattr(
        lifecycle_hooks,
        "_is_tracked",
        lambda path, claude_home: path == packaged_touched and claude_home.name == ".claude",
        raising=True,
    )
    monkeypatch.setitem(
        sys.modules,
        "backup_mirror",
        SimpleNamespace(snapshot_if_changed=_record_packaged_snapshot),
    )
    monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps(packaged_payload)))
    assert lifecycle_hooks.main(["backup-on-change"]) == 0
    assert packaged_snapshot_reasons == ["MultiEdit:packaged-settings.json"]

    monkeypatch.setattr(lifecycle_hooks, "_is_tracked", lambda path, home: False, raising=True)
    monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps(packaged_payload)))
    assert lifecycle_hooks.main(["backup-on-change"]) == 0
    assert packaged_snapshot_reasons == ["MultiEdit:packaged-settings.json"]


class _StdinStub:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
