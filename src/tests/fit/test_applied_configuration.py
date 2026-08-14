from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

import ctx.cli.run as run_cli
from ctx.adapters.generic.loop import TurnPreparation
from ctx.adapters.generic.providers import Usage
from ctx.fit.applied_configuration import (
    APPLIED_CONFIGURATION_PATH,
    AppliedConfigurationError,
    load_applied_configuration,
    load_applied_configuration_for_path,
)
from ctx.fit.candidates import (
    CapabilityMaterial,
    CandidateConfiguration,
    InstructionMaterial,
    render_candidate_user_context,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate(
    *, capability_body: str = "# Focus\n\nUse the narrow test first.\n"
) -> CandidateConfiguration:
    return CandidateConfiguration(
        candidate_id="lean",
        role="lean",
        capability_ids=("skill:focused-testing",),
        model="openai/gpt-5.5",
        instructions=("AGENTS.md",),
        selection_reason="private selection reason that must not become model context",
        evidence=("private ranking evidence",),
        capability_materials=(
            CapabilityMaterial.from_content(
                capability_id="skill:focused-testing",
                delivery_mode="task-user-context",
                source_identity="package:catalog#skill:focused-testing",
                catalog_entry_digest=_digest("catalog entry"),
                content=capability_body,
            ),
        ),
        instruction_materials=(
            InstructionMaterial.from_content(
                path="AGENTS.md",
                content="# Repository rules\n\nPreserve public behavior.\n",
            ),
        ),
    )


def _payload(candidate: CandidateConfiguration | None = None) -> dict[str, object]:
    selected = candidate or _candidate()
    return {
        "schema": "ctx.fit.applied-configuration-v1",
        "configuration_hash": selected.configuration_hash,
        "candidate": selected.to_dict(),
    }


def _write_manifest(repo: Path, payload: dict[str, object]) -> Path:
    target = repo / APPLIED_CONFIGURATION_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_absent_manifest_has_no_applied_configuration(tmp_path: Path) -> None:
    assert load_applied_configuration(tmp_path) is None


def test_valid_manifest_loads_only_exact_candidate_user_context(tmp_path: Path) -> None:
    candidate = _candidate()
    _write_manifest(tmp_path, _payload(candidate))

    loaded = load_applied_configuration(tmp_path)

    assert loaded is not None
    assert loaded.model == candidate.model
    assert loaded.configuration_hash == candidate.configuration_hash
    assert loaded.user_context == render_candidate_user_context(candidate)
    assert candidate.instruction_materials[0].content in loaded.user_context
    assert candidate.capability_materials[0].content in loaded.user_context
    assert candidate.selection_reason not in loaded.user_context
    assert candidate.evidence[0] not in loaded.user_context


def test_applied_configuration_is_found_from_a_repository_subdirectory(tmp_path: Path) -> None:
    candidate = _candidate()
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    _write_manifest(tmp_path, _payload(candidate))

    loaded = load_applied_configuration_for_path(nested)

    assert loaded is not None
    assert loaded.configuration_hash == candidate.configuration_hash


def test_applied_configuration_search_does_not_cross_a_nested_repository_boundary(
    tmp_path: Path,
) -> None:
    _write_manifest(tmp_path, _payload())
    nested = tmp_path / "nested"
    (nested / ".git").mkdir(parents=True)
    child = nested / "src"
    child.mkdir()

    assert load_applied_configuration_for_path(child) is None


def test_nested_sidecar_cannot_shadow_the_repository_root_applied_authority(
    tmp_path: Path,
) -> None:
    root_candidate = _candidate(capability_body="# Root winner\n")
    nested_candidate = _candidate(capability_body="# Nested shadow\n")
    (tmp_path / ".git").mkdir()
    _write_manifest(tmp_path, _payload(root_candidate))
    nested = tmp_path / "src"
    nested.mkdir()
    _write_manifest(nested, _payload(nested_candidate))

    loaded = load_applied_configuration_for_path(nested)

    assert loaded is not None
    assert loaded.configuration_hash == root_candidate.configuration_hash


def test_same_capability_id_with_different_body_loads_different_context(tmp_path: Path) -> None:
    first = _candidate(capability_body="# Focus\n\nFirst body.\n")
    second = _candidate(capability_body="# Focus\n\nSecond body.\n")
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    _write_manifest(first_repo, _payload(first))
    _write_manifest(second_repo, _payload(second))

    first_loaded = load_applied_configuration(first_repo)
    second_loaded = load_applied_configuration(second_repo)

    assert first_loaded is not None
    assert second_loaded is not None
    assert first_loaded.configuration_hash != second_loaded.configuration_hash
    assert first_loaded.user_context != second_loaded.user_context


def _mutate(payload: dict[str, object], case: str) -> None:
    candidate = payload["candidate"]
    assert isinstance(candidate, dict)
    capabilities = candidate["capability_materials"]
    instructions = candidate["instruction_materials"]
    assert isinstance(capabilities, list) and isinstance(capabilities[0], dict)
    assert isinstance(instructions, list) and isinstance(instructions[0], dict)
    capability = capabilities[0]
    instruction = instructions[0]
    mutations: dict[str, Any] = {
        "root-schema": lambda: payload.__setitem__("schema", "ctx.fit.other-v1"),
        "candidate-schema": lambda: candidate.__setitem__("schema", "ctx.fit.other-v1"),
        "outer-hash": lambda: payload.__setitem__("configuration_hash", "0" * 64),
        "candidate-hash": lambda: candidate.__setitem__("configuration_hash", "0" * 64),
        "unpinned-model": lambda: candidate.__setitem__("model", None),
        "capability-id-alignment": lambda: candidate.__setitem__(
            "capability_ids", ["skill:different"]
        ),
        "capability-delivery": lambda: capability.__setitem__("delivery_mode", "system"),
        "capability-source": lambda: capability.__setitem__(
            "source_identity", "package:catalog#skill:different"
        ),
        "capability-content": lambda: capability.__setitem__("content", "tampered"),
        "capability-bytes": lambda: capability.__setitem__("content_bytes", 999),
        "capability-digest": lambda: capability.__setitem__("content_sha256", "0" * 64),
        "catalog-digest": lambda: capability.__setitem__("catalog_entry_digest", "bad"),
        "instruction-path-alignment": lambda: candidate.__setitem__("instructions", ["CLAUDE.md"]),
        "instruction-delivery": lambda: instruction.__setitem__("delivery_mode", "system"),
        "instruction-source": lambda: instruction.__setitem__(
            "source_identity", "repository:CLAUDE.md"
        ),
        "instruction-content": lambda: instruction.__setitem__("content", "tampered"),
        "instruction-bytes": lambda: instruction.__setitem__("content_bytes", 999),
        "instruction-digest": lambda: instruction.__setitem__("content_sha256", "0" * 64),
        "encoding": lambda: instruction.__setitem__("encoding", "utf-16"),
        "unknown-field": lambda: candidate.__setitem__("ignored", "authority"),
    }
    mutations[case]()


@pytest.mark.parametrize(
    "case",
    (
        "root-schema",
        "candidate-schema",
        "outer-hash",
        "candidate-hash",
        "unpinned-model",
        "capability-id-alignment",
        "capability-delivery",
        "capability-source",
        "capability-content",
        "capability-bytes",
        "capability-digest",
        "catalog-digest",
        "instruction-path-alignment",
        "instruction-delivery",
        "instruction-source",
        "instruction-content",
        "instruction-bytes",
        "instruction-digest",
        "encoding",
        "unknown-field",
    ),
)
def test_manifest_tampering_is_rejected(tmp_path: Path, case: str) -> None:
    payload = _payload()
    _mutate(payload, case)
    _write_manifest(tmp_path, payload)

    with pytest.raises(AppliedConfigurationError):
        load_applied_configuration(tmp_path)


def test_manifest_symlink_is_rejected(tmp_path: Path) -> None:
    external = tmp_path / "external.json"
    external.write_text(json.dumps(_payload()), encoding="utf-8")
    target = tmp_path / APPLIED_CONFIGURATION_PATH
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(external)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(AppliedConfigurationError, match="symbolic link"):
        load_applied_configuration(tmp_path)


def test_manifest_fifo_is_rejected_without_waiting_for_a_writer(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable on this platform")
    target = tmp_path / APPLIED_CONFIGURATION_PATH
    target.parent.mkdir(parents=True)
    os.mkfifo(target)

    with pytest.raises(AppliedConfigurationError, match="not a regular file"):
        load_applied_configuration(tmp_path)


def test_duplicate_json_field_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / APPLIED_CONFIGURATION_PATH
    target.parent.mkdir(parents=True)
    target.write_text(
        '{"schema":"ctx.fit.applied-configuration-v1",'
        '"schema":"ctx.fit.applied-configuration-v1",'
        '"configuration_hash":"ignored","candidate":{}}',
        encoding="utf-8",
    )

    with pytest.raises(AppliedConfigurationError, match="duplicate JSON field"):
        load_applied_configuration(tmp_path)


def _run_args(repo: Path, sessions: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--task",
        "fix it",
        "--sessions-dir",
        str(sessions),
        "--no-ctx-tools",
        "--quiet",
        "--json",
        *extra,
    ]


def _completed_result() -> Any:
    return type(
        "Result",
        (),
        {
            "stop_reason": "completed",
            "final_message": "done",
            "iterations": 1,
            "usage": Usage(input_tokens=1, output_tokens=1),
            "messages": (),
            "detail": "",
        },
    )()


def test_ctx_run_uses_pinned_model_and_prepares_exact_applied_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    sessions = tmp_path / "sessions"
    candidate = _candidate()
    _write_manifest(repo, _payload(candidate))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(run_cli, "_load_model_profile", lambda: {})
    provider_models: list[str | None] = []
    prepared: list[TurnPreparation] = []

    def fake_provider(**kwargs: Any) -> object:
        provider_models.append(kwargs.get("default_model"))
        return object()

    def fake_run_loop(**kwargs: Any) -> Any:
        controller = kwargs["turn_controller"]
        preparation = controller.prepare_turn(
            1,
            (),
            (),
            deadline_monotonic=None,
            cancel_event=None,
        )
        prepared.append(preparation)
        controller.on_provider_request(1, preparation.capability_epoch)
        return _completed_result()

    monkeypatch.setattr(run_cli, "get_provider", fake_provider)
    monkeypatch.setattr(run_cli, "run_loop", fake_run_loop)

    assert run_cli.main(_run_args(repo, sessions)) == 0
    assert provider_models == [candidate.model]
    assert prepared[0].ephemeral_user_context == (render_candidate_user_context(candidate),)


def test_ctx_run_from_repository_subdirectory_activates_only_root_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    root_candidate = _candidate(capability_body="# Root winner\n")
    nested_candidate = _candidate(capability_body="# Nested shadow\n")
    _write_manifest(repo, _payload(root_candidate))
    _write_manifest(nested, _payload(nested_candidate))
    monkeypatch.chdir(nested)
    monkeypatch.setattr(run_cli, "_load_model_profile", lambda: {})
    provider_models: list[str | None] = []
    prepared: list[TurnPreparation] = []

    def fake_provider(**kwargs: Any) -> object:
        provider_models.append(kwargs.get("default_model"))
        return object()

    def fake_run_loop(**kwargs: Any) -> Any:
        controller = kwargs["turn_controller"]
        preparation = controller.prepare_turn(
            1,
            (),
            (),
            deadline_monotonic=None,
            cancel_event=None,
        )
        prepared.append(preparation)
        controller.on_provider_request(1, preparation.capability_epoch)
        return _completed_result()

    monkeypatch.setattr(run_cli, "get_provider", fake_provider)
    monkeypatch.setattr(run_cli, "run_loop", fake_run_loop)

    assert run_cli.main(_run_args(repo, tmp_path / "sessions")) == 0
    assert provider_models == [root_candidate.model]
    assert prepared[0].ephemeral_user_context == (render_candidate_user_context(root_candidate),)


def test_applied_context_composes_after_existing_engine_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    candidate = _candidate()
    _write_manifest(repo, _payload(candidate))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(run_cli, "_load_model_profile", lambda: {})
    monkeypatch.setattr(run_cli, "get_provider", lambda **_kwargs: object())
    monkeypatch.setattr(
        run_cli,
        "_prepare_ctx_engine_for_run",
        lambda **_kwargs: ({"effective_mode": "recommend"}, "existing engine context"),
    )
    prepared: list[TurnPreparation] = []

    def fake_run_loop(**kwargs: Any) -> Any:
        controller = kwargs["turn_controller"]
        preparation = controller.prepare_turn(
            1,
            (),
            (),
            deadline_monotonic=None,
            cancel_event=None,
        )
        prepared.append(preparation)
        controller.on_provider_request(1, preparation.capability_epoch)
        return _completed_result()

    monkeypatch.setattr(run_cli, "run_loop", fake_run_loop)

    assert run_cli.main(_run_args(repo, tmp_path / "sessions")) == 0
    assert prepared[0].ephemeral_user_context == (
        "existing engine context",
        render_candidate_user_context(candidate),
    )


def test_fit_controlled_trial_suppresses_ambient_applied_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    candidate = _candidate()
    _write_manifest(repo, _payload(candidate))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(run_cli, "_load_model_profile", lambda: {})
    monkeypatch.setattr(run_cli, "get_provider", lambda **_kwargs: object())
    observed: list[object] = []

    def fake_run_loop(**kwargs: Any) -> Any:
        observed.append(kwargs["turn_controller"])
        return _completed_result()

    monkeypatch.setattr(run_cli, "run_loop", fake_run_loop)
    assert candidate.model is not None

    assert (
        run_cli.main(
            _run_args(
                repo,
                tmp_path / "sessions",
                "--fit-controlled-trial",
                "--model",
                candidate.model,
            )
        )
        == 0
    )
    assert observed == [None]


def test_ctx_run_refuses_explicit_model_conflict_before_provider_or_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    sessions = tmp_path / "sessions"
    _write_manifest(repo, _payload())
    monkeypatch.chdir(repo)
    monkeypatch.setattr(run_cli, "_load_model_profile", lambda: {})
    monkeypatch.setattr(
        run_cli,
        "get_provider",
        lambda **_kwargs: pytest.fail("provider must not be constructed"),
    )
    assert run_cli.main(_run_args(repo, sessions, "--model", "anthropic/claude-conflict")) == 2
    assert not sessions.exists()


def test_applied_model_overrides_a_conflicting_saved_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    sessions = tmp_path / "sessions"
    candidate = _candidate()
    _write_manifest(repo, _payload(candidate))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        run_cli,
        "_load_model_profile",
        lambda: {
            "model": "anthropic/claude-conflict",
            "provider": "anthropic",
            "api_key_env": "ANTHROPIC_API_KEY",
            "base_url": "https://profile.invalid",
        },
    )
    provider_arguments: list[dict[str, Any]] = []

    def fake_provider(**kwargs: Any) -> object:
        provider_arguments.append(kwargs)
        return object()

    monkeypatch.setattr(run_cli, "get_provider", fake_provider)
    monkeypatch.setattr(run_cli, "run_loop", lambda **_kwargs: _completed_result())

    assert run_cli.main(_run_args(repo, sessions)) == 0
    assert provider_arguments == [
        {
            "default_model": candidate.model,
            "base_url": None,
            "api_key_env": "OPENAI_API_KEY",
            "timeout": 120.0,
        }
    ]
    assert sessions.exists()


def test_invalid_present_manifest_refuses_before_provider_or_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    sessions = tmp_path / "sessions"
    payload = _payload()
    _mutate(payload, "capability-content")
    _write_manifest(repo, payload)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        run_cli,
        "get_provider",
        lambda **_kwargs: pytest.fail("provider must not be constructed"),
    )

    assert run_cli.main(_run_args(repo, sessions)) == 2
    assert not sessions.exists()


def test_ctx_run_without_manifest_preserves_no_applied_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = tmp_path / "sessions"
    monkeypatch.chdir(repo)
    prepared_controllers: list[object | None] = []
    monkeypatch.setattr(run_cli, "get_provider", lambda **_kwargs: object())

    def fake_run_loop(**kwargs: Any) -> Any:
        prepared_controllers.append(kwargs["turn_controller"])
        return _completed_result()

    monkeypatch.setattr(run_cli, "run_loop", fake_run_loop)

    assert run_cli.main(_run_args(repo, sessions, "--model", "openai/gpt-5.5")) == 0
    assert prepared_controllers == [None]
