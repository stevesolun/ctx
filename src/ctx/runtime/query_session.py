"""Compatibility entry point for the host-neutral closed query factory.

Raw task text is normalized only inside the release-pinned catalog.  The
authoritative engine journal receives an opaque one-use observation reference
and its bounded surrogate, never task prose, source text, or repository paths.
This module performs no provider call and returns no live catalog, engine,
planner, material, installation, or receipt authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from ctx.engine.benefit_audit_store import SQLiteBenefitAuditStore
from ctx.engine.engine import (
    CtxEngine,
    EngineSnapshot,
    _PromptContextMaterialPermit,
    _PromptContextReceiptPermit,
)
from ctx.engine.planning_v3 import (
    AuthenticatedNetBenefitPlanner,
    CapabilityPlanSelectionV3,
    LoadPlanningAuthority,
)
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.protocol import (
    PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
    EngineEvent,
    HostAction,
    PrivacyLabel,
    ScopeRef,
    Transition,
)
from ctx.engine.reducer import INSTALLATION_REDUCER_VERSION, PROMPT_CONTEXT_REDUCER_VERSION
from ctx.engine.replay import DefaultReplayInputFactory
from ctx.engine.state import CommittedPlanV3
from ctx.engine.store import SQLiteEngineStore
from ctx.runtime.authenticated_benefit import capability_presentation_digest
from ctx.runtime.activated_skill_availability import (
    ActivatedSkillQueryAvailability,
)
from ctx.runtime.benefit_closure import (
    EligibleCatalogClaim,
    QueryCapabilityEligibility,
    eligible_catalog_claim_digest,
)
from ctx.runtime.planning_v3 import AuthenticatedReplayDecisionPlannerV3
from ctx.runtime.production_catalog import (
    open_release_pinned_query_catalog,
)
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_ID
from ctx.runtime.prepared_query_delivery import (
    PreparedQueryDelivery,
    _create_prepared_query_delivery,
    render_prepared_prompt_context,
)
from ctx.runtime.eligible_catalog import PreparedEligibleCatalogQuery
from ctx.runtime.query_decision import (
    CommittedQueryDecision,
    QueryDecisionFailure,
    QueryDecisionResult,
    QueryHostDescriptor,
    _commit_query_decision,
)
from ctx.runtime.query_observation import QueryObservationRegistry
from ctx.runtime.workspace_identity import capture_workspace_identity


CTX_RUN_QUERY_ENGINE_VERSION: Final = "ctx-query-engine-v1"
CTX_RUN_QUERY_PLANNER_VERSION: Final = "ctx-query-planner-v3"
_SEMANTIC_MODEL_DIGEST: Final = hashlib.sha256(b"ctx-query-lexical-v1").hexdigest()
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


CtxRunQueryDecision = CommittedQueryDecision


@dataclass(frozen=True, slots=True)
class _QueryHostPolicy:
    """Conservative query-only feasibility facts, never installation consent."""

    host_policy_snapshot_digest: str

    @classmethod
    def current(cls) -> _QueryHostPolicy:
        return cls(
            host_policy_snapshot_digest=_digest(
                {
                    "mode": "query-only",
                    "platform": os.name,
                    "schema": "ctx-query-host-policy-v1",
                    "supported_actionability": ["load", "manual"],
                }
            )
        )

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility:
        available = presentation.actionability in {"load", "manual"}
        if presentation.capability_id == RELEASE_INSTALL_SKILL_ID:
            available = False
        return QueryCapabilityEligibility(
            presentation_digest=capability_presentation_digest(presentation),
            catalog_entry_claim_digest=claim.catalog_entry_claim_digest,
            catalog_claim_digest=eligible_catalog_claim_digest(claim),
            available=available,
            permissions_allowed=available,
            credentials_available=available,
        )


class _QueryOnlyEngine:
    """Narrow lifetime facade for one closed recommendation or prompt context."""

    __slots__ = ("_closed", "_engine", "_host", "_processed")

    def __init__(self, engine: CtxEngine, host: QueryHostDescriptor) -> None:
        if not isinstance(engine, CtxEngine):
            raise TypeError("query-only engine requires a CtxEngine")
        self._engine = engine
        self._host = host
        self._processed = 0
        self._closed = False

    def process(self, event: EngineEvent) -> Transition:
        if self._closed:
            raise RuntimeError("query-only engine is closed")
        if self._processed >= 2:
            if (
                self._processed != 2
                or self._host.execution_intent == "recommend"
                or event.kind not in {"ActionApplied", "ActionFailed"}
            ):
                raise ValueError("query-only engine event order is invalid")
            expected_kind = event.kind
        else:
            expected_kind = ("SessionStarted", "IntentObserved")[self._processed]
        if self._processed > 2:
            raise ValueError("query-only engine event order is invalid")
        if event.kind != expected_kind:
            raise ValueError("query-only engine event order is invalid")
        transition = self._engine.process(event)
        if self._processed == 0:
            if transition.actions:
                raise ValueError("query-only session start emitted a host action")
        elif self._processed == 1:
            actions = transition.actions
            allowed = (
                {(), ("PresentBundle",)}
                if self._host.execution_intent == "recommend"
                else {(), ("PresentBundle", "PreparePromptContext")}
            )
            if tuple(action.kind for action in actions) not in allowed:
                raise ValueError("query-only decision emitted a non-presentation action")
        elif transition.actions:
            raise ValueError("query-only prompt context receipt emitted a host action")
        self._processed += 1
        return transition

    def snapshot(self, scope: ScopeRef) -> EngineSnapshot:
        if self._closed:
            raise RuntimeError("query-only engine is closed")
        if self._processed not in {2, 3}:
            raise RuntimeError("query-only snapshot requires the committed decision")
        return self._engine.snapshot(scope)

    def authorize_prompt_context(
        self,
        action: object,
        selections: object,
        *,
        expected_catalog_snapshot_digest: str,
    ) -> None:
        self._engine.authorize_prompt_context(
            action,  # type: ignore[arg-type]
            selections,  # type: ignore[arg-type]
            expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
        )

    def issue_prompt_context_material_permit(
        self,
        action: HostAction,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        *,
        expected_catalog_snapshot_digest: str,
    ) -> _PromptContextMaterialPermit:
        if self._closed or self._processed != 2:
            raise RuntimeError("prompt context material permit requires a committed plan")
        return self._engine._issue_prompt_context_material_permit(  # noqa: SLF001
            action,
            selections,
            expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
        )

    def issue_prompt_context_receipt_permit(
        self,
        action: HostAction,
        receipt_event: EngineEvent,
    ) -> _PromptContextReceiptPermit:
        if self._closed or self._processed != 3:
            raise RuntimeError("prompt context receipt permit requires the committed receipt")
        return self._engine._issue_prompt_context_receipt_permit(action, receipt_event)

    def close(self) -> None:
        self._closed = True


def _scope(
    *,
    host: QueryHostDescriptor,
    session_id: str,
    workspace: Path,
) -> ScopeRef:
    workspace_digest = capture_workspace_identity(workspace).digest
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return ScopeRef(
        tenant_id="local",
        workspace_id=f"workspace-{workspace_digest}",
        repository_id=f"repository-{workspace_digest}",
        session_id=session_id,
        exposure_id=f"exposure-{session_digest}",
        host_context_id=host.host_context_id,
    )


def _occurred_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event(
    *,
    host: QueryHostDescriptor,
    kind: str,
    expected_revision: int,
    scope: ScopeRef,
    payload: dict[str, object],
    event_suffix: str,
    occurred_at: str,
    planner: AuthenticatedReplayDecisionPlannerV3,
    work_signature: str,
    policy_digest: str,
    semantic_index_digest: str,
) -> EngineEvent:
    session_digest = hashlib.sha256(scope.session_id.encode("utf-8")).hexdigest()
    host_prefix = f"ctx-query-{host.host_context_id}"
    start_id = f"{host_prefix}-start-{session_digest[:24]}"
    event_id = f"{host_prefix}-{event_suffix}-{session_digest[:24]}"
    correlation_id = f"{host_prefix}-{session_digest[:24]}"
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=scope,
        expected_revision=expected_revision,
        occurred_at=occurred_at,
        payload=payload,
        privacy=PrivacyLabel(classification="private", retention="local"),
        correlation_id=correlation_id,
        causation_id=host.host_context_id if kind == "SessionStarted" else start_id,
        engine_version=CTX_RUN_QUERY_ENGINE_VERSION,
        planner_version=CTX_RUN_QUERY_PLANNER_VERSION,
        policy_version=policy_digest,
        host_descriptor_digest=host.host_descriptor_digest,
        catalog_snapshot_digest=planner.catalog_snapshot_digest,
        semantic_model_digest=_SEMANTIC_MODEL_DIGEST,
        semantic_index_digest=semantic_index_digest,
        work_signature=work_signature,
        random_seed=0,
    )


def _open_query_engine(
    *,
    prepared: PreparedEligibleCatalogQuery,
    registry: QueryObservationRegistry,
    journal_path: Path,
    benefit_audit_path: Path,
    host: QueryHostDescriptor,
) -> tuple[_QueryOnlyEngine, AuthenticatedReplayDecisionPlannerV3]:
    closure = prepared.closure
    planner = AuthenticatedReplayDecisionPlannerV3(
        planner=AuthenticatedNetBenefitPlanner(
            policy=closure.policy,
            audit_store=SQLiteBenefitAuditStore(benefit_audit_path),
        ),
        source=closure.source,
        benefit_facts_port=closure.benefit_facts,
        material_port=prepared.material_authority,
        install_bundle_port=prepared.install_authority,
        planner_version=CTX_RUN_QUERY_PLANNER_VERSION,
        catalog_namespace_digest=closure.catalog_namespace_digest,
    )
    replay_factory = DefaultReplayInputFactory(
        observation_normalizer=registry,
        decision_planner=planner,
        reducer_version=(
            INSTALLATION_REDUCER_VERSION
            if host.execution_intent == "recommend"
            else PROMPT_CONTEXT_REDUCER_VERSION
        ),
    )
    return (
        _QueryOnlyEngine(
            CtxEngine(
                store=SQLiteEngineStore(journal_path),
                replay_factory=replay_factory,
            ),
            host,
        ),
        planner,
    )


def _validate_inputs(
    *,
    task: object,
    language: object,
    session_id: object,
    workspace: object,
    journal_path: object,
    benefit_audit_path: object,
    host_invocation_digest: object,
) -> tuple[Path, Path]:
    if not isinstance(task, str):
        raise TypeError("task must be a string")
    if not isinstance(language, str):
        raise TypeError("language must be a string")
    if not isinstance(session_id, str) or _TOKEN_RE.fullmatch(session_id) is None:
        raise ValueError("session_id must be a canonical safe token")
    if (
        type(host_invocation_digest) is not str
        or _SHA256_RE.fullmatch(host_invocation_digest) is None
    ):
        raise ValueError("host_invocation_digest must be a lowercase SHA-256 digest")
    for field_name, value in (
        ("workspace", workspace),
        ("journal_path", journal_path),
        ("benefit_audit_path", benefit_audit_path),
    ):
        if not isinstance(value, Path):
            raise TypeError(f"{field_name} must be a Path")
    assert isinstance(journal_path, Path)
    assert isinstance(benefit_audit_path, Path)
    normalized_journal_path = Path(os.path.abspath(os.fspath(journal_path)))
    normalized_benefit_audit_path = Path(os.path.abspath(os.fspath(benefit_audit_path)))
    if normalized_journal_path == normalized_benefit_audit_path:
        raise ValueError("journal and benefit audit paths must be distinct")
    try:
        same_existing_file = (
            normalized_journal_path.exists()
            and normalized_benefit_audit_path.exists()
            and os.path.samefile(normalized_journal_path, normalized_benefit_audit_path)
        )
    except OSError:
        same_existing_file = False
    if same_existing_file:
        raise ValueError("journal and benefit audit paths must not alias the same file")
    return normalized_journal_path, normalized_benefit_audit_path


def _prepare_query_decision(
    *,
    host: QueryHostDescriptor,
    task: str,
    language: str,
    session_id: str,
    workspace: Path,
    journal_path: Path,
    benefit_audit_path: Path,
    host_invocation_digest: str,
) -> QueryDecisionResult:
    """Commit one release-pinned query decision and close all live authority.

    Operational or validation failures become a stable stage code for the host
    circuit breaker.  Invalid direct caller types remain programming errors.
    """

    if type(host) is not QueryHostDescriptor:
        raise TypeError("host must be an exact QueryHostDescriptor")
    if host.execution_intent != "recommend":
        return QueryDecisionFailure(failure_code="explicit-factory-required")
    journal_path, benefit_audit_path = _validate_inputs(
        task=task,
        language=language,
        session_id=session_id,
        workspace=workspace,
        journal_path=journal_path,
        benefit_audit_path=benefit_audit_path,
        host_invocation_digest=host_invocation_digest,
    )
    stack = ExitStack()
    decision: CommittedQueryDecision | None = None
    failure_code: str | None = None
    stage = "catalog-open"
    interrupted = False
    try:
        registry = QueryObservationRegistry(provider_id=f"{host.host_context_id}-query")
        stack.callback(registry.close)
        catalog = open_release_pinned_query_catalog()
        stack.callback(catalog.close)
        release_sequence = catalog.release_sequence
        catalog_mode = catalog.mode
        release_root_digest = catalog.release_root_digest

        stage = "catalog-prepare"
        prepared = catalog.prepare_query(
            task=task,
            language=language,
            host_policy=_QueryHostPolicy.current(),
        )
        stack.callback(prepared.close)
        reference = registry.register(prepared.closure.observation)

        stage = "engine-open"
        query_engine, planner = _open_query_engine(
            prepared=prepared,
            registry=registry,
            journal_path=journal_path,
            benefit_audit_path=benefit_audit_path,
            host=host,
        )
        stack.callback(query_engine.close)
        scope = _scope(host=host, session_id=session_id, workspace=workspace)
        occurred_at = _occurred_at()

        stage = "engine-process"
        query_engine.process(
            _event(
                host=host,
                kind="SessionStarted",
                expected_revision=0,
                scope=scope,
                payload={"host_level": host.engine_host_level},
                event_suffix="start",
                occurred_at=occurred_at,
                planner=planner,
                work_signature=reference.content_digest,
                policy_digest=prepared.closure.policy_digest,
                semantic_index_digest=prepared.closure.catalog_retrieval_snapshot_digest,
            )
        )
        transition = query_engine.process(
            _event(
                host=host,
                kind="IntentObserved",
                expected_revision=1,
                scope=scope,
                payload={
                    "observation_ref": {
                        "provider_id": reference.provider_id,
                        "opaque_id": reference.opaque_id,
                        "content_digest": reference.content_digest,
                    }
                },
                event_suffix="intent",
                occurred_at=occurred_at,
                planner=planner,
                work_signature=reference.content_digest,
                policy_digest=prepared.closure.policy_digest,
                semantic_index_digest=prepared.closure.catalog_retrieval_snapshot_digest,
            )
        )

        stage = "engine-snapshot"
        snapshot = query_engine.snapshot(scope)
        plan = None if snapshot.state is None else snapshot.state.committed_plan
        if (
            snapshot.revision != 2
            or not isinstance(plan, CommittedPlanV3)
            or snapshot.record_digest is None
            or transition.to_revision != 2
            or transition.scope != scope
        ):
            raise ValueError("query decision did not commit one exact schema-v3 plan")

        stage = "decision-bind"
        if plan.status == "ready":
            if not 1 <= len(plan.capabilities) <= 5:
                raise ValueError("ready query plan has no exact presentation")
        elif plan.status == "abstained":
            if plan.capabilities or plan.abstention_code is None:
                raise ValueError("abstained query plan produced a presentation")
        else:
            raise ValueError("query-only planning degraded instead of deciding")
        decision = _commit_query_decision(
            host=host,
            transition=transition,
            plan=plan,
            journal_revision=snapshot.revision,
            journal_record_digest=snapshot.record_digest,
            release_root_digest=release_root_digest,
            release_sequence=release_sequence,
            catalog_mode=catalog_mode,
            work_signature_digest=reference.content_digest,
            host_invocation_digest=host_invocation_digest,
        )
    except Exception:
        failure_code = f"{stage}-failed"
    except BaseException:
        interrupted = True
        raise
    finally:
        try:
            stack.close()
        except Exception:
            if not interrupted and failure_code is None:
                failure_code = "cleanup-failed"
        except BaseException:
            if not interrupted:
                raise
    if failure_code is not None:
        return QueryDecisionFailure(failure_code=failure_code)
    if decision is None:
        return QueryDecisionFailure(failure_code="decision-missing")
    return decision


def _prompt_context_receipt_event(
    *,
    host: QueryHostDescriptor,
    action: HostAction,
    applied: bool,
    context_sha256: str | None,
    context_bytes: int | None,
    capabilities: tuple[dict[str, object], ...],
    occurred_at: str,
) -> EngineEvent:
    if applied:
        payload: dict[str, object] = {
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "verification": {
                "schema": PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
                "host_state": "prompt-context-prepared",
                "prompt_context_sha256": context_sha256,
                "prompt_context_bytes": context_bytes,
                "capabilities": list(capabilities),
            },
        }
        kind = "ActionApplied"
    else:
        payload = {
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "error": {"code": "prompt-context-preparation-failed"},
        }
        kind = "ActionFailed"
    return EngineEvent(
        event_id=f"{action.action_id}-receipt",
        kind=kind,
        scope=action.scope,
        expected_revision=action.precondition_revision,
        occurred_at=occurred_at,
        payload=payload,
        privacy=action.privacy,
        correlation_id=action.plan_id,
        causation_id=action.action_id,
        host_descriptor_digest=host.host_descriptor_digest,
        catalog_snapshot_digest=action.catalog_snapshot_id,
    )


def prepare_query_delivery(
    *,
    host: QueryHostDescriptor,
    task: str,
    language: str,
    session_id: str,
    workspace: Path,
    journal_path: Path,
    benefit_audit_path: Path,
    host_invocation_digest: str,
    managed_availability: ActivatedSkillQueryAvailability | None = None,
) -> PreparedQueryDelivery | QueryDecisionResult:
    """Prepare one explicit, engine-authorized ephemeral prompt bundle."""

    if type(host) is not QueryHostDescriptor:
        raise TypeError("host must be an exact QueryHostDescriptor")
    if host.execution_intent not in {"activate", "experiment"}:
        raise ValueError("prepared query delivery requires activate or experiment intent")
    if host.execution_intent == "experiment":
        return QueryDecisionFailure(failure_code="experiment-authorization-required")
    if (
        managed_availability is not None
        and type(managed_availability) is not ActivatedSkillQueryAvailability
    ):
        raise TypeError("managed_availability must be an exact availability snapshot or None")
    journal_path, benefit_audit_path = _validate_inputs(
        task=task,
        language=language,
        session_id=session_id,
        workspace=workspace,
        journal_path=journal_path,
        benefit_audit_path=benefit_audit_path,
        host_invocation_digest=host_invocation_digest,
    )
    stack = ExitStack()
    result: PreparedQueryDelivery | CommittedQueryDecision | None = None
    failure_code: str | None = None
    stage = "catalog-open"
    interrupted = False
    occurred_at = _occurred_at()
    availability = managed_availability
    try:
        registry = QueryObservationRegistry(provider_id=f"{host.host_context_id}-query")
        stack.callback(registry.close)
        catalog = open_release_pinned_query_catalog()
        stack.callback(catalog.close)

        stage = "catalog-prepare"
        prepared = catalog.prepare_query(
            task=task,
            language=language,
            host_policy=(availability or _QueryHostPolicy.current()),
        )
        stack.callback(prepared.close)
        reference = registry.register(prepared.closure.observation)

        stage = "engine-open"
        query_engine, planner = _open_query_engine(
            prepared=prepared,
            registry=registry,
            journal_path=journal_path,
            benefit_audit_path=benefit_audit_path,
            host=host,
        )
        stack.callback(query_engine.close)
        scope = _scope(host=host, session_id=session_id, workspace=workspace)
        stage = "engine-process"
        query_engine.process(
            _event(
                host=host,
                kind="SessionStarted",
                expected_revision=0,
                scope=scope,
                payload={"host_level": host.engine_host_level},
                event_suffix="start",
                occurred_at=occurred_at,
                planner=planner,
                work_signature=reference.content_digest,
                policy_digest=prepared.closure.policy_digest,
                semantic_index_digest=prepared.closure.catalog_retrieval_snapshot_digest,
            )
        )
        transition = query_engine.process(
            _event(
                host=host,
                kind="IntentObserved",
                expected_revision=1,
                scope=scope,
                payload={
                    "observation_ref": {
                        "provider_id": reference.provider_id,
                        "opaque_id": reference.opaque_id,
                        "content_digest": reference.content_digest,
                    }
                },
                event_suffix="intent",
                occurred_at=occurred_at,
                planner=planner,
                work_signature=reference.content_digest,
                policy_digest=prepared.closure.policy_digest,
                semantic_index_digest=prepared.closure.catalog_retrieval_snapshot_digest,
            )
        )
        snapshot = query_engine.snapshot(scope)
        plan = None if snapshot.state is None else snapshot.state.committed_plan
        if (
            snapshot.revision != 2
            or not isinstance(plan, CommittedPlanV3)
            or snapshot.record_digest is None
            or transition.to_revision != 2
            or transition.scope != scope
        ):
            raise ValueError("explicit query did not commit one exact schema-v3 plan")
        decision = _commit_query_decision(
            host=host,
            transition=transition,
            plan=plan,
            journal_revision=snapshot.revision,
            journal_record_digest=snapshot.record_digest,
            release_root_digest=catalog.release_root_digest,
            release_sequence=catalog.release_sequence,
            catalog_mode=catalog.mode,
            work_signature_digest=reference.content_digest,
            host_invocation_digest=host_invocation_digest,
        )
        if plan.status == "abstained":
            if transition.actions or plan.capabilities or plan.abstention_code is None:
                raise ValueError("explicit abstention emitted a host action")
            result = decision
        elif plan.status == "ready":
            if tuple(action.kind for action in transition.actions) != (
                "PresentBundle",
                "PreparePromptContext",
            ):
                raise ValueError("explicit query did not commit one prompt context action")
            action = transition.actions[1]
            selections: tuple[CapabilityPlanSelectionV3, ...] = tuple(
                row.selection
                for row in plan.capabilities
                if isinstance(row.authority, LoadPlanningAuthority)
            )
            stage = "prompt-context-prepare"
            try:
                material_authority = query_engine.issue_prompt_context_material_permit(
                    action,
                    selections,
                    expected_catalog_snapshot_digest=plan.catalog_snapshot_id,
                )
                if availability is None:
                    contents = catalog.prepare_prompt_context(
                        action,
                        selections,
                        expected_catalog_snapshot_digest=plan.catalog_snapshot_id,
                        authority=material_authority,
                    )
                else:
                    contents = availability.prepare_prompt_context(
                        catalog=catalog,
                        action=action,
                        selections=selections,
                        expected_catalog_snapshot_digest=plan.catalog_snapshot_id,
                        authority=material_authority,
                    )
                context = render_prepared_prompt_context(contents)
            except Exception:
                failed_event = _prompt_context_receipt_event(
                    host=host,
                    action=action,
                    applied=False,
                    context_sha256=None,
                    context_bytes=None,
                    capabilities=(),
                    occurred_at=_occurred_at(),
                )
                query_engine.process(failed_event)
                failure_code = "prompt-context-prepare-failed"
            else:
                encoded = context.encode("utf-8")
                receipt_capabilities = tuple(
                    {
                        "capability_id": item.capability_id,
                        "content_sha256": item.content_sha256,
                        "content_bytes": item.content_bytes,
                    }
                    for item in contents
                )
                receipt_event = _prompt_context_receipt_event(
                    host=host,
                    action=action,
                    applied=True,
                    context_sha256=hashlib.sha256(encoded).hexdigest(),
                    context_bytes=len(encoded),
                    capabilities=receipt_capabilities,
                    occurred_at=_occurred_at(),
                )
                query_engine.process(receipt_event)
                final_snapshot = query_engine.snapshot(scope)
                if final_snapshot.revision != 3 or final_snapshot.record_digest is None:
                    raise ValueError("prompt context receipt did not commit exactly revision three")
                receipt_authority = query_engine.issue_prompt_context_receipt_permit(
                    action,
                    receipt_event,
                )
                result = _create_prepared_query_delivery(
                    decision=decision,
                    execution_intent=host.execution_intent,
                    action=action,
                    receipt_event=receipt_event,
                    contents=contents,
                    receipt_authority=receipt_authority,
                )
        else:
            raise ValueError("explicit query planning degraded instead of deciding")
    except Exception:
        if failure_code is None:
            failure_code = f"{stage}-failed"
    except BaseException:
        interrupted = True
        raise
    finally:
        try:
            stack.close()
        except Exception:
            if not interrupted and failure_code is None:
                failure_code = "cleanup-failed"
        except BaseException:
            if not interrupted:
                raise
    if failure_code is not None:
        return QueryDecisionFailure(failure_code=failure_code)
    if result is None:
        return QueryDecisionFailure(failure_code="delivery-missing")
    return result


def prepare_ctx_run_query_decision(
    *,
    task: str,
    language: str,
    session_id: str,
    workspace: Path,
    journal_path: Path,
    benefit_audit_path: Path,
) -> QueryDecisionResult:
    """Compatibility wrapper over the host-neutral production query factory."""

    host_invocation_digest = _digest(
        {
            "schema": "ctx-run-query-invocation-v1",
            "host": "ctx-run",
            "session_digest": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
            "workspace_digest": hashlib.sha256(
                os.path.abspath(os.fspath(workspace)).encode("utf-8")
            ).hexdigest(),
            "slot": "initial-query-v1",
        }
    )
    return _prepare_query_decision(
        host=QueryHostDescriptor.ctx_run(),
        task=task,
        language=language,
        session_id=session_id,
        workspace=workspace,
        journal_path=journal_path,
        benefit_audit_path=benefit_audit_path,
        host_invocation_digest=host_invocation_digest,
    )


__all__ = [
    "CTX_RUN_QUERY_ENGINE_VERSION",
    "CTX_RUN_QUERY_PLANNER_VERSION",
    "CtxRunQueryDecision",
    "QueryDecisionFailure",
    "prepare_query_delivery",
    "prepare_ctx_run_query_decision",
]
