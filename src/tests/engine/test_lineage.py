from __future__ import annotations

import hashlib

import pytest

from ctx import engine as engine_api
from ctx.engine.lineage import (
    CatalogCapabilityIdentity,
    CapabilityLineageBinding,
    InstalledMaterialLineage,
    classify_lineage_transition,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _binding(
    *,
    capability_id: str = "skill:python-testing",
    kind: str = "skill",
    catalog_identity_digest: str | None = None,
    actionability: str = "install",
    material_identity_digest: str | None = None,
    install_descriptor_digest: str | None = None,
    installed_material_lineage_digest: str | None = None,
) -> CapabilityLineageBinding:
    return CapabilityLineageBinding(
        capability_id=capability_id,
        kind=kind,
        catalog_identity_digest=(catalog_identity_digest or _digest("catalog-identity")),
        actionability=actionability,
        material_identity_digest=(material_identity_digest or _digest("material")),
        install_descriptor_digest=(
            _digest("install-descriptor")
            if actionability == "install" and install_descriptor_digest is None
            else install_descriptor_digest
        ),
        installed_material_lineage_digest=installed_material_lineage_digest,
    )


def _installed_lineage(
    *,
    capability_id: str = "skill:python-testing",
    kind: str = "skill",
    catalog_identity_digest: str | None = None,
    material_identity_digest: str | None = None,
    origin_install_descriptor_digest: str | None = None,
    install_action_content_digest: str | None = None,
    install_receipt_content_digest: str | None = None,
) -> InstalledMaterialLineage:
    return InstalledMaterialLineage.create(
        capability_id=capability_id,
        kind=kind,
        catalog_identity_digest=(catalog_identity_digest or _digest("catalog-identity")),
        material_identity_digest=(material_identity_digest or _digest("material")),
        origin_install_descriptor_digest=(
            origin_install_descriptor_digest or _digest("install-descriptor")
        ),
        install_action_content_digest=(install_action_content_digest or _digest("install-action")),
        install_receipt_content_digest=(
            install_receipt_content_digest or _digest("install-receipt")
        ),
    )


def test_catalog_identity_is_stable_across_availability_and_metadata_changes() -> None:
    first = CatalogCapabilityIdentity.create(
        capability_id="skill:python-testing",
        kind="skill",
        catalog_namespace_digest=_digest("organization-catalog"),
    )
    second = CatalogCapabilityIdentity.create(
        capability_id="skill:python-testing",
        kind="skill",
        catalog_namespace_digest=_digest("organization-catalog"),
    )

    assert second == first
    assert CatalogCapabilityIdentity.from_dict(first.to_dict()) == first
    assert set(first.to_dict()) == {
        "capability_id",
        "catalog_namespace_digest",
        "identity_digest",
        "kind",
        "schema",
    }

    other_namespace = CatalogCapabilityIdentity.create(
        capability_id="skill:python-testing",
        kind="skill",
        catalog_namespace_digest=_digest("user-catalog"),
    )
    assert other_namespace.identity_digest != first.identity_digest


def test_installed_material_lineage_has_canonical_round_trip_and_digest() -> None:
    lineage = _installed_lineage()

    assert InstalledMaterialLineage.from_dict(lineage.to_dict()) == lineage
    assert lineage.recomputed_lineage_digest == lineage.lineage_digest
    assert set(lineage.to_dict()) == {
        "capability_id",
        "catalog_identity_digest",
        "install_action_content_digest",
        "install_receipt_content_digest",
        "kind",
        "lineage_digest",
        "material_identity_digest",
        "origin_install_descriptor_digest",
        "schema",
    }
    assert engine_api.InstalledMaterialLineage is InstalledMaterialLineage


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("capability_id", "skill:other-testing"),
        ("kind", "agent"),
        ("catalog_identity_digest", _digest("other-catalog")),
        ("material_identity_digest", _digest("other-material")),
        ("origin_install_descriptor_digest", _digest("other-descriptor")),
        ("install_action_content_digest", _digest("other-action")),
        ("install_receipt_content_digest", _digest("other-receipt")),
    ],
)
def test_installed_material_lineage_rejects_substituted_binding_fields(
    field_name: str,
    value: str,
) -> None:
    raw = _installed_lineage().to_dict()
    raw[field_name] = value

    with pytest.raises(ValueError, match="lineage|identity|kind"):
        InstalledMaterialLineage.from_dict(raw)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("capability_id", "agent:python-testing"),
        ("kind", "agent"),
        ("catalog_namespace_digest", _digest("substituted-namespace")),
        ("identity_digest", _digest("forged-identity")),
    ],
)
def test_catalog_identity_rejects_substituted_identity_fields(
    field_name: str,
    value: str,
) -> None:
    identity = CatalogCapabilityIdentity.create(
        capability_id="skill:python-testing",
        kind="skill",
        catalog_namespace_digest=_digest("organization-catalog"),
    )
    raw = identity.to_dict()
    raw[field_name] = value

    with pytest.raises(ValueError, match="identity|kind"):
        CatalogCapabilityIdentity.from_dict(raw)


def test_exact_install_to_load_promotion_is_allowed_without_identity_churn() -> None:
    lineage = _installed_lineage()
    current = _binding(installed_material_lineage_digest=lineage.lineage_digest)
    proposed = _binding(
        actionability="load",
        install_descriptor_digest=None,
    )

    result = classify_lineage_transition(
        current,
        proposed,
        installed_lineage=lineage,
        has_pending_effect=False,
    )

    assert result.transition == "install-to-load"
    assert result.allowed
    assert result.reason_code == "exact-receipt-material-lineage"


@pytest.mark.parametrize(
    ("proposed", "lineage", "pending", "reason"),
    [
        (
            _binding(
                actionability="load",
                material_identity_digest=_digest("other-material"),
                install_descriptor_digest=None,
            ),
            _installed_lineage(),
            False,
            "material-identity-mismatch",
        ),
        (
            _binding(
                actionability="load",
                catalog_identity_digest=_digest("other-catalog-identity"),
                install_descriptor_digest=None,
            ),
            _installed_lineage(),
            False,
            "catalog-identity-mismatch",
        ),
        (
            _binding(actionability="load", install_descriptor_digest=None),
            None,
            False,
            "installed-lineage-missing",
        ),
        (
            _binding(actionability="load", install_descriptor_digest=None),
            _installed_lineage(),
            True,
            "pending-effect",
        ),
    ],
)
def test_promotion_rejects_different_material_identity_missing_receipt_or_pending_effect(
    proposed: CapabilityLineageBinding,
    lineage: InstalledMaterialLineage | None,
    pending: bool,
    reason: str,
) -> None:
    result = classify_lineage_transition(
        _binding(
            installed_material_lineage_digest=(None if lineage is None else lineage.lineage_digest)
        ),
        proposed,
        installed_lineage=lineage,
        has_pending_effect=pending,
    )

    assert result.transition == "rejected"
    assert not result.allowed
    assert result.reason_code == reason


def test_exact_unchanged_binding_does_not_require_installed_lineage() -> None:
    current = _binding(actionability="load", install_descriptor_digest=None)

    result = classify_lineage_transition(
        current,
        current,
        installed_lineage=None,
        has_pending_effect=True,
    )

    assert result.transition == "unchanged"
    assert result.allowed
    assert result.reason_code == "exact-binding"


def test_promotion_rejects_a_typed_proof_not_bound_to_current_install_state() -> None:
    result = classify_lineage_transition(
        _binding(),
        _binding(actionability="load", install_descriptor_digest=None),
        installed_lineage=_installed_lineage(),
        has_pending_effect=False,
    )

    assert result.transition == "rejected"
    assert result.reason_code == "lineage-binding-missing"


@pytest.mark.parametrize(
    ("lineage", "reason"),
    [
        (
            _installed_lineage(capability_id="skill:other-testing"),
            "lineage-capability-identity-mismatch",
        ),
        (
            _installed_lineage(
                capability_id="agent:python-testing",
                kind="agent",
            ),
            "lineage-capability-identity-mismatch",
        ),
        (
            _installed_lineage(catalog_identity_digest=_digest("other-catalog")),
            "lineage-catalog-identity-mismatch",
        ),
        (
            _installed_lineage(material_identity_digest=_digest("other-material")),
            "lineage-material-identity-mismatch",
        ),
        (
            _installed_lineage(origin_install_descriptor_digest=_digest("other-descriptor")),
            "lineage-install-descriptor-mismatch",
        ),
        (
            _installed_lineage(install_action_content_digest=_digest("other-action")),
            "lineage-proof-digest-mismatch",
        ),
        (
            _installed_lineage(install_receipt_content_digest=_digest("other-receipt")),
            "lineage-proof-digest-mismatch",
        ),
    ],
)
def test_promotion_rejects_substituted_installed_lineage(
    lineage: InstalledMaterialLineage,
    reason: str,
) -> None:
    expected = _installed_lineage()
    result = classify_lineage_transition(
        _binding(installed_material_lineage_digest=expected.lineage_digest),
        _binding(actionability="load", install_descriptor_digest=None),
        installed_lineage=lineage,
        has_pending_effect=False,
    )

    assert result.transition == "rejected"
    assert result.reason_code == reason


@pytest.mark.parametrize(
    ("capability_id", "kind"),
    [
        ("mcp:legacy", "mcp"),
        ("skill:python-testing:extra", "skill"),
        ("skill:" + "x" * 123, "skill"),
    ],
)
def test_lineage_identities_use_the_shared_authoritative_grammar(
    capability_id: str,
    kind: str,
) -> None:
    with pytest.raises(ValueError, match="kind.*capability_id|canonical identity"):
        CatalogCapabilityIdentity.create(
            capability_id=capability_id,
            kind=kind,
            catalog_namespace_digest=_digest("catalog"),
        )
