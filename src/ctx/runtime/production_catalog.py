"""Production-owned release-pinned CTX query catalog factory.

The release trust anchor is the literal root-manifest SHA-256 in this module.
It detects packaged-data substitution within this installed CTX build.  It is
not a cryptographic signature over the wheel and does not provide downgrade
protection across separately installed historical CTX releases.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from importlib.resources import files
from typing import Final

from ctx import __version__
from ctx.engine.content import MaterialIdentity, PreparedCapabilityContent
from ctx.engine.engine import CtxEngine, _PromptContextMaterialPermit
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3
from ctx.engine.protocol import HostAction
from ctx.runtime.authenticated_benefit import (
    MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES,
    load_reviewed_net_benefit_policy_bytes,
)
from ctx.runtime.benefit_closure import (
    QueryHostPolicyAuthority,
    ReviewedBenefitAuthorities,
    load_reviewed_benefit_profiles_bytes,
)
from ctx.runtime.eligible_catalog import (
    ELIGIBLE_ENTRY_PROJECTION,
    MAX_ELIGIBLE_CATALOG_BYTES,
    PreparedEligibleCatalogQuery,
    ReviewedQueryCatalog,
    load_eligible_catalog_layer_bytes,
    open_reviewed_query_catalog,
)
from ctx.runtime.query_work import normalize_query_work
from ctx.runtime.release_material import (
    MAX_RELEASE_SKILL_MATERIAL_BYTES,
    RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE,
    RELEASE_LOAD_SKILL_MATERIAL_RESOURCE,
    ReleasePinnedSkillMaterialSource,
    open_release_pinned_skill_material,
)
from ctx.runtime.install_execution import InstallDriverRequest


RELEASE_QUERY_CATALOG_ROOT_SCHEMA: Final = "ctx.release-pinned-query-catalog-root-v1"
RELEASE_QUERY_CATALOG_ROOT_RESOURCE: Final = "release-query-catalog-root-v1.json"
RELEASE_QUERY_CATALOG_ROOT_SHA256: Final = (
    "fc05acfcee2a3a9a901aac8a909af3af83fc2e80f50c984d212bb228d50b0b8b"
)
RELEASE_QUERY_CATALOG_MODE: Final = "reviewed"
RELEASE_QUERY_CATALOG_SEQUENCE: Final = 4
RELEASE_ELIGIBLE_CATALOG_COMPILER_SCHEMA: Final = "ctx.release-eligible-catalog-compiler-v1"
MAX_RELEASE_ROOT_BYTES: Final = 64 * 1024

_ROOT_FIELDS = frozenset(
    {
        "assets",
        "authority",
        "bindings",
        "mode",
        "package_version",
        "release_sequence",
        "schema",
    }
)
_ASSET_SET_FIELDS = frozenset(
    {"catalog", "install_materials", "load_materials", "policy", "profiles"}
)
_ASSET_FIELDS = frozenset({"name", "sha256"})
_AUTHORITY_FIELDS = frozenset(
    {"authority_digest", "authority_id", "authority_kind", "catalog_layer_kind"}
)
_BINDING_FIELDS = frozenset(
    {"candidate_projection_version", "catalog_namespace_digest", "compiler_schema"}
)
_EXPECTED_ASSET_NAMES: Final = {
    "catalog": "benefit-eligible-catalog-v1.json",
    "install_materials": RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE,
    "load_materials": RELEASE_LOAD_SKILL_MATERIAL_RESOURCE,
    "policy": "reviewed-net-benefit-policy-v1.json",
    "profiles": "reviewed-benefit-profiles-v2.json",
}
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_FACTORY_TOKEN = object()


class ReleaseCatalogError(ValueError):
    """The packaged release catalog failed its code-owned trust contract."""


def _fail(message: str) -> ReleaseCatalogError:
    return ReleaseCatalogError(message)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("release root contains a duplicate object key")
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


def _read_resource(name: str, *, maximum_bytes: int) -> bytes:
    if name not in {RELEASE_QUERY_CATALOG_ROOT_RESOURCE, *_EXPECTED_ASSET_NAMES.values()}:
        raise _fail("release root requested an undeclared package resource")
    try:
        resource = files("ctx.assets").joinpath(name)
        with resource.open("rb") as stream:
            value = stream.read(maximum_bytes + 1)
    except (OSError, TypeError, ValueError):
        raise _fail("release catalog resource is unavailable") from None
    if not isinstance(value, bytes) or len(value) > maximum_bytes:
        raise _fail("release catalog resource exceeds its byte bound")
    return value


def _decode_release_root(value: bytes) -> Mapping[str, object]:
    if hashlib.sha256(value).hexdigest() != RELEASE_QUERY_CATALOG_ROOT_SHA256:
        raise _fail("release root does not match the code-owned SHA-256 pin")
    try:
        decoded = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                _fail("release root contains a non-finite number")
            ),
        )
    except ReleaseCatalogError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise _fail("release root must be valid UTF-8 JSON") from None
    if not isinstance(decoded, dict):
        raise _fail("release root must be an object")
    canonical = _canonical_bytes(decoded)
    if value not in (canonical, canonical + b"\n"):
        raise _fail("release root must use canonical JSON encoding")
    return decoded


def _asset_bindings(root: Mapping[str, object]) -> dict[str, tuple[str, str]]:
    assets = _closed(root["assets"], _ASSET_SET_FIELDS, "release root assets")
    result: dict[str, tuple[str, str]] = {}
    for role, expected_name in _EXPECTED_ASSET_NAMES.items():
        asset = _closed(assets[role], _ASSET_FIELDS, f"release root {role} asset")
        name = asset["name"]
        if name != expected_name:
            raise _fail("release root asset name is not code-approved")
        result[role] = (expected_name, _digest(asset["sha256"], f"{role} asset SHA-256"))
    return result


class ReleasePinnedQueryCatalog:
    """Deep production facade over one release-owned reviewed catalog."""

    _catalog: ReviewedQueryCatalog
    _materials: ReleasePinnedSkillMaterialSource
    _close_requested: bool
    _closed: bool
    _in_flight: int
    _lock: threading.Lock
    mode: str
    release_root_digest: str
    release_sequence: int

    __slots__ = (
        "_catalog",
        "_close_requested",
        "_closed",
        "_in_flight",
        "_lock",
        "_materials",
        "mode",
        "release_root_digest",
        "release_sequence",
    )

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("release-pinned query catalogs must be opened by the release factory")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        catalog: ReviewedQueryCatalog,
        materials: ReleasePinnedSkillMaterialSource,
        mode: str,
        release_sequence: int,
    ) -> ReleasePinnedQueryCatalog:
        if factory_token is not _FACTORY_TOKEN:
            raise TypeError("release-pinned query catalogs must be opened by the release factory")
        instance = object.__new__(cls)
        instance._catalog = catalog
        instance._materials = materials
        instance._close_requested = False
        instance._closed = False
        instance._in_flight = 0
        instance._lock = threading.Lock()
        instance.mode = mode
        instance.release_sequence = release_sequence
        instance.release_root_digest = RELEASE_QUERY_CATALOG_ROOT_SHA256
        return instance

    def prepare_query(
        self,
        *,
        task: str,
        language: str,
        host_policy: QueryHostPolicyAuthority,
    ) -> PreparedEligibleCatalogQuery:
        with self._lock:
            if self._closed or self._close_requested:
                raise _fail("release-pinned query catalog is closed")
            self._in_flight += 1
            catalog = self._catalog
            vocabulary = catalog.vocabulary
        try:
            observation = normalize_query_work(
                task=task,
                language=language,
                vocabulary=vocabulary,
                expected_catalog_namespace_digest=vocabulary.catalog_namespace_digest,
                expected_graph_artifact_sha256=vocabulary.graph_artifact_sha256,
            )
            return catalog.prepare_query(
                observation=observation,
                host_policy=host_policy,
            )
        finally:
            close_catalog: ReviewedQueryCatalog | None = None
            with self._lock:
                self._in_flight -= 1
                if self._in_flight == 0 and self._close_requested and not self._closed:
                    self._closed = True
                    close_catalog = self._catalog
            if close_catalog is not None:
                close_catalog.close()

    def prepare_prompt_context(
        self,
        action: HostAction,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        *,
        expected_catalog_snapshot_digest: str,
        authority: _PromptContextMaterialPermit,
        external_material_source: object | None = None,
    ) -> tuple[PreparedCapabilityContent, ...]:
        """Prepare the exact reviewed prompt bundle under journal authority."""

        with self._lock:
            if self._closed or self._close_requested:
                raise _fail("release-pinned query catalog is closed")
            self._in_flight += 1
            materials = self._materials
        try:
            return materials.prepare_prompt_context(
                action,
                selections,
                expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
                authority=authority,
                external_material_source=external_material_source,
            )
        finally:
            close_catalog: ReviewedQueryCatalog | None = None
            close_materials: ReleasePinnedSkillMaterialSource | None = None
            with self._lock:
                self._in_flight -= 1
                if self._in_flight == 0 and self._close_requested and not self._closed:
                    self._closed = True
                    close_catalog = self._catalog
                    close_materials = self._materials
            if close_materials is not None:
                close_materials.close()
            if close_catalog is not None:
                close_catalog.close()

    def _load_install_skill_body(
        self,
        engine: CtxEngine,
        request: InstallDriverRequest,
        material: MaterialIdentity,
    ) -> str:
        """Keep release material alive for the claim-bound install driver only."""

        if not isinstance(engine, CtxEngine):
            raise TypeError("release install material requires an exact CTX engine")
        status = engine.install_execution_status(request.action)
        if not status.claimed or status.execution_binding_digest != request.binding.binding_digest:
            raise _fail("release install material requires an exact durable claim")

        with self._lock:
            if self._closed or self._close_requested:
                raise _fail("release-pinned query catalog is closed")
            self._in_flight += 1
            materials = self._materials
        try:
            install_body = _read_resource(
                RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE,
                maximum_bytes=MAX_RELEASE_SKILL_MATERIAL_BYTES,
            )
            return materials.load(request, material, install_body)
        finally:
            close_catalog: ReviewedQueryCatalog | None = None
            close_materials: ReleasePinnedSkillMaterialSource | None = None
            with self._lock:
                self._in_flight -= 1
                if self._in_flight == 0 and self._close_requested and not self._closed:
                    self._closed = True
                    close_catalog = self._catalog
                    close_materials = self._materials
            if close_materials is not None:
                close_materials.close()
            if close_catalog is not None:
                close_catalog.close()

    def close(self) -> None:
        close_catalog: ReviewedQueryCatalog | None = None
        close_materials: ReleasePinnedSkillMaterialSource | None = None
        with self._lock:
            if self._closed:
                return
            self._close_requested = True
            if self._in_flight == 0:
                self._closed = True
                close_catalog = self._catalog
                close_materials = self._materials
        if close_materials is not None:
            close_materials.close()
        if close_catalog is not None:
            close_catalog.close()


def open_release_pinned_query_catalog() -> ReleasePinnedQueryCatalog:
    """Open the exact CTX release catalog without caller-controlled trust inputs."""

    root_body = _read_resource(
        RELEASE_QUERY_CATALOG_ROOT_RESOURCE,
        maximum_bytes=MAX_RELEASE_ROOT_BYTES,
    )
    root = _closed(_decode_release_root(root_body), _ROOT_FIELDS, "release root")
    if root["schema"] != RELEASE_QUERY_CATALOG_ROOT_SCHEMA:
        raise _fail("release root schema is unsupported")
    if root["package_version"] != __version__:
        raise _fail("release root package version does not match this CTX build")
    if (
        type(root["release_sequence"]) is not int
        or root["release_sequence"] != RELEASE_QUERY_CATALOG_SEQUENCE
    ):
        raise _fail("release root sequence is not code-approved")
    if root["mode"] != RELEASE_QUERY_CATALOG_MODE:
        raise _fail("release root mode is not code-approved")
    authority = _closed(root["authority"], _AUTHORITY_FIELDS, "release root authority")
    bindings = _closed(root["bindings"], _BINDING_FIELDS, "release root bindings")
    if (
        bindings["candidate_projection_version"] != ELIGIBLE_ENTRY_PROJECTION
        or bindings["compiler_schema"] != RELEASE_ELIGIBLE_CATALOG_COMPILER_SCHEMA
    ):
        raise _fail("release root compiler or projection is unsupported")
    asset_bindings = _asset_bindings(root)
    bodies: dict[str, bytes] = {}
    for role, (name, expected_sha256) in asset_bindings.items():
        if role == "install_materials":
            continue
        maximum = (
            MAX_ELIGIBLE_CATALOG_BYTES
            if role == "catalog"
            else (
                MAX_RELEASE_SKILL_MATERIAL_BYTES
                if role == "load_materials"
                else MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES
            )
        )
        body = _read_resource(name, maximum_bytes=maximum)
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise _fail("release catalog asset does not match its pinned root")
        bodies[role] = body
    try:
        layer = load_eligible_catalog_layer_bytes(
            bodies["catalog"],
            asset_bindings["catalog"][1],
        )
        profiles = load_reviewed_benefit_profiles_bytes(
            bodies["profiles"],
            asset_bindings["profiles"][1],
        )
        policy = load_reviewed_net_benefit_policy_bytes(
            bodies["policy"],
            asset_bindings["policy"][1],
        )
    except (TypeError, ValueError):
        raise _fail("release catalog assets failed authenticated decoding") from None
    expected_authority = (
        authority["authority_id"],
        authority["authority_kind"],
        authority["authority_digest"],
        authority["catalog_layer_kind"],
        root["release_sequence"],
    )
    if (
        layer.authority_id,
        layer.authority_kind,
        layer.authority_digest,
        layer.catalog_layer_kind,
        layer.sequence,
    ) != expected_authority or (
        profiles.authority_id,
        profiles.authority_kind,
        profiles.authority_digest,
        profiles.catalog_layer_kind,
        profiles.sequence,
    ) != expected_authority:
        raise _fail("release catalog assets do not match the code-pinned authority")
    if (
        layer.catalog_namespace_digest != bindings["catalog_namespace_digest"]
        or profiles.catalog_namespace_digest != bindings["catalog_namespace_digest"]
    ):
        raise _fail("release catalog namespace does not match the pinned root")
    if (
        layer.material_snapshot_digest != asset_bindings["load_materials"][1]
        or profiles.material_snapshot_digest != asset_bindings["load_materials"][1]
        or layer.installation_snapshot_digest != asset_bindings["install_materials"][1]
        or profiles.installation_snapshot_digest != asset_bindings["install_materials"][1]
    ):
        raise _fail("release material does not match the reviewed catalog authority")
    expected_entries = (
        ("skill:ctx-python-state-protocols", "install"),
        ("skill:ctx-python-state-protocols", "load"),
        ("skill:ctx-python-testing", "load"),
    )
    if (
        tuple((entry.capability_id, entry.actionability) for entry in layer.entries)
        != expected_entries
        or tuple((profile.capability_id, profile.actionability) for profile in profiles.profiles)
        != expected_entries
    ):
        raise _fail("reviewed release must contain its exact positive capabilities")
    try:
        catalog = open_reviewed_query_catalog(
            layers=(layer,),
            profiles=ReviewedBenefitAuthorities.create((profiles,)),
            policy=policy,
        )
    except (TypeError, ValueError):
        raise _fail("release catalog authority join failed") from None
    try:
        materials = open_release_pinned_skill_material(
            load_body=bodies["load_materials"],
            load_asset_sha256=asset_bindings["load_materials"][1],
            install_asset_sha256=asset_bindings["install_materials"][1],
        )
    except (TypeError, ValueError):
        catalog.close()
        raise _fail("release material authority failed authenticated decoding") from None
    return ReleasePinnedQueryCatalog._create(
        factory_token=_FACTORY_TOKEN,
        catalog=catalog,
        materials=materials,
        mode=RELEASE_QUERY_CATALOG_MODE,
        release_sequence=RELEASE_QUERY_CATALOG_SEQUENCE,
    )


__all__ = [
    "MAX_RELEASE_ROOT_BYTES",
    "RELEASE_ELIGIBLE_CATALOG_COMPILER_SCHEMA",
    "RELEASE_QUERY_CATALOG_MODE",
    "RELEASE_QUERY_CATALOG_ROOT_RESOURCE",
    "RELEASE_QUERY_CATALOG_ROOT_SCHEMA",
    "RELEASE_QUERY_CATALOG_ROOT_SHA256",
    "RELEASE_QUERY_CATALOG_SEQUENCE",
    "ReleaseCatalogError",
    "ReleasePinnedQueryCatalog",
    "open_release_pinned_query_catalog",
]
