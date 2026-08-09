from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast

import pytest

from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.engine.replay import (
    MAX_REPLAY_BYTES,
    ReplayInput,
    ReplayValidationError,
    StructuredSurrogate,
)


NOW = "2026-08-02T12:00:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id="host-1",
    )


def _event() -> EngineEvent:
    return EngineEvent(
        event_id="event-1",
        kind="SessionStarted",
        scope=_scope(),
        expected_revision=0,
        occurred_at=NOW,
        payload={"host_level": "query-only"},
        engine_version="engine-v3",
        planner_version="planner-v3",
        policy_version="policy-v3",
        host_descriptor_digest=_digest("host"),
        catalog_snapshot_digest=_digest("catalog"),
        semantic_model_digest=_digest("model"),
        semantic_index_digest=_digest("index"),
        work_signature=_digest("work"),
        random_seed=17,
    )


def _catalog_identity(capability_id: str) -> CatalogCapabilityIdentity:
    return CatalogCapabilityIdentity.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        catalog_namespace_digest=_digest("organization-catalog"),
    )


def _material(capability_id: str, *, salt: str) -> MaterialIdentity:
    return MaterialIdentity.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        content_sha256=_digest(f"content:{salt}"),
        content_bytes=32,
    )


def _presentation(capability_id: str, actionability: str) -> dict[str, object]:
    kind, name = capability_id.split(":", 1)
    return {
        "actionability": actionability,
        "capability_id": capability_id,
        "catalog_entry_digest": _digest(f"entry:{capability_id}"),
        "install_descriptor_digest": None,
        "install_plan_digest": None,
        "kind": kind,
        "matching_signals": ["python"],
        "name": name,
        "normalized_score_ppm": 900_000,
        "reason_codes": ["signal-match"],
    }


def _benefit(tier: str = "executable") -> dict[str, object]:
    return {
        "tier": tier,
        "individual_net_benefit_u": 600_000,
        "marginal_net_benefit_u": 600_000,
    }


def _load_row(capability_id: str = "skill:local") -> dict[str, object]:
    identity = _catalog_identity(capability_id)
    material = _material(capability_id, salt="load")
    descriptor = MaterialDescriptor.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        actionability="load",
        content_sha256=material.content_sha256,
        content_bytes=material.content_bytes,
        estimated_tokens=8,
        provenance_digest=_digest("catalog-snapshot"),
        material_identity_digest=material.identity_digest,
    )
    authorized = AuthorizedMaterial.from_catalog(
        catalog_identity_digest=identity.identity_digest,
        descriptor=descriptor,
    )
    return {
        **_presentation(capability_id, "load"),
        "catalog_identity": identity.to_dict(),
        "benefit": _benefit(),
        "authority": {"type": "load", "material": authorized.to_dict()},
    }


def _install_row(capability_id: str = "skill:remote") -> dict[str, object]:
    identity = _catalog_identity(capability_id)
    result_material = _material(capability_id, salt="install")
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        installer_id="skill-installer",
        plan_digest=_digest(f"plan:{capability_id}"),
        provenance_digest=_digest("install-snapshot"),
        result_material_identity_digest=result_material.identity_digest,
    )
    presentation = _presentation(capability_id, "install")
    presentation["install_descriptor_digest"] = descriptor.descriptor_digest
    presentation["install_plan_digest"] = descriptor.plan_digest
    return {
        **presentation,
        "catalog_identity": identity.to_dict(),
        "benefit": _benefit(),
        "authority": {
            "type": "install",
            "descriptor": descriptor.to_dict(),
            "result_material": result_material.to_dict(),
        },
    }


def _manual_row(capability_id: str = "agent:advisor") -> dict[str, object]:
    return {
        **_presentation(capability_id, "manual"),
        "catalog_identity": _catalog_identity(capability_id).to_dict(),
        "benefit": _benefit("advisory"),
        "authority": {"type": "manual"},
    }


def _audit(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "result_schema_id": "ctx.benefit-selection-result-v1",
        "result_digest": _digest("benefit-result"),
        "policy_schema_id": "ctx.net-benefit-policy-v3",
        "policy_digest": _digest("benefit-policy"),
        "selection_algorithm_id": "ctx.greedy-bounded-subset-exchange-v1",
        "calibration_digest": _digest("calibration"),
        "requested_limit": 5,
        "candidate_pool_count": 3,
        "search_evaluation_count": 9,
    }
    value.update(overrides)
    return value


def _plan(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "ready",
        "abstention_code": None,
        "benefit_audit": _audit(),
        "capabilities": [_load_row(), _install_row(), _manual_row()],
    }
    value.update(overrides)
    return value


def _decision(value: dict[str, object], *, schema_version: int = 3) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=schema_version,
        value=value,
    )


def _replay(
    decision: StructuredSurrogate,
    *,
    reducer_version: str = "ctx-reducer-v3",
) -> ReplayInput:
    event = _event()
    return ReplayInput(
        source_event_content_digest=event.content_digest,
        reducer_event=event,
        decision_surrogate=decision,
        reducer_version=reducer_version,
    )


def _assert_plan_rejected(value: dict[str, object], match: str | None = None) -> None:
    with pytest.raises(ReplayValidationError, match=match):
        _replay(_decision(value))


def test_schema_v3_replay_round_trips_exact_audit_identity_benefit_and_authority() -> None:
    replay = _replay(_decision(_plan()))

    decoded = ReplayInput.from_json(replay.to_json())

    assert decoded == replay
    assert decoded.reducer_version == "ctx-reducer-v3"
    assert decoded.decision_surrogate is not None
    value = decoded.decision_surrogate.value
    assert isinstance(value, Mapping)
    assert set(value) == {"status", "abstention_code", "benefit_audit", "capabilities"}
    capabilities = value["capabilities"]
    assert isinstance(capabilities, tuple)
    install = capabilities[1]
    assert isinstance(install, Mapping)
    authority = install["authority"]
    assert isinstance(authority, Mapping)
    assert set(authority) == {"type", "descriptor", "result_material"}


@pytest.mark.parametrize(
    ("status", "code", "audit"),
    [
        ("abstained", "limit-zero", _audit(requested_limit=0, search_evaluation_count=0)),
        (
            "abstained",
            "no-feasible-capability",
            _audit(candidate_pool_count=0, search_evaluation_count=0),
        ),
        ("abstained", "below-net-benefit", _audit()),
        ("degraded", "planner-failed", None),
        ("degraded", "catalog-unavailable", None),
    ],
)
def test_schema_v3_empty_status_semantics_are_exact(
    status: str,
    code: str,
    audit: dict[str, object] | None,
) -> None:
    replay = _replay(
        _decision(
            _plan(
                status=status,
                abstention_code=code,
                benefit_audit=audit,
                capabilities=[],
            )
        )
    )

    assert replay.decision_surrogate is not None
    assert replay.decision_surrogate.value["status"] == status


@pytest.mark.parametrize(
    "value",
    [
        _plan(assessments=[]),
        _plan(unknown=True),
        _plan(benefit_audit={**_audit(), "assessments": []}),
        _plan(capabilities=[{**_load_row(), "unknown": True}]),
        _plan(capabilities=[{**_load_row(), "benefit": {**_benefit(), "unknown": 1}}]),
        _plan(
            capabilities=[
                {
                    **_install_row(),
                    "authority": {
                        **cast(dict[str, object], _install_row()["authority"]),
                        "unknown": True,
                    },
                }
            ]
        ),
        _plan(capabilities=[{**_manual_row(), "authority": {"type": "manual", "x": 1}}]),
    ],
)
def test_schema_v3_rejects_unknown_fields_and_inline_assessments(
    value: dict[str, object],
) -> None:
    _assert_plan_rejected(value, "missing or unknown")


def test_schema_v3_rejects_nested_identity_digest_and_tier_substitution() -> None:
    foreign_load = _load_row("skill:foreign")
    foreign_install = _install_row("skill:foreign")

    substitutions = []
    catalog_substitution = _load_row()
    catalog_substitution["catalog_identity"] = foreign_load["catalog_identity"]
    substitutions.append(catalog_substitution)
    material_substitution = _load_row()
    material_substitution["authority"] = foreign_load["authority"]
    substitutions.append(material_substitution)
    install_substitution = _install_row()
    install_substitution["authority"] = foreign_install["authority"]
    substitutions.append(install_substitution)
    descriptor_digest_substitution = _install_row()
    descriptor_digest_substitution["install_descriptor_digest"] = _digest("wrong")
    substitutions.append(descriptor_digest_substitution)
    load_advisory = _load_row()
    load_advisory["benefit"] = _benefit("advisory")
    substitutions.append(load_advisory)
    manual_executable = _manual_row()
    manual_executable["benefit"] = _benefit("executable")
    substitutions.append(manual_executable)

    for row in substitutions:
        _assert_plan_rejected(_plan(capabilities=[row]))


@pytest.mark.parametrize(
    "value",
    [
        _plan(status="ready", benefit_audit=None),
        _plan(status="ready", abstention_code="below-net-benefit"),
        _plan(status="ready", capabilities=[]),
        _plan(status="abstained", abstention_code="limit-zero", capabilities=[]),
        _plan(
            status="abstained",
            abstention_code="below-net-benefit",
            benefit_audit=None,
            capabilities=[],
        ),
        _plan(
            status="degraded",
            abstention_code="planner-failed",
            benefit_audit=_audit(),
            capabilities=[],
        ),
        _plan(capabilities=[_load_row()] * 6),
        _plan(benefit_audit=_audit(requested_limit=2)),
        _plan(benefit_audit=_audit(candidate_pool_count=2)),
    ],
)
def test_schema_v3_rejects_inconsistent_status_bounds_and_audit(
    value: dict[str, object],
) -> None:
    _assert_plan_rejected(value)


@pytest.mark.parametrize(
    "audit",
    [
        _audit(result_schema_id="ctx.benefit-selection-result-v2"),
        _audit(policy_schema_id="ctx.net-benefit-policy-v2"),
        _audit(selection_algorithm_id="ctx.other-selection-v1"),
        _audit(result_digest="x" * 64),
        _audit(requested_limit=6),
        _audit(candidate_pool_count=513),
        _audit(search_evaluation_count=1_000_001),
    ],
)
def test_schema_v3_rejects_invalid_benefit_audit(audit: dict[str, object]) -> None:
    _assert_plan_rejected(_plan(benefit_audit=audit))


@pytest.mark.parametrize(
    "value",
    [
        _plan(benefit_audit=_audit(search_evaluation_count=0)),
        _plan(
            status="abstained",
            abstention_code="below-net-benefit",
            benefit_audit=_audit(candidate_pool_count=0, search_evaluation_count=0),
            capabilities=[],
        ),
        _plan(
            status="abstained",
            abstention_code="below-net-benefit",
            benefit_audit=_audit(candidate_pool_count=1, search_evaluation_count=0),
            capabilities=[],
        ),
        _plan(
            status="abstained",
            abstention_code="no-feasible-capability",
            benefit_audit=_audit(
                requested_limit=0, candidate_pool_count=0, search_evaluation_count=0
            ),
            capabilities=[],
        ),
    ],
)
def test_schema_v3_rejects_impossible_compact_benefit_outcomes(
    value: dict[str, object],
) -> None:
    _assert_plan_rejected(value, "inconsistent")


def test_schema_v3_uses_benefit_canonical_order_not_legacy_score_order() -> None:
    wrong = [_manual_row(), _install_row(), _load_row()]

    _assert_plan_rejected(_plan(capabilities=wrong), "canonical")


def test_reducer_decision_compatibility_matrix_is_closed() -> None:
    v1 = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=1,
        value={"status": "abstained", "abstention_code": "no-signals", "capabilities": []},
    )
    v2 = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=2,
        value={
            "status": "abstained",
            "abstention_code": "no-relevant-capability",
            "capabilities": [],
        },
    )
    v3 = _decision(_plan())

    assert StructuredSurrogate.from_json(v2.to_json()) == v2
    assert _replay(v1, reducer_version="ctx-reducer-v2").decision_surrogate == v1
    assert _replay(v3, reducer_version="ctx-reducer-v3").decision_surrogate == v3
    for decision, reducer in (
        (v1, "ctx-reducer-v3"),
        (v2, "ctx-reducer-v1"),
        (v2, "ctx-reducer-v2"),
        (v2, "ctx-reducer-v3"),
        (v3, "ctx-reducer-v2"),
    ):
        with pytest.raises(ReplayValidationError, match="not compatible"):
            _replay(decision, reducer_version=reducer)


def test_replay_decoder_rejects_payload_over_64_kib_before_nested_validation() -> None:
    replay = _replay(_decision(_plan())).to_dict()
    replay["padding"] = "x" * MAX_REPLAY_BYTES
    encoded = json.dumps(replay, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert len(encoded.encode()) > MAX_REPLAY_BYTES

    with pytest.raises(ReplayValidationError, match="size limit"):
        ReplayInput.from_json(encoded)


def test_schema_v3_replay_does_not_retain_mutable_input_mappings() -> None:
    value = _plan()
    replay = _replay(_decision(value))
    value["capabilities"] = []

    assert replay.decision_surrogate is not None
    frozen_value = replay.decision_surrogate.value
    assert isinstance(frozen_value, Mapping)
    capabilities = frozen_value["capabilities"]
    assert isinstance(capabilities, tuple)
    assert len(capabilities) == 3
