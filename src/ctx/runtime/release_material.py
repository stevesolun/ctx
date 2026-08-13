"""Release-pinned raw skill material prepared by one exact bundle action."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from typing import Final

from ctx.engine.content import PreparedCapabilityContent
from ctx.engine.content import MaterialDescriptor, MaterialIdentity
from ctx.engine.engine import _PromptContextMaterialPermit
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, LoadPlanningAuthority
from ctx.engine.protocol import HostAction
from ctx.runtime.install_execution import InstallDriverRequest


RELEASE_LOAD_SKILL_MATERIAL_RESOURCE: Final = "release-load-skill-material-v1.json"
RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE: Final = "release-install-skill-material-v1.json"
MAX_RELEASE_SKILL_MATERIAL_BYTES: Final = 64 * 1024
RELEASE_LOAD_SKILL_ID: Final = "skill:ctx-python-testing"
RELEASE_INSTALL_SKILL_ID: Final = "skill:ctx-python-state-protocols"
_RELEASE_LOAD_SKILL_PATH: Final = "converted/ctx-python-testing/SKILL.md"
_RELEASE_INSTALL_SKILL_PATH: Final = "converted/ctx-python-state-protocols/SKILL.md"
_PACKAGED_LOAD_SKILL_PATHS: Final = {
    RELEASE_LOAD_SKILL_ID: _RELEASE_LOAD_SKILL_PATH,
}
_EXTERNAL_LOAD_SKILL_IDS: Final = frozenset({RELEASE_INSTALL_SKILL_ID})
_REVIEWED_LOAD_SKILL_IDS: Final = frozenset(
    {*_PACKAGED_LOAD_SKILL_PATHS, *_EXTERNAL_LOAD_SKILL_IDS}
)
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_SOURCE_TOKEN = object()


class ReleaseMaterialError(ValueError):
    """The exact release material cannot satisfy an authorized prompt bundle."""


def _fail(message: str) -> ReleaseMaterialError:
    return ReleaseMaterialError(message)


def _content_from_body(body: bytes, capability_id: str, expected_path: str) -> str:
    """Decode one code-pinned material asset containing one exact target body."""

    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise _fail("release skill material is invalid") from None
    if not isinstance(decoded, Mapping):
        raise _fail("release skill material is invalid")
    entries = decoded.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 512:
        raise _fail("release skill material entries are invalid")
    target: Mapping[str, object] | None = None
    seen: set[str] = set()
    for value in entries:
        if not isinstance(value, Mapping) or not isinstance(value.get("id"), str):
            raise _fail("release skill material entry is invalid")
        entry_id = value["id"]
        assert isinstance(entry_id, str)
        if entry_id in seen:
            raise _fail("release skill material contains a duplicate capability")
        seen.add(entry_id)
        if entry_id == capability_id:
            target = value
    if target is None or set(target) != {"files", "id", "no_api_keys", "type"}:
        raise _fail("reviewed release skill material is missing")
    if target["type"] != "skill" or target["no_api_keys"] is not True:
        raise _fail("reviewed release skill material changed its safety facts")
    files = target["files"]
    if not isinstance(files, list) or len(files) != 1:
        raise _fail("reviewed release skill material must contain one file")
    record = files[0]
    if not isinstance(record, Mapping) or set(record) != {"content", "path"}:
        raise _fail("reviewed release skill file is invalid")
    if record["path"] != expected_path or not isinstance(record["content"], str):
        raise _fail("reviewed release skill file identity changed")
    content = record["content"]
    assert isinstance(content, str)
    if not content or "\x00" in content:
        raise _fail("reviewed release skill content is invalid")
    return content


class ReleasePinnedSkillMaterialSource:
    """One code-owned material asset bound to the release-root hash."""

    __slots__ = (
        "_claimed_actions",
        "_closed",
        "_install_asset_sha256",
        "_load_asset_sha256",
        "_load_body",
        "_lock",
    )

    _load_asset_sha256: str
    _load_body: bytes
    _install_asset_sha256: str
    _claimed_actions: set[str]
    _closed: bool
    _lock: threading.Lock

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("release skill material must be opened by the release factory")

    @classmethod
    def _create(
        cls,
        *,
        load_body: bytes,
        load_asset_sha256: str,
        install_asset_sha256: str,
        factory_token: object,
    ) -> ReleasePinnedSkillMaterialSource:
        if factory_token is not _SOURCE_TOKEN:
            raise TypeError("release skill material must be opened by the release factory")
        if (
            not isinstance(load_body, bytes)
            or not 1 <= len(load_body) <= MAX_RELEASE_SKILL_MATERIAL_BYTES
            or not isinstance(load_asset_sha256, str)
            or _SHA256_RE.fullmatch(load_asset_sha256) is None
            or hashlib.sha256(load_body).hexdigest() != load_asset_sha256
            or not isinstance(install_asset_sha256, str)
            or _SHA256_RE.fullmatch(install_asset_sha256) is None
        ):
            raise _fail("release skill material does not match its pinned asset")
        instance = object.__new__(cls)
        instance._load_body = bytes(load_body)
        instance._load_asset_sha256 = load_asset_sha256
        instance._install_asset_sha256 = install_asset_sha256
        instance._claimed_actions = set()
        instance._closed = False
        instance._lock = threading.Lock()
        return instance

    def _content(self, capability_id: str, expected_path: str) -> str:
        with self._lock:
            if self._closed:
                raise _fail("release skill material is closed")
            if capability_id in _PACKAGED_LOAD_SKILL_PATHS:
                body = self._load_body
            else:
                raise _fail("release skill material is not code-approved")
        return _content_from_body(body, capability_id, expected_path)

    def _validated_descriptor(
        self,
        selection: CapabilityPlanSelectionV3,
    ) -> MaterialDescriptor:
        presentation = selection.presentation
        planning_authority = selection.authority
        if (
            presentation.capability_id not in _REVIEWED_LOAD_SKILL_IDS
            or presentation.kind != "skill"
            or presentation.actionability != "load"
            or not isinstance(planning_authority, LoadPlanningAuthority)
        ):
            raise _fail("release prompt selection is not the reviewed skill")
        descriptor = planning_authority.material.catalog_material_descriptor
        if (
            descriptor is None
            or descriptor.provenance_digest != self._load_asset_sha256
            or descriptor.capability_id != presentation.capability_id
            or descriptor.kind != presentation.kind
        ):
            raise _fail("release prompt selection lost its exact material descriptor")
        return descriptor

    def _validated_content(self, selection: CapabilityPlanSelectionV3) -> str:
        descriptor = self._validated_descriptor(selection)
        presentation = selection.presentation
        expected_path = _PACKAGED_LOAD_SKILL_PATHS.get(presentation.capability_id)
        if expected_path is None:
            raise _fail("release prompt material requires its installed CAS route")
        content = self._content(
            presentation.capability_id,
            expected_path,
        )
        encoded = content.encode("utf-8")
        if (
            hashlib.sha256(encoded).hexdigest() != descriptor.content_sha256
            or len(encoded) != descriptor.content_bytes
        ):
            raise _fail("release prompt content does not match its descriptor")
        return content

    def load(
        self,
        request: InstallDriverRequest,
        material: MaterialIdentity,
        install_body: bytes,
    ) -> str:
        """Resolve the one reviewed absent skill for the claimed skill-CAS driver."""

        if not isinstance(request, InstallDriverRequest):
            raise TypeError("release install material requires an exact driver request")
        if not isinstance(material, MaterialIdentity):
            raise TypeError("release install material requires an exact material identity")
        if not isinstance(install_body, bytes):
            raise TypeError("release install material requires exact package bytes")
        action = request.action
        descriptor = request.descriptor
        if (
            material.capability_id != RELEASE_INSTALL_SKILL_ID
            or material.kind != "skill"
            or descriptor.capability_id != material.capability_id
            or descriptor.kind != material.kind
            or not descriptor.matches_result_material(material)
            or action.kind != "InstallCapability"
            or action.entity_id != material.capability_id
            or action.payload.get("capability_kind") != material.kind
            or action.payload.get("result_material") != material.to_dict()
            or action.payload.get("install_plan_descriptor") != descriptor.to_dict()
            or descriptor.provenance_digest != self._install_asset_sha256
        ):
            raise _fail("release install request is not the reviewed absent skill")
        if (
            not 1 <= len(install_body) <= MAX_RELEASE_SKILL_MATERIAL_BYTES
            or hashlib.sha256(install_body).hexdigest() != self._install_asset_sha256
        ):
            raise _fail("release install material does not match its pinned asset")
        content = _content_from_body(
            install_body,
            RELEASE_INSTALL_SKILL_ID,
            _RELEASE_INSTALL_SKILL_PATH,
        )
        encoded = content.encode("utf-8")
        if (
            hashlib.sha256(encoded).hexdigest() != material.content_sha256
            or len(encoded) != material.content_bytes
        ):
            raise _fail("release install content does not match its material identity")
        return content

    def prepare_prompt_context(
        self,
        action: HostAction,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        *,
        expected_catalog_snapshot_digest: str,
        authority: _PromptContextMaterialPermit,
        external_material_source: object | None = None,
    ) -> tuple[PreparedCapabilityContent, ...]:
        """Prepare exact raw bytes only for the pending bundle action."""

        if (
            not isinstance(action, HostAction)
            or not isinstance(selections, tuple)
            or not selections
            or not all(isinstance(item, CapabilityPlanSelectionV3) for item in selections)
        ):
            raise TypeError("release prompt context requires an exact typed bundle")
        if type(authority) is not _PromptContextMaterialPermit:
            raise TypeError("release prompt context requires engine-issued material authority")
        if external_material_source is not None:
            from ctx.runtime.activated_skill_exposure import ActivatedSkillMaterialPermit

            if type(external_material_source) is not ActivatedSkillMaterialPermit:
                raise TypeError(
                    "external material source must be an exact activated-skill permit or None"
                )
        action_digest = action.content_digest
        with self._lock:
            if self._closed:
                raise _fail("release skill material is closed")
            if action_digest in self._claimed_actions:
                raise _fail("release prompt context action was already claimed")
            self._claimed_actions.add(action_digest)
        try:
            external_ids = frozenset(
                selection.presentation.capability_id
                for selection in selections
                if selection.presentation.capability_id in _EXTERNAL_LOAD_SKILL_IDS
            )
            routes = authority._consume_and_issue_routes(
                action=action,
                selections=selections,
                expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
                external_capability_ids=external_ids,
            )
            rows = action.payload.get("capabilities")
            if not isinstance(rows, tuple) or tuple(
                row.get("capability_id") for row in rows if isinstance(row, Mapping)
            ) != tuple(item.presentation.capability_id for item in selections):
                raise _fail("release prompt context action changed its capability order")
            prepared: list[PreparedCapabilityContent] = []
            for selection in selections:
                presentation = selection.presentation
                planning_authority = selection.authority
                if not isinstance(planning_authority, LoadPlanningAuthority):
                    raise _fail("release prompt context lost its load authority")
                descriptor = planning_authority.material.catalog_material_descriptor
                if descriptor is None:
                    raise _fail("release prompt context lost its material descriptor")
                self._validated_descriptor(selection)
                route = routes.get(presentation.capability_id)
                if route is None:
                    content = self._validated_content(selection)
                    prepared_content = PreparedCapabilityContent(
                        capability_id=presentation.capability_id,
                        source_digest=presentation.source_digest,
                        catalog_snapshot_digest=expected_catalog_snapshot_digest,
                        action_id=action.action_id,
                        lease_id=action.lease_id or "",
                        content=content,
                        content_sha256=descriptor.content_sha256 or "",
                        content_bytes=descriptor.content_bytes,
                        estimated_tokens=descriptor.estimated_tokens,
                    )
                else:
                    if external_material_source is None:
                        raise _fail("release prompt material requires its installed CAS route")
                    prepared_content = external_material_source.prepare_prompt_context_once(
                        route_authority=route,
                        action=action,
                        selection=selection,
                        selections=selections,
                        expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
                    )
                    self._validate_external_content(
                        prepared_content,
                        selection=selection,
                        action=action,
                        expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
                    )
                prepared.append(prepared_content)
            return tuple(prepared)
        except BaseException:
            with self._lock:
                self._claimed_actions.discard(action_digest)
            raise

    @staticmethod
    def _validate_external_content(
        content: PreparedCapabilityContent,
        *,
        selection: CapabilityPlanSelectionV3,
        action: HostAction,
        expected_catalog_snapshot_digest: str,
    ) -> None:
        if not isinstance(content, PreparedCapabilityContent):
            raise _fail("external prompt material did not return prepared content")
        planning_authority = selection.authority
        if not isinstance(planning_authority, LoadPlanningAuthority):
            raise _fail("external prompt material lost its load authority")
        descriptor = planning_authority.material.catalog_material_descriptor
        encoded = content.content.encode("utf-8")
        if (
            descriptor is None
            or content.capability_id != selection.presentation.capability_id
            or content.source_digest != selection.presentation.source_digest
            or content.catalog_snapshot_digest != expected_catalog_snapshot_digest
            or content.action_id != action.action_id
            or content.lease_id != (action.lease_id or "")
            or content.content_sha256 != descriptor.content_sha256
            or content.content_bytes != descriptor.content_bytes
            or content.estimated_tokens != descriptor.estimated_tokens
            or hashlib.sha256(encoded).hexdigest() != content.content_sha256
            or len(encoded) != content.content_bytes
        ):
            raise _fail("external prompt material changed its exact reviewed identity")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._load_body = b""
            self._claimed_actions.clear()


def open_release_pinned_skill_material(
    *,
    load_body: bytes,
    load_asset_sha256: str,
    install_asset_sha256: str,
) -> ReleasePinnedSkillMaterialSource:
    """Open material only after the production root authenticated its bytes."""

    return ReleasePinnedSkillMaterialSource._create(
        load_body=load_body,
        load_asset_sha256=load_asset_sha256,
        install_asset_sha256=install_asset_sha256,
        factory_token=_SOURCE_TOKEN,
    )


__all__ = [
    "MAX_RELEASE_SKILL_MATERIAL_BYTES",
    "RELEASE_INSTALL_SKILL_ID",
    "RELEASE_LOAD_SKILL_ID",
    "RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE",
    "RELEASE_LOAD_SKILL_MATERIAL_RESOURCE",
    "ReleaseMaterialError",
    "ReleasePinnedSkillMaterialSource",
    "open_release_pinned_skill_material",
]
