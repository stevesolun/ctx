"""Strict schema-v3 planning values for authenticated net-benefit decisions.

This module is additive.  The legacy score-threshold planner and its schema-v1
and schema-v2 serializers remain in :mod:`ctx.engine.planner`.  Schema v3 only
accepts candidates whose catalog identity, benefit facts, and effect authority
were authenticated before selection.  The graph may retrieve a candidate, but
it cannot manufacture load or install authority.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import InitVar, dataclass
from typing import Any, Protocol, TypeAlias

from ctx.engine.benefit import (
    MAX_CANDIDATES,
    MAX_SEARCH_EVALUATIONS,
    MAX_SELECTED_CAPABILITIES,
    BenefitCandidate,
    BenefitSelection,
    BenefitSelectionResult,
    BenefitValidationError,
    NetBenefitPolicy,
)
from ctx.engine.content import AuthorizedMaterial, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import (
    DEGRADATION_CODES,
    CapabilityCandidate,
    CapabilitySelection,
    PlannerValidationError,
)


_SCHEMA_ID_RE = re.compile(r"\Actx\.[a-z0-9][a-z0-9.-]{0,126}\Z")


@dataclass(frozen=True, slots=True, kw_only=True)
class LoadPlanningAuthority:
    """Exact material that an accepted load selection may later expose."""

    material: AuthorizedMaterial

    def __post_init__(self) -> None:
        if not isinstance(self.material, AuthorizedMaterial):
            raise PlannerValidationError("load authority requires AuthorizedMaterial")

    def to_mapping(self) -> dict[str, object]:
        return {"type": "load", "material": self.material.to_dict()}


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallPlanningAuthority:
    """Exact schema-v2 install descriptor and its typed result material."""

    descriptor: InstallPlanDescriptor
    result_material: MaterialIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, InstallPlanDescriptor):
            raise PlannerValidationError("install authority requires an InstallPlanDescriptor")
        if self.descriptor.schema_version != 2:
            raise PlannerValidationError(
                "install authority requires InstallPlanDescriptor schema v2"
            )
        if not isinstance(self.result_material, MaterialIdentity):
            raise PlannerValidationError(
                "install authority requires an exact result material identity"
            )
        if not self.descriptor.matches_result_material(self.result_material):
            raise PlannerValidationError(
                "install descriptor does not match the exact result material identity"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "type": "install",
            "descriptor": self.descriptor.to_dict(),
            "result_material": self.result_material.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualPlanningAuthority:
    """Empty advisory marker; deliberately carries no effect authority."""

    def to_mapping(self) -> dict[str, str]:
        return {"type": "manual"}


PlanningAuthority: TypeAlias = (
    LoadPlanningAuthority | InstallPlanningAuthority | ManualPlanningAuthority
)


def _validate_identity_and_authority(
    *,
    presentation: CapabilityCandidate,
    catalog_identity: CatalogCapabilityIdentity,
    availability: str,
    authority: PlanningAuthority,
) -> None:
    if (
        presentation.capability_id,
        presentation.kind,
    ) != (
        catalog_identity.capability_id,
        catalog_identity.kind,
    ):
        raise PlannerValidationError("catalog identity must exactly match the presented capability")

    if presentation.actionability == "load":
        if not isinstance(authority, LoadPlanningAuthority):
            raise PlannerValidationError("load presentation requires load authority")
        if availability != "executable":
            raise PlannerValidationError("load authority requires executable benefit facts")
        material = authority.material
        if (
            material.capability_id,
            material.kind,
            material.catalog_identity_digest,
        ) != (
            presentation.capability_id,
            presentation.kind,
            catalog_identity.identity_digest,
        ):
            raise PlannerValidationError(
                "load authority must exactly match capability and catalog identity"
            )
        return

    if presentation.actionability == "install":
        if not isinstance(authority, InstallPlanningAuthority):
            raise PlannerValidationError("install presentation requires install authority")
        if availability != "executable":
            raise PlannerValidationError("install authority requires executable benefit facts")
        install_descriptor = authority.descriptor
        if (
            install_descriptor.capability_id,
            install_descriptor.kind,
            authority.result_material.capability_id,
            authority.result_material.kind,
            install_descriptor.descriptor_digest,
            install_descriptor.plan_digest,
        ) != (
            presentation.capability_id,
            presentation.kind,
            presentation.capability_id,
            presentation.kind,
            presentation.install_descriptor_digest,
            presentation.install_plan_digest,
        ):
            raise PlannerValidationError(
                "install authority must exactly match capability and presentation digests"
            )
        return

    if presentation.actionability == "manual":
        if not isinstance(authority, ManualPlanningAuthority):
            raise PlannerValidationError("manual presentation requires empty manual authority")
        if availability != "advisory":
            raise PlannerValidationError("manual authority is advisory only")
        return

    raise PlannerValidationError(
        "schema-v3 planning accepts only load, install, or manual presentation"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedCapabilityCandidate:
    """One retrieval projection plus authenticated value and effect facts."""

    presentation: CapabilityCandidate
    catalog_identity: CatalogCapabilityIdentity
    benefit_candidate: BenefitCandidate
    authority: PlanningAuthority

    def __post_init__(self) -> None:
        if not isinstance(self.presentation, CapabilityCandidate):
            raise PlannerValidationError("presentation must be a CapabilityCandidate")
        if not isinstance(self.catalog_identity, CatalogCapabilityIdentity):
            raise PlannerValidationError("catalog_identity must be a CatalogCapabilityIdentity")
        if not isinstance(self.benefit_candidate, BenefitCandidate):
            raise PlannerValidationError("benefit_candidate must be a BenefitCandidate")
        if not isinstance(
            self.authority,
            (LoadPlanningAuthority, InstallPlanningAuthority, ManualPlanningAuthority),
        ):
            raise PlannerValidationError("authority is not a declared planning authority")
        if (
            self.presentation.capability_id,
            self.presentation.source_digest,
        ) != (
            self.benefit_candidate.capability_id,
            self.benefit_candidate.source_digest,
        ):
            raise PlannerValidationError(
                "benefit candidate must exactly match presented capability and source"
            )
        _validate_identity_and_authority(
            presentation=self.presentation,
            catalog_identity=self.catalog_identity,
            availability=self.benefit_candidate.availability,
            authority=self.authority,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BenefitAuditReference:
    """Small replay-safe reference to the full result held by an audit store."""

    result_schema_id: str
    result_digest: str
    policy_schema_id: str
    policy_digest: str
    selection_algorithm_id: str
    calibration_digest: str
    requested_limit: int
    candidate_pool_count: int
    search_evaluation_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "result_schema_id",
            "policy_schema_id",
            "selection_algorithm_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _SCHEMA_ID_RE.fullmatch(value) is None:
                raise PlannerValidationError(f"{field_name} must be a declared schema token")
        for field_name in (
            "result_digest",
            "policy_digest",
            "calibration_digest",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise PlannerValidationError(f"{field_name} must be a lowercase SHA-256 digest")
        for field_name, maximum in (
            ("requested_limit", MAX_SELECTED_CAPABILITIES),
            ("candidate_pool_count", MAX_CANDIDATES),
            ("search_evaluation_count", MAX_SEARCH_EVALUATIONS),
        ):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 <= value <= maximum:
                raise PlannerValidationError(f"{field_name} is outside its bounded range")

    @classmethod
    def from_validated_result(
        cls,
        *,
        policy: NetBenefitPolicy,
        result: BenefitSelectionResult,
    ) -> BenefitAuditReference:
        return cls(
            result_schema_id=result.result_schema_id,
            result_digest=result.result_digest,
            policy_schema_id=policy.policy_schema_id,
            policy_digest=result.policy_digest,
            selection_algorithm_id=policy.selection_algorithm_id,
            calibration_digest=policy.calibration_digest,
            requested_limit=result.requested_limit,
            candidate_pool_count=result.candidate_pool_count,
            search_evaluation_count=result.search_evaluation_count,
        )

    def matches(
        self,
        *,
        policy: NetBenefitPolicy,
        result: BenefitSelectionResult,
    ) -> bool:
        return (
            self.result_schema_id,
            self.result_digest,
            self.policy_schema_id,
            self.policy_digest,
            self.selection_algorithm_id,
            self.calibration_digest,
            self.requested_limit,
            self.candidate_pool_count,
            self.search_evaluation_count,
        ) == (
            result.result_schema_id,
            result.result_digest,
            policy.policy_schema_id,
            result.policy_digest,
            policy.selection_algorithm_id,
            policy.calibration_digest,
            result.requested_limit,
            result.candidate_pool_count,
            result.search_evaluation_count,
        ) and result.policy_digest == policy.policy_digest

    def to_mapping(self) -> dict[str, str | int]:
        return {
            "result_schema_id": self.result_schema_id,
            "result_digest": self.result_digest,
            "policy_schema_id": self.policy_schema_id,
            "policy_digest": self.policy_digest,
            "selection_algorithm_id": self.selection_algorithm_id,
            "calibration_digest": self.calibration_digest,
            "requested_limit": self.requested_limit,
            "candidate_pool_count": self.candidate_pool_count,
            "search_evaluation_count": self.search_evaluation_count,
        }


class BenefitAuditStore(Protocol):
    """Durably record a full validated result under its canonical digest."""

    def store(self, result: BenefitSelectionResult) -> str:
        """Return the exact ``result.result_digest`` after recording succeeds."""


class BenefitAuditStoreUnavailable(RuntimeError):
    """The audit result could not be durably stored for an operational reason."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityBenefitProjection:
    """Exact selected benefit fields permitted in one decision row."""

    tier: str
    individual_net_benefit_u: int
    marginal_net_benefit_u: int

    @classmethod
    def from_selection(cls, selection: BenefitSelection) -> CapabilityBenefitProjection:
        if not isinstance(selection, BenefitSelection):
            raise PlannerValidationError("benefit projection requires BenefitSelection")
        return cls(
            tier=selection.tier,
            individual_net_benefit_u=selection.individual_net_benefit_u,
            marginal_net_benefit_u=selection.marginal_net_benefit_u,
        )

    def __post_init__(self) -> None:
        if self.tier not in {"executable", "advisory"}:
            raise PlannerValidationError("benefit tier must be executable or advisory")
        if type(self.individual_net_benefit_u) is not int:
            raise PlannerValidationError("individual net benefit must be an integer")
        if type(self.marginal_net_benefit_u) is not int or self.marginal_net_benefit_u < 1:
            raise PlannerValidationError("marginal net benefit must be positive")

    def to_mapping(self) -> dict[str, str | int]:
        return {
            "tier": self.tier,
            "individual_net_benefit_u": self.individual_net_benefit_u,
            "marginal_net_benefit_u": self.marginal_net_benefit_u,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityPlanSelectionV3:
    """One schema-v3 row with presentation, identity, value, and authority."""

    presentation: CapabilityCandidate
    catalog_identity: CatalogCapabilityIdentity
    benefit: CapabilityBenefitProjection
    authority: PlanningAuthority

    def __post_init__(self) -> None:
        if not isinstance(self.presentation, CapabilityCandidate):
            raise PlannerValidationError("selection presentation is invalid")
        if not isinstance(self.catalog_identity, CatalogCapabilityIdentity):
            raise PlannerValidationError("selection catalog identity is invalid")
        if not isinstance(self.benefit, CapabilityBenefitProjection):
            raise PlannerValidationError("selection benefit projection is invalid")
        if not isinstance(
            self.authority,
            (LoadPlanningAuthority, InstallPlanningAuthority, ManualPlanningAuthority),
        ):
            raise PlannerValidationError("selection authority is invalid")
        _validate_identity_and_authority(
            presentation=self.presentation,
            catalog_identity=self.catalog_identity,
            availability=self.benefit.tier,
            authority=self.authority,
        )

    @classmethod
    def from_candidate_and_selection(
        cls,
        *,
        candidate: AuthenticatedCapabilityCandidate,
        selection: BenefitSelection,
    ) -> CapabilityPlanSelectionV3:
        if (
            candidate.presentation.capability_id,
            candidate.presentation.source_digest,
        ) != (
            selection.capability_id,
            selection.source_digest,
        ):
            raise PlannerValidationError(
                "selected benefit identity does not match authenticated candidate"
            )
        return cls(
            presentation=candidate.presentation,
            catalog_identity=candidate.catalog_identity,
            benefit=CapabilityBenefitProjection.from_selection(selection),
            authority=candidate.authority,
        )

    def selected_identity(self) -> tuple[str, str, str, int, int]:
        return (
            self.presentation.capability_id,
            self.presentation.source_digest,
            self.benefit.tier,
            self.benefit.individual_net_benefit_u,
            self.benefit.marginal_net_benefit_u,
        )

    def to_mapping(self) -> dict[str, Any]:
        # Reuse the frozen v2 presentation projection without modifying it.
        value: dict[str, Any] = CapabilitySelection.from_candidate(self.presentation).to_mapping(
            schema_version=2
        )
        value["catalog_identity"] = self.catalog_identity.to_dict()
        value["benefit"] = self.benefit.to_mapping()
        value["authority"] = self.authority.to_mapping()
        return value


def _result_selection_identity(
    selection: BenefitSelection,
) -> tuple[str, str, str, int, int]:
    return (
        selection.capability_id,
        selection.source_digest,
        selection.tier,
        selection.individual_net_benefit_u,
        selection.marginal_net_benefit_u,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityPlanV3:
    """Strict decision value; the full benefit result is validated then dropped."""

    status: str
    abstention_code: str | None
    benefit_audit: BenefitAuditReference | None
    selections: tuple[CapabilityPlanSelectionV3, ...]
    validated_result: InitVar[BenefitSelectionResult | None]
    validated_policy: InitVar[NetBenefitPolicy | None]

    def __post_init__(
        self,
        validated_result: BenefitSelectionResult | None,
        validated_policy: NetBenefitPolicy | None,
    ) -> None:
        if not isinstance(self.selections, tuple) or not all(
            isinstance(item, CapabilityPlanSelectionV3) for item in self.selections
        ):
            raise PlannerValidationError(
                "schema-v3 selections must be CapabilityPlanSelectionV3 values"
            )
        if len(self.selections) > MAX_SELECTED_CAPABILITIES:
            raise PlannerValidationError("schema-v3 plan exceeds the global five-item bound")
        if self.status == "degraded":
            if (
                self.abstention_code not in DEGRADATION_CODES
                or self.benefit_audit is not None
                or self.selections
                or validated_result is not None
                or validated_policy is not None
            ):
                raise PlannerValidationError(
                    "degraded schema-v3 plan requires no audit, result, or capabilities"
                )
            return
        if self.status not in {"ready", "abstained"}:
            raise PlannerValidationError("schema-v3 plan status is unsupported")
        if not isinstance(self.benefit_audit, BenefitAuditReference):
            raise PlannerValidationError("ready and abstained schema-v3 plans require audit")
        if not isinstance(validated_result, BenefitSelectionResult):
            raise PlannerValidationError(
                "ready and abstained schema-v3 plans require a validated result"
            )
        if not isinstance(validated_policy, NetBenefitPolicy):
            raise PlannerValidationError(
                "ready and abstained schema-v3 plans require a validated policy"
            )
        if not self.benefit_audit.matches(
            policy=validated_policy,
            result=validated_result,
        ):
            raise PlannerValidationError("benefit audit does not match validated result")
        expected_rows = tuple(
            _result_selection_identity(item) for item in validated_result.selections
        )
        actual_rows = tuple(item.selected_identity() for item in self.selections)
        if actual_rows != expected_rows:
            raise PlannerValidationError(
                "capability rows do not exactly project the validated benefit result"
            )
        if self.selections:
            if self.status != "ready" or self.abstention_code is not None:
                raise PlannerValidationError(
                    "selected schema-v3 plan must be ready without abstention"
                )
        elif self.status != "abstained" or self.abstention_code != validated_result.abstention_code:
            raise PlannerValidationError(
                "empty schema-v3 plan must preserve the benefit abstention code"
            )

    @classmethod
    def degraded(cls, code: str) -> CapabilityPlanV3:
        return cls(
            status="degraded",
            abstention_code=code,
            benefit_audit=None,
            selections=(),
            validated_result=None,
            validated_policy=None,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "abstention_code": self.abstention_code,
            "benefit_audit": (
                None if self.benefit_audit is None else self.benefit_audit.to_mapping()
            ),
            "capabilities": [item.to_mapping() for item in self.selections],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedNetBenefitPlanner:
    """Schema-v3 orchestrator over authenticated facts and a frozen policy.

    This name is intentionally distinct from ``BoundedCapabilityPlanner``.  The
    latter remains the legacy schema-v1/v2 score planner until runtime migration
    is complete.
    """

    policy: NetBenefitPolicy
    audit_store: BenefitAuditStore

    def __post_init__(self) -> None:
        if not isinstance(self.policy, NetBenefitPolicy):
            raise PlannerValidationError("policy must be a NetBenefitPolicy")
        if not callable(getattr(self.audit_store, "store", None)):
            raise PlannerValidationError("audit_store must implement result storage")

    def plan(
        self,
        candidates: Sequence[AuthenticatedCapabilityCandidate],
        *,
        requested_limit: int = MAX_SELECTED_CAPABILITIES,
    ) -> CapabilityPlanV3:
        if isinstance(candidates, (str, bytes, bytearray)) or not isinstance(candidates, Sequence):
            raise TypeError("candidates must be a bounded sequence")
        if len(candidates) > MAX_CANDIDATES:
            raise PlannerValidationError("candidate pool exceeds its bounded limit")
        if (
            type(requested_limit) is not int
            or not 0 <= requested_limit <= MAX_SELECTED_CAPABILITIES
        ):
            raise PlannerValidationError(
                "requested_limit must be an integer from zero through five"
            )
        values = tuple(candidates)
        if not all(isinstance(item, AuthenticatedCapabilityCandidate) for item in values):
            raise PlannerValidationError("candidate pool contains unauthenticated planning facts")
        identities = tuple(item.presentation.capability_id for item in values)
        if len(set(identities)) != len(identities):
            raise PlannerValidationError("candidate pool contains duplicate identities")

        try:
            result = self.policy.select(
                tuple(item.benefit_candidate for item in values),
                requested_limit=requested_limit,
            )
            self.policy.validate_result(result)
        except (BenefitValidationError, TypeError, ValueError) as exc:
            raise PlannerValidationError(
                "net-benefit policy rejected the authenticated candidate pool"
            ) from exc

        by_identity = {item.presentation.capability_id: item for item in values}
        selections = tuple(
            CapabilityPlanSelectionV3.from_candidate_and_selection(
                candidate=by_identity[item.capability_id],
                selection=item,
            )
            for item in result.selections
        )
        try:
            stored_digest = self.audit_store.store(result)
        except BenefitAuditStoreUnavailable:
            return CapabilityPlanV3.degraded("planner-failed")
        if stored_digest != result.result_digest:
            raise PlannerValidationError(
                "audit store acknowledged a different benefit result digest"
            )

        audit = BenefitAuditReference.from_validated_result(
            policy=self.policy,
            result=result,
        )
        return CapabilityPlanV3(
            status="ready" if selections else "abstained",
            abstention_code=None if selections else result.abstention_code,
            benefit_audit=audit,
            selections=selections,
            validated_result=result,
            validated_policy=self.policy,
        )


__all__ = [
    "AuthenticatedCapabilityCandidate",
    "AuthenticatedNetBenefitPlanner",
    "BenefitAuditReference",
    "BenefitAuditStore",
    "BenefitAuditStoreUnavailable",
    "CapabilityBenefitProjection",
    "CapabilityPlanSelectionV3",
    "CapabilityPlanV3",
    "InstallPlanningAuthority",
    "LoadPlanningAuthority",
    "ManualPlanningAuthority",
    "PlanningAuthority",
]
