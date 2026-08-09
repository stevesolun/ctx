"""Exact-hash, benefit-eligible catalog inputs for query-only planning.

This module is deliberately not a package-release trust root.  The bytes loader
proves only equality with its caller-supplied digest.  Production code must own
the expected digest and all other authority inputs outside the loaded assets.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from types import MappingProxyType
from typing import Final

from ctx.engine.benefit import BenefitCandidate, CandidateAssessment, NetBenefitPolicy
from ctx.engine.capability_schema import (
    MAX_MATCHING_SIGNALS,
    MAX_REASON_CODES,
    PRESENTED_ACTIONABILITY_STATES,
    validate_capability_identity,
)
from ctx.engine.content import MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor, InstallPlanningBundle
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.observation import LANGUAGE_ALIASES
from ctx.engine.planner import (
    MAX_CANDIDATES,
    CapabilityCandidate,
    PlannerValidationError,
    WorkObservation,
)
from ctx.runtime.authenticated_benefit import capability_presentation_digest
from ctx.runtime.benefit_closure import (
    EligibleCatalogClaim,
    QueryBenefitClosure,
    QueryCapabilityEligibility,
    QueryHostPolicyAuthority,
    ReviewedBenefitAuthorities,
    ReviewedBenefitProfile,
    ReviewedBenefitProfiles,
    _reviewed_query_benefit_candidate,
    catalog_candidate_entry_claim_digest,
    eligible_catalog_claim_digest,
    prepare_query_benefit_closure,
)
from ctx.runtime.query_vocabulary import AuthenticatedQueryVocabulary
from ctx.runtime.planning_v3 import (
    AuthorityBoundInstallPlanningBundle,
    CatalogLoadPlanningBundle,
)


ELIGIBLE_CATALOG_SCHEMA: Final = "ctx.benefit-eligible-catalog-v1"
ELIGIBLE_ENTRY_PROJECTION: Final = "ctx.benefit-eligible-entry-v1"
MAX_ELIGIBLE_CATALOG_BYTES: Final = 4 * 1024 * 1024
MAX_ELIGIBLE_CATALOG_ENTRIES: Final = 4_096
_PREPARED_QUERY_FACTORY_TOKEN: Final = object()

_ROOT_FIELDS = frozenset({"authority", "bindings", "entries", "schema"})
_AUTHORITY_FIELDS = frozenset(
    {
        "authority_digest",
        "authority_id",
        "authority_kind",
        "catalog_layer_kind",
        "sequence",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "candidate_projection_version",
        "catalog_namespace_digest",
        "catalog_provenance_digest",
        "installation_snapshot_digest",
        "material_snapshot_digest",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "actionability",
        "capability_id",
        "equivalence_key",
        "install_bundle",
        "kind",
        "material_descriptor",
        "name",
    }
)
_INSTALL_BUNDLE_FIELDS = frozenset({"descriptor", "result_material"})
_AUTHORITY_KINDS = frozenset({"ctx-release", "organization", "user"})
_LAYER_KINDS = frozenset({"ctx", "organization", "user"})
_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._@-]{0,127}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_LANGUAGE_SIGNALS = frozenset(
    token for canonical, aliases in LANGUAGE_ALIASES.items() for token in (canonical, *aliases)
)


class EligibleCatalogError(ValueError):
    """An eligible catalog violates its closed, bounded contract."""


def _fail(message: str) -> EligibleCatalogError:
    return EligibleCatalogError(message)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("catalog JSON contains a duplicate object key")
        result[key] = value
    return result


def _closed(value: object, fields: frozenset[str], field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _fail(f"{field_name} must contain exactly its declared fields")
    if any(not isinstance(key, str) for key in value):
        raise _fail(f"{field_name} must use string fields")
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


def _decode_catalog(value: bytes, expected_sha256: str) -> Mapping[str, object]:
    if not isinstance(value, bytes):
        raise TypeError("catalog body must be immutable bytes")
    expected_sha256 = _digest(expected_sha256, "expected catalog SHA-256")
    if len(value) > MAX_ELIGIBLE_CATALOG_BYTES:
        raise _fail("catalog exceeds the authenticated byte bound")
    if hashlib.sha256(value).hexdigest() != expected_sha256:
        raise _fail("catalog does not match the caller-supplied SHA-256")
    try:
        decoded = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                _fail("catalog JSON contains a non-finite number")
            ),
        )
    except EligibleCatalogError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _fail("catalog must be valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise _fail("catalog root must be an object")
    canonical = _canonical_bytes(decoded)
    if value not in (canonical, canonical + b"\n"):
        raise _fail("catalog must use canonical JSON encoding")
    return decoded


def _entry_digest_mapping(
    *,
    actionability: str,
    capability_id: str,
    equivalence_key: str | None,
    install_bundle: InstallPlanningBundle | None,
    kind: str,
    material_descriptor: MaterialDescriptor | None,
    name: str,
) -> dict[str, object]:
    return {
        "actionability": actionability,
        "capability_id": capability_id,
        "equivalence_key": equivalence_key,
        "install_bundle": (
            None
            if install_bundle is None
            else {
                "descriptor": install_bundle.descriptor.to_dict(),
                "result_material": install_bundle.result_material.to_dict(),
            }
        ),
        "kind": kind,
        "material_descriptor": (
            None if material_descriptor is None else material_descriptor.to_dict()
        ),
        "name": name,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class EligibleCatalogEntry:
    """One prose-free static entry authenticated by its containing artifact."""

    capability_id: str
    kind: str
    name: str
    actionability: str
    equivalence_key: str | None
    material_descriptor: MaterialDescriptor | None
    install_bundle: InstallPlanningBundle | None
    source_digest: str
    catalog_entry_claim_digest: str

    def __post_init__(self) -> None:
        try:
            capability_id, kind = validate_capability_identity(self.capability_id, self.kind)
        except ValueError as exc:
            raise _fail("catalog entry capability identity is invalid") from exc
        if not isinstance(self.name, str) or _NAME_RE.fullmatch(self.name) is None:
            raise _fail("catalog entry name is invalid")
        if capability_id != f"{kind}:{self.name}":
            raise _fail("catalog entry name does not match its identity")
        if self.actionability not in PRESENTED_ACTIONABILITY_STATES:
            raise _fail("catalog entry actionability is unsupported")
        if self.equivalence_key is not None:
            _token(self.equivalence_key, "catalog entry equivalence_key")
        _digest(self.source_digest, "catalog entry source_digest")
        _digest(self.catalog_entry_claim_digest, "catalog entry claim digest")
        if self.actionability == "manual":
            if self.material_descriptor is not None or self.install_bundle is not None:
                raise _fail("manual catalog entries cannot carry planning authority")
        elif self.actionability == "load":
            if not isinstance(self.material_descriptor, MaterialDescriptor):
                raise _fail("load catalog entries require one material descriptor")
            if self.install_bundle is not None:
                raise _fail("load catalog entries cannot carry install authority")
            if (
                self.material_descriptor.capability_id,
                self.material_descriptor.kind,
                self.material_descriptor.actionability,
            ) != (self.capability_id, self.kind, "load"):
                raise _fail("load catalog planning authority crosses capability identity")
        else:
            if not isinstance(self.install_bundle, InstallPlanningBundle):
                raise _fail("install catalog entries require one exact install bundle")
            if self.material_descriptor is not None:
                raise _fail("install catalog entries cannot carry load authority")
            if (
                self.install_bundle.descriptor.capability_id,
                self.install_bundle.descriptor.kind,
            ) != (self.capability_id, self.kind):
                raise _fail("install catalog planning authority crosses capability identity")
        candidate = self.static_candidate()
        if self.catalog_entry_claim_digest != catalog_candidate_entry_claim_digest(candidate):
            raise _fail("catalog entry claim digest does not match its static entry")

    def static_candidate(self) -> CapabilityCandidate:
        bundle = self.install_bundle
        return CapabilityCandidate(
            capability_id=self.capability_id,
            kind=self.kind,
            name=self.name,
            source_digest=self.source_digest,
            normalized_score_ppm=0,
            matching_signals=(),
            reason_codes=("reviewed-entry",),
            actionability=self.actionability,
            install_descriptor_digest=(
                bundle.descriptor.descriptor_digest if bundle is not None else None
            ),
            install_plan_digest=(bundle.descriptor.plan_digest if bundle is not None else None),
            equivalence_key=self.equivalence_key,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EligibleCatalogLayer:
    """One exact-hash catalog layer; authentication is owned by its caller."""

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
    entries: tuple[EligibleCatalogEntry, ...]

    def __post_init__(self) -> None:
        _token(self.authority_id, "catalog authority_id")
        if self.authority_kind not in _AUTHORITY_KINDS:
            raise _fail("catalog authority_kind is unsupported")
        if self.catalog_layer_kind not in _LAYER_KINDS:
            raise _fail("catalog layer kind is unsupported")
        expected_layer = "ctx" if self.authority_kind == "ctx-release" else self.authority_kind
        if self.catalog_layer_kind != expected_layer:
            raise _fail("catalog authority cannot cross layer kind")
        _digest(self.authority_digest, "catalog authority_digest")
        if type(self.sequence) is not int or not 1 <= self.sequence <= 2**63 - 1:
            raise _fail("catalog sequence is invalid")
        if self.candidate_projection_version != ELIGIBLE_ENTRY_PROJECTION:
            raise _fail("catalog candidate projection is unsupported")
        for field_name in (
            "catalog_namespace_digest",
            "catalog_provenance_digest",
            "catalog_artifact_sha256",
            "catalog_retrieval_snapshot_digest",
        ):
            _digest(getattr(self, field_name), field_name)
        _optional_digest(self.material_snapshot_digest, "material_snapshot_digest")
        _optional_digest(self.installation_snapshot_digest, "installation_snapshot_digest")
        if not isinstance(self.entries, tuple) or len(self.entries) > MAX_ELIGIBLE_CATALOG_ENTRIES:
            raise _fail("catalog entries must be a bounded immutable tuple")
        order = tuple((entry.capability_id, entry.actionability) for entry in self.entries)
        if order != tuple(sorted(order)):
            raise _fail("catalog entries must use canonical capability order")
        if len(set(order)) != len(order):
            raise _fail("catalog entries contain duplicate capability actionability")


def _parse_install_bundle(value: object, index: int) -> InstallPlanningBundle:
    mapping = _closed(value, _INSTALL_BUNDLE_FIELDS, f"entries[{index}].install_bundle")
    try:
        return InstallPlanningBundle(
            descriptor=InstallPlanDescriptor.from_dict(mapping["descriptor"]),  # type: ignore[arg-type]
            result_material=MaterialIdentity.from_dict(mapping["result_material"]),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise _fail(f"entries[{index}].install_bundle is invalid") from exc


def _parse_entry(
    value: object,
    index: int,
    *,
    artifact_sha256: str,
) -> EligibleCatalogEntry:
    mapping = _closed(value, _ENTRY_FIELDS, f"entries[{index}]")
    actionability = mapping["actionability"]
    material: MaterialDescriptor | None = None
    install: InstallPlanningBundle | None = None
    try:
        if mapping["material_descriptor"] is not None:
            material = MaterialDescriptor.from_dict(mapping["material_descriptor"])  # type: ignore[arg-type]
        if mapping["install_bundle"] is not None:
            install = _parse_install_bundle(mapping["install_bundle"], index)
        capability_id = mapping["capability_id"]
        kind = mapping["kind"]
        name = mapping["name"]
        equivalence_key = mapping["equivalence_key"]
        static_mapping = _entry_digest_mapping(
            actionability=actionability,  # type: ignore[arg-type]
            capability_id=capability_id,  # type: ignore[arg-type]
            equivalence_key=equivalence_key,  # type: ignore[arg-type]
            install_bundle=install,
            kind=kind,  # type: ignore[arg-type]
            material_descriptor=material,
            name=name,  # type: ignore[arg-type]
        )
        source_digest = _canonical_digest(
            {
                "catalog_artifact_sha256": artifact_sha256,
                "entry": static_mapping,
                "schema": "ctx.benefit-eligible-entry-source-v1",
            }
        )
        bundle = install
        candidate = CapabilityCandidate(
            capability_id=capability_id,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            name=name,  # type: ignore[arg-type]
            source_digest=source_digest,
            normalized_score_ppm=0,
            matching_signals=(),
            reason_codes=("reviewed-entry",),
            actionability=actionability,  # type: ignore[arg-type]
            install_descriptor_digest=(
                bundle.descriptor.descriptor_digest if bundle is not None else None
            ),
            install_plan_digest=(bundle.descriptor.plan_digest if bundle is not None else None),
            equivalence_key=equivalence_key,  # type: ignore[arg-type]
        )
        return EligibleCatalogEntry(
            capability_id=candidate.capability_id,
            kind=candidate.kind,
            name=candidate.name,
            actionability=candidate.actionability,
            equivalence_key=candidate.equivalence_key,
            material_descriptor=material,
            install_bundle=install,
            source_digest=source_digest,
            catalog_entry_claim_digest=catalog_candidate_entry_claim_digest(candidate),
        )
    except (EligibleCatalogError, PlannerValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, EligibleCatalogError):
            raise
        raise _fail(f"entries[{index}] is invalid") from exc


def load_eligible_catalog_layer_bytes(
    value: bytes,
    expected_sha256: str,
) -> EligibleCatalogLayer:
    """Load one canonical catalog layer from already-owned exact bytes.

    ``expected_sha256`` is a caller assertion, not a trust root.  The production
    release factory must supply a code-owned pin.
    """

    artifact_sha256 = _digest(expected_sha256, "expected catalog SHA-256")
    root = _closed(_decode_catalog(value, artifact_sha256), _ROOT_FIELDS, "catalog root")
    if root["schema"] != ELIGIBLE_CATALOG_SCHEMA:
        raise _fail("catalog schema is unsupported")
    authority = _closed(root["authority"], _AUTHORITY_FIELDS, "catalog authority")
    bindings = _closed(root["bindings"], _BINDING_FIELDS, "catalog bindings")
    raw_entries = root["entries"]
    if not isinstance(raw_entries, list) or len(raw_entries) > MAX_ELIGIBLE_CATALOG_ENTRIES:
        raise _fail("catalog entries must be a bounded array")
    entries = tuple(
        _parse_entry(item, index, artifact_sha256=artifact_sha256)
        for index, item in enumerate(raw_entries)
    )
    retrieval_digest = _canonical_digest(
        {
            "artifact_sha256": artifact_sha256,
            "claims": [entry.catalog_entry_claim_digest for entry in entries],
            "projection": bindings["candidate_projection_version"],
            "schema": "ctx.benefit-eligible-retrieval-v1",
        }
    )
    try:
        return EligibleCatalogLayer(
            authority_id=_token(authority["authority_id"], "catalog authority_id"),
            authority_kind=authority["authority_kind"],  # type: ignore[arg-type]
            authority_digest=_digest(authority["authority_digest"], "authority_digest"),
            catalog_layer_kind=authority["catalog_layer_kind"],  # type: ignore[arg-type]
            sequence=authority["sequence"],  # type: ignore[arg-type]
            candidate_projection_version=bindings["candidate_projection_version"],  # type: ignore[arg-type]
            catalog_namespace_digest=_digest(
                bindings["catalog_namespace_digest"], "catalog_namespace_digest"
            ),
            catalog_provenance_digest=_digest(
                bindings["catalog_provenance_digest"], "catalog_provenance_digest"
            ),
            catalog_artifact_sha256=artifact_sha256,
            catalog_retrieval_snapshot_digest=retrieval_digest,
            material_snapshot_digest=_optional_digest(
                bindings["material_snapshot_digest"], "material_snapshot_digest"
            ),
            installation_snapshot_digest=_optional_digest(
                bindings["installation_snapshot_digest"],
                "installation_snapshot_digest",
            ),
            entries=entries,
        )
    except (EligibleCatalogError, TypeError, ValueError) as exc:
        if isinstance(exc, EligibleCatalogError):
            raise
        raise _fail("catalog layer is invalid") from exc


def _authority_key(value: ReviewedBenefitProfiles | EligibleCatalogLayer) -> tuple[object, ...]:
    return (
        value.catalog_layer_kind,
        value.authority_kind,
        value.authority_id,
        value.sequence,
        value.authority_digest,
    )


def _layer_matches_authority(
    layer: EligibleCatalogLayer,
    authority: ReviewedBenefitProfiles,
) -> bool:
    return _authority_key(layer) == _authority_key(authority) and all(
        getattr(layer, field_name) == getattr(authority, field_name)
        for field_name in (
            "candidate_projection_version",
            "catalog_namespace_digest",
            "catalog_provenance_digest",
            "catalog_artifact_sha256",
            "catalog_retrieval_snapshot_digest",
            "material_snapshot_digest",
            "installation_snapshot_digest",
        )
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReviewedEntry:
    layer: EligibleCatalogLayer
    entry: EligibleCatalogEntry
    authority: ReviewedBenefitProfiles
    profile: ReviewedBenefitProfile


@dataclass(frozen=True, slots=True, kw_only=True)
class _PrefilteredCandidate:
    reviewed: _ReviewedEntry
    presentation: CapabilityCandidate
    claim: EligibleCatalogClaim
    eligibility: QueryCapabilityEligibility
    benefit: BenefitCandidate
    assessment: CandidateAssessment


class _PrefilteredHostPolicyAuthority:
    """Exact retained facts while preserving the upstream host-policy pin."""

    __slots__ = ("_records", "host_policy_snapshot_digest")

    def __init__(
        self,
        *,
        upstream_snapshot_digest: str,
        values: Sequence[_PrefilteredCandidate],
    ) -> None:
        self.host_policy_snapshot_digest = _digest(
            upstream_snapshot_digest,
            "upstream host policy snapshot digest",
        )
        self._records = MappingProxyType(
            {
                (
                    capability_presentation_digest(value.presentation),
                    eligible_catalog_claim_digest(value.claim),
                    value.claim.catalog_entry_claim_digest,
                ): value.eligibility
                for value in values
            }
        )

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility | None:
        return self._records.get(
            (
                capability_presentation_digest(presentation),
                eligible_catalog_claim_digest(claim),
                claim.catalog_entry_claim_digest,
            )
        )


def _host_policy_digest(authority: QueryHostPolicyAuthority) -> str:
    try:
        return _digest(
            authority.host_policy_snapshot_digest,
            "host policy snapshot digest",
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


def _direct_contribution(policy: NetBenefitPolicy, assessment: CandidateAssessment) -> int:
    return (
        assessment.individual_net_benefit_u
        + len(assessment.candidate.coverage_keys) * policy.new_coverage_bonus_u_per_key
    )


def _policy_prefilter(
    values: Sequence[_PrefilteredCandidate],
    policy: NetBenefitPolicy,
    requested_limit: int,
) -> tuple[_PrefilteredCandidate, ...]:
    by_id = {value.presentation.capability_id: value for value in values}
    ranked: list[tuple[int, _PrefilteredCandidate]] = []
    threshold = policy.minimum_marginal_net_benefit_u
    for value in values:
        direct = _direct_contribution(policy, value.assessment)
        reciprocal_complements = 0
        for peer_id in value.benefit.complements:
            peer = by_id.get(peer_id)
            if (
                peer is not None
                and value.presentation.capability_id in peer.benefit.complements
                and peer_id not in value.benefit.conflicts
                and value.presentation.capability_id not in peer.benefit.conflicts
            ):
                reciprocal_complements += 1
        # This is a conservative upper bound on leave-one-out contribution in
        # any selectable set.  Ignoring overlap penalties may retain extra rows,
        # but never discards a candidate that needs several positive peers.
        upper_bound = (
            direct
            + min(
                reciprocal_complements,
                max(requested_limit - 1, 0),
            )
            * policy.complementarity_bonus_u
        )
        if upper_bound >= threshold:
            ranked.append((upper_bound, value))
    ranked.sort(
        key=lambda item: (
            -item[0],
            -item[1].assessment.individual_net_benefit_u,
            -item[1].presentation.normalized_score_ppm,
            item[1].presentation.capability_id,
            item[1].presentation.source_digest,
        )
    )
    ordered = tuple(value for _score, value in ranked)
    if len(ordered) <= MAX_CANDIDATES:
        return ordered

    # Collapse only rows that are mutually exclusive by equivalence and have
    # the same policy-visible interaction signature.  The first row dominates
    # later rows because ``ordered`` is already sorted by upper bound, direct
    # benefit, and relevance.  Complementary peers are never collapsed because
    # the policy may legally retain them together despite their equivalence key.
    equivalent_ids: dict[str, set[str]] = {}
    inbound_complements: dict[str, set[str]] = {}
    inbound_conflicts: dict[str, set[str]] = {}
    for value in ordered:
        key = value.benefit.equivalence_key
        if key is not None:
            equivalent_ids.setdefault(key, set()).add(value.presentation.capability_id)
        for peer_id in value.benefit.complements:
            if peer_id in by_id:
                inbound_complements.setdefault(peer_id, set()).add(value.presentation.capability_id)
        for peer_id in value.benefit.conflicts:
            if peer_id in by_id:
                inbound_conflicts.setdefault(peer_id, set()).add(value.presentation.capability_id)
    retained: list[_PrefilteredCandidate] = []
    seen_dominance_keys: set[tuple[object, ...]] = set()
    for value in ordered:
        benefit = value.benefit
        equivalence_key = benefit.equivalence_key
        if equivalence_key is None:
            retained.append(value)
            continue
        peers = equivalent_ids[equivalence_key]
        reciprocal_complement_ids = tuple(
            sorted(
                set(benefit.complements).intersection(
                    inbound_complements.get(benefit.capability_id, set())
                )
            )
        )
        effective_conflicts = tuple(
            sorted(
                set(benefit.conflicts).union(inbound_conflicts.get(benefit.capability_id, set()))
            )
        )
        has_equivalent_complement = bool(set(reciprocal_complement_ids).intersection(peers))
        if has_equivalent_complement:
            retained.append(value)
            continue
        dominance_key = (
            value.assessment.tier,
            equivalence_key,
            (
                benefit.coverage_keys
                if (policy.new_coverage_bonus_u_per_key or policy.overlap_penalty_u_per_key)
                else ()
            ),
            reciprocal_complement_ids if policy.complementarity_bonus_u else (),
            effective_conflicts,
        )
        if dominance_key in seen_dominance_keys:
            continue
        seen_dominance_keys.add(dominance_key)
        retained.append(value)
    if len(retained) > MAX_CANDIDATES:
        raise _fail("benefit-eligible query exceeds its exact bounded policy frontier")
    return tuple(retained)


class _EligibleQuerySource:
    def __init__(
        self,
        *,
        observation: WorkObservation,
        entries: tuple[_ReviewedEntry, ...],
        profiles: ReviewedBenefitAuthorities,
        policy: NetBenefitPolicy,
        host_policy: QueryHostPolicyAuthority,
        catalog_snapshot_digest: str,
        eligibility_snapshot_digest: str,
    ) -> None:
        self.catalog_snapshot_digest = catalog_snapshot_digest
        self.catalog_retrieval_snapshot_digest = profiles.catalog_retrieval_snapshot_digest
        self.catalog_namespace_digest = profiles.catalog_namespace_digest
        self.catalog_provenance_digest = profiles.catalog_provenance_digest
        self.catalog_artifact_sha256 = profiles.catalog_artifact_sha256
        self.candidate_projection_version = profiles.candidate_projection_version
        self.material_snapshot_digest = profiles.material_snapshot_digest
        self.installation_snapshot_digest = profiles.installation_snapshot_digest
        self.profile_snapshot_digest = profiles.profile_snapshot_digest
        self.authority_snapshot_digest = profiles.authority_snapshot_digest
        self.eligibility_snapshot_digest = eligibility_snapshot_digest
        self.calibration_digest = profiles.calibration_digest
        self.policy_digest = profiles.policy_digest
        self._observation = observation
        self._lock = threading.Lock()
        self._closed = False
        upstream_host_policy_snapshot_digest = _host_policy_digest(host_policy)
        prefiltered: list[_PrefilteredCandidate] = []
        available_actionability_by_id: dict[str, str] = {}
        observed = {*observation.signals, *observation.languages}
        suppressed = {*observation.baseline_capability_ids, *observation.rejected_capability_ids}
        if observation.requested_limit > 0:
            for reviewed in entries:
                profile = reviewed.profile
                entry = reviewed.entry
                if entry.capability_id in suppressed:
                    continue
                if not (
                    profile.source_trusted
                    and profile.security_approved
                    and profile.expected_task_benefit_ppm > 0
                    and profile.maximum_relevance_ppm >= policy.minimum_relevance_ppm
                    and profile.trust_ppm >= policy.minimum_trust_ppm
                ):
                    continue
                matches = tuple(
                    sorted(set(profile.match_policy.allowed_signals).intersection(observed))
                )
                reasons = profile.match_policy.allowed_reason_codes
                if not reasons:
                    raise _fail("reviewed eligible profile must provide a reason code")
                candidate = CapabilityCandidate(
                    capability_id=entry.capability_id,
                    kind=entry.kind,
                    name=entry.name,
                    source_digest=entry.source_digest,
                    normalized_score_ppm=profile.maximum_relevance_ppm,
                    matching_signals=matches,
                    reason_codes=reasons,
                    actionability=entry.actionability,
                    install_descriptor_digest=(
                        entry.install_bundle.descriptor.descriptor_digest
                        if entry.install_bundle is not None
                        else None
                    ),
                    install_plan_digest=(
                        entry.install_bundle.descriptor.plan_digest
                        if entry.install_bundle is not None
                        else None
                    ),
                    equivalence_key=entry.equivalence_key,
                )
                if not profile.match_policy.accepts(candidate, observation):
                    continue
                claim = EligibleCatalogClaim.create(reviewed.authority, presentation=candidate)
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
                previous_actionability = available_actionability_by_id.get(candidate.capability_id)
                if previous_actionability is not None:
                    raise _fail(
                        "host policy exposes multiple actionability variants for one capability"
                    )
                available_actionability_by_id[candidate.capability_id] = candidate.actionability
                benefit = _reviewed_query_benefit_candidate(
                    candidate,
                    profile,
                    reviewed.authority,
                    first_eligibility,
                )
                assessment = policy.assess(benefit)
                if assessment.tier == "ineligible":
                    continue
                prefiltered.append(
                    _PrefilteredCandidate(
                        reviewed=reviewed,
                        presentation=candidate,
                        claim=claim,
                        eligibility=first_eligibility,
                        benefit=benefit,
                        assessment=assessment,
                    )
                )
        if _host_policy_digest(host_policy) != upstream_host_policy_snapshot_digest:
            raise _fail("host policy authority drift detected")
        retained = _policy_prefilter(prefiltered, policy, observation.requested_limit)
        self._presentations = tuple(value.presentation for value in retained)
        self._claims = {
            capability_presentation_digest(value.presentation): value.claim for value in retained
        }
        self.host_policy_authority = _PrefilteredHostPolicyAuthority(
            upstream_snapshot_digest=upstream_host_policy_snapshot_digest,
            values=retained,
        )

    def retrieve(self, observation: WorkObservation) -> tuple[CapabilityCandidate, ...]:
        with self._lock:
            if self._closed:
                raise PlannerValidationError("eligible query source is closed")
            if observation != self._observation:
                raise PlannerValidationError("eligible query source observation mismatch")
            return self._presentations

    def entry_claim(self, presentation: CapabilityCandidate) -> EligibleCatalogClaim:
        with self._lock:
            if self._closed:
                raise PlannerValidationError("eligible query source is closed")
            try:
                return self._claims[capability_presentation_digest(presentation)]
            except (KeyError, TypeError, ValueError):
                raise PlannerValidationError(
                    "presentation is not a member of the eligible query source"
                ) from None

    def close(self) -> None:
        with self._lock:
            self._closed = True


class _FrozenCatalogMaterialAuthority:
    """Exact-presentation material authority derived from one prepared closure."""

    __slots__ = (
        "_catalog_namespace_digest",
        "_catalog_snapshot_digest",
        "_closed",
        "_lock",
        "_records",
        "material_snapshot_digest",
    )

    def __init__(
        self,
        *,
        records: Mapping[
            str,
            tuple[CapabilityCandidate, MaterialDescriptor, str],
        ],
        catalog_namespace_digest: str,
        catalog_snapshot_digest: str,
        material_snapshot_digest: str,
    ) -> None:
        self._records = MappingProxyType(dict(records))
        self._catalog_namespace_digest = _digest(
            catalog_namespace_digest,
            "material authority catalog_namespace_digest",
        )
        self._catalog_snapshot_digest = _digest(
            catalog_snapshot_digest,
            "material authority catalog_snapshot_digest",
        )
        self.material_snapshot_digest = _digest(
            material_snapshot_digest,
            "material authority snapshot digest",
        )
        self._closed = False
        self._lock = threading.Lock()

    def load_bundle(
        self,
        presentation: CapabilityCandidate,
    ) -> CatalogLoadPlanningBundle | None:
        with self._lock:
            if self._closed:
                raise PlannerValidationError("catalog material authority is closed")
            if not isinstance(presentation, CapabilityCandidate):
                raise TypeError("presentation must be a CapabilityCandidate")
            record = self._records.get(capability_presentation_digest(presentation))
            if record is None or record[0] != presentation:
                return None
            descriptor = record[1]
            return CatalogLoadPlanningBundle(
                presentation=presentation,
                catalog_identity=CatalogCapabilityIdentity.create(
                    capability_id=presentation.capability_id,
                    kind=presentation.kind,
                    catalog_namespace_digest=self._catalog_namespace_digest,
                ),
                descriptor=descriptor,
                catalog_snapshot_digest=self._catalog_snapshot_digest,
                material_snapshot_digest=self.material_snapshot_digest,
                authority_material_snapshot_digest=record[2],
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True


class _FrozenInstallBundleAuthority:
    """Capability-bound install authority compiled from the prepared catalog."""

    __slots__ = ("_closed", "_lock", "_records", "installation_snapshot_digest")

    def __init__(
        self,
        *,
        records: Mapping[tuple[str, str], tuple[InstallPlanningBundle, str]],
        installation_snapshot_digest: str,
    ) -> None:
        self._records = MappingProxyType(dict(records))
        self.installation_snapshot_digest = _digest(
            installation_snapshot_digest,
            "install authority snapshot digest",
        )
        self._closed = False
        self._lock = threading.Lock()

    def describe_bundle(
        self,
        capability_id: str,
        kind: str,
    ) -> InstallPlanningBundle | None:
        with self._lock:
            if self._closed:
                raise PlannerValidationError("catalog install authority is closed")
            try:
                identity = validate_capability_identity(capability_id, kind)
            except ValueError as exc:
                raise ValueError("install authority capability identity is invalid") from exc
            record = self._records.get(identity)
            if record is None:
                return None
            bundle, authority_snapshot_digest = record
            return AuthorityBoundInstallPlanningBundle(
                descriptor=bundle.descriptor,
                result_material=bundle.result_material,
                authority_installation_snapshot_digest=authority_snapshot_digest,
                installation_snapshot_digest=self.installation_snapshot_digest,
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True


class PreparedEligibleCatalogQuery:
    """Sealed query closure plus the exact planning-authority views it needs."""

    closure: QueryBenefitClosure
    material_authority: _FrozenCatalogMaterialAuthority | None
    install_authority: _FrozenInstallBundleAuthority | None
    _closed: bool
    _lock: threading.Lock

    __slots__ = ("closure", "install_authority", "material_authority", "_closed", "_lock")

    def __init__(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        raise TypeError("prepared queries must be created by the catalog factory")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        closure: QueryBenefitClosure,
        material_authority: _FrozenCatalogMaterialAuthority | None,
        install_authority: _FrozenInstallBundleAuthority | None,
    ) -> PreparedEligibleCatalogQuery:
        if factory_token is not _PREPARED_QUERY_FACTORY_TOKEN:
            raise TypeError("prepared queries must be created by the catalog factory")
        if not isinstance(closure, QueryBenefitClosure):
            raise TypeError("prepared query closure is invalid")
        candidates = closure.source.retrieve(closure.observation)
        load_candidates = tuple(value for value in candidates if value.actionability == "load")
        install_candidates = tuple(
            value for value in candidates if value.actionability == "install"
        )
        if bool(load_candidates) != (material_authority is not None):
            raise _fail("prepared query load authority does not close over its candidates")
        if bool(install_candidates) != (install_authority is not None):
            raise _fail("prepared query install authority does not close over its candidates")
        if material_authority is not None and any(
            material_authority.load_bundle(candidate) is None for candidate in load_candidates
        ):
            raise _fail("prepared query has an incomplete load authority")
        if install_authority is not None and any(
            install_authority.describe_bundle(candidate.capability_id, candidate.kind) is None
            for candidate in install_candidates
        ):
            raise _fail("prepared query has an incomplete install authority")
        instance = object.__new__(cls)
        instance.closure = closure
        instance.material_authority = material_authority
        instance.install_authority = install_authority
        instance._closed = False
        instance._lock = threading.Lock()
        return instance

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.closure.source.close()
            if self.material_authority is not None:
                self.material_authority.close()
            if self.install_authority is not None:
                self.install_authority.close()


class ReviewedQueryCatalog:
    """Immutable reviewed catalog that can prepare exact query-scoped closures."""

    __slots__ = (
        "_catalog_snapshot_digest",
        "_closed",
        "_eligibility_snapshot_digest",
        "_entries",
        "_lock",
        "_policy",
        "_profiles",
        "vocabulary",
    )

    def __init__(
        self,
        *,
        entries: tuple[_ReviewedEntry, ...],
        artifact_sha256s: tuple[str, ...],
        profiles: ReviewedBenefitAuthorities,
        policy: NetBenefitPolicy,
    ) -> None:
        self._entries = entries
        self._profiles = profiles
        self._policy = policy
        self._catalog_snapshot_digest = _canonical_digest(
            {
                "artifacts": list(artifact_sha256s),
                "policy_digest": policy.policy_digest,
                "profile_snapshot_digest": profiles.profile_snapshot_digest,
                "schema": "ctx.reviewed-query-catalog-v1",
            }
        )
        self._eligibility_snapshot_digest = _canonical_digest(
            {
                "claims": [entry.entry.catalog_entry_claim_digest for entry in entries],
                "schema": "ctx.reviewed-query-eligibility-v1",
            }
        )
        vocabulary_signals = sorted(
            {
                signal
                for value in entries
                for signal in value.profile.match_policy.allowed_signals
                if signal not in _LANGUAGE_SIGNALS
            }
        )
        self.vocabulary = AuthenticatedQueryVocabulary.create(
            signals=vocabulary_signals,
            catalog_namespace_digest=profiles.catalog_namespace_digest,
            graph_artifact_sha256=profiles.catalog_artifact_sha256,
        )
        self._closed = False
        self._lock = threading.Lock()

    def prepare_query(
        self,
        *,
        observation: WorkObservation,
        host_policy: QueryHostPolicyAuthority,
    ) -> PreparedEligibleCatalogQuery:
        if not isinstance(observation, WorkObservation):
            raise TypeError("observation must be a WorkObservation")
        with self._lock:
            if self._closed:
                raise _fail("reviewed query catalog is closed")
            entries = self._entries
            profiles = self._profiles
            policy = self._policy
            catalog_snapshot_digest = self._catalog_snapshot_digest
            eligibility_snapshot_digest = self._eligibility_snapshot_digest
        source = _EligibleQuerySource(
            observation=observation,
            entries=entries,
            profiles=profiles,
            policy=policy,
            host_policy=host_policy,
            catalog_snapshot_digest=catalog_snapshot_digest,
            eligibility_snapshot_digest=eligibility_snapshot_digest,
        )
        closure = prepare_query_benefit_closure(
            source=source,
            observation=observation,
            profiles=profiles,
            policy=policy,
            host_policy=source.host_policy_authority,
        )
        retained = closure.source.retrieve(observation)
        entry_by_identity = {
            (
                value.entry.capability_id,
                value.entry.source_digest,
                value.entry.actionability,
            ): value
            for value in entries
        }
        material_records: dict[
            str,
            tuple[CapabilityCandidate, MaterialDescriptor, str],
        ] = {}
        install_records: dict[
            tuple[str, str],
            tuple[InstallPlanningBundle, str],
        ] = {}
        try:
            for candidate in retained:
                reviewed = entry_by_identity.get(
                    (candidate.capability_id, candidate.source_digest, candidate.actionability)
                )
                if reviewed is None:
                    raise _fail("prepared closure contains an unknown catalog entry")
                entry = reviewed.entry
                if candidate.actionability == "load":
                    descriptor = entry.material_descriptor
                    authority_snapshot_digest = reviewed.layer.material_snapshot_digest
                    if descriptor is None or authority_snapshot_digest is None:
                        raise _fail("prepared load candidate lost material authority")
                    material_records[capability_presentation_digest(candidate)] = (
                        candidate,
                        descriptor,
                        authority_snapshot_digest,
                    )
                elif candidate.actionability == "install":
                    bundle = entry.install_bundle
                    authority_snapshot_digest = reviewed.layer.installation_snapshot_digest
                    if bundle is None or authority_snapshot_digest is None:
                        raise _fail("prepared install candidate lost install authority")
                    install_records[(candidate.capability_id, candidate.kind)] = (
                        bundle,
                        authority_snapshot_digest,
                    )
            material_authority = None
            if material_records:
                if closure.material_snapshot_digest is None:
                    raise _fail("prepared load candidates have no material snapshot")
                material_authority = _FrozenCatalogMaterialAuthority(
                    records=material_records,
                    catalog_namespace_digest=closure.catalog_namespace_digest,
                    catalog_snapshot_digest=closure.closure_snapshot_digest,
                    material_snapshot_digest=closure.material_snapshot_digest,
                )
            install_authority = None
            if install_records:
                if closure.installation_snapshot_digest is None:
                    raise _fail("prepared install candidates have no installation snapshot")
                install_authority = _FrozenInstallBundleAuthority(
                    records=install_records,
                    installation_snapshot_digest=closure.installation_snapshot_digest,
                )
            return PreparedEligibleCatalogQuery._create(
                factory_token=_PREPARED_QUERY_FACTORY_TOKEN,
                closure=closure,
                material_authority=material_authority,
                install_authority=install_authority,
            )
        except BaseException:
            closure.source.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True


def open_reviewed_query_catalog(
    *,
    layers: Sequence[EligibleCatalogLayer],
    profiles: ReviewedBenefitAuthorities,
    policy: NetBenefitPolicy,
) -> ReviewedQueryCatalog:
    """Join exact catalog entries to independently supplied reviewed profiles."""

    if isinstance(layers, (str, bytes, bytearray)) or not isinstance(layers, Sequence):
        raise TypeError("layers must be a bounded sequence")
    if not isinstance(profiles, ReviewedBenefitAuthorities):
        raise TypeError("profiles must be ReviewedBenefitAuthorities")
    if not isinstance(policy, NetBenefitPolicy):
        raise TypeError("policy must be a NetBenefitPolicy")
    try:
        frozen_layers = tuple(islice(iter(layers), 65))
    except Exception:
        raise _fail("layers must contain a bounded set of eligible catalog layers") from None
    if not 1 <= len(frozen_layers) <= 64 or not all(
        isinstance(layer, EligibleCatalogLayer) for layer in frozen_layers
    ):
        raise _fail("layers must contain a bounded set of eligible catalog layers")
    if tuple(sorted(frozen_layers, key=_authority_key)) != frozen_layers:
        raise _fail("eligible catalog layers must use canonical authority order")
    layer_by_key = {_authority_key(layer): layer for layer in frozen_layers}
    if len(layer_by_key) != len(frozen_layers):
        raise _fail("eligible catalog layers contain a duplicate authority")
    authority_by_key = {_authority_key(authority): authority for authority in profiles.authorities}
    if set(layer_by_key) != set(authority_by_key):
        raise _fail("catalog layers and reviewed authorities are not one-to-one")
    if policy.policy_digest != profiles.policy_digest:
        raise _fail("catalog policy does not match reviewed profiles")
    if policy.calibration_digest != profiles.calibration_digest:
        raise _fail("catalog calibration does not match reviewed profiles")

    reviewed_entries: list[_ReviewedEntry] = []
    seen_variants: set[tuple[str, str]] = set()
    matched_profiles: set[tuple[str, str, str]] = set()
    for key, layer in layer_by_key.items():
        authority = authority_by_key[key]
        if not _layer_matches_authority(layer, authority):
            raise _fail("catalog layer bindings do not match reviewed authority")
        for entry in layer.entries:
            variant = (entry.capability_id, entry.actionability)
            if variant in seen_variants:
                raise _fail("catalog layers contain a capability actionability collision")
            seen_variants.add(variant)
            profile = authority.profile_for(
                entry.static_candidate(),
                entry.catalog_entry_claim_digest,
            )
            if profile is None:
                raise _fail("catalog entry has no exact reviewed benefit profile")
            if (
                len(profile.match_policy.allowed_signals) > MAX_MATCHING_SIGNALS
                or not 1 <= len(profile.match_policy.allowed_reason_codes) <= MAX_REASON_CODES
            ):
                raise _fail("reviewed profile exceeds eligible presentation bounds")
            if entry.actionability == "load":
                descriptor = entry.material_descriptor
                if (
                    layer.material_snapshot_digest is None
                    or descriptor is None
                    or descriptor.schema_version != 2
                    or descriptor.provenance_digest != layer.material_snapshot_digest
                ):
                    raise _fail("load entry does not match its catalog material authority")
            if entry.actionability == "install":
                bundle = entry.install_bundle
                if (
                    layer.installation_snapshot_digest is None
                    or bundle is None
                    or bundle.descriptor.schema_version != 2
                    or bundle.descriptor.provenance_digest != layer.installation_snapshot_digest
                ):
                    raise _fail("install entry does not match its catalog install authority")
            matched_profiles.add(
                (authority.authority_id, profile.profile_id, profile.profile_digest)
            )
            reviewed_entries.append(
                _ReviewedEntry(
                    layer=layer,
                    entry=entry,
                    authority=authority,
                    profile=profile,
                )
            )
    all_profiles = {
        (authority.authority_id, profile.profile_id, profile.profile_digest)
        for authority in profiles.authorities
        for profile in authority.profiles
    }
    if matched_profiles != all_profiles:
        raise _fail("reviewed profiles and catalog entries are not one-to-one")
    reviewed_entries.sort(key=lambda value: (value.entry.capability_id, value.entry.actionability))
    return ReviewedQueryCatalog(
        entries=tuple(reviewed_entries),
        artifact_sha256s=tuple(layer.catalog_artifact_sha256 for layer in frozen_layers),
        profiles=profiles,
        policy=policy,
    )


__all__ = [
    "ELIGIBLE_CATALOG_SCHEMA",
    "ELIGIBLE_ENTRY_PROJECTION",
    "MAX_ELIGIBLE_CATALOG_BYTES",
    "MAX_ELIGIBLE_CATALOG_ENTRIES",
    "EligibleCatalogEntry",
    "EligibleCatalogError",
    "EligibleCatalogLayer",
    "PreparedEligibleCatalogQuery",
    "ReviewedQueryCatalog",
    "load_eligible_catalog_layer_bytes",
    "open_reviewed_query_catalog",
]
