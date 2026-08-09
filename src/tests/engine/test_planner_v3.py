from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, fields

import pytest

from ctx.engine.benefit import (
    BenefitCandidate,
    BenefitSelectionResult,
    EvidenceSummary,
    NetBenefitPolicy,
    ResourceCosts,
)
from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import (
    CapabilityCandidate,
    CapabilitySelection,
    PlannerValidationError,
)
from ctx.engine.planning_v3 import (
    AuthenticatedCapabilityCandidate,
    AuthenticatedNetBenefitPlanner,
    BenefitAuditReference,
    BenefitAuditStore,
    BenefitAuditStoreUnavailable,
    CapabilityPlanV3,
    InstallPlanningAuthority,
    LoadPlanningAuthority,
    ManualPlanningAuthority,
    PlanningAuthority,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _policy() -> NetBenefitPolicy:
    return NetBenefitPolicy(
        calibration_digest=_digest("calibration-v3"),
        minimum_relevance_ppm=1,
    )


def _catalog_identity(capability_id: str) -> CatalogCapabilityIdentity:
    kind = capability_id.split(":", 1)[0]
    return CatalogCapabilityIdentity.create(
        capability_id=capability_id,
        kind=kind,
        catalog_namespace_digest=_digest("catalog-namespace"),
    )


def _presentation(capability_id: str, actionability: str) -> CapabilityCandidate:
    kind, name = capability_id.split(":", 1)
    descriptor = _install_descriptor(capability_id) if actionability == "install" else None
    return CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        name=name,
        source_digest=_digest(f"source:{capability_id}"),
        normalized_score_ppm=900_000,
        matching_signals=("python",),
        reason_codes=("signal-match",),
        actionability=actionability,
        install_descriptor_digest=(None if descriptor is None else descriptor.descriptor_digest),
        install_plan_digest=None if descriptor is None else descriptor.plan_digest,
    )


def _benefit_candidate(
    presentation: CapabilityCandidate,
    *,
    availability: str,
    expected_task_benefit_ppm: int = 600_000,
) -> BenefitCandidate:
    return BenefitCandidate(
        capability_id=presentation.capability_id,
        source_digest=presentation.source_digest,
        resource_profile_digest=_digest(f"resource-profile:{presentation.capability_id}"),
        availability=availability,
        expected_task_benefit_ppm=expected_task_benefit_ppm,
        relevance_ppm=1_000_000,
        trust_ppm=1_000_000,
        costs=ResourceCosts(),
        evidence=EvidenceSummary(
            capability_id=presentation.capability_id,
            kind=presentation.kind,
            source_digest=presentation.source_digest,
            evidence_window_digest=_digest(f"evidence-window:{presentation.capability_id}"),
            opportunity_observable=False,
        ),
        source_trusted=True,
        security_approved=True,
        permissions_allowed=True,
        credentials_available=True,
    )


def _load_authority(
    capability_id: str,
    catalog_identity: CatalogCapabilityIdentity,
) -> LoadPlanningAuthority:
    kind = capability_id.split(":", 1)[0]
    material_identity = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"content:{capability_id}"),
        content_bytes=32,
    )
    descriptor = MaterialDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        actionability="load",
        content_sha256=material_identity.content_sha256,
        content_bytes=material_identity.content_bytes,
        estimated_tokens=8,
        provenance_digest=catalog_identity.catalog_namespace_digest,
        material_identity_digest=material_identity.identity_digest,
    )
    return LoadPlanningAuthority(
        material=AuthorizedMaterial.from_catalog(
            catalog_identity_digest=catalog_identity.identity_digest,
            descriptor=descriptor,
        )
    )


def _install_descriptor(capability_id: str) -> InstallPlanDescriptor:
    kind = capability_id.split(":", 1)[0]
    material_identity = _result_material(capability_id)
    return InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id="skill-installer",
        plan_digest=_digest(f"plan:{capability_id}"),
        provenance_digest=_digest("catalog-namespace"),
        result_material_identity_digest=material_identity.identity_digest,
    )


def _result_material(capability_id: str) -> MaterialIdentity:
    kind = capability_id.split(":", 1)[0]
    return MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"installed-content:{capability_id}"),
        content_bytes=32,
    )


def _candidate(
    capability_id: str,
    actionability: str,
    *,
    expected_task_benefit_ppm: int = 600_000,
) -> AuthenticatedCapabilityCandidate:
    presentation = _presentation(capability_id, actionability)
    identity = _catalog_identity(capability_id)
    authority: PlanningAuthority
    if actionability == "load":
        authority = _load_authority(capability_id, identity)
        availability = "executable"
    elif actionability == "install":
        authority = InstallPlanningAuthority(
            descriptor=_install_descriptor(capability_id),
            result_material=_result_material(capability_id),
        )
        availability = "executable"
    else:
        authority = ManualPlanningAuthority()
        availability = "advisory"
    return AuthenticatedCapabilityCandidate(
        presentation=presentation,
        catalog_identity=identity,
        benefit_candidate=_benefit_candidate(
            presentation,
            availability=availability,
            expected_task_benefit_ppm=expected_task_benefit_ppm,
        ),
        authority=authority,
    )


@dataclass
class RecordingAuditStore(BenefitAuditStore):
    results: dict[str, BenefitSelectionResult] = field(default_factory=dict)
    fail: bool = False

    def store(self, result: BenefitSelectionResult) -> str:
        if self.fail:
            raise BenefitAuditStoreUnavailable("private audit path")
        self.results[result.result_digest] = result
        return result.result_digest


def _planner(
    store: RecordingAuditStore | None = None,
) -> tuple[AuthenticatedNetBenefitPlanner, RecordingAuditStore]:
    actual_store = RecordingAuditStore() if store is None else store
    return (
        AuthenticatedNetBenefitPlanner(policy=_policy(), audit_store=actual_store),
        actual_store,
    )


def test_v3_ready_plan_has_strict_audit_identity_benefit_and_authority_union() -> None:
    planner, store = _planner()
    load = _candidate("skill:local", "load")
    install = _candidate("skill:remote", "install")
    manual = _candidate("agent:advisor", "manual")

    plan = planner.plan((manual, install, load))
    mapping = plan.to_mapping()

    assert set(mapping) == {
        "status",
        "abstention_code",
        "benefit_audit",
        "capabilities",
    }
    assert mapping["status"] == "ready"
    assert mapping["abstention_code"] is None
    audit = mapping["benefit_audit"]
    assert isinstance(audit, dict)
    assert set(audit) == {
        "result_schema_id",
        "result_digest",
        "policy_schema_id",
        "policy_digest",
        "selection_algorithm_id",
        "calibration_digest",
        "requested_limit",
        "candidate_pool_count",
        "search_evaluation_count",
    }
    assert "assessments" not in audit
    assert store.results[audit["result_digest"]].candidate_pool_count == 3
    assert {item.name for item in fields(plan)} == {
        "status",
        "abstention_code",
        "benefit_audit",
        "selections",
    }

    rows = mapping["capabilities"]
    assert isinstance(rows, list)
    assert [row["capability_id"] for row in rows] == [
        "skill:local",
        "skill:remote",
        "agent:advisor",
    ]
    by_id = {row["capability_id"]: row for row in rows}
    for source, row in ((load, by_id["skill:local"]), (install, by_id["skill:remote"])):
        assert set(row) == {
            "actionability",
            "capability_id",
            "catalog_entry_digest",
            "catalog_identity",
            "install_descriptor_digest",
            "install_plan_digest",
            "kind",
            "matching_signals",
            "name",
            "normalized_score_ppm",
            "reason_codes",
            "benefit",
            "authority",
        }
        assert row["catalog_identity"] == source.catalog_identity.to_dict()
        assert set(row["benefit"]) == {
            "tier",
            "individual_net_benefit_u",
            "marginal_net_benefit_u",
        }
    assert by_id["skill:local"]["authority"] == {
        "type": "load",
        "material": load.authority.material.to_dict(),  # type: ignore[union-attr]
    }
    assert by_id["skill:remote"]["authority"] == {
        "type": "install",
        "descriptor": install.authority.descriptor.to_dict(),  # type: ignore[union-attr]
        "result_material": install.authority.result_material.to_dict(),  # type: ignore[union-attr]
    }
    assert by_id["agent:advisor"]["authority"] == {"type": "manual"}


@pytest.mark.parametrize(
    ("candidates", "requested_limit", "code"),
    [
        ((), 5, "no-feasible-capability"),
        ((_candidate("skill:zero", "load", expected_task_benefit_ppm=0),), 5, "below-net-benefit"),
        ((_candidate("skill:unused", "load"),), 0, "limit-zero"),
    ],
)
def test_v3_abstention_keeps_digest_bound_audit_without_inline_assessments(
    candidates: Sequence[AuthenticatedCapabilityCandidate],
    requested_limit: int,
    code: str,
) -> None:
    planner, store = _planner()

    plan = planner.plan(candidates, requested_limit=requested_limit)
    mapping = plan.to_mapping()

    assert mapping["status"] == "abstained"
    assert mapping["abstention_code"] == code
    assert mapping["capabilities"] == []
    assert isinstance(mapping["benefit_audit"], dict)
    result_digest = mapping["benefit_audit"]["result_digest"]
    assert store.results[result_digest].abstention_code == code
    assert "assessments" not in str(mapping)


def test_v3_audit_store_failure_degrades_without_audit_or_exception_text() -> None:
    planner, _store = _planner(RecordingAuditStore(fail=True))

    plan = planner.plan((_candidate("skill:local", "load"),))

    assert plan.to_mapping() == {
        "status": "degraded",
        "abstention_code": "planner-failed",
        "benefit_audit": None,
        "capabilities": [],
    }
    assert "private audit path" not in str(plan.to_mapping())


def test_v3_audit_store_contract_errors_are_not_silently_mislabeled() -> None:
    class WrongDigestStore:
        def store(self, _result: BenefitSelectionResult) -> str:
            return _digest("wrong-result")

    class ProgrammingErrorStore:
        def store(self, _result: BenefitSelectionResult) -> str:
            raise RuntimeError("store invariant failed")

    candidate = _candidate("skill:local", "load")
    with pytest.raises(PlannerValidationError, match="different benefit result"):
        AuthenticatedNetBenefitPlanner(
            policy=_policy(),
            audit_store=WrongDigestStore(),
        ).plan((candidate,))
    with pytest.raises(RuntimeError, match="store invariant failed"):
        AuthenticatedNetBenefitPlanner(
            policy=_policy(),
            audit_store=ProgrammingErrorStore(),
        ).plan((candidate,))


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("policy_schema_id", "ctx.false-policy-v1"),
        ("selection_algorithm_id", "ctx.false-selection-v1"),
        ("calibration_digest", _digest("false-calibration")),
    ],
)
def test_v3_plan_rejects_false_policy_metadata_in_audit_reference(
    field_name: str,
    tampered_value: str,
) -> None:
    planner, store = _planner()
    original = planner.plan((_candidate("skill:local", "load"),))
    assert original.benefit_audit is not None
    result = store.results[original.benefit_audit.result_digest]
    audit_values = original.benefit_audit.to_mapping()
    audit_values[field_name] = tampered_value
    tampered = BenefitAuditReference(**audit_values)  # type: ignore[arg-type]

    with pytest.raises(PlannerValidationError, match="audit"):
        CapabilityPlanV3(
            status=original.status,
            abstention_code=original.abstention_code,
            benefit_audit=tampered,
            selections=original.selections,
            validated_result=result,
            validated_policy=planner.policy,
        )


def test_v3_selection_and_audit_digest_are_invariant_to_candidate_order() -> None:
    candidates = (
        _candidate("skill:local", "load"),
        _candidate("skill:remote", "install"),
        _candidate("agent:advisor", "manual"),
    )
    first, _ = _planner()
    second, _ = _planner()

    assert (
        first.plan(candidates).to_mapping() == second.plan(tuple(reversed(candidates))).to_mapping()
    )


def test_v3_candidate_requires_exact_identity_source_and_authority_binding() -> None:
    valid = _candidate("skill:local", "load")
    other_identity = _catalog_identity("skill:other")
    with pytest.raises(PlannerValidationError, match="catalog identity"):
        AuthenticatedCapabilityCandidate(
            presentation=valid.presentation,
            catalog_identity=other_identity,
            benefit_candidate=valid.benefit_candidate,
            authority=valid.authority,
        )

    mismatched_benefit = _benefit_candidate(
        _presentation("skill:other", "load"),
        availability="executable",
    )
    with pytest.raises(PlannerValidationError, match="benefit candidate"):
        AuthenticatedCapabilityCandidate(
            presentation=valid.presentation,
            catalog_identity=valid.catalog_identity,
            benefit_candidate=mismatched_benefit,
            authority=valid.authority,
        )

    foreign_material = _load_authority(
        "skill:other",
        _catalog_identity("skill:other"),
    )
    with pytest.raises(PlannerValidationError, match="load authority"):
        AuthenticatedCapabilityCandidate(
            presentation=valid.presentation,
            catalog_identity=valid.catalog_identity,
            benefit_candidate=valid.benefit_candidate,
            authority=foreign_material,
        )


def test_v3_install_requires_descriptor_v2_and_exact_presentation_digests() -> None:
    presentation = _presentation("skill:remote", "install")
    identity = _catalog_identity("skill:remote")
    v1 = InstallPlanDescriptor.create(
        capability_id="skill:remote",
        kind="skill",
        installer_id="skill-installer",
        plan_digest=presentation.install_plan_digest or "",
        provenance_digest=identity.catalog_namespace_digest,
    )

    with pytest.raises(PlannerValidationError, match="schema v2"):
        InstallPlanningAuthority(
            descriptor=v1,
            result_material=_result_material("skill:remote"),
        )

    other = _install_descriptor("skill:other")
    with pytest.raises(PlannerValidationError, match="install authority"):
        AuthenticatedCapabilityCandidate(
            presentation=presentation,
            catalog_identity=identity,
            benefit_candidate=_benefit_candidate(
                presentation,
                availability="executable",
            ),
            authority=InstallPlanningAuthority(
                descriptor=other,
                result_material=_result_material("skill:other"),
            ),
        )


def test_v3_install_authority_rejects_result_material_substitution() -> None:
    with pytest.raises(PlannerValidationError, match="result material"):
        InstallPlanningAuthority(
            descriptor=_install_descriptor("skill:remote"),
            result_material=_result_material("skill:other"),
        )


@pytest.mark.parametrize(
    ("actionability", "availability", "authority", "message"),
    [
        ("manual", "executable", ManualPlanningAuthority(), "advisory"),
        ("load", "advisory", ManualPlanningAuthority(), "authority"),
    ],
)
def test_v3_manual_authority_is_empty_advisory_only(
    actionability: str,
    availability: str,
    authority: ManualPlanningAuthority,
    message: str,
) -> None:
    presentation = _presentation("agent:advisor", actionability)

    with pytest.raises(PlannerValidationError, match=message):
        AuthenticatedCapabilityCandidate(
            presentation=presentation,
            catalog_identity=_catalog_identity("agent:advisor"),
            benefit_candidate=_benefit_candidate(
                presentation,
                availability=availability,
            ),
            authority=authority,
        )


def test_legacy_plan_objects_still_reject_schema_v3_instead_of_changing_v1_v2() -> None:
    legacy = CapabilitySelection.from_candidate(_presentation("skill:local", "load"))

    with pytest.raises(PlannerValidationError, match="unsupported"):
        # Schema v3 is a separate strict model, not optional fields on legacy rows.
        legacy.to_mapping(schema_version=3)
