"""Typed, ephemeral capability material prepared for one provider exposure."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from ctx.engine.capability_schema import validate_capability_identity
from ctx.engine.lineage import InstalledMaterialLineage

if TYPE_CHECKING:
    from ctx.engine.planner import CapabilitySelection
    from ctx.engine.protocol import HostAction


MAX_PREPARED_CONTENT_BYTES = 6_000
MAX_PREPARED_CONTENT_TOKENS = 1_500
_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_MATERIAL_IDENTITY_SCHEMA = "ctx.material-identity-v2"
_MATERIAL_DESCRIPTOR_V1_SCHEMA = "ctx.material-descriptor-v1"
_MATERIAL_DESCRIPTOR_V2_SCHEMA = "ctx.material-descriptor-v2"
_AUTHORIZED_MATERIAL_SCHEMA = "ctx.authorized-material-v1"


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical token")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _bounded_int(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} is outside its bounded range")
    return value


def _canonical_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
class MaterialIdentity:
    """Snapshot-independent digest identity for exact capability material."""

    capability_id: str
    kind: str
    content_sha256: str
    content_bytes: int
    identity_digest: str

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "capability_id",
            "content_bytes",
            "content_sha256",
            "identity_digest",
            "kind",
            "schema",
        }
    )

    def __post_init__(self) -> None:
        validate_capability_identity(self.capability_id, self.kind)
        _digest(self.content_sha256, "content_sha256")
        _bounded_int(self.content_bytes, "content_bytes", MAX_PREPARED_CONTENT_BYTES)
        if self.content_bytes == 0:
            raise ValueError("content_bytes must describe non-empty material")
        supplied = _digest(self.identity_digest, "identity_digest")
        if supplied != self.recomputed_identity_digest:
            raise ValueError("identity_digest does not match material identity fields")

    def _digest_mapping(self) -> dict[str, str | int]:
        return {
            "capability_id": self.capability_id,
            "content_bytes": self.content_bytes,
            "content_sha256": self.content_sha256,
            "kind": self.kind,
            "schema": _MATERIAL_IDENTITY_SCHEMA,
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
        content_sha256: str,
        content_bytes: int,
    ) -> MaterialIdentity:
        digest_mapping: dict[str, object] = {
            "capability_id": capability_id,
            "content_bytes": content_bytes,
            "content_sha256": content_sha256,
            "kind": kind,
            "schema": _MATERIAL_IDENTITY_SCHEMA,
        }
        return cls(
            capability_id=capability_id,
            kind=kind,
            content_sha256=content_sha256,
            content_bytes=content_bytes,
            identity_digest=_canonical_digest(digest_mapping),
        )

    def to_dict(self) -> dict[str, str | int]:
        return {**self._digest_mapping(), "identity_digest": self.identity_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MaterialIdentity:
        parsed = _exact_mapping(value, cls._FIELDS, "material identity")
        if parsed["schema"] != _MATERIAL_IDENTITY_SCHEMA:
            raise ValueError("material identity schema is unsupported")
        return cls(
            capability_id=parsed["capability_id"],  # type: ignore[arg-type]
            kind=parsed["kind"],  # type: ignore[arg-type]
            content_sha256=parsed["content_sha256"],  # type: ignore[arg-type]
            content_bytes=parsed["content_bytes"],  # type: ignore[arg-type]
            identity_digest=parsed["identity_digest"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MaterialDescriptor:
    """Prose-free catalog fact used to decide whether content is loadable."""

    capability_id: str
    kind: str
    actionability: str
    content_sha256: str | None
    content_bytes: int
    estimated_tokens: int
    provenance_digest: str
    descriptor_digest: str
    material_identity_digest: str | None = None
    schema_version: int = 1

    _V1_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "actionability",
            "capability_id",
            "content_bytes",
            "content_sha256",
            "descriptor_digest",
            "estimated_tokens",
            "kind",
            "material_snapshot_digest",
            "schema",
        }
    )
    _V2_FIELDS: ClassVar[frozenset[str]] = _V1_FIELDS | frozenset({"material_identity_digest"})

    def __post_init__(self) -> None:
        capability_id, _kind = validate_capability_identity(
            self.capability_id,
            self.kind,
        )
        if self.actionability not in {"load", "manual"}:
            raise ValueError("descriptor actionability is unsupported")
        if self.content_sha256 is not None:
            _digest(self.content_sha256, "content_sha256")
        if self.actionability == "load" and self.content_sha256 is None:
            raise ValueError("loadable material requires an exact content digest")
        if self.actionability == "manual" and self.content_sha256 is not None:
            raise ValueError("manual material cannot claim exposed content")
        _bounded_int(self.content_bytes, "content_bytes", MAX_PREPARED_CONTENT_BYTES)
        _bounded_int(
            self.estimated_tokens,
            "estimated_tokens",
            MAX_PREPARED_CONTENT_TOKENS,
        )
        _digest(self.provenance_digest, "provenance_digest")
        descriptor_digest = _digest(self.descriptor_digest, "descriptor_digest")
        if self.schema_version not in {1, 2}:
            raise ValueError("material descriptor schema version is unsupported")
        if self.schema_version == 1:
            if self.material_identity_digest is not None:
                raise ValueError("v1 material descriptor cannot carry material identity")
        else:
            if self.actionability != "load":
                raise ValueError("v2 material descriptor must be loadable")
            supplied_identity = _digest(
                self.material_identity_digest,
                "material_identity_digest",
            )
            expected_identity = MaterialIdentity.create(
                capability_id=capability_id,
                kind=self.kind,
                content_sha256=self.content_sha256 or "",
                content_bytes=self.content_bytes,
            )
            if supplied_identity != expected_identity.identity_digest:
                raise ValueError("material_identity_digest does not match descriptor content")
        if self.recomputed_descriptor_digest != descriptor_digest:
            raise ValueError("descriptor_digest does not match descriptor fields")

    def _digest_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "actionability": self.actionability,
            "capability_id": self.capability_id,
            "content_bytes": self.content_bytes,
            "content_sha256": self.content_sha256,
            "estimated_tokens": self.estimated_tokens,
            "kind": self.kind,
            "material_snapshot_digest": self.provenance_digest,
            "schema": (
                _MATERIAL_DESCRIPTOR_V1_SCHEMA
                if self.schema_version == 1
                else _MATERIAL_DESCRIPTOR_V2_SCHEMA
            ),
        }
        if self.schema_version == 2:
            result["material_identity_digest"] = self.material_identity_digest
        return result

    @property
    def recomputed_descriptor_digest(self) -> str:
        return _canonical_digest(self._digest_mapping())

    @classmethod
    def create(
        cls,
        *,
        capability_id: str,
        kind: str,
        actionability: str,
        content_sha256: str | None,
        content_bytes: int,
        estimated_tokens: int,
        provenance_digest: str,
        material_identity_digest: str | None = None,
    ) -> MaterialDescriptor:
        schema_version = 2 if material_identity_digest is not None else 1
        digest_mapping: dict[str, object] = {
            "actionability": actionability,
            "capability_id": capability_id,
            "content_bytes": content_bytes,
            "content_sha256": content_sha256,
            "estimated_tokens": estimated_tokens,
            "kind": kind,
            "material_snapshot_digest": provenance_digest,
            "schema": (
                _MATERIAL_DESCRIPTOR_V2_SCHEMA
                if schema_version == 2
                else _MATERIAL_DESCRIPTOR_V1_SCHEMA
            ),
        }
        if schema_version == 2:
            digest_mapping["material_identity_digest"] = material_identity_digest
        return cls(
            capability_id=capability_id,
            kind=kind,
            actionability=actionability,
            content_sha256=content_sha256,
            content_bytes=content_bytes,
            estimated_tokens=estimated_tokens,
            provenance_digest=provenance_digest,
            descriptor_digest=_canonical_digest(digest_mapping),
            material_identity_digest=material_identity_digest,
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_mapping(), "descriptor_digest": self.descriptor_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MaterialDescriptor:
        if not isinstance(value, Mapping):
            raise ValueError("material descriptor must be an object")
        schema = value.get("schema")
        if schema == _MATERIAL_DESCRIPTOR_V1_SCHEMA:
            parsed = _exact_mapping(value, cls._V1_FIELDS, "material descriptor")
            schema_version = 1
        elif schema == _MATERIAL_DESCRIPTOR_V2_SCHEMA:
            parsed = _exact_mapping(value, cls._V2_FIELDS, "material descriptor")
            schema_version = 2
        else:
            raise ValueError("material descriptor schema is unsupported")
        return cls(
            capability_id=parsed["capability_id"],  # type: ignore[arg-type]
            kind=parsed["kind"],  # type: ignore[arg-type]
            actionability=parsed["actionability"],  # type: ignore[arg-type]
            content_sha256=parsed["content_sha256"],  # type: ignore[arg-type]
            content_bytes=parsed["content_bytes"],  # type: ignore[arg-type]
            estimated_tokens=parsed["estimated_tokens"],  # type: ignore[arg-type]
            provenance_digest=parsed["material_snapshot_digest"],  # type: ignore[arg-type]
            descriptor_digest=parsed["descriptor_digest"],  # type: ignore[arg-type]
            material_identity_digest=parsed.get("material_identity_digest"),  # type: ignore[arg-type]
            schema_version=schema_version,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizedMaterial:
    """Exact journal-authorized material identity without raw content or paths."""

    capability_id: str
    kind: str
    catalog_identity_digest: str
    material_identity_digest: str
    origin: str
    catalog_material_descriptor: MaterialDescriptor | None = None
    installed_material_lineage: InstalledMaterialLineage | None = None

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "capability_id",
            "catalog_identity_digest",
            "catalog_material_descriptor",
            "installed_material_lineage",
            "kind",
            "material_identity_digest",
            "origin",
            "schema",
        }
    )

    def __post_init__(self) -> None:
        validate_capability_identity(self.capability_id, self.kind)
        _digest(self.catalog_identity_digest, "catalog_identity_digest")
        _digest(self.material_identity_digest, "material_identity_digest")
        if self.origin == "catalog":
            descriptor = self.catalog_material_descriptor
            if not isinstance(descriptor, MaterialDescriptor):
                raise ValueError("catalog material requires an exact catalog descriptor")
            if self.installed_material_lineage is not None:
                raise ValueError(
                    "catalog material requires a descriptor and forbids installed lineage"
                )
            if descriptor.material_identity_digest is None:
                raise ValueError("catalog material requires a material-identity descriptor")
            if (
                self.capability_id,
                self.kind,
                self.material_identity_digest,
            ) != (
                descriptor.capability_id,
                descriptor.kind,
                descriptor.material_identity_digest,
            ):
                raise ValueError("catalog descriptor identity does not match authorized material")
        elif self.origin == "installed":
            if self.catalog_material_descriptor is not None:
                raise ValueError("installed material forbids a catalog descriptor")
            lineage = self.installed_material_lineage
            if not isinstance(lineage, InstalledMaterialLineage):
                raise ValueError("installed material requires exact installed lineage")
            if (
                self.capability_id,
                self.kind,
                self.catalog_identity_digest,
                self.material_identity_digest,
            ) != (
                lineage.capability_id,
                lineage.kind,
                lineage.catalog_identity_digest,
                lineage.material_identity_digest,
            ):
                raise ValueError("installed material identity does not match installed lineage")
        else:
            raise ValueError("material origin must be catalog or installed")

    @classmethod
    def from_catalog(
        cls,
        *,
        catalog_identity_digest: str,
        descriptor: MaterialDescriptor,
    ) -> AuthorizedMaterial:
        if not isinstance(descriptor, MaterialDescriptor):
            raise TypeError("descriptor must be a MaterialDescriptor")
        if descriptor.material_identity_digest is None:
            raise ValueError("catalog material requires a material-identity descriptor")
        return cls(
            capability_id=descriptor.capability_id,
            kind=descriptor.kind,
            catalog_identity_digest=catalog_identity_digest,
            material_identity_digest=descriptor.material_identity_digest,
            origin="catalog",
            catalog_material_descriptor=descriptor,
        )

    @classmethod
    def from_installed(
        cls,
        lineage: InstalledMaterialLineage,
    ) -> AuthorizedMaterial:
        if not isinstance(lineage, InstalledMaterialLineage):
            raise TypeError("lineage must be InstalledMaterialLineage")
        return cls(
            capability_id=lineage.capability_id,
            kind=lineage.kind,
            catalog_identity_digest=lineage.catalog_identity_digest,
            material_identity_digest=lineage.material_identity_digest,
            origin="installed",
            installed_material_lineage=lineage,
        )

    @property
    def material_descriptor_digest(self) -> str | None:
        descriptor = self.catalog_material_descriptor
        return None if descriptor is None else descriptor.descriptor_digest

    @property
    def origin_proof_digest(self) -> str:
        descriptor = self.catalog_material_descriptor
        if descriptor is not None:
            return descriptor.descriptor_digest
        lineage = self.installed_material_lineage
        if lineage is None:  # Narrowing; construction rejects this state.
            raise AssertionError("unreachable missing material origin proof")
        return lineage.lineage_digest

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "catalog_identity_digest": self.catalog_identity_digest,
            "catalog_material_descriptor": (
                None
                if self.catalog_material_descriptor is None
                else self.catalog_material_descriptor.to_dict()
            ),
            "installed_material_lineage": (
                None
                if self.installed_material_lineage is None
                else self.installed_material_lineage.to_dict()
            ),
            "kind": self.kind,
            "material_identity_digest": self.material_identity_digest,
            "origin": self.origin,
            "schema": _AUTHORIZED_MATERIAL_SCHEMA,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AuthorizedMaterial:
        parsed = _exact_mapping(value, cls._FIELDS, "authorized material")
        if parsed["schema"] != _AUTHORIZED_MATERIAL_SCHEMA:
            raise ValueError("authorized material schema is unsupported")
        raw_descriptor = parsed["catalog_material_descriptor"]
        descriptor = (
            None if raw_descriptor is None else MaterialDescriptor.from_dict(raw_descriptor)  # type: ignore[arg-type]
        )
        raw_lineage = parsed["installed_material_lineage"]
        lineage = (
            None if raw_lineage is None else InstalledMaterialLineage.from_dict(raw_lineage)  # type: ignore[arg-type]
        )
        return cls(
            capability_id=parsed["capability_id"],  # type: ignore[arg-type]
            kind=parsed["kind"],  # type: ignore[arg-type]
            catalog_identity_digest=parsed["catalog_identity_digest"],  # type: ignore[arg-type]
            material_identity_digest=parsed["material_identity_digest"],  # type: ignore[arg-type]
            origin=parsed["origin"],  # type: ignore[arg-type]
            catalog_material_descriptor=descriptor,
            installed_material_lineage=lineage,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedCapabilityContent:
    """Raw content kept outside authoritative replay and scoped to one action."""

    capability_id: str
    source_digest: str
    catalog_snapshot_digest: str
    action_id: str
    lease_id: str
    content: str
    content_sha256: str
    content_bytes: int
    estimated_tokens: int

    def __post_init__(self) -> None:
        _token(self.capability_id, "capability_id")
        _digest(self.source_digest, "source_digest")
        _digest(self.catalog_snapshot_digest, "catalog_snapshot_digest")
        _token(self.action_id, "action_id")
        _token(self.lease_id, "lease_id")
        _digest(self.content_sha256, "content_sha256")
        if not isinstance(self.content, str) or not self.content or "\x00" in self.content:
            raise ValueError("prepared content must be non-empty NUL-free text")
        encoded = self.content.encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.content_sha256:
            raise ValueError("prepared content digest does not match content")
        if len(encoded) != self.content_bytes:
            raise ValueError("prepared content byte count does not match content")
        _bounded_int(self.content_bytes, "content_bytes", MAX_PREPARED_CONTENT_BYTES)
        _bounded_int(
            self.estimated_tokens,
            "estimated_tokens",
            MAX_PREPARED_CONTENT_TOKENS,
        )


class CapabilityMaterialPort(Protocol):
    """Host-neutral material boundary; preparation requires an engine action."""

    material_snapshot_digest: str

    def describe(self, capability_id: str, kind: str) -> MaterialDescriptor: ...

    def prepare(
        self,
        action: HostAction,
        selection: CapabilitySelection,
        *,
        expected_catalog_snapshot_digest: str,
        authority: ExposureAuthorizer | None = None,
    ) -> PreparedCapabilityContent: ...


class ExposureAuthorizer(Protocol):
    """Journal-backed authority for one exact pending exposure action."""

    def authorize_exposure(
        self,
        action: HostAction,
        selection: CapabilitySelection,
        *,
        expected_catalog_snapshot_digest: str,
    ) -> None: ...


__all__ = [
    "AuthorizedMaterial",
    "CapabilityMaterialPort",
    "ExposureAuthorizer",
    "MAX_PREPARED_CONTENT_BYTES",
    "MAX_PREPARED_CONTENT_TOKENS",
    "MaterialDescriptor",
    "MaterialIdentity",
    "PreparedCapabilityContent",
]
