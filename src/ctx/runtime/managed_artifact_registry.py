"""Private content-addressed inputs for restart-safe managed-query planning.

The registry persists one already-authenticated, read-only graph-store artifact
and only the bounded current-work surrogate plus exact snapshot digests needed
to reproduce its planning environment. It exposes no artifact path or raw
SQLite connection. The observation boundary rejects non-canonical structure
and obvious credential-like tokens; trusted normalization remains responsible
for semantic sanitization because opaque safe tokens cannot prove their own
meaning. The manifest does not yet bind normalizer or privacy-policy
provenance, so ingestion must remain behind the trusted service boundary; this
registry alone does not authenticate who produced the surrogate.

This implementation deliberately requires POSIX descriptor-relative filesystem
primitives.  Native Windows needs an equivalent HANDLE/DACL/reparse-point
implementation before this trust boundary can be enabled there.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NoReturn, SupportsIndex, cast

from ctx.core.resolve.engine_candidates import (
    DEFAULT_CANDIDATE_LIMIT,
    IndexedGraphCandidateSource,
)
from ctx.engine.planner import WorkObservation
from ctx.engine.replay import (
    MAX_SURROGATE_BYTES,
    ObservationReference,
    ReplayError,
    StructuredSurrogate,
)
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import (
    ensure_secure_directory,
    secure_directory,
    supports_secure_directory_fds,
)

if TYPE_CHECKING:
    from ctx.engine.content import CapabilityMaterialPort
    from ctx.engine.installation import CapabilityInstallPlanPort
    from ctx.engine.state import EngineState


MANAGED_ARTIFACT_MANIFEST_SCHEMA: Final = "ctx.managed-query-artifact-manifest"
MANAGED_ARTIFACT_REGISTRY_VERSION: Final = 1
MAX_GRAPH_ARTIFACT_BYTES: Final = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES: Final = 32 * 1024
NO_MATERIAL_SNAPSHOT_DIGEST: Final = hashlib.sha256(
    b"ctx.managed-artifact.no-material-snapshot-v1"
).hexdigest()
NO_INSTALLATION_SNAPSHOT_DIGEST: Final = hashlib.sha256(
    b"ctx.managed-artifact.no-installation-snapshot-v1"
).hexdigest()

_OBSERVATION_SCHEMA = "ctx.observation.current-work"
_OBSERVATION_SCHEMA_VERSION = 1
_OBSERVATION_PROVIDER_ID = "ctx-managed-artifact-observation-v1"
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o400
_STAGE_MODE = 0o600
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_VERSION_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_ARTIFACT_SUFFIX = ".sqlite3"
_MANIFEST_SUFFIX = ".json"
_ARTIFACT_DIRECTORY = "artifacts"
_MANIFEST_DIRECTORY = "manifests"
_LOCK_TARGET = "registry-publication"
_PUBLICATION_STAGE_RE = re.compile(
    r"\A\.(artifact|manifest)-([0-9a-f]{64})-([0-9a-f]{32})\.stage\Z"
)
_CREDENTIAL_PREFIXES = ("ghp-", "github-pat-", "sk-", "xoxb-", "xoxp-")
_SENSITIVE_TOKEN_PARTS = frozenset(
    {
        "api-key",
        "apikey",
        "auth-token",
        "bearer",
        "credential",
        "credentials",
        "passwd",
        "password",
        "private-key",
        "secret",
    }
)
_MAX_DIRECTORY_ENTRIES = 8_192
_MANIFEST_FIELDS = frozenset(
    {
        "benefit_facts_snapshot_digest",
        "benefit_policy_snapshot_digest",
        "catalog_namespace_digest",
        "catalog_retrieval_digest",
        "graph_artifact_digest",
        "installation_snapshot_digest",
        "material_snapshot_digest",
        "observation_surrogate",
        "observation_surrogate_digest",
        "planning_environment_digest",
        "planning_schema_version",
        "registry_version",
        "schema",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "active_capability_ids",
        "baseline_capability_ids",
        "languages",
        "rejected_capability_ids",
        "requested_limit",
        "signals",
    }
)
_REQUIRED_GRAPH_TABLE_COLUMNS = {
    "edges": frozenset({"source", "target", "weight", "attrs_json"}),
    "metadata": frozenset({"key", "value"}),
    "nodes": frozenset({"id", "type", "label", "title", "tags_json", "attrs_json", "search_text"}),
}
_REQUIRED_GRAPH_INDEXES = frozenset(
    {"idx_edges_source", "idx_edges_target", "idx_nodes_search_text", "idx_nodes_type"}
)
_FACTORY_TOKEN = object()
_HANDLE_FACTORY_TOKEN = object()


class ManagedArtifactRegistryError(RuntimeError):
    """Managed-query input persistence failed its closed integrity contract."""


def _fail(message: str) -> ManagedArtifactRegistryError:
    return ManagedArtifactRegistryError(message)


def _require_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise _fail(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _require_version(value: object) -> str:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise _fail("planning_schema_version must be a canonical bounded token")
    return value


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("managed artifact manifest contains a duplicate field")
        result[key] = value
    return result


def _file_snapshot(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _directory_fd(directory: object) -> int:
    descriptor = getattr(directory, "_directory_fd", None)
    if not isinstance(descriptor, int):
        raise _fail("descriptor-relative managed artifact storage is unavailable")
    return descriptor


def _require_supported_platform() -> None:
    dir_fd_support: set[object] = getattr(os, "supports_dir_fd", set())
    follow_support: set[object] = getattr(os, "supports_follow_symlinks", set())
    fd_support: set[object] = getattr(os, "supports_fd", set())
    if (
        os.name == "nt"
        or not callable(getattr(os, "geteuid", None))
        or not supports_secure_directory_fds()
        or not getattr(os, "O_NOFOLLOW", 0)
        or os.link not in dir_fd_support
        or os.link not in follow_support
        or os.listdir not in fd_support
    ):
        raise _fail("secure managed artifact storage is unavailable on this platform")


def _require_private_directory(metadata: os.stat_result, label: str) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE
    ):
        raise _fail(f"{label} must be a real owner-private directory")


def _require_private_file(
    metadata: os.stat_result,
    label: str,
    *,
    expected_size: int | None = None,
    allowed_links: frozenset[int] = frozenset({1}),
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
        or metadata.st_nlink not in allowed_links
        or (expected_size is not None and metadata.st_size != expected_size)
    ):
        raise _fail(f"{label} must be an exact owner-private file")


def _validate_protected_ancestry(root: Path) -> None:
    current = root
    current_metadata = os.stat(current, follow_symlinks=False)
    while current != Path(current.anchor):
        parent = current.parent
        parent_metadata = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
            raise _fail("managed artifact registry ancestor is not a real directory")
        if parent_metadata.st_uid not in {0, os.geteuid()}:
            raise _fail("managed artifact registry ancestor has an untrusted owner")
        writable = stat.S_IMODE(parent_metadata.st_mode) & 0o022
        sticky = bool(parent_metadata.st_mode & stat.S_ISVTX)
        if writable and not (sticky and current_metadata.st_uid in {0, os.geteuid()}):
            raise _fail("managed artifact registry ancestor permits unsafe replacement")
        current = parent
        current_metadata = parent_metadata


def _open_bound_directory(path: Path, expected_identity: tuple[int, int]) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        _require_private_directory(opened, "managed artifact directory")
        if _identity(opened) != expected_identity or not os.path.samestat(opened, current):
            raise _fail("managed artifact directory identity changed")
        return descriptor
    except ManagedArtifactRegistryError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except OSError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise _fail("managed artifact directory is unavailable") from None


def _validate_root_bindings(
    root: Path,
    artifacts: Path,
    manifests: Path,
    identities: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
) -> None:
    for path, expected in zip((root, artifacts, manifests), identities, strict=True):
        descriptor = _open_bound_directory(path, expected)
        os.close(descriptor)


def _ensure_durable_secure_directory_tree(path: Path) -> None:
    """Create a secure tree and persist every directory entry newly traversed."""

    current = Path(path.anchor)
    missing: list[Path] = []
    for part in path.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            missing.append(current)
    ensure_secure_directory(path)
    for directory in missing:
        with secure_directory(directory.parent, create=False) as opened_parent:
            os.fsync(_directory_fd(opened_parent))


def _validate_observation_surrogate(value: object) -> StructuredSurrogate:
    if type(value) is not StructuredSurrogate:
        raise TypeError("observation_surrogate must be an exact StructuredSurrogate")
    if (
        value.schema_id != _OBSERVATION_SCHEMA
        or value.schema_version != _OBSERVATION_SCHEMA_VERSION
        or set(value.value) != _OBSERVATION_FIELDS
    ):
        raise _fail("observation surrogate must use the exact current-work schema")

    def tokens(field_name: str) -> tuple[str, ...]:
        raw = value.value[field_name]
        if not isinstance(raw, tuple) or not all(isinstance(item, str) for item in raw):
            raise _fail(f"observation surrogate {field_name} must be a token array")
        return cast(tuple[str, ...], raw)

    def privacy_safe_tokens(field_name: str) -> tuple[str, ...]:
        result = tokens(field_name)
        for token in result:
            normalized = token.replace("_", "-").replace(".", "-").replace(":", "-")
            parts = normalized.split("-")
            pairs = {f"{left}-{right}" for left, right in zip(parts, parts[1:])}
            if (
                token.startswith(_CREDENTIAL_PREFIXES)
                or _SENSITIVE_TOKEN_PARTS.intersection(parts)
                or _SENSITIVE_TOKEN_PARTS.intersection(pairs)
                or (field_name in {"signals", "languages"} and len(token) >= 32)
            ):
                raise _fail("observation surrogate contains credential-like material")
        return result

    try:
        observation = WorkObservation(
            signals=privacy_safe_tokens("signals"),
            languages=privacy_safe_tokens("languages"),
            baseline_capability_ids=privacy_safe_tokens("baseline_capability_ids"),
            active_capability_ids=privacy_safe_tokens("active_capability_ids"),
            rejected_capability_ids=privacy_safe_tokens("rejected_capability_ids"),
            requested_limit=value.value["requested_limit"],  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail("observation surrogate contains invalid current-work facts") from exc
    expected_value = {
        "active_capability_ids": list(observation.active_capability_ids),
        "baseline_capability_ids": list(observation.baseline_capability_ids),
        "languages": list(observation.languages),
        "rejected_capability_ids": list(observation.rejected_capability_ids),
        "requested_limit": observation.requested_limit,
        "signals": list(observation.signals),
    }
    canonical = StructuredSurrogate.create(
        schema_id=_OBSERVATION_SCHEMA,
        schema_version=_OBSERVATION_SCHEMA_VERSION,
        value=expected_value,
    )
    if canonical != value or canonical.to_json().encode("utf-8") != value.to_json().encode("utf-8"):
        raise _fail("observation surrogate must use exact canonical current-work facts")
    if len(canonical.to_json().encode("utf-8")) > MAX_SURROGATE_BYTES:
        raise _fail("observation surrogate exceeds its persistent byte bound")
    return canonical


def _manifest_mapping(
    *,
    graph_artifact_digest: str,
    planning_environment_digest: str,
    catalog_namespace_digest: str,
    catalog_retrieval_digest: str,
    benefit_facts_snapshot_digest: str,
    benefit_policy_snapshot_digest: str,
    material_snapshot_digest: str,
    installation_snapshot_digest: str,
    observation_surrogate: StructuredSurrogate,
    planning_schema_version: str,
) -> dict[str, object]:
    return {
        "benefit_facts_snapshot_digest": benefit_facts_snapshot_digest,
        "benefit_policy_snapshot_digest": benefit_policy_snapshot_digest,
        "catalog_namespace_digest": catalog_namespace_digest,
        "catalog_retrieval_digest": catalog_retrieval_digest,
        "graph_artifact_digest": graph_artifact_digest,
        "installation_snapshot_digest": installation_snapshot_digest,
        "material_snapshot_digest": material_snapshot_digest,
        "observation_surrogate": observation_surrogate.to_dict(),
        "observation_surrogate_digest": observation_surrogate.value_digest,
        "planning_environment_digest": planning_environment_digest,
        "planning_schema_version": planning_schema_version,
        "registry_version": MANAGED_ARTIFACT_REGISTRY_VERSION,
        "schema": MANAGED_ARTIFACT_MANIFEST_SCHEMA,
    }


def _validate_manifest_mapping(value: object) -> tuple[dict[str, object], StructuredSurrogate]:
    if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
        raise _fail("managed artifact manifest has missing or unknown fields")
    if (
        value["schema"] != MANAGED_ARTIFACT_MANIFEST_SCHEMA
        or value["registry_version"] != MANAGED_ARTIFACT_REGISTRY_VERSION
    ):
        raise _fail("managed artifact manifest schema or version is unsupported")
    for field_name in (
        "benefit_facts_snapshot_digest",
        "benefit_policy_snapshot_digest",
        "catalog_namespace_digest",
        "catalog_retrieval_digest",
        "graph_artifact_digest",
        "installation_snapshot_digest",
        "material_snapshot_digest",
        "observation_surrogate_digest",
        "planning_environment_digest",
    ):
        _require_digest(value[field_name], field_name)
    _require_version(value["planning_schema_version"])
    raw_surrogate = value["observation_surrogate"]
    if not isinstance(raw_surrogate, Mapping):
        raise _fail("managed artifact observation surrogate must be an object")
    try:
        surrogate = _validate_observation_surrogate(StructuredSurrogate.from_dict(raw_surrogate))
    except (ReplayError, TypeError, ValueError) as exc:
        raise _fail("managed artifact observation surrogate is invalid") from exc
    if surrogate.value_digest != value["observation_surrogate_digest"]:
        raise _fail("observation surrogate bytes do not match their bound digest")
    expected = _manifest_mapping(
        graph_artifact_digest=value["graph_artifact_digest"],  # type: ignore[arg-type]
        planning_environment_digest=value["planning_environment_digest"],  # type: ignore[arg-type]
        catalog_namespace_digest=value["catalog_namespace_digest"],  # type: ignore[arg-type]
        catalog_retrieval_digest=value["catalog_retrieval_digest"],  # type: ignore[arg-type]
        benefit_facts_snapshot_digest=value["benefit_facts_snapshot_digest"],  # type: ignore[arg-type]
        benefit_policy_snapshot_digest=value["benefit_policy_snapshot_digest"],  # type: ignore[arg-type]
        material_snapshot_digest=value["material_snapshot_digest"],  # type: ignore[arg-type]
        installation_snapshot_digest=value["installation_snapshot_digest"],  # type: ignore[arg-type]
        observation_surrogate=surrogate,
        planning_schema_version=value["planning_schema_version"],  # type: ignore[arg-type]
    )
    if value != expected:
        raise _fail("managed artifact manifest is not exact")
    return expected, surrogate


def _source_sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))


def _source_sidecars_exist(path: Path) -> bool:
    return any(os.path.lexists(sidecar) for sidecar in _source_sidecars(path))


def _open_authenticated_source(path: Path, expected_digest: str) -> tuple[int, os.stat_result]:
    if not isinstance(path, Path):
        raise TypeError("graph_store_path must be a Path")
    expected_digest = _require_digest(expected_digest, "expected_graph_artifact_digest")
    descriptor = -1
    try:
        before = path.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size > MAX_GRAPH_ARTIFACT_BYTES
            or _source_sidecars_exist(path)
        ):
            raise _fail("graph-store source is not one stable read-only regular artifact")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _file_snapshot(opened) != _file_snapshot(before):
            raise _fail("graph-store source changed while it was opened")
        return descriptor, opened
    except ManagedArtifactRegistryError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise _fail("graph-store source is unavailable") from None


def _write_all(descriptor: int, body: bytes) -> None:
    view = memoryview(body)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise OSError("managed artifact write made no progress")
        offset += written


def _descriptor_sqlite_uri(descriptor: int) -> str:
    """Return a process-local path that duplicates one already-open descriptor."""

    opened = os.fstat(descriptor)
    for parent in (Path("/proc/self/fd"), Path("/dev/fd")):
        alias = parent / str(descriptor)
        duplicate = -1
        try:
            duplicate = os.open(
                alias,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0),
            )
            duplicate_metadata = os.fstat(duplicate)
        except OSError:
            continue
        finally:
            if duplicate >= 0:
                os.close(duplicate)
        # macOS devfs reports a synthetic st_dev for stat('/dev/fd/N'),
        # while fstat() on a descriptor duplicated through that alias retains
        # the authenticated file identity. Compare the duplicate descriptors.
        if os.path.samestat(opened, duplicate_metadata):
            return f"{alias.as_uri()}?mode=ro&immutable=1"
    raise _fail("descriptor-bound SQLite validation is unavailable")


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _cleanup_open_stage(directory_fd: int, name: str, descriptor: int) -> None:
    """Remove only the exact still-pinned unpublished stage descriptor."""

    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _require_private_file(opened, "managed artifact stage")
        if _file_snapshot(opened) != _file_snapshot(current):
            raise _fail("managed artifact stage changed before failure cleanup")
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except ManagedArtifactRegistryError:
        raise
    except OSError:
        raise _fail("managed artifact stage failure cleanup was unsafe") from None


def _copy_source_to_stage(
    source_fd: int,
    source_snapshot: os.stat_result,
    directory_fd: int,
    stage_name: str,
    expected_digest: str,
) -> tuple[int, int]:
    stage_fd = -1
    try:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        stage_fd = os.open(stage_name, flags, _STAGE_MODE, dir_fd=directory_fd)
        os.fchmod(stage_fd, _FILE_MODE)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(source_fd, 1024 * 1024):
            total += len(chunk)
            if total > MAX_GRAPH_ARTIFACT_BYTES:
                raise _fail("graph-store artifact exceeds its persistent byte bound")
            _write_all(stage_fd, chunk)
            digest.update(chunk)
        source_after = os.fstat(source_fd)
        if _file_snapshot(source_after) != _file_snapshot(source_snapshot):
            raise _fail("graph-store source changed during authenticated copying")
        if total != source_snapshot.st_size or digest.hexdigest() != expected_digest:
            raise _fail("graph-store source does not match its exact SHA-256")
        os.fsync(stage_fd)
        stage_metadata = os.fstat(stage_fd)
        _require_private_file(stage_metadata, "graph-store stage", expected_size=total)
        published_descriptor = stage_fd
        stage_fd = -1
        return published_descriptor, total
    except BaseException as error:
        if stage_fd >= 0:
            try:
                _cleanup_open_stage(directory_fd, stage_name, stage_fd)
            except BaseException as cleanup_error:
                error.add_note(
                    "managed artifact stage cleanup also failed with "
                    f"{type(cleanup_error).__name__}"
                )
        raise
    finally:
        if stage_fd >= 0:
            os.close(stage_fd)


def _publish_stage(
    directory_fd: int,
    *,
    stage_name: str,
    final_name: str,
    expected_digest: str,
    expected_size: int,
) -> None:
    published = False
    stage: os.stat_result | None
    try:
        os.link(
            stage_name,
            final_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        published = True
        os.fsync(directory_fd)
    except FileExistsError:
        _read_exact_child(
            directory_fd,
            final_name,
            expected_digest=expected_digest,
            maximum_bytes=max(expected_size, 1),
            expected_size=expected_size,
            return_body=False,
        )
    finally:
        try:
            stage = os.stat(stage_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            stage = None
        if stage is not None:
            allowed_links = 2 if published else 1
            if (
                stat.S_ISREG(stage.st_mode)
                and stage.st_uid == os.geteuid()
                and stat.S_IMODE(stage.st_mode) == _FILE_MODE
                and stage.st_nlink == allowed_links
            ):
                os.unlink(stage_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            else:
                raise _fail("managed artifact stage changed before cleanup")
    _read_exact_child(
        directory_fd,
        final_name,
        expected_digest=expected_digest,
        maximum_bytes=max(expected_size, 1),
        expected_size=expected_size,
        return_body=False,
    )


def _read_exact_child(
    directory_fd: int,
    name: str,
    *,
    expected_digest: str,
    maximum_bytes: int,
    expected_size: int | None = None,
    allowed_links: frozenset[int] = frozenset({1}),
    return_body: bool = True,
) -> bytes:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _require_private_file(
            before,
            "managed artifact child",
            expected_size=expected_size,
            allowed_links=allowed_links,
        )
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        if _file_snapshot(opened) != _file_snapshot(before):
            raise _fail("managed artifact child changed while it was opened")
        remaining = maximum_bytes + 1
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        total = 0
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if return_body:
                chunks.append(chunk)
            remaining -= len(chunk)
        after_fd = os.fstat(descriptor)
        after_path = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            total > maximum_bytes
            or _file_snapshot(opened) != _file_snapshot(after_fd)
            or _file_snapshot(after_fd) != _file_snapshot(after_path)
            or digest.hexdigest() != expected_digest
        ):
            raise _fail("managed artifact child is corrupt or was replaced")
        return b"".join(chunks) if return_body else b""
    except ManagedArtifactRegistryError:
        raise
    except OSError:
        raise _fail("managed artifact child is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reconcile_publication_stages(directory_fd: int, *, kind: str) -> None:
    """Finish exact link publications and reject ambiguous hard-link residue."""

    if kind not in {"artifact", "manifest"}:
        raise ValueError("publication stage kind is unsupported")
    try:
        names = os.listdir(directory_fd)
    except OSError:
        raise _fail("managed artifact directory cannot be reconciled") from None
    if len(names) > _MAX_DIRECTORY_ENTRIES:
        raise _fail("managed artifact directory exceeds its bounded entry count")
    stage_names = tuple(sorted(name for name in names if name.endswith(".stage")))
    suffix = _ARTIFACT_SUFFIX if kind == "artifact" else _MANIFEST_SUFFIX
    maximum = MAX_GRAPH_ARTIFACT_BYTES if kind == "artifact" else MAX_MANIFEST_BYTES
    for stage_name in stage_names:
        match = _PUBLICATION_STAGE_RE.fullmatch(stage_name)
        if match is None or match.group(1) != kind:
            raise _fail("managed artifact directory contains an unknown publication stage")
        expected_digest = match.group(2)
        final_name = f"{expected_digest}{suffix}"
        try:
            stage = os.stat(stage_name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            raise _fail("managed artifact publication stage is unavailable") from None
        if stage.st_nlink == 1:
            if (
                not stat.S_ISREG(stage.st_mode)
                or stat.S_ISLNK(stage.st_mode)
                or stage.st_uid != os.geteuid()
                or stat.S_IMODE(stage.st_mode) not in {_FILE_MODE, _STAGE_MODE}
            ):
                raise _fail("unpublished managed artifact stage is unsafe")
            try:
                os.unlink(stage_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                raise _fail("unpublished managed artifact stage could not be reconciled") from None
            continue
        _require_private_file(
            stage,
            "managed artifact publication stage",
            allowed_links=frozenset({2}),
        )
        try:
            final = os.stat(final_name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            raise _fail("linked publication stage has no exact final address") from None
        _require_private_file(
            final,
            "managed artifact publication target",
            allowed_links=frozenset({2}),
        )
        if not os.path.samestat(stage, final) or _file_snapshot(stage) != _file_snapshot(final):
            raise _fail("linked publication stage does not match its exact final address")
        _read_exact_child(
            directory_fd,
            final_name,
            expected_digest=expected_digest,
            maximum_bytes=maximum,
            allowed_links=frozenset({2}),
            return_body=False,
        )
        try:
            current_stage = os.stat(stage_name, dir_fd=directory_fd, follow_symlinks=False)
            current_final = os.stat(final_name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                _file_snapshot(current_stage) != _file_snapshot(stage)
                or _file_snapshot(current_final) != _file_snapshot(final)
                or not os.path.samestat(current_stage, current_final)
            ):
                raise _fail("linked publication changed during reconciliation")
            os.unlink(stage_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except ManagedArtifactRegistryError:
            raise
        except OSError:
            raise _fail("linked publication stage could not be reconciled") from None
        _read_exact_child(
            directory_fd,
            final_name,
            expected_digest=expected_digest,
            maximum_bytes=maximum,
            return_body=False,
        )


def _pin_child_snapshot(
    directory_fd: int,
    name: str,
    known: dict[str, tuple[int, int, int, int, int, int, int]],
) -> None:
    """Pin or revalidate one content address for this registry lifetime."""

    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        raise _fail("managed artifact child identity is unavailable") from None
    _require_private_file(metadata, "managed artifact child")
    current = _file_snapshot(metadata)
    previous = known.get(name)
    if previous is not None and previous != current:
        raise _fail("managed artifact child was replaced")
    known[name] = current


def _discard_unpublished_stage(directory_fd: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise _fail("managed artifact stage cannot be inspected for cleanup") from None
    _require_private_file(metadata, "managed artifact stage")
    try:
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError:
        raise _fail("managed artifact stage cannot be cleaned safely") from None


def _write_manifest_stage(directory_fd: int, name: str, body: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            _STAGE_MODE,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, _FILE_MODE)
        _write_all(descriptor, body)
        os.fsync(descriptor)
        _require_private_file(os.fstat(descriptor), "manifest stage", expected_size=len(body))
    except BaseException as error:
        if descriptor >= 0:
            try:
                _cleanup_open_stage(directory_fd, name, descriptor)
            except BaseException as cleanup_error:
                error.add_note(
                    "managed artifact manifest cleanup also failed with "
                    f"{type(cleanup_error).__name__}"
                )
        if isinstance(error, ManagedArtifactRegistryError):
            raise
        raise _fail("managed artifact manifest could not be staged") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode_manifest(
    body: bytes, expected_manifest_digest: str
) -> tuple[dict[str, object], StructuredSurrogate]:
    if len(body) > MAX_MANIFEST_BYTES:
        raise _fail("managed artifact manifest exceeds its byte bound")
    if hashlib.sha256(body).hexdigest() != expected_manifest_digest:
        raise _fail("managed artifact manifest digest is inconsistent")
    try:
        decoded = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                _fail("managed artifact manifest contains a non-finite number")
            ),
        )
    except ManagedArtifactRegistryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _fail("managed artifact manifest is not strict canonical JSON") from exc
    mapping, surrogate = _validate_manifest_mapping(decoded)
    if _canonical_bytes(mapping) != body:
        raise _fail("managed artifact manifest must use exact canonical JSON")
    return mapping, surrogate


def _validate_graph_store_descriptor(descriptor: int, expected_digest: str) -> None:
    """Validate schema and digest through one already-authenticated file authority."""

    expected_digest = _require_digest(expected_digest, "expected_graph_artifact_digest")
    before = os.fstat(descriptor)
    _require_private_file(before, "managed graph artifact")
    if before.st_size > MAX_GRAPH_ARTIFACT_BYTES:
        raise _fail("managed graph artifact exceeds its persistent byte bound")
    if _hash_descriptor(descriptor) != expected_digest:
        raise _fail("managed graph artifact is corrupt")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _descriptor_sqlite_uri(descriptor),
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]).lower() != "ok":
            raise _fail("managed graph artifact failed SQLite integrity checking")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not _REQUIRED_GRAPH_TABLE_COLUMNS.keys() <= tables:
            raise _fail("managed graph artifact is not a complete graph store")
        for table_name, required_columns in _REQUIRED_GRAPH_TABLE_COLUMNS.items():
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            if not required_columns <= columns:
                raise _fail("managed graph artifact has an incomplete graph schema")
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        if not _REQUIRED_GRAPH_INDEXES <= indexes:
            raise _fail("managed graph artifact is missing a required retrieval index")
        metadata = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT key, value FROM metadata").fetchall()
        }
        if metadata.get("schema_version") != "1":
            raise _fail("managed graph artifact schema is unsupported")
        for metadata_key, table_name in (("node_count", "nodes"), ("edge_count", "edges")):
            expected_count = int(metadata[metadata_key])
            actual_count = int(
                connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            )
            if expected_count != actual_count:
                raise _fail("managed graph artifact count metadata is inconsistent")
    except ManagedArtifactRegistryError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise _fail("managed graph artifact is not a valid read-only graph store") from None
    finally:
        if connection is not None:
            connection.close()
    after = os.fstat(descriptor)
    if (
        _file_snapshot(before) != _file_snapshot(after)
        or _hash_descriptor(descriptor) != expected_digest
    ):
        raise _fail("managed graph artifact changed during validation")


def _validate_graph_store_child(
    directory_fd: int,
    name: str,
    expected_digest: str,
) -> None:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _require_private_file(before, "managed graph artifact")
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0),
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        if _file_snapshot(opened) != _file_snapshot(before):
            raise _fail("managed graph artifact changed while it was opened")
        _validate_graph_store_descriptor(descriptor, expected_digest)
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_snapshot(opened) != _file_snapshot(after):
            raise _fail("managed graph artifact changed during validation")
    except ManagedArtifactRegistryError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise _fail("managed graph artifact is not a valid read-only graph store") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class ManagedArtifactHandle:
    """Path-free process-bound manifest and observation-normalizer handle."""

    __slots__ = (
        "_pid",
        "_registry_token",
        "_surrogate",
        "benefit_facts_snapshot_digest",
        "benefit_policy_snapshot_digest",
        "catalog_namespace_digest",
        "catalog_retrieval_digest",
        "graph_artifact_digest",
        "installation_snapshot_digest",
        "manifest_digest",
        "material_snapshot_digest",
        "observation_reference",
        "observation_surrogate_digest",
        "planning_environment_digest",
        "planning_schema_version",
    )

    manifest_digest: str
    graph_artifact_digest: str
    planning_environment_digest: str
    catalog_namespace_digest: str
    catalog_retrieval_digest: str
    benefit_facts_snapshot_digest: str
    benefit_policy_snapshot_digest: str
    material_snapshot_digest: str
    installation_snapshot_digest: str
    observation_surrogate_digest: str
    planning_schema_version: str
    observation_reference: ObservationReference
    _pid: int
    _registry_token: object
    _surrogate: StructuredSurrogate

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed artifact handles are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        registry_token: object,
        manifest_digest: str,
        mapping: Mapping[str, object],
        surrogate: StructuredSurrogate,
    ) -> ManagedArtifactHandle:
        if factory_token is not _HANDLE_FACTORY_TOKEN:
            raise TypeError("managed artifact handles are factory-issued only")
        instance = object.__new__(cls)
        for field_name in (
            "graph_artifact_digest",
            "planning_environment_digest",
            "catalog_namespace_digest",
            "catalog_retrieval_digest",
            "benefit_facts_snapshot_digest",
            "benefit_policy_snapshot_digest",
            "material_snapshot_digest",
            "installation_snapshot_digest",
            "observation_surrogate_digest",
            "planning_schema_version",
        ):
            object.__setattr__(instance, field_name, mapping[field_name])
        object.__setattr__(instance, "manifest_digest", manifest_digest)
        object.__setattr__(instance, "_surrogate", surrogate)
        object.__setattr__(instance, "_registry_token", registry_token)
        object.__setattr__(instance, "_pid", os.getpid())
        object.__setattr__(
            instance,
            "observation_reference",
            ObservationReference(
                provider_id=_OBSERVATION_PROVIDER_ID,
                opaque_id=f"manifest-{manifest_digest}",
                content_digest=surrogate.value_digest,
            ),
        )
        return instance

    def __call__(
        self,
        reference: ObservationReference,
        state: EngineState | None,
    ) -> StructuredSurrogate:
        """Return the exact persisted surrogate for the exact bound reference."""

        if os.getpid() != self._pid:
            raise _fail("managed artifact handle is unavailable outside its issuing process")
        if type(reference) is not ObservationReference or reference != self.observation_reference:
            raise _fail("managed artifact observation reference is unavailable")
        if state is not None:
            from ctx.engine.state import EngineState

            if type(state) is not EngineState:
                raise TypeError("state must be an exact EngineState or None")
        return self._surrogate

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed artifact handle is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed artifact handle is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed artifact handle cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed artifact handle cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed artifact handle cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed artifact handle cannot be serialized")

    def __repr__(self) -> str:
        return f"ManagedArtifactHandle(manifest_digest={self.manifest_digest!r})"


class ManagedArtifactRegistry:
    """Factory-issued owner of one pinned private content-addressed registry."""

    __slots__ = (
        "_artifact_identity",
        "_artifacts",
        "_known_artifacts",
        "_known_manifests",
        "_manifest_identity",
        "_manifests",
        "_pid",
        "_registry_token",
        "_root",
        "_root_identity",
    )

    _known_artifacts: dict[str, tuple[int, int, int, int, int, int, int]]
    _known_manifests: dict[str, tuple[int, int, int, int, int, int, int]]
    _artifact_identity: tuple[int, int]
    _artifacts: Path
    _manifest_identity: tuple[int, int]
    _manifests: Path
    _pid: int
    _registry_token: object
    _root: Path
    _root_identity: tuple[int, int]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed artifact registries are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        root: Path,
        artifacts: Path,
        manifests: Path,
        identities: tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
    ) -> ManagedArtifactRegistry:
        if factory_token is not _FACTORY_TOKEN:
            raise TypeError("managed artifact registries are factory-issued only")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_root", root)
        object.__setattr__(instance, "_artifacts", artifacts)
        object.__setattr__(instance, "_manifests", manifests)
        object.__setattr__(instance, "_root_identity", identities[0])
        object.__setattr__(instance, "_artifact_identity", identities[1])
        object.__setattr__(instance, "_manifest_identity", identities[2])
        object.__setattr__(instance, "_registry_token", object())
        object.__setattr__(instance, "_known_artifacts", {})
        object.__setattr__(instance, "_known_manifests", {})
        object.__setattr__(instance, "_pid", os.getpid())
        return instance

    def _assert_current(self) -> None:
        if os.getpid() != self._pid:
            raise _fail("managed artifact registry is unavailable outside its issuing process")
        _validate_root_bindings(
            self._root,
            self._artifacts,
            self._manifests,
            (self._root_identity, self._artifact_identity, self._manifest_identity),
        )

    def _reconcile_publications_locked(self) -> None:
        artifact_fd = _open_bound_directory(self._artifacts, self._artifact_identity)
        try:
            _reconcile_publication_stages(artifact_fd, kind="artifact")
        finally:
            os.close(artifact_fd)
        manifest_fd = _open_bound_directory(self._manifests, self._manifest_identity)
        try:
            _reconcile_publication_stages(manifest_fd, kind="manifest")
        finally:
            os.close(manifest_fd)
        self._assert_current()

    def ingest_graph_store(
        self,
        *,
        graph_store_path: Path,
        expected_graph_artifact_digest: str,
        planning_environment_digest: str,
        catalog_namespace_digest: str,
        catalog_retrieval_digest: str,
        benefit_facts_snapshot_digest: str,
        benefit_policy_snapshot_digest: str,
        material_snapshot_digest: str,
        installation_snapshot_digest: str,
        observation_surrogate: StructuredSurrogate,
        planning_schema_version: str,
    ) -> ManagedArtifactHandle:
        """Copy and bind one exact authenticated graph artifact without replacement."""

        graph_digest = _require_digest(
            expected_graph_artifact_digest,
            "expected_graph_artifact_digest",
        )
        bindings = {
            field_name: _require_digest(value, field_name)
            for field_name, value in (
                ("planning_environment_digest", planning_environment_digest),
                ("catalog_namespace_digest", catalog_namespace_digest),
                ("catalog_retrieval_digest", catalog_retrieval_digest),
                ("benefit_facts_snapshot_digest", benefit_facts_snapshot_digest),
                ("benefit_policy_snapshot_digest", benefit_policy_snapshot_digest),
                ("material_snapshot_digest", material_snapshot_digest),
                ("installation_snapshot_digest", installation_snapshot_digest),
            )
        }
        surrogate = _validate_observation_surrogate(observation_surrogate)
        planning_version = _require_version(planning_schema_version)
        mapping = _manifest_mapping(
            graph_artifact_digest=graph_digest,
            observation_surrogate=surrogate,
            planning_schema_version=planning_version,
            **bindings,
        )
        body = _canonical_bytes(mapping)
        if len(body) > MAX_MANIFEST_BYTES:
            raise _fail("managed artifact manifest exceeds its byte bound")
        manifest_digest = hashlib.sha256(body).hexdigest()
        source_fd = -1
        try:
            source_fd, source_snapshot = _open_authenticated_source(
                graph_store_path,
                graph_digest,
            )
            self._assert_current()
            with secure_file_lock(self._root / _LOCK_TARGET):
                self._assert_current()
                self._reconcile_publications_locked()
                artifact_fd = _open_bound_directory(self._artifacts, self._artifact_identity)
                try:
                    artifact_name = f"{graph_digest}{_ARTIFACT_SUFFIX}"
                    stage_name = f".artifact-{graph_digest}-{secrets.token_hex(16)}.stage"
                    stage_fd, artifact_size = _copy_source_to_stage(
                        source_fd,
                        source_snapshot,
                        artifact_fd,
                        stage_name,
                        graph_digest,
                    )
                    try:
                        _validate_graph_store_descriptor(stage_fd, graph_digest)
                        os.close(stage_fd)
                        stage_fd = -1
                        _publish_stage(
                            artifact_fd,
                            stage_name=stage_name,
                            final_name=artifact_name,
                            expected_digest=graph_digest,
                            expected_size=artifact_size,
                        )
                    except BaseException:
                        if stage_fd >= 0:
                            os.close(stage_fd)
                        _discard_unpublished_stage(artifact_fd, stage_name)
                        raise
                    _pin_child_snapshot(
                        artifact_fd,
                        artifact_name,
                        self._known_artifacts,
                    )
                finally:
                    os.close(artifact_fd)
                if _file_snapshot(os.fstat(source_fd)) != _file_snapshot(
                    source_snapshot
                ) or _source_sidecars_exist(graph_store_path):
                    raise _fail("graph-store source changed before publication completed")
                manifest_fd = _open_bound_directory(self._manifests, self._manifest_identity)
                try:
                    manifest_name = f"{manifest_digest}{_MANIFEST_SUFFIX}"
                    try:
                        os.stat(manifest_name, dir_fd=manifest_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        stage_name = f".manifest-{manifest_digest}-{secrets.token_hex(16)}.stage"
                        _write_manifest_stage(manifest_fd, stage_name, body)
                        _publish_stage(
                            manifest_fd,
                            stage_name=stage_name,
                            final_name=manifest_name,
                            expected_digest=manifest_digest,
                            expected_size=len(body),
                        )
                    else:
                        existing = _read_exact_child(
                            manifest_fd,
                            manifest_name,
                            expected_digest=manifest_digest,
                            maximum_bytes=MAX_MANIFEST_BYTES,
                            expected_size=len(body),
                        )
                        if existing != body:
                            raise _fail("managed artifact manifest address is occupied")
                    _pin_child_snapshot(
                        manifest_fd,
                        manifest_name,
                        self._known_manifests,
                    )
                finally:
                    os.close(manifest_fd)
                self._assert_current()
        except ManagedArtifactRegistryError:
            raise
        except (OSError, ValueError):
            raise _fail("managed graph artifact could not be published safely") from None
        finally:
            if source_fd >= 0:
                os.close(source_fd)
        return self.reopen(
            manifest_digest=manifest_digest,
            planning_environment_digest=bindings["planning_environment_digest"],
        )

    def reopen(
        self,
        *,
        manifest_digest: str,
        planning_environment_digest: str,
    ) -> ManagedArtifactHandle:
        """Authenticate exact persisted inputs and return a typed normalizer handle."""

        manifest_digest = _require_digest(manifest_digest, "manifest_digest")
        environment_digest = _require_digest(
            planning_environment_digest,
            "planning_environment_digest",
        )
        self._assert_current()
        with secure_file_lock(self._root / _LOCK_TARGET):
            self._assert_current()
            self._reconcile_publications_locked()
            manifest_fd = _open_bound_directory(self._manifests, self._manifest_identity)
            try:
                body = _read_exact_child(
                    manifest_fd,
                    f"{manifest_digest}{_MANIFEST_SUFFIX}",
                    expected_digest=manifest_digest,
                    maximum_bytes=MAX_MANIFEST_BYTES,
                )
                _pin_child_snapshot(
                    manifest_fd,
                    f"{manifest_digest}{_MANIFEST_SUFFIX}",
                    self._known_manifests,
                )
            finally:
                os.close(manifest_fd)
            mapping, surrogate = _decode_manifest(body, manifest_digest)
            if mapping["planning_environment_digest"] != environment_digest:
                raise _fail("managed artifact planning environment does not match")
            graph_digest = mapping["graph_artifact_digest"]
            assert isinstance(graph_digest, str)
            artifact_fd = _open_bound_directory(self._artifacts, self._artifact_identity)
            try:
                _validate_graph_store_child(
                    artifact_fd,
                    f"{graph_digest}{_ARTIFACT_SUFFIX}",
                    graph_digest,
                )
                _pin_child_snapshot(
                    artifact_fd,
                    f"{graph_digest}{_ARTIFACT_SUFFIX}",
                    self._known_artifacts,
                )
            finally:
                os.close(artifact_fd)
            self._assert_current()
        return ManagedArtifactHandle._create(
            factory_token=_HANDLE_FACTORY_TOKEN,
            registry_token=self._registry_token,
            manifest_digest=manifest_digest,
            mapping=mapping,
            surrogate=surrogate,
        )

    def _open_indexed_source_for_composition(
        self,
        handle: ManagedArtifactHandle,
        *,
        factory_token: object,
        material_port: CapabilityMaterialPort | None = None,
        install_plan_port: CapabilityInstallPlanPort | None = None,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> IndexedGraphCandidateSource:
        """Open one exact authenticated source for the production composition."""

        from ctx.runtime.composition import _MANAGED_SOURCE_FACTORY_TOKEN

        if factory_token is not _MANAGED_SOURCE_FACTORY_TOKEN:
            raise _fail("managed indexed sources require the trusted composition factory")

        if (
            type(handle) is not ManagedArtifactHandle
            or handle._registry_token is not self._registry_token
            or handle._pid != os.getpid()
        ):
            raise _fail("managed artifact handle was not issued by this registry")
        material_digest = (
            NO_MATERIAL_SNAPSHOT_DIGEST
            if material_port is None
            else _require_digest(
                getattr(material_port, "material_snapshot_digest", None),
                "material_port.material_snapshot_digest",
            )
        )
        installation_digest = (
            NO_INSTALLATION_SNAPSHOT_DIGEST
            if install_plan_port is None
            else _require_digest(
                getattr(install_plan_port, "installation_snapshot_digest", None),
                "install_plan_port.installation_snapshot_digest",
            )
        )
        if (
            material_digest != handle.material_snapshot_digest
            or installation_digest != handle.installation_snapshot_digest
        ):
            raise _fail("managed artifact authority snapshots do not match the handle")
        exact = self.reopen(
            manifest_digest=handle.manifest_digest,
            planning_environment_digest=handle.planning_environment_digest,
        )
        if (
            exact.graph_artifact_digest != handle.graph_artifact_digest
            or exact.material_snapshot_digest != material_digest
            or exact.installation_snapshot_digest != installation_digest
        ):
            raise _fail("managed artifact handle changed before source opening")
        source: IndexedGraphCandidateSource | None = None
        try:
            source = IndexedGraphCandidateSource(
                self._artifacts / f"{handle.graph_artifact_digest}{_ARTIFACT_SUFFIX}",
                handle.graph_artifact_digest,
                candidate_limit=candidate_limit,
                install_plan_port=install_plan_port,
                material_port=material_port,
                remove_snapshot_namespace_on_open=True,
            )
            if (
                getattr(source, "_material_snapshot_digest", None)
                != (None if material_port is None else material_digest)
                or getattr(source, "_installation_snapshot_digest", None)
                != (None if install_plan_port is None else installation_digest)
                or (
                    material_port is not None
                    and getattr(material_port, "material_snapshot_digest", None) != material_digest
                )
                or (
                    install_plan_port is not None
                    and getattr(install_plan_port, "installation_snapshot_digest", None)
                    != installation_digest
                )
            ):
                raise _fail("managed artifact authority changed while its source was opening")
            if source.catalog_snapshot_digest != handle.catalog_retrieval_digest:
                raise _fail("managed catalog retrieval digest does not match the handle")
            confirmed = self.reopen(
                manifest_digest=handle.manifest_digest,
                planning_environment_digest=handle.planning_environment_digest,
            )
            if (
                confirmed.graph_artifact_digest != exact.graph_artifact_digest
                or confirmed.material_snapshot_digest != material_digest
                or confirmed.installation_snapshot_digest != installation_digest
            ):
                raise _fail("managed artifact changed while its source was opening")
            return source
        except BaseException as error:
            if source is not None:
                try:
                    source.close()
                except BaseException as cleanup_error:
                    error.add_note(
                        "managed indexed-source cleanup also failed with "
                        f"{type(cleanup_error).__name__}"
                    )
            raise

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed artifact registry is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed artifact registry is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed artifact registry cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed artifact registry cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed artifact registry cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed artifact registry cannot be serialized")

    def __repr__(self) -> str:
        return (
            "ManagedArtifactRegistry("
            f"schema={MANAGED_ARTIFACT_MANIFEST_SCHEMA!r}, "
            f"version={MANAGED_ARTIFACT_REGISTRY_VERSION})"
        )


def open_managed_artifact_registry(*, root: Path) -> ManagedArtifactRegistry:
    """Open or create one POSIX owner-private managed-query artifact registry."""

    _require_supported_platform()
    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    absolute = Path(os.path.abspath(root))
    if absolute == Path(absolute.anchor):
        raise _fail("managed artifact registry cannot use a filesystem root")
    try:
        _ensure_durable_secure_directory_tree(absolute)
        with secure_directory(absolute, create=False) as opened_root:
            canonical_root = opened_root.path
            root_fd = _directory_fd(opened_root)
            root_metadata = os.fstat(root_fd)
            _require_private_directory(root_metadata, "managed artifact registry root")
        artifacts = canonical_root / _ARTIFACT_DIRECTORY
        manifests = canonical_root / _MANIFEST_DIRECTORY
        for directory in (artifacts, manifests):
            _ensure_durable_secure_directory_tree(directory)
        with secure_directory(canonical_root, create=False) as opened_root:
            os.fsync(_directory_fd(opened_root))
        identities: list[tuple[int, int]] = [_identity(root_metadata)]
        for directory, label in (
            (artifacts, "managed artifact content directory"),
            (manifests, "managed artifact manifest directory"),
        ):
            with secure_directory(directory, create=False) as opened:
                metadata = os.fstat(_directory_fd(opened))
                _require_private_directory(metadata, label)
                identities.append(_identity(metadata))
        _validate_protected_ancestry(canonical_root)
        frozen_identities = (identities[0], identities[1], identities[2])
        _validate_root_bindings(
            canonical_root,
            artifacts,
            manifests,
            frozen_identities,
        )
    except ManagedArtifactRegistryError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _fail("managed artifact registry root is unsafe or unavailable") from None
    registry = ManagedArtifactRegistry._create(
        factory_token=_FACTORY_TOKEN,
        root=canonical_root,
        artifacts=artifacts,
        manifests=manifests,
        identities=frozen_identities,
    )
    try:
        with secure_file_lock(canonical_root / _LOCK_TARGET):
            registry._assert_current()
            registry._reconcile_publications_locked()
            root_fd = _open_bound_directory(canonical_root, frozen_identities[0])
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
    except ManagedArtifactRegistryError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _fail("managed artifact registry could not reconcile startup state") from None
    return registry


__all__ = [
    "MANAGED_ARTIFACT_MANIFEST_SCHEMA",
    "MANAGED_ARTIFACT_REGISTRY_VERSION",
    "MAX_GRAPH_ARTIFACT_BYTES",
    "MAX_MANIFEST_BYTES",
    "NO_INSTALLATION_SNAPSHOT_DIGEST",
    "NO_MATERIAL_SNAPSHOT_DIGEST",
    "ManagedArtifactHandle",
    "ManagedArtifactRegistry",
    "ManagedArtifactRegistryError",
    "open_managed_artifact_registry",
]
