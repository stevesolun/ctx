from __future__ import annotations

import ast
import hashlib
import inspect

from ctx.adapters.codex import (
    render_committed_query_hook,
    render_prepared_context,
    render_recommendation_context,
)
from ctx.adapters.codex import query_hook
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
from ctx.runtime.production_catalog import (
    RELEASE_QUERY_CATALOG_MODE,
    RELEASE_QUERY_CATALOG_ROOT_SHA256,
    RELEASE_QUERY_CATALOG_SEQUENCE,
)
from ctx.runtime.query_decision import (
    CommittedQueryDecision,
    QueryDecisionFailure,
    QueryHostDescriptor,
    _commit_query_decision,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(host: QueryHostDescriptor) -> ScopeRef:
    return ScopeRef(
        tenant_id="local",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id=host.host_context_id,
    )


def _audit(*, candidates: int, evaluations: int) -> BenefitAuditReference:
    return BenefitAuditReference(
        result_schema_id="ctx.benefit-result-v1",
        result_digest=_digest("synthetic-reviewed-result"),
        policy_schema_id="ctx.benefit-policy-v1",
        policy_digest=_digest("synthetic-reviewed-policy"),
        selection_algorithm_id="ctx.benefit-selection-v1",
        calibration_digest=_digest("synthetic-reviewed-calibration"),
        requested_limit=5,
        candidate_pool_count=candidates,
        search_evaluation_count=evaluations,
    )


def _plan_selection(
    capability_id: str,
    *,
    score: int,
) -> CapabilityPlanSelectionV3:
    kind, name = capability_id.split(":", 1)
    presentation = CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        name=name,
        source_digest=_digest(f"synthetic-reviewed-entry:{capability_id}"),
        normalized_score_ppm=score,
        matching_signals=(name,),
        reason_codes=("workflow-match",),
        actionability="manual",
    )
    return CapabilityPlanSelectionV3(
        presentation=presentation,
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=capability_id,
            kind=kind,
            catalog_namespace_digest=_digest("synthetic-reviewed-catalog-namespace"),
        ),
        benefit=CapabilityBenefitProjection(
            tier="advisory",
            individual_net_benefit_u=score,
            marginal_net_benefit_u=score,
        ),
        authority=ManualPlanningAuthority(),
    )


def _reviewed_decision(host: QueryHostDescriptor | None = None) -> CommittedQueryDecision:
    """Seal a non-production reviewed receipt from an exact plan and transition."""

    descriptor = QueryHostDescriptor.codex() if host is None else host
    scope = _scope(descriptor)
    selections = (
        _plan_selection("agent:reviewer", score=600_000),
        _plan_selection("skill:python-tdd", score=900_000),
    )
    plan = CommittedPlanV3(
        plan_id="synthetic-reviewed-plan",
        catalog_snapshot_id=_digest("synthetic-reviewed-catalog-snapshot"),
        decision_digest=_digest("synthetic-reviewed-decision"),
        status="ready",
        abstention_code=None,
        benefit_audit=_audit(candidates=2, evaluations=2),
        capabilities=tuple(PlanCapabilityV3(selection=item) for item in selections),
    )
    transition = Transition(
        event_id="synthetic-reviewed-intent",
        scope=scope,
        from_revision=1,
        to_revision=2,
        actions=(
            HostAction(
                action_id="synthetic-reviewed-present",
                kind="PresentBundle",
                scope=scope,
                precondition_revision=2,
                payload={
                    "plan_digest": plan.decision_digest,
                    "capabilities": [item.to_mapping() for item in selections],
                },
            ),
        ),
    )
    return _commit_query_decision(
        host=descriptor,
        transition=transition,
        plan=plan,
        journal_revision=2,
        journal_record_digest=_digest("synthetic-reviewed-journal-record"),
        release_root_digest=_digest("synthetic-reviewed-release-root"),
        release_sequence=2,
        catalog_mode="reviewed",
        work_signature_digest=_digest("synthetic-reviewed-work"),
        host_invocation_digest=_digest("synthetic-reviewed-invocation"),
    )


def _production_abstention() -> CommittedQueryDecision:
    host = QueryHostDescriptor.codex()
    plan = CommittedPlanV3(
        plan_id="production-abstention-plan",
        catalog_snapshot_id=_digest("production-abstention-catalog-snapshot"),
        decision_digest=_digest("production-abstention-decision"),
        status="abstained",
        abstention_code="no-feasible-capability",
        benefit_audit=_audit(candidates=0, evaluations=0),
        capabilities=(),
    )
    transition = Transition(
        event_id="production-abstention-intent",
        scope=_scope(host),
        from_revision=1,
        to_revision=2,
    )
    return _commit_query_decision(
        host=host,
        transition=transition,
        plan=plan,
        journal_revision=2,
        journal_record_digest=_digest("production-abstention-journal-record"),
        release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
        release_sequence=RELEASE_QUERY_CATALOG_SEQUENCE,
        catalog_mode=RELEASE_QUERY_CATALOG_MODE,
        work_signature_digest=_digest("production-abstention-work"),
        host_invocation_digest=_digest("production-abstention-invocation"),
    )


EXPECTED_CONTEXT = """CTX recommendation bundle (committed, advisory only):
1. kind=agent | name=reviewer | id=agent:reviewer | actionability=manual | score_ppm=600000
2. kind=skill | name=python-tdd | id=skill:python-tdd | actionability=manual | score_ppm=900000
Use only capabilities relevant to the current task. Do not install, load, or activate anything without user approval."""


def test_codex_adapter_preserves_the_transition_compatibility_exports() -> None:
    assert callable(render_prepared_context)
    assert callable(render_recommendation_context)


def test_codex_user_prompt_submit_projects_the_exact_committed_context() -> None:
    decision = _reviewed_decision()

    envelope = render_committed_query_hook(decision)

    assert envelope == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": EXPECTED_CONTEXT,
        }
    }


def test_codex_query_hook_abstains_for_committed_abstention() -> None:
    assert render_committed_query_hook(_production_abstention()) is None


def test_codex_query_hook_fails_soft_for_a_closed_failure() -> None:
    assert (
        render_committed_query_hook(QueryDecisionFailure(failure_code="catalog-open-failed"))
        is None
    )


def test_codex_query_hook_fails_soft_for_an_unsealed_value() -> None:
    assert render_committed_query_hook(object()) is None


def test_codex_query_hook_fails_soft_for_a_decision_sealed_for_another_host() -> None:
    foreign = _reviewed_decision(QueryHostDescriptor.claude_code())

    assert render_committed_query_hook(foreign) is None


def test_codex_query_hook_does_not_mutate_or_replan_the_decision() -> None:
    decision = _reviewed_decision()
    before = repr(decision)

    first = render_committed_query_hook(decision)
    second = render_committed_query_hook(decision)

    assert first == second
    assert repr(decision) == before


def test_codex_query_hook_has_no_policy_engine_or_mutation_dependencies() -> None:
    source = inspect.getsource(query_hook)
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden_prefixes = (
        "ctx.adapters.claude_code",
        "ctx.adapters.codex.engine_adapter",
        "ctx.cli",
        "ctx.core",
        "ctx.engine",
        "ctx.mcp_server",
        "ctx.runtime.production_catalog",
        "ctx.runtime.query_session",
    )
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert not any(name.startswith(forbidden_prefixes) for name in imported)
    assert calls.isdisjoint(
        {
            "connect",
            "install",
            "open",
            "sort",
            "sorted",
            "unlink",
            "write",
            "write_bytes",
            "write_text",
        }
    )
