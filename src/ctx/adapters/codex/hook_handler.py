"""Executable Codex ``UserPromptSubmit`` adapter for the CTX query engine."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import BinaryIO

from ctx.adapters.query_hook_io import (
    ControllerFactory,
    MAX_STDIN_BYTES,
    bounded_text,
    emit_report,
    existing_workspace,
    open_query_delivery_controller,
    read_bounded_json_object,
)
from ctx.runtime.query_decision import QueryHostDescriptor
from ctx.runtime.query_delivery import QueryDeliveryController, SensitiveQueryInput


_REQUIRED_FIELDS = frozenset(
    {
        "cwd",
        "hook_event_name",
        "model",
        "permission_mode",
        "prompt",
        "session_id",
        "transcript_path",
        "turn_id",
    }
)
_OPTIONAL_FIELDS = frozenset({"agent_id", "agent_type"})
_PERMISSION_MODES = frozenset({"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"})


def _open_controller(environment: Mapping[str, str]) -> QueryDeliveryController:
    return open_query_delivery_controller(
        host=QueryHostDescriptor.codex(),
        environment=environment,
    )


def _parse_request(value: Mapping[str, object]) -> SensitiveQueryInput | None:
    if set(value) - _OPTIONAL_FIELDS != _REQUIRED_FIELDS:
        return None
    if value.get("hook_event_name") != "UserPromptSubmit":
        return None
    workspace = existing_workspace(value.get("cwd"))
    session_id = bounded_text(value.get("session_id"), maximum_bytes=4_096)
    turn_id = bounded_text(value.get("turn_id"), maximum_bytes=4_096)
    prompt = bounded_text(value.get("prompt"), maximum_bytes=256 * 1024, allow_empty=True)
    model = bounded_text(value.get("model"), maximum_bytes=256)
    permission_mode = value.get("permission_mode")
    transcript = value.get("transcript_path")
    if (
        workspace is None
        or session_id is None
        or turn_id is None
        or prompt is None
        or model is None
        or permission_mode not in _PERMISSION_MODES
        or (
            transcript is not None
            and bounded_text(transcript, maximum_bytes=4_096, allow_empty=True) is None
        )
    ):
        return None
    for field_name in _OPTIONAL_FIELDS & set(value):
        if bounded_text(value[field_name], maximum_bytes=4_096) is None:
            return None
    return SensitiveQueryInput(
        native_session_id=session_id,
        logical_prompt_id=turn_id,
        workspace=workspace,
        prompt=prompt,
        language="",
    )


def run_hook(
    *,
    stdin: BinaryIO,
    stdout: BinaryIO,
    environment: Mapping[str, str],
    controller_factory: ControllerFactory = _open_controller,
) -> int:
    try:
        value = read_bounded_json_object(stdin)
        if value is None:
            return 0
        request = _parse_request(value)
        if request is None:
            return 0
        controller = controller_factory(environment)
        emit_report(controller.issue(request), stdout)
    except Exception:
        return 0
    return 0


def main() -> int:
    return run_hook(
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
        environment=os.environ,
    )


if __name__ == "__main__":  # pragma: no cover - exercised through packaged subprocess tests
    raise SystemExit(main())


__all__ = ["MAX_STDIN_BYTES", "main", "run_hook"]
