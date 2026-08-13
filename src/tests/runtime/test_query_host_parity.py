from __future__ import annotations

import io
import json
from pathlib import Path

from ctx.adapters.claude_code import query_handler as claude_handler
from ctx.adapters.codex import hook_handler as codex_handler


def test_native_hosts_emit_byte_identical_authenticated_context(tmp_path: Path) -> None:
    codex_output = io.BytesIO()
    claude_output = io.BytesIO()
    codex_payload = {
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5.6-sol",
        "permission_mode": "default",
        "prompt": "Fix the Python tests",
        "session_id": "codex-parity-session",
        "transcript_path": None,
        "turn_id": "parity-turn",
    }
    claude_payload = {
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "permission_mode": "default",
        "prompt": "Fix the Python tests",
        "session_id": "claude-parity-session",
        "transcript_path": None,
    }

    assert (
        codex_handler.run_hook(
            stdin=io.BytesIO(json.dumps(codex_payload).encode()),
            stdout=codex_output,
            environment={
                "CTX_ENGINE_MODE": "activate",
                "CTX_QUERY_DELIVERY_ROOT": str(tmp_path / "codex-state"),
            },
            controller_factory=codex_handler._open_controller,
        )
        == 0
    )
    assert (
        claude_handler.run_hook(
            stdin=io.BytesIO(json.dumps(claude_payload).encode()),
            stdout=claude_output,
            environment={
                "CTX_ENGINE_MODE": "activate",
                "CTX_QUERY_DELIVERY_ROOT": str(tmp_path / "claude-state"),
            },
            controller_factory=claude_handler._open_controller,
        )
        == 0
    )

    assert codex_output.getvalue() == claude_output.getvalue()
    envelope = json.loads(codex_output.getvalue())
    context = envelope["hookSpecificOutput"]["additionalContext"]
    assert "# ctx Python Testing" in context
    assert "issuance is not evidence of use" in context
