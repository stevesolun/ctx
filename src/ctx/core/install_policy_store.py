"""Hardened local persistence for installation-consent policy snapshots."""

from __future__ import annotations

import errno
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctx.engine.installation import InstallConsentPolicy
from ctx.utils._fs_utils import (
    ensure_secure_directory,
    reject_symlink_path,
    safe_atomic_write_text,
)
from ctx.utils._file_lock import secure_file_lock


MAX_POLICY_FILE_BYTES = 4_096
_CURRENT_FILE = "current.json"
_SNAPSHOTS_DIR = "snapshots"


class InstallPolicyStoreError(ValueError):
    """The on-disk policy store violates its integrity contract."""


def default_install_policy_root() -> Path:
    """Return the user-local policy-store root without creating it."""

    return Path(os.path.expanduser("~")) / ".ctx" / "install-policy"


def load_current_install_policy(root: Path | None = None) -> InstallConsentPolicy:
    """Load and authenticate the current policy, or return the safe default."""

    store_root = _store_root(root)
    if _lstat(store_root) is None:
        return InstallConsentPolicy.safe_default()
    reject_symlink_path(store_root)
    with secure_file_lock(_policy_lock_target(store_root)):
        return _load_current_install_policy_unlocked(store_root)


def _load_current_install_policy_unlocked(store_root: Path) -> InstallConsentPolicy:
    snapshots = _validate_existing_store(store_root)
    current_path = store_root / _CURRENT_FILE
    if _lstat(current_path) is None:
        return InstallConsentPolicy.safe_default()
    policy, encoded = _load_policy_file(current_path)
    snapshot_path = snapshots / f"{policy.policy_digest}.json"
    if _lstat(snapshot_path) is None:
        raise InstallPolicyStoreError("current policy has no matching immutable snapshot")
    snapshot_policy, snapshot_encoded = _load_policy_file(
        snapshot_path,
        expected_digest=policy.policy_digest,
    )
    if snapshot_policy != policy or snapshot_encoded != encoded:
        raise InstallPolicyStoreError("current policy does not match its immutable snapshot")
    return policy


def persist_install_policy(
    policy: InstallConsentPolicy,
    root: Path | None = None,
) -> str:
    """Persist an immutable snapshot, then atomically select it as current."""

    if type(policy) is not InstallConsentPolicy:
        raise TypeError("policy must be an InstallConsentPolicy")
    store_root = _store_root(root)
    _prepare_policy_lock_parent(store_root)
    with secure_file_lock(_policy_lock_target(store_root)):
        snapshots = _prepare_store_for_write(store_root)
        encoded = _canonical_policy_text(policy)
        snapshot_path = snapshots / f"{policy.policy_digest}.json"
        snapshot_metadata = _lstat(snapshot_path)
        if snapshot_metadata is None:
            safe_atomic_write_text(snapshot_path, encoded, encoding="utf-8")
        else:
            existing, existing_encoded = _load_policy_file(
                snapshot_path,
                expected_digest=policy.policy_digest,
            )
            if existing != policy or existing_encoded.decode("utf-8") != encoded:
                raise InstallPolicyStoreError("immutable policy snapshot content changed")
        _validate_private_file(snapshot_path)
        safe_atomic_write_text(store_root / _CURRENT_FILE, encoded, encoding="utf-8")
        _validate_private_file(store_root / _CURRENT_FILE)
    return policy.policy_digest


def has_persisted_install_policy(root: Path | None = None) -> bool:
    """Return whether a valid, snapshot-backed current policy exists."""

    store_root = _store_root(root)
    if _lstat(store_root) is None:
        return False
    reject_symlink_path(store_root)
    with secure_file_lock(_policy_lock_target(store_root)):
        _validate_existing_store(store_root)
        if _lstat(store_root / _CURRENT_FILE) is None:
            return False
        _load_current_install_policy_unlocked(store_root)
        return True


@contextmanager
def hold_current_install_policy(
    expected_policy_digest: str,
    root: Path | None = None,
) -> Iterator[HeldInstallConsentPolicy]:
    """Hold the policy-current lock across an automatic consent commit."""

    if (
        not isinstance(expected_policy_digest, str)
        or len(expected_policy_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_policy_digest)
    ):
        raise InstallPolicyStoreError("expected policy digest must be lowercase SHA-256")
    store_root = _store_root(root)
    if _lstat(store_root) is None:
        raise InstallPolicyStoreError("no persisted install policy is available")
    reject_symlink_path(store_root)
    with secure_file_lock(_policy_lock_target(store_root)):
        root_metadata = _required_store_root_identity(store_root)
        policy = _load_current_install_policy_unlocked(store_root)
        if policy.policy_digest != expected_policy_digest:
            raise InstallPolicyStoreError("current install policy snapshot changed")
        authority = HeldInstallConsentPolicy(
            policy=policy,
            _store_root=store_root,
            _root_metadata=root_metadata,
        )
        authority.assert_current()
        yield authority


@dataclass(frozen=True, slots=True)
class HeldInstallConsentPolicy:
    """Pinned policy value revalidated under the stable parent lock."""

    policy: InstallConsentPolicy
    _store_root: Path
    _root_metadata: os.stat_result

    def assert_current(self) -> None:
        """Fail if the selected policy or its store root changed while held."""

        before = _required_store_root_identity(self._store_root)
        if not os.path.samestat(self._root_metadata, before):
            raise InstallPolicyStoreError("install policy store identity changed")
        current = _load_current_install_policy_unlocked(self._store_root)
        after = _required_store_root_identity(self._store_root)
        if (
            not os.path.samestat(before, after)
            or not os.path.samestat(self._root_metadata, after)
            or current != self.policy
        ):
            raise InstallPolicyStoreError("current install policy snapshot changed")


def _store_root(root: Path | None) -> Path:
    raw = default_install_policy_root() if root is None else Path(root).expanduser()
    absolute = Path(os.path.abspath(raw))
    if absolute == Path(absolute.anchor):
        raise InstallPolicyStoreError("install policy root cannot be a filesystem root")
    return absolute


def _policy_lock_target(store_root: Path) -> Path:
    return store_root.parent / f".{store_root.name}.policy-current"


def _prepare_policy_lock_parent(store_root: Path) -> None:
    parent = store_root.parent
    reject_symlink_path(parent)
    if _lstat(parent) is None:
        ensure_secure_directory(parent)


def _required_store_root_identity(store_root: Path) -> os.stat_result:
    metadata = _lstat(store_root)
    if metadata is None:
        raise InstallPolicyStoreError("install policy store identity changed")
    _validate_directory(store_root, "install policy root")
    return metadata


def _prepare_store_for_write(root: Path) -> Path:
    reject_symlink_path(root)
    if _lstat(root) is None:
        ensure_secure_directory(root)
    _validate_directory(root, "install policy root")
    _make_directory_private(root)
    snapshots = root / _SNAPSHOTS_DIR
    if _lstat(snapshots) is None:
        ensure_secure_directory(snapshots)
    _validate_directory(snapshots, "install policy snapshots directory")
    _make_directory_private(snapshots)
    return snapshots


def _validate_existing_store(root: Path) -> Path:
    reject_symlink_path(root)
    _validate_directory(root, "install policy root")
    snapshots = root / _SNAPSHOTS_DIR
    snapshot_metadata = _lstat(snapshots)
    if snapshot_metadata is None:
        current_metadata = _lstat(root / _CURRENT_FILE)
        if current_metadata is None:
            return snapshots
        raise InstallPolicyStoreError("current policy exists without a snapshots directory")
    _validate_directory(snapshots, "install policy snapshots directory")
    return snapshots


def _validate_directory(path: Path, label: str) -> None:
    metadata = _lstat(path)
    if metadata is None or stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise InstallPolicyStoreError(f"{label} must be a real directory")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InstallPolicyStoreError(f"{label} must not be group or world writable")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o700:
        raise InstallPolicyStoreError(f"{label} must use mode 0700")
    _validate_owner(metadata, label)


def _make_directory_private(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700, follow_symlinks=False)
    _validate_directory(path, str(path))


def _validate_private_file(path: Path) -> None:
    metadata = _lstat(path)
    if metadata is None:
        raise InstallPolicyStoreError(f"policy file is missing: {path.name}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallPolicyStoreError(f"policy file must be a regular file: {path.name}")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InstallPolicyStoreError(
            f"policy file must not be group or world writable: {path.name}"
        )
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise InstallPolicyStoreError(f"policy file must use mode 0600: {path.name}")
    if getattr(metadata, "st_nlink", 1) != 1:
        raise InstallPolicyStoreError(f"policy file must not be hard linked: {path.name}")
    _validate_owner(metadata, f"policy file {path.name}")


def _validate_owner(metadata: os.stat_result, label: str) -> None:
    getuid = getattr(os, "getuid", None)
    if callable(getuid) and metadata.st_uid != getuid():
        raise InstallPolicyStoreError(f"{label} must be owned by the current user")


def _load_policy_file(
    path: Path,
    *,
    expected_digest: str | None = None,
) -> tuple[InstallConsentPolicy, bytes]:
    encoded = _read_bounded_regular_file(path)
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise InstallPolicyStoreError(f"policy file must be strict UTF-8: {path.name}") from exc
    try:
        decoded = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except InstallPolicyStoreError:
        raise
    except json.JSONDecodeError as exc:
        raise InstallPolicyStoreError(f"policy file is invalid JSON: {path.name}") from exc
    if not isinstance(decoded, dict):
        raise InstallPolicyStoreError("policy JSON must be an object")
    try:
        policy = InstallConsentPolicy.from_dict(decoded)
    except (TypeError, ValueError) as exc:
        raise InstallPolicyStoreError(str(exc)) from exc
    canonical = _canonical_policy_text(policy).encode("utf-8")
    if encoded != canonical:
        raise InstallPolicyStoreError("policy file must use exact canonical JSON")
    if expected_digest is not None and policy.policy_digest != expected_digest:
        raise InstallPolicyStoreError("policy snapshot digest does not match its filename")
    return policy, encoded


def _read_bounded_regular_file(path: Path) -> bytes:
    reject_symlink_path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise InstallPolicyStoreError(
                f"policy file must not be a symlink: {path.name}"
            ) from exc
        raise
    try:
        before = os.fstat(descriptor)
        _validate_open_file(path, before)
        if before.st_size > MAX_POLICY_FILE_BYTES:
            raise InstallPolicyStoreError("policy file exceeds the size limit")
        chunks: list[bytes] = []
        remaining = MAX_POLICY_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > MAX_POLICY_FILE_BYTES:
            raise InstallPolicyStoreError("policy file exceeds the size limit")
        after = os.fstat(descriptor)
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(encoded) != after.st_size
        ):
            raise InstallPolicyStoreError("policy file changed while being read")
        return encoded
    finally:
        os.close(descriptor)


def _validate_open_file(path: Path, metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise InstallPolicyStoreError(f"policy file must be a regular file: {path.name}")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InstallPolicyStoreError(
            f"policy file must not be group or world writable: {path.name}"
        )
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise InstallPolicyStoreError(f"policy file must use mode 0600: {path.name}")
    if getattr(metadata, "st_nlink", 1) != 1:
        raise InstallPolicyStoreError(f"policy file must not be hard linked: {path.name}")
    _validate_owner(metadata, f"policy file {path.name}")


def _canonical_policy_text(policy: InstallConsentPolicy) -> str:
    return (
        json.dumps(
            policy.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise InstallPolicyStoreError(f"duplicate JSON key: {key}")
        decoded[key] = value
    return decoded


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None


__all__ = [
    "HeldInstallConsentPolicy",
    "InstallPolicyStoreError",
    "MAX_POLICY_FILE_BYTES",
    "default_install_policy_root",
    "has_persisted_install_policy",
    "hold_current_install_policy",
    "load_current_install_policy",
    "persist_install_policy",
]
