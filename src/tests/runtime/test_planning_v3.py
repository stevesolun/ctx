from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

import pytest
import ctx.runtime.planning_v3 as planning_v3_module

from ctx.engine.benefit import (
    BenefitCandidate,
    BenefitSelectionResult,
    EvidenceSummary,
    NetBenefitPolicy,
    ResourceCosts,
)
from ctx.engine.content import MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor, InstallPlanningBundle
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import (
    CandidateAuthorityUnavailable,
    CandidateSourceUnavailable,
    CapabilityCandidate,
    PlannerValidationError,
    WorkObservation,
)
from ctx.engine.planning_v3 import (
    AuthenticatedNetBenefitPlanner,
    BenefitAuditStore,
)
from ctx.engine.protocol import ScopeRef
from ctx.engine.replay import PlanningContext, StructuredSurrogate
from ctx.engine.state import CapabilityState, EngineState, LeaseRef
from ctx.runtime.planning_v3 import (
    AuthenticatedReplayDecisionPlannerV3,
    CatalogLoadPlanningBundle,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _material(capability_id: str, salt: str | None = None) -> MaterialIdentity:
    kind = capability_id.split(":", 1)[0]
    return MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"content:{salt or capability_id}"),
        content_bytes=32,
    )


def _load_descriptor(
    capability_id: str,
    *,
    schema_version: int = 2,
    material_salt: str | None = None,
    provenance_salt: str = "material-snapshot",
) -> MaterialDescriptor:
    kind = capability_id.split(":", 1)[0]
    material = _material(capability_id, material_salt)
    return MaterialDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        actionability="load",
        content_sha256=material.content_sha256,
        content_bytes=material.content_bytes,
        estimated_tokens=8,
        provenance_digest=_digest(provenance_salt),
        material_identity_digest=(material.identity_digest if schema_version == 2 else None),
    )


def _install_bundle(
    capability_id: str,
    *,
    provenance_salt: str = "install-snapshot",
    result_salt: str = "installed",
) -> InstallPlanningBundle:
    kind = capability_id.split(":", 1)[0]
    result = _material(capability_id, result_salt)
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id="ctx-installer-v1",
        plan_digest=_digest(f"plan:{capability_id}"),
        provenance_digest=_digest(provenance_salt),
        result_material_identity_digest=result.identity_digest,
    )
    return InstallPlanningBundle(descriptor=descriptor, result_material=result)


def _candidate(
    capability_id: str,
    actionability: str,
    *,
    source_salt: str | None = None,
) -> CapabilityCandidate:
    kind, name = capability_id.split(":", 1)
    bundle = _install_bundle(capability_id) if actionability == "install" else None
    return CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        name=name,
        source_digest=_digest(f"source:{source_salt or capability_id}"),
        normalized_score_ppm=900_000,
        matching_signals=("python",),
        reason_codes=("signal-match",),
        actionability=actionability,
        install_descriptor_digest=(
            None
            if actionability != "install"
            else (
                bundle.descriptor.descriptor_digest
                if bundle is not None
                else _digest(f"descriptor:{capability_id}")
            )
        ),
        install_plan_digest=(
            None
            if actionability != "install"
            else (
                bundle.descriptor.plan_digest
                if bundle is not None
                else _digest(f"plan:{capability_id}")
            )
        ),
    )


def _benefit(
    candidate: CapabilityCandidate,
    *,
    availability: str | None = None,
    source_digest: str | None = None,
    expected_task_benefit_ppm: int = 700_000,
) -> BenefitCandidate:
    tier = availability or ("advisory" if candidate.actionability == "manual" else "executable")
    return BenefitCandidate(
        capability_id=candidate.capability_id,
        source_digest=source_digest or candidate.source_digest,
        resource_profile_digest=_digest(f"profile:{candidate.capability_id}"),
        availability=tier,
        expected_task_benefit_ppm=expected_task_benefit_ppm,
        relevance_ppm=1_000_000,
        trust_ppm=1_000_000,
        costs=ResourceCosts(),
        evidence=EvidenceSummary(
            capability_id=candidate.capability_id,
            kind=candidate.kind,
            source_digest=source_digest or candidate.source_digest,
            evidence_window_digest=_digest(f"window:{candidate.capability_id}"),
            opportunity_observable=False,
        ),
        source_trusted=True,
        security_approved=True,
        permissions_allowed=True,
        credentials_available=True,
    )


@dataclass
class _AuditStore(BenefitAuditStore):
    results: dict[str, BenefitSelectionResult] = field(default_factory=dict)

    def store(self, result: BenefitSelectionResult) -> str:
        self.results[result.result_digest] = result
        return result.result_digest


@dataclass
class _Source:
    values: Sequence[CapabilityCandidate]
    catalog_snapshot_digest: str = field(default_factory=lambda: _digest("catalog-snapshot"))
    observations: list[WorkObservation] = field(default_factory=list)

    def retrieve(self, observation: WorkObservation) -> Sequence[CapabilityCandidate]:
        self.observations.append(observation)
        return self.values


@dataclass
class _Facts:
    values: dict[str, BenefitCandidate | None]
    benefit_facts_snapshot_digest: str = field(default_factory=lambda: _digest("benefit-facts"))
    calls: list[str] = field(default_factory=list)

    def benefit_candidate(
        self,
        presentation: CapabilityCandidate,
        _observation: WorkObservation,
    ) -> BenefitCandidate | None:
        self.calls.append(presentation.capability_id)
        return self.values.get(presentation.capability_id)


@dataclass
class _Materials:
    values: dict[str, CatalogLoadPlanningBundle | MaterialDescriptor | None]
    material_snapshot_digest: str = field(default_factory=lambda: _digest("material-snapshot"))
    calls: list[str] = field(default_factory=list)

    def load_bundle(
        self,
        presentation: CapabilityCandidate,
    ) -> CatalogLoadPlanningBundle | None:
        self.calls.append(presentation.capability_id)
        return cast(
            CatalogLoadPlanningBundle | None,
            self.values.get(presentation.capability_id),
        )


@dataclass
class _Installs:
    values: dict[str, InstallPlanningBundle | None]
    installation_snapshot_digest: str = field(default_factory=lambda: _digest("install-snapshot"))
    calls: list[str] = field(default_factory=list)

    def describe_bundle(self, capability_id: str, _kind: str) -> InstallPlanningBundle | None:
        self.calls.append(capability_id)
        return self.values.get(capability_id)


def _observation(
    *,
    baseline: tuple[str, ...] = (),
    active: tuple[str, ...] = (),
    rejected: tuple[str, ...] = (),
    requested_limit: int = 5,
) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": ["python", "testing"],
            "languages": ["python"],
            "baseline_capability_ids": list(baseline),
            "active_capability_ids": list(active),
            "rejected_capability_ids": list(rejected),
            "requested_limit": requested_limit,
        },
    )


def _catalog_load_bundle(
    presentation: CapabilityCandidate,
    *,
    descriptor: MaterialDescriptor | None = None,
    catalog_snapshot_digest: str | None = None,
    catalog_namespace_digest: str | None = None,
) -> CatalogLoadPlanningBundle:
    value = descriptor or _load_descriptor(presentation.capability_id)
    return CatalogLoadPlanningBundle(
        presentation=presentation,
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=presentation.capability_id,
            kind=presentation.kind,
            catalog_namespace_digest=(catalog_namespace_digest or _digest("catalog-namespace")),
        ),
        descriptor=value,
        catalog_snapshot_digest=(catalog_snapshot_digest or _digest("catalog-snapshot")),
        material_snapshot_digest=value.provenance_digest,
    )


def _adapter(
    candidates: Sequence[CapabilityCandidate],
    *,
    facts: dict[str, BenefitCandidate | None] | None = None,
    materials: dict[str, CatalogLoadPlanningBundle | MaterialDescriptor | None] | None = None,
    installs: dict[str, InstallPlanningBundle | None] | None = None,
) -> tuple[
    AuthenticatedReplayDecisionPlannerV3,
    _Source,
    _Facts,
    _Materials,
    _Installs,
]:
    source = _Source(candidates)
    facts_port = _Facts(
        facts
        if facts is not None
        else {candidate.capability_id: _benefit(candidate) for candidate in candidates}
    )
    material_port = _Materials(
        materials
        if materials is not None
        else {
            candidate.capability_id: _catalog_load_bundle(candidate)
            for candidate in candidates
            if candidate.actionability == "load"
        }
    )
    install_port = _Installs(
        installs
        if installs is not None
        else {
            candidate.capability_id: _install_bundle(candidate.capability_id)
            for candidate in candidates
            if candidate.actionability == "install"
        }
    )
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=AuthenticatedNetBenefitPlanner(
            policy=NetBenefitPolicy(
                calibration_digest=_digest("calibration"),
                minimum_relevance_ppm=1,
            ),
            audit_store=_AuditStore(),
        ),
        source=source,
        benefit_facts_port=facts_port,
        material_port=material_port,
        install_bundle_port=install_port,
        planner_version="planner-v3",
        catalog_namespace_digest=_digest("catalog-namespace"),
    )
    return adapter, source, facts_port, material_port, install_port


def _context(adapter: AuthenticatedReplayDecisionPlannerV3) -> PlanningContext:
    return PlanningContext(
        planner_version="planner-v3",
        catalog_snapshot_digest=adapter.catalog_snapshot_digest,
    )


def _active_state(candidate: CapabilityCandidate) -> EngineState:
    lease = LeaseRef(
        lease_id="lease-active",
        owner_id="owner-active",
        exposure_id="exposure-1",
    )
    return EngineState(
        revision=1,
        scope=ScopeRef(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            repository_id="repository-1",
            session_id="session-1",
            exposure_id="exposure-1",
            host_context_id="host-1",
        ),
        host_level="activating",
        host_descriptor_digest=_digest("host"),
        capabilities=(
            CapabilityState(
                capability_id=candidate.capability_id,
                source_digest=candidate.source_digest,
                plan_id="plan-active",
                catalog_snapshot_id=_digest("catalog-snapshot"),
                kind=candidate.kind,
                actionability=candidate.actionability,
                leases=(lease,),
                activation="active",
                activation_lease_id=lease.lease_id,
            ),
        ),
        _contract_version=2,
    )


def test_emits_only_schema_v3_for_mixed_load_install_and_manual_authorities() -> None:
    candidates = (
        _candidate("agent:advisor", "manual"),
        _candidate("skill:remote", "install"),
        _candidate("skill:local", "load"),
    )
    adapter, _source, _facts, _materials, _installs = _adapter(candidates)

    decision = adapter(_observation(), None, _context(adapter))

    assert (decision.schema_id, decision.schema_version) == (
        "ctx.decision.capability-plan",
        3,
    )
    rows = decision.to_dict()["value"]["capabilities"]
    assert [row["capability_id"] for row in rows] == [
        "skill:local",
        "skill:remote",
        "agent:advisor",
    ]
    assert [row["authority"]["type"] for row in rows] == ["load", "install", "manual"]
    assert rows[0]["authority"]["material"]["origin"] == "catalog"
    assert rows[1]["authority"]["descriptor"]["schema"] == "ctx.install-plan-descriptor-v2"
    assert rows[2]["authority"] == {"type": "manual"}


@pytest.mark.parametrize(
    ("candidates", "requested_limit", "code"),
    [
        ((), 5, "no-feasible-capability"),
        ((_candidate("skill:local", "load"),), 0, "limit-zero"),
    ],
)
def test_preserves_zero_selection_abstention(
    candidates: Sequence[CapabilityCandidate],
    requested_limit: int,
    code: str,
) -> None:
    adapter, _source, _facts, _materials, _installs = _adapter(candidates)

    value = adapter(
        _observation(requested_limit=requested_limit),
        None,
        _context(adapter),
    ).to_dict()["value"]

    assert value["status"] == "abstained"
    assert value["abstention_code"] == code
    assert value["capabilities"] == []
    assert value["benefit_audit"] is not None


def test_declared_candidate_source_unavailability_emits_schema_v3_degradation() -> None:
    class _UnavailableSource(_Source):
        def retrieve(self, observation: WorkObservation) -> Sequence[CapabilityCandidate]:
            self.observations.append(observation)
            raise CandidateSourceUnavailable("private graph failure")

    adapter, source, facts, materials, installs = _adapter(())
    unavailable = _UnavailableSource((), catalog_snapshot_digest=source.catalog_snapshot_digest)
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=adapter.planner,
        source=unavailable,
        benefit_facts_port=facts,
        material_port=materials,
        install_bundle_port=installs,
        planner_version="planner-v3",
        catalog_namespace_digest=_digest("catalog-namespace"),
    )

    value = adapter(_observation(), None, _context(adapter)).to_dict()["value"]

    assert value == {
        "status": "degraded",
        "abstention_code": "catalog-unavailable",
        "benefit_audit": None,
        "capabilities": [],
    }
    assert "private graph failure" not in str(value)


@pytest.mark.parametrize("mode", ["missing", "mismatched-id", "mismatched-source"])
def test_rejects_missing_or_mismatched_authenticated_benefit_facts(mode: str) -> None:
    candidate = _candidate("skill:local", "load")
    facts: dict[str, BenefitCandidate | None]
    if mode == "missing":
        facts = {candidate.capability_id: None}
    elif mode == "mismatched-id":
        other = _candidate("skill:other", "load")
        facts = {candidate.capability_id: _benefit(other)}
    else:
        facts = {
            candidate.capability_id: _benefit(
                candidate,
                source_digest=_digest("substituted-source"),
            )
        }
    adapter, _source, _facts, _materials, _installs = _adapter((candidate,), facts=facts)

    with pytest.raises(PlannerValidationError, match="benefit"):
        adapter(_observation(), None, _context(adapter))


def test_isolates_v1_load_descriptor_without_downgrading_to_manual() -> None:
    candidate = _candidate("skill:local", "load")
    adapter, _source, _facts, _materials, _installs = _adapter(
        (candidate,),
        materials={
            candidate.capability_id: _load_descriptor(candidate.capability_id, schema_version=1)
        },
    )

    value = adapter(_observation(), None, _context(adapter)).to_dict()["value"]

    assert value["status"] == "abstained"
    assert value["capabilities"] == []


def test_rejects_load_descriptor_from_a_different_material_snapshot() -> None:
    candidate = _candidate("skill:local", "load")
    descriptor = _load_descriptor(candidate.capability_id)
    materials = _Materials(
        {candidate.capability_id: _catalog_load_bundle(candidate, descriptor=descriptor)},
        material_snapshot_digest=_digest("different-material-snapshot"),
    )
    adapter, source, facts, _default_materials, installs = _adapter((candidate,))
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=adapter.planner,
        source=source,
        benefit_facts_port=facts,
        material_port=materials,
        install_bundle_port=installs,
        planner_version="planner-v3",
        catalog_namespace_digest=_digest("catalog-namespace"),
    )

    value = adapter(_observation(), None, _context(adapter)).to_dict()["value"]

    assert value["status"] == "abstained"
    assert value["capabilities"] == []


@pytest.mark.parametrize("mode", ["missing", "substituted"])
def test_isolates_missing_or_substituted_install_bundle_instead_of_manual(
    mode: str,
) -> None:
    candidate = _candidate("skill:remote", "install")
    bundle = None if mode == "missing" else _install_bundle("skill:other")
    adapter, _source, _facts, _materials, _installs = _adapter(
        (candidate,),
        installs={candidate.capability_id: bundle},
    )

    value = adapter(_observation(), None, _context(adapter)).to_dict()["value"]

    assert value["status"] == "abstained"
    assert value["capabilities"] == []


def test_rejects_install_bundle_from_a_different_installation_snapshot() -> None:
    candidate = _candidate("skill:remote", "install")
    adapter, _source, _facts, _materials, _installs = _adapter(
        (candidate,),
        installs={
            candidate.capability_id: _install_bundle(
                candidate.capability_id,
                provenance_salt="different-install-snapshot",
            )
        },
    )

    value = adapter(_observation(), None, _context(adapter)).to_dict()["value"]

    assert value["status"] == "abstained"
    assert value["capabilities"] == []


def test_rejects_planner_catalog_and_injected_port_snapshot_drift() -> None:
    candidate = _candidate("skill:local", "load")
    adapter, source, facts, materials, installs = _adapter((candidate,))

    with pytest.raises(PlannerValidationError, match="planner version"):
        adapter(
            _observation(),
            None,
            PlanningContext(
                planner_version="different-planner",
                catalog_snapshot_digest=source.catalog_snapshot_digest,
            ),
        )
    with pytest.raises(PlannerValidationError, match="catalog snapshot"):
        adapter(
            _observation(),
            None,
            PlanningContext(
                planner_version="planner-v3",
                catalog_snapshot_digest=_digest("different-catalog"),
            ),
        )

    for port, attribute, message in (
        (source, "catalog_snapshot_digest", "catalog snapshot"),
        (facts, "benefit_facts_snapshot_digest", "benefit facts snapshot"),
        (materials, "material_snapshot_digest", "material snapshot"),
        (installs, "installation_snapshot_digest", "installation snapshot"),
    ):
        setattr(port, attribute, _digest(f"changed:{attribute}"))
        with pytest.raises(PlannerValidationError, match=message):
            adapter(_observation(), None, _context(adapter))
        setattr(port, attribute, getattr(adapter, f"_{attribute}"))


def test_excludes_baseline_and_rejected_before_authenticated_fact_lookup() -> None:
    baseline = _candidate("skill:baseline", "load")
    rejected = _candidate("agent:rejected", "manual")
    selected = _candidate("skill:selected", "load")
    adapter, _source, facts, materials, _installs = _adapter((baseline, rejected, selected))

    decision = adapter(
        _observation(
            baseline=(baseline.capability_id,),
            rejected=(rejected.capability_id,),
        ),
        None,
        _context(adapter),
    )

    rows = decision.to_dict()["value"]["capabilities"]
    assert [row["capability_id"] for row in rows] == [selected.capability_id]
    assert facts.calls == [selected.capability_id, selected.capability_id]
    assert materials.calls == [selected.capability_id, selected.capability_id]


def test_authoritative_state_active_ids_replace_observed_active_ids_without_exclusion() -> None:
    active = _candidate("skill:active", "load")
    adapter, source, _facts, _materials, _installs = _adapter((active,))

    decision = adapter(
        _observation(active=("skill:forged",)),
        _active_state(active),
        _context(adapter),
    )

    assert source.observations[0].active_capability_ids == (active.capability_id,)
    assert decision.to_dict()["value"]["capabilities"][0]["capability_id"] == active.capability_id


def test_excludes_ambiguous_duplicate_identity_but_deduplicates_exact_rows() -> None:
    exact = _candidate("skill:exact", "load")
    ambiguous_a = _candidate("skill:ambiguous", "load", source_salt="a")
    ambiguous_b = _candidate("skill:ambiguous", "load", source_salt="b")
    adapter, _source, facts, _materials, _installs = _adapter(
        (ambiguous_a, exact, ambiguous_b, exact),
        facts={exact.capability_id: _benefit(exact)},
    )

    decision = adapter(_observation(), None, _context(adapter))

    assert [row["capability_id"] for row in decision.to_dict()["value"]["capabilities"]] == [
        exact.capability_id
    ]
    assert facts.calls == [exact.capability_id, exact.capability_id]


@pytest.mark.parametrize("requested_limit", [-1, 6, True])
def test_rejects_invalid_requested_limit_before_retrieval(requested_limit: int) -> None:
    candidate = _candidate("skill:local", "load")
    adapter, source, _facts, _materials, _installs = _adapter((candidate,))

    with pytest.raises(PlannerValidationError, match="requested_limit"):
        adapter(
            _observation(requested_limit=requested_limit),
            None,
            _context(adapter),
        )
    assert source.observations == []


def test_rejects_oversized_candidate_pool() -> None:
    repeated = tuple(_candidate(f"skill:item-{index}", "manual") for index in range(513))
    adapter, _source, _facts, _materials, _installs = _adapter(repeated)

    with pytest.raises(PlannerValidationError, match="candidate pool"):
        adapter(_observation(), None, _context(adapter))


def test_rejects_non_current_work_and_unknown_observation_fields() -> None:
    adapter, source, _facts, _materials, _installs = _adapter(())
    wrong = StructuredSurrogate.create(
        schema_id="ctx.observation.other",
        schema_version=1,
        value={},
    )
    unknown = StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={**_observation().to_dict()["value"], "raw_prompt": "raw-prompt"},
    )

    with pytest.raises(PlannerValidationError, match="current-work"):
        adapter(wrong, None, _context(adapter))
    with pytest.raises(PlannerValidationError, match="fields"):
        adapter(unknown, None, _context(adapter))
    assert source.observations == []


def test_decision_surrogate_never_contains_raw_port_data() -> None:
    candidate = _candidate("skill:local", "load")
    adapter, _source, _facts, _materials, _installs = _adapter((candidate,))

    decision = adapter(_observation(), None, _context(adapter))

    encoded = decision.to_json()
    assert "raw_prompt" not in encoded
    assert "filesystem_path" not in encoded
    assert "material body" not in encoded


def test_planning_context_digest_binds_the_complete_planning_environment() -> None:
    candidate = _candidate("skill:local", "load")
    adapter, source, facts, materials, installs = _adapter((candidate,))
    expected = hashlib.sha256(
        json.dumps(
            {
                "benefit_facts_snapshot_digest": facts.benefit_facts_snapshot_digest,
                "calibration_digest": adapter.planner.policy.calibration_digest,
                "catalog_namespace_digest": _digest("catalog-namespace"),
                "catalog_retrieval_snapshot_digest": source.catalog_snapshot_digest,
                "installation_snapshot_digest": installs.installation_snapshot_digest,
                "material_snapshot_digest": materials.material_snapshot_digest,
                "planner_version": "planner-v3",
                "policy_digest": adapter.planner.policy.policy_digest,
                "schema": "ctx.planning-environment-v1",
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()

    assert adapter.catalog_snapshot_digest == expected
    assert adapter.catalog_retrieval_snapshot_digest == source.catalog_snapshot_digest
    assert adapter.catalog_snapshot_digest != source.catalog_snapshot_digest
    with pytest.raises(PlannerValidationError, match="planning environment"):
        adapter(
            _observation(),
            None,
            PlanningContext(
                planner_version="planner-v3",
                catalog_snapshot_digest=source.catalog_snapshot_digest,
            ),
        )


def test_typed_install_bundles_are_supported_uniformly_across_installable_kinds() -> None:
    candidates = (
        _candidate("skill:remote", "install"),
        _candidate("agent:remote", "install"),
        _candidate("mcp-server:remote", "install"),
    )
    adapter, _source, _facts, _materials, _installs = _adapter(candidates)

    rows = adapter(_observation(), None, _context(adapter)).to_dict()["value"]["capabilities"]

    assert [row["capability_id"] for row in rows] == [
        "agent:remote",
        "mcp-server:remote",
        "skill:remote",
    ]
    assert {row["authority"]["type"] for row in rows} == {"install"}


def test_invalid_install_authority_is_local_to_one_candidate() -> None:
    invalid = _candidate("agent:remote", "install")
    valid = _candidate("skill:local", "load")
    adapter, _source, _facts, _materials, installs = _adapter(
        (invalid, valid),
        installs={invalid.capability_id: None},
    )

    rows = adapter(_observation(), None, _context(adapter)).to_dict()["value"]["capabilities"]

    assert [row["capability_id"] for row in rows] == [valid.capability_id]
    assert installs.calls == [invalid.capability_id, invalid.capability_id]


def test_malformed_install_bundle_is_local_to_one_candidate() -> None:
    invalid = _candidate("agent:remote", "install")
    valid = _candidate("skill:local", "load")
    adapter, _source, _facts, _materials, installs = _adapter(
        (invalid, valid),
        installs={invalid.capability_id: cast(InstallPlanningBundle, object())},
    )

    rows = adapter(_observation(), None, _context(adapter)).to_dict()["value"]["capabilities"]

    assert [row["capability_id"] for row in rows] == [valid.capability_id]
    assert rows[0]["authority"]["type"] == "load"
    assert installs.calls == [invalid.capability_id, invalid.capability_id]


def test_direct_load_authority_valid_to_unavailable_is_global_across_observations() -> None:
    candidate = _candidate("skill:local", "load")
    adapter, source, facts, materials, installs = _adapter((candidate,))

    class _UnavailableMaterials(_Materials):
        unavailable = False

        def load_bundle(
            self,
            presentation: CapabilityCandidate,
        ) -> CatalogLoadPlanningBundle | None:
            if self.unavailable:
                raise CandidateAuthorityUnavailable("injected candidate-local failure")
            return super().load_bundle(presentation)

    changing = _UnavailableMaterials(
        dict(materials.values),
        material_snapshot_digest=materials.material_snapshot_digest,
    )
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=adapter.planner,
        source=source,
        benefit_facts_port=facts,
        material_port=changing,
        install_bundle_port=installs,
        planner_version="planner-v3",
        catalog_namespace_digest=_digest("catalog-namespace"),
    )
    adapter(_observation(), None, _context(adapter))
    changing.unavailable = True

    with pytest.raises(PlannerValidationError, match="load authority output drifted"):
        adapter(
            _observation(active=("skill:unrelated",)),
            None,
            _context(adapter),
        )


def test_direct_install_authority_valid_to_unavailable_is_global_across_observations() -> None:
    candidate = _candidate("skill:remote", "install")
    adapter, source, facts, materials, installs = _adapter((candidate,))

    class _UnavailableInstalls(_Installs):
        unavailable = False

        def describe_bundle(
            self,
            capability_id: str,
            kind: str,
        ) -> InstallPlanningBundle | None:
            if self.unavailable:
                raise CandidateAuthorityUnavailable("injected candidate-local failure")
            return super().describe_bundle(capability_id, kind)

    changing = _UnavailableInstalls(
        dict(installs.values),
        installation_snapshot_digest=installs.installation_snapshot_digest,
    )
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=adapter.planner,
        source=source,
        benefit_facts_port=facts,
        material_port=materials,
        install_bundle_port=changing,
        planner_version="planner-v3",
        catalog_namespace_digest=_digest("catalog-namespace"),
    )
    adapter(_observation(), None, _context(adapter))
    changing.unavailable = True

    with pytest.raises(PlannerValidationError, match="install authority output drifted"):
        adapter(
            _observation(active=("skill:unrelated",)),
            None,
            _context(adapter),
        )


def test_load_authority_rejects_a_different_retrieval_presentation() -> None:
    candidate = _candidate("skill:local", "load")
    substituted = _candidate("skill:local", "load", source_salt="substituted")
    adapter, _source, _facts, materials, _installs = _adapter(
        (candidate,),
        materials={candidate.capability_id: _catalog_load_bundle(substituted)},
    )

    value = adapter(_observation(), None, _context(adapter)).to_dict()["value"]

    assert value["status"] == "abstained"
    assert value["capabilities"] == []
    assert materials.calls == [candidate.capability_id, candidate.capability_id]


def test_typed_load_bundle_cannot_mix_cross_snapshot_material() -> None:
    invalid = _candidate("skill:cross-snapshot", "load")
    valid = _candidate("skill:valid-load", "load")
    cross_snapshot = _load_descriptor(
        invalid.capability_id,
        provenance_salt="other-material-snapshot",
    )
    adapter, _source, _facts, _materials, _installs = _adapter(
        (invalid, valid),
        materials={
            invalid.capability_id: _catalog_load_bundle(
                invalid,
                descriptor=cross_snapshot,
            ),
            valid.capability_id: _catalog_load_bundle(valid),
        },
    )

    rows = adapter(_observation(), None, _context(adapter)).to_dict()["value"]["capabilities"]

    assert [row["capability_id"] for row in rows] == [valid.capability_id]
    assert rows[0]["authority"]["type"] == "load"


def test_end_validation_catches_same_digest_output_mutation_on_first_call() -> None:
    candidate = _candidate("skill:local", "load")
    adapter, source, facts, materials, installs = _adapter((candidate,))
    first = materials.values[candidate.capability_id]
    replacement = _catalog_load_bundle(
        candidate,
        descriptor=_load_descriptor(candidate.capability_id, material_salt="replacement"),
    )

    class _MutatingMaterials(_Materials):
        def load_bundle(
            self,
            presentation: CapabilityCandidate,
        ) -> CatalogLoadPlanningBundle | None:
            value = super().load_bundle(presentation)
            if len(self.calls) == 1:
                self.values[presentation.capability_id] = replacement
            return value

    mutating = _MutatingMaterials(
        {candidate.capability_id: cast(CatalogLoadPlanningBundle, first)},
        material_snapshot_digest=materials.material_snapshot_digest,
    )
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=adapter.planner,
        source=source,
        benefit_facts_port=facts,
        material_port=mutating,
        install_bundle_port=installs,
        planner_version="planner-v3",
        catalog_namespace_digest=_digest("catalog-namespace"),
    )

    with pytest.raises(PlannerValidationError, match="output drifted"):
        adapter(_observation(), None, _context(adapter))


def test_port_cannot_hide_concurrent_digest_drift_during_a_read() -> None:
    candidate = _candidate("skill:local", "load")
    adapter, source, facts, materials, installs = _adapter((candidate,))

    class _DriftingMaterials(_Materials):
        def load_bundle(
            self,
            presentation: CapabilityCandidate,
        ) -> CatalogLoadPlanningBundle | None:
            value = super().load_bundle(presentation)
            self.material_snapshot_digest = _digest("concurrent-drift")
            return value

    drifting = _DriftingMaterials(
        dict(materials.values),
        material_snapshot_digest=materials.material_snapshot_digest,
    )
    adapter = AuthenticatedReplayDecisionPlannerV3(
        planner=adapter.planner,
        source=source,
        benefit_facts_port=facts,
        material_port=drifting,
        install_bundle_port=installs,
        planner_version="planner-v3",
        catalog_namespace_digest=_digest("catalog-namespace"),
    )

    with pytest.raises(PlannerValidationError, match="material snapshot drifted"):
        adapter(_observation(), None, _context(adapter))


def test_snapshot_output_memory_is_bounded_and_never_evicts_drift_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(planning_v3_module, "_MAX_PINNED_SNAPSHOT_OUTPUTS", 1)
    adapter, _source, _facts, _materials, _installs = _adapter(())

    adapter(_observation(requested_limit=5), None, _context(adapter))
    with pytest.raises(PlannerValidationError, match="bound is exhausted"):
        adapter(_observation(requested_limit=4), None, _context(adapter))
