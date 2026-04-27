"""
test_harness_state.py -- SessionStore / JsonlObserver / load_session tests.

Covers:
  * SessionStore lifecycle + append-mode (resume) safety
  * Event serialisation for every event type the observer emits
  * JsonlObserver drives a round-trip: a run_loop session written
    to disk + load_session replay reconstructs the same conversation
  * Malformed-line tolerance on the reader
  * ``list_sessions`` + path helpers
  * Session-id validation rejects traversal-shaped ids
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctx.adapters.generic.loop import run_loop, LoopResult
from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ModelProvider,
    ToolCall,
    ToolDefinition,
    Usage,
)
from ctx.adapters.generic.state import (
    JsonlObserver,
    SessionStore,
    _safe_session_id,
    default_sessions_dir,
    list_sessions,
    load_session,
    new_session_id,
)


# ── Scripted provider for the round-trip tests ─────────────────────────────


class _Scripted(ModelProvider):
    name = "scripted"

    def __init__(self, responses: list[CompletionResponse]) -> None:
        self._responses = list(responses)

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResponse:
        if not self._responses:
            raise RuntimeError("scripted: no more responses")
        return self._responses.pop(0)


def _stop_response(content: str = "done") -> CompletionResponse:
    return CompletionResponse(
        content=content,
        tool_calls=(),
        finish_reason="stop",
        usage=Usage(input_tokens=5, output_tokens=3),
        provider="scripted",
        model="x",
    )


def _tool_response(*calls: ToolCall) -> CompletionResponse:
    return CompletionResponse(
        content="",
        tool_calls=tuple(calls),
        finish_reason="tool_calls",
        usage=Usage(input_tokens=10, output_tokens=4),
        provider="scripted",
        model="x",
    )


# ── Session-id safety ────────────────────────────────────────────────────


class TestSessionIdValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "../evil",
            "a/b",
            "a\\b",
            ".",
            "..",
            "has space",
            "has.dot",  # dots disallowed — risks '.jsonl' shadowing
            "a" * 129,  # too long
        ],
    )
    def test_rejects_bad(self, bad: str) -> None:
        with pytest.raises((ValueError, TypeError)):
            _safe_session_id(bad)

    @pytest.mark.parametrize(
        "good",
        [
            "abc123",
            "nightly_backfill",
            "2026-04-24-trial",
            new_session_id(),
        ],
    )
    def test_accepts_good(self, good: str) -> None:
        assert _safe_session_id(good) == good

    def test_non_string_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _safe_session_id(None)  # type: ignore[arg-type]


class TestSessionIdGen:
    def test_new_id_is_32_char_hex(self) -> None:
        sid = new_session_id()
        assert len(sid) == 32
        assert all(c in "0123456789abcdef" for c in sid)

    def test_ids_are_unique(self) -> None:
        assert new_session_id() != new_session_id()


# ── default_sessions_dir ─────────────────────────────────────────────────


class TestDefaultSessionsDir:
    def test_under_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        p = default_sessions_dir()
        assert p == tmp_path / ".ctx" / "sessions"

    def test_not_created_by_call(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        p = default_sessions_dir()
        # Intentionally lazy — SessionStore creates it, not the helper.
        assert not p.exists()


# ── SessionStore ─────────────────────────────────────────────────────────


class TestSessionStore:
    def test_create_makes_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "deeply" / "nested"
        store = SessionStore.create(
            session_id="abc", sessions_dir=nested,
        )
        try:
            assert nested.is_dir()
            assert store.path == nested / "abc.jsonl"
        finally:
            store.close()

    def test_create_auto_generates_id(self, tmp_path: Path) -> None:
        store = SessionStore.create(sessions_dir=tmp_path)
        try:
            assert len(store.session_id) == 32
        finally:
            store.close()

    def test_write_event_shapes_line(self, tmp_path: Path) -> None:
        store = SessionStore.create(session_id="s1", sessions_dir=tmp_path)
        try:
            store.write_event("custom", {"key": "value"})
        finally:
            store.close()
        raw = (tmp_path / "s1.jsonl").read_text(encoding="utf-8")
        event = json.loads(raw)
        assert event["type"] == "custom"
        assert event["session_id"] == "s1"
        assert event["key"] == "value"
        assert "ts" in event

    def test_create_rejects_existing_session_by_default(self, tmp_path: Path) -> None:
        path = tmp_path / "s1.jsonl"
        path.write_text("sentinel\n", encoding="utf-8")
        with pytest.raises(FileExistsError):
            SessionStore.create(session_id="s1", sessions_dir=tmp_path)
        assert path.read_text(encoding="utf-8") == "sentinel\n"

    def test_create_can_overwrite_existing_session_explicitly(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "s1.jsonl"
        path.write_text("sentinel\n", encoding="utf-8")
        store = SessionStore.create(
            session_id="s1",
            sessions_dir=tmp_path,
            overwrite=True,
        )
        store.close()
        assert path.read_text(encoding="utf-8") == ""

    def test_context_manager(self, tmp_path: Path) -> None:
        with SessionStore.create(session_id="s1", sessions_dir=tmp_path) as store:
            store.write_event("x", {})
        assert store.closed
        assert (tmp_path / "s1.jsonl").is_file()

    def test_write_after_close_raises(self, tmp_path: Path) -> None:
        store = SessionStore.create(session_id="s1", sessions_dir=tmp_path)
        store.close()
        with pytest.raises(RuntimeError, match="closed"):
            store.write_event("x", {})

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        store = SessionStore.create(session_id="s1", sessions_dir=tmp_path)
        store.close()
        store.close()  # must not raise

    def test_attach_requires_existing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            SessionStore.attach("nope", sessions_dir=tmp_path)

    def test_attach_appends_to_existing_file(self, tmp_path: Path) -> None:
        # Create + write one event.
        s1 = SessionStore.create(session_id="s1", sessions_dir=tmp_path)
        s1.write_event("first", {})
        s1.close()
        # Attach + append.
        s2 = SessionStore.attach("s1", sessions_dir=tmp_path)
        s2.write_event("second", {})
        s2.close()
        lines = (tmp_path / "s1.jsonl").read_text(encoding="utf-8").strip().split("\n")
        types = [json.loads(line)["type"] for line in lines]
        assert types == ["first", "second"]

    def test_unicode_content_roundtrips(self, tmp_path: Path) -> None:
        with SessionStore.create(session_id="uni", sessions_dir=tmp_path) as s:
            s.write_event("x", {"content": "café 日本 🚀"})
        event = json.loads((tmp_path / "uni.jsonl").read_text(encoding="utf-8"))
        assert event["content"] == "café 日本 🚀"


# ── Convenience writers ──────────────────────────────────────────────────


class TestConvenienceWriters:
    def test_write_session_start(self, tmp_path: Path) -> None:
        with SessionStore.create(session_id="s", sessions_dir=tmp_path) as store:
            store.write_session_start({"task": "hello", "model": "ollama/x"})
        event = json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8"))
        assert event["type"] == "session_start"
        assert event["task"] == "hello"
        assert event["model"] == "ollama/x"

    def test_write_message(self, tmp_path: Path) -> None:
        with SessionStore.create(session_id="s", sessions_dir=tmp_path) as store:
            store.write_message(Message(role="user", content="hi"))
            store.write_message(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall(id="c1", name="t", arguments={"x": 1}),),
                )
            )
        events = [
            json.loads(line) for line in
            (tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines() if line
        ]
        assert events[0]["role"] == "user"
        assert events[1]["tool_calls"][0]["arguments"] == {"x": 1}

    def test_write_model_response(self, tmp_path: Path) -> None:
        with SessionStore.create(session_id="s", sessions_dir=tmp_path) as store:
            store.write_model_response(
                3,
                CompletionResponse(
                    content="ok",
                    tool_calls=(),
                    finish_reason="stop",
                    usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.002),
                    provider="litellm",
                    model="openrouter/x",
                ),
            )
        event = json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8"))
        assert event["iteration"] == 3
        assert event["usage"]["cost_usd"] == 0.002
        assert event["provider"] == "litellm"
        assert event["model"] == "openrouter/x"

    def test_write_tool_call(self, tmp_path: Path) -> None:
        with SessionStore.create(session_id="s", sessions_dir=tmp_path) as store:
            store.write_tool_call(
                1,
                ToolCall(id="c1", name="fs__read", arguments={"path": "/tmp"}),
                result="file contents",
                error=None,
            )
        event = json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8"))
        assert event["call"]["arguments"] == {"path": "/tmp"}
        assert event["error"] is None

    def test_write_stop(self, tmp_path: Path) -> None:
        with SessionStore.create(session_id="s", sessions_dir=tmp_path) as store:
            store.write_stop(
                LoopResult(
                    stop_reason="completed",
                    final_message="done",
                    iterations=3,
                    usage=Usage(input_tokens=30, output_tokens=15, cost_usd=0.01),
                    messages=(),
                    detail="",
                )
            )
        event = json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8"))
        assert event["stop_reason"] == "completed"
        assert event["iterations"] == 3


# ── load_session ─────────────────────────────────────────────────────────


class TestLoadSession:
    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_session("nope", sessions_dir=tmp_path)

    def test_replay_simple_session(self, tmp_path: Path) -> None:
        with SessionStore.create(session_id="s", sessions_dir=tmp_path) as store:
            store.write_session_start({"task": "hello", "model": "x"})
            store.write_message(Message(role="system", content="sys"))
            store.write_message(Message(role="user", content="hi"))
            store.write_message(Message(role="assistant", content="hello back"))
            store.write_stop(
                LoopResult(
                    stop_reason="completed", final_message="hello back",
                    iterations=1, usage=Usage(), messages=(), detail="",
                )
            )
        state = load_session("s", sessions_dir=tmp_path)
        assert state.session_id == "s"
        assert state.path == tmp_path / "s.jsonl"
        assert [m.role for m in state.messages] == ["system", "user", "assistant"]
        assert state.messages[2].content == "hello back"
        assert state.metadata["task"] == "hello"
        assert state.stopped is True
        assert state.stop_reason == "completed"

    def test_replay_unstopped_session(self, tmp_path: Path) -> None:
        """Session that trails off mid-run (no stop event) is flagged."""
        with SessionStore.create(session_id="s", sessions_dir=tmp_path) as store:
            store.write_session_start({"task": "x"})
            store.write_message(Message(role="user", content="hi"))
        state = load_session("s", sessions_dir=tmp_path)
        assert state.stopped is False
        assert state.stop_reason is None

    def test_replay_tool_call_messages(self, tmp_path: Path) -> None:
        """Assistant-with-tool_calls + tool-result must round-trip faithfully."""
        with SessionStore.create(session_id="s", sessions_dir=tmp_path) as store:
            store.write_message(Message(role="user", content="go"))
            tc = ToolCall(id="c1", name="srv__fetch", arguments={"url": "https://x"})
            store.write_message(
                Message(role="assistant", content="", tool_calls=(tc,))
            )
            store.write_message(
                Message(role="tool", content="ok", tool_call_id="c1", name="srv__fetch")
            )
        state = load_session("s", sessions_dir=tmp_path)
        assert len(state.messages) == 3
        assistant = state.messages[1]
        assert assistant.tool_calls[0].id == "c1"
        assert assistant.tool_calls[0].arguments == {"url": "https://x"}
        tool_msg = state.messages[2]
        assert tool_msg.tool_call_id == "c1"
        assert tool_msg.name == "srv__fetch"

    def test_malformed_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text(
            '\n'.join([
                json.dumps({"type": "session_start", "ts": "t", "session_id": "s", "task": "hi"}),
                "this is not json at all",
                json.dumps({"type": "message", "ts": "t", "session_id": "s",
                            "role": "user", "content": "ok"}),
            ]) + "\n",
            encoding="utf-8",
        )
        state = load_session("s", sessions_dir=tmp_path)
        # Malformed line dropped; valid events still processed.
        assert len(state.messages) == 1
        assert state.metadata["task"] == "hi"

    def test_empty_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text(
            json.dumps({"type": "session_start", "ts": "t", "session_id": "s", "task": "hi"})
            + "\n\n\n"
            + json.dumps({"type": "message", "ts": "t", "session_id": "s",
                          "role": "user", "content": "hi"})
            + "\n",
            encoding="utf-8",
        )
        state = load_session("s", sessions_dir=tmp_path)
        assert len(state.messages) == 1


# ── list_sessions ────────────────────────────────────────────────────────


class TestListSessions:
    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        assert list_sessions(sessions_dir=tmp_path) == []

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        assert list_sessions(sessions_dir=tmp_path / "missing") == []

    def test_only_jsonl_files_listed(self, tmp_path: Path) -> None:
        (tmp_path / "alpha.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "beta.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "not_a_session.txt").write_text("", encoding="utf-8")
        (tmp_path / "subdir").mkdir()
        assert list_sessions(sessions_dir=tmp_path) == ["alpha", "beta"]

    def test_sorted_alphabetically(self, tmp_path: Path) -> None:
        for name in ("zzz", "aaa", "mmm"):
            (tmp_path / f"{name}.jsonl").write_text("", encoding="utf-8")
        assert list_sessions(sessions_dir=tmp_path) == ["aaa", "mmm", "zzz"]


# ── JsonlObserver end-to-end via run_loop ───────────────────────────────


class TestJsonlObserverRoundTrip:
    def test_simple_completion_replay(self, tmp_path: Path) -> None:
        provider = _Scripted([_stop_response("final answer")])
        store = SessionStore.create(session_id="trip1", sessions_dir=tmp_path)
        observer = JsonlObserver(
            store,
            session_metadata={"task": "smoke-test", "model": "ollama/x"},
        )
        try:
            result = run_loop(
                provider=provider,
                system_prompt="sys",
                task="hi",
                observer=observer,
            )
        finally:
            store.close()

        assert result.stop_reason == "completed"

        state = load_session("trip1", sessions_dir=tmp_path)
        # Replay must match the live conversation byte-for-byte (for
        # roles + content).
        live = [
            (m.role, m.content) for m in result.messages
        ]
        replayed = [(m.role, m.content) for m in state.messages]
        assert live == replayed
        assert state.metadata["task"] == "smoke-test"
        assert state.stop_reason == "completed"
        assert state.stopped is True

    def test_tool_call_round_trip(self, tmp_path: Path) -> None:
        tc = ToolCall(id="c1", name="srv__echo", arguments={"text": "hi"})
        provider = _Scripted(
            [_tool_response(tc), _stop_response("seen hi")]
        )
        store = SessionStore.create(session_id="trip2", sessions_dir=tmp_path)
        observer = JsonlObserver(store, session_metadata={"task": "go"})
        try:
            run_loop(
                provider=provider,
                system_prompt="",
                task="go",
                tool_executor=lambda call: f"echo:{call.arguments['text']}",
                observer=observer,
            )
        finally:
            store.close()

        state = load_session("trip2", sessions_dir=tmp_path)
        roles = [m.role for m in state.messages]
        # user(task) + assistant(tool_calls) + tool(result) + assistant(final)
        assert roles == ["user", "assistant", "tool", "assistant"]
        tool_msg = state.messages[2]
        assert tool_msg.content == "echo:hi"
        assert tool_msg.tool_call_id == "c1"

    def test_resume_via_messages_kwarg(self, tmp_path: Path) -> None:
        """Load a prior session and hand its messages to a fresh run_loop."""
        # First run.
        provider1 = _Scripted([_stop_response("first-answer")])
        store1 = SessionStore.create(session_id="resume1", sessions_dir=tmp_path)
        try:
            run_loop(
                provider=provider1,
                system_prompt="sys",
                task="initial task",
                observer=JsonlObserver(store1, session_metadata={"task": "initial task"}),
            )
        finally:
            store1.close()

        # Resume: load + feed back.
        state = load_session("resume1", sessions_dir=tmp_path)
        provider2 = _Scripted([_stop_response("resumed-answer")])
        store2 = SessionStore.attach("resume1", sessions_dir=tmp_path)
        try:
            result2 = run_loop(
                provider=provider2,
                system_prompt="sys",
                task="follow-up",
                messages=list(state.messages),
                observer=JsonlObserver(
                    store2,
                    session_metadata={},
                    emit_session_start=False,  # already written in first run
                ),
            )
        finally:
            store2.close()

        # First run's conversation is visible in the resumed result.
        resumed_roles = [m.role for m in result2.messages]
        # system + user(follow-up) + ...prior(system,user,assistant)... + assistant(final)
        # The seeded 'messages' param appends AFTER the system+task, so:
        #   [system, user(follow-up), system, user(initial), assistant(first-answer),
        #    assistant(resumed-answer)]
        assert resumed_roles.count("system") == 2
        contents = [m.content for m in result2.messages]
        assert "first-answer" in contents
        assert "resumed-answer" in contents

    def test_metadata_includes_seed_messages(self, tmp_path: Path) -> None:
        """session_start payload captures the seed conversation."""
        provider = _Scripted([_stop_response("ok")])
        store = SessionStore.create(session_id="meta", sessions_dir=tmp_path)
        observer = JsonlObserver(store, session_metadata={"task": "t"})
        try:
            run_loop(
                provider=provider, system_prompt="sys", task="t", observer=observer,
            )
        finally:
            store.close()
        # Find the session_start event directly so we can inspect seeds.
        first_line = (tmp_path / "meta.jsonl").read_text(encoding="utf-8").splitlines()[0]
        event = json.loads(first_line)
        assert event["type"] == "session_start"
        assert len(event["seed_messages"]) == 2  # system + user(task)
        assert event["seed_messages"][0]["role"] == "system"
        assert event["seed_messages"][1]["role"] == "user"

    def test_observer_no_emit_start(self, tmp_path: Path) -> None:
        """When emit_session_start=False the observer doesn't write it — caller did."""
        provider = _Scripted([_stop_response("ok")])
        store = SessionStore.create(session_id="noe", sessions_dir=tmp_path)
        observer = JsonlObserver(
            store, session_metadata={"task": "t"}, emit_session_start=False,
        )
        try:
            run_loop(
                provider=provider, system_prompt="", task="t", observer=observer,
            )
        finally:
            store.close()
        events = [
            json.loads(line) for line in
            (tmp_path / "noe.jsonl").read_text(encoding="utf-8").splitlines() if line
        ]
        types = [e["type"] for e in events]
        assert "session_start" not in types

    def test_stop_event_always_written(self, tmp_path: Path) -> None:
        provider = _Scripted([_stop_response("ok")])
        store = SessionStore.create(session_id="stop", sessions_dir=tmp_path)
        observer = JsonlObserver(store, session_metadata={"task": "t"})
        try:
            run_loop(
                provider=provider, system_prompt="", task="t", observer=observer,
            )
        finally:
            store.close()
        last_line = (
            (tmp_path / "stop.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .split("\n")[-1]
        )
        event = json.loads(last_line)
        assert event["type"] == "stop"
        assert event["stop_reason"] == "completed"
