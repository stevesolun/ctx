from __future__ import annotations

import hashlib
import json
import traceback
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from ctx.engine.protocol import EngineEvent, PrivacyLabel, ScopeRef, Transition
from ctx.engine.replay import (
    DEFAULT_REDUCER_VERSION,
    DefaultReplayInputFactory,
    ObservationReference,
    PlanningContext,
    ReplayBindingError,
    ReplayInput,
    ReplayPrivacyError,
    ReplayValidationError,
    StructuredSurrogate,
    UnsupportedReplayEvent,
)
from ctx.engine.store import JournalRecord, StreamId
from ctx.engine.state import EngineState


NOW = "2026-08-01T12:00:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(**overrides: str | None) -> ScopeRef:
    values: dict[str, str | None] = {
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "repository_id": "repository-1",
        "session_id": "session-1",
        "exposure_id": "exposure-1",
        "host_context_id": "host-1",
        "parent_exposure_id": None,
    }
    values.update(overrides)
    return ScopeRef(**values)  # type: ignore[arg-type]


def _event(
    kind: str,
    *,
    payload: Mapping[str, object] | None = None,
    revision: int = 0,
    event_id: str = "event-1",
    scope: ScopeRef | None = None,
    privacy: PrivacyLabel | None = None,
    metadata: bool = True,
) -> EngineEvent:
    replay_metadata: dict[str, Any] = {}
    if metadata:
        replay_metadata = {
            "engine_version": "engine-v1",
            "planner_version": "planner-v1",
            "policy_version": "policy-v1",
            "host_descriptor_digest": _digest("host"),
            "catalog_snapshot_digest": _digest("catalog"),
            "semantic_model_digest": _digest("model"),
            "semantic_index_digest": _digest("index"),
            "work_signature": _digest("work"),
            "random_seed": 17,
        }
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=scope or _scope(),
        expected_revision=revision,
        occurred_at=NOW,
        payload=payload or {},
        privacy=privacy or PrivacyLabel(),
        correlation_id="plan-1",
        causation_id="cause-1",
        **replay_metadata,
    )


def _factory() -> DefaultReplayInputFactory:
    return DefaultReplayInputFactory()


def _planning_factory() -> DefaultReplayInputFactory:
    return DefaultReplayInputFactory(reducer_version="ctx-reducer-v2")


def _prepare(
    factory: DefaultReplayInputFactory,
    event: EngineEvent,
    *,
    decision_surrogate: StructuredSurrogate | None = None,
) -> ReplayInput:
    checked = factory.preflight(event)
    return factory.prepare(checked, None, decision_surrogate=decision_surrogate)


def _current_work(**overrides: object) -> StructuredSurrogate:
    value: dict[str, object] = {
        "signals": ["async", "fastapi", "python"],
        "languages": ["python"],
        "baseline_capability_ids": ["mcp-server:codex-cli"],
        "active_capability_ids": ["skill:pytest"],
        "rejected_capability_ids": ["agent:legacy-reviewer"],
        "requested_limit": 5,
    }
    value.update(overrides)
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value=value,
    )


def _capability(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "capability_id": "skill:fastapi-testing",
        "kind": "skill",
        "name": "fastapi-testing",
        "catalog_entry_digest": _digest("fastapi-testing"),
        "normalized_score_ppm": 910_000,
        "matching_signals": ["fastapi", "python"],
        "reason_codes": ["language-match", "signal-match"],
        "actionability": "manual",
    }
    value.update(overrides)
    return value


def _capability_plan(**overrides: object) -> StructuredSurrogate:
    value: dict[str, object] = {
        "status": "ready",
        "abstention_code": None,
        "capabilities": [_capability()],
    }
    value.update(overrides)
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=1,
        value=value,
    )


def _capability_plan_v2(**overrides: object) -> StructuredSurrogate:
    capability = _capability(install_descriptor_digest=None, install_plan_digest=None)
    value: dict[str, object] = {
        "status": "ready",
        "abstention_code": None,
        "capabilities": [capability],
    }
    value.update(overrides)
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=2,
        value=value,
    )


def _observation_event() -> EngineEvent:
    return _event(
        "DevelopmentObserved",
        payload={
            "observation_ref": {
                "provider_id": "host-buffer",
                "opaque_id": "observation-1",
                "content_digest": _digest("raw buffer"),
            }
        },
    )


def _record(
    replay: ReplayInput,
    *,
    event_id: str | None = None,
    scope: ScopeRef | None = None,
    revision: int | None = None,
    privacy_classification: str | None = None,
    retention_class: str | None = None,
) -> JournalRecord:
    record_scope = scope or replay.reducer_event.scope
    record_revision = revision or replay.reducer_event.expected_revision + 1
    record_event_id = event_id or replay.reducer_event.event_id
    transition = Transition(
        event_id=record_event_id,
        scope=record_scope,
        from_revision=record_revision - 1,
        to_revision=record_revision,
    )
    return JournalRecord(
        stream_id=StreamId.from_scope(record_scope),
        revision=record_revision,
        event_id=record_event_id,
        event_content_digest=replay.source_event_content_digest,
        replay_json=replay.to_json(),
        transition_json=transition.to_json(),
        result_state_json="{}",
        privacy_classification=(
            privacy_classification or replay.reducer_event.privacy.classification
        ),
        retention_class=retention_class or replay.reducer_event.privacy.retention,
        reducer_version=replay.reducer_version,
    )


def test_session_start_is_sanitized_and_replay_codec_is_canonical() -> None:
    source = _event("SessionStarted", payload={}, metadata=True)

    replay = _prepare(_factory(), source)

    assert replay.reducer_event is not source
    assert dict(replay.reducer_event.payload) == {"host_level": "query-only"}
    assert replay.source_event_content_digest == source.content_digest
    assert replay.reducer_version == DEFAULT_REDUCER_VERSION
    assert ReplayInput.from_json(replay.to_json()) == replay
    assert replay.to_json() == json.dumps(
        json.loads(replay.to_json()),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_observing_host_level_is_preserved_by_replay_boundary() -> None:
    replay = _prepare(
        _factory(),
        _event("SessionStarted", payload={"host_level": "observing"}, metadata=True),
    )

    assert dict(replay.reducer_event.payload) == {"host_level": "observing"}


def test_replay_codec_rejects_unknown_duplicate_and_noncanonical_json() -> None:
    replay = _prepare(_factory(), _event("SessionStarted"))
    value = replay.to_dict()
    value["unknown"] = True
    with pytest.raises(ReplayValidationError, match="unknown"):
        ReplayInput.from_dict(value)

    duplicate = replay.to_json()[:-1] + ',"schema_version":1}'
    with pytest.raises(ReplayValidationError, match="duplicate"):
        ReplayInput.from_json(duplicate)

    noncanonical = json.dumps(replay.to_dict(), indent=2)
    with pytest.raises(ReplayValidationError, match="canonical"):
        ReplayInput.from_json(noncanonical)

    nested = replay.to_dict()
    del nested["reducer_event"]["correlation_id"]
    with pytest.raises(ReplayValidationError, match="exact canonical shape"):
        ReplayInput.from_dict(nested)
    with pytest.raises(ReplayValidationError, match="exact canonical shape"):
        ReplayInput.from_json(
            json.dumps(
                nested,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )


def test_replay_codec_binds_source_and_reducer_versions() -> None:
    replay = _prepare(_factory(), _event("SessionStarted"))
    record = _record(replay)
    with pytest.raises(ReplayBindingError, match="source event"):
        replay.assert_record_binding(replace(record, event_content_digest=_digest("different")))

    with pytest.raises(ReplayBindingError, match="reducer version"):
        replay.assert_record_binding(replace(record, reducer_version="other-reducer-v1"))


@pytest.mark.parametrize(
    ("record_factory", "message"),
    [
        (lambda replay: _record(replay, event_id="event-other"), "event id"),
        (
            lambda replay: _record(
                replay,
                scope=_scope(repository_id="repository-other"),
            ),
            "stream",
        ),
        (lambda replay: _record(replay, revision=2), "revision"),
        (
            lambda replay: _record(replay, privacy_classification="confidential"),
            "privacy classification",
        ),
        (lambda replay: _record(replay, retention_class="workspace"), "retention class"),
    ],
)
def test_record_binding_rejects_all_cross_record_mismatches(
    record_factory: object,
    message: str,
) -> None:
    replay = _prepare(_factory(), _event("SessionStarted"))
    assert callable(record_factory)
    with pytest.raises(ReplayBindingError, match=message):
        replay.assert_record_binding(record_factory(replay))


def test_record_binding_accepts_the_exact_journal_record() -> None:
    replay = _prepare(_factory(), _event("SessionStarted"))
    replay.assert_record_binding(_record(replay))


def test_structured_surrogate_is_strict_bounded_and_digest_bound() -> None:
    surrogate = StructuredSurrogate.create(
        schema_id="ctx.observation.languages",
        schema_version=1,
        value={"languages": ["python", "cpp"], "file_count": 12},
    )
    assert StructuredSurrogate.from_json(surrogate.to_json()) == surrogate

    changed = surrogate.to_dict()
    changed["value"] = {"languages": ["rust"]}
    with pytest.raises(ReplayBindingError, match="value_digest"):
        StructuredSurrogate.from_dict(changed)

    with pytest.raises(ReplayPrivacyError, match="safe token"):
        StructuredSurrogate.create(
            schema_id="ctx.observation.intent",
            schema_version=1,
            value={"intent": "print(secret)"},
        )

    nested: object = "leaf"
    for _ in range(12):
        nested = {"next": nested}
    with pytest.raises(ReplayValidationError, match="depth"):
        StructuredSurrogate.create(
            schema_id="ctx.observation.deep",
            schema_version=1,
            value={"root": nested},
        )


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        (
            "ReassessmentRequested",
            {
                "owner_id": "owner-1",
                "desired_capabilities": [
                    {
                        "capability_id": "skill:pytest",
                        "source_digest": _digest("pytest"),
                        "lease_id": "lease-1",
                    }
                ],
            },
            None,
        ),
        (
            "ReassessmentRequested",
            {"retry_failed_deactivations": ["skill:pytest"]},
            None,
        ),
        (
            "ProviderSubmissionObserved",
            {
                "capabilities": [
                    {"capability_id": "skill:pytest", "source_digest": _digest("pytest")}
                ]
            },
            None,
        ),
        (
            "ToolCallObserved",
            {
                "capability_id": "skill:pytest",
                "source_digest": _digest("pytest"),
                "outcome": "succeeded",
            },
            None,
        ),
        ("TurnStarting", {}, {}),
        ("TurnEnded", {}, {}),
        ("SessionEnded", {}, {}),
    ],
)
def test_safe_event_payloads_are_accepted(
    kind: str,
    payload: Mapping[str, object],
    expected: Mapping[str, object] | None,
) -> None:
    replay = _prepare(_factory(), _event(kind, payload=payload))
    if expected is not None:
        assert dict(replay.reducer_event.payload) == expected
    assert replay.reducer_event.to_json() in replay.to_json()


def test_user_decision_keeps_only_typed_consent_identity() -> None:
    payload = {
        "consent_id": "consent-1",
        "decision": "granted",
        "requested_action_id": "action-1",
        "requested_action_kind": "InstallCapability",
        "requested_action_content_digest": _digest("action"),
        "requested_action_precondition_revision": 2,
    }
    replay = _prepare(_factory(), _event("UserDecision", revision=1, payload=payload))
    assert dict(replay.reducer_event.payload) == payload


def test_install_consent_expiry_replay_preserves_exact_machine_binding() -> None:
    payload = {
        "consent_id": "consent-1",
        "policy_snapshot_digest": _digest("policy"),
        "requested_action_id": "action-1",
        "requested_action_kind": "InstallCapability",
        "requested_action_content_digest": _digest("action"),
        "requested_action_precondition_revision": 2,
        "install_expires_at": "2026-08-01T13:00:00Z",
    }
    factory = DefaultReplayInputFactory(reducer_version="ctx-reducer-v3")
    replay = _prepare(
        factory,
        _event("InstallConsentExpired", revision=1, payload=payload),
    )

    decoded = ReplayInput.from_json(replay.to_json())
    assert decoded == replay
    assert dict(decoded.reducer_event.payload) == payload
    assert "decision" not in decoded.reducer_event.payload
    assert "decision_basis" not in decoded.reducer_event.payload


def test_install_consent_expiry_is_not_accepted_by_historical_reducer() -> None:
    payload = {
        "consent_id": "consent-1",
        "policy_snapshot_digest": _digest("policy"),
        "requested_action_id": "action-1",
        "requested_action_kind": "InstallCapability",
        "requested_action_content_digest": _digest("action"),
        "requested_action_precondition_revision": 2,
        "install_expires_at": "2026-08-01T13:00:00Z",
    }

    with pytest.raises(UnsupportedReplayEvent, match="installation reducer"):
        _factory().preflight(_event("InstallConsentExpired", revision=1, payload=payload))


def _receipt_payload(kind: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "action_id": "action-1",
        "action_kind": "ActivateCapability",
        "action_content_digest": _digest("action"),
        "action_precondition_revision": 2,
    }
    if kind == "ActionApplied":
        payload["verification"] = {"host_state": "active"}
    elif kind == "ActionFailed":
        payload["error"] = {"code": "host-failure"}
    else:
        payload["reason"] = "expired"
    return payload


@pytest.mark.parametrize("kind", ["ActionApplied", "ActionFailed", "ActionExpired"])
def test_receipts_preserve_only_the_exact_machine_evidence_schema(kind: str) -> None:
    replay = _prepare(
        _factory(), _event(kind, payload=_receipt_payload(kind), revision=2, metadata=False)
    )
    encoded = replay.to_json()
    for forbidden in ("raw_content", "tool_output", "command", "detail"):
        assert forbidden not in encoded
    if kind == "ActionApplied":
        assert dict(replay.reducer_event.payload)["verification"] == {"host_state": "active"}
    elif kind == "ActionFailed":
        assert dict(replay.reducer_event.payload)["error"] == {"code": "redacted-host-failure"}
        assert "rollback" not in replay.reducer_event.payload
    else:
        assert dict(replay.reducer_event.payload)["reason"] == "expired"


@pytest.mark.parametrize(
    "kind",
    ["WorkspaceObserved", "IntentObserved", "DevelopmentObserved", "ValidationObserved"],
)
def test_raw_observations_are_rejected_before_normalizer_hook(kind: str) -> None:
    calls = 0

    def normalizer(_: ObservationReference, __: EngineState | None) -> StructuredSurrogate:
        nonlocal calls
        calls += 1
        return StructuredSurrogate.create(
            schema_id="ctx.observation.safe",
            schema_version=1,
            value={"kind": "safe"},
        )

    factory = DefaultReplayInputFactory(observation_normalizer=normalizer)
    with pytest.raises(ReplayPrivacyError) as error:
        factory.preflight(
            _event(kind, payload={"prompt": "fix this", "path": "/private/repo/app.py"})
        )
    assert calls == 0
    assert "fix this" not in str(error.value)
    assert "/private/repo/app.py" not in str(error.value)


def test_opaque_observation_reference_can_be_normalized_without_persisting_handle_data() -> None:
    def normalizer(reference: ObservationReference, __: EngineState | None) -> StructuredSurrogate:
        return StructuredSurrogate.create(
            schema_id="ctx.observation.opaque-ref",
            schema_version=1,
            value={
                "provider_id": reference.provider_id,
                "content_digest": reference.content_digest,
            },
        )

    source = _event(
        "DevelopmentObserved",
        payload={
            "observation_ref": {
                "provider_id": "host-buffer",
                "opaque_id": "observation-1",
                "content_digest": _digest("raw buffer"),
            }
        },
    )
    replay = _prepare(DefaultReplayInputFactory(observation_normalizer=normalizer), source)
    assert replay.observation_surrogate is not None
    assert dict(replay.reducer_event.payload) == {}
    assert "host-buffer" in replay.to_json()
    assert "observation-1" not in replay.to_json()


def test_current_work_observation_is_planned_after_normalization_and_replays() -> None:
    calls: list[str] = []

    def normalizer(
        _: ObservationReference,
        __: EngineState | None,
    ) -> StructuredSurrogate:
        calls.append("normalize")
        return _current_work()

    def planner(
        observation: StructuredSurrogate,
        state: EngineState | None,
        context: PlanningContext,
    ) -> StructuredSurrogate:
        calls.append("plan")
        assert observation.schema_id == "ctx.observation.current-work"
        assert state is None
        assert context.planner_version == "planner-v1"
        assert context.catalog_snapshot_digest == _digest("catalog")
        return _capability_plan()

    replay = _prepare(
        DefaultReplayInputFactory(
            observation_normalizer=normalizer,
            decision_planner=planner,
            reducer_version="ctx-reducer-v2",
        ),
        _observation_event(),
    )

    assert calls == ["normalize", "plan"]
    assert replay.observation_surrogate == _current_work()
    assert replay.decision_surrogate == _capability_plan()
    assert ReplayInput.from_json(replay.to_json()) == replay


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown": "token"},
        {"signals": ["python", "python"]},
        {"signals": ["python", "async"]},
        {"signals": [f"signal-{index}" for index in range(101)]},
        {"active_capability_ids": [f"skill:active-{index}" for index in range(6)]},
        {"requested_limit": -1},
        {"requested_limit": 6},
    ],
)
def test_current_work_schema_rejects_unknown_duplicate_noncanonical_and_oversize_values(
    overrides: Mapping[str, object],
) -> None:
    base = _prepare(_factory(), _event("SessionStarted"))

    with pytest.raises(ReplayValidationError):
        observation = _current_work(**overrides)
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            observation_surrogate=observation,
        )


def test_current_work_schema_rejects_missing_and_unsafe_values() -> None:
    missing = _current_work().to_dict()["value"]
    assert isinstance(missing, dict)
    del missing["languages"]
    missing_observation = StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value=missing,
    )
    base = _prepare(_factory(), _event("SessionStarted"))

    with pytest.raises(ReplayValidationError, match="missing or unknown"):
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            observation_surrogate=missing_observation,
        )
    with pytest.raises(ReplayPrivacyError, match="safe token"):
        _current_work(signals=["read /private/repo/app.py"])


@pytest.mark.parametrize("requested_limit", [0, 5])
def test_current_work_requested_limit_boundaries_are_valid(requested_limit: int) -> None:
    observation = _current_work(requested_limit=requested_limit)
    base = _prepare(_factory(), _event("SessionStarted"))

    replay = ReplayInput(
        source_event_content_digest=base.source_event_content_digest,
        reducer_event=base.reducer_event,
        observation_surrogate=observation,
    )

    assert replay.observation_surrogate == observation


@pytest.mark.parametrize(
    "observation",
    [
        _current_work(signals=["Python"]),
        _current_work(signals=[f"signal-{index:02d}" for index in range(33)]),
        _current_work(languages=[f"language-{index:02d}" for index in range(11)]),
    ],
)
def test_current_work_schema_matches_planner_case_and_size_limits(
    observation: StructuredSurrogate,
) -> None:
    base = _prepare(_factory(), _event("SessionStarted"))

    with pytest.raises(ReplayValidationError):
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            observation_surrogate=observation,
        )


@pytest.mark.parametrize(
    "decision",
    [
        _capability_plan(unknown="token"),
        _capability_plan(capabilities=[_capability(), _capability()]),
        _capability_plan(
            capabilities=[
                _capability(
                    capability_id=f"skill:item-{index}",
                    name=f"item-{index}",
                    catalog_entry_digest=_digest(f"item-{index}"),
                )
                for index in range(6)
            ]
        ),
        _capability_plan(capabilities=[_capability(kind="plugin")]),
        _capability_plan(capabilities=[_capability(normalized_score_ppm=-1)]),
        _capability_plan(capabilities=[_capability(normalized_score_ppm=1_000_001)]),
        _capability_plan(capabilities=[_capability(matching_signals=["python", "fastapi"])]),
        _capability_plan(capabilities=[_capability(reason_codes=["signal-match", "signal-match"])]),
        _capability_plan(status="unknown"),
        _capability_plan(status="ready", abstention_code="below-threshold"),
        _capability_plan(status="ready", capabilities=[]),
        _capability_plan(
            status="abstained",
            abstention_code="below-threshold",
            capabilities=[_capability()],
        ),
        _capability_plan(status="abstained", abstention_code=None, capabilities=[]),
        _capability_plan(status="abstained", abstention_code="planner-failed", capabilities=[]),
        _capability_plan(status="degraded", abstention_code="below-threshold", capabilities=[]),
    ],
)
def test_capability_plan_schema_fails_closed(decision: StructuredSurrogate) -> None:
    base = _prepare(_planning_factory(), _event("SessionStarted"))

    with pytest.raises(ReplayValidationError):
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            decision_surrogate=decision,
            reducer_version=base.reducer_version,
        )


def test_capability_plan_rejects_missing_and_unsafe_values() -> None:
    missing = _capability_plan().to_dict()["value"]
    assert isinstance(missing, dict)
    capabilities = missing["capabilities"]
    assert isinstance(capabilities, list)
    capability = capabilities[0]
    assert isinstance(capability, dict)
    del capability["actionability"]
    missing_decision = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=1,
        value=missing,
    )
    base = _prepare(_planning_factory(), _event("SessionStarted"))

    with pytest.raises(ReplayValidationError, match="missing or unknown"):
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            decision_surrogate=missing_decision,
            reducer_version=base.reducer_version,
        )
    with pytest.raises(ReplayPrivacyError, match="safe token"):
        _capability_plan(capabilities=[_capability(name="Python testing skill")])

    outer_missing = _capability_plan().to_dict()["value"]
    assert isinstance(outer_missing, dict)
    del outer_missing["status"]
    outer_missing_decision = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=1,
        value=outer_missing,
    )
    with pytest.raises(ReplayValidationError, match="missing or unknown"):
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            decision_surrogate=outer_missing_decision,
            reducer_version=base.reducer_version,
        )


def test_capability_plan_v1_rejects_install_without_plan_identity() -> None:
    base = _prepare(_planning_factory(), _event("SessionStarted"))
    decision = _capability_plan(capabilities=[_capability(actionability="install")])

    with pytest.raises(ReplayValidationError, match="schema v1"):
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            decision_surrogate=decision,
            reducer_version="ctx-reducer-v2",
        )


def test_capability_plan_v2_round_trips_exact_install_identity_but_has_no_reducer() -> None:
    descriptor_digest = _digest("install-descriptor")
    plan_digest = _digest("install-plan")
    decision = _capability_plan_v2(
        capabilities=[
            _capability(
                actionability="install",
                install_descriptor_digest=descriptor_digest,
                install_plan_digest=plan_digest,
            )
        ]
    )
    base = _prepare(_factory(), _event("SessionStarted"))
    decoded = StructuredSurrogate.from_json(decision.to_json())
    assert decoded == decision
    capabilities = decoded.value["capabilities"]
    assert isinstance(capabilities, tuple)
    capability = capabilities[0]
    assert isinstance(capability, Mapping)
    assert capability["install_descriptor_digest"] == descriptor_digest
    assert capability["install_plan_digest"] == plan_digest

    for reducer_version in ("ctx-reducer-v1", "ctx-reducer-v2", "ctx-reducer-v3"):
        with pytest.raises(ReplayValidationError, match="not compatible"):
            ReplayInput(
                source_event_content_digest=base.source_event_content_digest,
                reducer_event=base.reducer_event,
                decision_surrogate=decision,
                reducer_version=reducer_version,
            )


def test_capability_plan_v2_replay_distinguishes_same_plan_with_different_descriptors() -> None:
    plan_digest = _digest("shared-plan")
    base = _prepare(_factory(), _event("SessionStarted"))

    def bound_decision(descriptor_digest: str) -> StructuredSurrogate:
        return _capability_plan_v2(
            capabilities=[
                _capability(
                    actionability="install",
                    install_descriptor_digest=descriptor_digest,
                    install_plan_digest=plan_digest,
                )
            ]
        )

    first = bound_decision(_digest("descriptor-one"))
    second = bound_decision(_digest("descriptor-two"))

    assert first.to_json() != second.to_json()
    assert StructuredSurrogate.from_json(first.to_json()) == first
    assert StructuredSurrogate.from_json(second.to_json()) == second
    for decision in (first, second):
        for reducer_version in ("ctx-reducer-v1", "ctx-reducer-v2", "ctx-reducer-v3"):
            with pytest.raises(ReplayValidationError, match="not compatible"):
                ReplayInput(
                    source_event_content_digest=base.source_event_content_digest,
                    reducer_event=base.reducer_event,
                    decision_surrogate=decision,
                    reducer_version=reducer_version,
                )


@pytest.mark.parametrize(
    "decision",
    [
        _capability_plan_v2(
            capabilities=[
                _capability(
                    actionability="install",
                    install_descriptor_digest=None,
                    install_plan_digest=_digest("present-plan"),
                )
            ]
        ),
        _capability_plan_v2(
            capabilities=[
                _capability(
                    actionability="install",
                    install_descriptor_digest=_digest("present-descriptor"),
                    install_plan_digest=None,
                )
            ]
        ),
        _capability_plan_v2(
            capabilities=[
                _capability(
                    actionability="manual",
                    install_descriptor_digest=_digest("unexpected-descriptor"),
                    install_plan_digest=_digest("unexpected"),
                )
            ]
        ),
    ],
)
def test_capability_plan_v2_fails_closed_on_missing_or_inconsistent_install_identity(
    decision: StructuredSurrogate,
) -> None:
    base = _prepare(_factory(), _event("SessionStarted"))
    with pytest.raises(ReplayValidationError):
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            decision_surrogate=decision,
            reducer_version="ctx-reducer-v3",
        )


def test_replay_v3_sanitizes_install_identity_and_consent_basis_without_changing_v2() -> None:
    descriptor_digest = _digest("install-descriptor")
    plan_digest = _digest("install-plan")
    desired = {
        "actionability": "install",
        "capability_id": "skill:remote",
        "install_descriptor_digest": descriptor_digest,
        "install_plan_digest": plan_digest,
        "kind": "skill",
        "lease_id": "lease-1",
        "source_digest": _digest("candidate"),
    }
    v3 = DefaultReplayInputFactory(reducer_version="ctx-reducer-v3")
    reassessment = _prepare(
        v3,
        _event(
            "ReassessmentRequested",
            payload={
                "owner_id": "owner-1",
                "desired_capabilities": [desired],
                "policy_snapshot_digest": _digest("policy"),
            },
        ),
    )
    assert reassessment.reducer_event.payload["desired_capabilities"] == (desired,)
    assert reassessment.reducer_event.payload["policy_snapshot_digest"] == _digest("policy")

    with pytest.raises(ReplayValidationError, match="unsupported schema"):
        _prepare(
            v3,
            _event(
                "ReassessmentRequested",
                payload={"owner_id": "owner-1", "desired_capabilities": [desired]},
            ),
        )

    with pytest.raises(ReplayValidationError, match="missing or unknown"):
        _prepare(
            _planning_factory(),
            _event(
                "ReassessmentRequested",
                payload={"owner_id": "owner-1", "desired_capabilities": [desired]},
            ),
        )

    decision_payload = {
        "consent_id": "consent-1",
        "decision": "granted",
        "decision_basis": "preapproved-policy",
        "policy_snapshot_digest": _digest("policy"),
        "requested_action_id": "action-1",
        "requested_action_kind": "InstallCapability",
        "requested_action_content_digest": _digest("action"),
        "requested_action_precondition_revision": 4,
    }
    decision_replay = _prepare(
        v3,
        _event("UserDecision", payload=decision_payload, revision=3),
    )
    assert dict(decision_replay.reducer_event.payload) == decision_payload

    with pytest.raises(ReplayValidationError, match="missing or unknown"):
        _prepare(
            _planning_factory(),
            _event("UserDecision", payload=decision_payload, revision=3),
        )


@pytest.mark.parametrize("kind", ["skill", "agent", "mcp-server", "harness"])
@pytest.mark.parametrize("score", [0, 1_000_000])
def test_capability_plan_kind_and_score_boundaries_are_valid(kind: str, score: int) -> None:
    decision = _capability_plan(
        capabilities=[
            _capability(
                capability_id=f"{kind}:candidate",
                kind=kind,
                name="candidate",
                normalized_score_ppm=score,
            )
        ]
    )
    replay = _prepare(
        _planning_factory(),
        _event("SessionStarted"),
        decision_surrogate=decision,
    )

    assert replay.decision_surrogate == decision


@pytest.mark.parametrize(
    ("status", "abstention_code"),
    [
        ("abstained", "no-signals"),
        ("abstained", "below-threshold"),
        ("abstained", "no-relevant-capability"),
        ("degraded", "catalog-unavailable"),
        ("degraded", "planner-failed"),
    ],
)
def test_empty_capability_plan_status_combinations_are_exact(
    status: str,
    abstention_code: str,
) -> None:
    decision = _capability_plan(
        status=status,
        abstention_code=abstention_code,
        capabilities=[],
    )
    replay = _prepare(
        _planning_factory(),
        _event("SessionStarted"),
        decision_surrogate=decision,
    )

    assert replay.decision_surrogate == decision


def test_capability_plan_preserves_canonical_ranked_capability_order() -> None:
    second = _capability(
        capability_id="agent:reviewer",
        kind="agent",
        name="reviewer",
        catalog_entry_digest=_digest("reviewer"),
        normalized_score_ppm=800_000,
    )
    decision = _capability_plan(capabilities=[_capability(), second])
    replay = _prepare(
        _planning_factory(),
        _event("SessionStarted"),
        decision_surrogate=decision,
    )

    assert replay.decision_surrogate is not None
    capabilities = replay.decision_surrogate.value["capabilities"]
    assert isinstance(capabilities, tuple)
    capability_ids: list[object] = []
    for item in capabilities:
        assert isinstance(item, Mapping)
        capability_ids.append(item["capability_id"])
    assert capability_ids == [
        "skill:fastapi-testing",
        "agent:reviewer",
    ]


@pytest.mark.parametrize(
    "capability",
    [
        _capability(kind="agent"),
        _capability(name="other"),
        _capability(actionability="unknown"),
        _capability(reason_codes=[]),
    ],
)
def test_capability_plan_rejects_semantically_invalid_rows(
    capability: dict[str, object],
) -> None:
    with pytest.raises(ReplayValidationError):
        _prepare(
            _planning_factory(),
            _event("SessionStarted"),
            decision_surrogate=_capability_plan(capabilities=[capability]),
        )


def test_capability_plan_rejects_noncanonical_rank_order() -> None:
    lower = _capability(
        capability_id="agent:reviewer",
        kind="agent",
        name="reviewer",
        catalog_entry_digest=_digest("reviewer"),
        normalized_score_ppm=800_000,
    )

    with pytest.raises(ReplayValidationError, match="ranked order"):
        _prepare(
            _planning_factory(),
            _event("SessionStarted"),
            decision_surrogate=_capability_plan(capabilities=[lower, _capability()]),
        )


@pytest.mark.parametrize(
    "capability",
    [
        _capability(matching_signals=[f"signal-{index:02d}" for index in range(33)]),
        _capability(reason_codes=[f"reason-{index:02d}" for index in range(17)]),
        _capability(
            capability_id="skill:python:testing",
            name="python:testing",
        ),
    ],
)
def test_capability_plan_matches_planner_name_and_size_limits(
    capability: dict[str, object],
) -> None:
    with pytest.raises(ReplayValidationError):
        _prepare(
            _planning_factory(),
            _event("SessionStarted"),
            decision_surrogate=_capability_plan(capabilities=[capability]),
        )


def test_explicit_decision_and_configured_planner_are_mutually_exclusive() -> None:
    planner_calls = 0

    def planner(
        _: StructuredSurrogate,
        __: EngineState | None,
        ___: PlanningContext,
    ) -> StructuredSurrogate:
        nonlocal planner_calls
        planner_calls += 1
        return _capability_plan()

    factory = DefaultReplayInputFactory(
        decision_planner=planner,
        reducer_version="ctx-reducer-v2",
    )

    with pytest.raises(ReplayValidationError, match="explicit decision.*configured planner"):
        _prepare(
            factory,
            _event("SessionStarted"),
            decision_surrogate=_capability_plan(),
        )
    assert planner_calls == 0


def test_configured_planner_requires_a_planning_aware_reducer() -> None:
    with pytest.raises(ReplayValidationError, match="planning-aware reducer"):
        DefaultReplayInputFactory(
            decision_planner=lambda _observation, _state, _context: _capability_plan(),
        )


def test_capability_plan_decision_cannot_be_paired_with_legacy_reducer() -> None:
    with pytest.raises(ReplayValidationError, match="decision schema.*reducer version"):
        _prepare(
            _factory(),
            _event("SessionStarted"),
            decision_surrogate=_capability_plan(),
        )


def test_planner_is_not_called_until_the_normalized_observation_is_approved() -> None:
    planner_calls = 0

    def invalid_normalizer(
        _: ObservationReference,
        __: EngineState | None,
    ) -> StructuredSurrogate:
        return _current_work(signals=["python", "async"])

    def planner(
        _: StructuredSurrogate,
        __: EngineState | None,
        ___: PlanningContext,
    ) -> StructuredSurrogate:
        nonlocal planner_calls
        planner_calls += 1
        return _capability_plan()

    factory = DefaultReplayInputFactory(
        observation_normalizer=invalid_normalizer,
        decision_planner=planner,
        reducer_version="ctx-reducer-v2",
    )

    with pytest.raises(ReplayValidationError, match="canonical order"):
        _prepare(factory, _observation_event())
    assert planner_calls == 0


def test_planner_failure_does_not_retain_secret_exception_chain() -> None:
    sentinel = "secret-planner-password-private-repo"

    def normalizer(
        _: ObservationReference,
        __: EngineState | None,
    ) -> StructuredSurrogate:
        return _current_work()

    def failing_planner(
        _: StructuredSurrogate,
        __: EngineState | None,
        ___: PlanningContext,
    ) -> StructuredSurrogate:
        raise RuntimeError(sentinel)

    factory = DefaultReplayInputFactory(
        observation_normalizer=normalizer,
        decision_planner=failing_planner,
        reducer_version="ctx-reducer-v2",
    )
    checked = factory.preflight(_observation_event())

    with pytest.raises(ReplayValidationError, match="decision planning failed") as error:
        factory.prepare(checked, None)

    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert sentinel not in "".join(traceback.format_exception(exception))


def test_opaque_observation_reference_requires_an_injected_normalizer() -> None:
    source = _event(
        "DevelopmentObserved",
        payload={
            "observation_ref": {
                "provider_id": "host-buffer",
                "opaque_id": "observation-1",
                "content_digest": _digest("raw buffer"),
            }
        },
    )
    factory = _factory()
    checked = factory.preflight(source)
    with pytest.raises(UnsupportedReplayEvent, match="typed observation normalizer"):
        factory.prepare(checked, None)


@pytest.mark.parametrize(
    "scope",
    [
        _scope(repository_id="/private/repo"),
        _scope(repository_id=r"C:\\Users\\repo"),
        _scope(repository_id=r"\\server\\share"),
    ],
)
def test_native_paths_cannot_enter_persisted_scope(scope: ScopeRef) -> None:
    with pytest.raises(ReplayPrivacyError, match="repository_id"):
        _factory().preflight(_event("SessionStarted", scope=scope))


def test_non_digest_replay_metadata_and_unsafe_retention_are_rejected() -> None:
    unsafe_metadata = EngineEvent(
        event_id="event-unsafe",
        kind="SessionStarted",
        scope=_scope(),
        expected_revision=0,
        occurred_at=NOW,
        payload={},
        host_descriptor_digest="/private/repo",
    )
    with pytest.raises(ReplayPrivacyError, match="host_descriptor_digest"):
        _factory().preflight(unsafe_metadata)

    for retention in ("ephemeral", "aggregate"):
        with pytest.raises(ReplayPrivacyError, match="retention"):
            _factory().preflight(
                _event(
                    "SessionStarted",
                    privacy=PrivacyLabel(classification="private", retention=retention),
                )
            )


def test_optional_decision_surrogate_is_validated_and_bound() -> None:
    decision = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-set",
        schema_version=1,
        value={
            "capabilities": [{"capability_id": "skill:pytest", "source_digest": _digest("pytest")}]
        },
    )
    replay = _prepare(_factory(), _event("SessionStarted"), decision_surrogate=decision)
    assert replay.decision_surrogate == decision


@pytest.mark.parametrize("role", ["observation", "decision"])
def test_token_looking_raw_material_cannot_bypass_surrogate_schema_approval(
    role: str,
) -> None:
    unapproved = StructuredSurrogate.create(
        schema_id=f"ctx.{role}.unapproved",
        schema_version=1,
        value={"prompt": "password123", "path": "app.py", "code": "rm"},
    )
    base = _prepare(_factory(), _event("SessionStarted"))
    with pytest.raises(ReplayPrivacyError, match="not approved") as error:
        if role == "observation":
            ReplayInput(
                source_event_content_digest=base.source_event_content_digest,
                reducer_event=base.reducer_event,
                observation_surrogate=unapproved,
            )
        else:
            ReplayInput(
                source_event_content_digest=base.source_event_content_digest,
                reducer_event=base.reducer_event,
                decision_surrogate=unapproved,
            )
    rendered = str(error.value)
    for secret in ("password123", "app.py", "rm"):
        assert secret not in rendered


def test_approved_surrogate_schema_rejects_token_looking_extra_fields() -> None:
    decision = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-set",
        schema_version=1,
        value={
            "capabilities": [],
            "prompt": "password123",
            "path": "app.py",
            "code": "rm",
        },
    )
    base = _prepare(_factory(), _event("SessionStarted"))
    with pytest.raises(ReplayValidationError, match="missing or unknown") as error:
        ReplayInput(
            source_event_content_digest=base.source_event_content_digest,
            reducer_event=base.reducer_event,
            decision_surrogate=decision,
        )
    rendered = str(error.value)
    for secret in ("password123", "app.py", "rm"):
        assert secret not in rendered


def test_payload_and_surrogate_size_limits_fail_closed() -> None:
    with pytest.raises(ReplayValidationError, match="size"):
        StructuredSurrogate.create(
            schema_id="ctx.observation.large",
            schema_version=1,
            value={f"k{index:03d}" + "x" * 110: "v" + "x" * 120 for index in range(100)},
        )

    calls: list[str] = []

    def hook(_: ObservationReference, __: EngineState | None) -> StructuredSurrogate:
        calls.append("called")
        raise AssertionError("must not be called")

    factory = DefaultReplayInputFactory(observation_normalizer=hook)
    with pytest.raises((ReplayPrivacyError, ReplayValidationError)):
        factory.preflight(
            _event(
                "DevelopmentObserved",
                payload={"observation_ref": {"provider_id": "host", "opaque_id": "x" * 1000}},
            )
        )
    assert calls == []


def test_normalizer_failure_does_not_retain_secret_exception_chain() -> None:
    sentinel = "secret-token-password123-private-repo"

    def failing(_: ObservationReference, __: EngineState | None) -> StructuredSurrogate:
        raise RuntimeError(sentinel)

    factory = DefaultReplayInputFactory(observation_normalizer=failing)
    checked = factory.preflight(
        _event(
            "DevelopmentObserved",
            payload={
                "observation_ref": {
                    "provider_id": "host-buffer",
                    "opaque_id": "observation-1",
                    "content_digest": _digest("raw buffer"),
                }
            },
        )
    )
    with pytest.raises(ReplayValidationError, match="normalization failed") as error:
        factory.prepare(checked, None)

    exception = error.value
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert sentinel not in "".join(traceback.format_exception(exception))


@pytest.mark.parametrize("shape", ["deep", "large-string", "too-many-items"])
def test_ingress_bounds_fail_before_original_event_serialization(
    shape: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error: object = {"code": "host-failure"}
    if shape == "deep":
        for _ in range(12):
            error = {"nested": error}
    elif shape == "large-string":
        error = {"message": "s" * (65 * 1024)}
    else:
        error = {f"item-{index}": index for index in range(101)}
    source = _event(
        "DevelopmentObserved",
        revision=2,
        payload={"unsafe": error},
    )
    serializations = 0
    original = EngineEvent.to_json

    def counted(event: EngineEvent) -> str:
        nonlocal serializations
        serializations += 1
        return original(event)

    monkeypatch.setattr(EngineEvent, "to_json", counted)
    with pytest.raises(ReplayValidationError, match="depth|size|item") as failure:
        _factory().preflight(source)
    assert serializations == 0
    assert "s" * 200 not in str(failure.value)


def test_session_start_requires_a_real_host_descriptor_digest() -> None:
    source = _event("SessionStarted", metadata=False)
    with pytest.raises(ReplayValidationError, match="host descriptor digest"):
        _factory().preflight(source)
