"""Canonical restart-safe local layout for managed release-skill state."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ctx.runtime._query_attempt_posix import validate_query_state_root_parent
from ctx.runtime.release_skill_dispatcher import ReleaseSkillInstallRequest
from ctx.runtime.workspace_identity import (
    WorkspaceIdentity,
    WorkspaceIdentityError,
    capture_workspace_identity,
)
from ctx.utils._fs_utils import ensure_secure_directory


_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_FACTORY_TOKEN = object()
_MANAGED_DIRECTORY = "managed-capabilities-v1"
_WORKSPACE_MANAGED_DIRECTORY = "managed-capabilities-v2"
_WORKSPACE_MANAGEMENT_HOST_CONTEXT_ID = "ctx-workspace-management"
_CONSENT_BROKER_FILENAME = "install-consent-v1.sqlite3"


class ReleaseSkillRuntimeLayoutError(RuntimeError):
    """The canonical managed-capability layout is unsafe or no longer current."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class ReleaseSkillRuntimeLayout:
    """Path-private derivation of one workspace/host/session lifecycle stream."""

    host_context_id: str
    host_identity_digest: str
    session_id: str
    workspace_identity_digest: str
    state_root: Path = field(repr=False)
    managed_root: Path = field(repr=False)
    session_root: Path = field(repr=False)
    journal_path: Path = field(repr=False)
    benefit_audit_path: Path = field(repr=False)
    consent_broker_path: Path | None = field(repr=False)
    policy_store_root: Path = field(repr=False)
    skill_store_root: Path = field(repr=False)
    _workspace: Path = field(repr=False, compare=True)
    _workspace_identity: WorkspaceIdentity = field(repr=False, compare=True)
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _FACTORY_TOKEN:
            raise TypeError("release skill layouts are issued only by the layout factory")
        if _TOKEN_RE.fullmatch(self.host_context_id) is None:
            raise ReleaseSkillRuntimeLayoutError("host context identity is invalid")
        for digest in (self.host_identity_digest, self.workspace_identity_digest):
            if not isinstance(digest, str) or len(digest) != 64:
                raise ReleaseSkillRuntimeLayoutError("layout digest is invalid")
        if self.workspace_identity_digest != self._workspace_identity.digest:
            raise ReleaseSkillRuntimeLayoutError("workspace identity is inconsistent")

    def assert_current(self) -> None:
        try:
            self._workspace_identity.assert_current()
            directories = (
                self.state_root,
                self.managed_root,
                self.session_root,
                self.policy_store_root,
                self.skill_store_root,
            )
            for directory in directories:
                _require_private_directory(directory)
            if self.consent_broker_path is not None:
                _require_consent_broker_path(
                    self.consent_broker_path,
                    workspace_root=self.session_root,
                    excluded_roots=(
                        self._workspace,
                        self.policy_store_root,
                        self.skill_store_root,
                    ),
                )
            _require_distinct_paths(
                (
                    *directories,
                    self.journal_path,
                    self.benefit_audit_path,
                    *((self.consent_broker_path,) if self.consent_broker_path is not None else ()),
                    self._workspace,
                )
            )
        except (OSError, ValueError, WorkspaceIdentityError):
            raise ReleaseSkillRuntimeLayoutError(
                "release skill runtime layout changed or became unsafe"
            ) from None

    def install_request(
        self,
        *,
        task: str,
        language: str,
        occurred_at: str,
    ) -> ReleaseSkillInstallRequest:
        self.assert_current()
        return ReleaseSkillInstallRequest(
            host_context_id=self.host_context_id,
            host_identity_digest=self.host_identity_digest,
            task=task,
            language=language,
            session_id=self.session_id,
            workspace=self._workspace,
            journal_path=self.journal_path,
            benefit_audit_path=self.benefit_audit_path,
            policy_store_root=self.policy_store_root,
            skill_store_root=self.skill_store_root,
            occurred_at=occurred_at,
        )

    def __repr__(self) -> str:
        return (
            "ReleaseSkillRuntimeLayout("
            f"workspace_identity_digest={self.workspace_identity_digest!r}, "
            f"host_context_id={self.host_context_id!r}, session_id={self.session_id!r})"
        )

    def __copy__(self) -> ReleaseSkillRuntimeLayout:
        raise TypeError("ReleaseSkillRuntimeLayout cannot be copied")

    def __deepcopy__(self, _memo: object) -> ReleaseSkillRuntimeLayout:
        raise TypeError("ReleaseSkillRuntimeLayout cannot be copied")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("ReleaseSkillRuntimeLayout cannot be serialized")


def open_release_skill_runtime_layout(
    *,
    state_root: Path,
    host_context_id: str,
    native_session_id: str,
    workspace: Path,
) -> ReleaseSkillRuntimeLayout:
    """Open or create the canonical owner-private state for one host session."""

    if os.name == "nt":
        raise ReleaseSkillRuntimeLayoutError(
            "managed release skill layout is not available on Windows"
        )
    if not isinstance(state_root, Path):
        raise TypeError("state_root must be a Path")
    if not isinstance(host_context_id, str) or _TOKEN_RE.fullmatch(host_context_id) is None:
        raise ValueError("host_context_id must be a canonical token")
    if not isinstance(native_session_id, str) or not native_session_id:
        raise ValueError("native_session_id must be a non-empty string")
    if len(native_session_id.encode("utf-8")) > 4_096:
        raise ValueError("native_session_id exceeds its byte bound")
    try:
        identity = capture_workspace_identity(workspace)
        root = Path(os.path.abspath(os.fspath(state_root)))
        validate_query_state_root_parent(root)
        managed_root = root / _MANAGED_DIRECTORY
        policy_root = managed_root / "install-policy-v1"
        skill_root = managed_root / "skill-cas-v1"
        session_id = f"session-{_digest(native_session_id)}"
        host_digest = _digest(f"ctx-managed-host-v1\0{host_context_id}")
        session_root = (
            managed_root
            / "workspaces"
            / identity.digest
            / f"host-{_digest(host_context_id)}"
            / session_id
        )
        for directory in (root, managed_root, policy_root, skill_root, session_root):
            ensure_secure_directory(directory)
            _require_private_directory(directory)
    except Exception:
        raise ReleaseSkillRuntimeLayoutError(
            "managed release skill state root is unsafe or unavailable"
        ) from None
    return ReleaseSkillRuntimeLayout(
        host_context_id=host_context_id,
        host_identity_digest=host_digest,
        session_id=session_id,
        workspace_identity_digest=identity.digest,
        state_root=root,
        managed_root=managed_root,
        session_root=session_root,
        journal_path=session_root / "engine.sqlite3",
        benefit_audit_path=session_root / "benefit.sqlite3",
        consent_broker_path=None,
        policy_store_root=policy_root,
        skill_store_root=skill_root,
        _workspace=workspace,
        _workspace_identity=identity,
        _token=_FACTORY_TOKEN,
    )


def open_workspace_release_skill_runtime_layout(
    *,
    state_root: Path,
    policy_store_root: Path,
    workspace: Path,
) -> ReleaseSkillRuntimeLayout:
    """Open CTX-owned management state shared by one stable workspace."""

    if os.name == "nt":
        raise ReleaseSkillRuntimeLayoutError(
            "workspace release skill layout is not available on Windows"
        )
    if not isinstance(state_root, Path):
        raise TypeError("state_root must be a Path")
    if not isinstance(policy_store_root, Path):
        raise TypeError("policy_store_root must be a Path")
    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a Path")
    for path, field_name in (
        (state_root, "state_root"),
        (policy_store_root, "policy_store_root"),
        (workspace, "workspace"),
    ):
        if not path.is_absolute():
            raise ReleaseSkillRuntimeLayoutError(f"{field_name} must be absolute")
    try:
        root = _normalized_absolute_path(state_root)
        policy_root = _normalized_absolute_path(policy_store_root)
        workspace_path = _normalized_absolute_path(workspace)
        _require_non_overlapping_roots((root, policy_root, workspace_path))
        identity = capture_workspace_identity(workspace_path)
        validate_query_state_root_parent(root)
        managed_root = root / _WORKSPACE_MANAGED_DIRECTORY
        workspace_root = managed_root / "workspaces" / identity.digest
        skill_root = workspace_root / "skill-cas-v1"
        journal_path = workspace_root / "engine.sqlite3"
        benefit_audit_path = workspace_root / "benefit.sqlite3"
        consent_broker_path = workspace_root / _CONSENT_BROKER_FILENAME
        layout_paths = (
            root,
            managed_root,
            workspace_root,
            skill_root,
            policy_root,
            journal_path,
            benefit_audit_path,
            consent_broker_path,
            workspace_path,
        )
        _require_distinct_paths(layout_paths)
        for directory in (root, managed_root, workspace_root, skill_root, policy_root):
            ensure_secure_directory(directory)
            _require_private_directory(directory)
        _require_consent_broker_path(
            consent_broker_path,
            workspace_root=workspace_root,
            excluded_roots=(workspace_path, policy_root, skill_root),
        )
        _require_distinct_paths(layout_paths)
        session_id = "workspace-session-" + _digest("ctx-managed-session-v2\0" + identity.digest)
    except ReleaseSkillRuntimeLayoutError:
        raise
    except Exception:
        raise ReleaseSkillRuntimeLayoutError(
            "workspace release skill state roots are unsafe or unavailable"
        ) from None
    return ReleaseSkillRuntimeLayout(
        host_context_id=_WORKSPACE_MANAGEMENT_HOST_CONTEXT_ID,
        host_identity_digest=_digest(
            "ctx-managed-host-v2\0" + _WORKSPACE_MANAGEMENT_HOST_CONTEXT_ID
        ),
        session_id=session_id,
        workspace_identity_digest=identity.digest,
        state_root=root,
        managed_root=managed_root,
        session_root=workspace_root,
        journal_path=journal_path,
        benefit_audit_path=benefit_audit_path,
        consent_broker_path=consent_broker_path,
        policy_store_root=policy_root,
        skill_store_root=skill_root,
        _workspace=workspace_path,
        _workspace_identity=identity,
        _token=_FACTORY_TOKEN,
    )


def _require_private_directory(path: Path) -> None:
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseSkillRuntimeLayoutError("managed state path is not a directory")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ReleaseSkillRuntimeLayoutError("managed state directory is not owner-private")


def _normalized_absolute_path(path: Path) -> Path:
    return Path(os.path.normcase(os.path.abspath(os.fspath(path))))


def _require_consent_broker_path(
    path: Path,
    *,
    workspace_root: Path,
    excluded_roots: tuple[Path, ...],
) -> None:
    expected = workspace_root / _CONSENT_BROKER_FILENAME
    if _normalized_absolute_path(path) != _normalized_absolute_path(expected):
        raise ReleaseSkillRuntimeLayoutError(
            "consent broker path changed or escaped its managed workspace root"
        )
    physical_path = Path(os.path.normcase(os.path.realpath(os.fspath(path))))
    for root in excluded_roots:
        physical_root = Path(os.path.normcase(os.path.realpath(os.fspath(root))))
        if (
            physical_path == physical_root
            or physical_root in physical_path.parents
            or physical_path in physical_root.parents
        ):
            raise ReleaseSkillRuntimeLayoutError(
                "consent broker path overlaps a workspace, policy, or CAS root"
            )
    if not os.path.lexists(path):
        return
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ReleaseSkillRuntimeLayoutError(
            "consent broker path is not a private single-link regular file"
        )
    if metadata.st_uid != os.geteuid():
        raise ReleaseSkillRuntimeLayoutError(
            "consent broker database is not owned by the current user"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ReleaseSkillRuntimeLayoutError("consent broker database is not owner-private")


def _require_non_overlapping_roots(paths: tuple[Path, ...]) -> None:
    _require_distinct_paths(paths)
    physical_paths = tuple(
        Path(os.path.normcase(os.path.realpath(os.fspath(path)))) for path in paths
    )
    if len(set(physical_paths)) != len(physical_paths):
        raise ReleaseSkillRuntimeLayoutError("workspace management roots must be distinct")
    for index, first in enumerate(physical_paths):
        for second in physical_paths[index + 1 :]:
            if first in second.parents or second in first.parents:
                raise ReleaseSkillRuntimeLayoutError(
                    "caller-controlled workspace management roots must not overlap"
                )


def _require_distinct_paths(paths: tuple[Path, ...]) -> None:
    normalized: set[str] = set()
    physical: set[tuple[int, int]] = set()
    for path in paths:
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if key in normalized:
            raise ReleaseSkillRuntimeLayoutError("workspace management roots must be distinct")
        normalized.add(key)
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in physical:
            raise ReleaseSkillRuntimeLayoutError("workspace management roots must be distinct")
        physical.add(identity)


__all__ = [
    "ReleaseSkillRuntimeLayout",
    "ReleaseSkillRuntimeLayoutError",
    "open_release_skill_runtime_layout",
    "open_workspace_release_skill_runtime_layout",
]
