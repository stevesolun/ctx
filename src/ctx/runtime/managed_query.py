"""Safe projection of one committed cross-type managed capability plan.

The module does not rank, install, activate, render, or expose planning
authority.  It drives one already-composed :class:`CtxEngine` through its
initial planning boundary and projects the exact committed schema-v3 plan into
an immutable, non-serializable result suitable for a later management layer.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NoReturn, SupportsIndex

from ctx.engine.capability_schema import MAX_SELECTED_CAPABILITIES, validate_capability_identity
from ctx.engine.engine import EngineSnapshot
from ctx.engine.planning_v3 import (
    InstallPlanningAuthority,
    LoadPlanningAuthority,
    ManualPlanningAuthority,
)
from ctx.engine.protocol import EngineEvent, ScopeRef, Transition
from ctx.engine.state import CommittedPlanV3, EngineState, PlanCapabilityV3
from ctx.engine.store import StreamId

if TYPE_CHECKING:
    from ctx.runtime.composition import EngineComposition


_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_PLAN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_PREPARED_FACTORY_TOKEN = object()
_SELECTION_FACTORY_TOKEN = object()
_ADVANCE_FACTORY_TOKEN = object()


class ManagedQueryError(RuntimeError):
    """One exact managed planning transaction could not be projected safely."""


def _require_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ManagedQueryError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


class ManagedCapabilitySelection:
    """Prose-free classification of one exact row from the committed plan."""

    __slots__ = (
        "actionability",
        "benefit_tier",
        "capability_id",
        "catalog_identity_digest",
        "individual_net_benefit_u",
        "install_descriptor_digest",
        "install_plan_digest",
        "kind",
        "marginal_net_benefit_u",
        "matching_signals",
        "name",
        "reason_codes",
        "source_digest",
    )

    capability_id: str
    kind: str
    name: str
    actionability: str
    matching_signals: tuple[str, ...]
    reason_codes: tuple[str, ...]
    benefit_tier: str
    individual_net_benefit_u: int
    marginal_net_benefit_u: int
    source_digest: str
    catalog_identity_digest: str
    install_descriptor_digest: str | None
    install_plan_digest: str | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed capability selections are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        capability: PlanCapabilityV3,
    ) -> ManagedCapabilitySelection:
        if factory_token is not _SELECTION_FACTORY_TOKEN:
            raise TypeError("managed capability selections are factory-issued only")
        if not isinstance(capability, PlanCapabilityV3):
            raise TypeError("managed capability selection requires a committed schema-v3 row")
        authority = capability.authority
        if isinstance(authority, LoadPlanningAuthority):
            actionability = "load"
        elif isinstance(authority, InstallPlanningAuthority):
            actionability = "install"
        elif isinstance(authority, ManualPlanningAuthority):
            actionability = "manual"
        else:  # CommittedPlanV3 validation should make this unreachable.
            raise ManagedQueryError("committed capability has unsupported planning authority")
        if capability.actionability != actionability:
            raise ManagedQueryError(
                "committed capability actionability does not match its typed authority"
            )
        try:
            capability_id, kind = validate_capability_identity(
                capability.capability_id,
                capability.kind,
            )
        except ValueError as exc:
            raise ManagedQueryError("committed capability identity is invalid") from exc
        instance = object.__new__(cls)
        object.__setattr__(instance, "capability_id", capability_id)
        object.__setattr__(instance, "kind", kind)
        object.__setattr__(instance, "name", capability.name)
        object.__setattr__(instance, "actionability", actionability)
        object.__setattr__(
            instance,
            "matching_signals",
            tuple(capability.selection.presentation.matching_signals),
        )
        object.__setattr__(
            instance,
            "reason_codes",
            tuple(capability.selection.presentation.reason_codes),
        )
        object.__setattr__(instance, "benefit_tier", capability.benefit.tier)
        object.__setattr__(
            instance,
            "individual_net_benefit_u",
            capability.benefit.individual_net_benefit_u,
        )
        object.__setattr__(
            instance,
            "marginal_net_benefit_u",
            capability.benefit.marginal_net_benefit_u,
        )
        object.__setattr__(
            instance,
            "source_digest",
            _require_digest(capability.source_digest, "source_digest"),
        )
        object.__setattr__(
            instance,
            "catalog_identity_digest",
            _require_digest(
                capability.catalog_identity.identity_digest,
                "catalog_identity_digest",
            ),
        )
        object.__setattr__(
            instance,
            "install_descriptor_digest",
            (
                None
                if capability.install_descriptor_digest is None
                else _require_digest(
                    capability.install_descriptor_digest,
                    "install_descriptor_digest",
                )
            ),
        )
        object.__setattr__(
            instance,
            "install_plan_digest",
            (
                None
                if capability.install_plan_digest is None
                else _require_digest(capability.install_plan_digest, "install_plan_digest")
            ),
        )
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed capability selection is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed capability selection is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed capability selection cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed capability selection cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed capability selection cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed capability selection cannot be serialized")

    def __repr__(self) -> str:
        return (
            "ManagedCapabilitySelection("
            f"capability_id={self.capability_id!r}, kind={self.kind!r}, "
            f"actionability={self.actionability!r})"
        )


class PreparedManagedQuery:
    """Immutable, authority-free projection of one committed schema-v3 plan."""

    __slots__ = (
        "abstention_code",
        "benefit_result_digest",
        "candidate_pool_count",
        "decision_digest",
        "journal_record_digest",
        "journal_revision",
        "plan_id",
        "planning_environment_digest",
        "requested_limit",
        "search_evaluation_count",
        "selections",
        "status",
    )

    status: str
    abstention_code: str | None
    plan_id: str
    planning_environment_digest: str
    decision_digest: str
    journal_revision: int
    journal_record_digest: str
    benefit_result_digest: str | None
    requested_limit: int | None
    candidate_pool_count: int | None
    search_evaluation_count: int | None
    selections: tuple[ManagedCapabilitySelection, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("prepared managed queries are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        committed: CommittedPlanV3,
        journal_revision: int,
        journal_record_digest: str,
    ) -> PreparedManagedQuery:
        if factory_token is not _PREPARED_FACTORY_TOKEN:
            raise TypeError("prepared managed queries are factory-issued only")
        if not isinstance(committed, CommittedPlanV3):
            raise TypeError("prepared managed query requires one committed schema-v3 plan")
        selections = tuple(
            ManagedCapabilitySelection._create(
                factory_token=_SELECTION_FACTORY_TOKEN,
                capability=capability,
            )
            for capability in committed.capabilities
        )
        if len(selections) > MAX_SELECTED_CAPABILITIES:
            raise ManagedQueryError("committed plan exceeds the global capability bound")
        audit = committed.benefit_audit
        instance = object.__new__(cls)
        object.__setattr__(instance, "status", committed.status)
        object.__setattr__(instance, "abstention_code", committed.abstention_code)
        object.__setattr__(instance, "plan_id", committed.plan_id)
        object.__setattr__(
            instance,
            "planning_environment_digest",
            _require_digest(
                committed.catalog_snapshot_id,
                "planning_environment_digest",
            ),
        )
        object.__setattr__(
            instance,
            "decision_digest",
            _require_digest(committed.decision_digest, "decision_digest"),
        )
        if type(journal_revision) is not int or journal_revision < 2:
            raise ManagedQueryError("managed query journal revision must be at least two")
        object.__setattr__(instance, "journal_revision", journal_revision)
        object.__setattr__(
            instance,
            "journal_record_digest",
            _require_digest(journal_record_digest, "journal_record_digest"),
        )
        object.__setattr__(
            instance,
            "benefit_result_digest",
            None
            if audit is None
            else _require_digest(audit.result_digest, "benefit_result_digest"),
        )
        object.__setattr__(
            instance, "requested_limit", None if audit is None else audit.requested_limit
        )
        object.__setattr__(
            instance,
            "candidate_pool_count",
            None if audit is None else audit.candidate_pool_count,
        )
        object.__setattr__(
            instance,
            "search_evaluation_count",
            None if audit is None else audit.search_evaluation_count,
        )
        object.__setattr__(instance, "selections", selections)
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("prepared managed query is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("prepared managed query is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("prepared managed query cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("prepared managed query cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("prepared managed query cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("prepared managed query cannot be serialized")

    def __repr__(self) -> str:
        return f"PreparedManagedQuery(decision_digest={self.decision_digest!r})"


class ManagedAdvanceResult:
    """Sealed managed projection plus the exact committed host-action transition."""

    __slots__ = ("prepared", "transition")

    prepared: PreparedManagedQuery
    transition: Transition

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed advance results are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        prepared: PreparedManagedQuery,
        transition: Transition,
    ) -> ManagedAdvanceResult:
        if factory_token is not _ADVANCE_FACTORY_TOKEN:
            raise TypeError("managed advance results are factory-issued only")
        if type(prepared) is not PreparedManagedQuery:
            raise TypeError("managed advance result requires an exact prepared query")
        if type(transition) is not Transition:
            raise TypeError("managed advance result requires an exact transition")
        instance = object.__new__(cls)
        object.__setattr__(instance, "prepared", prepared)
        object.__setattr__(instance, "transition", transition)
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed advance result is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed advance result is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed advance result cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed advance result cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed advance result cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed advance result cannot be serialized")

    def __repr__(self) -> str:
        return (
            "ManagedAdvanceResult("
            f"plan_id={self.prepared.plan_id!r}, "
            f"event_id={self.transition.event_id!r})"
        )


def _create_managed_advance_result(
    *,
    prepared: PreparedManagedQuery,
    transition: Transition,
) -> ManagedAdvanceResult:
    return ManagedAdvanceResult._create(
        factory_token=_ADVANCE_FACTORY_TOKEN,
        prepared=prepared,
        transition=transition,
    )


def _project_prepared_managed_query(
    *,
    committed: CommittedPlanV3,
    journal_revision: int,
    journal_record_digest: str,
) -> PreparedManagedQuery:
    """Project a plan already authenticated by the owned composition boundary."""

    return PreparedManagedQuery._create(
        factory_token=_PREPARED_FACTORY_TOKEN,
        committed=committed,
        journal_revision=journal_revision,
        journal_record_digest=journal_record_digest,
    )


def _project_prepared_managed_query_snapshot(
    *,
    snapshot: EngineSnapshot,
    expected_plan_id: str | None = None,
) -> PreparedManagedQuery:
    """Project the committed plan at one exact authoritative snapshot head.

    ``journal_revision`` identifies the current authenticated stream head.  It
    may therefore be later than the planning event that originally committed
    the plan.  This projection consumes only durable state and never invokes a
    planner or returns the committed plan's live authority.
    """

    if type(snapshot) is not EngineSnapshot:
        raise TypeError("snapshot must be an exact EngineSnapshot")
    if expected_plan_id is not None:
        if not isinstance(expected_plan_id, str):
            raise TypeError("expected_plan_id must be a canonical token or None")
        if _PLAN_ID_RE.fullmatch(expected_plan_id) is None:
            raise ManagedQueryError("expected_plan_id must be a canonical token")
    if snapshot.revision < 2:
        raise ManagedQueryError("managed query snapshot revision must be at least two")
    state = snapshot.state
    if type(state) is not EngineState or state.revision != snapshot.revision:
        raise ManagedQueryError("managed query snapshot must contain its exact state head")
    if snapshot.stream_id != StreamId.from_scope(state.scope):
        raise ManagedQueryError("managed query snapshot stream does not match its state")
    committed = state.committed_plan
    if type(committed) is not CommittedPlanV3:
        raise ManagedQueryError("managed query snapshot has no exact committed schema-v3 plan")
    if _PLAN_ID_RE.fullmatch(committed.plan_id) is None:
        raise ManagedQueryError("committed managed plan identity is not canonical")
    if expected_plan_id is not None and committed.plan_id != expected_plan_id:
        raise ManagedQueryError("managed query snapshot does not match the expected plan identity")
    record_digest = _require_digest(snapshot.record_digest, "journal_record_digest")
    return _project_prepared_managed_query(
        committed=committed,
        journal_revision=snapshot.revision,
        journal_record_digest=record_digest,
    )


def prepare_managed_query(
    *,
    composition: EngineComposition,
    session_started: EngineEvent,
    intent_observed: EngineEvent,
) -> PreparedManagedQuery:
    """Commit one plan through an exact factory-issued production composition."""

    # Local import avoids a module cycle: composition owns the engine and uses
    # this module only for its authority-free result projection.
    from ctx.runtime.composition import EngineComposition

    if type(composition) is not EngineComposition:
        raise TypeError("composition must be an exact factory-issued EngineComposition")
    return composition.prepare_managed_query(
        session_started=session_started,
        intent_observed=intent_observed,
    )


def advance_managed_query(
    *,
    composition: EngineComposition,
    session_started: EngineEvent,
    planning_observed: EngineEvent,
) -> ManagedAdvanceResult:
    """Commit or exactly replay one plan through a trusted composition."""

    from ctx.runtime.composition import EngineComposition

    if type(composition) is not EngineComposition:
        raise TypeError("composition must be an exact factory-issued EngineComposition")
    return composition.advance_managed_query(
        session_started=session_started,
        planning_observed=planning_observed,
    )


def reopen_managed_query(
    *,
    composition: EngineComposition,
    scope: ScopeRef,
    expected_plan_id: str | None = None,
) -> PreparedManagedQuery:
    """Project the latest plan through a trusted composition without planning."""

    from ctx.runtime.composition import EngineComposition

    if type(composition) is not EngineComposition:
        raise TypeError("composition must be an exact factory-issued EngineComposition")
    if type(scope) is not ScopeRef:
        raise TypeError("scope must be an exact ScopeRef")
    return composition.reopen_managed_query(
        scope,
        expected_plan_id=expected_plan_id,
    )


__all__ = [
    "ManagedAdvanceResult",
    "ManagedCapabilitySelection",
    "ManagedQueryError",
    "PreparedManagedQuery",
    "advance_managed_query",
    "prepare_managed_query",
    "reopen_managed_query",
]
