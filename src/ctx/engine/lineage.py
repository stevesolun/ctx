"""Pure identity and promotion rules for installed capability material."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ctx.engine.capability_schema import validate_capability_identity

_ACTIONABILITY_STATES = frozenset({"install", "load"})
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CATALOG_IDENTITY_SCHEMA = "ctx.catalog-capability-identity-v1"
_INSTALLED_MATERIAL_LINEAGE_SCHEMA = "ctx.installed-material-lineage-v1"


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _canonical_digest(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _exact_mapping(
    value: object,
    fields: frozenset[str],
    object_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{object_name} must be an object with string fields")
    if set(value) != fields:
        raise ValueError(f"{object_name} has missing or unknown fields")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogCapabilityIdentity:
    """Stable identity independent of ranking, availability, and material version."""

    capability_id: str
    kind: str
    catalog_namespace_digest: str
    identity_digest: str

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "capability_id",
            "catalog_namespace_digest",
            "identity_digest",
            "kind",
            "schema",
        }
    )

    def __post_init__(self) -> None:
        validate_capability_identity(self.capability_id, self.kind)
        _digest(self.catalog_namespace_digest, "catalog_namespace_digest")
        supplied = _digest(self.identity_digest, "identity_digest")
        if supplied != self.recomputed_identity_digest:
            raise ValueError("identity_digest does not match catalog identity fields")

    def _digest_mapping(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "catalog_namespace_digest": self.catalog_namespace_digest,
            "kind": self.kind,
            "schema": _CATALOG_IDENTITY_SCHEMA,
        }

    @property
    def recomputed_identity_digest(self) -> str:
        return _canonical_digest(self._digest_mapping())

    @classmethod
    def create(
        cls,
        *,
        capability_id: str,
        kind: str,
        catalog_namespace_digest: str,
    ) -> CatalogCapabilityIdentity:
        digest_mapping = {
            "capability_id": capability_id,
            "catalog_namespace_digest": catalog_namespace_digest,
            "kind": kind,
            "schema": _CATALOG_IDENTITY_SCHEMA,
        }
        return cls(
            capability_id=capability_id,
            kind=kind,
            catalog_namespace_digest=catalog_namespace_digest,
            identity_digest=_canonical_digest(digest_mapping),
        )

    def to_dict(self) -> dict[str, str]:
        return {**self._digest_mapping(), "identity_digest": self.identity_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CatalogCapabilityIdentity:
        parsed = _exact_mapping(value, cls._FIELDS, "catalog capability identity")
        if parsed["schema"] != _CATALOG_IDENTITY_SCHEMA:
            raise ValueError("catalog capability identity schema is unsupported")
        return cls(
            capability_id=parsed["capability_id"],  # type: ignore[arg-type]
            kind=parsed["kind"],  # type: ignore[arg-type]
            catalog_namespace_digest=parsed["catalog_namespace_digest"],  # type: ignore[arg-type]
            identity_digest=parsed["identity_digest"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InstalledMaterialLineage:
    """Digest-bound proof connecting an install descriptor, action, and receipt."""

    capability_id: str
    kind: str
    catalog_identity_digest: str
    material_identity_digest: str
    origin_install_descriptor_digest: str
    install_action_content_digest: str
    install_receipt_content_digest: str
    lineage_digest: str

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
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
    )

    def __post_init__(self) -> None:
        validate_capability_identity(self.capability_id, self.kind)
        for field_name in (
            "catalog_identity_digest",
            "material_identity_digest",
            "origin_install_descriptor_digest",
            "install_action_content_digest",
            "install_receipt_content_digest",
        ):
            _digest(getattr(self, field_name), field_name)
        supplied = _digest(self.lineage_digest, "lineage_digest")
        if supplied != self.recomputed_lineage_digest:
            raise ValueError("lineage_digest does not match installed material lineage fields")

    def _digest_mapping(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "catalog_identity_digest": self.catalog_identity_digest,
            "install_action_content_digest": self.install_action_content_digest,
            "install_receipt_content_digest": self.install_receipt_content_digest,
            "kind": self.kind,
            "material_identity_digest": self.material_identity_digest,
            "origin_install_descriptor_digest": self.origin_install_descriptor_digest,
            "schema": _INSTALLED_MATERIAL_LINEAGE_SCHEMA,
        }

    @property
    def recomputed_lineage_digest(self) -> str:
        return _canonical_digest(self._digest_mapping())

    @classmethod
    def create(
        cls,
        *,
        capability_id: str,
        kind: str,
        catalog_identity_digest: str,
        material_identity_digest: str,
        origin_install_descriptor_digest: str,
        install_action_content_digest: str,
        install_receipt_content_digest: str,
    ) -> InstalledMaterialLineage:
        digest_mapping = {
            "capability_id": capability_id,
            "catalog_identity_digest": catalog_identity_digest,
            "install_action_content_digest": install_action_content_digest,
            "install_receipt_content_digest": install_receipt_content_digest,
            "kind": kind,
            "material_identity_digest": material_identity_digest,
            "origin_install_descriptor_digest": origin_install_descriptor_digest,
            "schema": _INSTALLED_MATERIAL_LINEAGE_SCHEMA,
        }
        return cls(
            capability_id=capability_id,
            kind=kind,
            catalog_identity_digest=catalog_identity_digest,
            material_identity_digest=material_identity_digest,
            origin_install_descriptor_digest=origin_install_descriptor_digest,
            install_action_content_digest=install_action_content_digest,
            install_receipt_content_digest=install_receipt_content_digest,
            lineage_digest=_canonical_digest(digest_mapping),
        )

    def to_dict(self) -> dict[str, str]:
        return {**self._digest_mapping(), "lineage_digest": self.lineage_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InstalledMaterialLineage:
        parsed = _exact_mapping(value, cls._FIELDS, "installed material lineage")
        if parsed["schema"] != _INSTALLED_MATERIAL_LINEAGE_SCHEMA:
            raise ValueError("installed material lineage schema is unsupported")
        return cls(
            capability_id=parsed["capability_id"],  # type: ignore[arg-type]
            kind=parsed["kind"],  # type: ignore[arg-type]
            catalog_identity_digest=parsed["catalog_identity_digest"],  # type: ignore[arg-type]
            material_identity_digest=parsed["material_identity_digest"],  # type: ignore[arg-type]
            origin_install_descriptor_digest=parsed["origin_install_descriptor_digest"],  # type: ignore[arg-type]
            install_action_content_digest=parsed["install_action_content_digest"],  # type: ignore[arg-type]
            install_receipt_content_digest=parsed["install_receipt_content_digest"],  # type: ignore[arg-type]
            lineage_digest=parsed["lineage_digest"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityLineageBinding:
    """Availability-specific binding compared by the pure promotion classifier."""

    capability_id: str
    kind: str
    catalog_identity_digest: str
    actionability: str
    material_identity_digest: str
    install_descriptor_digest: str | None = None
    installed_material_lineage_digest: str | None = None

    def __post_init__(self) -> None:
        validate_capability_identity(self.capability_id, self.kind)
        _digest(self.catalog_identity_digest, "catalog_identity_digest")
        _digest(self.material_identity_digest, "material_identity_digest")
        if self.actionability not in _ACTIONABILITY_STATES:
            raise ValueError("actionability must be install or load")
        if self.actionability == "install":
            _digest(self.install_descriptor_digest, "install_descriptor_digest")
        elif self.install_descriptor_digest is not None:
            raise ValueError("load binding cannot carry an install descriptor")
        if self.installed_material_lineage_digest is not None:
            _digest(
                self.installed_material_lineage_digest,
                "installed_material_lineage_digest",
            )
        if self.actionability == "load" and self.installed_material_lineage_digest is not None:
            raise ValueError("load binding cannot carry installed lineage proof")


@dataclass(frozen=True, slots=True, kw_only=True)
class LineageTransition:
    """Closed result of comparing one current and proposed material binding."""

    transition: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.transition not in {"unchanged", "install-to-load", "rejected"}:
            raise ValueError("lineage transition is unsupported")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("reason_code must be non-empty text")

    @property
    def allowed(self) -> bool:
        return self.transition != "rejected"


def _rejected(reason_code: str) -> LineageTransition:
    return LineageTransition(transition="rejected", reason_code=reason_code)


def classify_lineage_transition(
    current: CapabilityLineageBinding,
    proposed: CapabilityLineageBinding,
    *,
    installed_lineage: InstalledMaterialLineage | None,
    has_pending_effect: bool,
) -> LineageTransition:
    """Allow only an unchanged binding or exact receipt-confirmed install promotion."""

    if not isinstance(current, CapabilityLineageBinding) or not isinstance(
        proposed, CapabilityLineageBinding
    ):
        raise TypeError("lineage classification requires typed current and proposed bindings")
    if type(has_pending_effect) is not bool:
        raise TypeError("has_pending_effect must be a boolean")
    if installed_lineage is not None and not isinstance(
        installed_lineage, InstalledMaterialLineage
    ):
        raise TypeError("installed_lineage must be InstalledMaterialLineage or None")

    if (current.capability_id, current.kind) != (proposed.capability_id, proposed.kind):
        return _rejected("capability-identity-mismatch")
    if current.catalog_identity_digest != proposed.catalog_identity_digest:
        return _rejected("catalog-identity-mismatch")
    if current == proposed:
        return LineageTransition(transition="unchanged", reason_code="exact-binding")
    if current.actionability != "install" or proposed.actionability != "load":
        return _rejected("unsupported-lineage-transition")
    if current.material_identity_digest != proposed.material_identity_digest:
        return _rejected("material-identity-mismatch")
    if installed_lineage is None:
        return _rejected("installed-lineage-missing")
    if has_pending_effect:
        return _rejected("pending-effect")
    if (installed_lineage.capability_id, installed_lineage.kind) != (
        current.capability_id,
        current.kind,
    ):
        return _rejected("lineage-capability-identity-mismatch")
    if installed_lineage.catalog_identity_digest != current.catalog_identity_digest:
        return _rejected("lineage-catalog-identity-mismatch")
    if installed_lineage.material_identity_digest != current.material_identity_digest:
        return _rejected("lineage-material-identity-mismatch")
    if installed_lineage.origin_install_descriptor_digest != current.install_descriptor_digest:
        return _rejected("lineage-install-descriptor-mismatch")
    if current.installed_material_lineage_digest is None:
        return _rejected("lineage-binding-missing")
    if installed_lineage.lineage_digest != current.installed_material_lineage_digest:
        return _rejected("lineage-proof-digest-mismatch")
    return LineageTransition(
        transition="install-to-load",
        reason_code="exact-receipt-material-lineage",
    )


__all__ = [
    "CatalogCapabilityIdentity",
    "CapabilityLineageBinding",
    "InstalledMaterialLineage",
    "LineageTransition",
    "classify_lineage_transition",
]
