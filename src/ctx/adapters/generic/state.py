"""ctx.adapters.generic.state — append-only JSONL session persistence.

Every ``run_loop`` invocation produces a stream of events — model
responses, tool calls, stop reason. This module writes each one as a
single JSONL line so a session can be:

  * inspected later without re-running the agent
  * replayed to pick up where a crashed or canceled run left off
  * grep-audited for cost/time analysis
  * shipped to ``ctx.adapters.claude_code`` or any other consumer
    without format conversion

Why append-only JSONL instead of LangGraph-style per-step
checkpointing?
  Plan 001 §6 calls this out explicitly. JSONL is the
  Claude-Agent-SDK default, is the industry floor for coding agents
  (Aider, Cline, Goose all track sessions in some JSONL-shaped log),
  and has zero runtime deps beyond ``json``. If a future phase needs
  time-travel or branching, we add an opt-in SQL-backed store behind
  the same ``StateStore`` Protocol without ripping this out.

File layout (on disk)::

    <sessions_dir>/<session_id>.jsonl

Each line is a single JSON object with at minimum:

    {
      "type": "<event-type>",
      "ts":   "<ISO-8601 UTC>",
      "session_id": "<uuid hex>",
      ...event-specific payload...
    }

Event types this module emits:

    session_start    one per session, first line. Carries the task,
                     system prompt, model, provider, budget caps.
    iteration_start  per iteration. Marks the boundary for --resume.
    model_response   the CompletionResponse from the provider.
    tool_call        one per tool invocation. Has result + error.
    message          every Message appended to the conversation —
                     the canonical replay substrate.
    stop             one per session, last line. LoopResult summary.

Resume semantics (H4 v1):
  * ``load_session(id)`` reads every ``message`` event in order.
  * Returns ``ReplayState(messages, last_user_task, system_prompt)``.
  * Caller passes ``messages=state.messages`` into ``run_loop`` with
    the original ``system_prompt``/``task`` to continue the run.
  * Sessions with a ``stop`` event are resumable — resume appends a
    new task and keeps going. Plan 001 Phase H7 wires the CLI flag.

Plan 001 Phase H4.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO, cast

from ctx.adapters.generic.loop import LoopResult, LoopObserver
from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ToolCall,
    Usage,
)


_logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _safe_session_id(raw: str) -> str:
    """Validate a caller-supplied session id before interpolating into a path.

    Rejects anything that could escape the sessions directory. The
    generated-by-us id is a uuid.hex (lowercase hex, 32 chars) — we
    accept the full lowercase-hex + underscore alphabet so user-named
    sessions ('nightly_backfill') keep working.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("session_id must be a non-empty string")
    if not all(c.isalnum() or c in "-_" for c in raw):
        raise ValueError(
            f"invalid session_id {raw!r}; allowed: alphanumeric, '-', '_'"
        )
    if len(raw) > 128:
        raise ValueError(f"session_id too long ({len(raw)} > 128)")
    return raw


def default_sessions_dir() -> Path:
    """Return ``~/.ctx/sessions`` (not created by this call).

    The generic harness stores its state under ``~/.ctx/`` to keep
    explicit distance from Claude Code's ``~/.claude/`` — the two
    adapters are deliberately decoupled on disk. Plan 001 §1 (Q4):
    user wants both layouts populated out of the box; init is the
    responsibility of the CLI (H7), not this module.
    """
    return Path(os.path.expanduser("~")) / ".ctx" / "sessions"


def new_session_id() -> str:
    """Fresh uuid-hex session id. Short enough to paste, long enough to not collide."""
    return uuid.uuid4().hex


# ── Serialisation helpers ─────────────────────────────────────────────────


def _reject_symlink(path: Path, label: str = "session path") -> None:
    try:
        is_link = path.is_symlink()
    except OSError as exc:
        raise ValueError(f"{label} cannot be inspected: {path}") from exc
    if is_link:
        raise ValueError(f"{label} must not be a symlink: {path}")


def _open_regular_text(
    path: Path,
    *,
    append: bool = False,
    overwrite: bool = False,
    read: bool = False,
) -> TextIO:
    _reject_symlink(path.parent, "sessions directory")
    _reject_symlink(path, "session log")
    if read:
        flags = os.O_RDONLY
        fd_mode = "r"
    elif append:
        flags = os.O_WRONLY | os.O_APPEND
        fd_mode = "a"
    else:
        flags = os.O_WRONLY | os.O_CREAT
        flags |= os.O_TRUNC if overwrite else os.O_EXCL
        fd_mode = "w"
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"session log must be a regular file: {path}")
        return cast(TextIO, os.fdopen(fd, fd_mode, encoding="utf-8", buffering=1))
    except Exception:
        os.close(fd)
        raise


def _message_to_dict(msg: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role, "content": msg.content}
    if msg.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in msg.tool_calls
        ]
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    if msg.name is not None:
        out["name"] = msg.name
    return out


def _dict_to_message(d: dict[str, Any]) -> Message:
    raw_tcs = d.get("tool_calls") or []
    tool_calls = tuple(
        ToolCall(
            id=str(tc.get("id", "")),
            name=str(tc.get("name", "")),
            arguments=dict(tc.get("arguments") or {}),
        )
        for tc in raw_tcs
    )
    return Message(
        role=d.get("role", "user"),       # type: ignore[arg-type]
        content=str(d.get("content", "")),
        tool_calls=tool_calls,
        tool_call_id=d.get("tool_call_id"),
        name=d.get("name"),
    )


def _repair_unresolved_tool_call_tail(
    messages: list[Message],
    *,
    session_id: str,
) -> list[Message]:
    """Add error tool messages when a crash left the replay tail invalid."""
    idx = len(messages) - 1
    seen_tool_results: set[str] = set()
    while idx >= 0 and messages[idx].role == "tool":
        tool_call_id = messages[idx].tool_call_id
        if tool_call_id:
            seen_tool_results.add(tool_call_id)
        idx -= 1

    if idx < 0:
        return messages

    assistant = messages[idx]
    if assistant.role != "assistant" or not assistant.tool_calls:
        return messages

    missing = [
        call for call in assistant.tool_calls
        if call.id not in seen_tool_results
    ]
    if not missing:
        return messages

    repaired = list(messages)
    for call in missing:
        repaired.append(
            Message(
                role="tool",
                content=(
                    "ERROR: tool call did not complete before session replay; "
                    "no persisted tool result was found."
                ),
                tool_call_id=call.id,
                name=call.name,
            )
        )
    _logger.warning(
        "session %s: repaired %d unresolved tool call(s) at replay tail",
        session_id,
        len(missing),
    )
    return repaired


def _usage_to_dict(usage: Usage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": usage.cost_usd,
    }


# ── SessionStore — one file, one session ─────────────────────────────────


class SessionStore:
    """Append-only JSONL writer for one session.

    Thread-safe on the write path — a single lock serialises appends
    so a concurrent observer (e.g. a future streaming consumer) can't
    interleave partial lines.

    Usage::

        store = SessionStore.create(session_id, sessions_dir)
        store.write_session_start({"task": "...", "model": "..."})
        ...
        store.close()

    Or as a context manager::

        with SessionStore.create(...) as store:
            ...
    """

    def __init__(
        self,
        *,
        session_id: str,
        path: Path,
        append: bool = False,
        overwrite: bool = False,
    ) -> None:
        self._session_id = _safe_session_id(session_id)
        self._path = path
        self._lock = threading.Lock()
        self._closed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered so crash-ish terminations still flush per line.
        self._fh = _open_regular_text(path, append=append, overwrite=overwrite)

    # ── construction ─────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        session_id: str | None = None,
        sessions_dir: Path | None = None,
        overwrite: bool = False,
    ) -> "SessionStore":
        """Open a fresh session file; generates an id if none given."""
        sid = session_id if session_id is not None else new_session_id()
        sdir = sessions_dir if sessions_dir is not None else default_sessions_dir()
        path = sdir / f"{_safe_session_id(sid)}.jsonl"
        return cls(session_id=sid, path=path, append=False, overwrite=overwrite)

    @classmethod
    def attach(
        cls,
        session_id: str,
        sessions_dir: Path | None = None,
    ) -> "SessionStore":
        """Open an existing session in append mode (for --resume)."""
        sdir = sessions_dir if sessions_dir is not None else default_sessions_dir()
        path = sdir / f"{_safe_session_id(session_id)}.jsonl"
        _reject_symlink(sdir, "sessions directory")
        _reject_symlink(path, "session log")
        if not path.is_file():
            raise FileNotFoundError(f"session log not found: {path}")
        return cls(session_id=session_id, path=path, append=True)

    # ── properties ───────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        return self._closed

    # ── lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._fh.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._closed = True

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── write primitives ────────────────────────────────────────────────

    def write_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Write one event as a single JSONL line. Thread-safe, atomic-per-line."""
        if self._closed:
            raise RuntimeError(f"session {self._session_id!r} is closed")
        event = {
            "type": event_type,
            "ts": _now_iso(),
            "session_id": self._session_id,
            **payload,
        }
        line = json.dumps(event, ensure_ascii=False, default=_json_default) + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()

    # ── convenience writers ─────────────────────────────────────────────

    def write_session_start(self, payload: dict[str, Any]) -> None:
        self.write_event("session_start", payload)

    def write_iteration_start(self, iteration: int) -> None:
        self.write_event("iteration_start", {"iteration": iteration})

    def write_message(self, message: Message) -> None:
        self.write_event("message", _message_to_dict(message))

    def write_model_response(
        self, iteration: int, response: CompletionResponse,
    ) -> None:
        self.write_event(
            "model_response",
            {
                "iteration": iteration,
                "content": response.content,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in response.tool_calls
                ],
                "finish_reason": response.finish_reason,
                "usage": _usage_to_dict(response.usage),
                "provider": response.provider,
                "model": response.model,
            },
        )

    def write_tool_call(
        self,
        iteration: int,
        call: ToolCall,
        result: str,
        error: str | None,
    ) -> None:
        self.write_event(
            "tool_call",
            {
                "iteration": iteration,
                "call": {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                },
                "result": result,
                "error": error,
            },
        )

    def write_stop(self, result: LoopResult) -> None:
        self.write_event(
            "stop",
            {
                "stop_reason": result.stop_reason,
                "final_message": result.final_message,
                "iterations": result.iterations,
                "usage": _usage_to_dict(result.usage),
                "detail": result.detail,
            },
        )


def _json_default(obj: Any) -> Any:
    """Catch-all JSON serializer for types ``json`` rejects by default."""
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "isoformat"):  # datetime/date
        return obj.isoformat()
    raise TypeError(f"not JSON-serialisable: {type(obj).__name__}")


# ── Observer hook into run_loop ──────────────────────────────────────────


class JsonlObserver(LoopObserver):
    """Persist every ``run_loop`` event to a ``SessionStore``.

    Plug into run_loop like:

        store = SessionStore.create()
        observer = JsonlObserver(
            store,
            session_metadata={"task": task, "model": "openrouter/..."},
        )
        result = run_loop(..., observer=observer)
        store.close()

    The observer emits ``session_start`` on first ``on_iteration_start``
    (we can't intercept a pre-iteration hook without changing the loop
    API); pass ``emit_session_start=False`` if the caller wants to
    emit it manually BEFORE starting the run (e.g. to capture task
    config that the observer does not have visibility into).
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        session_metadata: dict[str, Any] | None = None,
        emit_session_start: bool = True,
        persisted_message_count: int = 0,
    ) -> None:
        if persisted_message_count < 0:
            raise ValueError("persisted_message_count must be >= 0")
        self._store = store
        self._metadata = dict(session_metadata or {})
        self._emit_session_start = emit_session_start
        self._persisted_message_count = persisted_message_count
        self._session_started = False
        # Track previous-iteration message count so we only persist
        # messages appended this iteration (not the full snapshot).
        self._last_message_count = 0

    def _emit_start_if_needed(self, messages: list[Message]) -> None:
        if self._session_started:
            self._session_started = True
            return
        if not self._emit_session_start:
            self._last_message_count = min(self._persisted_message_count, len(messages))
            self._session_started = True
            return
        payload = dict(self._metadata)
        # Capture the seed conversation (system prompt + task + any
        # resumed messages) in the session_start event so a reader
        # can reconstruct the prior state without grepping messages.
        payload["seed_messages"] = [_message_to_dict(m) for m in messages]
        self._store.write_session_start(payload)
        # Persist each seed message as its own message event so
        # load_session()'s replay path produces the full conversation.
        for msg in messages:
            self._store.write_message(msg)
        self._last_message_count = len(messages)
        self._session_started = True

    # ── LoopObserver surface ─────────────────────────────────────────────

    def on_iteration_start(self, iteration: int, messages: list[Message]) -> None:
        self._emit_start_if_needed(messages)
        self._store.write_iteration_start(iteration)
        # Flush any messages appended since the previous hook (in
        # practice only the first iteration has pre-existing messages
        # that weren't recorded; subsequent iterations append through
        # on_model_response + on_tool_call).
        new_msgs = messages[self._last_message_count:]
        for msg in new_msgs:
            self._store.write_message(msg)
        self._last_message_count = len(messages)

    def on_model_response(
        self, iteration: int, response: CompletionResponse,
    ) -> None:
        self._store.write_model_response(iteration, response)
        # The loop appends an assistant Message directly after — we
        # mirror that here so load_session replay has the full list.
        assistant = Message(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls,
        )
        self._store.write_message(assistant)
        self._last_message_count += 1

    def on_tool_call(
        self,
        iteration: int,
        call: ToolCall,
        result: str,
        error: str | None,
    ) -> None:
        self._store.write_tool_call(iteration, call, result, error)
        tool_msg = Message(
            role="tool",
            content=result if error is None else f"ERROR: {error}",
            tool_call_id=call.id,
            name=call.name,
        )
        self._store.write_message(tool_msg)
        self._last_message_count += 1

    def on_stop(self, result: LoopResult) -> None:
        self._store.write_stop(result)


# ── Reader / replay ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplayState:
    """What ``load_session`` returns.

    ``messages`` is the reconstructed conversation — pass it as
    ``run_loop(messages=...)`` to resume. ``metadata`` is whatever the
    original ``session_start`` recorded (task, model, provider, ...).
    ``stopped`` is True when the session ended normally; False when
    the JSONL trails off mid-run (crash).
    """

    session_id: str
    path: Path
    messages: tuple[Message, ...]
    metadata: dict[str, Any]
    stopped: bool
    stop_reason: str | None
    event_count: int


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each valid JSON object from a JSONL file. Malformed lines skipped."""
    with _open_regular_text(path, read=True) as fh:
        for line_no, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                _logger.warning(
                    "session %s line %d: dropping malformed JSONL (%s)",
                    path.name, line_no, exc,
                )
                continue


def load_session(
    session_id: str,
    sessions_dir: Path | None = None,
) -> ReplayState:
    """Read a session file and reconstruct its conversation state.

    The replay walks every ``message`` event in order — this is the
    single source of truth for "what did the conversation look like".
    Metadata comes from the first ``session_start`` event (if any).
    """
    sdir = sessions_dir if sessions_dir is not None else default_sessions_dir()
    path = sdir / f"{_safe_session_id(session_id)}.jsonl"
    _reject_symlink(sdir, "sessions directory")
    _reject_symlink(path, "session log")
    if not path.is_file():
        raise FileNotFoundError(f"session log not found: {path}")

    messages: list[Message] = []
    metadata: dict[str, Any] = {}
    stopped = False
    stop_reason: str | None = None
    event_count = 0

    for event in _iter_events(path):
        event_count += 1
        etype = event.get("type")
        if etype == "session_start":
            metadata = {
                k: v for k, v in event.items()
                if k not in ("type", "ts", "session_id", "seed_messages")
            }
        elif etype == "message":
            try:
                messages.append(_dict_to_message(event))
            except (TypeError, ValueError) as exc:
                _logger.warning(
                    "session %s: dropping malformed message event: %s",
                    session_id, exc,
                )
        elif etype == "stop":
            stopped = True
            stop_reason = event.get("stop_reason")

    return ReplayState(
        session_id=session_id,
        path=path,
        messages=tuple(_repair_unresolved_tool_call_tail(
            messages, session_id=session_id,
        )),
        metadata=metadata,
        stopped=stopped,
        stop_reason=stop_reason,
        event_count=event_count,
    )


def list_sessions(sessions_dir: Path | None = None) -> list[str]:
    """Return all session ids in the sessions dir, sorted alphabetically."""
    sdir = sessions_dir if sessions_dir is not None else default_sessions_dir()
    if not sdir.is_dir() or sdir.is_symlink():
        return []
    ids: list[str] = []
    for entry in sdir.iterdir():
        if not entry.is_symlink() and entry.is_file() and entry.suffix == ".jsonl":
            ids.append(entry.stem)
    return sorted(ids)
