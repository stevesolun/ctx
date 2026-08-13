"""Pure, deterministic net-benefit assessment and bounded set selection.

This module is deliberately disconnected from catalog retrieval, replay, host
mutation, and persistence.  It accepts already authenticated, privacy-safe
facts and returns an auditable zero-to-five selection.  Integration layers are
responsible for deriving those facts from pinned descriptors and authoritative
engine evidence; graph prose cannot supply trust or execution authority here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import ClassVar

from ctx.engine.capability_schema import CAPABILITY_KINDS


MAX_CANDIDATES = 512
MAX_BENEFIT_RESULT_JSON_BYTES = 32 * 1024 * 1024
MAX_SELECTED_CAPABILITIES = 5
MAX_PPM = 1_000_000
MAX_QUANTITY = 1_000_000
MAX_RELATION_KEYS = 64
MAX_SEARCH_EVALUATIONS = 1_000_000
MAX_UTILITY_UNITS = 2**63 - 1

AVAILABILITY_STATES = frozenset({"executable", "advisory", "unsupported"})
ASSESSMENT_TIERS = frozenset({"executable", "advisory", "ineligible"})
ABSTENTION_CODES = frozenset({"below-net-benefit", "limit-zero", "no-feasible-capability"})

_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_CAPABILITY_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._@-]{0,127}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_POLICY_SCHEMA_ID = "ctx.net-benefit-policy-v3"
_SELECTION_ALGORITHM_ID = "ctx.greedy-bounded-subset-exchange-v1"
_RESULT_SCHEMA_ID = "ctx.benefit-selection-result-v1"


class BenefitValidationError(ValueError):
    """A net-benefit input violates the closed deterministic contract."""


def _integer(
    value: object,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_QUANTITY,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BenefitValidationError(
            f"{field_name} must be an integer from {minimum} through {maximum}"
        )
    return value


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise BenefitValidationError(f"{field_name} must be a boolean")
    return value


def _token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise BenefitValidationError(f"{field_name} must be a canonical safe token")
    return value


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise BenefitValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _capability_id(value: object, field_name: str) -> str:
    value = _token(value, field_name)
    kind, separator, name = value.partition(":")
    if (
        separator != ":"
        or kind not in CAPABILITY_KINDS
        or _CAPABILITY_NAME_RE.fullmatch(name) is None
    ):
        raise BenefitValidationError(
            f"{field_name} must be a canonical declared-kind capability identity"
        )
    return value


def _canonical_tokens(
    value: object,
    field_name: str,
    *,
    maximum: int = MAX_RELATION_KEYS,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise BenefitValidationError(f"{field_name} must be an immutable tuple")
    if len(value) > maximum:
        raise BenefitValidationError(f"{field_name} exceeds its bounded item limit")
    result = tuple(_token(item, f"{field_name} item") for item in value)
    if result != tuple(sorted(result)):
        raise BenefitValidationError(f"{field_name} must use canonical sorted order")
    if len(set(result)) != len(result):
        raise BenefitValidationError(f"{field_name} contains duplicate values")
    return result


def _canonical_capability_ids(value: object, field_name: str) -> tuple[str, ...]:
    result = _canonical_tokens(value, field_name)
    for item in result:
        _capability_id(item, f"{field_name} item")
    return result


def _checked_utility(value: int, field_name: str) -> int:
    if not -MAX_UTILITY_UNITS <= value <= MAX_UTILITY_UNITS:
        raise BenefitValidationError(f"{field_name} exceeds the signed utility bound")
    return value


def _ppm_multiply(left: int, right: int) -> int:
    """Multiply non-negative ppm values using deterministic half-up rounding."""

    return (left * right + MAX_PPM // 2) // MAX_PPM


def _signed_rounded_divide(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise BenefitValidationError("evidence denominator must be positive")
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceCosts:
    """Bounded physical and attention quantities for one capability.

    These are quantities, not policy valuations.  ``NetBenefitPolicy`` owns the
    conversion into common utility units so a catalog cannot choose its own
    cost weights.
    """

    context_tokens: int = 0
    tool_schema_tokens: int = 0
    runtime_millis: int = 0
    permission_burden_units: int = 0
    credential_burden_units: int = 0
    approval_prompts: int = 0
    process_units: int = 0
    child_agent_units: int = 0

    _FIELDS: ClassVar[tuple[str, ...]] = (
        "context_tokens",
        "tool_schema_tokens",
        "runtime_millis",
        "permission_burden_units",
        "credential_burden_units",
        "approval_prompts",
        "process_units",
        "child_agent_units",
    )

    def __post_init__(self) -> None:
        for field_name in self._FIELDS:
            _integer(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceSummary:
    """Privacy-safe bounded evidence attributed to one exact capability source.

    Exposure is retained for audit but intentionally has zero utility weight.
    Missing invocation contributes negative evidence only when the adapter has
    declared the relevant opportunity observable.
    """

    capability_id: str
    kind: str
    source_digest: str
    evidence_window_digest: str
    opportunity_observable: bool
    opportunities_observed: int = 0
    exposed_count: int = 0
    successful_invocations: int = 0
    failed_invocations: int = 0
    effective_outcomes: int = 0
    validated_outcomes: int = 0
    harmful_outcomes: int = 0

    _COUNT_FIELDS: ClassVar[tuple[str, ...]] = (
        "opportunities_observed",
        "exposed_count",
        "successful_invocations",
        "failed_invocations",
        "effective_outcomes",
        "validated_outcomes",
        "harmful_outcomes",
    )

    def __post_init__(self) -> None:
        capability_id = _capability_id(self.capability_id, "capability_id")
        if capability_id.split(":", 1)[0] != self.kind:
            raise BenefitValidationError("evidence kind does not match capability_id")
        _digest(self.source_digest, "source_digest")
        _digest(self.evidence_window_digest, "evidence_window_digest")
        _boolean(self.opportunity_observable, "opportunity_observable")
        for field_name in self._COUNT_FIELDS:
            _integer(getattr(self, field_name), field_name)
        opportunities = self.opportunities_observed
        attempts = self.successful_invocations + self.failed_invocations
        observed_evidence = (
            self.exposed_count
            + attempts
            + self.effective_outcomes
            + self.validated_outcomes
            + self.harmful_outcomes
        )
        if observed_evidence and opportunities == 0:
            raise BenefitValidationError(
                "observed evidence requires at least one opportunity in its window"
            )
        if self.exposed_count > opportunities:
            raise BenefitValidationError("exposures cannot exceed observed opportunities")
        if attempts > opportunities:
            raise BenefitValidationError("invocation attempts cannot exceed observed opportunities")
        if self.validated_outcomes > self.effective_outcomes:
            raise BenefitValidationError("validated outcomes cannot exceed effective outcomes")
        if self.kind == "skill" and self.effective_outcomes > self.exposed_count:
            raise BenefitValidationError("skill effective outcomes cannot exceed exposures")
        if self.kind != "skill" and self.effective_outcomes > self.successful_invocations:
            raise BenefitValidationError(
                "agent, MCP, and harness effective outcomes cannot exceed successful invocations"
            )
        attributable = self.exposed_count + attempts
        if self.harmful_outcomes > attributable:
            raise BenefitValidationError(
                "harmful outcomes cannot exceed attributable exposure or invocation observations"
            )
        if self.harmful_outcomes > opportunities:
            raise BenefitValidationError("harmful outcomes cannot exceed observed opportunities")
        if self.effective_outcomes + self.harmful_outcomes > opportunities:
            raise BenefitValidationError(
                "effective and harmful outcomes cannot exceed the evidence window"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class BenefitCandidate:
    """One capability's authenticated static and contextual assessment facts."""

    capability_id: str
    source_digest: str
    resource_profile_digest: str
    availability: str
    expected_task_benefit_ppm: int
    relevance_ppm: int
    trust_ppm: int
    costs: ResourceCosts | None
    evidence: EvidenceSummary
    source_trusted: bool
    security_approved: bool
    permissions_allowed: bool
    credentials_available: bool
    coverage_keys: tuple[str, ...] = ()
    equivalence_key: str | None = None
    complements: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        capability_id = _capability_id(self.capability_id, "capability_id")
        _digest(self.source_digest, "source_digest")
        _digest(self.resource_profile_digest, "resource_profile_digest")
        if self.availability not in AVAILABILITY_STATES:
            raise BenefitValidationError("availability is not a declared state")
        for field_name in (
            "expected_task_benefit_ppm",
            "relevance_ppm",
            "trust_ppm",
        ):
            _integer(getattr(self, field_name), field_name, maximum=MAX_PPM)
        if self.costs is not None and not isinstance(self.costs, ResourceCosts):
            raise BenefitValidationError("costs must be ResourceCosts or None")
        if not isinstance(self.evidence, EvidenceSummary):
            raise BenefitValidationError("evidence must be an EvidenceSummary")
        if (
            self.evidence.capability_id != capability_id
            or self.evidence.kind != capability_id.split(":", 1)[0]
            or self.evidence.source_digest != self.source_digest
        ):
            raise BenefitValidationError(
                "evidence must exactly match candidate capability, kind, and source"
            )
        for field_name in (
            "source_trusted",
            "security_approved",
            "permissions_allowed",
            "credentials_available",
        ):
            _boolean(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "coverage_keys",
            _canonical_tokens(self.coverage_keys, "coverage_keys"),
        )
        for field_name in ("complements", "conflicts"):
            object.__setattr__(
                self,
                field_name,
                _canonical_capability_ids(getattr(self, field_name), field_name),
            )
        if self.equivalence_key is not None:
            object.__setattr__(
                self,
                "equivalence_key",
                _token(self.equivalence_key, "equivalence_key"),
            )
        if self.capability_id in self.conflicts:
            raise BenefitValidationError("a capability cannot conflict with itself")
        if self.capability_id in self.complements:
            raise BenefitValidationError("a capability cannot complement itself")
        if set(self.complements) & set(self.conflicts):
            raise BenefitValidationError("a peer cannot be both a complement and a conflict")


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateAssessment:
    """Deterministic individual assessment before set-level marginal effects."""

    candidate: BenefitCandidate
    tier: str
    reason_codes: tuple[str, ...]
    evidence_adjustment_ppm: int
    expected_benefit_u: int
    expected_cost_u: int
    individual_net_benefit_u: int

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, BenefitCandidate):
            raise BenefitValidationError("assessment candidate is invalid")
        if self.tier not in ASSESSMENT_TIERS:
            raise BenefitValidationError("assessment tier is not declared")
        object.__setattr__(
            self,
            "reason_codes",
            _canonical_tokens(self.reason_codes, "reason_codes"),
        )
        _integer(
            self.evidence_adjustment_ppm,
            "evidence_adjustment_ppm",
            minimum=-MAX_PPM,
            maximum=MAX_PPM,
        )
        for field_name in ("expected_benefit_u", "expected_cost_u"):
            _integer(
                getattr(self, field_name),
                field_name,
                maximum=MAX_UTILITY_UNITS,
            )
        _checked_utility(self.individual_net_benefit_u, "individual_net_benefit_u")

    @property
    def capability_id(self) -> str:
        return self.candidate.capability_id

    @property
    def source_digest(self) -> str:
        return self.candidate.source_digest


@dataclass(frozen=True, slots=True, kw_only=True)
class BenefitSelection:
    """One selected assessment plus its order-independent leave-one-out gain."""

    capability_id: str
    source_digest: str
    tier: str
    individual_net_benefit_u: int
    marginal_net_benefit_u: int

    def __post_init__(self) -> None:
        _capability_id(self.capability_id, "capability_id")
        _digest(self.source_digest, "source_digest")
        if self.tier not in {"executable", "advisory"}:
            raise BenefitValidationError("selection tier must be executable or advisory")
        _checked_utility(self.individual_net_benefit_u, "individual_net_benefit_u")
        _integer(
            self.marginal_net_benefit_u,
            "marginal_net_benefit_u",
            minimum=1,
            maximum=MAX_UTILITY_UNITS,
        )


def _resource_cost_mapping(costs: ResourceCosts) -> dict[str, int]:
    return {field_name: getattr(costs, field_name) for field_name in ResourceCosts._FIELDS}


def _evidence_mapping(evidence: EvidenceSummary) -> dict[str, object]:
    return {
        "capability_id": evidence.capability_id,
        "effective_outcomes": evidence.effective_outcomes,
        "evidence_window_digest": evidence.evidence_window_digest,
        "exposed_count": evidence.exposed_count,
        "failed_invocations": evidence.failed_invocations,
        "harmful_outcomes": evidence.harmful_outcomes,
        "kind": evidence.kind,
        "opportunities_observed": evidence.opportunities_observed,
        "opportunity_observable": evidence.opportunity_observable,
        "source_digest": evidence.source_digest,
        "successful_invocations": evidence.successful_invocations,
        "validated_outcomes": evidence.validated_outcomes,
    }


def _candidate_mapping(candidate: BenefitCandidate) -> dict[str, object]:
    return {
        "availability": candidate.availability,
        "capability_id": candidate.capability_id,
        "complements": candidate.complements,
        "conflicts": candidate.conflicts,
        "costs": None if candidate.costs is None else _resource_cost_mapping(candidate.costs),
        "coverage_keys": candidate.coverage_keys,
        "credentials_available": candidate.credentials_available,
        "equivalence_key": candidate.equivalence_key,
        "evidence": _evidence_mapping(candidate.evidence),
        "expected_task_benefit_ppm": candidate.expected_task_benefit_ppm,
        "permissions_allowed": candidate.permissions_allowed,
        "relevance_ppm": candidate.relevance_ppm,
        "resource_profile_digest": candidate.resource_profile_digest,
        "security_approved": candidate.security_approved,
        "source_digest": candidate.source_digest,
        "source_trusted": candidate.source_trusted,
        "trust_ppm": candidate.trust_ppm,
    }


def _assessment_mapping(assessment: CandidateAssessment) -> dict[str, object]:
    return {
        "candidate": _candidate_mapping(assessment.candidate),
        "evidence_adjustment_ppm": assessment.evidence_adjustment_ppm,
        "expected_benefit_u": assessment.expected_benefit_u,
        "expected_cost_u": assessment.expected_cost_u,
        "individual_net_benefit_u": assessment.individual_net_benefit_u,
        "reason_codes": assessment.reason_codes,
        "tier": assessment.tier,
    }


def _selection_mapping(selection: BenefitSelection) -> dict[str, object]:
    return {
        "capability_id": selection.capability_id,
        "individual_net_benefit_u": selection.individual_net_benefit_u,
        "marginal_net_benefit_u": selection.marginal_net_benefit_u,
        "source_digest": selection.source_digest,
        "tier": selection.tier,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class BenefitSelectionResult:
    """Canonical, versioned result; policy revalidation confers trust."""

    selections: tuple[BenefitSelection, ...]
    assessments: tuple[CandidateAssessment, ...]
    abstention_code: str | None
    policy_digest: str
    requested_limit: int
    candidate_pool_count: int
    search_evaluation_count: int
    result_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.selections, tuple) or not all(
            isinstance(item, BenefitSelection) for item in self.selections
        ):
            raise BenefitValidationError("selections must contain BenefitSelection values")
        if len(self.selections) > MAX_SELECTED_CAPABILITIES:
            raise BenefitValidationError("selection cannot contain more than five capabilities")
        canonical_selections = tuple(
            sorted(
                self.selections,
                key=lambda item: (
                    0 if item.tier == "executable" else 1,
                    item.capability_id,
                    item.source_digest,
                ),
            )
        )
        if self.selections != canonical_selections:
            raise BenefitValidationError("selections must use canonical tier and identity order")
        selection_ids = tuple(item.capability_id for item in self.selections)
        if len(set(selection_ids)) != len(selection_ids):
            raise BenefitValidationError("selection contains duplicate capability identities")
        if not isinstance(self.assessments, tuple) or not all(
            isinstance(item, CandidateAssessment) for item in self.assessments
        ):
            raise BenefitValidationError("assessments must contain CandidateAssessment values")
        canonical_assessments = tuple(
            sorted(
                self.assessments,
                key=lambda item: (item.capability_id, item.source_digest),
            )
        )
        if self.assessments != canonical_assessments:
            raise BenefitValidationError("assessments must use canonical identity order")
        assessment_keys = tuple(
            (item.capability_id, item.source_digest) for item in self.assessments
        )
        assessment_ids = tuple(item.capability_id for item in self.assessments)
        if len(set(assessment_keys)) != len(assessment_keys) or len(set(assessment_ids)) != len(
            assessment_ids
        ):
            raise BenefitValidationError("assessments contain duplicate candidate identities")
        assessments_by_key = {
            (item.capability_id, item.source_digest): item for item in self.assessments
        }
        for selection in self.selections:
            assessment = assessments_by_key.get((selection.capability_id, selection.source_digest))
            if (
                assessment is None
                or assessment.tier != selection.tier
                or assessment.individual_net_benefit_u != selection.individual_net_benefit_u
            ):
                raise BenefitValidationError(
                    "selection does not exactly project a matching assessment"
                )
        _integer(
            self.requested_limit,
            "requested_limit",
            maximum=MAX_SELECTED_CAPABILITIES,
        )
        if len(self.selections) > self.requested_limit:
            raise BenefitValidationError("selection count cannot exceed requested_limit")
        _integer(
            self.candidate_pool_count,
            "candidate_pool_count",
            maximum=MAX_CANDIDATES,
        )
        _integer(
            self.search_evaluation_count,
            "search_evaluation_count",
            maximum=MAX_SEARCH_EVALUATIONS,
        )
        if self.candidate_pool_count != len(self.assessments):
            raise BenefitValidationError(
                "candidate_pool_count must equal the canonical assessment count"
            )
        feasible = any(item.tier != "ineligible" for item in self.assessments)
        expected_abstention = (
            None
            if self.selections
            else "limit-zero"
            if self.requested_limit == 0
            else "no-feasible-capability"
            if not feasible
            else "below-net-benefit"
        )
        if self.abstention_code != expected_abstention:
            raise BenefitValidationError(
                "abstention_code does not match selection and feasibility semantics"
            )
        if self.requested_limit == 0 and self.selections:
            raise BenefitValidationError("limit-zero result cannot contain selections")
        if self.abstention_code in {"limit-zero", "no-feasible-capability"} and (
            self.search_evaluation_count != 0
        ):
            raise BenefitValidationError("non-search abstention must have zero search evaluations")
        _digest(self.policy_digest, "policy_digest")
        supplied_digest = _digest(self.result_digest, "result_digest")
        if supplied_digest != self.recomputed_result_digest:
            raise BenefitValidationError("result_digest does not match canonical result fields")

    @property
    def result_schema_id(self) -> str:
        return _RESULT_SCHEMA_ID

    def _digest_mapping(self) -> dict[str, object]:
        return {
            "abstention_code": self.abstention_code,
            "assessments": tuple(_assessment_mapping(item) for item in self.assessments),
            "candidate_pool_count": self.candidate_pool_count,
            "policy_digest": self.policy_digest,
            "requested_limit": self.requested_limit,
            "schema": self.result_schema_id,
            "search_evaluation_count": self.search_evaluation_count,
            "selections": tuple(_selection_mapping(item) for item in self.selections),
        }

    @property
    def recomputed_result_digest(self) -> str:
        return _canonical_digest(self._digest_mapping())

    def to_json(self) -> str:
        """Encode the complete audit result as strict canonical JSON.

        The schema contains only bounded safe tokens, digests, booleans, and
        integers.  It deliberately has no fields capable of carrying prompts,
        source code, or filesystem paths.
        """

        encoded = json.dumps(
            self._digest_mapping() | {"result_digest": self.result_digest},
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > MAX_BENEFIT_RESULT_JSON_BYTES:
            raise BenefitValidationError("benefit result JSON exceeds its bounded byte limit")
        return encoded

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> BenefitSelectionResult:
        """Decode canonical bounded audit JSON and revalidate its digest."""

        return _benefit_result_from_json(value)

    @classmethod
    def _create(
        cls,
        *,
        selections: tuple[BenefitSelection, ...],
        assessments: tuple[CandidateAssessment, ...],
        abstention_code: str | None,
        policy_digest: str,
        requested_limit: int,
        candidate_pool_count: int,
        search_evaluation_count: int,
    ) -> BenefitSelectionResult:
        mapping = {
            "abstention_code": abstention_code,
            "assessments": tuple(_assessment_mapping(item) for item in assessments),
            "candidate_pool_count": candidate_pool_count,
            "policy_digest": policy_digest,
            "requested_limit": requested_limit,
            "schema": _RESULT_SCHEMA_ID,
            "search_evaluation_count": search_evaluation_count,
            "selections": tuple(_selection_mapping(item) for item in selections),
        }
        return cls(
            selections=selections,
            assessments=assessments,
            abstention_code=abstention_code,
            policy_digest=policy_digest,
            requested_limit=requested_limit,
            candidate_pool_count=candidate_pool_count,
            search_evaluation_count=search_evaluation_count,
            result_digest=_canonical_digest(mapping),
        )


def _closed_mapping(
    value: object,
    *,
    field_name: str,
    fields: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BenefitValidationError(f"{field_name} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise BenefitValidationError(f"{field_name} fields must be strings")
    actual = frozenset(value)
    if actual != fields:
        raise BenefitValidationError(f"{field_name} has invalid fields")
    return value


def _bounded_json_array(
    value: object,
    *,
    field_name: str,
    maximum: int,
) -> list[object]:
    if not isinstance(value, list):
        raise BenefitValidationError(f"{field_name} must be a JSON array")
    if len(value) > maximum:
        raise BenefitValidationError(f"{field_name} exceeds its bounded item limit")
    return value


def _resource_costs_from_mapping(value: object) -> ResourceCosts:
    mapping = _closed_mapping(
        value,
        field_name="costs",
        fields=frozenset(ResourceCosts._FIELDS),
    )
    return ResourceCosts(
        context_tokens=_json_integer(mapping["context_tokens"], "context_tokens"),
        tool_schema_tokens=_json_integer(mapping["tool_schema_tokens"], "tool_schema_tokens"),
        runtime_millis=_json_integer(mapping["runtime_millis"], "runtime_millis"),
        permission_burden_units=_json_integer(
            mapping["permission_burden_units"], "permission_burden_units"
        ),
        credential_burden_units=_json_integer(
            mapping["credential_burden_units"], "credential_burden_units"
        ),
        approval_prompts=_json_integer(mapping["approval_prompts"], "approval_prompts"),
        process_units=_json_integer(mapping["process_units"], "process_units"),
        child_agent_units=_json_integer(mapping["child_agent_units"], "child_agent_units"),
    )


def _json_integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise BenefitValidationError(f"{field_name} must be an integer")
    return value


def _evidence_from_mapping(value: object) -> EvidenceSummary:
    fields = frozenset(
        {
            "capability_id",
            "effective_outcomes",
            "evidence_window_digest",
            "exposed_count",
            "failed_invocations",
            "harmful_outcomes",
            "kind",
            "opportunities_observed",
            "opportunity_observable",
            "source_digest",
            "successful_invocations",
            "validated_outcomes",
        }
    )
    mapping = _closed_mapping(value, field_name="evidence", fields=fields)
    return EvidenceSummary(
        capability_id=mapping["capability_id"],  # type: ignore[arg-type]
        kind=mapping["kind"],  # type: ignore[arg-type]
        source_digest=mapping["source_digest"],  # type: ignore[arg-type]
        evidence_window_digest=mapping["evidence_window_digest"],  # type: ignore[arg-type]
        opportunity_observable=mapping["opportunity_observable"],  # type: ignore[arg-type]
        opportunities_observed=mapping["opportunities_observed"],  # type: ignore[arg-type]
        exposed_count=mapping["exposed_count"],  # type: ignore[arg-type]
        successful_invocations=mapping["successful_invocations"],  # type: ignore[arg-type]
        failed_invocations=mapping["failed_invocations"],  # type: ignore[arg-type]
        effective_outcomes=mapping["effective_outcomes"],  # type: ignore[arg-type]
        validated_outcomes=mapping["validated_outcomes"],  # type: ignore[arg-type]
        harmful_outcomes=mapping["harmful_outcomes"],  # type: ignore[arg-type]
    )


def _token_tuple_from_json(value: object, field_name: str) -> tuple[str, ...]:
    items = _bounded_json_array(value, field_name=field_name, maximum=MAX_RELATION_KEYS)
    if not all(isinstance(item, str) for item in items):
        raise BenefitValidationError(f"{field_name} items must be strings")
    return tuple(item for item in items if isinstance(item, str))


def _candidate_from_mapping(value: object) -> BenefitCandidate:
    fields = frozenset(
        {
            "availability",
            "capability_id",
            "complements",
            "conflicts",
            "costs",
            "coverage_keys",
            "credentials_available",
            "equivalence_key",
            "evidence",
            "expected_task_benefit_ppm",
            "permissions_allowed",
            "relevance_ppm",
            "resource_profile_digest",
            "security_approved",
            "source_digest",
            "source_trusted",
            "trust_ppm",
        }
    )
    mapping = _closed_mapping(value, field_name="candidate", fields=fields)
    costs_value = mapping["costs"]
    equivalence_key = mapping["equivalence_key"]
    if equivalence_key is not None and not isinstance(equivalence_key, str):
        raise BenefitValidationError("equivalence_key must be a string or null")
    return BenefitCandidate(
        capability_id=mapping["capability_id"],  # type: ignore[arg-type]
        source_digest=mapping["source_digest"],  # type: ignore[arg-type]
        resource_profile_digest=mapping["resource_profile_digest"],  # type: ignore[arg-type]
        availability=mapping["availability"],  # type: ignore[arg-type]
        expected_task_benefit_ppm=mapping["expected_task_benefit_ppm"],  # type: ignore[arg-type]
        relevance_ppm=mapping["relevance_ppm"],  # type: ignore[arg-type]
        trust_ppm=mapping["trust_ppm"],  # type: ignore[arg-type]
        costs=None if costs_value is None else _resource_costs_from_mapping(costs_value),
        evidence=_evidence_from_mapping(mapping["evidence"]),
        source_trusted=mapping["source_trusted"],  # type: ignore[arg-type]
        security_approved=mapping["security_approved"],  # type: ignore[arg-type]
        permissions_allowed=mapping["permissions_allowed"],  # type: ignore[arg-type]
        credentials_available=mapping["credentials_available"],  # type: ignore[arg-type]
        coverage_keys=_token_tuple_from_json(mapping["coverage_keys"], "coverage_keys"),
        equivalence_key=equivalence_key,
        complements=_token_tuple_from_json(mapping["complements"], "complements"),
        conflicts=_token_tuple_from_json(mapping["conflicts"], "conflicts"),
    )


def _assessment_from_mapping(value: object) -> CandidateAssessment:
    fields = frozenset(
        {
            "candidate",
            "evidence_adjustment_ppm",
            "expected_benefit_u",
            "expected_cost_u",
            "individual_net_benefit_u",
            "reason_codes",
            "tier",
        }
    )
    mapping = _closed_mapping(value, field_name="assessment", fields=fields)
    return CandidateAssessment(
        candidate=_candidate_from_mapping(mapping["candidate"]),
        tier=mapping["tier"],  # type: ignore[arg-type]
        reason_codes=_token_tuple_from_json(mapping["reason_codes"], "reason_codes"),
        evidence_adjustment_ppm=mapping["evidence_adjustment_ppm"],  # type: ignore[arg-type]
        expected_benefit_u=mapping["expected_benefit_u"],  # type: ignore[arg-type]
        expected_cost_u=mapping["expected_cost_u"],  # type: ignore[arg-type]
        individual_net_benefit_u=mapping["individual_net_benefit_u"],  # type: ignore[arg-type]
    )


def _selection_from_mapping(value: object) -> BenefitSelection:
    fields = frozenset(
        {
            "capability_id",
            "individual_net_benefit_u",
            "marginal_net_benefit_u",
            "source_digest",
            "tier",
        }
    )
    mapping = _closed_mapping(value, field_name="selection", fields=fields)
    return BenefitSelection(
        capability_id=mapping["capability_id"],  # type: ignore[arg-type]
        source_digest=mapping["source_digest"],  # type: ignore[arg-type]
        tier=mapping["tier"],  # type: ignore[arg-type]
        individual_net_benefit_u=mapping["individual_net_benefit_u"],  # type: ignore[arg-type]
        marginal_net_benefit_u=mapping["marginal_net_benefit_u"],  # type: ignore[arg-type]
    )


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BenefitValidationError("benefit result JSON contains a duplicate field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise BenefitValidationError(f"benefit result JSON contains invalid constant {value}")


def _benefit_result_from_json(value: str | bytes | bytearray) -> BenefitSelectionResult:
    if isinstance(value, str):
        if len(value) > MAX_BENEFIT_RESULT_JSON_BYTES:
            raise BenefitValidationError("benefit result JSON exceeds its bounded byte limit")
        if not value.isascii():
            raise BenefitValidationError("benefit result JSON must use canonical ASCII encoding")
        encoded = value.encode("ascii")
        text = value
    elif isinstance(value, bytes):
        if len(value) > MAX_BENEFIT_RESULT_JSON_BYTES:
            raise BenefitValidationError("benefit result JSON exceeds its bounded byte limit")
        encoded = value
        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BenefitValidationError("benefit result JSON must be UTF-8") from exc
    elif isinstance(value, bytearray):
        if len(value) > MAX_BENEFIT_RESULT_JSON_BYTES:
            raise BenefitValidationError("benefit result JSON exceeds its bounded byte limit")
        encoded = bytes(value)
        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BenefitValidationError("benefit result JSON must be UTF-8") from exc
    else:
        raise TypeError("benefit result JSON must be str, bytes, or bytearray")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except BenefitValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BenefitValidationError("benefit result must be valid JSON") from exc
    fields = frozenset(
        {
            "abstention_code",
            "assessments",
            "candidate_pool_count",
            "policy_digest",
            "requested_limit",
            "result_digest",
            "schema",
            "search_evaluation_count",
            "selections",
        }
    )
    mapping = _closed_mapping(decoded, field_name="benefit result", fields=fields)
    if mapping["schema"] != _RESULT_SCHEMA_ID:
        raise BenefitValidationError("benefit result schema is unsupported")
    abstention_code = mapping["abstention_code"]
    if abstention_code is not None and not isinstance(abstention_code, str):
        raise BenefitValidationError("abstention_code must be a string or null")
    selections = tuple(
        _selection_from_mapping(item)
        for item in _bounded_json_array(
            mapping["selections"],
            field_name="selections",
            maximum=MAX_SELECTED_CAPABILITIES,
        )
    )
    assessments = tuple(
        _assessment_from_mapping(item)
        for item in _bounded_json_array(
            mapping["assessments"],
            field_name="assessments",
            maximum=MAX_CANDIDATES,
        )
    )
    result = BenefitSelectionResult(
        selections=selections,
        assessments=assessments,
        abstention_code=abstention_code,
        policy_digest=mapping["policy_digest"],  # type: ignore[arg-type]
        requested_limit=mapping["requested_limit"],  # type: ignore[arg-type]
        candidate_pool_count=mapping["candidate_pool_count"],  # type: ignore[arg-type]
        search_evaluation_count=mapping["search_evaluation_count"],  # type: ignore[arg-type]
        result_digest=mapping["result_digest"],  # type: ignore[arg-type]
    )
    if result.to_json() != text:
        raise BenefitValidationError("benefit result JSON must use canonical encoding")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class NetBenefitPolicy:
    """Frozen policy using greedy admission plus bounded subset exchange.

    Admission always requires positive direct marginal value against the current
    frozen set.  Consequently, two mutually nonpositive unselected candidates
    cannot bootstrap one another merely by declaring complementarity.  The search
    explores every removal subset of the at-most-five non-frozen selections,
    greedily refills each neighborhood, accepts strict utility improvement, and
    repeats to stability before leave-one-out pruning.  This bounded local search
    is not a proof of the global optimum of the underlying NP-hard problem.
    """

    calibration_digest: str
    minimum_relevance_ppm: int
    minimum_trust_ppm: int = 0
    minimum_marginal_net_benefit_u: int = 1
    context_token_cost_u: int = 0
    tool_schema_token_cost_u: int = 0
    runtime_millisecond_cost_u: int = 0
    permission_burden_cost_u: int = 0
    credential_burden_cost_u: int = 0
    approval_prompt_cost_u: int = 0
    process_unit_cost_u: int = 0
    child_agent_unit_cost_u: int = 0
    new_coverage_bonus_u_per_key: int = 0
    overlap_penalty_u_per_key: int = 0
    complementarity_bonus_u: int = 0
    successful_invocation_evidence_ppm: int = 100_000
    failed_invocation_evidence_ppm: int = -250_000
    effective_outcome_evidence_ppm: int = 500_000
    validated_outcome_evidence_ppm: int = 1_000_000
    harmful_outcome_evidence_ppm: int = -1_000_000
    idle_opportunity_evidence_ppm: int = -100_000
    evidence_prior_observations: int = 1
    policy_digest: str = field(init=False)

    _NONNEGATIVE_WEIGHT_FIELDS: ClassVar[tuple[str, ...]] = (
        "context_token_cost_u",
        "tool_schema_token_cost_u",
        "runtime_millisecond_cost_u",
        "permission_burden_cost_u",
        "credential_burden_cost_u",
        "approval_prompt_cost_u",
        "process_unit_cost_u",
        "child_agent_unit_cost_u",
        "new_coverage_bonus_u_per_key",
        "overlap_penalty_u_per_key",
        "complementarity_bonus_u",
    )
    _POSITIVE_EVIDENCE_FIELDS: ClassVar[tuple[str, ...]] = (
        "successful_invocation_evidence_ppm",
        "effective_outcome_evidence_ppm",
        "validated_outcome_evidence_ppm",
    )
    _NEGATIVE_EVIDENCE_FIELDS: ClassVar[tuple[str, ...]] = (
        "failed_invocation_evidence_ppm",
        "harmful_outcome_evidence_ppm",
        "idle_opportunity_evidence_ppm",
    )

    def __post_init__(self) -> None:
        _digest(self.calibration_digest, "calibration_digest")
        _integer(
            self.minimum_relevance_ppm,
            "minimum_relevance_ppm",
            maximum=MAX_PPM,
        )
        _integer(self.minimum_trust_ppm, "minimum_trust_ppm", maximum=MAX_PPM)
        _integer(
            self.minimum_marginal_net_benefit_u,
            "minimum_marginal_net_benefit_u",
            minimum=1,
            maximum=MAX_UTILITY_UNITS,
        )
        for field_name in self._NONNEGATIVE_WEIGHT_FIELDS:
            _integer(getattr(self, field_name), field_name)
        for field_name in self._POSITIVE_EVIDENCE_FIELDS:
            _integer(getattr(self, field_name), field_name, maximum=MAX_PPM)
        for field_name in self._NEGATIVE_EVIDENCE_FIELDS:
            _integer(
                getattr(self, field_name),
                field_name,
                minimum=-MAX_PPM,
                maximum=0,
            )
        _integer(
            self.evidence_prior_observations,
            "evidence_prior_observations",
            minimum=1,
        )
        if not (
            self.successful_invocation_evidence_ppm
            < self.effective_outcome_evidence_ppm
            < self.validated_outcome_evidence_ppm
        ):
            raise BenefitValidationError(
                "positive evidence weights must order success below effect below validation"
            )
        if not (self.harmful_outcome_evidence_ppm < self.failed_invocation_evidence_ppm <= 0):
            raise BenefitValidationError(
                "negative evidence weights must order harm below invocation failure"
            )
        canonical = json.dumps(
            {
                field_name: getattr(self, field_name)
                for field_name in (
                    "calibration_digest",
                    "minimum_relevance_ppm",
                    "minimum_trust_ppm",
                    "minimum_marginal_net_benefit_u",
                    *self._NONNEGATIVE_WEIGHT_FIELDS,
                    *self._POSITIVE_EVIDENCE_FIELDS,
                    *self._NEGATIVE_EVIDENCE_FIELDS,
                    "evidence_prior_observations",
                )
            }
            | {
                "policy_schema_id": self.policy_schema_id,
                "selection_algorithm_id": self.selection_algorithm_id,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        object.__setattr__(
            self,
            "policy_digest",
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    @property
    def policy_schema_id(self) -> str:
        return _POLICY_SCHEMA_ID

    @property
    def selection_algorithm_id(self) -> str:
        return _SELECTION_ALGORITHM_ID

    def _evidence_adjustment(self, evidence: EvidenceSummary) -> int:
        attempted = evidence.successful_invocations + evidence.failed_invocations
        idle = (
            max(evidence.opportunities_observed - attempted, 0)
            if evidence.opportunity_observable
            else 0
        )
        weighted = (
            evidence.successful_invocations * self.successful_invocation_evidence_ppm
            + evidence.failed_invocations * self.failed_invocation_evidence_ppm
            + evidence.effective_outcomes * self.effective_outcome_evidence_ppm
            + evidence.validated_outcomes * self.validated_outcome_evidence_ppm
            + evidence.harmful_outcomes * self.harmful_outcome_evidence_ppm
            + idle * self.idle_opportunity_evidence_ppm
        )
        observations = (
            evidence.successful_invocations
            + evidence.failed_invocations
            + evidence.effective_outcomes
            + evidence.validated_outcomes
            + evidence.harmful_outcomes
            + idle
        )
        adjustment = _signed_rounded_divide(
            weighted,
            self.evidence_prior_observations + observations,
        )
        return min(MAX_PPM, max(-MAX_PPM, adjustment))

    def _resource_cost(self, costs: ResourceCosts) -> int:
        total = (
            costs.context_tokens * self.context_token_cost_u
            + costs.tool_schema_tokens * self.tool_schema_token_cost_u
            + costs.runtime_millis * self.runtime_millisecond_cost_u
            + costs.permission_burden_units * self.permission_burden_cost_u
            + costs.credential_burden_units * self.credential_burden_cost_u
            + costs.approval_prompts * self.approval_prompt_cost_u
            + costs.process_units * self.process_unit_cost_u
            + costs.child_agent_units * self.child_agent_unit_cost_u
        )
        return _integer(total, "expected_cost_u", maximum=MAX_UTILITY_UNITS)

    def assess(self, candidate: BenefitCandidate) -> CandidateAssessment:
        """Apply hard gates, then compute one candidate's integer net benefit."""

        if not isinstance(candidate, BenefitCandidate):
            raise TypeError("candidate must be a BenefitCandidate")
        reasons: set[str] = set()
        if candidate.availability == "unsupported":
            reasons.add("host-unsupported")
        if not candidate.source_trusted:
            reasons.add("source-untrusted")
        if not candidate.security_approved:
            reasons.add("security-blocked")
        if not candidate.permissions_allowed:
            reasons.add("permission-blocked")
        if not candidate.credentials_available:
            reasons.add("credential-unavailable")
        if candidate.relevance_ppm < self.minimum_relevance_ppm:
            reasons.add("relevance-below-threshold")
        if candidate.trust_ppm < self.minimum_trust_ppm:
            reasons.add("trust-below-threshold")
        if candidate.costs is None:
            reasons.add("resource-cost-unknown")
        if reasons:
            return CandidateAssessment(
                candidate=candidate,
                tier="ineligible",
                reason_codes=tuple(sorted(reasons)),
                evidence_adjustment_ppm=0,
                expected_benefit_u=0,
                expected_cost_u=0,
                individual_net_benefit_u=0,
            )

        evidence_adjustment = self._evidence_adjustment(candidate.evidence)
        expected_effect_ppm = min(
            MAX_PPM,
            max(0, candidate.expected_task_benefit_ppm + evidence_adjustment),
        )
        expected_benefit = _ppm_multiply(candidate.relevance_ppm, expected_effect_ppm)
        expected_benefit = _ppm_multiply(expected_benefit, candidate.trust_ppm)
        costs = candidate.costs
        if costs is None:  # Narrowing for static type checkers; hard gate returned above.
            raise AssertionError("unreachable unknown resource cost")
        expected_cost = self._resource_cost(costs)
        net_benefit = _checked_utility(
            expected_benefit - expected_cost,
            "individual_net_benefit_u",
        )
        reason_codes = {"net-benefit-assessed"}
        if candidate.availability == "advisory":
            reason_codes.add("advisory-only")
        if evidence_adjustment > 0:
            reason_codes.add("positive-outcome-evidence")
        elif evidence_adjustment < 0:
            reason_codes.add("negative-outcome-evidence")
        return CandidateAssessment(
            candidate=candidate,
            tier=candidate.availability,
            reason_codes=tuple(sorted(reason_codes)),
            evidence_adjustment_ppm=evidence_adjustment,
            expected_benefit_u=expected_benefit,
            expected_cost_u=expected_cost,
            individual_net_benefit_u=net_benefit,
        )

    @staticmethod
    def _complementary(left: BenefitCandidate, right: BenefitCandidate) -> bool:
        return left.capability_id in right.complements and right.capability_id in left.complements

    @staticmethod
    def _conflicting(left: BenefitCandidate, right: BenefitCandidate) -> bool:
        return left.capability_id in right.conflicts or right.capability_id in left.conflicts

    def _marginal_net_benefit(
        self,
        assessment: CandidateAssessment,
        selected: tuple[CandidateAssessment, ...],
    ) -> int | None:
        if not self._can_add(assessment, selected):
            return None
        candidate = assessment.candidate
        selected_candidates = tuple(item.candidate for item in selected)
        covered = {key for prior in selected_candidates for key in prior.coverage_keys}
        coverage = set(candidate.coverage_keys)
        complement_count = sum(
            self._complementary(candidate, prior) for prior in selected_candidates
        )
        value = (
            assessment.individual_net_benefit_u
            + len(coverage - covered) * self.new_coverage_bonus_u_per_key
            - len(coverage & covered) * self.overlap_penalty_u_per_key
            + complement_count * self.complementarity_bonus_u
        )
        return _checked_utility(value, "marginal_net_benefit_u")

    def _can_add(
        self,
        assessment: CandidateAssessment,
        selected: tuple[CandidateAssessment, ...],
    ) -> bool:
        candidate = assessment.candidate
        selected_candidates = tuple(item.candidate for item in selected)
        if any(self._conflicting(candidate, prior) for prior in selected_candidates):
            return False
        return not (
            candidate.equivalence_key is not None
            and any(
                candidate.equivalence_key == prior.equivalence_key
                and not self._complementary(candidate, prior)
                for prior in selected_candidates
            )
        )

    @staticmethod
    def _canonical_set(
        values: Sequence[CandidateAssessment],
    ) -> tuple[CandidateAssessment, ...]:
        return tuple(
            sorted(
                values,
                key=lambda item: (item.capability_id, item.source_digest),
            )
        )

    def _set_utility(self, selected: tuple[CandidateAssessment, ...]) -> int:
        """Return order-independent utility for one feasible capability set."""

        coverage_counts: dict[str, int] = {}
        for assessment in selected:
            for key in assessment.candidate.coverage_keys:
                coverage_counts[key] = coverage_counts.get(key, 0) + 1
        unique_coverage = len(coverage_counts)
        overlap = sum(count - 1 for count in coverage_counts.values())
        complement_pairs = sum(
            self._complementary(left.candidate, right.candidate)
            for index, left in enumerate(selected)
            for right in selected[index + 1 :]
        )
        value = (
            sum(item.individual_net_benefit_u for item in selected)
            + unique_coverage * self.new_coverage_bonus_u_per_key
            - overlap * self.overlap_penalty_u_per_key
            + complement_pairs * self.complementarity_bonus_u
        )
        return _checked_utility(value, "set_utility_u")

    def _leave_one_out_contribution(
        self,
        assessment: CandidateAssessment,
        selected: tuple[CandidateAssessment, ...],
    ) -> int:
        without = tuple(item for item in selected if item is not assessment)
        return _checked_utility(
            self._set_utility(selected) - self._set_utility(without),
            "leave_one_out_contribution_u",
        )

    def _set_meets_marginal_threshold(
        self,
        selected: tuple[CandidateAssessment, ...],
    ) -> bool:
        return all(
            self._leave_one_out_contribution(item, selected) >= self.minimum_marginal_net_benefit_u
            for item in selected
        )

    def _addition_preserves_retained_value(
        self,
        assessment: CandidateAssessment,
        selected: tuple[CandidateAssessment, ...],
        *,
        marginal: int,
        coverage_counts: dict[str, int],
        retained_contributions: dict[str, int],
    ) -> bool:
        """Check every prospective leave-one-out contribution incrementally."""

        if marginal < self.minimum_marginal_net_benefit_u:
            return False
        candidate = assessment.candidate
        candidate_coverage = set(candidate.coverage_keys)
        for prior_assessment in selected:
            prior = prior_assessment.candidate
            contribution = retained_contributions[prior.capability_id]
            if self._complementary(candidate, prior):
                contribution += self.complementarity_bonus_u
            uniquely_covered_overlap = sum(
                coverage_counts[key] == 1 for key in candidate_coverage & set(prior.coverage_keys)
            )
            contribution -= uniquely_covered_overlap * (
                self.new_coverage_bonus_u_per_key + self.overlap_penalty_u_per_key
            )
            if contribution < self.minimum_marginal_net_benefit_u:
                return False
        return True

    def _prune_set(
        self,
        selected: tuple[CandidateAssessment, ...],
        *,
        frozen: tuple[CandidateAssessment, ...],
    ) -> tuple[CandidateAssessment, ...]:
        """Prune additions until every retained member has positive set value."""

        current = self._canonical_set(selected)
        frozen_ids = {item.capability_id for item in frozen}
        while current and any(
            self._leave_one_out_contribution(item, current) < self.minimum_marginal_net_benefit_u
            for item in current
        ):
            removable = tuple(item for item in current if item.capability_id not in frozen_ids)
            if not removable:
                return self._canonical_set(frozen)
            _removed, current = min(
                (
                    (
                        item,
                        self._canonical_set(tuple(value for value in current if value is not item)),
                    )
                    for item in removable
                ),
                key=lambda pair: (
                    -self._set_utility(pair[1]),
                    tuple(value.capability_id for value in pair[1]),
                ),
            )
        return current

    def _best_direct_admission(
        self,
        pool: tuple[CandidateAssessment, ...],
        *,
        selected: tuple[CandidateAssessment, ...],
        excluded_ids: frozenset[str],
    ) -> tuple[CandidateAssessment | None, int, int]:
        selected_ids = {item.capability_id for item in selected}
        coverage_counts: dict[str, int] = {}
        for item in selected:
            for key in item.candidate.coverage_keys:
                coverage_counts[key] = coverage_counts.get(key, 0) + 1
        retained_contributions = {
            item.capability_id: self._leave_one_out_contribution(item, selected)
            for item in selected
        }
        scored: list[tuple[int, CandidateAssessment]] = []
        evaluations = 0
        for assessment in pool:
            if assessment.capability_id in selected_ids or assessment.capability_id in excluded_ids:
                continue
            evaluations += 1
            marginal = self._marginal_net_benefit(assessment, selected)
            if marginal is not None and self._addition_preserves_retained_value(
                assessment,
                selected,
                marginal=marginal,
                coverage_counts=coverage_counts,
                retained_contributions=retained_contributions,
            ):
                scored.append((marginal, assessment))
        if not scored:
            return None, 0, evaluations
        marginal, winner = min(
            scored,
            key=lambda item: (
                -item[0],
                -item[1].individual_net_benefit_u,
                -item[1].candidate.relevance_ppm,
                item[1].capability_id,
                item[1].source_digest,
            ),
        )
        return winner, marginal, evaluations

    def _greedy_fill(
        self,
        pool: tuple[CandidateAssessment, ...],
        *,
        selected: tuple[CandidateAssessment, ...],
        excluded_ids: frozenset[str],
        requested_limit: int,
    ) -> tuple[tuple[CandidateAssessment, ...], int]:
        current = self._canonical_set(selected)
        evaluations = 0
        while len(current) < requested_limit:
            winner, _marginal, step_evaluations = self._best_direct_admission(
                pool,
                selected=current,
                excluded_ids=excluded_ids,
            )
            evaluations += step_evaluations
            if winner is None:
                break
            current = self._canonical_set((*current, winner))
        return current, evaluations

    def _solution_key(
        self,
        values: tuple[CandidateAssessment, ...],
    ) -> tuple[int, int, tuple[str, ...], tuple[str, ...]]:
        return (
            -self._set_utility(values),
            len(values),
            tuple(item.capability_id for item in values),
            tuple(item.source_digest for item in values),
        )

    def _search_tier(
        self,
        pool: tuple[CandidateAssessment, ...],
        *,
        frozen: tuple[CandidateAssessment, ...],
        requested_limit: int,
    ) -> tuple[tuple[CandidateAssessment, ...], int]:
        """Greedily build, then improve by bounded selected-subset exchange."""

        if len(frozen) >= requested_limit:
            return self._canonical_set(frozen), 0
        current, evaluations = self._greedy_fill(
            pool,
            selected=frozen,
            excluded_ids=frozenset(),
            requested_limit=requested_limit,
        )
        frozen_ids = {item.capability_id for item in frozen}
        while True:
            movable = tuple(item for item in current if item.capability_id not in frozen_ids)
            current_utility = self._set_utility(current)
            improving: list[tuple[CandidateAssessment, ...]] = []
            for size in range(1, len(movable) + 1):
                for removed in combinations(movable, size):
                    removed_ids = frozenset(item.capability_id for item in removed)
                    base = self._canonical_set(
                        tuple(item for item in current if item not in removed)
                    )
                    base = self._prune_set(base, frozen=frozen)
                    neighbor, neighbor_evaluations = self._greedy_fill(
                        pool,
                        selected=base,
                        excluded_ids=removed_ids,
                        requested_limit=requested_limit,
                    )
                    evaluations += neighbor_evaluations
                    if evaluations > MAX_SEARCH_EVALUATIONS:
                        raise BenefitValidationError(
                            "search exceeded its deterministic evaluation bound"
                        )
                    if (
                        self._set_meets_marginal_threshold(neighbor)
                        and self._set_utility(neighbor) > current_utility
                    ):
                        improving.append(neighbor)
            if not improving:
                break
            current = min(improving, key=self._solution_key)
        if not self._set_meets_marginal_threshold(current):
            raise BenefitValidationError(
                "search terminated with a below-threshold retained capability"
            )
        return current, evaluations

    def select(
        self,
        candidates: Sequence[BenefitCandidate],
        *,
        requested_limit: int = MAX_SELECTED_CAPABILITIES,
    ) -> BenefitSelectionResult:
        """Select executable value first, then advisory value for unused slots."""

        if isinstance(candidates, (str, bytes, bytearray)) or not isinstance(candidates, Sequence):
            raise TypeError("candidates must be a bounded sequence")
        if len(candidates) > MAX_CANDIDATES:
            raise BenefitValidationError("candidate pool exceeds its bounded limit")
        _integer(
            requested_limit,
            "requested_limit",
            maximum=MAX_SELECTED_CAPABILITIES,
        )
        values = tuple(candidates)
        if not all(isinstance(candidate, BenefitCandidate) for candidate in values):
            raise TypeError("candidate pool contains an invalid value")
        identities = tuple(candidate.capability_id for candidate in values)
        if len(set(identities)) != len(identities):
            raise BenefitValidationError("candidate pool contains duplicate capability identities")
        candidates_by_id = {candidate.capability_id: candidate for candidate in values}
        for candidate in values:
            for peer_id in candidate.complements:
                peer = candidates_by_id.get(peer_id)
                if peer is not None and (
                    peer_id in candidate.conflicts or candidate.capability_id in peer.conflicts
                ):
                    raise BenefitValidationError(
                        "candidate pool contains a cross-record complement/conflict contradiction"
                    )

        assessments = tuple(
            sorted(
                (self.assess(candidate) for candidate in values),
                key=lambda item: (item.capability_id, item.source_digest),
            )
        )
        if requested_limit == 0:
            return BenefitSelectionResult._create(
                selections=(),
                assessments=assessments,
                abstention_code="limit-zero",
                policy_digest=self.policy_digest,
                requested_limit=requested_limit,
                candidate_pool_count=len(values),
                search_evaluation_count=0,
            )

        feasible = tuple(item for item in assessments if item.tier != "ineligible")
        if not feasible:
            return BenefitSelectionResult._create(
                selections=(),
                assessments=assessments,
                abstention_code="no-feasible-capability",
                policy_digest=self.policy_digest,
                requested_limit=requested_limit,
                candidate_pool_count=len(values),
                search_evaluation_count=0,
            )

        executable = tuple(item for item in feasible if item.tier == "executable")
        selected_executable, executable_evaluations = self._search_tier(
            executable,
            frozen=(),
            requested_limit=requested_limit,
        )
        advisory = tuple(item for item in feasible if item.tier == "advisory")
        selected_assessments, advisory_evaluations = self._search_tier(
            advisory,
            frozen=selected_executable,
            requested_limit=requested_limit,
        )
        ordered_selected = tuple(
            sorted(
                selected_assessments,
                key=lambda item: (
                    0 if item.tier == "executable" else 1,
                    item.capability_id,
                    item.source_digest,
                ),
            )
        )
        selections = tuple(
            BenefitSelection(
                capability_id=assessment.capability_id,
                source_digest=assessment.source_digest,
                tier=assessment.tier,
                individual_net_benefit_u=assessment.individual_net_benefit_u,
                marginal_net_benefit_u=self._leave_one_out_contribution(
                    assessment,
                    selected_assessments,
                ),
            )
            for assessment in ordered_selected
        )

        return BenefitSelectionResult._create(
            selections=selections,
            assessments=assessments,
            abstention_code=None if selections else "below-net-benefit",
            policy_digest=self.policy_digest,
            requested_limit=requested_limit,
            candidate_pool_count=len(values),
            search_evaluation_count=(executable_evaluations + advisory_evaluations),
        )

    def validate_result(self, result: BenefitSelectionResult) -> None:
        """Recompute a result from its exact candidate facts and reject drift."""

        if not isinstance(result, BenefitSelectionResult):
            raise TypeError("result must be a BenefitSelectionResult")
        if result.policy_digest != self.policy_digest:
            raise BenefitValidationError("result policy digest does not match this policy")
        recomputed = self.select(
            tuple(assessment.candidate for assessment in result.assessments),
            requested_limit=result.requested_limit,
        )
        if recomputed != result:
            raise BenefitValidationError("result does not match exact policy revalidation")


__all__ = [
    "ABSTENTION_CODES",
    "ASSESSMENT_TIERS",
    "AVAILABILITY_STATES",
    "MAX_BENEFIT_RESULT_JSON_BYTES",
    "MAX_CANDIDATES",
    "MAX_PPM",
    "MAX_SEARCH_EVALUATIONS",
    "MAX_SELECTED_CAPABILITIES",
    "BenefitCandidate",
    "BenefitSelection",
    "BenefitSelectionResult",
    "BenefitValidationError",
    "CandidateAssessment",
    "EvidenceSummary",
    "NetBenefitPolicy",
    "ResourceCosts",
]
