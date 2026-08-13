from __future__ import annotations

from hypothesis import given, settings, strategies as st

from ctx.engine.protocol import EngineEvent, HostAction, ScopeRef
from ctx.engine.reducer import MAX_ACTIVE_CAPABILITIES, reduce


NOW = "2026-08-01T12:00:00Z"
SCOPE = ScopeRef(
    tenant_id="tenant-property",
    workspace_id="workspace-property",
    repository_id="repository-property",
    session_id="session-property",
    exposure_id="exposure-property",
    host_context_id="host-property",
)


def _event(
    kind: str,
    revision: int,
    event_id: str,
    payload: dict[str, object],
) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=SCOPE,
        expected_revision=revision,
        occurred_at=NOW,
        payload=payload,
        correlation_id="plan-property",
        catalog_snapshot_digest="catalog-property",
        host_descriptor_digest="host-property-v1",
        engine_version="engine-property-v1",
        planner_version="planner-property-v1",
        policy_version="policy-property-v1",
        semantic_model_digest="semantic-model-property-v1",
        semantic_index_digest="semantic-index-property-v1",
        work_signature="work-signature-property-v1",
        random_seed=19,
    )


def _receipt(state, action: HostAction, event_id: str, *, applied: bool):
    payload: dict[str, object] = {
        "action_id": action.action_id,
        "action_kind": action.kind,
        "action_content_digest": action.content_digest,
        "action_precondition_revision": action.precondition_revision,
    }
    if applied:
        payload["verification"] = {"host_state": action.verification["expected_state"]}
        kind = "ActionApplied"
    else:
        payload["error"] = {"code": "generated-host-failure"}
        kind = "ActionFailed"
    return reduce(state, _event(kind, state.revision, event_id, payload))


@settings(max_examples=30, deadline=None)
@given(st.integers(min_value=0, max_value=12))
def test_requests_alone_never_activate_or_exceed_the_global_belt(size: int) -> None:
    state, _ = reduce(
        None,
        _event(
            "SessionStarted",
            0,
            "event-start",
            {"host_level": "activating"},
        ),
    )
    desired = [
        {
            "capability_id": f"skill:capability-{index}",
            "source_digest": f"digest-{index}",
            "lease_id": f"lease-{index}",
        }
        for index in range(size)
    ]

    state, transition = reduce(
        state,
        _event(
            "ReassessmentRequested",
            state.revision,
            "event-desired",
            {
                "owner_id": "owner-property",
                "desired_capabilities": desired,
            },
        ),
    )

    assert state.active_capability_ids == frozenset()
    assert (
        len([action for action in transition.actions if action.kind == "ActivateCapability"])
        <= MAX_ACTIVE_CAPABILITIES
    )


@settings(max_examples=8, deadline=None)
@given(
    child_shares_capability=st.booleans(),
    replacement_fails=st.booleans(),
    first_terminal_cleanup_fails=st.booleans(),
)
def test_generated_owner_receipt_replacement_and_terminal_interleavings(
    child_shares_capability: bool,
    replacement_fails: bool,
    first_terminal_cleanup_fails: bool,
) -> None:
    state, _ = reduce(
        None,
        _event(
            "SessionStarted",
            0,
            "sequence-start",
            {"host_level": "activating"},
        ),
    )
    original = [
        {
            "capability_id": f"skill:sequence-{index}",
            "source_digest": f"digest-{index}",
            "lease_id": f"lease-parent-{index}",
        }
        for index in range(5)
    ]
    state, transition = reduce(
        state,
        _event(
            "ReassessmentRequested",
            state.revision,
            "sequence-original",
            {"owner_id": "parent", "desired_capabilities": original},
        ),
    )
    counter = 0
    for action in transition.actions:
        state, _ = _receipt(
            state,
            action,
            f"sequence-initial-receipt-{counter}",
            applied=True,
        )
        counter += 1
        assert len(state.active_capability_ids) <= MAX_ACTIVE_CAPABILITIES

    if child_shares_capability:
        state, _ = reduce(
            state,
            _event(
                "ReassessmentRequested",
                state.revision,
                "sequence-child",
                {
                    "owner_id": "child",
                    "desired_capabilities": [
                        {
                            **original[1],
                            "lease_id": "lease-child-shared",
                        }
                    ],
                },
            ),
        )

    replacement = [
        *original[1:],
        {
            "capability_id": "skill:sequence-new",
            "source_digest": "digest-new",
            "lease_id": "lease-new",
        },
    ]
    state, transition = reduce(
        state,
        _event(
            "ReassessmentRequested",
            state.revision,
            "sequence-replacement",
            {"owner_id": "parent", "desired_capabilities": replacement},
        ),
    )
    removal = next(action for action in transition.actions if action.kind == "DeactivateCapability")
    state, transition = _receipt(
        state,
        removal,
        "sequence-old-inactive",
        applied=True,
    )
    addition = next(action for action in transition.actions if action.kind == "ActivateCapability")
    state, transition = _receipt(
        state,
        addition,
        "sequence-new-result",
        applied=not replacement_fails,
    )
    if replacement_fails:
        rollback = next(
            action for action in transition.actions if action.kind == "ActivateCapability"
        )
        state, _ = _receipt(
            state,
            rollback,
            "sequence-rollback-active",
            applied=True,
        )
    assert len(state.active_capability_ids) <= MAX_ACTIVE_CAPABILITIES

    state, transition = reduce(
        state,
        _event("SessionEnded", state.revision, "sequence-ended", {}),
    )
    initial_cleanup = next(
        action for action in transition.actions if action.kind == "DeactivateCapability"
    )
    if first_terminal_cleanup_fails:
        state, _ = _receipt(
            state,
            initial_cleanup,
            "sequence-cleanup-failed",
            applied=False,
        )
        state, transition = reduce(
            state,
            _event(
                "ReassessmentRequested",
                state.revision,
                "sequence-cleanup-retry",
                {"retry_failed_deactivations": [initial_cleanup.entity_id]},
            ),
        )
        cleanup: HostAction | None = next(
            action for action in transition.actions if action.kind == "DeactivateCapability"
        )
    else:
        cleanup = initial_cleanup

    while cleanup is not None:
        state, transition = _receipt(
            state,
            cleanup,
            f"sequence-cleanup-applied-{counter}",
            applied=True,
        )
        counter += 1
        assert len(state.active_capability_ids) <= MAX_ACTIVE_CAPABILITIES
        cleanup = next(
            (action for action in transition.actions if action.kind == "DeactivateCapability"),
            None,
        )

    assert state.active_capability_ids == frozenset()
