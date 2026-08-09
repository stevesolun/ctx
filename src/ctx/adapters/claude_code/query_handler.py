"""Executable Claude Code ``UserPromptSubmit`` adapter for the CTX query engine."""

from __future__ import annotations

import hashlib
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
from ctx.runtime.workspace_identity import capture_workspace_identity


def _open_controller(environment: Mapping[str, str]) -> QueryDeliveryController:
    return open_query_delivery_controller(
        host=QueryHostDescriptor.claude_code(),
        environment=environment,
    )


def _parse_request(
    value: Mapping[str, object],
    environment: Mapping[str, str],
) -> SensitiveQueryInput | None:
    if value.get("hook_event_name") != "UserPromptSubmit":
        return None
    session_id = bounded_text(value.get("session_id"), maximum_bytes=4_096)
    prompt = bounded_text(value.get("prompt"), maximum_bytes=256 * 1024, allow_empty=True)
    project_root = environment.get("CLAUDE_PROJECT_DIR", "")
    payload_workspace = existing_workspace(value.get("cwd"))
    if project_root:
        workspace = existing_workspace(project_root)
        if (
            workspace is None
            or payload_workspace is None
            or capture_workspace_identity(workspace).digest
            != capture_workspace_identity(payload_workspace).digest
        ):
            return None
    else:
        workspace = payload_workspace
    if session_id is None or prompt is None or workspace is None:
        return None
    for field_name in ("permission_mode", "transcript_path"):
        if field_name in value and value[field_name] is not None:
            if bounded_text(value[field_name], maximum_bytes=4_096, allow_empty=True) is None:
                return None
    return SensitiveQueryInput(
        native_session_id=session_id,
        # Claude Code's documented UserPromptSubmit payload has no turn or
        # prompt identifier. Bind the private invocation to exact prompt bytes
        # without persisting those bytes; the controller HMACs this value
        # before it reaches its digest-only ledger.
        logical_prompt_id=f"prompt-{hashlib.sha256(prompt.encode('utf-8')).hexdigest()}",
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
        request = _parse_request(value, environment)
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
