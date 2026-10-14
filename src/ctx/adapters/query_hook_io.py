"""Shared fail-soft I/O primitives for native ``UserPromptSubmit`` adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, Protocol

from ctx.runtime.query_decision import QueryHostDescriptor
from ctx.runtime.query_delivery import SensitiveQueryInput
from ctx.runtime.user_recommendations import UserKnowledgeQueryController


MAX_STDIN_BYTES = 512 * 1024


class QueryController(Protocol):
    def issue(self, request: SensitiveQueryInput) -> object: ...


ControllerFactory = Callable[[Mapping[str, str]], QueryController]


def read_bounded_json_object(stdin: BinaryIO) -> dict[str, object] | None:
    raw = stdin.read(MAX_STDIN_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > MAX_STDIN_BYTES:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def bounded_text(
    value: object,
    *,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> str | None:
    if type(value) is not str or (not allow_empty and not value):
        return None
    try:
        if len(value.encode("utf-8")) > maximum_bytes:
            return None
    except UnicodeEncodeError:
        return None
    return value


def existing_workspace(value: object) -> Path | None:
    raw = bounded_text(value, maximum_bytes=4_096)
    if raw is None or "\x00" in raw:
        return None
    path = Path(raw)
    try:
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():
            return None
    except OSError:
        return None
    return path


def open_query_delivery_controller(
    *,
    host: QueryHostDescriptor,
    environment: Mapping[str, str],
) -> UserKnowledgeQueryController:
    if host not in {QueryHostDescriptor.codex(), QueryHostDescriptor.claude_code()}:
        raise ValueError("native recommendations support Codex and Claude Code")
    return UserKnowledgeQueryController(environment)


def emit_report(report: object, stdout: BinaryIO) -> None:
    permit = getattr(report, "emission_permit", None)
    if permit is not None:
        permit.emit_once(stdout)


_FILE_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".cs": "csharp",
    ".go": "go",
    ".java": "java",
    ".php": "php",
    ".rs": "rust",
}


def emit_tool_observation(
    value: Mapping[str, object],
    environment: Mapping[str, str],
    stdout: BinaryIO,
) -> None:
    """Use only declared file-path metadata, never tool output/source/commands."""
    workspace = existing_workspace(value.get("cwd"))
    session_id = bounded_text(value.get("session_id"), maximum_bytes=4_096)
    tool_name = bounded_text(value.get("tool_name"), maximum_bytes=256)
    tool_input = value.get("tool_input")
    if workspace is None or session_id is None or tool_name is None:
        return
    if not isinstance(tool_input, dict):
        return
    project = environment.get("CLAUDE_PROJECT_DIR")
    if project and existing_workspace(project) != workspace:
        return
    languages: set[str] = set()
    for key in ("file_path", "path", "filename"):
        file_path = bounded_text(tool_input.get(key), maximum_bytes=4_096)
        if file_path is not None:
            language = _FILE_LANGUAGES.get(Path(file_path).suffix.lower())
            if language:
                languages.add(language)
    if not languages:
        return
    query = " ".join(sorted(languages))
    request = SensitiveQueryInput(
        native_session_id=session_id,
        logical_prompt_id="file-language-observation",
        workspace=workspace,
        prompt=query,
        language=next(iter(languages)) if len(languages) == 1 else "",
    )
    emit_report(
        UserKnowledgeQueryController(environment).issue(
            request,
            hook_event="PostToolUse",
        ),
        stdout,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "ControllerFactory",
    "MAX_STDIN_BYTES",
    "QueryController",
    "bounded_text",
    "emit_report",
    "existing_workspace",
    "open_query_delivery_controller",
    "read_bounded_json_object",
]
