"""Authenticated packaged-content adapter for engine-issued exposure actions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from ctx.engine.content import (
    ExposureAuthorizer,
    MAX_PREPARED_CONTENT_BYTES,
    MAX_PREPARED_CONTENT_TOKENS,
    MaterialDescriptor,
    MaterialIdentity,
    PreparedCapabilityContent,
)
from ctx.engine.planner import CapabilitySelection
from ctx.engine.protocol import HostAction
from ctx.utils._secret_scan import redact_secret_text


_CAPABILITY_RE = re.compile(r"\A(skill|agent|mcp-server|harness):[a-z0-9][a-z0-9._@-]{0,127}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_MANIFEST_FIELDS = frozenset({"capability_id", "path", "sha256", "size_bytes"})
_MAX_MANIFEST_RECORDS = 512


class CatalogContentUnavailable(RuntimeError):
    """Authenticated catalog material cannot satisfy an exposure action."""


def _unavailable() -> CatalogContentUnavailable:
    return CatalogContentUnavailable("catalog capability content is unavailable")


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _unavailable()
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _unavailable()
    return path.as_posix()


def _manifest_records(value: object) -> tuple[dict[str, object], ...]:
    if (
        isinstance(value, (str, bytes, bytearray))
        or not isinstance(value, Sequence)
        or not 1 <= len(value) <= _MAX_MANIFEST_RECORDS
    ):
        raise _unavailable()
    records: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _MANIFEST_FIELDS:
            raise _unavailable()
        capability_id = raw["capability_id"]
        digest = raw["sha256"]
        size = raw["size_bytes"]
        if (
            not isinstance(capability_id, str)
            or _CAPABILITY_RE.fullmatch(capability_id) is None
            or not isinstance(digest, str)
            or _DIGEST_RE.fullmatch(digest) is None
            or type(size) is not int
            or not 1 <= size <= MAX_PREPARED_CONTENT_BYTES
        ):
            raise _unavailable()
        path = _safe_path(raw["path"])
        if path in seen_paths:
            raise _unavailable()
        seen_paths.add(path)
        records.append(
            {
                "capability_id": capability_id,
                "path": path,
                "sha256": digest,
                "size_bytes": size,
            }
        )
    return tuple(sorted(records, key=lambda row: (str(row["capability_id"]), str(row["path"]))))


class AuthenticatedCatalogContentSource:
    """Prepare one exact packaged skill only after an engine exposure action."""

    def __init__(self, root: Path, manifest: object) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be a Path")
        self._root = root
        self._records = _manifest_records(manifest)
        self.material_snapshot_digest = _canonical_digest(
            {"records": self._records, "schema": "ctx.material-manifest-v1"}
        )

    def _records_for(self, capability_id: str) -> tuple[dict[str, object], ...]:
        return tuple(record for record in self._records if record["capability_id"] == capability_id)

    def describe(self, capability_id: str, kind: str) -> MaterialDescriptor:
        if (
            not isinstance(capability_id, str)
            or _CAPABILITY_RE.fullmatch(capability_id) is None
            or kind not in {"skill", "agent", "mcp-server", "harness"}
            or not capability_id.startswith(f"{kind}:")
        ):
            raise _unavailable()
        records = self._records_for(capability_id)
        loadable = kind == "skill" and len(records) == 1
        content_sha256 = str(records[0]["sha256"]) if loadable else None
        content_bytes = cast(int, records[0]["size_bytes"]) if loadable else 0
        estimated_tokens = (content_bytes + 3) // 4
        material_identity = (
            MaterialIdentity.create(
                capability_id=capability_id,
                kind=kind,
                content_sha256=content_sha256 or "",
                content_bytes=content_bytes,
            )
            if loadable
            else None
        )
        return MaterialDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            actionability="load" if loadable else "manual",
            content_sha256=content_sha256,
            content_bytes=content_bytes,
            estimated_tokens=estimated_tokens,
            provenance_digest=self.material_snapshot_digest,
            material_identity_digest=(
                None if material_identity is None else material_identity.identity_digest
            ),
        )

    def _read_exact(self, record: Mapping[str, object]) -> bytes:
        descriptors: list[int] = []
        try:
            if os.open not in os.supports_dir_fd or not getattr(os, "O_NOFOLLOW", 0):
                raise _unavailable()
            root_stat = self._root.lstat()
            if (
                stat.S_ISLNK(root_stat.st_mode)
                or not stat.S_ISDIR(root_stat.st_mode)
                or root_stat.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise _unavailable()
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | os.O_NOFOLLOW
            )
            root_descriptor = os.open(self._root, directory_flags)
            descriptors.append(root_descriptor)
            opened_root = os.fstat(root_descriptor)
            if (
                not stat.S_ISDIR(opened_root.st_mode)
                or opened_root.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                or (opened_root.st_dev, opened_root.st_ino) != (root_stat.st_dev, root_stat.st_ino)
            ):
                raise _unavailable()

            parts = PurePosixPath(str(record["path"])).parts
            parent_descriptor = root_descriptor
            file_descriptor = -1
            for index, part in enumerate(parts):
                is_file = index == len(parts) - 1
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | os.O_NOFOLLOW
                if not is_file:
                    flags |= getattr(os, "O_DIRECTORY", 0)
                opened_descriptor = os.open(part, flags, dir_fd=parent_descriptor)
                descriptors.append(opened_descriptor)
                opened = os.fstat(opened_descriptor)
                if opened.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                    raise _unavailable()
                if is_file:
                    if not stat.S_ISREG(opened.st_mode):
                        raise _unavailable()
                    file_descriptor = opened_descriptor
                else:
                    if not stat.S_ISDIR(opened.st_mode):
                        raise _unavailable()
                    parent_descriptor = opened_descriptor

            if file_descriptor < 0:
                raise _unavailable()
            opened = os.fstat(file_descriptor)
            expected_size = cast(int, record["size_bytes"])
            if opened.st_size != expected_size:
                raise _unavailable()
            content = bytearray()
            while chunk := os.read(
                file_descriptor,
                min(1024, expected_size + 1 - len(content)),
            ):
                content.extend(chunk)
                if len(content) > expected_size:
                    raise _unavailable()
            if len(content) != expected_size:
                raise _unavailable()
        except CatalogContentUnavailable:
            raise
        except (NotImplementedError, OSError, TypeError, ValueError):
            raise _unavailable() from None
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        value = bytes(content)
        if hashlib.sha256(value).hexdigest() != record["sha256"]:
            raise _unavailable()
        return value

    def prepare(
        self,
        action: HostAction,
        selection: CapabilitySelection,
        *,
        expected_catalog_snapshot_digest: str,
        authority: ExposureAuthorizer | None = None,
    ) -> PreparedCapabilityContent:
        if not isinstance(action, HostAction) or not isinstance(selection, CapabilitySelection):
            raise TypeError("prepare requires an engine action and committed selection")
        if authority is None or not callable(getattr(authority, "authorize_exposure", None)):
            raise TypeError("prepare requires a journal-backed exposure authority")
        authority.authorize_exposure(
            action,
            selection,
            expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
        )
        try:
            expires_at = datetime.fromisoformat(str(action.expires_at).replace("Z", "+00:00"))
        except ValueError:
            raise _unavailable() from None
        if (
            action.kind != "PrepareExposure"
            or action.entity_id != selection.capability_id
            or action.source_digest != selection.source_digest
            or action.catalog_snapshot_id != expected_catalog_snapshot_digest
            or selection.kind != "skill"
            or selection.actionability != "load"
            or action.lease_id is None
            or expires_at <= datetime.now(UTC)
        ):
            raise _unavailable()
        descriptor = self.describe(selection.capability_id, selection.kind)
        if descriptor.actionability != "load":
            raise _unavailable()
        records = self._records_for(selection.capability_id)
        if len(records) != 1:
            raise _unavailable()
        raw = self._read_exact(records[0])
        try:
            content = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _unavailable() from None
        if not content or "\x00" in content:
            raise _unavailable()
        if redact_secret_text(content) != content:
            raise _unavailable()
        estimated_tokens = (len(raw) + 3) // 4
        if estimated_tokens > MAX_PREPARED_CONTENT_TOKENS:
            raise _unavailable()
        return PreparedCapabilityContent(
            capability_id=selection.capability_id,
            source_digest=selection.source_digest,
            catalog_snapshot_digest=expected_catalog_snapshot_digest,
            action_id=action.action_id,
            lease_id=action.lease_id,
            content=content,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            content_bytes=len(raw),
            estimated_tokens=estimated_tokens,
        )


__all__ = ["AuthenticatedCatalogContentSource", "CatalogContentUnavailable"]
