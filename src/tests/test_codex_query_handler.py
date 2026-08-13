from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

import ctx.adapters.codex.hook_handler as handler
from ctx.core.install_policy_store import persist_install_policy
from ctx.engine.installation import InstallConsentPolicy
from ctx.runtime.query_delivery import SensitiveQueryInput


class _Permit:
    def emit_once(self, stream: io.BytesIO) -> None:
        stream.write(b'{"codex":true}\n')


class _Report:
    emission_permit = _Permit()


class _Controller:
    def __init__(self) -> None:
        self.requests: list[SensitiveQueryInput] = []

    def issue(self, request: SensitiveQueryInput) -> _Report:
        self.requests.append(request)
        return _Report()


def _payload(tmp_path: Path) -> dict[str, object]:
    return {
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5.6-sol",
        "permission_mode": "default",
        "prompt": "Fix the Python tests",
        "session_id": "session-secret",
        "transcript_path": None,
        "turn_id": "turn-1",
    }


def _stdin(value: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(value).encode("utf-8"))


def test_codex_handler_accepts_exact_user_prompt_submit_and_emits_once(tmp_path: Path) -> None:
    controller = _Controller()
    output = io.BytesIO()

    result = handler.run_hook(
        stdin=_stdin(_payload(tmp_path)),
        stdout=output,
        environment={},
        controller_factory=lambda _environment: controller,
    )

    assert result == 0
    assert output.getvalue() == b'{"codex":true}\n'
    assert len(controller.requests) == 1
    request = controller.requests[0]
    assert request.native_session_id == "session-secret"
    assert request.logical_prompt_id == "turn-1"
    assert request.workspace == tmp_path
    assert request.prompt == "Fix the Python tests"


@pytest.mark.parametrize(
    "mutation",
    [
        "malformed-json",
        "wrong-event",
        "missing-turn",
        "unknown-field",
        "wrong-type",
        "oversized",
    ],
)
def test_codex_handler_fails_soft_without_calling_engine(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload = _payload(tmp_path)
    raw: io.BytesIO
    if mutation == "malformed-json":
        raw = io.BytesIO(b"{")
    elif mutation == "wrong-event":
        payload["hook_event_name"] = "PreToolUse"
        raw = _stdin(payload)
    elif mutation == "missing-turn":
        del payload["turn_id"]
        raw = _stdin(payload)
    elif mutation == "unknown-field":
        payload["future"] = True
        raw = _stdin(payload)
    elif mutation == "wrong-type":
        payload["prompt"] = 123
        raw = _stdin(payload)
    else:
        raw = io.BytesIO(b" " * (handler.MAX_STDIN_BYTES + 1))
    controller = _Controller()
    output = io.BytesIO()

    result = handler.run_hook(
        stdin=raw,
        stdout=output,
        environment={},
        controller_factory=lambda _environment: controller,
    )

    assert result == 0
    assert output.getvalue() == b""
    assert controller.requests == []


def test_codex_handler_controller_or_emission_failure_is_silent(tmp_path: Path) -> None:
    class Exploding:
        def issue(self, _request: SensitiveQueryInput) -> object:
            raise RuntimeError("token=secret /private/path")

    output = io.BytesIO()

    assert (
        handler.run_hook(
            stdin=_stdin(_payload(tmp_path)),
            stdout=output,
            environment={},
            controller_factory=lambda _environment: Exploding(),
        )
        == 0
    )
    assert output.getvalue() == b""


def test_codex_handler_activate_uses_the_real_shared_delivery_controller(
    tmp_path: Path,
) -> None:
    output = io.BytesIO()
    state_root = tmp_path / "codex-state"

    result = handler.run_hook(
        stdin=_stdin(_payload(tmp_path)),
        stdout=output,
        environment={
            "CTX_ENGINE_MODE": "activate",
            "CTX_QUERY_DELIVERY_ROOT": str(state_root),
        },
        controller_factory=handler._open_controller,
    )

    assert result == 0
    assert b"# ctx Python Testing" in output.getvalue()
    assert b'"hookEventName":"UserPromptSubmit"' in output.getvalue()
    persisted = b"\n".join(path.read_bytes() for path in state_root.rglob("*") if path.is_file())
    assert b"# ctx Python Testing" not in persisted


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_codex_handler_manage_auto_installs_and_loads_relevant_skill(tmp_path: Path) -> None:
    output = io.BytesIO()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "codex-state"
    policy_root = tmp_path / "onboarding-policy"
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        policy_root,
    )
    payload = _payload(workspace)
    payload["prompt"] = "repair nested Python context manager state restoration"

    result = handler.run_hook(
        stdin=_stdin(payload),
        stdout=output,
        environment={
            "CTX_ENGINE_MODE": "manage",
            "CTX_INSTALL_POLICY_ROOT": str(policy_root),
            "CTX_QUERY_DELIVERY_ROOT": str(state_root),
        },
        controller_factory=handler._open_controller,
    )

    assert result == 0
    assert b"# ctx Python State and Protocols" in output.getvalue()
    assert b'"hookEventName":"UserPromptSubmit"' in output.getvalue()


def test_codex_handler_does_not_treat_experiment_string_as_authority(
    tmp_path: Path,
) -> None:
    output = io.BytesIO()
    state_root = tmp_path / "codex-experiment-state"

    result = handler.run_hook(
        stdin=_stdin(_payload(tmp_path)),
        stdout=output,
        environment={
            "CTX_ENGINE_MODE": "experiment",
            "CTX_QUERY_DELIVERY_ROOT": str(state_root),
        },
        controller_factory=handler._open_controller,
    )

    assert result == 0
    assert output.getvalue() == b""
    assert not state_root.exists()
