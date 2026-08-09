from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import overload

import pytest

from ctx.engine.benefit import BenefitSelectionResult, NetBenefitPolicy
from ctx.engine.content import MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor, InstallPlanningBundle
from ctx.engine.planner import CapabilityCandidate, WorkObservation
from ctx.engine.planning_v3 import AuthenticatedNetBenefitPlanner
from ctx.engine.replay import PlanningContext, StructuredSurrogate
from ctx.runtime.authenticated_benefit import capability_presentation_digest
from ctx.runtime.benefit_closure import (
    EligibleCatalogClaim,
    QueryCapabilityEligibility,
    ReviewedBenefitAuthorities,
    eligible_catalog_claim_digest,
    load_reviewed_benefit_profiles_bytes,
)
from ctx.runtime.eligible_catalog import (
    EligibleCatalogLayer,
    EligibleCatalogError,
    PreparedEligibleCatalogQuery,
    load_eligible_catalog_layer_bytes,
    open_reviewed_query_catalog,
)
from ctx.runtime.planning_v3 import (
    AuthenticatedReplayDecisionPlannerV3,
    AuthorityBoundInstallPlanningBundle,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


CALIBRATION_DIGEST = _digest("eligible-catalog-calibration")
POLICY = NetBenefitPolicy(
    calibration_digest=CALIBRATION_DIGEST,
    minimum_relevance_ppm=1,
    context_token_cost_u=1,
)


def _aggregate_binding(field_name: str, value: str) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "field": field_name,
                "schema": "ctx.reviewed-authority-bindings-v1",
                "values": [value],
            }
        )
    ).hexdigest()


def _catalog_manifest(
    *,
    actionability: str = "manual",
    material_snapshot_digest: str | None = None,
    installation_snapshot_digest: str | None = None,
    material_descriptor: MaterialDescriptor | None = None,
    install_bundle: InstallPlanningBundle | None = None,
) -> dict[str, object]:
    return {
        "authority": {
            "authority_digest": _digest("ctx-release-authority"),
            "authority_id": "ctx-release",
            "authority_kind": "ctx-release",
            "catalog_layer_kind": "ctx",
            "sequence": 1,
        },
        "bindings": {
            "candidate_projection_version": "ctx.benefit-eligible-entry-v1",
            "catalog_namespace_digest": _digest("ctx-catalog-namespace"),
            "catalog_provenance_digest": _digest("ctx-catalog-provenance"),
            "installation_snapshot_digest": installation_snapshot_digest,
            "material_snapshot_digest": material_snapshot_digest,
        },
        "entries": [
            {
                "actionability": actionability,
                "capability_id": "skill:ctx-python-testing",
                "equivalence_key": None,
                "install_bundle": (
                    None
                    if install_bundle is None
                    else {
                        "descriptor": install_bundle.descriptor.to_dict(),
                        "result_material": install_bundle.result_material.to_dict(),
                    }
                ),
                "kind": "skill",
                "material_descriptor": (
                    None if material_descriptor is None else material_descriptor.to_dict()
                ),
                "name": "ctx-python-testing",
            }
        ],
        "schema": "ctx.benefit-eligible-catalog-v1",
    }


def _reviewed_profiles_body(layer) -> bytes:  # type: ignore[no-untyped-def]
    entry = layer.entries[0]
    manifest = {
        "authority": {
            "authority_digest": layer.authority_digest,
            "authority_id": layer.authority_id,
            "authority_kind": layer.authority_kind,
            "catalog_layer_kind": layer.catalog_layer_kind,
            "sequence": layer.sequence,
        },
        "bindings": {
            "calibration_digest": CALIBRATION_DIGEST,
            "candidate_projection_version": layer.candidate_projection_version,
            "catalog_artifact_sha256": layer.catalog_artifact_sha256,
            "catalog_namespace_digest": layer.catalog_namespace_digest,
            "catalog_provenance_digest": layer.catalog_provenance_digest,
            "catalog_retrieval_snapshot_digest": layer.catalog_retrieval_snapshot_digest,
            "installation_snapshot_digest": layer.installation_snapshot_digest,
            "material_snapshot_digest": layer.material_snapshot_digest,
            "policy_digest": POLICY.policy_digest,
        },
        "profiles": [
            {
                "actionability": entry.actionability,
                "capability_id": entry.capability_id,
                "catalog_entry_claim_digest": entry.catalog_entry_claim_digest,
                "complements": [],
                "conflicts": [],
                "costs": {
                    "approval_prompts": 0,
                    "child_agent_units": 0,
                    "context_tokens": 120,
                    "credential_burden_units": 0,
                    "permission_burden_units": 0,
                    "process_units": 0,
                    "runtime_millis": 0,
                    "tool_schema_tokens": 0,
                },
                "coverage_keys": ["python-testing"],
                "expected_task_benefit_ppm": 500_000,
                "kind": entry.kind,
                "match_policy": {
                    "allowed_equivalence_keys": [],
                    "allowed_reason_codes": ["reviewed-match"],
                    "allowed_signals": ["python", "testing"],
                    "minimum_matching_signals": 2,
                    "minimum_non_language_matching_signals": 1,
                    "required_any_signals": ["testing"],
                },
                "maximum_relevance_ppm": 700_000,
                "name": entry.name,
                "profile_id": "profile-ctx-python-testing",
                "review_basis_digest": _digest("review-basis:ctx-python-testing"),
                "security_approved": True,
                "source_trusted": True,
                "trust_ppm": 900_000,
            }
        ],
        "schema": "ctx.reviewed-benefit-profiles-v2",
    }
    return _canonical_bytes(manifest)


@dataclass
class _HostPolicy:
    host_policy_snapshot_digest: str = _digest("eligible-catalog-host-policy")

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility:
        return QueryCapabilityEligibility(
            presentation_digest=capability_presentation_digest(presentation),
            catalog_entry_claim_digest=claim.catalog_entry_claim_digest,
            catalog_claim_digest=eligible_catalog_claim_digest(claim),
            available=True,
            permissions_allowed=True,
            credentials_available=True,
        )


@dataclass
class _AuditStore:
    results: dict[str, BenefitSelectionResult]

    def store(self, result: BenefitSelectionResult) -> str:
        self.results[result.result_digest] = result
        return result.result_digest


def _open_catalog():  # type: ignore[no-untyped-def]
    catalog_body = _canonical_bytes(_catalog_manifest())
    layer = load_eligible_catalog_layer_bytes(
        catalog_body,
        hashlib.sha256(catalog_body).hexdigest(),
    )
    profiles_body = _reviewed_profiles_body(layer)
    profiles = ReviewedBenefitAuthorities.create(
        (
            load_reviewed_benefit_profiles_bytes(
                profiles_body,
                hashlib.sha256(profiles_body).hexdigest(),
            ),
        )
    )
    return layer, open_reviewed_query_catalog(
        layers=(layer,),
        profiles=profiles,
        policy=POLICY,
    )


def test_exact_catalog_profile_join_prepares_one_sealed_query() -> None:
    layer, catalog = _open_catalog()
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())

    candidates = prepared.closure.source.retrieve(observation)
    assert tuple(candidate.capability_id for candidate in candidates) == (
        "skill:ctx-python-testing",
    )
    assert candidates[0].matching_signals == ("python", "testing")
    assert candidates[0].reason_codes == ("reviewed-match",)
    assert candidates[0].source_digest == layer.entries[0].source_digest
    assert prepared.material_authority is None
    assert prepared.install_authority is None
    assert catalog.vocabulary.signals == ("testing",)

    prepared.close()
    catalog.close()
    with pytest.raises(EligibleCatalogError, match="closed"):
        catalog.prepare_query(observation=observation, host_policy=_HostPolicy())


def test_catalog_bytes_are_closed_canonical_and_exact_hash_bound() -> None:
    body = _canonical_bytes(_catalog_manifest())

    with pytest.raises(EligibleCatalogError, match="SHA-256"):
        load_eligible_catalog_layer_bytes(body, _digest("wrong"))

    manifest = _catalog_manifest()
    manifest["description"] = "graph prose is not catalog authority"
    mutated = _canonical_bytes(manifest)
    with pytest.raises(EligibleCatalogError, match="declared fields"):
        load_eligible_catalog_layer_bytes(mutated, hashlib.sha256(mutated).hexdigest())

    noncanonical = json.dumps(_catalog_manifest()).encode("utf-8")
    with pytest.raises(EligibleCatalogError, match="canonical"):
        load_eligible_catalog_layer_bytes(
            noncanonical,
            hashlib.sha256(noncanonical).hexdigest(),
        )


def _open_actionable_catalog(actionability: str):  # type: ignore[no-untyped-def]
    capability_id = "skill:ctx-python-testing"
    kind = "skill"
    material_identity = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"{actionability}-content"),
        content_bytes=240,
    )
    raw_material_snapshot = _digest("raw-material-snapshot")
    raw_installation_snapshot = _digest("raw-installation-snapshot")
    material_descriptor = None
    install_bundle = None
    if actionability == "load":
        material_descriptor = MaterialDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            actionability="load",
            content_sha256=material_identity.content_sha256,
            content_bytes=material_identity.content_bytes,
            estimated_tokens=60,
            provenance_digest=raw_material_snapshot,
            material_identity_digest=material_identity.identity_digest,
        )
    else:
        descriptor = InstallPlanDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            installer_id="ctx-skill-cas",
            plan_digest=_digest("install-plan"),
            provenance_digest=raw_installation_snapshot,
            result_material_identity_digest=material_identity.identity_digest,
        )
        install_bundle = InstallPlanningBundle(
            descriptor=descriptor,
            result_material=material_identity,
        )
    body = _canonical_bytes(
        _catalog_manifest(
            actionability=actionability,
            material_snapshot_digest=(raw_material_snapshot if actionability == "load" else None),
            installation_snapshot_digest=(
                raw_installation_snapshot if actionability == "install" else None
            ),
            material_descriptor=material_descriptor,
            install_bundle=install_bundle,
        )
    )
    layer = load_eligible_catalog_layer_bytes(body, hashlib.sha256(body).hexdigest())
    profiles_body = _reviewed_profiles_body(layer)
    profile_manifest = json.loads(profiles_body)
    profile_manifest["profiles"][0]["actionability"] = actionability
    profile_manifest["profiles"][0]["catalog_entry_claim_digest"] = layer.entries[
        0
    ].catalog_entry_claim_digest
    profiles_body = _canonical_bytes(profile_manifest)
    profiles = ReviewedBenefitAuthorities.create(
        (
            load_reviewed_benefit_profiles_bytes(
                profiles_body,
                hashlib.sha256(profiles_body).hexdigest(),
            ),
        )
    )
    return layer, open_reviewed_query_catalog(
        layers=(layer,),
        profiles=profiles,
        policy=POLICY,
    )


def _actionability_variant_manifest() -> dict[str, object]:
    capability_id = "skill:ctx-python-testing"
    kind = "skill"
    raw_material_snapshot = _digest("variant-material-snapshot")
    raw_installation_snapshot = _digest("variant-installation-snapshot")
    material_identity = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest("variant-content"),
        content_bytes=240,
    )
    material_descriptor = MaterialDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        actionability="load",
        content_sha256=material_identity.content_sha256,
        content_bytes=material_identity.content_bytes,
        estimated_tokens=60,
        provenance_digest=raw_material_snapshot,
        material_identity_digest=material_identity.identity_digest,
    )
    install_bundle = InstallPlanningBundle(
        descriptor=InstallPlanDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            installer_id="ctx-skill-cas",
            plan_digest=_digest("variant-install-plan"),
            provenance_digest=raw_installation_snapshot,
            result_material_identity_digest=material_identity.identity_digest,
        ),
        result_material=material_identity,
    )
    manifest = _catalog_manifest()
    manifest["bindings"] = {
        "candidate_projection_version": "ctx.benefit-eligible-entry-v1",
        "catalog_namespace_digest": _digest("ctx-catalog-namespace"),
        "catalog_provenance_digest": _digest("ctx-catalog-provenance"),
        "installation_snapshot_digest": raw_installation_snapshot,
        "material_snapshot_digest": raw_material_snapshot,
    }
    manifest["entries"] = [
        {
            "actionability": "install",
            "capability_id": capability_id,
            "equivalence_key": None,
            "install_bundle": {
                "descriptor": install_bundle.descriptor.to_dict(),
                "result_material": install_bundle.result_material.to_dict(),
            },
            "kind": kind,
            "material_descriptor": None,
            "name": "ctx-python-testing",
        },
        {
            "actionability": "load",
            "capability_id": capability_id,
            "equivalence_key": None,
            "install_bundle": None,
            "kind": kind,
            "material_descriptor": material_descriptor.to_dict(),
            "name": "ctx-python-testing",
        },
    ]
    return manifest


def _open_actionability_variant_catalog():  # type: ignore[no-untyped-def]
    body = _canonical_bytes(_actionability_variant_manifest())
    layer = load_eligible_catalog_layer_bytes(body, hashlib.sha256(body).hexdigest())
    profile_manifest = json.loads(_reviewed_profiles_body(layer))
    prototype = profile_manifest["profiles"][0]
    profile_manifest["profiles"] = []
    for entry in layer.entries:
        profile = deepcopy(prototype)
        profile["actionability"] = entry.actionability
        profile["catalog_entry_claim_digest"] = entry.catalog_entry_claim_digest
        profile["profile_id"] = f"profile-{entry.name}-{entry.actionability}"
        profile["review_basis_digest"] = _digest(
            f"review:{entry.capability_id}:{entry.actionability}"
        )
        profile_manifest["profiles"].append(profile)
    profiles_body = _canonical_bytes(profile_manifest)
    profiles = ReviewedBenefitAuthorities.create(
        (
            load_reviewed_benefit_profiles_bytes(
                profiles_body,
                hashlib.sha256(profiles_body).hexdigest(),
            ),
        )
    )
    return layer, open_reviewed_query_catalog(
        layers=(layer,),
        profiles=profiles,
        policy=POLICY,
    )


@dataclass
class _ActionabilityHostPolicy:
    selected_actionability: str
    host_policy_snapshot_digest: str = _digest("variant-host-policy")

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility:
        return QueryCapabilityEligibility(
            presentation_digest=capability_presentation_digest(presentation),
            catalog_entry_claim_digest=claim.catalog_entry_claim_digest,
            catalog_claim_digest=eligible_catalog_claim_digest(claim),
            available=presentation.actionability == self.selected_actionability,
            permissions_allowed=True,
            credentials_available=True,
        )


def test_catalog_accepts_distinct_install_and_load_variants_for_one_capability() -> None:
    layer, catalog = _open_actionability_variant_catalog()

    assert tuple((entry.capability_id, entry.actionability) for entry in layer.entries) == (
        ("skill:ctx-python-testing", "install"),
        ("skill:ctx-python-testing", "load"),
    )

    catalog.close()


def test_catalog_layer_rejects_duplicate_capability_actionability_pair() -> None:
    manifest = _actionability_variant_manifest()
    entries = manifest["entries"]
    assert isinstance(entries, list)
    entries.insert(1, deepcopy(entries[0]))
    body = _canonical_bytes(manifest)

    with pytest.raises(EligibleCatalogError, match="capability actionability"):
        load_eligible_catalog_layer_bytes(body, hashlib.sha256(body).hexdigest())


def test_catalog_layers_reject_duplicate_capability_actionability_pair() -> None:
    ctx_body = _canonical_bytes(_catalog_manifest())
    ctx_layer = load_eligible_catalog_layer_bytes(
        ctx_body,
        hashlib.sha256(ctx_body).hexdigest(),
    )
    user_manifest = _catalog_manifest()
    user_manifest["authority"] = {
        "authority_digest": _digest("user-authority"),
        "authority_id": "user-local",
        "authority_kind": "user",
        "catalog_layer_kind": "user",
        "sequence": 1,
    }
    user_manifest["bindings"] = {
        "candidate_projection_version": "ctx.benefit-eligible-entry-v1",
        "catalog_namespace_digest": _digest("user-catalog-namespace"),
        "catalog_provenance_digest": _digest("user-catalog-provenance"),
        "installation_snapshot_digest": None,
        "material_snapshot_digest": None,
    }
    user_body = _canonical_bytes(user_manifest)
    user_layer = load_eligible_catalog_layer_bytes(
        user_body,
        hashlib.sha256(user_body).hexdigest(),
    )
    profiles = ReviewedBenefitAuthorities.create(
        (
            load_reviewed_benefit_profiles_bytes(
                _reviewed_profiles_body(ctx_layer),
                hashlib.sha256(_reviewed_profiles_body(ctx_layer)).hexdigest(),
            ),
            load_reviewed_benefit_profiles_bytes(
                _reviewed_profiles_body(user_layer),
                hashlib.sha256(_reviewed_profiles_body(user_layer)).hexdigest(),
            ),
        )
    )

    with pytest.raises(EligibleCatalogError, match="capability actionability collision"):
        open_reviewed_query_catalog(
            layers=(ctx_layer, user_layer),
            profiles=profiles,
            policy=POLICY,
        )


@pytest.mark.parametrize("selected_actionability", ["install", "load"])
def test_host_policy_selects_one_variant_for_the_single_global_planner(
    selected_actionability: str,
) -> None:
    _layer, catalog = _open_actionability_variant_catalog()
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    prepared = catalog.prepare_query(
        observation=observation,
        host_policy=_ActionabilityHostPolicy(selected_actionability),
    )
    candidates = prepared.closure.source.retrieve(observation)
    assert tuple(
        (candidate.capability_id, candidate.actionability) for candidate in candidates
    ) == (("skill:ctx-python-testing", selected_actionability),)
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=AuthenticatedNetBenefitPlanner(
            policy=POLICY,
            audit_store=_AuditStore(results={}),
        ),
        source=prepared.closure.source,
        benefit_facts_port=prepared.closure.benefit_facts,
        material_port=prepared.material_authority,
        install_bundle_port=prepared.install_authority,
        planner_version="eligible-catalog-v1",
        catalog_namespace_digest=prepared.closure.catalog_namespace_digest,
    )
    decision = adapter(
        StructuredSurrogate.create(
            schema_id="ctx.observation.current-work",
            schema_version=1,
            value={
                "active_capability_ids": [],
                "baseline_capability_ids": [],
                "languages": ["python"],
                "rejected_capability_ids": [],
                "requested_limit": 5,
                "signals": ["testing"],
            },
        ),
        None,
        PlanningContext(
            planner_version="eligible-catalog-v1",
            catalog_snapshot_digest=adapter.catalog_snapshot_digest,
        ),
    )

    selections = decision.value["capabilities"]
    assert isinstance(selections, tuple)
    assert len(selections) == 1
    selection = selections[0]
    assert isinstance(selection, Mapping)
    assert selection["actionability"] == selected_actionability


def test_host_policy_exposing_both_variants_fails_before_planning() -> None:
    _layer, catalog = _open_actionability_variant_catalog()
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    with pytest.raises(EligibleCatalogError, match="multiple actionability variants"):
        catalog.prepare_query(observation=observation, host_policy=_HostPolicy())


def test_load_authority_is_frozen_from_the_same_catalog_bytes() -> None:
    _layer, catalog = _open_actionable_catalog("load")
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())
    candidate = prepared.closure.source.retrieve(observation)[0]

    assert prepared.material_authority is not None
    bundle = prepared.material_authority.load_bundle(candidate)
    assert bundle is not None
    assert bundle.presentation == candidate
    assert bundle.catalog_snapshot_digest == prepared.closure.closure_snapshot_digest
    assert bundle.material_snapshot_digest == prepared.closure.material_snapshot_digest
    assert bundle.descriptor.material_identity_digest is not None
    assert prepared.install_authority is None

    substituted = replace(candidate, normalized_score_ppm=candidate.normalized_score_ppm - 1)
    assert prepared.material_authority.load_bundle(substituted) is None


def test_install_authority_is_frozen_from_the_same_catalog_bytes() -> None:
    _layer, catalog = _open_actionable_catalog("install")
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())
    candidate = prepared.closure.source.retrieve(observation)[0]

    assert prepared.install_authority is not None
    bundle = prepared.install_authority.describe_bundle(candidate.capability_id, candidate.kind)
    assert bundle is not None
    assert bundle.descriptor.descriptor_digest == candidate.install_descriptor_digest
    assert bundle.descriptor.plan_digest == candidate.install_plan_digest
    assert bundle.descriptor.provenance_digest == _digest("raw-installation-snapshot")
    assert bundle.installation_snapshot_digest == prepared.closure.installation_snapshot_digest
    assert prepared.material_authority is None


@pytest.mark.parametrize("actionability", ["load", "install"])
def test_actionable_catalog_composes_into_schema_v3_without_silent_drop(
    actionability: str,
) -> None:
    _layer, catalog = _open_actionable_catalog(actionability)
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )
    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=AuthenticatedNetBenefitPlanner(
            policy=POLICY,
            audit_store=_AuditStore(results={}),
        ),
        source=prepared.closure.source,
        benefit_facts_port=prepared.closure.benefit_facts,
        material_port=prepared.material_authority,
        install_bundle_port=prepared.install_authority,
        planner_version="eligible-catalog-v1",
        catalog_namespace_digest=prepared.closure.catalog_namespace_digest,
    )
    surrogate = StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "active_capability_ids": [],
            "baseline_capability_ids": [],
            "languages": ["python"],
            "rejected_capability_ids": [],
            "requested_limit": 5,
            "signals": ["testing"],
        },
    )

    decision = adapter(
        surrogate,
        None,
        PlanningContext(
            planner_version="eligible-catalog-v1",
            catalog_snapshot_digest=adapter.catalog_snapshot_digest,
        ),
    )

    selections = decision.value["capabilities"]
    assert isinstance(selections, tuple)
    assert len(selections) == 1
    selection = selections[0]
    assert isinstance(selection, Mapping)
    assert selection["actionability"] == actionability


def _manual_entry(name: str) -> dict[str, object]:
    return {
        "actionability": "manual",
        "capability_id": f"skill:{name}",
        "equivalence_key": None,
        "install_bundle": None,
        "kind": "skill",
        "material_descriptor": None,
        "name": name,
    }


def test_catalog_rejects_noncanonical_entry_order_instead_of_reordering_it() -> None:
    manifest = _catalog_manifest()
    manifest["entries"] = [_manual_entry("z-last"), _manual_entry("a-first")]
    body = _canonical_bytes(manifest)

    with pytest.raises(EligibleCatalogError, match="canonical capability order"):
        load_eligible_catalog_layer_bytes(body, hashlib.sha256(body).hexdigest())


def test_catalog_and_reviewed_profiles_must_be_bijective() -> None:
    body = _canonical_bytes(_catalog_manifest())
    layer = load_eligible_catalog_layer_bytes(body, hashlib.sha256(body).hexdigest())
    profile_manifest = json.loads(_reviewed_profiles_body(layer))
    orphan = deepcopy(profile_manifest["profiles"][0])
    orphan["capability_id"] = "skill:orphan"
    orphan["name"] = "orphan"
    orphan["profile_id"] = "profile-orphan"
    orphan["catalog_entry_claim_digest"] = _digest("orphan-entry-claim")
    orphan["review_basis_digest"] = _digest("orphan-review")
    orphan["coverage_keys"] = ["orphan"]
    profile_manifest["profiles"].append(orphan)
    profiles_body = _canonical_bytes(profile_manifest)
    profiles = ReviewedBenefitAuthorities.create(
        (
            load_reviewed_benefit_profiles_bytes(
                profiles_body,
                hashlib.sha256(profiles_body).hexdigest(),
            ),
        )
    )

    with pytest.raises(EligibleCatalogError, match="one-to-one"):
        open_reviewed_query_catalog(layers=(layer,), profiles=profiles, policy=POLICY)


def _open_large_catalog_with_target(
    *,
    noise_context_tokens: int,
    noise_equivalence_key: str | None = None,
):
    manifest = _catalog_manifest()
    names = [f"noise-{index:03d}" for index in range(513)] + ["target"]
    manifest["entries"] = [_manual_entry(name) for name in names]
    if noise_equivalence_key is not None:
        raw_entries = manifest["entries"]
        assert isinstance(raw_entries, list)
        for entry in raw_entries:
            assert isinstance(entry, dict)
            if entry["name"] != "target":
                entry["equivalence_key"] = noise_equivalence_key
    body = _canonical_bytes(manifest)
    layer = load_eligible_catalog_layer_bytes(body, hashlib.sha256(body).hexdigest())
    profile_manifest = json.loads(_reviewed_profiles_body(layer))
    prototype = profile_manifest["profiles"][0]
    records = []
    for entry in layer.entries:
        record = deepcopy(prototype)
        record["capability_id"] = entry.capability_id
        record["name"] = entry.name
        record["profile_id"] = f"profile-{entry.name}"
        record["catalog_entry_claim_digest"] = entry.catalog_entry_claim_digest
        record["review_basis_digest"] = _digest(f"review:{entry.capability_id}")
        record["coverage_keys"] = [entry.name]
        record["costs"]["context_tokens"] = 120 if entry.name == "target" else noise_context_tokens
        record["maximum_relevance_ppm"] = 500_000 if entry.name == "target" else 1_000_000
        record["match_policy"]["allowed_equivalence_keys"] = (
            [] if noise_equivalence_key is None else [noise_equivalence_key]
        )
        records.append(record)
    profile_manifest["profiles"] = records
    profiles_body = _canonical_bytes(profile_manifest)
    profiles = ReviewedBenefitAuthorities.create(
        (
            load_reviewed_benefit_profiles_bytes(
                profiles_body,
                hashlib.sha256(profiles_body).hexdigest(),
            ),
        )
    )
    return open_reviewed_query_catalog(
        layers=(layer,),
        profiles=profiles,
        policy=POLICY,
    )


def test_net_benefit_filter_runs_before_the_512_candidate_bound() -> None:
    catalog = _open_large_catalog_with_target(noise_context_tokens=1_000_000)
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())

    assert tuple(
        candidate.capability_id for candidate in prepared.closure.source.retrieve(observation)
    ) == ("skill:target",)


@dataclass
class _TargetOnlyHostPolicy(_HostPolicy):
    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility:
        return QueryCapabilityEligibility(
            presentation_digest=capability_presentation_digest(presentation),
            catalog_entry_claim_digest=claim.catalog_entry_claim_digest,
            catalog_claim_digest=eligible_catalog_claim_digest(claim),
            available=presentation.capability_id == "skill:target",
            permissions_allowed=True,
            credentials_available=True,
        )


def test_host_feasibility_filter_runs_before_the_512_candidate_bound() -> None:
    catalog = _open_large_catalog_with_target(noise_context_tokens=120)
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    prepared = catalog.prepare_query(
        observation=observation,
        host_policy=_TargetOnlyHostPolicy(),
    )

    assert tuple(
        candidate.capability_id for candidate in prepared.closure.source.retrieve(observation)
    ) == ("skill:target",)


def test_equivalent_high_score_rows_cannot_poison_the_512_candidate_bound() -> None:
    catalog = _open_large_catalog_with_target(
        noise_context_tokens=120,
        noise_equivalence_key="noise-equivalent",
    )
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())
    candidates = prepared.closure.source.retrieve(observation)

    assert "skill:target" in {candidate.capability_id for candidate in candidates}
    assert len(candidates) <= 512


def test_distinct_feasible_frontier_over_512_fails_closed() -> None:
    catalog = _open_large_catalog_with_target(noise_context_tokens=120)
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    with pytest.raises(EligibleCatalogError, match="exact bounded policy frontier"):
        catalog.prepare_query(observation=observation, host_policy=_HostPolicy())


def test_equivalence_dominance_preserves_inbound_conflict_alternatives() -> None:
    manifest = _catalog_manifest()
    names = ["a", "b", *(f"noise-{index:03d}" for index in range(511)), "peer"]
    entries = [_manual_entry(name) for name in names]
    for raw_entry in entries:
        if raw_entry["name"] in {"a", "b"}:
            raw_entry["equivalence_key"] = "tool-equivalent"
        elif str(raw_entry["name"]).startswith("noise-"):
            raw_entry["equivalence_key"] = "noise-equivalent"
    manifest["entries"] = entries
    body = _canonical_bytes(manifest)
    layer = load_eligible_catalog_layer_bytes(body, hashlib.sha256(body).hexdigest())
    profile_manifest = json.loads(_reviewed_profiles_body(layer))
    prototype = profile_manifest["profiles"][0]
    records = []
    for catalog_entry in layer.entries:
        record = deepcopy(prototype)
        record["capability_id"] = catalog_entry.capability_id
        record["name"] = catalog_entry.name
        record["profile_id"] = f"profile-{catalog_entry.name}"
        record["catalog_entry_claim_digest"] = catalog_entry.catalog_entry_claim_digest
        record["review_basis_digest"] = _digest(f"review:{catalog_entry.capability_id}")
        record["coverage_keys"] = [catalog_entry.name]
        record["conflicts"] = ["skill:a"] if catalog_entry.name == "peer" else []
        record["maximum_relevance_ppm"] = {
            "a": 1_000_000,
            "b": 900_000,
            "peer": 950_000,
        }.get(catalog_entry.name, 800_000)
        record["match_policy"]["allowed_equivalence_keys"] = [
            "noise-equivalent",
            "tool-equivalent",
        ]
        records.append(record)
    profile_manifest["profiles"] = records
    profiles_body = _canonical_bytes(profile_manifest)
    profiles = ReviewedBenefitAuthorities.create(
        (
            load_reviewed_benefit_profiles_bytes(
                profiles_body,
                hashlib.sha256(profiles_body).hexdigest(),
            ),
        )
    )
    catalog = open_reviewed_query_catalog(
        layers=(layer,),
        profiles=profiles,
        policy=POLICY,
    )
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )

    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())
    retained = {value.capability_id for value in prepared.closure.source.retrieve(observation)}

    assert {"skill:a", "skill:b", "skill:peer"}.issubset(retained)


def test_prepared_query_cannot_be_constructed_by_callers() -> None:
    _layer, catalog = _open_catalog()
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )
    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())

    with pytest.raises(TypeError, match="catalog factory"):
        PreparedEligibleCatalogQuery(
            closure=prepared.closure,
            material_authority=None,
            install_authority=None,
        )


class _OverlongLayerSequence(Sequence[EligibleCatalogLayer]):
    def __len__(self) -> int:
        return 65

    def __iter__(self) -> Iterator[EligibleCatalogLayer]:
        raise AssertionError("layer input was read beyond its declared bound")

    @overload
    def __getitem__(self, index: int) -> EligibleCatalogLayer: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[EligibleCatalogLayer]: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> EligibleCatalogLayer | Sequence[EligibleCatalogLayer]:
        raise AssertionError(f"layer input index {index!r} was read before the length bound")


def test_catalog_layer_input_is_bounded_before_materialization() -> None:
    _layer, catalog = _open_catalog()

    with pytest.raises(EligibleCatalogError, match="bounded"):
        open_reviewed_query_catalog(
            layers=_OverlongLayerSequence(),
            profiles=catalog._profiles,
            policy=POLICY,
        )


@pytest.mark.parametrize("actionability", ["load", "install"])
def test_actionable_authority_survives_multi_layer_aggregation(
    actionability: str,
) -> None:
    ctx_layer, single_catalog = _open_actionable_catalog(actionability)
    ctx_profile = single_catalog._profiles.authorities[0]

    user_manifest = _catalog_manifest()
    user_manifest["authority"] = {
        "authority_digest": _digest("user-authority"),
        "authority_id": "user-local",
        "authority_kind": "user",
        "catalog_layer_kind": "user",
        "sequence": 1,
    }
    user_manifest["bindings"] = {
        "candidate_projection_version": "ctx.benefit-eligible-entry-v1",
        "catalog_namespace_digest": _digest("user-catalog-namespace"),
        "catalog_provenance_digest": _digest("user-catalog-provenance"),
        "installation_snapshot_digest": None,
        "material_snapshot_digest": None,
    }
    user_manifest["entries"] = [_manual_entry("user-helper")]
    user_body = _canonical_bytes(user_manifest)
    user_layer = load_eligible_catalog_layer_bytes(
        user_body,
        hashlib.sha256(user_body).hexdigest(),
    )
    user_profiles_body = _reviewed_profiles_body(user_layer)
    user_profile = load_reviewed_benefit_profiles_bytes(
        user_profiles_body,
        hashlib.sha256(user_profiles_body).hexdigest(),
    )
    profiles = ReviewedBenefitAuthorities.create((ctx_profile, user_profile))
    catalog = open_reviewed_query_catalog(
        layers=(ctx_layer, user_layer),
        profiles=profiles,
        policy=POLICY,
    )
    observation = WorkObservation(
        signals=("testing",),
        languages=("python",),
        requested_limit=5,
    )
    prepared = catalog.prepare_query(observation=observation, host_policy=_HostPolicy())
    candidate = next(
        value
        for value in prepared.closure.source.retrieve(observation)
        if value.capability_id == "skill:ctx-python-testing"
    )

    if actionability == "load":
        assert prepared.material_authority is not None
        load_bundle = prepared.material_authority.load_bundle(candidate)
        assert load_bundle is not None
        assert load_bundle.descriptor.provenance_digest == ctx_layer.material_snapshot_digest
        assert load_bundle.authority_material_snapshot_digest == ctx_layer.material_snapshot_digest
        assert load_bundle.material_snapshot_digest == profiles.material_snapshot_digest
        assert (
            load_bundle.material_snapshot_digest != load_bundle.authority_material_snapshot_digest
        )
    else:
        assert prepared.install_authority is not None
        install_bundle = prepared.install_authority.describe_bundle(
            candidate.capability_id,
            candidate.kind,
        )
        assert isinstance(install_bundle, AuthorityBoundInstallPlanningBundle)
        assert install_bundle.descriptor.provenance_digest == ctx_layer.installation_snapshot_digest
        assert install_bundle.authority_installation_snapshot_digest == (
            ctx_layer.installation_snapshot_digest
        )
        assert install_bundle.installation_snapshot_digest == profiles.installation_snapshot_digest
        assert install_bundle.installation_snapshot_digest != (
            install_bundle.authority_installation_snapshot_digest
        )

    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=AuthenticatedNetBenefitPlanner(
            policy=POLICY,
            audit_store=_AuditStore(results={}),
        ),
        source=prepared.closure.source,
        benefit_facts_port=prepared.closure.benefit_facts,
        material_port=prepared.material_authority,
        install_bundle_port=prepared.install_authority,
        planner_version="eligible-catalog-v1",
        catalog_namespace_digest=prepared.closure.catalog_namespace_digest,
    )
    decision = adapter(
        StructuredSurrogate.create(
            schema_id="ctx.observation.current-work",
            schema_version=1,
            value={
                "active_capability_ids": [],
                "baseline_capability_ids": [],
                "languages": ["python"],
                "rejected_capability_ids": [],
                "requested_limit": 5,
                "signals": ["testing"],
            },
        ),
        None,
        PlanningContext(
            planner_version="eligible-catalog-v1",
            catalog_snapshot_digest=adapter.catalog_snapshot_digest,
        ),
    )
    raw_selections = decision.value["capabilities"]
    assert isinstance(raw_selections, tuple)
    selected: dict[str, str] = {}
    for raw_selection in raw_selections:
        assert isinstance(raw_selection, Mapping)
        capability_id = raw_selection["capability_id"]
        selected_actionability = raw_selection["actionability"]
        assert isinstance(capability_id, str)
        assert isinstance(selected_actionability, str)
        selected[capability_id] = selected_actionability
    assert selected["skill:ctx-python-testing"] == actionability


@pytest.mark.parametrize("actionability", ["load", "install"])
def test_actionable_entry_rejects_cross_capability_planning_authority(
    actionability: str,
) -> None:
    raw_snapshot = _digest(f"raw-{actionability}-snapshot")
    aggregate = _aggregate_binding(f"{actionability}ation_snapshot_digest", raw_snapshot)
    if actionability == "load":
        aggregate = _aggregate_binding("material_snapshot_digest", raw_snapshot)
    other_material = MaterialIdentity.create(
        capability_id="skill:other",
        kind="skill",
        content_sha256=_digest("other-content"),
        content_bytes=100,
    )
    material_descriptor = None
    install_bundle = None
    if actionability == "load":
        material_descriptor = MaterialDescriptor.create(
            capability_id="skill:other",
            kind="skill",
            actionability="load",
            content_sha256=other_material.content_sha256,
            content_bytes=other_material.content_bytes,
            estimated_tokens=25,
            provenance_digest=aggregate,
            material_identity_digest=other_material.identity_digest,
        )
    else:
        install_bundle = InstallPlanningBundle(
            descriptor=InstallPlanDescriptor.create(
                capability_id="skill:other",
                kind="skill",
                installer_id="ctx-skill-cas",
                plan_digest=_digest("other-plan"),
                provenance_digest=aggregate,
                result_material_identity_digest=other_material.identity_digest,
            ),
            result_material=other_material,
        )
    body = _canonical_bytes(
        _catalog_manifest(
            actionability=actionability,
            material_snapshot_digest=raw_snapshot if actionability == "load" else None,
            installation_snapshot_digest=(raw_snapshot if actionability == "install" else None),
            material_descriptor=material_descriptor,
            install_bundle=install_bundle,
        )
    )

    with pytest.raises(EligibleCatalogError, match="planning authority"):
        load_eligible_catalog_layer_bytes(body, hashlib.sha256(body).hexdigest())
