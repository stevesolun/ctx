"""Deterministic query-only capability selection for the unified CTX engine.

The planner owns bounded cross-type selection policy, but no catalog, filesystem,
host, or persistence behavior.  A caller injects one candidate source and may
persist :meth:`CapabilityPlan.to_mapping` as the approved decision surrogate.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ctx.engine.capability_schema import (
    CAPABILITY_KINDS,
    MAX_CANONICAL_TOKEN_CHARS,
    MAX_MATCHING_SIGNALS,
    MAX_REASON_CODES,
    MAX_SELECTED_CAPABILITIES,
    PRESENTED_ACTIONABILITY_STATES,
    SHA256_HEX_CHARS,
)
from ctx.engine.replay import PlanningContext, StructuredSurrogate

if TYPE_CHECKING:
    from ctx.engine.state import EngineState


MAX_CANDIDATES = 512
MAX_SIGNALS = 32
MAX_LANGUAGES = 10
MAX_CONTEXT_CAPABILITY_IDS = 100

ACTIONABILITY_STATES = frozenset({"load", "install", "manual", "unavailable", "blocked", "none"})
ACTIONABLE_STATES = PRESENTED_ACTIONABILITY_STATES
PLAN_STATUSES = frozenset({"ready", "abstained", "degraded"})
ABSTENTION_CODES = frozenset({"no-signals", "below-threshold", "no-relevant-capability"})
DEGRADATION_CODES = frozenset({"catalog-unavailable", "planner-failed"})

_SAFE_TOKEN_RE = re.compile(rf"\A[a-z0-9][a-z0-9._:@-]{{0,{MAX_CANONICAL_TOKEN_CHARS - 1}}}\Z")
_CAPABILITY_NAME_RE = re.compile(rf"\A[a-z0-9][a-z0-9._@-]{{0,{MAX_CANONICAL_TOKEN_CHARS - 1}}}\Z")
_SHA256_RE = re.compile(rf"\A[0-9a-f]{{{SHA256_HEX_CHARS}}}\Z")


class PlannerValidationError(ValueError):
    """Planner input or an injected source result violates the closed contract."""


class CandidateSourceUnavailable(RuntimeError):
    """The injected catalog snapshot cannot currently serve candidates."""


class CandidateAuthorityUnavailable(RuntimeError):
    """One candidate has malformed effect authority; peer rows remain usable."""


def _safe_token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_TOKEN_RE.fullmatch(value) is None:
        raise PlannerValidationError(f"{field_name} must be a canonical safe token")
    return value


def _capability_name(value: object) -> str:
    if not isinstance(value, str) or _CAPABILITY_NAME_RE.fullmatch(value) is None:
        raise PlannerValidationError("name must be a canonical capability name")
    return value


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PlannerValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _bounded_integer(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PlannerValidationError(
            f"{field_name} must be an integer from {minimum} through {maximum}"
        )
    return value


def _canonical_tokens(
    value: object,
    field_name: str,
    *,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise PlannerValidationError(f"{field_name} must be an immutable tuple")
    if len(value) > maximum:
        raise PlannerValidationError(f"{field_name} exceeds its bounded item limit")
    tokens = tuple(_safe_token(item, f"{field_name} item") for item in value)
    if len(set(tokens)) != len(tokens):
        raise PlannerValidationError(f"{field_name} contains duplicate values")
    if tokens != tuple(sorted(tokens)):
        raise PlannerValidationError(f"{field_name} must use canonical sorted order")
    return tokens


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkObservation:
    """Privacy-safe current-work signals consumed by candidate retrieval."""

    signals: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    baseline_capability_ids: tuple[str, ...] = ()
    active_capability_ids: tuple[str, ...] = ()
    rejected_capability_ids: tuple[str, ...] = ()
    requested_limit: int = MAX_SELECTED_CAPABILITIES

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "signals",
            _canonical_tokens(self.signals, "signals", maximum=MAX_SIGNALS),
        )
        object.__setattr__(
            self,
            "languages",
            _canonical_tokens(self.languages, "languages", maximum=MAX_LANGUAGES),
        )
        for field_name, maximum in (
            ("baseline_capability_ids", MAX_CONTEXT_CAPABILITY_IDS),
            ("active_capability_ids", MAX_SELECTED_CAPABILITIES),
            ("rejected_capability_ids", MAX_CONTEXT_CAPABILITY_IDS),
        ):
            object.__setattr__(
                self,
                field_name,
                _canonical_tokens(
                    getattr(self, field_name),
                    field_name,
                    maximum=maximum,
                ),
            )
        object.__setattr__(
            self,
            "requested_limit",
            _bounded_integer(
                self.requested_limit,
                "requested_limit",
                minimum=0,
                maximum=MAX_SELECTED_CAPABILITIES,
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityCandidate:
    """One typed, scored candidate returned by the injected catalog source."""

    capability_id: str
    kind: str
    name: str
    source_digest: str
    normalized_score_ppm: int
    matching_signals: tuple[str, ...] = ()
    reason_codes: tuple[str, ...]
    actionability: str
    install_descriptor_digest: str | None = None
    install_plan_digest: str | None = None
    equivalence_key: str | None = None

    def __post_init__(self) -> None:
        capability_id = _safe_token(self.capability_id, "capability_id")
        if self.kind not in CAPABILITY_KINDS:
            raise PlannerValidationError("kind is not a recommendable capability kind")
        name = _capability_name(self.name)
        if capability_id != f"{self.kind}:{name}":
            raise PlannerValidationError("capability_id must be the canonical kind:name identity")
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self,
            "source_digest",
            _digest(self.source_digest, "source_digest"),
        )
        object.__setattr__(
            self,
            "normalized_score_ppm",
            _bounded_integer(
                self.normalized_score_ppm,
                "normalized_score_ppm",
                minimum=0,
                maximum=1_000_000,
            ),
        )
        object.__setattr__(
            self,
            "matching_signals",
            _canonical_tokens(
                self.matching_signals,
                "matching_signals",
                maximum=MAX_MATCHING_SIGNALS,
            ),
        )
        reason_codes = _canonical_tokens(
            self.reason_codes,
            "reason_codes",
            maximum=MAX_REASON_CODES,
        )
        if not reason_codes:
            raise PlannerValidationError("reason_codes must explain the candidate")
        object.__setattr__(self, "reason_codes", reason_codes)
        if self.actionability not in ACTIONABILITY_STATES:
            raise PlannerValidationError("actionability is not a declared state")
        if self.actionability == "install":
            object.__setattr__(
                self,
                "install_descriptor_digest",
                _digest(self.install_descriptor_digest, "install_descriptor_digest"),
            )
            object.__setattr__(
                self,
                "install_plan_digest",
                _digest(self.install_plan_digest, "install_plan_digest"),
            )
        elif self.install_descriptor_digest is not None or self.install_plan_digest is not None:
            raise PlannerValidationError(
                "install descriptor and plan digests are allowed only for install candidates"
            )
        if self.equivalence_key is not None:
            object.__setattr__(
                self,
                "equivalence_key",
                _safe_token(self.equivalence_key, "equivalence_key"),
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilitySelection:
    """Approved query-only projection of one selected candidate."""

    capability_id: str
    kind: str
    name: str
    source_digest: str
    normalized_score_ppm: int
    matching_signals: tuple[str, ...]
    reason_codes: tuple[str, ...]
    actionability: str
    install_descriptor_digest: str | None = None
    install_plan_digest: str | None = None

    def __post_init__(self) -> None:
        candidate = CapabilityCandidate(
            capability_id=self.capability_id,
            kind=self.kind,
            name=self.name,
            source_digest=self.source_digest,
            normalized_score_ppm=self.normalized_score_ppm,
            matching_signals=self.matching_signals,
            reason_codes=self.reason_codes,
            actionability=self.actionability,
            install_descriptor_digest=self.install_descriptor_digest,
            install_plan_digest=self.install_plan_digest,
        )
        for field_name in (
            "capability_id",
            "kind",
            "name",
            "source_digest",
            "normalized_score_ppm",
            "matching_signals",
            "reason_codes",
            "actionability",
            "install_descriptor_digest",
            "install_plan_digest",
        ):
            object.__setattr__(self, field_name, getattr(candidate, field_name))

    @classmethod
    def from_candidate(cls, candidate: CapabilityCandidate) -> CapabilitySelection:
        if not isinstance(candidate, CapabilityCandidate):
            raise TypeError("candidate must be a CapabilityCandidate")
        return cls(
            capability_id=candidate.capability_id,
            kind=candidate.kind,
            name=candidate.name,
            source_digest=candidate.source_digest,
            normalized_score_ppm=candidate.normalized_score_ppm,
            matching_signals=candidate.matching_signals,
            reason_codes=candidate.reason_codes,
            actionability=candidate.actionability,
            install_descriptor_digest=candidate.install_descriptor_digest,
            install_plan_digest=candidate.install_plan_digest,
        )

    def to_mapping(self, *, schema_version: int = 1) -> dict[str, Any]:
        mapping: dict[str, Any] = {
            "actionability": self.actionability,
            "capability_id": self.capability_id,
            "catalog_entry_digest": self.source_digest,
            "kind": self.kind,
            "matching_signals": list(self.matching_signals),
            "name": self.name,
            "normalized_score_ppm": self.normalized_score_ppm,
            "reason_codes": list(self.reason_codes),
        }
        if schema_version == 1:
            if self.actionability == "install":
                raise PlannerValidationError(
                    "capability-plan schema v1 cannot encode install plan identity"
                )
            return mapping
        if schema_version == 2:
            mapping["install_descriptor_digest"] = self.install_descriptor_digest
            mapping["install_plan_digest"] = self.install_plan_digest
            return mapping
        raise PlannerValidationError("unsupported capability-plan schema version")


def _selection_order_key(selection: CapabilitySelection) -> tuple[int, str]:
    return (-selection.normalized_score_ppm, selection.capability_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityPlan:
    """Exact ready, abstained, or degraded output for authoritative replay."""

    status: str
    abstention_code: str | None
    selections: tuple[CapabilitySelection, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in PLAN_STATUSES:
            raise PlannerValidationError("status is not a declared capability-plan state")
        if not isinstance(self.selections, tuple) or not all(
            isinstance(item, CapabilitySelection) for item in self.selections
        ):
            raise PlannerValidationError("selections must be a tuple of CapabilitySelection values")
        if len(self.selections) > MAX_SELECTED_CAPABILITIES:
            raise PlannerValidationError("a capability plan cannot contain more than five items")
        identities = tuple(item.capability_id for item in self.selections)
        if len(set(identities)) != len(identities):
            raise PlannerValidationError("a capability plan cannot contain duplicate identities")
        if tuple(sorted(self.selections, key=_selection_order_key)) != self.selections:
            raise PlannerValidationError(
                "capability selections must use deterministic ranked order"
            )

        if self.status == "ready":
            if not self.selections or self.abstention_code is not None:
                raise PlannerValidationError(
                    "ready plans require one to five selections and no abstention code"
                )
            return
        if self.selections:
            raise PlannerValidationError("non-ready plans cannot contain selections")
        allowed_codes = ABSTENTION_CODES if self.status == "abstained" else DEGRADATION_CODES
        if self.abstention_code not in allowed_codes:
            raise PlannerValidationError("plan status and abstention_code are inconsistent")

    def to_mapping(self, *, schema_version: int = 1) -> dict[str, Any]:
        """Return the exact free-form-text-free decision-surrogate value."""

        if schema_version not in {1, 2}:
            raise PlannerValidationError("unsupported capability-plan schema version")

        return {
            "status": self.status,
            "abstention_code": self.abstention_code,
            "capabilities": [
                selection.to_mapping(schema_version=schema_version) for selection in self.selections
            ],
        }


class CandidateSource(Protocol):
    """Single injected dependency for all-type candidate retrieval."""

    @property
    def catalog_snapshot_digest(self) -> str: ...

    def retrieve(self, observation: WorkObservation) -> Sequence[CapabilityCandidate]: ...


def _candidate_order_key(candidate: CapabilityCandidate) -> tuple[Any, ...]:
    return (
        -candidate.normalized_score_ppm,
        candidate.capability_id,
        candidate.source_digest,
        candidate.actionability,
        candidate.equivalence_key or "",
        candidate.matching_signals,
        candidate.reason_codes,
    )


@dataclass(frozen=True, slots=True)
class BoundedCapabilityPlanner:
    """Select one deterministic, globally bounded, cross-type capability set."""

    source: CandidateSource
    minimum_normalized_score_ppm: int = 300_000
    minimum_matching_signals: int = 0
    minimum_non_language_matching_signals: int = 0
    allowed_actionability_states: frozenset[str] = ACTIONABLE_STATES

    def __post_init__(self) -> None:
        if not callable(getattr(self.source, "retrieve", None)):
            raise PlannerValidationError("source must implement candidate retrieval")
        object.__setattr__(
            self,
            "minimum_normalized_score_ppm",
            _bounded_integer(
                self.minimum_normalized_score_ppm,
                "minimum_normalized_score_ppm",
                minimum=0,
                maximum=1_000_000,
            ),
        )
        object.__setattr__(
            self,
            "minimum_matching_signals",
            _bounded_integer(
                self.minimum_matching_signals,
                "minimum_matching_signals",
                minimum=0,
                maximum=MAX_MATCHING_SIGNALS,
            ),
        )
        object.__setattr__(
            self,
            "minimum_non_language_matching_signals",
            _bounded_integer(
                self.minimum_non_language_matching_signals,
                "minimum_non_language_matching_signals",
                minimum=0,
                maximum=MAX_MATCHING_SIGNALS,
            ),
        )
        if (
            type(self.allowed_actionability_states) is not frozenset
            or not self.allowed_actionability_states
            or not self.allowed_actionability_states <= ACTIONABLE_STATES
        ):
            raise PlannerValidationError(
                "allowed_actionability_states must be a non-empty frozen subset of "
                "actionable states"
            )

    def plan(
        self,
        observation: WorkObservation,
        *,
        retain_relevant_active: bool = False,
    ) -> CapabilityPlan:
        if not isinstance(observation, WorkObservation):
            raise TypeError("observation must be a WorkObservation")
        if type(retain_relevant_active) is not bool:
            raise TypeError("retain_relevant_active must be a boolean")
        if not observation.signals and not observation.languages:
            return CapabilityPlan(status="abstained", abstention_code="no-signals")
        if observation.requested_limit == 0:
            return CapabilityPlan(
                status="abstained",
                abstention_code="no-relevant-capability",
            )

        try:
            retrieved = self.source.retrieve(observation)
        except CandidateSourceUnavailable:
            return CapabilityPlan(
                status="degraded",
                abstention_code="catalog-unavailable",
            )
        except Exception:
            return CapabilityPlan(status="degraded", abstention_code="planner-failed")

        candidates = self._validated_candidates(retrieved)
        if not candidates:
            return CapabilityPlan(
                status="abstained",
                abstention_code="no-relevant-capability",
            )

        eligible = self._eligible_unique_candidates(
            candidates,
            observation,
            retain_relevant_active=retain_relevant_active,
        )
        if not eligible:
            return CapabilityPlan(
                status="abstained",
                abstention_code="no-relevant-capability",
            )
        sufficiently_matched = tuple(
            candidate
            for candidate in eligible
            if len(candidate.matching_signals) >= self.minimum_matching_signals
            and len(set(candidate.matching_signals) - set(observation.languages))
            >= self.minimum_non_language_matching_signals
        )
        if not sufficiently_matched:
            return CapabilityPlan(
                status="abstained",
                abstention_code="no-relevant-capability",
            )
        above_threshold = tuple(
            candidate
            for candidate in sufficiently_matched
            if candidate.normalized_score_ppm >= self.minimum_normalized_score_ppm
        )
        if not above_threshold:
            return CapabilityPlan(status="abstained", abstention_code="below-threshold")

        ranked = sorted(above_threshold, key=_candidate_order_key)
        representatives: list[CapabilityCandidate] = []
        seen_equivalence: set[tuple[str, ...]] = set()
        for candidate in ranked:
            equivalence: tuple[str, ...]
            if candidate.equivalence_key is not None:
                equivalence = ("explicit", candidate.equivalence_key)
            elif self.minimum_matching_signals > 0 and candidate.matching_signals:
                equivalence = ("coverage", candidate.kind, *candidate.matching_signals)
            else:
                equivalence = ("identity", candidate.capability_id)
            if equivalence in seen_equivalence:
                continue
            seen_equivalence.add(equivalence)
            representatives.append(candidate)
            if len(representatives) >= observation.requested_limit:
                break

        return CapabilityPlan(
            status="ready",
            abstention_code=None,
            selections=tuple(
                CapabilitySelection.from_candidate(candidate) for candidate in representatives
            ),
        )

    @staticmethod
    def _validated_candidates(value: object) -> tuple[CapabilityCandidate, ...]:
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            raise PlannerValidationError("candidate source must return a bounded sequence")
        if len(value) > MAX_CANDIDATES:
            raise PlannerValidationError("candidate source exceeded its bounded pool limit")
        candidates = tuple(value)
        if not all(isinstance(item, CapabilityCandidate) for item in candidates):
            raise PlannerValidationError("candidate source returned an invalid value")
        return candidates

    def _eligible_unique_candidates(
        self,
        candidates: tuple[CapabilityCandidate, ...],
        observation: WorkObservation,
        *,
        retain_relevant_active: bool,
    ) -> tuple[CapabilityCandidate, ...]:
        by_identity: dict[str, list[CapabilityCandidate]] = {}
        for candidate in candidates:
            by_identity.setdefault(candidate.capability_id, []).append(candidate)

        excluded_context = {
            *observation.baseline_capability_ids,
            *observation.rejected_capability_ids,
        }
        if not retain_relevant_active:
            excluded_context.update(observation.active_capability_ids)
        active_ids = set(observation.active_capability_ids)
        eligible: list[CapabilityCandidate] = []
        for capability_id in sorted(by_identity):
            identity_candidates = by_identity[capability_id]
            if len({item.source_digest for item in identity_candidates}) != 1:
                continue
            if capability_id in excluded_context:
                continue
            actionable = [
                candidate
                for candidate in identity_candidates
                if candidate.actionability in self.allowed_actionability_states
                and not (
                    retain_relevant_active
                    and capability_id in active_ids
                    and candidate.actionability == "manual"
                )
            ]
            if not actionable:
                continue
            eligible.append(min(actionable, key=_candidate_order_key))
        return tuple(eligible)


def _surrogate_tokens(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise PlannerValidationError(f"current-work {field_name} must be a token array")
    return value


@dataclass(frozen=True, slots=True)
class ReplayDecisionPlanner:
    """Adapt the pure bounded planner to the replay factory's decision hook."""

    planner: BoundedCapabilityPlanner
    planner_version: str
    decision_schema_version: int = 1
    _catalog_snapshot_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.planner, BoundedCapabilityPlanner):
            raise PlannerValidationError("planner must be a BoundedCapabilityPlanner")
        object.__setattr__(
            self,
            "planner_version",
            _safe_token(self.planner_version, "planner_version"),
        )
        if type(self.decision_schema_version) is not int or self.decision_schema_version not in {
            1,
            2,
        }:
            raise PlannerValidationError("decision_schema_version must be 1 or 2")
        object.__setattr__(
            self,
            "_catalog_snapshot_digest",
            _digest(
                getattr(self.planner.source, "catalog_snapshot_digest", None),
                "candidate source catalog_snapshot_digest",
            ),
        )

    @staticmethod
    def _drop_unverified_active_retention(
        plan: CapabilityPlan,
        state: EngineState | None,
        active_capability_ids: tuple[str, ...],
    ) -> CapabilityPlan:
        from ctx.engine.state import CapabilityState

        if plan.status != "ready" or not active_capability_ids:
            return plan
        active_ids = set(active_capability_ids)
        retained: list[CapabilitySelection] = []
        for selection in plan.selections:
            if selection.capability_id not in active_ids:
                retained.append(selection)
                continue
            capability = None if state is None else state.capability(selection.capability_id)
            if not isinstance(capability, CapabilityState) or capability.activation != "active":
                continue
            if (
                selection.source_digest,
                selection.kind,
                selection.actionability,
                selection.install_descriptor_digest,
                selection.install_plan_digest,
            ) != (
                capability.source_digest,
                capability.kind,
                capability.actionability,
                capability.install_descriptor_digest,
                capability.install_plan_digest,
            ):
                continue
            retained.append(selection)
        if retained:
            return CapabilityPlan(
                status="ready",
                abstention_code=None,
                selections=tuple(retained),
            )
        return CapabilityPlan(
            status="abstained",
            abstention_code="no-relevant-capability",
        )

    def __call__(
        self,
        observation: StructuredSurrogate,
        state: EngineState | None,
        context: PlanningContext,
    ) -> StructuredSurrogate:
        if (
            not isinstance(observation, StructuredSurrogate)
            or observation.schema_id != "ctx.observation.current-work"
            or observation.schema_version != 1
        ):
            raise PlannerValidationError("planner requires a current-work observation")
        if not isinstance(context, PlanningContext):
            raise PlannerValidationError("planner requires a frozen planning context")
        if context.planner_version != self.planner_version:
            raise PlannerValidationError("planner version mismatch")
        if context.catalog_snapshot_digest != self._catalog_snapshot_digest:
            raise PlannerValidationError("catalog snapshot mismatch")
        value = observation.value
        requested_limit = value.get("requested_limit")
        if type(requested_limit) is not int:
            raise PlannerValidationError("current-work requested_limit must be an integer")
        observed_active_ids = _surrogate_tokens(
            value.get("active_capability_ids"),
            "active_capability_ids",
        )
        active_capability_ids = (
            observed_active_ids if state is None else tuple(sorted(state.active_capability_ids))
        )
        work = WorkObservation(
            signals=_surrogate_tokens(value.get("signals"), "signals"),
            languages=_surrogate_tokens(value.get("languages"), "languages"),
            baseline_capability_ids=_surrogate_tokens(
                value.get("baseline_capability_ids"),
                "baseline_capability_ids",
            ),
            active_capability_ids=active_capability_ids,
            rejected_capability_ids=_surrogate_tokens(
                value.get("rejected_capability_ids"),
                "rejected_capability_ids",
            ),
            requested_limit=requested_limit,
        )
        retain_relevant_active = self.decision_schema_version == 2
        plan = self.planner.plan(
            work,
            retain_relevant_active=retain_relevant_active,
        )
        if retain_relevant_active:
            plan = self._drop_unverified_active_retention(
                plan,
                state,
                active_capability_ids,
            )
        return StructuredSurrogate.create(
            schema_id="ctx.decision.capability-plan",
            schema_version=self.decision_schema_version,
            value=plan.to_mapping(schema_version=self.decision_schema_version),
        )


__all__ = [
    "ABSTENTION_CODES",
    "ACTIONABILITY_STATES",
    "CAPABILITY_KINDS",
    "DEGRADATION_CODES",
    "MAX_SELECTED_CAPABILITIES",
    "PLAN_STATUSES",
    "BoundedCapabilityPlanner",
    "CandidateAuthorityUnavailable",
    "CandidateSource",
    "CandidateSourceUnavailable",
    "CapabilityCandidate",
    "CapabilityPlan",
    "CapabilitySelection",
    "PlannerValidationError",
    "ReplayDecisionPlanner",
    "WorkObservation",
]
