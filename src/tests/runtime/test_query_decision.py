from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

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
    CapabilitySelection,
    CommittedQueryDecision,
    QueryDecisionFailure,
    QueryDecisionValidationError,
    QueryHostDescriptor,
    _capability_selections_from_committed_transition,
    _commit_query_decision,
    _receipt_digest,
    accept_query_decision,
    prepare_query_decision,
    render_query_decision_context,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selection() -> CapabilitySelection:
    return CapabilitySelection(
        capability_id="skill:python-tdd",
        kind="skill",
        name="python-tdd",
        actionability="load",
        normalized_score_ppm=900_000,
        source_digest=_digest("catalog-entry"),
        matching_signals=("python", "testing"),
        reason_codes=("signal-match",),
    )


def _reviewed_decision(host: QueryHostDescriptor) -> CommittedQueryDecision:
    plan, transition = _ready_plan_and_transition(host)
    return _commit_query_decision(
        host=host,
        plan=plan,
        transition=transition,
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
        release_root_digest=_digest("reviewed-release-root"),
        release_sequence=2,
        catalog_mode="reviewed",
        work_signature_digest=_digest("reviewed-work-signature"),
        host_invocation_digest=_digest("reviewed-host-invocation"),
    )


def test_host_descriptors_are_closed_and_bind_distinct_host_provenance() -> None:
    hosts = (
        QueryHostDescriptor.ctx_run(),
        QueryHostDescriptor.codex(),
        QueryHostDescriptor.claude_code(),
    )

    assert tuple(host.host_context_id for host in hosts) == (
        "ctx-run",
        "codex",
        "claude-code",
    )
    assert len({host.host_descriptor_digest for host in hosts}) == 3
    with pytest.raises(TypeError):

        class DerivedHost(QueryHostDescriptor):
            pass


def test_committed_decision_stores_only_safe_selections_and_derives_context() -> None:
    host = QueryHostDescriptor.codex()
    decision = _reviewed_decision(host)

    stored_fields = {field.name for field in dataclasses.fields(decision)}
    assert "recommendation_context" not in stored_fields
    assert "presentation_digest" in stored_fields
    assert decision.status == "presented"
    assert decision.recommendation_count == 1
    assert decision.failure_code is None
    assert decision.recommendation_context == render_query_decision_context(decision, host=host)
    assert decision.recommendation_context == (
        "CTX recommendation bundle (committed, advisory only):\n"
        "1. kind=agent | name=reviewer | id=agent:reviewer | "
        "actionability=manual | score_ppm=600000\n"
        "Use only capabilities relevant to the current task. "
        "Do not install, load, or activate anything without user approval."
    )
    assert "matching_signals" not in decision.recommendation_context
    assert "catalog_entry_digest" not in decision.recommendation_context
    assert "_receipt_seal" not in repr(decision)


def test_public_constructor_cannot_fabricate_a_committed_receipt() -> None:
    host = QueryHostDescriptor.codex()

    with pytest.raises(QueryDecisionValidationError, match="closed factory"):
        CommittedQueryDecision(
            host_context_id=host.host_context_id,
            host_descriptor_digest=host.host_descriptor_digest,
            capabilities=(_selection(),),
            plan_digest=_digest("plan"),
            catalog_snapshot_digest=_digest("catalog-snapshot"),
            journal_revision=2,
            journal_record_digest=_digest("journal-record"),
            release_root_digest=_digest("reviewed-release-root"),
            release_sequence=2,
            catalog_mode="reviewed",
            abstention_code=None,
            presentation_action_id="action-present-1",
            presentation_action_content_digest=_digest("present-action"),
            work_signature_digest=_digest("work-signature"),
            host_invocation_digest=_digest("host-invocation"),
            presentation_digest=_digest("invented-presentation"),
            receipt_digest=_digest("invented-receipt"),
            _receipt_seal=object(),
        )


def _ready_plan_and_transition(
    host: QueryHostDescriptor,
) -> tuple[CommittedPlanV3, Transition]:
    scope = ScopeRef(
        tenant_id="local",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id=host.host_context_id,
    )
    presentation = CapabilityCandidate(
        capability_id="agent:reviewer",
        kind="agent",
        name="reviewer",
        source_digest=_digest("catalog-entry:agent:reviewer"),
        normalized_score_ppm=600_000,
        matching_signals=("review",),
        reason_codes=("signal-match",),
        actionability="manual",
    )
    selection = CapabilityPlanSelectionV3(
        presentation=presentation,
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=presentation.capability_id,
            kind=presentation.kind,
            catalog_namespace_digest=_digest("catalog-namespace"),
        ),
        benefit=CapabilityBenefitProjection(
            tier="advisory",
            individual_net_benefit_u=600_000,
            marginal_net_benefit_u=600_000,
        ),
        authority=ManualPlanningAuthority(),
    )
    plan = CommittedPlanV3(
        plan_id="plan-1",
        catalog_snapshot_id=_digest("catalog-snapshot"),
        decision_digest=_digest("decision"),
        status="ready",
        abstention_code=None,
        benefit_audit=BenefitAuditReference(
            result_schema_id="ctx.benefit-result-v1",
            result_digest=_digest("result"),
            policy_schema_id="ctx.benefit-policy-v1",
            policy_digest=_digest("policy"),
            selection_algorithm_id="ctx.benefit-selection-v1",
            calibration_digest=_digest("calibration"),
            requested_limit=5,
            candidate_pool_count=1,
            search_evaluation_count=1,
        ),
        capabilities=(PlanCapabilityV3(selection=selection),),
    )
    transition = Transition(
        event_id="event-intent-1",
        scope=scope,
        from_revision=1,
        to_revision=2,
        actions=(
            HostAction(
                action_id="action-present-1",
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
    return plan, transition


def _current_abstention(host: QueryHostDescriptor) -> CommittedQueryDecision:
    scope = ScopeRef(
        tenant_id="local",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id=host.host_context_id,
    )
    plan = CommittedPlanV3(
        plan_id="plan-1",
        catalog_snapshot_id=_digest("catalog-snapshot"),
        decision_digest=_digest("abstained-decision"),
        status="abstained",
        abstention_code="no-feasible-capability",
        benefit_audit=BenefitAuditReference(
            result_schema_id="ctx.benefit-result-v1",
            result_digest=_digest("result"),
            policy_schema_id="ctx.benefit-policy-v1",
            policy_digest=_digest("policy"),
            selection_algorithm_id="ctx.benefit-selection-v1",
            calibration_digest=_digest("calibration"),
            requested_limit=5,
            candidate_pool_count=0,
            search_evaluation_count=0,
        ),
        capabilities=(),
    )
    transition = Transition(
        event_id="event-intent-1",
        scope=scope,
        from_revision=1,
        to_revision=2,
    )
    return _commit_query_decision(
        host=host,
        plan=plan,
        transition=transition,
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
        release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
        release_sequence=RELEASE_QUERY_CATALOG_SEQUENCE,
        catalog_mode=RELEASE_QUERY_CATALOG_MODE,
        work_signature_digest=_digest("current-work-signature"),
        host_invocation_digest=_digest("current-host-invocation"),
    )


@pytest.mark.parametrize("mutation", ["plan-digest", "full-v3-row"])
def test_commit_projection_rejects_substitution_between_transition_and_plan(
    mutation: str,
) -> None:
    plan, transition = _ready_plan_and_transition(QueryHostDescriptor.codex())
    action = transition.actions[0]
    payload = {
        "plan_digest": action.payload["plan_digest"],
        "capabilities": [dict(action.payload["capabilities"][0])],
    }
    if mutation == "plan-digest":
        payload["plan_digest"] = _digest("substituted-plan")
    else:
        row = payload["capabilities"][0]
        assert isinstance(row, dict)
        row["normalized_score_ppm"] = 599_999
    substituted = dataclasses.replace(
        transition,
        actions=(dataclasses.replace(action, payload=payload),),
    )

    with pytest.raises(QueryDecisionValidationError, match="committed plan"):
        _capability_selections_from_committed_transition(substituted, plan)


def test_commit_projection_rejects_an_extra_non_presentation_action() -> None:
    host = QueryHostDescriptor.codex()
    plan, transition = _ready_plan_and_transition(host)
    substituted = dataclasses.replace(
        transition,
        actions=(
            *transition.actions,
            HostAction(
                action_id="action-unexpected-notify",
                kind="Notify",
                scope=transition.scope,
                precondition_revision=2,
                payload={"message": "unexpected"},
            ),
        ),
    )

    with pytest.raises(QueryDecisionValidationError, match="committed plan"):
        _capability_selections_from_committed_transition(substituted, plan)


def test_acceptance_copies_exact_values_and_rejects_false_host_provenance() -> None:
    codex = QueryHostDescriptor.codex()
    decision = _reviewed_decision(codex)

    accepted = accept_query_decision(decision, host=codex)

    assert accepted == decision
    assert accepted is not decision
    assert accepted.capabilities[0] is not decision.capabilities[0]
    with pytest.raises(QueryDecisionValidationError, match="host provenance"):
        accept_query_decision(decision, host=QueryHostDescriptor.claude_code())
    with pytest.raises(TypeError, match="sealed"):

        class DerivedDecision(CommittedQueryDecision):
            pass


def test_acceptance_rejects_coherent_host_rebranding_and_journal_substitution() -> None:
    rebranded = _reviewed_decision(QueryHostDescriptor.claude_code())
    codex = QueryHostDescriptor.codex()
    object.__setattr__(rebranded, "host_context_id", codex.host_context_id)
    object.__setattr__(
        rebranded,
        "host_descriptor_digest",
        codex.host_descriptor_digest,
    )
    object.__setattr__(
        rebranded,
        "receipt_digest",
        _receipt_digest(
            host_context_id=rebranded.host_context_id,
            host_descriptor_digest=rebranded.host_descriptor_digest,
            journal_revision=rebranded.journal_revision,
            journal_record_digest=rebranded.journal_record_digest,
            presentation_digest=rebranded.presentation_digest,
            presentation_action_id=rebranded.presentation_action_id,
            presentation_action_content_digest=(rebranded.presentation_action_content_digest),
            work_signature_digest=rebranded.work_signature_digest,
            host_invocation_digest=rebranded.host_invocation_digest,
        ),
    )
    substituted_journal = _reviewed_decision(QueryHostDescriptor.claude_code())
    object.__setattr__(
        substituted_journal,
        "journal_record_digest",
        _digest("substituted-journal-record"),
    )
    object.__setattr__(
        substituted_journal,
        "receipt_digest",
        _receipt_digest(
            host_context_id=substituted_journal.host_context_id,
            host_descriptor_digest=substituted_journal.host_descriptor_digest,
            journal_revision=substituted_journal.journal_revision,
            journal_record_digest=substituted_journal.journal_record_digest,
            presentation_digest=substituted_journal.presentation_digest,
            presentation_action_id=substituted_journal.presentation_action_id,
            presentation_action_content_digest=(
                substituted_journal.presentation_action_content_digest
            ),
            work_signature_digest=substituted_journal.work_signature_digest,
            host_invocation_digest=substituted_journal.host_invocation_digest,
        ),
    )

    with pytest.raises(QueryDecisionValidationError):
        accept_query_decision(rebranded, host=codex)
    with pytest.raises(QueryDecisionValidationError):
        accept_query_decision(
            substituted_journal,
            host=QueryHostDescriptor.claude_code(),
        )


def test_acceptance_normalizes_hostile_exact_field_values() -> None:
    class HostileHostId(str):
        def __hash__(self) -> int:
            raise RuntimeError("secret=/private/repository/path")

    decision = _reviewed_decision(QueryHostDescriptor.codex())
    object.__setattr__(decision, "host_context_id", HostileHostId("codex"))

    with pytest.raises(QueryDecisionValidationError) as raised:
        accept_query_decision(decision, host=QueryHostDescriptor.codex())

    assert "secret" not in str(raised.value)
    assert "/private" not in str(raised.value)


def test_current_reviewed_release_can_commit_an_exact_presented_bundle() -> None:
    host = QueryHostDescriptor.ctx_run()

    plan, transition = _ready_plan_and_transition(host)

    decision = _commit_query_decision(
        host=host,
        plan=plan,
        transition=transition,
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
        release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
        release_sequence=RELEASE_QUERY_CATALOG_SEQUENCE,
        catalog_mode=RELEASE_QUERY_CATALOG_MODE,
        work_signature_digest=_digest("current-work-signature"),
        host_invocation_digest=_digest("current-host-invocation"),
    )

    assert decision.status == "presented"
    assert tuple(item.capability_id for item in decision.capabilities) == ("agent:reviewer",)
    assert decision.journal_revision == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host_descriptor_digest", _digest("substituted-host")),
        ("release_sequence", RELEASE_QUERY_CATALOG_SEQUENCE + 1),
        ("catalog_mode", "abstention-only"),
    ],
)
def test_acceptance_rejects_substituted_current_release_or_host_fields(
    field: str,
    value: object,
) -> None:
    host = QueryHostDescriptor.ctx_run()
    decision = _current_abstention(host)
    unsafe = object.__new__(CommittedQueryDecision)
    for data_field in dataclasses.fields(decision):
        object.__setattr__(
            unsafe,
            data_field.name,
            value if data_field.name == field else getattr(decision, data_field.name),
        )

    with pytest.raises(QueryDecisionValidationError):
        accept_query_decision(unsafe, host=host)


def test_failure_is_separate_bounded_and_contains_no_decision_receipt() -> None:
    failure = QueryDecisionFailure(failure_code="catalog-open-failed")

    assert failure.status == "failed"
    assert failure.recommendation_context is None
    assert failure.recommendation_count == 0
    assert render_query_decision_context(failure, host=QueryHostDescriptor.codex()) is None
    with pytest.raises(QueryDecisionValidationError):
        QueryDecisionFailure(failure_code="failure /private/repository token=secret")


@pytest.mark.parametrize(
    "host",
    [
        QueryHostDescriptor.ctx_run(),
        QueryHostDescriptor.codex(),
        QueryHostDescriptor.claude_code(),
    ],
    ids=lambda host: host.host_context_id,
)
def test_production_factory_commits_the_same_closed_abstention_for_each_host(
    host: QueryHostDescriptor,
    tmp_path: Path,
) -> None:
    host_root = tmp_path / host.host_context_id
    task = "Repair token=do-not-journal at /private/repository/path"

    result = prepare_query_decision(
        host=host,
        task=task,
        language="Python",
        session_id=f"session-{host.host_context_id}",
        workspace=tmp_path,
        journal_path=host_root / "session.engine.sqlite3",
        benefit_audit_path=host_root / "session.benefit.sqlite3",
        host_invocation_digest=_digest(f"host-invocation:{host.host_context_id}"),
    )

    assert isinstance(result, CommittedQueryDecision)
    assert result.status == "abstained"
    assert result.capabilities == ()
    assert result.abstention_code is not None
    assert result.host_context_id == host.host_context_id
    assert result.host_descriptor_digest == host.host_descriptor_digest
    assert result.journal_revision == 2
    assert result.release_root_digest == RELEASE_QUERY_CATALOG_ROOT_SHA256
    assert result.release_sequence == RELEASE_QUERY_CATALOG_SEQUENCE
    assert result.catalog_mode == RELEASE_QUERY_CATALOG_MODE
    journal_bytes = (host_root / "session.engine.sqlite3").read_bytes()
    assert task.encode() not in journal_bytes
    assert b"do-not-journal" not in journal_bytes
    assert str(tmp_path).encode() not in journal_bytes


def test_host_brand_does_not_perturb_the_semantic_plan_or_presentation(
    tmp_path: Path,
) -> None:
    results: list[CommittedQueryDecision] = []
    for host in (
        QueryHostDescriptor.ctx_run(),
        QueryHostDescriptor.codex(),
        QueryHostDescriptor.claude_code(),
    ):
        host_root = tmp_path / host.host_context_id
        result = prepare_query_decision(
            host=host,
            task="Review the Python implementation",
            language="Python",
            session_id="same-session",
            workspace=tmp_path,
            journal_path=host_root / "session.engine.sqlite3",
            benefit_audit_path=host_root / "session.benefit.sqlite3",
            host_invocation_digest=_digest("same-host-invocation"),
        )
        assert isinstance(result, CommittedQueryDecision)
        results.append(result)

    assert len({result.plan_digest for result in results}) == 1
    assert len({result.catalog_snapshot_digest for result in results}) == 1
    assert len({result.presentation_digest for result in results}) == 1
    assert len({result.receipt_digest for result in results}) == 3
    assert len({result.host_descriptor_digest for result in results}) == 3


def test_host_receipt_binds_exact_action_work_and_invocation_without_changing_semantics() -> None:
    host = QueryHostDescriptor.codex()
    plan, transition = _ready_plan_and_transition(host)
    first = _commit_query_decision(
        host=host,
        plan=plan,
        transition=transition,
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
        release_root_digest=_digest("reviewed-release-root"),
        release_sequence=2,
        catalog_mode="reviewed",
        work_signature_digest=_digest("work-a"),
        host_invocation_digest=_digest("invocation-a"),
    )
    substituted_action = dataclasses.replace(
        transition,
        actions=(dataclasses.replace(transition.actions[0], action_id="action-present-2"),),
    )
    second = _commit_query_decision(
        host=host,
        plan=plan,
        transition=substituted_action,
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
        release_root_digest=_digest("reviewed-release-root"),
        release_sequence=2,
        catalog_mode="reviewed",
        work_signature_digest=_digest("work-b"),
        host_invocation_digest=_digest("invocation-b"),
    )

    assert first.presentation_digest == second.presentation_digest
    assert first.presentation_action_id == "action-present-1"
    assert second.presentation_action_id == "action-present-2"
    assert first.presentation_action_content_digest == transition.actions[0].content_digest
    assert second.presentation_action_content_digest == substituted_action.actions[0].content_digest
    assert first.work_signature_digest == _digest("work-a")
    assert second.work_signature_digest == _digest("work-b")
    assert first.host_invocation_digest == _digest("invocation-a")
    assert second.host_invocation_digest == _digest("invocation-b")
    assert first.receipt_digest != second.receipt_digest


@pytest.mark.parametrize(
    "field",
    [
        "presentation_action_id",
        "presentation_action_content_digest",
        "work_signature_digest",
        "host_invocation_digest",
    ],
)
def test_acceptance_rejects_coherently_rehashed_host_receipt_substitution(field: str) -> None:
    host = QueryHostDescriptor.codex()
    decision = _reviewed_decision(host)
    replacement: str | None = (
        "substituted-present-action"
        if field == "presentation_action_id"
        else _digest(f"substituted:{field}")
    )
    object.__setattr__(decision, field, replacement)
    object.__setattr__(
        decision,
        "receipt_digest",
        _receipt_digest(
            host_context_id=decision.host_context_id,
            host_descriptor_digest=decision.host_descriptor_digest,
            journal_revision=decision.journal_revision,
            journal_record_digest=decision.journal_record_digest,
            presentation_digest=decision.presentation_digest,
            presentation_action_id=decision.presentation_action_id,
            presentation_action_content_digest=decision.presentation_action_content_digest,
            work_signature_digest=decision.work_signature_digest,
            host_invocation_digest=decision.host_invocation_digest,
        ),
    )

    with pytest.raises(QueryDecisionValidationError):
        accept_query_decision(decision, host=host)
