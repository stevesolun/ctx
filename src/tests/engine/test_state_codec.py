from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ctx.engine.protocol import EngineEvent, HostAction, ScopeRef
from ctx.engine.reducer import reduce
from ctx.engine.state import (
    CapabilityEvidence,
    CapabilityState,
    CommittedPlan,
    EngineState,
    LeaseRef,
    PendingEffect,
    PlanCapability,
    StateValidationError,
)


def _scope(*, exposure_id: str = "exposure-parent") -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id=exposure_id,
        host_context_id="host-1",
    )


def _lease(index: int = 0) -> LeaseRef:
    return LeaseRef(
        lease_id=f"lease-{index}",
        owner_id=f"owner-{index}",
        exposure_id="exposure-parent",
    )


def _capability(
    index: int = 0,
    *,
    active: bool = True,
    leases: tuple[LeaseRef, ...] | None = None,
) -> CapabilityState:
    return CapabilityState(
        capability_id=f"skill:capability-{index}",
        source_digest=f"source-{index}",
        plan_id=f"plan-{index}",
        catalog_snapshot_id="catalog-1",
        leases=(_lease(index),) if leases is None else leases,
        activation="active" if active else "inactive",
        activation_lease_id=f"lease-{index}" if active else None,
    )


def _action(capability: CapabilityState, *, action_id: str = "action-1") -> HostAction:
    return HostAction(
        action_id=action_id,
        kind="PrepareExposure",
        scope=_scope(),
        precondition_revision=7,
        entity_id=capability.capability_id,
        source_digest=capability.source_digest,
        plan_id=capability.plan_id,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        lease_id=capability.activation_lease_id,
        expires_at="2026-08-01T13:00:00Z",
        required_host_feature="activation",
        verification={"expected_state": "prepared", "receipt_required": True},
        rollback={"kind": "cleanup-prepared-exposure"},
    )


def _state() -> EngineState:
    capability = _capability()
    return EngineState(
        revision=7,
        scope=_scope(),
        host_level="activating",
        host_descriptor_digest="host-descriptor-1",
        capabilities=(capability,),
        pending_effects=(PendingEffect(action=_action(capability), effect="prepare"),),
        evidence=(
            CapabilityEvidence(
                exposure_id="exposure-parent",
                capability_id=capability.capability_id,
                source_digest=capability.source_digest,
                exposure="submitted",
                invocation="invoked-succeeded",
            ),
        ),
        last_manual_bundle=(capability.capability_id,),
    )


def test_engine_state_round_trips_as_stable_store_canonical_json() -> None:
    state = _state()

    encoded = state.to_json()
    decoded = EngineState.from_json(encoded)

    assert decoded == state
    assert decoded.to_dict() == state.to_dict()
    assert decoded.to_json() == encoded
    assert encoded == json.dumps(
        json.loads(encoded),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_historical_state_json_remains_byte_stable_without_v3_fields() -> None:
    encoded = _state().to_json()

    assert "committed_plan" not in encoded
    assert "pending_consents" not in encoded
    assert "blocked_install_descriptor_digests" not in encoded
    assert "install_descriptor_digest" not in encoded
    assert "install_plan_digest" not in encoded
    assert EngineState.from_json(encoded).to_json() == encoded


def test_unreleased_draft_v3_projection_cannot_enter_the_tagged_v3_codec() -> None:
    selection = PlanCapability(
        capability_id="skill:remote-testing",
        source_digest="a" * 64,
        kind="skill",
        actionability="install",
        install_descriptor_digest="9" * 64,
        install_plan_digest="b" * 64,
    )
    with pytest.raises(StateValidationError, match="CapabilityStateV3"):
        EngineState(
            revision=3,
            scope=_scope(),
            host_level="managing",
            host_descriptor_digest="c" * 64,
            capabilities=(
                CapabilityState(
                    capability_id=selection.capability_id,
                    source_digest=selection.source_digest,
                    plan_id="plan-install",
                    catalog_snapshot_id="d" * 64,
                    kind=selection.kind,
                    actionability=selection.actionability,
                    install_descriptor_digest=selection.install_descriptor_digest,
                    install_plan_digest=selection.install_plan_digest,
                    installation="absent",
                    leases=(_lease(),),
                ),
            ),
            committed_plan=CommittedPlan(
                plan_id="plan-install",
                catalog_snapshot_id="d" * 64,
                decision_digest="e" * 64,
                capabilities=(selection,),
            ),
            install_policy_snapshot_digest="f" * 64,
            blocked_install_descriptor_digests=("8" * 64,),
            _contract_version=3,
        )


def test_nested_state_values_round_trip_through_the_projection() -> None:
    state = _state()
    data = state.to_dict()

    assert LeaseRef.from_dict(data["capabilities"][0]["leases"][0]) == _lease()
    assert CapabilityState.from_dict(data["capabilities"][0]) == state.capabilities[0]
    assert CapabilityEvidence.from_dict(data["evidence"][0]) == state.evidence[0]
    assert PendingEffect.from_dict(data["pending_effects"][0]) == state.pending_effects[0]
    assert data["pending_effects"][0]["action"] == _action(state.capabilities[0]).to_dict()


@pytest.mark.parametrize(
    ("factory", "data"),
    [
        (LeaseRef.from_dict, {"lease_id": "lease", "owner_id": "owner"}),
        (
            CapabilityState.from_dict,
            {
                key: value
                for key, value in _state().to_dict()["capabilities"][0].items()
                if key != "activation"
            },
        ),
        (
            CapabilityEvidence.from_dict,
            {
                **_state().to_dict()["evidence"][0],
                "unexpected": True,
            },
        ),
        (
            PendingEffect.from_dict,
            {
                **_state().to_dict()["pending_effects"][0],
                "unexpected": True,
            },
        ),
        (
            EngineState.from_dict,
            {
                **_state().to_dict(),
                "unexpected": True,
            },
        ),
    ],
)
def test_codecs_reject_missing_or_unknown_fields(factory, data) -> None:
    with pytest.raises(StateValidationError, match="missing|unknown"):
        factory(data)


@pytest.mark.parametrize(
    "remove",
    [
        lambda data: data["scope"].pop("parent_exposure_id"),
        lambda data: data["pending_effects"][0]["action"].pop("consent_id"),
        lambda data: data["pending_effects"][0]["action"].pop("payload"),
        lambda data: data["pending_effects"][0]["action"]["scope"].pop("parent_exposure_id"),
        lambda data: data["pending_effects"][0]["action"]["privacy"].pop("retention"),
    ],
)
def test_projection_rejects_missing_canonical_nested_protocol_fields(remove) -> None:
    data = _state().to_dict()
    remove(data)

    with pytest.raises(StateValidationError, match="missing"):
        EngineState.from_dict(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["capabilities"][0].update(activation="made-up"),
        lambda data: data["capabilities"][0].update(
            activation="inactive", activation_lease_id="lease-0"
        ),
        lambda data: data["capabilities"][0].update(rollback_held=True, rollback_owner_id=None),
        lambda data: data["evidence"][0].update(exposure="made-up"),
        lambda data: data["evidence"][0].update(invocation="made-up"),
        lambda data: data["pending_effects"][0].update(effect="made-up"),
        lambda data: data.update(revision=0),
        lambda data: data.update(host_level="made-up"),
        lambda data: data.update(session_status="made-up"),
        lambda data: data.update(host_descriptor_digest=" "),
    ],
)
def test_projection_rejects_enum_cross_field_and_session_invariants(mutate) -> None:
    data = _state().to_dict()
    mutate(data)

    with pytest.raises(StateValidationError):
        EngineState.from_dict(data)


def test_projection_rejects_duplicate_nested_identities() -> None:
    for field_name in ("capabilities", "evidence", "pending_effects"):
        data = _state().to_dict()
        data[field_name].append(data[field_name][0])
        with pytest.raises(StateValidationError, match="duplicate"):
            EngineState.from_dict(data)


def test_projection_rejects_duplicate_leases_and_dangling_references() -> None:
    data = _state().to_dict()
    data["capabilities"][0]["leases"].append(data["capabilities"][0]["leases"][0])
    with pytest.raises(StateValidationError, match="duplicate.*lease"):
        EngineState.from_dict(data)

    data = _state().to_dict()
    data["evidence"][0]["capability_id"] = "skill:missing"
    with pytest.raises(StateValidationError, match="unknown capability"):
        EngineState.from_dict(data)

    data = _state().to_dict()
    second = dict(data["capabilities"][0])
    second.update(
        capability_id="skill:second",
        source_digest="source-second",
        plan_id="plan-second",
        activation="inactive",
        activation_lease_id=None,
    )
    data["capabilities"].append(second)
    data["capabilities"].sort(key=lambda item: item["capability_id"])
    with pytest.raises(StateValidationError, match="duplicate.*lease"):
        EngineState.from_dict(data)


def test_projection_rejects_action_mismatch_and_duplicate_pending_action_ids() -> None:
    data = _state().to_dict()
    data["pending_effects"][0]["effect"] = "deactivate"
    with pytest.raises(StateValidationError, match="effect|action"):
        EngineState.from_dict(data)

    data = _state().to_dict()
    duplicate = dict(data["pending_effects"][0])
    data["pending_effects"].append(duplicate)
    with pytest.raises(StateValidationError, match="duplicate.*action"):
        EngineState.from_dict(data)


def test_projection_rejects_more_than_five_active_capabilities() -> None:
    capabilities = tuple(_capability(index) for index in range(6))

    with pytest.raises(StateValidationError, match="five|5"):
        EngineState(
            revision=1,
            scope=_scope(),
            host_level="activating",
            host_descriptor_digest="host-descriptor-1",
            capabilities=capabilities,
        )


def test_projection_rejects_noncanonical_json_and_duplicate_keys() -> None:
    canonical = _state().to_json()
    noncanonical = json.dumps(json.loads(canonical), indent=2, ensure_ascii=False)
    with pytest.raises(StateValidationError, match="canonical"):
        EngineState.from_json(noncanonical)

    duplicate = canonical[:-1] + ',"revision":7}'
    with pytest.raises(StateValidationError, match="duplicate"):
        EngineState.from_json(duplicate)


def test_projection_rejects_invalid_json_roots_and_bytes() -> None:
    for value in ("[]", "not-json", b"\xff"):
        with pytest.raises(StateValidationError, match="JSON|UTF-8"):
            EngineState.from_json(value)


def test_from_dict_rejects_non_string_mapping_keys_as_state_validation() -> None:
    data = _state().to_dict()
    data[1] = "not-a-json-object-key"  # type: ignore[index]

    with pytest.raises(StateValidationError, match="field names"):
        EngineState.from_dict(data)


def test_to_json_rejects_constructor_valid_uncommitted_intermediate_state() -> None:
    state = _state()
    future_action = replace(
        state.pending_effects[0].action,
        precondition_revision=state.revision + 1,
    )
    intermediate = replace(
        state,
        pending_effects=(PendingEffect(action=future_action, effect="prepare"),),
    )

    with pytest.raises(StateValidationError, match="persisted.*revision"):
        intermediate.to_json()


def test_committed_reducer_output_round_trips_through_state_codec() -> None:
    state, _ = reduce(
        None,
        EngineEvent(
            event_id="codec-start",
            kind="SessionStarted",
            scope=_scope(),
            expected_revision=0,
            occurred_at="2026-08-01T12:00:00Z",
            payload={"host_level": "activating"},
            correlation_id="plan-start",
            engine_version="engine-v1",
            planner_version="planner-v1",
            policy_version="policy-v1",
            host_descriptor_digest="host-descriptor-1",
            catalog_snapshot_digest="catalog-1",
            semantic_model_digest="model-v1",
            semantic_index_digest="index-v1",
            work_signature="work-v1",
            random_seed=7,
        ),
    )
    state, _ = reduce(
        state,
        EngineEvent(
            event_id="codec-desired",
            kind="ReassessmentRequested",
            scope=_scope(),
            expected_revision=state.revision,
            occurred_at="2026-08-01T12:00:01Z",
            payload={
                "owner_id": "owner-0",
                "desired_capabilities": [
                    {
                        "capability_id": "skill:capability-0",
                        "source_digest": "source-0",
                        "lease_id": "lease-0",
                    }
                ],
            },
            correlation_id="plan-0",
            engine_version="engine-v1",
            planner_version="planner-v1",
            policy_version="policy-v1",
            host_descriptor_digest="host-descriptor-1",
            catalog_snapshot_digest="catalog-1",
            semantic_model_digest="model-v1",
            semantic_index_digest="index-v1",
            work_signature="work-v1",
            random_seed=7,
        ),
    )

    assert EngineState.from_json(state.to_json()) == state
