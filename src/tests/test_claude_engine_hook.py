from __future__ import annotations

import hashlib

import pytest

from ctx.adapters.claude_code import engine_hook
from ctx.adapters.claude_code.engine_hook import (
    render_committed_query_hook,
    render_recommendation_hook,
)
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planning_v3 import (
    BenefitAuditReference,
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    ManualPlanningAuthority,
)
from ctx.engine.protocol import HostAction, ScopeRef, Transition
from ctx.engine.state import CommittedPlanV3, PlanCapabilityV3
from ctx.runtime.query_decision import (
    CommittedQueryDecision,
    QueryDecisionFailure,
    QueryHostDescriptor,
    _commit_query_decision,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reviewed_decision(
    *,
    host: QueryHostDescriptor,
    abstained: bool = False,
) -> CommittedQueryDecision:
    scope = ScopeRef(
        tenant_id="local",
        workspace_id="workspace",
        repository_id="repository",
        session_id="session",
        exposure_id="exposure",
        host_context_id=host.host_context_id,
    )
    candidate = CapabilityCandidate(
        capability_id="agent:python-reviewer",
        kind="agent",
        name="python-reviewer",
        source_digest=_digest("entry:agent:python-reviewer"),
        normalized_score_ppm=700_000,
        matching_signals=("python",),
        reason_codes=("signal-match",),
        actionability="manual",
    )
    selection = CapabilityPlanSelectionV3(
        presentation=candidate,
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=candidate.capability_id,
            kind=candidate.kind,
            catalog_namespace_digest=_digest("reviewed-catalog-namespace"),
        ),
        benefit=CapabilityBenefitProjection(
            tier="advisory",
            individual_net_benefit_u=700_000,
            marginal_net_benefit_u=700_000,
        ),
        authority=ManualPlanningAuthority(),
    )
    plan = CommittedPlanV3(
        plan_id="plan-1",
        catalog_snapshot_id=_digest("reviewed-catalog"),
        decision_digest=_digest("reviewed-plan"),
        status="abstained" if abstained else "ready",
        abstention_code="no-feasible-capability" if abstained else None,
        benefit_audit=BenefitAuditReference(
            result_schema_id="ctx.benefit-result-v1",
            result_digest=_digest("reviewed-benefit-result"),
            policy_schema_id="ctx.benefit-policy-v1",
            policy_digest=_digest("reviewed-benefit-policy"),
            selection_algorithm_id="ctx.benefit-selection-v1",
            calibration_digest=_digest("reviewed-calibration"),
            requested_limit=5,
            candidate_pool_count=0 if abstained else 1,
            search_evaluation_count=0 if abstained else 1,
        ),
        capabilities=() if abstained else (PlanCapabilityV3(selection=selection),),
    )
    transition = Transition(
        event_id="event-1",
        scope=scope,
        from_revision=1,
        to_revision=2,
        actions=()
        if abstained
        else (
            HostAction(
                action_id="present-1",
                kind="PresentBundle",
                scope=scope,
                precondition_revision=2,
                payload={
                    "plan_digest": plan.decision_digest,
                    "capabilities": [selection.to_mapping()],
                },
            ),
        ),
    )
    return _commit_query_decision(
        host=host,
        transition=transition,
        plan=plan,
        journal_revision=2,
        journal_record_digest=_digest("reviewed-journal-record"),
        release_root_digest=_digest("synthetic-reviewed-release"),
        release_sequence=2,
        catalog_mode="reviewed",
        work_signature_digest=_digest("reviewed-work-signature"),
        host_invocation_digest=_digest("reviewed-host-invocation"),
    )


EXPECTED_CONTEXT = """CTX recommendation bundle (committed, advisory only):
1. kind=agent | name=python-reviewer | id=agent:python-reviewer | actionability=manual | score_ppm=700000
Use only capabilities relevant to the current task. Do not install, load, or activate anything without user approval."""


def test_committed_query_hook_wraps_exact_claude_decision_for_user_prompt_submit() -> None:
    decision = _reviewed_decision(
        host=QueryHostDescriptor.claude_code(),
    )

    assert render_committed_query_hook(decision) == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": EXPECTED_CONTEXT,
        }
    }


def test_committed_query_hook_abstains_for_empty_committed_decision() -> None:
    decision = _reviewed_decision(
        host=QueryHostDescriptor.claude_code(),
        abstained=True,
    )

    assert render_committed_query_hook(decision) is None


def test_committed_query_hook_fails_soft_for_failure_and_wrong_host() -> None:
    wrong_host = _reviewed_decision(
        host=QueryHostDescriptor.codex(),
    )

    assert (
        render_committed_query_hook(QueryDecisionFailure(failure_code="catalog-open-failed"))
        is None
    )
    assert render_committed_query_hook(wrong_host) is None


def test_committed_query_hook_fails_soft_for_an_untrusted_value() -> None:
    assert render_committed_query_hook(object()) is None


def test_committed_query_hook_does_not_hide_unexpected_adapter_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = _reviewed_decision(
        host=QueryHostDescriptor.claude_code(),
    )

    def fail_unexpectedly(_value: object, *, expected_host_context_id: str) -> str | None:
        assert expected_host_context_id == "claude-code"
        raise RuntimeError("unexpected adapter defect")

    monkeypatch.setattr(engine_hook, "render_committed_query_context", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="unexpected adapter defect"):
        render_committed_query_hook(decision)


def test_legacy_transition_projection_remains_post_tool_use_compatible() -> None:
    transition = Transition(
        event_id="legacy-event",
        scope=ScopeRef(
            tenant_id="local",
            workspace_id="workspace",
            repository_id="repository",
            session_id="session",
            exposure_id="exposure",
            host_context_id="claude-code",
        ),
        from_revision=1,
        to_revision=2,
    )

    assert render_recommendation_hook(transition) is None
