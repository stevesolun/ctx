"""Host-neutral replay adapter for authenticated schema-v3 capability plans.

The adapter joins four read-only, pinned inputs: graph retrieval, authenticated
benefit facts, exact catalog material descriptors, and exact installation
bundles.  It emits a replay-safe decision surrogate and performs no host or
filesystem effects.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from typing import Protocol, TypeVar

from ctx.engine.benefit import MAX_CANDIDATES, BenefitCandidate
from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor
from ctx.engine.installation import CapabilityInstallBundlePort, InstallPlanningBundle
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import (
    CandidateAuthorityUnavailable,
    CandidateSource,
    CandidateSourceUnavailable,
    CapabilityCandidate,
    PlannerValidationError,
    WorkObservation,
)
from ctx.engine.planning_v3 import (
    AuthenticatedCapabilityCandidate,
    AuthenticatedNetBenefitPlanner,
    CapabilityPlanV3,
    InstallPlanningAuthority,
    LoadPlanningAuthority,
    ManualPlanningAuthority,
    PlanningAuthority,
)
from ctx.engine.replay import PlanningContext, StructuredSurrogate
from ctx.engine.state import EngineState


_CURRENT_WORK_FIELDS = frozenset(
    {
        "signals",
        "languages",
        "baseline_capability_ids",
        "active_capability_ids",
        "rejected_capability_ids",
        "requested_limit",
    }
)
_ELIGIBLE_ACTIONABILITY = frozenset({"load", "install", "manual"})
_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_PLANNING_ENVIRONMENT_SCHEMA = "ctx.planning-environment-v1"
_MAX_PINNED_SNAPSHOT_OUTPUTS = 65_536
_MISSING_SNAPSHOT_OUTPUT = object()
_UNAVAILABLE_SNAPSHOT_OUTPUT = object()
_UNAVAILABLE_AUTHORITY_OUTPUT = object()
_SnapshotValue = TypeVar("_SnapshotValue")


class AuthenticatedBenefitFactsPort(Protocol):
    """Pinned source of policy inputs for one exact retrieved presentation."""

    benefit_facts_snapshot_digest: str

    def benefit_candidate(
        self,
        presentation: CapabilityCandidate,
        observation: WorkObservation,
    ) -> BenefitCandidate | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogLoadPlanningBundle:
    """Typed proof joining one retrieval presentation to catalog material.

    The material authority port, rather than this adapter, supplies the catalog
    identity.  Carrying the exact presentation in the same typed value prevents
    a descriptor for a same-named or differently sourced retrieval row from
    being authorized through unrelated digest comparisons.
    """

    presentation: CapabilityCandidate
    catalog_identity: CatalogCapabilityIdentity
    descriptor: MaterialDescriptor
    catalog_snapshot_digest: str
    material_snapshot_digest: str
    authority_material_snapshot_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.presentation, CapabilityCandidate):
            raise PlannerValidationError("load bundle presentation is invalid")
        if self.presentation.actionability != "load":
            raise PlannerValidationError("load bundle requires a load presentation")
        if not isinstance(self.catalog_identity, CatalogCapabilityIdentity):
            raise PlannerValidationError("load bundle catalog identity is invalid")
        if not isinstance(self.descriptor, MaterialDescriptor):
            raise PlannerValidationError("load bundle material descriptor is invalid")
        catalog_snapshot_digest = _digest(
            self.catalog_snapshot_digest,
            "load bundle catalog_snapshot_digest",
        )
        material_snapshot_digest = _digest(
            self.material_snapshot_digest,
            "load bundle material_snapshot_digest",
        )
        authority_material_snapshot_digest = (
            material_snapshot_digest
            if self.authority_material_snapshot_digest is None
            else _digest(
                self.authority_material_snapshot_digest,
                "load bundle authority_material_snapshot_digest",
            )
        )
        if (
            self.presentation.capability_id,
            self.presentation.kind,
        ) != (
            self.catalog_identity.capability_id,
            self.catalog_identity.kind,
        ):
            raise PlannerValidationError(
                "load bundle catalog identity does not match its exact presentation"
            )
        if (
            self.descriptor.schema_version,
            self.descriptor.capability_id,
            self.descriptor.kind,
            self.descriptor.actionability,
            self.descriptor.provenance_digest,
        ) != (
            2,
            self.presentation.capability_id,
            self.presentation.kind,
            "load",
            authority_material_snapshot_digest,
        ):
            raise PlannerValidationError(
                "load bundle descriptor does not match its presentation or material snapshot"
            )
        object.__setattr__(self, "catalog_snapshot_digest", catalog_snapshot_digest)
        object.__setattr__(self, "material_snapshot_digest", material_snapshot_digest)
        object.__setattr__(
            self,
            "authority_material_snapshot_digest",
            authority_material_snapshot_digest,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityBoundInstallPlanningBundle(InstallPlanningBundle):
    """Install bundle with separate layer and aggregate composition pins."""

    authority_installation_snapshot_digest: str
    installation_snapshot_digest: str

    def __post_init__(self) -> None:
        super(AuthorityBoundInstallPlanningBundle, self).__post_init__()
        authority_digest = _digest(
            self.authority_installation_snapshot_digest,
            "install bundle authority_installation_snapshot_digest",
        )
        aggregate_digest = _digest(
            self.installation_snapshot_digest,
            "install bundle installation_snapshot_digest",
        )
        if self.descriptor.provenance_digest != authority_digest:
            raise PlannerValidationError(
                "install descriptor does not match its authority installation snapshot"
            )
        object.__setattr__(self, "authority_installation_snapshot_digest", authority_digest)
        object.__setattr__(self, "installation_snapshot_digest", aggregate_digest)


class CatalogMaterialAuthorityPort(Protocol):
    """Pinned source of typed, presentation-bound catalog load authority."""

    material_snapshot_digest: str

    def load_bundle(
        self,
        presentation: CapabilityCandidate,
    ) -> CatalogLoadPlanningBundle | None: ...


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise PlannerValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise PlannerValidationError(f"{field_name} must be a canonical safe token")
    return value


def _surrogate_tokens(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise PlannerValidationError(f"current-work {field_name} must be a token array")
    return value


def _candidate_order_key(candidate: CapabilityCandidate) -> tuple[object, ...]:
    return (
        candidate.capability_id,
        candidate.source_digest,
        candidate.actionability,
        -candidate.normalized_score_ppm,
        candidate.equivalence_key or "",
        candidate.matching_signals,
        candidate.reason_codes,
    )


def normalize_candidate_pool(
    value: object,
    observation: WorkObservation,
) -> tuple[CapabilityCandidate, ...]:
    """Return the one canonical pool shared by closure and schema-v3 planning.

    Baseline and rejected identities are excluded before benefit lookup, exact
    duplicate rows collapse, and conflicting presentations under one public
    identity fail locally by being omitted.  Keeping this logic at one boundary
    prevents a query closure from authenticating a different pool than replay.
    """

    if not isinstance(observation, WorkObservation):
        raise TypeError("observation must be a WorkObservation")
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise PlannerValidationError("candidate source must return a bounded sequence")
    try:
        candidates = tuple(islice(iter(value), MAX_CANDIDATES + 1))
    except Exception:
        raise PlannerValidationError("candidate source iteration failed") from None
    if len(candidates) > MAX_CANDIDATES:
        raise PlannerValidationError("candidate source exceeded its candidate pool limit")
    if not all(isinstance(item, CapabilityCandidate) for item in candidates):
        raise PlannerValidationError("candidate source returned an invalid candidate")

    excluded = {
        *observation.baseline_capability_ids,
        *observation.rejected_capability_ids,
    }
    by_identity: dict[str, list[CapabilityCandidate]] = {}
    for candidate in candidates:
        if (
            candidate.capability_id in excluded
            or candidate.actionability not in _ELIGIBLE_ACTIONABILITY
        ):
            continue
        by_identity.setdefault(candidate.capability_id, []).append(candidate)

    eligible: list[CapabilityCandidate] = []
    for capability_id in sorted(by_identity):
        rows = by_identity[capability_id]
        first = rows[0]
        if any(row != first for row in rows[1:]):
            continue
        eligible.append(first)
    return tuple(sorted(eligible, key=_candidate_order_key))


def _canonical_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedReplayDecisionPlannerV3:
    """Adapt authenticated net-benefit planning to the replay decision hook.

    Mutable ports are pinned at construction and checked before every decision.
    A missing or substituted executable authority fails only that candidate: it
    is never converted into an advisory/manual recommendation, and unrelated
    exact authorities remain eligible.
    """

    planner: AuthenticatedNetBenefitPlanner
    source: CandidateSource
    benefit_facts_port: AuthenticatedBenefitFactsPort
    planner_version: str
    catalog_namespace_digest: str
    material_port: CatalogMaterialAuthorityPort | None = None
    install_bundle_port: CapabilityInstallBundlePort | None = None
    _catalog_snapshot_digest: str = field(init=False, repr=False)
    _benefit_facts_snapshot_digest: str = field(init=False, repr=False)
    _material_snapshot_digest: str | None = field(init=False, repr=False)
    _installation_snapshot_digest: str | None = field(init=False, repr=False)
    _policy_digest: str = field(init=False, repr=False)
    _calibration_digest: str = field(init=False, repr=False)
    _planning_environment_digest: str = field(init=False, repr=False)
    _snapshot_outputs: dict[tuple[object, ...], object] = field(
        init=False,
        repr=False,
        compare=False,
        default_factory=dict,
    )
    _snapshot_lock: threading.RLock = field(
        init=False,
        repr=False,
        compare=False,
        default_factory=threading.RLock,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.planner, AuthenticatedNetBenefitPlanner):
            raise PlannerValidationError("planner must be an AuthenticatedNetBenefitPlanner")
        if not callable(getattr(self.source, "retrieve", None)):
            raise PlannerValidationError("source must implement candidate retrieval")
        if not callable(getattr(self.benefit_facts_port, "benefit_candidate", None)):
            raise PlannerValidationError("benefit_facts_port must provide authenticated facts")
        if self.material_port is not None and not callable(
            getattr(self.material_port, "load_bundle", None)
        ):
            raise PlannerValidationError("material_port must provide typed load bundles")
        if self.install_bundle_port is not None and not callable(
            getattr(self.install_bundle_port, "describe_bundle", None)
        ):
            raise PlannerValidationError("install_bundle_port must provide install bundles")

        object.__setattr__(
            self,
            "planner_version",
            _token(self.planner_version, "planner_version"),
        )
        object.__setattr__(
            self,
            "catalog_namespace_digest",
            _digest(self.catalog_namespace_digest, "catalog_namespace_digest"),
        )
        object.__setattr__(
            self,
            "_catalog_snapshot_digest",
            _digest(
                getattr(self.source, "catalog_snapshot_digest", None),
                "candidate source catalog_snapshot_digest",
            ),
        )
        object.__setattr__(
            self,
            "_benefit_facts_snapshot_digest",
            _digest(
                getattr(self.benefit_facts_port, "benefit_facts_snapshot_digest", None),
                "benefit facts snapshot digest",
            ),
        )
        object.__setattr__(
            self,
            "_material_snapshot_digest",
            (
                None
                if self.material_port is None
                else _digest(
                    getattr(self.material_port, "material_snapshot_digest", None),
                    "material snapshot digest",
                )
            ),
        )
        object.__setattr__(
            self,
            "_installation_snapshot_digest",
            (
                None
                if self.install_bundle_port is None
                else _digest(
                    getattr(self.install_bundle_port, "installation_snapshot_digest", None),
                    "installation snapshot digest",
                )
            ),
        )
        object.__setattr__(self, "_policy_digest", self.planner.policy.policy_digest)
        object.__setattr__(self, "_calibration_digest", self.planner.policy.calibration_digest)
        object.__setattr__(
            self,
            "_planning_environment_digest",
            _canonical_digest(
                {
                    "benefit_facts_snapshot_digest": self._benefit_facts_snapshot_digest,
                    "calibration_digest": self._calibration_digest,
                    "catalog_namespace_digest": self.catalog_namespace_digest,
                    "catalog_retrieval_snapshot_digest": self._catalog_snapshot_digest,
                    "installation_snapshot_digest": self._installation_snapshot_digest,
                    "material_snapshot_digest": self._material_snapshot_digest,
                    "planner_version": self.planner_version,
                    "policy_digest": self._policy_digest,
                    "schema": _PLANNING_ENVIRONMENT_SCHEMA,
                }
            ),
        )

    @property
    def catalog_snapshot_digest(self) -> str:
        """Composite planning-environment digest carried by PlanningContext.

        The property retains its legacy name because ``PlanningContext`` has a
        two-field compatibility contract.  Its value now binds every frozen
        input that can affect a schema-v3 decision, not just retrieval.
        """

        return self._planning_environment_digest

    @property
    def catalog_retrieval_snapshot_digest(self) -> str:
        """Raw catalog retrieval snapshot used inside typed authority checks."""

        return self._catalog_snapshot_digest

    def _assert_pins(self) -> None:
        if (
            self.planner.policy.policy_digest,
            self.planner.policy.calibration_digest,
        ) != (
            self._policy_digest,
            self._calibration_digest,
        ):
            raise PlannerValidationError("net-benefit policy or calibration drifted")
        if getattr(self.source, "catalog_snapshot_digest", None) != self._catalog_snapshot_digest:
            raise PlannerValidationError("candidate source catalog snapshot drifted")
        if (
            getattr(self.benefit_facts_port, "benefit_facts_snapshot_digest", None)
            != self._benefit_facts_snapshot_digest
        ):
            raise PlannerValidationError("benefit facts snapshot drifted")
        if self.material_port is not None and (
            getattr(self.material_port, "material_snapshot_digest", None)
            != self._material_snapshot_digest
        ):
            raise PlannerValidationError("material snapshot drifted")
        if self.install_bundle_port is not None and (
            getattr(self.install_bundle_port, "installation_snapshot_digest", None)
            != self._installation_snapshot_digest
        ):
            raise PlannerValidationError("installation snapshot drifted")

    def _remember_snapshot_output(
        self,
        key: tuple[object, ...],
        value: object,
        field_name: str,
    ) -> None:
        previous = self._snapshot_outputs.get(key, _MISSING_SNAPSHOT_OUTPUT)
        if previous is not _MISSING_SNAPSHOT_OUTPUT and previous != value:
            raise PlannerValidationError(
                f"{field_name} output drifted without a snapshot digest change"
            )
        if (
            previous is _MISSING_SNAPSHOT_OUTPUT
            and len(self._snapshot_outputs) >= _MAX_PINNED_SNAPSHOT_OUTPUTS
        ):
            raise PlannerValidationError(
                "pinned snapshot output bound is exhausted; a fresh adapter is required"
            )
        self._snapshot_outputs[key] = value

    def _snapshot_read(
        self,
        *,
        key: tuple[object, ...],
        field_name: str,
        read: Callable[[], _SnapshotValue],
    ) -> _SnapshotValue:
        """Serialize one pinned read and reject digest or same-digest drift."""

        with self._snapshot_lock:
            self._assert_pins()
            try:
                value = read()
            except CandidateAuthorityUnavailable:
                self._assert_pins()
                self._remember_snapshot_output(key, _UNAVAILABLE_AUTHORITY_OUTPUT, field_name)
                raise
            except CandidateSourceUnavailable:
                self._assert_pins()
                self._remember_snapshot_output(key, _UNAVAILABLE_SNAPSHOT_OUTPUT, field_name)
                raise
            except Exception:
                # A port must not be able to hide a concurrent snapshot change
                # behind its own operational or validation failure.
                self._assert_pins()
                raise
            self._assert_pins()
            self._remember_snapshot_output(key, value, field_name)
            return value

    @staticmethod
    def _work_observation(
        observation: StructuredSurrogate,
        state: EngineState | None,
    ) -> WorkObservation:
        if (
            not isinstance(observation, StructuredSurrogate)
            or observation.schema_id != "ctx.observation.current-work"
            or observation.schema_version != 1
        ):
            raise PlannerValidationError("planner requires a current-work observation v1")
        if state is not None and not isinstance(state, EngineState):
            raise PlannerValidationError("planner state must be an EngineState or None")
        value: Mapping[str, object] = observation.value
        if set(value) != _CURRENT_WORK_FIELDS:
            raise PlannerValidationError("current-work observation has missing or unknown fields")
        requested_limit = value["requested_limit"]
        if type(requested_limit) is not int:
            raise PlannerValidationError("current-work requested_limit must be an integer")
        observed_active = _surrogate_tokens(
            value["active_capability_ids"],
            "active_capability_ids",
        )
        active = observed_active if state is None else tuple(sorted(state.active_capability_ids))
        return WorkObservation(
            signals=_surrogate_tokens(value["signals"], "signals"),
            languages=_surrogate_tokens(value["languages"], "languages"),
            baseline_capability_ids=_surrogate_tokens(
                value["baseline_capability_ids"],
                "baseline_capability_ids",
            ),
            active_capability_ids=active,
            rejected_capability_ids=_surrogate_tokens(
                value["rejected_capability_ids"],
                "rejected_capability_ids",
            ),
            requested_limit=requested_limit,
        )

    def _load_authority(
        self,
        presentation: CapabilityCandidate,
    ) -> tuple[CatalogCapabilityIdentity, LoadPlanningAuthority]:
        material_port = self.material_port
        if material_port is None:
            raise CandidateAuthorityUnavailable("load candidate has no authenticated material port")
        bundle = self._snapshot_read(
            key=("load-authority", presentation),
            field_name="load authority",
            read=lambda: material_port.load_bundle(presentation),
        )
        if not isinstance(bundle, CatalogLoadPlanningBundle):
            raise CandidateAuthorityUnavailable(
                "load candidate is missing an exact typed load bundle"
            )
        identity = bundle.catalog_identity
        descriptor = bundle.descriptor
        if (
            bundle.presentation,
            identity.capability_id,
            identity.kind,
            identity.catalog_namespace_digest,
            bundle.catalog_snapshot_digest,
            bundle.material_snapshot_digest,
        ) != (
            presentation,
            presentation.capability_id,
            presentation.kind,
            self.catalog_namespace_digest,
            self._catalog_snapshot_digest,
            self._material_snapshot_digest,
        ):
            raise CandidateAuthorityUnavailable(
                "load bundle does not match the exact retrieval presentation or pinned snapshots"
            )
        try:
            material = AuthorizedMaterial.from_catalog(
                catalog_identity_digest=identity.identity_digest,
                descriptor=descriptor,
            )
            return identity, LoadPlanningAuthority(material=material)
        except (TypeError, ValueError) as exc:
            raise CandidateAuthorityUnavailable("load material authority is invalid") from exc

    def _install_authority(
        self,
        presentation: CapabilityCandidate,
    ) -> InstallPlanningAuthority:
        install_bundle_port = self.install_bundle_port
        if install_bundle_port is None:
            raise CandidateAuthorityUnavailable(
                "install candidate has no authenticated install port"
            )
        bundle = self._snapshot_read(
            key=("install-authority", presentation),
            field_name="install authority",
            read=lambda: install_bundle_port.describe_bundle(
                presentation.capability_id,
                presentation.kind,
            ),
        )
        if not isinstance(bundle, InstallPlanningBundle):
            raise CandidateAuthorityUnavailable(
                "install candidate is missing an exact install bundle"
            )
        descriptor = bundle.descriptor
        result = bundle.result_material
        aggregate_installation_snapshot_digest = self._installation_snapshot_digest
        descriptor_provenance_digest = aggregate_installation_snapshot_digest
        if isinstance(bundle, AuthorityBoundInstallPlanningBundle):
            if bundle.installation_snapshot_digest != aggregate_installation_snapshot_digest:
                raise CandidateAuthorityUnavailable(
                    "install bundle does not match the aggregate installation snapshot"
                )
            descriptor_provenance_digest = bundle.authority_installation_snapshot_digest
        if (
            descriptor.schema_version,
            descriptor.capability_id,
            descriptor.kind,
            descriptor.descriptor_digest,
            descriptor.plan_digest,
            descriptor.provenance_digest,
            result.capability_id,
            result.kind,
        ) != (
            2,
            presentation.capability_id,
            presentation.kind,
            presentation.install_descriptor_digest,
            presentation.install_plan_digest,
            descriptor_provenance_digest,
            presentation.capability_id,
            presentation.kind,
        ):
            raise CandidateAuthorityUnavailable(
                "install bundle does not match presentation or installation snapshot"
            )
        try:
            return InstallPlanningAuthority(
                descriptor=descriptor,
                result_material=result,
            )
        except (TypeError, ValueError) as exc:
            raise CandidateAuthorityUnavailable("install bundle authority is invalid") from exc

    def _authenticated_candidate(
        self,
        presentation: CapabilityCandidate,
        observation: WorkObservation,
    ) -> AuthenticatedCapabilityCandidate:
        facts = self._snapshot_read(
            key=("benefit-facts", presentation, observation),
            field_name="benefit facts",
            read=lambda: self.benefit_facts_port.benefit_candidate(
                presentation,
                observation,
            ),
        )
        if not isinstance(facts, BenefitCandidate):
            raise PlannerValidationError("candidate is missing authenticated benefit facts")
        if (
            facts.capability_id,
            facts.source_digest,
        ) != (
            presentation.capability_id,
            presentation.source_digest,
        ):
            raise PlannerValidationError("benefit facts do not exactly match candidate identity")
        authority: PlanningAuthority
        if presentation.actionability == "load":
            identity, authority = self._load_authority(presentation)
        elif presentation.actionability == "install":
            try:
                identity = CatalogCapabilityIdentity.create(
                    capability_id=presentation.capability_id,
                    kind=presentation.kind,
                    catalog_namespace_digest=self.catalog_namespace_digest,
                )
            except (TypeError, ValueError) as exc:
                raise PlannerValidationError("catalog capability identity is invalid") from exc
            authority = self._install_authority(presentation)
        elif presentation.actionability == "manual":
            try:
                identity = CatalogCapabilityIdentity.create(
                    capability_id=presentation.capability_id,
                    kind=presentation.kind,
                    catalog_namespace_digest=self.catalog_namespace_digest,
                )
            except (TypeError, ValueError) as exc:
                raise PlannerValidationError("catalog capability identity is invalid") from exc
            authority = ManualPlanningAuthority()
        else:  # Eligibility filtering makes this unreachable.
            raise AssertionError("unsupported presentation passed eligibility filtering")
        return AuthenticatedCapabilityCandidate(
            presentation=presentation,
            catalog_identity=identity,
            benefit_candidate=facts,
            authority=authority,
        )

    def _authenticated_candidates(
        self,
        candidates: tuple[CapabilityCandidate, ...],
        observation: WorkObservation,
    ) -> tuple[AuthenticatedCapabilityCandidate, ...]:
        values: list[AuthenticatedCapabilityCandidate] = []
        for candidate in candidates:
            try:
                values.append(self._authenticated_candidate(candidate, observation))
            except CandidateAuthorityUnavailable:
                # Authority failure is local to this candidate.  Never
                # manufacture a manual row, and never discard valid unrelated
                # authority because one bundle is absent or invalid.
                continue
        return tuple(values)

    def _retrieve_candidates(
        self,
        work: WorkObservation,
    ) -> tuple[CapabilityCandidate, ...]:
        retrieved = self._snapshot_read(
            key=("catalog-retrieval", work),
            field_name="catalog retrieval",
            read=lambda: normalize_candidate_pool(self.source.retrieve(work), work),
        )
        return retrieved

    def __call__(
        self,
        observation: StructuredSurrogate,
        state: EngineState | None,
        context: PlanningContext,
    ) -> StructuredSurrogate:
        if not isinstance(context, PlanningContext):
            raise PlannerValidationError("planner requires a frozen planning context")
        if context.planner_version != self.planner_version:
            raise PlannerValidationError("planner version mismatch")
        if context.catalog_snapshot_digest != self._planning_environment_digest:
            raise PlannerValidationError(
                "catalog snapshot does not match the pinned planning environment"
            )
        work = self._work_observation(observation, state)
        with self._snapshot_lock:
            self._assert_pins()
            try:
                candidates = self._retrieve_candidates(work)
            except CandidateSourceUnavailable:
                plan = CapabilityPlanV3.degraded("catalog-unavailable")
                # An unchanged snapshot cannot switch from unavailable to
                # available during one decision.  A second unavailable result
                # is the bounded end validation for this degraded plan.
                try:
                    self._retrieve_candidates(work)
                except CandidateSourceUnavailable:
                    pass
            else:
                authenticated = self._authenticated_candidates(candidates, work)
                self._assert_pins()
                plan = self.planner.plan(
                    authenticated,
                    requested_limit=work.requested_limit,
                )
                self._assert_pins()
                # End validation closes same-digest mutation on the first plan
                # call.  Reads are metadata-only and bounded by MAX_CANDIDATES.
                revalidated_candidates = self._retrieve_candidates(work)
                revalidated_authenticated = self._authenticated_candidates(
                    revalidated_candidates,
                    work,
                )
                if (
                    revalidated_candidates != candidates
                    or revalidated_authenticated != authenticated
                ):
                    raise PlannerValidationError("planning inputs drifted during end validation")
                self._assert_pins()
        return StructuredSurrogate.create(
            schema_id="ctx.decision.capability-plan",
            schema_version=3,
            value=plan.to_mapping(),
        )


__all__ = [
    "AuthenticatedBenefitFactsPort",
    "AuthenticatedReplayDecisionPlannerV3",
    "AuthorityBoundInstallPlanningBundle",
    "CatalogLoadPlanningBundle",
    "CatalogMaterialAuthorityPort",
    "normalize_candidate_pool",
]
