"""Reviewed benefit profiles and exact query-scoped planning closure.

This module deliberately separates three authorities:

* a catalog source may prove which capability presentation exists;
* a reviewed profile may declare bounded benefit, trust, and resource facts;
* installation consent independently decides whether an accepted install action
  may execute.

The closure consumes an already eligibility-filtered source, freezes at most
512 exact presentations for one :class:`WorkObservation`, and produces the
existing authenticated facts contract.  It never interprets catalog prose or
installation commands as benefit or execution authority.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Final, Protocol

from ctx.engine.benefit import (
    MAX_CANDIDATES,
    MAX_PPM,
    BenefitCandidate,
    EvidenceSummary,
    NetBenefitPolicy,
    ResourceCosts,
)
from ctx.engine.capability_schema import (
    MAX_MATCHING_SIGNALS,
    PRESENTED_ACTIONABILITY_STATES,
    validate_capability_identity,
)
from ctx.engine.planner import CapabilityCandidate, PlannerValidationError, WorkObservation
from ctx.runtime.authenticated_benefit import (
    AuthenticatedBenefitFacts,
    AuthenticatedBenefitManifestError,
    _canonical_bytes,
    _decode_authenticated_manifest_bytes,
    _read_authenticated_manifest,
    capability_presentation_digest,
    capability_presentation_mapping,
    load_authenticated_benefit_facts_bytes,
)
from ctx.runtime.planning_v3 import normalize_candidate_pool


REVIEWED_BENEFIT_PROFILES_SCHEMA: Final = "ctx.reviewed-benefit-profiles-v2"
QUERY_BENEFIT_CLOSURE_SCHEMA: Final = "ctx.query-benefit-closure-v2"
MAX_REVIEWED_BENEFIT_PROFILES: Final = 4_096
MAX_REVIEWED_PROFILE_AUTHORITIES: Final = 64
MAX_PROFILE_SIGNALS: Final = 64
_QUERY_CLOSURE_FACTORY_TOKEN: Final = object()

_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._@-]{0,127}\Z")
_AUTHORITY_KINDS = frozenset({"ctx-release", "organization", "user"})
_CATALOG_LAYER_KINDS = frozenset({"ctx", "organization", "user"})
_ROOT_FIELDS = frozenset({"authority", "bindings", "profiles", "schema"})
_AUTHORITY_FIELDS = frozenset(
    {"authority_digest", "authority_id", "authority_kind", "catalog_layer_kind", "sequence"}
)
_BINDING_FIELDS = frozenset(
    {
        "calibration_digest",
        "candidate_projection_version",
        "catalog_artifact_sha256",
        "catalog_namespace_digest",
        "catalog_provenance_digest",
        "catalog_retrieval_snapshot_digest",
        "installation_snapshot_digest",
        "material_snapshot_digest",
        "policy_digest",
    }
)
_PROFILE_FIELDS = frozenset(
    {
        "actionability",
        "capability_id",
        "catalog_entry_claim_digest",
        "complements",
        "conflicts",
        "costs",
        "coverage_keys",
        "expected_task_benefit_ppm",
        "kind",
        "match_policy",
        "maximum_relevance_ppm",
        "name",
        "profile_id",
        "review_basis_digest",
        "security_approved",
        "source_trusted",
        "trust_ppm",
    }
)
_MATCH_POLICY_FIELDS = frozenset(
    {
        "allowed_equivalence_keys",
        "allowed_reason_codes",
        "allowed_signals",
        "minimum_matching_signals",
        "minimum_non_language_matching_signals",
        "required_any_signals",
    }
)
_COST_FIELDS = frozenset(ResourceCosts._FIELDS)


class BenefitClosureError(ValueError):
    """A reviewed profile or query closure violated its closed contract."""


def _fail(message: str) -> BenefitClosureError:
    return BenefitClosureError(message)


def _closed(value: object, fields: frozenset[str], field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _fail(f"{field_name} must contain exactly its declared fields")
    return value


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise _fail(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _optional_digest(value: object, field_name: str) -> str | None:
    return None if value is None else _digest(value, field_name)


def _token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise _fail(f"{field_name} must be a canonical safe token")
    return value


def _integer(value: object, field_name: str, *, minimum: int = 0, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _fail(f"{field_name} must be an integer from {minimum} through {maximum}")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise _fail(f"{field_name} must be a boolean")
    return value


def _tokens(
    value: object, field_name: str, *, maximum: int = MAX_PROFILE_SIGNALS
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise _fail(f"{field_name} must be a bounded token array")
    result = tuple(_token(item, f"{field_name} item") for item in value)
    if result != tuple(sorted(result)) or len(set(result)) != len(result):
        raise _fail(f"{field_name} must be sorted and unique")
    return result


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedMatchPolicy:
    """Signals a profile reviewer explicitly allowed to support one profile."""

    allowed_signals: tuple[str, ...]
    allowed_reason_codes: tuple[str, ...]
    allowed_equivalence_keys: tuple[str, ...]
    required_any_signals: tuple[str, ...]
    minimum_matching_signals: int
    minimum_non_language_matching_signals: int

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, tuple)
            for value in (
                self.allowed_signals,
                self.allowed_reason_codes,
                self.allowed_equivalence_keys,
                self.required_any_signals,
            )
        ):
            raise _fail("reviewed match signals must be immutable tuples")
        if (
            len(self.allowed_signals) > MAX_PROFILE_SIGNALS
            or self.allowed_signals != tuple(sorted(set(self.allowed_signals)))
            or any(_TOKEN_RE.fullmatch(value) is None for value in self.allowed_signals)
        ):
            raise _fail("allowed_signals must be bounded, canonical, sorted, and unique")
        if self.required_any_signals != tuple(sorted(set(self.required_any_signals))) or not set(
            self.required_any_signals
        ).issubset(self.allowed_signals):
            raise _fail("required_any_signals must be a canonical subset of allowed_signals")
        _integer(
            self.minimum_matching_signals,
            "minimum_matching_signals",
            minimum=1,
            maximum=MAX_MATCHING_SIGNALS,
        )
        _integer(
            self.minimum_non_language_matching_signals,
            "minimum_non_language_matching_signals",
            maximum=MAX_MATCHING_SIGNALS,
        )
        if self.minimum_matching_signals > len(self.allowed_signals):
            raise _fail("minimum_matching_signals exceeds the reviewed signal set")
        for field_name in ("allowed_reason_codes", "allowed_equivalence_keys"):
            value = getattr(self, field_name)
            if (
                len(value) > MAX_PROFILE_SIGNALS
                or value != tuple(sorted(set(value)))
                or any(_TOKEN_RE.fullmatch(item) is None for item in value)
            ):
                raise _fail(f"{field_name} must be bounded, canonical, sorted, and unique")

    def accepts(self, presentation: CapabilityCandidate, observation: WorkObservation) -> bool:
        matches = set(presentation.matching_signals)
        observed = {*observation.signals, *observation.languages}
        if not matches.issubset(self.allowed_signals) or not matches.issubset(observed):
            return False
        if not set(presentation.reason_codes).issubset(self.allowed_reason_codes):
            return False
        if (
            presentation.equivalence_key is not None
            and presentation.equivalence_key not in self.allowed_equivalence_keys
        ):
            return False
        non_language = matches - set(observation.languages)
        return (
            len(matches) >= self.minimum_matching_signals
            and len(non_language) >= self.minimum_non_language_matching_signals
            and (
                not self.required_any_signals
                or bool(matches.intersection(self.required_any_signals))
            )
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedBenefitProfile:
    """One exact catalog-entry review with no catalog prose or commands."""

    profile_id: str
    capability_id: str
    kind: str
    name: str
    actionability: str
    catalog_entry_claim_digest: str
    review_basis_digest: str
    match_policy: ReviewedMatchPolicy
    expected_task_benefit_ppm: int
    maximum_relevance_ppm: int
    trust_ppm: int
    costs: ResourceCosts
    source_trusted: bool
    security_approved: bool
    coverage_keys: tuple[str, ...]
    complements: tuple[str, ...]
    conflicts: tuple[str, ...]
    profile_digest: str

    def __post_init__(self) -> None:
        _token(self.profile_id, "profile_id")
        try:
            capability_id, kind = validate_capability_identity(self.capability_id, self.kind)
        except ValueError as exc:
            raise _fail("profile capability identity is invalid") from exc
        if not isinstance(self.name, str) or _NAME_RE.fullmatch(self.name) is None:
            raise _fail("profile name must be a canonical capability name")
        if capability_id != f"{kind}:{self.name}":
            raise _fail("profile name does not match capability identity")
        if self.actionability not in PRESENTED_ACTIONABILITY_STATES:
            raise _fail("profile actionability is unsupported")
        _digest(self.catalog_entry_claim_digest, "catalog_entry_claim_digest")
        _digest(self.review_basis_digest, "review_basis_digest")
        if not isinstance(self.match_policy, ReviewedMatchPolicy):
            raise _fail("profile match policy is invalid")
        for field_name in (
            "expected_task_benefit_ppm",
            "maximum_relevance_ppm",
            "trust_ppm",
        ):
            _integer(getattr(self, field_name), field_name, maximum=MAX_PPM)
        if not isinstance(self.costs, ResourceCosts):
            raise _fail("profile costs must be ResourceCosts")
        for field_name in (
            "source_trusted",
            "security_approved",
        ):
            _boolean(getattr(self, field_name), field_name)
        for field_name in ("coverage_keys", "complements", "conflicts"):
            value = getattr(self, field_name)
            if not isinstance(value, tuple) or value != tuple(sorted(set(value))):
                raise _fail(f"{field_name} must be an immutable canonical token tuple")
            for item in value:
                _token(item, f"{field_name} item")
        if self.capability_id in {*self.complements, *self.conflicts}:
            raise _fail("profile cannot relate a capability to itself")
        if set(self.complements).intersection(self.conflicts):
            raise _fail("profile peer cannot be both complement and conflict")
        _digest(self.profile_digest, "profile_digest")
        if self.profile_digest != _canonical_digest(_reviewed_profile_mapping(self)):
            raise _fail("profile_digest does not match the reviewed profile")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedBenefitProfiles:
    """Exact-hash reviewed profile set scoped to one catalog authority."""

    authority_id: str
    authority_kind: str
    authority_digest: str
    catalog_layer_kind: str
    sequence: int
    candidate_projection_version: str
    catalog_namespace_digest: str
    catalog_provenance_digest: str
    catalog_artifact_sha256: str
    catalog_retrieval_snapshot_digest: str
    material_snapshot_digest: str | None
    installation_snapshot_digest: str | None
    calibration_digest: str
    policy_digest: str
    profiles: tuple[ReviewedBenefitProfile, ...]
    profile_snapshot_digest: str

    def __post_init__(self) -> None:
        _token(self.authority_id, "authority_id")
        if self.authority_kind not in _AUTHORITY_KINDS:
            raise _fail("authority_kind is unsupported")
        if self.catalog_layer_kind not in _CATALOG_LAYER_KINDS:
            raise _fail("catalog_layer_kind is unsupported")
        expected_layer = "ctx" if self.authority_kind == "ctx-release" else self.authority_kind
        if self.catalog_layer_kind != expected_layer:
            raise _fail("benefit authority cannot cross catalog layer kind")
        _digest(self.authority_digest, "authority_digest")
        _integer(self.sequence, "sequence", minimum=1, maximum=2**63 - 1)
        _token(self.candidate_projection_version, "candidate_projection_version")
        for field_name in (
            "catalog_namespace_digest",
            "catalog_provenance_digest",
            "catalog_artifact_sha256",
            "catalog_retrieval_snapshot_digest",
            "calibration_digest",
            "policy_digest",
            "profile_snapshot_digest",
        ):
            _digest(getattr(self, field_name), field_name)
        _optional_digest(self.material_snapshot_digest, "material_snapshot_digest")
        _optional_digest(self.installation_snapshot_digest, "installation_snapshot_digest")
        if (
            not isinstance(self.profiles, tuple)
            or len(self.profiles) > MAX_REVIEWED_BENEFIT_PROFILES
        ):
            raise _fail("profiles must be an immutable bounded tuple")
        order = tuple(
            (profile.capability_id, profile.actionability, profile.profile_id)
            for profile in self.profiles
        )
        if order != tuple(sorted(order)) or len(set(order)) != len(order):
            raise _fail("profiles must use unique canonical order")
        keys = tuple((profile.capability_id, profile.actionability) for profile in self.profiles)
        if len(set(keys)) != len(keys):
            raise _fail("profiles contain duplicate capability actionability")
        profile_ids = {profile.capability_id for profile in self.profiles}
        if any(
            peer not in profile_ids
            for profile in self.profiles
            for peer in (*profile.complements, *profile.conflicts)
        ):
            raise _fail("profile relationship points outside the reviewed profile set")
        if self.profile_snapshot_digest != _canonical_digest(
            _reviewed_profiles_manifest_mapping(self)
        ):
            raise _fail("profile_snapshot_digest does not match the reviewed manifest")

    def profile_for(
        self,
        presentation: CapabilityCandidate,
        catalog_entry_claim_digest: str,
    ) -> ReviewedBenefitProfile | None:
        for profile in self.profiles:
            if (
                profile.capability_id,
                profile.actionability,
                profile.catalog_entry_claim_digest,
            ) == (
                presentation.capability_id,
                presentation.actionability,
                catalog_entry_claim_digest,
            ):
                return profile
        return None


def _match_policy(value: object, index: int) -> ReviewedMatchPolicy:
    mapping = _closed(value, _MATCH_POLICY_FIELDS, f"profiles[{index}].match_policy")
    return ReviewedMatchPolicy(
        allowed_signals=_tokens(mapping["allowed_signals"], "allowed_signals"),
        allowed_reason_codes=_tokens(
            mapping["allowed_reason_codes"],
            "allowed_reason_codes",
        ),
        allowed_equivalence_keys=_tokens(
            mapping["allowed_equivalence_keys"],
            "allowed_equivalence_keys",
        ),
        required_any_signals=_tokens(mapping["required_any_signals"], "required_any_signals"),
        minimum_matching_signals=_integer(
            mapping["minimum_matching_signals"],
            "minimum_matching_signals",
            minimum=1,
            maximum=MAX_MATCHING_SIGNALS,
        ),
        minimum_non_language_matching_signals=_integer(
            mapping["minimum_non_language_matching_signals"],
            "minimum_non_language_matching_signals",
            maximum=MAX_MATCHING_SIGNALS,
        ),
    )


def _costs(value: object, index: int) -> ResourceCosts:
    mapping = _closed(value, _COST_FIELDS, f"profiles[{index}].costs")
    try:
        return ResourceCosts(
            context_tokens=mapping["context_tokens"],  # type: ignore[arg-type]
            tool_schema_tokens=mapping["tool_schema_tokens"],  # type: ignore[arg-type]
            runtime_millis=mapping["runtime_millis"],  # type: ignore[arg-type]
            permission_burden_units=mapping["permission_burden_units"],  # type: ignore[arg-type]
            credential_burden_units=mapping["credential_burden_units"],  # type: ignore[arg-type]
            approval_prompts=mapping["approval_prompts"],  # type: ignore[arg-type]
            process_units=mapping["process_units"],  # type: ignore[arg-type]
            child_agent_units=mapping["child_agent_units"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise _fail(f"profiles[{index}].costs is invalid") from exc


def _profile_mapping_for_digest(
    mapping: Mapping[str, object],
) -> dict[str, object]:
    return {key: mapping[key] for key in sorted(mapping)}


def _profile(value: object, index: int) -> ReviewedBenefitProfile:
    mapping = _closed(value, _PROFILE_FIELDS, f"profiles[{index}]")
    profile_digest = _canonical_digest(_profile_mapping_for_digest(mapping))
    try:
        return ReviewedBenefitProfile(
            profile_id=_token(mapping["profile_id"], "profile_id"),
            capability_id=mapping["capability_id"],  # type: ignore[arg-type]
            kind=mapping["kind"],  # type: ignore[arg-type]
            name=mapping["name"],  # type: ignore[arg-type]
            actionability=mapping["actionability"],  # type: ignore[arg-type]
            catalog_entry_claim_digest=_digest(
                mapping["catalog_entry_claim_digest"],
                "catalog_entry_claim_digest",
            ),
            review_basis_digest=_digest(mapping["review_basis_digest"], "review_basis_digest"),
            match_policy=_match_policy(mapping["match_policy"], index),
            expected_task_benefit_ppm=_integer(
                mapping["expected_task_benefit_ppm"],
                "expected_task_benefit_ppm",
                maximum=MAX_PPM,
            ),
            maximum_relevance_ppm=_integer(
                mapping["maximum_relevance_ppm"],
                "maximum_relevance_ppm",
                maximum=MAX_PPM,
            ),
            trust_ppm=_integer(mapping["trust_ppm"], "trust_ppm", maximum=MAX_PPM),
            costs=_costs(mapping["costs"], index),
            source_trusted=_boolean(mapping["source_trusted"], "source_trusted"),
            security_approved=_boolean(mapping["security_approved"], "security_approved"),
            coverage_keys=_tokens(mapping["coverage_keys"], "coverage_keys"),
            complements=_tokens(mapping["complements"], "complements"),
            conflicts=_tokens(mapping["conflicts"], "conflicts"),
            profile_digest=profile_digest,
        )
    except (BenefitClosureError, TypeError, ValueError) as exc:
        if isinstance(exc, BenefitClosureError):
            raise
        raise _fail(f"profiles[{index}] is invalid") from exc


def _profiles_from_manifest(
    value: Mapping[str, object],
    expected_sha256: str,
) -> ReviewedBenefitProfiles:
    root = _closed(value, _ROOT_FIELDS, "reviewed benefit profile manifest")
    if root["schema"] != REVIEWED_BENEFIT_PROFILES_SCHEMA:
        raise _fail("reviewed benefit profile schema is unsupported")
    authority = _closed(root["authority"], _AUTHORITY_FIELDS, "profile authority")
    bindings = _closed(root["bindings"], _BINDING_FIELDS, "profile catalog bindings")
    raw_profiles = root["profiles"]
    if not isinstance(raw_profiles, list) or len(raw_profiles) > MAX_REVIEWED_BENEFIT_PROFILES:
        raise _fail("profiles must be a bounded array")
    parsed = tuple(_profile(item, index) for index, item in enumerate(raw_profiles))
    try:
        return ReviewedBenefitProfiles(
            authority_id=_token(authority["authority_id"], "authority_id"),
            authority_kind=authority["authority_kind"],  # type: ignore[arg-type]
            authority_digest=_digest(authority["authority_digest"], "authority_digest"),
            catalog_layer_kind=authority["catalog_layer_kind"],  # type: ignore[arg-type]
            sequence=_integer(
                authority["sequence"],
                "sequence",
                minimum=1,
                maximum=2**63 - 1,
            ),
            candidate_projection_version=_token(
                bindings["candidate_projection_version"],
                "candidate_projection_version",
            ),
            catalog_namespace_digest=_digest(
                bindings["catalog_namespace_digest"],
                "catalog_namespace_digest",
            ),
            catalog_provenance_digest=_digest(
                bindings["catalog_provenance_digest"],
                "catalog_provenance_digest",
            ),
            catalog_artifact_sha256=_digest(
                bindings["catalog_artifact_sha256"],
                "catalog_artifact_sha256",
            ),
            catalog_retrieval_snapshot_digest=_digest(
                bindings["catalog_retrieval_snapshot_digest"],
                "catalog_retrieval_snapshot_digest",
            ),
            material_snapshot_digest=_optional_digest(
                bindings["material_snapshot_digest"],
                "material_snapshot_digest",
            ),
            installation_snapshot_digest=_optional_digest(
                bindings["installation_snapshot_digest"],
                "installation_snapshot_digest",
            ),
            calibration_digest=_digest(bindings["calibration_digest"], "calibration_digest"),
            policy_digest=_digest(bindings["policy_digest"], "policy_digest"),
            profiles=parsed,
            profile_snapshot_digest=_digest(expected_sha256, "profile_snapshot_digest"),
        )
    except (BenefitClosureError, TypeError, ValueError) as exc:
        if isinstance(exc, BenefitClosureError):
            raise
        raise _fail("reviewed benefit profile manifest is invalid") from exc


def load_reviewed_benefit_profiles(
    path: Path,
    expected_sha256: str,
) -> ReviewedBenefitProfiles:
    """Securely load one exact-hash reviewed profile file."""

    try:
        return _profiles_from_manifest(
            _read_authenticated_manifest(path, expected_sha256),
            expected_sha256,
        )
    except AuthenticatedBenefitManifestError:
        raise _fail("reviewed benefit profile authentication failed") from None


def load_reviewed_benefit_profiles_bytes(
    value: bytes,
    expected_sha256: str,
) -> ReviewedBenefitProfiles:
    """Load a release-pinned or otherwise already-owned canonical profile body."""

    try:
        decoded = _decode_authenticated_manifest_bytes(value, expected_sha256)
        return _profiles_from_manifest(
            decoded,
            hashlib.sha256(_canonical_bytes(decoded)).hexdigest(),
        )
    except AuthenticatedBenefitManifestError:
        raise _fail("reviewed benefit profile authentication failed") from None


def _aggregate_digest(schema: str, field_name: str, values: Sequence[object]) -> str:
    return _canonical_digest({"field": field_name, "schema": schema, "values": list(values)})


def _aggregate_optional_digest(
    authorities: Sequence[ReviewedBenefitProfiles], field_name: str
) -> str | None:
    values = [getattr(authority, field_name) for authority in authorities]
    if all(value is None for value in values):
        return None
    return _aggregate_digest("ctx.reviewed-authority-bindings-v1", field_name, values)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedBenefitAuthorities:
    """Frozen multi-layer collection of independently authenticated reviews."""

    authorities: tuple[ReviewedBenefitProfiles, ...]
    authority_snapshot_digest: str = ""
    profile_snapshot_digest: str = ""
    candidate_projection_version: str = ""
    calibration_digest: str = ""
    policy_digest: str = ""
    catalog_namespace_digest: str = ""
    catalog_provenance_digest: str = ""
    catalog_artifact_sha256: str = ""
    catalog_retrieval_snapshot_digest: str = ""
    material_snapshot_digest: str | None = None
    installation_snapshot_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authorities, tuple)
            or not 1 <= len(self.authorities) <= MAX_REVIEWED_PROFILE_AUTHORITIES
            or not all(isinstance(value, ReviewedBenefitProfiles) for value in self.authorities)
        ):
            raise _fail("reviewed authorities must be a bounded immutable tuple")
        order = tuple(
            (
                value.catalog_layer_kind,
                value.authority_kind,
                value.authority_id,
                value.sequence,
                value.authority_digest,
                value.profile_snapshot_digest,
            )
            for value in self.authorities
        )
        if order != tuple(sorted(order)):
            raise _fail("reviewed authorities must use canonical order")
        scopes = tuple(value[:3] for value in order)
        if len(scopes) != len(set(scopes)):
            raise _fail("reviewed authorities contain a duplicate authority scope")
        if sum(len(value.profiles) for value in self.authorities) > MAX_REVIEWED_BENEFIT_PROFILES:
            raise _fail("reviewed authority collection exceeds its profile bound")
        for field_name in (
            "candidate_projection_version",
            "calibration_digest",
            "policy_digest",
        ):
            values = {getattr(authority, field_name) for authority in self.authorities}
            if len(values) != 1:
                raise _fail(f"reviewed authorities disagree on {field_name}")
            object.__setattr__(self, field_name, values.pop())
        snapshots = [
            {
                "authority_digest": authority.authority_digest,
                "authority_id": authority.authority_id,
                "authority_kind": authority.authority_kind,
                "catalog_layer_kind": authority.catalog_layer_kind,
                "profile_snapshot_digest": authority.profile_snapshot_digest,
                "sequence": authority.sequence,
            }
            for authority in self.authorities
        ]
        object.__setattr__(
            self,
            "authority_snapshot_digest",
            _canonical_digest({"authorities": snapshots, "schema": "ctx.review-authorities-v1"}),
        )
        object.__setattr__(
            self,
            "profile_snapshot_digest",
            _aggregate_digest(
                "ctx.reviewed-profile-collection-v1",
                "profile_snapshot_digest",
                [value.profile_snapshot_digest for value in self.authorities],
            ),
        )
        for field_name in (
            "catalog_namespace_digest",
            "catalog_provenance_digest",
            "catalog_artifact_sha256",
            "catalog_retrieval_snapshot_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _aggregate_digest(
                    "ctx.reviewed-authority-bindings-v1",
                    field_name,
                    [getattr(value, field_name) for value in self.authorities],
                ),
            )
        object.__setattr__(
            self,
            "material_snapshot_digest",
            _aggregate_optional_digest(self.authorities, "material_snapshot_digest"),
        )
        object.__setattr__(
            self,
            "installation_snapshot_digest",
            _aggregate_optional_digest(self.authorities, "installation_snapshot_digest"),
        )

    @classmethod
    def create(cls, authorities: Sequence[ReviewedBenefitProfiles]) -> ReviewedBenefitAuthorities:
        if isinstance(authorities, (str, bytes, bytearray)) or not isinstance(
            authorities, Sequence
        ):
            raise TypeError("authorities must be a bounded sequence")
        try:
            bounded = tuple(islice(iter(authorities), MAX_REVIEWED_PROFILE_AUTHORITIES + 1))
        except Exception:
            raise TypeError("authorities could not be read as a bounded sequence") from None
        if len(bounded) > MAX_REVIEWED_PROFILE_AUTHORITIES:
            raise _fail("reviewed authorities exceed their bounded item limit")
        if not all(isinstance(value, ReviewedBenefitProfiles) for value in bounded):
            raise TypeError("authorities must contain ReviewedBenefitProfiles values")
        ordered = tuple(
            sorted(
                bounded,
                key=lambda value: (
                    value.catalog_layer_kind,
                    value.authority_kind,
                    value.authority_id,
                    value.sequence,
                    value.authority_digest,
                    value.profile_snapshot_digest,
                ),
            )
        )
        return cls(authorities=ordered)

    def profile_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> tuple[ReviewedBenefitProfiles, ReviewedBenefitProfile] | None:
        for authority in self.authorities:
            if not claim.matches(authority):
                continue
            profile = authority.profile_for(
                presentation,
                claim.catalog_entry_claim_digest,
            )
            if profile is not None:
                return authority, profile
        return None


def catalog_candidate_entry_claim_digest(presentation: CapabilityCandidate) -> str:
    """Bind the catalog-stable identity, source, actionability, and install plan."""

    if not isinstance(presentation, CapabilityCandidate):
        raise TypeError("presentation must be a CapabilityCandidate")
    return _canonical_digest(
        {
            "actionability": presentation.actionability,
            "capability_id": presentation.capability_id,
            "equivalence_key": presentation.equivalence_key,
            "install_descriptor_digest": presentation.install_descriptor_digest,
            "install_plan_digest": presentation.install_plan_digest,
            "kind": presentation.kind,
            "name": presentation.name,
            "schema": "ctx.catalog-candidate-entry-claim-v1",
            "source_digest": presentation.source_digest,
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class EligibleCatalogClaim:
    """Authority-qualified catalog claim for one retrieved presentation."""

    catalog_entry_claim_digest: str
    presentation_digest: str
    authority_id: str
    authority_kind: str
    authority_digest: str
    catalog_layer_kind: str
    sequence: int
    candidate_projection_version: str
    catalog_namespace_digest: str
    catalog_provenance_digest: str
    catalog_artifact_sha256: str
    catalog_retrieval_snapshot_digest: str
    material_snapshot_digest: str | None
    installation_snapshot_digest: str | None
    profile_snapshot_digest: str
    calibration_digest: str
    policy_digest: str

    def __post_init__(self) -> None:
        _token(self.authority_id, "claim authority_id")
        if self.authority_kind not in _AUTHORITY_KINDS:
            raise _fail("claim authority_kind is unsupported")
        expected_layer = "ctx" if self.authority_kind == "ctx-release" else self.authority_kind
        if self.catalog_layer_kind != expected_layer:
            raise _fail("claim authority cannot cross catalog layer kind")
        _integer(self.sequence, "claim sequence", minimum=1, maximum=2**63 - 1)
        _token(self.candidate_projection_version, "claim candidate_projection_version")
        for field_name in (
            "catalog_entry_claim_digest",
            "presentation_digest",
            "authority_digest",
            "catalog_namespace_digest",
            "catalog_provenance_digest",
            "catalog_artifact_sha256",
            "catalog_retrieval_snapshot_digest",
            "profile_snapshot_digest",
            "calibration_digest",
            "policy_digest",
        ):
            _digest(getattr(self, field_name), f"claim {field_name}")
        _optional_digest(self.material_snapshot_digest, "claim material_snapshot_digest")
        _optional_digest(
            self.installation_snapshot_digest,
            "claim installation_snapshot_digest",
        )

    @classmethod
    def create(
        cls,
        authority: ReviewedBenefitProfiles,
        *,
        presentation: CapabilityCandidate,
    ) -> EligibleCatalogClaim:
        if not isinstance(authority, ReviewedBenefitProfiles):
            raise TypeError("authority must be ReviewedBenefitProfiles")
        if not isinstance(presentation, CapabilityCandidate):
            raise TypeError("presentation must be a CapabilityCandidate")
        return cls(
            catalog_entry_claim_digest=catalog_candidate_entry_claim_digest(presentation),
            presentation_digest=capability_presentation_digest(presentation),
            authority_id=authority.authority_id,
            authority_kind=authority.authority_kind,
            authority_digest=authority.authority_digest,
            catalog_layer_kind=authority.catalog_layer_kind,
            sequence=authority.sequence,
            candidate_projection_version=authority.candidate_projection_version,
            catalog_namespace_digest=authority.catalog_namespace_digest,
            catalog_provenance_digest=authority.catalog_provenance_digest,
            catalog_artifact_sha256=authority.catalog_artifact_sha256,
            catalog_retrieval_snapshot_digest=authority.catalog_retrieval_snapshot_digest,
            material_snapshot_digest=authority.material_snapshot_digest,
            installation_snapshot_digest=authority.installation_snapshot_digest,
            profile_snapshot_digest=authority.profile_snapshot_digest,
            calibration_digest=authority.calibration_digest,
            policy_digest=authority.policy_digest,
        )

    def matches(self, authority: ReviewedBenefitProfiles) -> bool:
        return all(
            getattr(self, field_name) == getattr(authority, field_name)
            for field_name in (
                "authority_id",
                "authority_kind",
                "authority_digest",
                "catalog_layer_kind",
                "sequence",
                "candidate_projection_version",
                "catalog_namespace_digest",
                "catalog_provenance_digest",
                "catalog_artifact_sha256",
                "catalog_retrieval_snapshot_digest",
                "material_snapshot_digest",
                "installation_snapshot_digest",
                "profile_snapshot_digest",
                "calibration_digest",
                "policy_digest",
            )
        )


def eligible_catalog_claim_digest(claim: EligibleCatalogClaim) -> str:
    """Digest the complete authority-qualified catalog claim."""

    if not isinstance(claim, EligibleCatalogClaim):
        raise TypeError("claim must be an EligibleCatalogClaim")
    return _canonical_digest(
        {
            "claim": {
                field_name: getattr(claim, field_name)
                for field_name in EligibleCatalogClaim.__dataclass_fields__
            },
            "schema": "ctx.eligible-catalog-claim-v1",
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryCapabilityEligibility:
    """Exact query-scoped host/policy facts, separate from benefit review."""

    presentation_digest: str
    catalog_entry_claim_digest: str
    catalog_claim_digest: str
    available: bool
    permissions_allowed: bool
    credentials_available: bool

    def __post_init__(self) -> None:
        _digest(self.presentation_digest, "eligibility presentation_digest")
        _digest(self.catalog_entry_claim_digest, "eligibility catalog_entry_claim_digest")
        _digest(self.catalog_claim_digest, "eligibility catalog_claim_digest")
        _boolean(self.available, "eligibility available")
        _boolean(self.permissions_allowed, "eligibility permissions_allowed")
        _boolean(self.credentials_available, "eligibility credentials_available")


def _query_eligibility_mapping(value: QueryCapabilityEligibility) -> dict[str, object]:
    return {
        field_name: getattr(value, field_name)
        for field_name in QueryCapabilityEligibility.__dataclass_fields__
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class FrozenQueryHostPolicyAuthority:
    """Immutable exact host facts derived from one pinned upstream policy read."""

    upstream_snapshot_digest: str
    eligibilities: tuple[QueryCapabilityEligibility, ...]
    host_policy_snapshot_digest: str = ""

    def __post_init__(self) -> None:
        _digest(self.upstream_snapshot_digest, "upstream host policy snapshot digest")
        if (
            not isinstance(self.eligibilities, tuple)
            or len(self.eligibilities) > MAX_CANDIDATES
            or not all(
                isinstance(value, QueryCapabilityEligibility) for value in self.eligibilities
            )
        ):
            raise _fail("frozen host policy facts must be a bounded immutable tuple")
        order = tuple(
            (
                value.presentation_digest,
                value.catalog_claim_digest,
                value.catalog_entry_claim_digest,
            )
            for value in self.eligibilities
        )
        if order != tuple(sorted(order)) or len(set(order)) != len(order):
            raise _fail("frozen host policy facts must use unique canonical order")
        object.__setattr__(
            self,
            "host_policy_snapshot_digest",
            _canonical_digest(
                {
                    "eligibilities": [
                        _query_eligibility_mapping(value) for value in self.eligibilities
                    ],
                    "schema": "ctx.frozen-query-host-policy-v1",
                    "upstream_snapshot_digest": self.upstream_snapshot_digest,
                }
            ),
        )

    @classmethod
    def create(
        cls,
        *,
        upstream_snapshot_digest: str,
        eligibilities: Sequence[QueryCapabilityEligibility],
    ) -> FrozenQueryHostPolicyAuthority:
        if isinstance(eligibilities, (str, bytes, bytearray)) or not isinstance(
            eligibilities, Sequence
        ):
            raise TypeError("eligibilities must be a bounded sequence")
        try:
            bounded = tuple(islice(iter(eligibilities), MAX_CANDIDATES + 1))
        except Exception:
            raise TypeError("eligibilities could not be read as a bounded sequence") from None
        if len(bounded) > MAX_CANDIDATES:
            raise _fail("host policy facts exceed their bounded item limit")
        if not all(isinstance(value, QueryCapabilityEligibility) for value in bounded):
            raise TypeError("eligibilities must contain QueryCapabilityEligibility values")
        return cls(
            upstream_snapshot_digest=upstream_snapshot_digest,
            eligibilities=tuple(
                sorted(
                    bounded,
                    key=lambda value: (
                        value.presentation_digest,
                        value.catalog_claim_digest,
                        value.catalog_entry_claim_digest,
                    ),
                )
            ),
        )

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility | None:
        presentation_digest = capability_presentation_digest(presentation)
        claim_digest = eligible_catalog_claim_digest(claim)
        for value in self.eligibilities:
            if (
                value.presentation_digest == presentation_digest
                and value.catalog_claim_digest == claim_digest
                and value.catalog_entry_claim_digest == claim.catalog_entry_claim_digest
            ):
                return value
        return None


class QueryHostPolicyAuthority(Protocol):
    """Pinned current-host policy authority; never an installation consent source."""

    @property
    def host_policy_snapshot_digest(self) -> str: ...

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility | None: ...


class EligibleQueryCandidateSource(Protocol):
    """Eligibility-filtered, multi-layer source consumed by query closure."""

    catalog_snapshot_digest: str
    catalog_retrieval_snapshot_digest: str
    catalog_namespace_digest: str
    catalog_provenance_digest: str
    catalog_artifact_sha256: str
    candidate_projection_version: str
    material_snapshot_digest: str | None
    installation_snapshot_digest: str | None
    profile_snapshot_digest: str
    authority_snapshot_digest: str
    eligibility_snapshot_digest: str
    calibration_digest: str
    policy_digest: str

    def retrieve(self, observation: WorkObservation) -> Sequence[CapabilityCandidate]: ...

    def entry_claim(self, presentation: CapabilityCandidate) -> EligibleCatalogClaim: ...

    def close(self) -> None: ...


class FrozenQueryCandidateSource:
    """Immutable exact-observation source owned by one engine composition."""

    __slots__ = (
        "_candidates",
        "_closed",
        "_lock",
        "_observation",
        "catalog_snapshot_digest",
    )

    def __init__(
        self,
        *,
        observation: WorkObservation,
        candidates: tuple[CapabilityCandidate, ...],
        catalog_snapshot_digest: str,
    ) -> None:
        if not isinstance(observation, WorkObservation):
            raise TypeError("observation must be a WorkObservation")
        if not isinstance(candidates, tuple) or len(candidates) > MAX_CANDIDATES:
            raise _fail("frozen query candidates must be a bounded tuple")
        if not all(isinstance(candidate, CapabilityCandidate) for candidate in candidates):
            raise _fail("frozen query candidates are invalid")
        self._observation = observation
        self._candidates = candidates
        self.catalog_snapshot_digest = _digest(
            catalog_snapshot_digest,
            "catalog_snapshot_digest",
        )
        self._closed = False
        self._lock = threading.Lock()

    def retrieve(self, observation: WorkObservation) -> tuple[CapabilityCandidate, ...]:
        with self._lock:
            if self._closed:
                raise PlannerValidationError("frozen query candidate source is closed")
            if observation != self._observation:
                raise PlannerValidationError("frozen query candidate source observation mismatch")
            return self._candidates

    def close(self) -> None:
        with self._lock:
            self._closed = True


@dataclass(frozen=True, slots=True, kw_only=True, init=False)
class QueryBenefitClosure:
    """One exact, privacy-safe candidate, authority, policy, and facts closure."""

    observation: WorkObservation
    source: FrozenQueryCandidateSource
    benefit_facts: AuthenticatedBenefitFacts
    reviewed_authorities: ReviewedBenefitAuthorities
    policy: NetBenefitPolicy
    host_policy_authority: FrozenQueryHostPolicyAuthority
    catalog_claims: tuple[EligibleCatalogClaim, ...]
    host_eligibilities: tuple[QueryCapabilityEligibility, ...]
    authority_snapshot_digest: str
    catalog_snapshot_digest: str
    catalog_namespace_digest: str
    catalog_provenance_digest: str
    catalog_artifact_sha256: str
    catalog_retrieval_snapshot_digest: str
    candidate_projection_version: str
    eligibility_snapshot_digest: str
    material_snapshot_digest: str | None
    installation_snapshot_digest: str | None
    profile_snapshot_digest: str
    calibration_digest: str
    policy_digest: str
    upstream_host_policy_snapshot_digest: str
    host_policy_snapshot_digest: str
    benefit_facts_snapshot_digest: str
    closure_snapshot_digest: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("QueryBenefitClosure values must be created by the closure factory")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        values: Mapping[str, object],
    ) -> QueryBenefitClosure:
        if factory_token is not _QUERY_CLOSURE_FACTORY_TOKEN:
            raise TypeError("QueryBenefitClosure values must be created by the closure factory")
        expected_fields = frozenset(cls.__dataclass_fields__)
        if set(values) != expected_fields:
            raise _fail("query closure factory values are incomplete")
        instance = object.__new__(cls)
        for field_name in expected_fields:
            object.__setattr__(instance, field_name, values[field_name])
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        if not isinstance(self.observation, WorkObservation):
            raise TypeError("closure observation must be a WorkObservation")
        if not isinstance(self.source, FrozenQueryCandidateSource):
            raise TypeError("closure source must be a FrozenQueryCandidateSource")
        if not isinstance(self.benefit_facts, AuthenticatedBenefitFacts):
            raise TypeError("closure benefit_facts must be AuthenticatedBenefitFacts")
        if not isinstance(self.reviewed_authorities, ReviewedBenefitAuthorities):
            raise TypeError("closure reviewed_authorities must be ReviewedBenefitAuthorities")
        if not isinstance(self.policy, NetBenefitPolicy):
            raise TypeError("closure policy must be a NetBenefitPolicy")
        if not isinstance(self.host_policy_authority, FrozenQueryHostPolicyAuthority):
            raise TypeError("closure host_policy_authority must be FrozenQueryHostPolicyAuthority")
        if not isinstance(self.catalog_claims, tuple) or not all(
            isinstance(value, EligibleCatalogClaim) for value in self.catalog_claims
        ):
            raise TypeError("closure catalog_claims must be an immutable claim tuple")
        if not isinstance(self.host_eligibilities, tuple) or not all(
            isinstance(value, QueryCapabilityEligibility) for value in self.host_eligibilities
        ):
            raise TypeError("closure host_eligibilities must be an immutable eligibility tuple")
        _token(self.candidate_projection_version, "closure candidate_projection_version")
        for field_name in (
            "authority_snapshot_digest",
            "catalog_snapshot_digest",
            "catalog_namespace_digest",
            "catalog_provenance_digest",
            "catalog_artifact_sha256",
            "catalog_retrieval_snapshot_digest",
            "eligibility_snapshot_digest",
            "profile_snapshot_digest",
            "calibration_digest",
            "policy_digest",
            "upstream_host_policy_snapshot_digest",
            "host_policy_snapshot_digest",
            "benefit_facts_snapshot_digest",
            "closure_snapshot_digest",
        ):
            _digest(getattr(self, field_name), f"closure {field_name}")
        _optional_digest(self.material_snapshot_digest, "closure material_snapshot_digest")
        _optional_digest(
            self.installation_snapshot_digest,
            "closure installation_snapshot_digest",
        )
        if (
            self.policy.policy_digest != self.policy_digest
            or self.policy.calibration_digest != self.calibration_digest
        ):
            raise _fail("closure policy does not match its authenticated bindings")
        if (
            self.host_policy_authority.upstream_snapshot_digest
            != self.upstream_host_policy_snapshot_digest
            or self.host_policy_authority.host_policy_snapshot_digest
            != self.host_policy_snapshot_digest
        ):
            raise _fail("closure host policy pins do not match the frozen authority")
        authority_bindings = {
            "authority_snapshot_digest": self.reviewed_authorities.authority_snapshot_digest,
            "calibration_digest": self.reviewed_authorities.calibration_digest,
            "candidate_projection_version": (
                self.reviewed_authorities.candidate_projection_version
            ),
            "catalog_artifact_sha256": self.reviewed_authorities.catalog_artifact_sha256,
            "catalog_namespace_digest": self.reviewed_authorities.catalog_namespace_digest,
            "catalog_provenance_digest": self.reviewed_authorities.catalog_provenance_digest,
            "catalog_retrieval_snapshot_digest": (
                self.reviewed_authorities.catalog_retrieval_snapshot_digest
            ),
            "installation_snapshot_digest": (
                self.reviewed_authorities.installation_snapshot_digest
            ),
            "material_snapshot_digest": self.reviewed_authorities.material_snapshot_digest,
            "policy_digest": self.reviewed_authorities.policy_digest,
            "profile_snapshot_digest": self.reviewed_authorities.profile_snapshot_digest,
        }
        for field_name, expected in authority_bindings.items():
            if getattr(self, field_name) != expected:
                raise _fail(f"closure {field_name} does not match reviewed authorities")
        if self.source.catalog_snapshot_digest != self.closure_snapshot_digest:
            raise _fail("closure source digest does not match the closure")
        if self.benefit_facts.benefit_facts_snapshot_digest != self.benefit_facts_snapshot_digest:
            raise _fail("closure facts digest does not match the facts source")
        candidates = self.source.retrieve(self.observation)
        if not (len(candidates) == len(self.catalog_claims) == len(self.host_eligibilities)):
            raise _fail("closure candidates, claims, and host facts are not one-to-one")
        for candidate, claim, eligibility in zip(
            candidates,
            self.catalog_claims,
            self.host_eligibilities,
            strict=True,
        ):
            if claim.catalog_entry_claim_digest != catalog_candidate_entry_claim_digest(candidate):
                raise _fail("closure catalog claim does not match candidate material")
            if claim.presentation_digest != capability_presentation_digest(candidate):
                raise _fail("closure catalog claim does not match exact presentation")
            if eligibility.presentation_digest != capability_presentation_digest(candidate):
                raise _fail("closure host facts contain a substituted presentation")
            if eligibility.catalog_entry_claim_digest != claim.catalog_entry_claim_digest:
                raise _fail("closure host facts contain a substituted catalog claim")
            if eligibility.catalog_claim_digest != eligible_catalog_claim_digest(claim):
                raise _fail("closure host facts contain a cross-authority catalog claim")
            if not eligibility.available:
                raise _fail("closure retained a host-unavailable presentation")
            if self.host_policy_authority.eligibility_for(candidate, claim) != eligibility:
                raise _fail("closure host fact is not a member of its frozen policy authority")
            matched = self.reviewed_authorities.profile_for(candidate, claim)
            if matched is None:
                raise _fail("closure claim is not a member of its reviewed authority snapshot")
            authority, profile = matched
            if not profile.match_policy.accepts(candidate, self.observation):
                raise _fail("closure presentation does not satisfy its reviewed match policy")
            benefit = self.benefit_facts.benefit_candidate(candidate, self.observation)
            if benefit is None or not _benefit_matches_review(
                benefit,
                candidate,
                profile,
                authority,
                eligibility,
            ):
                raise _fail("closure benefit facts do not match the reviewed authority")
        presentation_digests = tuple(
            sorted(capability_presentation_digest(candidate) for candidate in candidates)
        )
        if presentation_digests != self.benefit_facts.presentation_digests:
            raise _fail("closure facts do not cover the frozen source exactly")
        expected_closure_digest = _query_closure_digest(
            observation=self.observation,
            presentation_digests=presentation_digests,
            claims=self.catalog_claims,
            eligibilities=self.host_eligibilities,
            authority_snapshot_digest=self.authority_snapshot_digest,
            benefit_facts_snapshot_digest=self.benefit_facts_snapshot_digest,
            calibration_digest=self.calibration_digest,
            candidate_projection_version=self.candidate_projection_version,
            catalog_artifact_sha256=self.catalog_artifact_sha256,
            catalog_namespace_digest=self.catalog_namespace_digest,
            catalog_provenance_digest=self.catalog_provenance_digest,
            catalog_retrieval_snapshot_digest=self.catalog_retrieval_snapshot_digest,
            catalog_snapshot_digest=self.catalog_snapshot_digest,
            eligibility_snapshot_digest=self.eligibility_snapshot_digest,
            host_policy_snapshot_digest=self.host_policy_snapshot_digest,
            upstream_host_policy_snapshot_digest=(self.upstream_host_policy_snapshot_digest),
            installation_snapshot_digest=self.installation_snapshot_digest,
            material_snapshot_digest=self.material_snapshot_digest,
            policy_digest=self.policy_digest,
            profile_snapshot_digest=self.profile_snapshot_digest,
        )
        if expected_closure_digest != self.closure_snapshot_digest:
            raise _fail("closure digest does not match its exact bound values")


@dataclass(frozen=True, slots=True, kw_only=True)
class _EligibleSourceBindings:
    catalog_snapshot_digest: str
    catalog_retrieval_snapshot_digest: str
    catalog_namespace_digest: str
    catalog_provenance_digest: str
    catalog_artifact_sha256: str
    candidate_projection_version: str
    material_snapshot_digest: str | None
    installation_snapshot_digest: str | None
    profile_snapshot_digest: str
    authority_snapshot_digest: str
    eligibility_snapshot_digest: str
    calibration_digest: str
    policy_digest: str

    def __post_init__(self) -> None:
        _token(self.candidate_projection_version, "source candidate_projection_version")
        for field_name in (
            "catalog_snapshot_digest",
            "catalog_retrieval_snapshot_digest",
            "catalog_namespace_digest",
            "catalog_provenance_digest",
            "catalog_artifact_sha256",
            "profile_snapshot_digest",
            "authority_snapshot_digest",
            "eligibility_snapshot_digest",
            "calibration_digest",
            "policy_digest",
        ):
            _digest(getattr(self, field_name), f"source {field_name}")
        _optional_digest(self.material_snapshot_digest, "source material_snapshot_digest")
        _optional_digest(
            self.installation_snapshot_digest,
            "source installation_snapshot_digest",
        )


def _source_binding(source: EligibleQueryCandidateSource, field_name: str) -> object:
    try:
        return getattr(source, field_name)
    except Exception:
        raise _fail(f"eligible source is missing {field_name}") from None


def _read_source_bindings(source: EligibleQueryCandidateSource) -> _EligibleSourceBindings:
    return _EligibleSourceBindings(
        catalog_snapshot_digest=_source_binding(source, "catalog_snapshot_digest"),  # type: ignore[arg-type]
        catalog_retrieval_snapshot_digest=_source_binding(  # type: ignore[arg-type]
            source, "catalog_retrieval_snapshot_digest"
        ),
        catalog_namespace_digest=_source_binding(source, "catalog_namespace_digest"),  # type: ignore[arg-type]
        catalog_provenance_digest=_source_binding(source, "catalog_provenance_digest"),  # type: ignore[arg-type]
        catalog_artifact_sha256=_source_binding(source, "catalog_artifact_sha256"),  # type: ignore[arg-type]
        candidate_projection_version=_source_binding(  # type: ignore[arg-type]
            source, "candidate_projection_version"
        ),
        material_snapshot_digest=_source_binding(source, "material_snapshot_digest"),  # type: ignore[arg-type]
        installation_snapshot_digest=_source_binding(  # type: ignore[arg-type]
            source, "installation_snapshot_digest"
        ),
        profile_snapshot_digest=_source_binding(source, "profile_snapshot_digest"),  # type: ignore[arg-type]
        authority_snapshot_digest=_source_binding(source, "authority_snapshot_digest"),  # type: ignore[arg-type]
        eligibility_snapshot_digest=_source_binding(source, "eligibility_snapshot_digest"),  # type: ignore[arg-type]
        calibration_digest=_source_binding(source, "calibration_digest"),  # type: ignore[arg-type]
        policy_digest=_source_binding(source, "policy_digest"),  # type: ignore[arg-type]
    )


def _assert_source_bindings(
    actual: _EligibleSourceBindings,
    expected: _EligibleSourceBindings,
) -> None:
    if actual != expected:
        raise _fail("eligible source binding drift detected")


def _validate_initial_source_bindings(
    bindings: _EligibleSourceBindings,
    profiles: ReviewedBenefitAuthorities,
    policy: NetBenefitPolicy,
) -> None:
    expected = {
        "authority_snapshot_digest": profiles.authority_snapshot_digest,
        "calibration_digest": profiles.calibration_digest,
        "candidate_projection_version": profiles.candidate_projection_version,
        "catalog_artifact_sha256": profiles.catalog_artifact_sha256,
        "catalog_namespace_digest": profiles.catalog_namespace_digest,
        "catalog_provenance_digest": profiles.catalog_provenance_digest,
        "catalog_retrieval_snapshot_digest": profiles.catalog_retrieval_snapshot_digest,
        "installation_snapshot_digest": profiles.installation_snapshot_digest,
        "material_snapshot_digest": profiles.material_snapshot_digest,
        "policy_digest": profiles.policy_digest,
        "profile_snapshot_digest": profiles.profile_snapshot_digest,
    }
    for field_name, value in expected.items():
        if getattr(bindings, field_name) != value:
            raise _fail(f"eligible source {field_name} binding mismatch")
    if policy.calibration_digest != profiles.calibration_digest:
        raise _fail("net-benefit policy calibration does not match reviewed profiles")
    if policy.policy_digest != profiles.policy_digest:
        raise _fail("net-benefit policy does not match reviewed profiles")


def _reviewed_profile_mapping(profile: ReviewedBenefitProfile) -> dict[str, object]:
    return {
        "actionability": profile.actionability,
        "capability_id": profile.capability_id,
        "catalog_entry_claim_digest": profile.catalog_entry_claim_digest,
        "complements": list(profile.complements),
        "conflicts": list(profile.conflicts),
        "costs": {
            field_name: getattr(profile.costs, field_name) for field_name in ResourceCosts._FIELDS
        },
        "coverage_keys": list(profile.coverage_keys),
        "expected_task_benefit_ppm": profile.expected_task_benefit_ppm,
        "kind": profile.kind,
        "match_policy": {
            "allowed_equivalence_keys": list(profile.match_policy.allowed_equivalence_keys),
            "allowed_reason_codes": list(profile.match_policy.allowed_reason_codes),
            "allowed_signals": list(profile.match_policy.allowed_signals),
            "minimum_matching_signals": profile.match_policy.minimum_matching_signals,
            "minimum_non_language_matching_signals": (
                profile.match_policy.minimum_non_language_matching_signals
            ),
            "required_any_signals": list(profile.match_policy.required_any_signals),
        },
        "maximum_relevance_ppm": profile.maximum_relevance_ppm,
        "name": profile.name,
        "profile_id": profile.profile_id,
        "review_basis_digest": profile.review_basis_digest,
        "security_approved": profile.security_approved,
        "source_trusted": profile.source_trusted,
        "trust_ppm": profile.trust_ppm,
    }


def _reviewed_profiles_manifest_mapping(
    authority: ReviewedBenefitProfiles,
) -> dict[str, object]:
    return {
        "authority": {
            "authority_digest": authority.authority_digest,
            "authority_id": authority.authority_id,
            "authority_kind": authority.authority_kind,
            "catalog_layer_kind": authority.catalog_layer_kind,
            "sequence": authority.sequence,
        },
        "bindings": {
            "calibration_digest": authority.calibration_digest,
            "candidate_projection_version": authority.candidate_projection_version,
            "catalog_artifact_sha256": authority.catalog_artifact_sha256,
            "catalog_namespace_digest": authority.catalog_namespace_digest,
            "catalog_provenance_digest": authority.catalog_provenance_digest,
            "catalog_retrieval_snapshot_digest": authority.catalog_retrieval_snapshot_digest,
            "installation_snapshot_digest": authority.installation_snapshot_digest,
            "material_snapshot_digest": authority.material_snapshot_digest,
            "policy_digest": authority.policy_digest,
        },
        "profiles": [_reviewed_profile_mapping(profile) for profile in authority.profiles],
        "schema": REVIEWED_BENEFIT_PROFILES_SCHEMA,
    }


def _resource_profile_digest(
    profile: ReviewedBenefitProfile,
    authority: ReviewedBenefitProfiles,
) -> str:
    return _canonical_digest(
        {
            "actionability": profile.actionability,
            "authority_digest": authority.authority_digest,
            "authority_id": authority.authority_id,
            "authority_kind": authority.authority_kind,
            "catalog_layer_kind": authority.catalog_layer_kind,
            "installation_snapshot_digest": authority.installation_snapshot_digest,
            "material_snapshot_digest": authority.material_snapshot_digest,
            "profile_digest": profile.profile_digest,
            "profile_snapshot_digest": authority.profile_snapshot_digest,
            "schema": "ctx.query-resource-profile-v2",
            "sequence": authority.sequence,
        }
    )


def _reviewed_query_benefit_candidate(
    presentation: CapabilityCandidate,
    profile: ReviewedBenefitProfile,
    authority: ReviewedBenefitProfiles,
    eligibility: QueryCapabilityEligibility,
) -> BenefitCandidate:
    """Build the exact zero-history policy input for one reviewed query row."""

    if not isinstance(presentation, CapabilityCandidate):
        raise TypeError("presentation must be a CapabilityCandidate")
    if not isinstance(profile, ReviewedBenefitProfile):
        raise TypeError("profile must be a ReviewedBenefitProfile")
    if not isinstance(authority, ReviewedBenefitProfiles):
        raise TypeError("authority must be ReviewedBenefitProfiles")
    if not isinstance(eligibility, QueryCapabilityEligibility):
        raise TypeError("eligibility must be QueryCapabilityEligibility")
    if not _benefit_matches_review_identity(presentation, profile, authority, eligibility):
        raise _fail("reviewed benefit inputs do not share one exact query identity")
    return BenefitCandidate(
        capability_id=presentation.capability_id,
        source_digest=presentation.source_digest,
        resource_profile_digest=_resource_profile_digest(profile, authority),
        availability="advisory" if presentation.actionability == "manual" else "executable",
        expected_task_benefit_ppm=profile.expected_task_benefit_ppm,
        relevance_ppm=min(profile.maximum_relevance_ppm, presentation.normalized_score_ppm),
        trust_ppm=profile.trust_ppm,
        costs=profile.costs,
        evidence=EvidenceSummary(
            capability_id=presentation.capability_id,
            kind=presentation.kind,
            source_digest=presentation.source_digest,
            evidence_window_digest="0" * 64,
            opportunity_observable=False,
        ),
        source_trusted=profile.source_trusted,
        security_approved=profile.security_approved,
        permissions_allowed=eligibility.permissions_allowed,
        credentials_available=eligibility.credentials_available,
        coverage_keys=profile.coverage_keys,
        equivalence_key=presentation.equivalence_key,
        complements=profile.complements,
        conflicts=profile.conflicts,
    )


def _benefit_matches_review_identity(
    presentation: CapabilityCandidate,
    profile: ReviewedBenefitProfile,
    authority: ReviewedBenefitProfiles,
    eligibility: QueryCapabilityEligibility,
) -> bool:
    return (
        profile.capability_id == presentation.capability_id
        and profile.kind == presentation.kind
        and profile.name == presentation.name
        and profile.actionability == presentation.actionability
        and eligibility.presentation_digest == capability_presentation_digest(presentation)
        and eligibility.catalog_entry_claim_digest == profile.catalog_entry_claim_digest
        and authority.profile_for(presentation, eligibility.catalog_entry_claim_digest) == profile
    )


def _cost_mapping(costs: ResourceCosts) -> dict[str, int]:
    return {field_name: getattr(costs, field_name) for field_name in ResourceCosts._FIELDS}


def _facts_record(
    presentation: CapabilityCandidate,
    profile: ReviewedBenefitProfile,
    authority: ReviewedBenefitProfiles,
    eligibility: QueryCapabilityEligibility,
) -> dict[str, object]:
    return {
        "availability": "advisory" if presentation.actionability == "manual" else "executable",
        "complements": list(profile.complements),
        "conflicts": list(profile.conflicts),
        "costs": _cost_mapping(profile.costs),
        "coverage_keys": list(profile.coverage_keys),
        "credentials_available": eligibility.credentials_available,
        "expected_task_benefit_ppm": profile.expected_task_benefit_ppm,
        "maximum_relevance_ppm": min(
            profile.maximum_relevance_ppm,
            presentation.normalized_score_ppm,
        ),
        "permissions_allowed": eligibility.permissions_allowed,
        "presentation": capability_presentation_mapping(presentation),
        "presentation_digest": capability_presentation_digest(presentation),
        "resource_profile_digest": _resource_profile_digest(profile, authority),
        "security_approved": profile.security_approved,
        "source_trusted": profile.source_trusted,
        "trust_ppm": profile.trust_ppm,
    }


def _benefit_matches_review(
    benefit: BenefitCandidate,
    presentation: CapabilityCandidate,
    profile: ReviewedBenefitProfile,
    authority: ReviewedBenefitProfiles,
    eligibility: QueryCapabilityEligibility,
) -> bool:
    return (
        benefit.capability_id == presentation.capability_id
        and benefit.source_digest == presentation.source_digest
        and benefit.resource_profile_digest == _resource_profile_digest(profile, authority)
        and benefit.availability
        == ("advisory" if presentation.actionability == "manual" else "executable")
        and benefit.expected_task_benefit_ppm == profile.expected_task_benefit_ppm
        and benefit.relevance_ppm
        == min(profile.maximum_relevance_ppm, presentation.normalized_score_ppm)
        and benefit.trust_ppm == profile.trust_ppm
        and benefit.costs == profile.costs
        and benefit.source_trusted == profile.source_trusted
        and benefit.security_approved == profile.security_approved
        and benefit.permissions_allowed == eligibility.permissions_allowed
        and benefit.credentials_available == eligibility.credentials_available
        and benefit.coverage_keys == profile.coverage_keys
        and benefit.equivalence_key == presentation.equivalence_key
        and benefit.complements == profile.complements
        and benefit.conflicts == profile.conflicts
    )


def _candidate_order(candidate: CapabilityCandidate) -> tuple[object, ...]:
    return (
        candidate.capability_id,
        candidate.source_digest,
        candidate.actionability,
        -candidate.normalized_score_ppm,
        candidate.matching_signals,
        candidate.reason_codes,
    )


def _read_candidates(
    source: EligibleQueryCandidateSource,
    observation: WorkObservation,
    bindings: _EligibleSourceBindings,
) -> tuple[CapabilityCandidate, ...]:
    try:
        retrieved = source.retrieve(observation)
    except Exception:
        raise _fail("eligible source retrieval failed") from None
    try:
        candidates = normalize_candidate_pool(retrieved, observation)
    except (PlannerValidationError, TypeError):
        raise _fail("eligible source returned an invalid candidate pool") from None
    _assert_source_bindings(_read_source_bindings(source), bindings)
    return candidates


def _read_claims(
    source: EligibleQueryCandidateSource,
    candidates: Sequence[CapabilityCandidate],
    bindings: _EligibleSourceBindings,
) -> tuple[EligibleCatalogClaim, ...]:
    claims: list[EligibleCatalogClaim] = []
    for candidate in candidates:
        try:
            claim = source.entry_claim(candidate)
        except Exception:
            raise _fail("eligible source catalog-claim lookup failed") from None
        if not isinstance(claim, EligibleCatalogClaim):
            raise _fail("eligible source returned an invalid catalog claim")
        if claim.catalog_entry_claim_digest != catalog_candidate_entry_claim_digest(candidate):
            raise _fail("eligible source catalog claim does not match candidate material")
        if claim.presentation_digest != capability_presentation_digest(candidate):
            raise _fail("eligible source catalog claim does not match exact presentation")
        claims.append(claim)
        _assert_source_bindings(_read_source_bindings(source), bindings)
    return tuple(claims)


def _host_policy_digest(authority: QueryHostPolicyAuthority) -> str:
    try:
        return _digest(
            authority.host_policy_snapshot_digest,
            "host_policy_snapshot_digest",
        )
    except Exception:
        raise _fail("host policy authority is missing its snapshot digest") from None


def _read_host_eligibility(
    authority: QueryHostPolicyAuthority,
    presentation: CapabilityCandidate,
    claim: EligibleCatalogClaim,
    snapshot_digest: str,
) -> QueryCapabilityEligibility:
    try:
        value = authority.eligibility_for(presentation, claim)
    except Exception:
        raise _fail("host policy eligibility lookup failed") from None
    if not isinstance(value, QueryCapabilityEligibility):
        raise _fail("host policy authority has no exact query eligibility fact")
    if value.presentation_digest != capability_presentation_digest(presentation):
        raise _fail("host policy authority returned a substituted presentation")
    if value.catalog_entry_claim_digest != claim.catalog_entry_claim_digest:
        raise _fail("host policy authority returned a substituted catalog claim")
    if value.catalog_claim_digest != eligible_catalog_claim_digest(claim):
        raise _fail("host policy authority returned a cross-authority catalog claim")
    if _host_policy_digest(authority) != snapshot_digest:
        raise _fail("host policy authority drift detected")
    return value


def _observation_mapping(observation: WorkObservation) -> dict[str, object]:
    return {
        "active_capability_ids": list(observation.active_capability_ids),
        "baseline_capability_ids": list(observation.baseline_capability_ids),
        "languages": list(observation.languages),
        "rejected_capability_ids": list(observation.rejected_capability_ids),
        "requested_limit": observation.requested_limit,
        "signals": list(observation.signals),
    }


def _claim_mapping(claim: EligibleCatalogClaim) -> dict[str, object]:
    return {
        field_name: getattr(claim, field_name)
        for field_name in EligibleCatalogClaim.__dataclass_fields__
    }


def _eligibility_mapping(value: QueryCapabilityEligibility) -> dict[str, object]:
    return _query_eligibility_mapping(value)


def _query_closure_digest(
    *,
    observation: WorkObservation,
    presentation_digests: Sequence[str],
    claims: Sequence[EligibleCatalogClaim],
    eligibilities: Sequence[QueryCapabilityEligibility],
    authority_snapshot_digest: str,
    benefit_facts_snapshot_digest: str,
    calibration_digest: str,
    candidate_projection_version: str,
    catalog_artifact_sha256: str,
    catalog_namespace_digest: str,
    catalog_provenance_digest: str,
    catalog_retrieval_snapshot_digest: str,
    catalog_snapshot_digest: str,
    eligibility_snapshot_digest: str,
    host_policy_snapshot_digest: str,
    upstream_host_policy_snapshot_digest: str,
    installation_snapshot_digest: str | None,
    material_snapshot_digest: str | None,
    policy_digest: str,
    profile_snapshot_digest: str,
) -> str:
    return _canonical_digest(
        {
            "authority_snapshot_digest": authority_snapshot_digest,
            "benefit_facts_snapshot_digest": benefit_facts_snapshot_digest,
            "calibration_digest": calibration_digest,
            "candidate_projection_version": candidate_projection_version,
            "catalog_artifact_sha256": catalog_artifact_sha256,
            "catalog_claims": [_claim_mapping(claim) for claim in claims],
            "catalog_namespace_digest": catalog_namespace_digest,
            "catalog_provenance_digest": catalog_provenance_digest,
            "catalog_retrieval_snapshot_digest": catalog_retrieval_snapshot_digest,
            "eligible_catalog_snapshot_digest": catalog_snapshot_digest,
            "eligibility_snapshot_digest": eligibility_snapshot_digest,
            "host_eligibilities": [_eligibility_mapping(value) for value in eligibilities],
            "host_policy_snapshot_digest": host_policy_snapshot_digest,
            "upstream_host_policy_snapshot_digest": (upstream_host_policy_snapshot_digest),
            "installation_snapshot_digest": installation_snapshot_digest,
            "material_snapshot_digest": material_snapshot_digest,
            "observation": _observation_mapping(observation),
            "policy_digest": policy_digest,
            "presentation_digests": list(presentation_digests),
            "profile_snapshot_digest": profile_snapshot_digest,
            "schema": QUERY_BENEFIT_CLOSURE_SCHEMA,
        }
    )


def prepare_query_benefit_closure(
    *,
    source: EligibleQueryCandidateSource,
    observation: WorkObservation,
    profiles: ReviewedBenefitAuthorities,
    policy: NetBenefitPolicy,
    host_policy: QueryHostPolicyAuthority,
) -> QueryBenefitClosure:
    """Consume an eligible source and freeze exact facts for one observation."""

    try:
        close_source = getattr(source, "close", None)
    except Exception:
        raise TypeError("source must transfer an explicit closeable ownership lease") from None
    if not callable(close_source):
        raise TypeError("source must transfer an explicit closeable ownership lease")
    primary_error: BaseException | None = None
    try:
        if not isinstance(observation, WorkObservation):
            raise TypeError("observation must be a WorkObservation")
        if not isinstance(profiles, ReviewedBenefitAuthorities):
            raise TypeError("profiles must be ReviewedBenefitAuthorities")
        if not isinstance(policy, NetBenefitPolicy):
            raise TypeError("policy must be a NetBenefitPolicy")
        source_bindings = _read_source_bindings(source)
        _validate_initial_source_bindings(source_bindings, profiles, policy)
        upstream_host_policy_snapshot_digest = _host_policy_digest(host_policy)

        first_candidates = _read_candidates(source, observation, source_bindings)
        first_claims = _read_claims(source, first_candidates, source_bindings)
        second_candidates = _read_candidates(source, observation, source_bindings)
        second_claims = _read_claims(source, second_candidates, source_bindings)
        if first_candidates != second_candidates or first_claims != second_claims:
            raise _fail("eligible source changed candidates or claims during closure")

        retained: list[
            tuple[
                CapabilityCandidate,
                EligibleCatalogClaim,
                ReviewedBenefitProfiles,
                ReviewedBenefitProfile,
                QueryCapabilityEligibility,
            ]
        ] = []
        for candidate, claim in zip(first_candidates, first_claims, strict=True):
            matched = profiles.profile_for(candidate, claim)
            if matched is None:
                raise _fail(
                    "eligible source returned a candidate without an exact reviewed profile"
                )
            authority, profile = matched
            if not profile.match_policy.accepts(candidate, observation):
                continue
            first_eligibility = _read_host_eligibility(
                host_policy,
                candidate,
                claim,
                upstream_host_policy_snapshot_digest,
            )
            second_eligibility = _read_host_eligibility(
                host_policy,
                candidate,
                claim,
                upstream_host_policy_snapshot_digest,
            )
            if first_eligibility != second_eligibility:
                raise _fail("host policy authority changed query eligibility facts")
            if not first_eligibility.available:
                continue
            retained.append((candidate, claim, authority, profile, first_eligibility))

        _assert_source_bindings(_read_source_bindings(source), source_bindings)
        if _host_policy_digest(host_policy) != upstream_host_policy_snapshot_digest:
            raise _fail("host policy authority drift detected")

        records = [
            _facts_record(candidate, profile, authority, eligibility)
            for candidate, _claim, authority, profile, eligibility in retained
        ]
        records.sort(key=lambda record: str(record["presentation_digest"]))
        facts_body = _canonical_bytes(
            {"records": records, "schema": "ctx.authenticated-benefit-facts-v1"}
        )
        facts_digest = hashlib.sha256(facts_body).hexdigest()
        facts = load_authenticated_benefit_facts_bytes(facts_body, facts_digest)
        presentation_digests = tuple(
            sorted(capability_presentation_digest(candidate) for candidate, *_rest in retained)
        )
        if facts.presentation_digests != presentation_digests:
            raise _fail("query facts do not close over the exact frozen presentations")
        if any(
            facts.benefit_candidate(candidate, observation) is None
            for candidate, *_rest in retained
        ):
            raise _fail("query facts reject a retained candidate for the exact observation")

        retained_claims = tuple(claim for _, claim, *_rest in retained)
        retained_eligibilities = tuple(eligibility for *_, eligibility in retained)
        frozen_host_policy = FrozenQueryHostPolicyAuthority.create(
            upstream_snapshot_digest=upstream_host_policy_snapshot_digest,
            eligibilities=retained_eligibilities,
        )
        closure_digest = _query_closure_digest(
            observation=observation,
            presentation_digests=presentation_digests,
            claims=retained_claims,
            eligibilities=retained_eligibilities,
            authority_snapshot_digest=profiles.authority_snapshot_digest,
            benefit_facts_snapshot_digest=facts_digest,
            calibration_digest=policy.calibration_digest,
            candidate_projection_version=source_bindings.candidate_projection_version,
            catalog_artifact_sha256=source_bindings.catalog_artifact_sha256,
            catalog_namespace_digest=source_bindings.catalog_namespace_digest,
            catalog_provenance_digest=source_bindings.catalog_provenance_digest,
            catalog_retrieval_snapshot_digest=source_bindings.catalog_retrieval_snapshot_digest,
            catalog_snapshot_digest=source_bindings.catalog_snapshot_digest,
            eligibility_snapshot_digest=source_bindings.eligibility_snapshot_digest,
            host_policy_snapshot_digest=frozen_host_policy.host_policy_snapshot_digest,
            upstream_host_policy_snapshot_digest=upstream_host_policy_snapshot_digest,
            installation_snapshot_digest=source_bindings.installation_snapshot_digest,
            material_snapshot_digest=source_bindings.material_snapshot_digest,
            policy_digest=policy.policy_digest,
            profile_snapshot_digest=profiles.profile_snapshot_digest,
        )
        frozen = FrozenQueryCandidateSource(
            observation=observation,
            candidates=tuple(candidate for candidate, *_rest in retained),
            catalog_snapshot_digest=closure_digest,
        )
        return QueryBenefitClosure._create(
            factory_token=_QUERY_CLOSURE_FACTORY_TOKEN,
            values={
                "observation": observation,
                "source": frozen,
                "benefit_facts": facts,
                "reviewed_authorities": profiles,
                "policy": policy,
                "host_policy_authority": frozen_host_policy,
                "catalog_claims": retained_claims,
                "host_eligibilities": retained_eligibilities,
                "authority_snapshot_digest": profiles.authority_snapshot_digest,
                "catalog_snapshot_digest": source_bindings.catalog_snapshot_digest,
                "catalog_namespace_digest": source_bindings.catalog_namespace_digest,
                "catalog_provenance_digest": source_bindings.catalog_provenance_digest,
                "catalog_artifact_sha256": source_bindings.catalog_artifact_sha256,
                "catalog_retrieval_snapshot_digest": (
                    source_bindings.catalog_retrieval_snapshot_digest
                ),
                "candidate_projection_version": source_bindings.candidate_projection_version,
                "eligibility_snapshot_digest": source_bindings.eligibility_snapshot_digest,
                "material_snapshot_digest": source_bindings.material_snapshot_digest,
                "installation_snapshot_digest": source_bindings.installation_snapshot_digest,
                "profile_snapshot_digest": profiles.profile_snapshot_digest,
                "calibration_digest": policy.calibration_digest,
                "policy_digest": policy.policy_digest,
                "upstream_host_policy_snapshot_digest": (upstream_host_policy_snapshot_digest),
                "host_policy_snapshot_digest": (frozen_host_policy.host_policy_snapshot_digest),
                "benefit_facts_snapshot_digest": facts_digest,
                "closure_snapshot_digest": closure_digest,
            },
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            close_source()
        except BaseException as cleanup_error:
            if primary_error is None:
                if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
                    raise
                raise _fail("eligible source cleanup failed") from None
            primary_error.add_note("CTX eligible-source cleanup also failed")


__all__ = [
    "MAX_REVIEWED_BENEFIT_PROFILES",
    "MAX_REVIEWED_PROFILE_AUTHORITIES",
    "QUERY_BENEFIT_CLOSURE_SCHEMA",
    "REVIEWED_BENEFIT_PROFILES_SCHEMA",
    "BenefitClosureError",
    "EligibleCatalogClaim",
    "EligibleQueryCandidateSource",
    "FrozenQueryCandidateSource",
    "FrozenQueryHostPolicyAuthority",
    "QueryBenefitClosure",
    "QueryCapabilityEligibility",
    "QueryHostPolicyAuthority",
    "ReviewedBenefitAuthorities",
    "ReviewedBenefitProfile",
    "ReviewedBenefitProfiles",
    "ReviewedMatchPolicy",
    "catalog_candidate_entry_claim_digest",
    "eligible_catalog_claim_digest",
    "load_reviewed_benefit_profiles",
    "load_reviewed_benefit_profiles_bytes",
    "prepare_query_benefit_closure",
]
