"""Pinned POSIX filesystem primitive for the built-in skill content store.

All child operations are descriptor-relative.  Durability is limited to the
platform filesystem's ordinary ``fsync`` contract; no stronger device-cache or
power-loss guarantee is claimed.  The configured store and its
ancestor chain must be protected from rename by users other than the current
user or root.  Processes running as the same user are part of the trusted CTX
runtime boundary and must cooperate through the content lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ctx.utils._fs_utils import secure_directory, supports_secure_directory_fds


_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_RECOVERY_LINK_COUNTS = frozenset(range(1, 66))
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)


class SkillCasFilesystemError(RuntimeError):
    """The physical skill store could not establish a safe exact state."""


class SkillCasFilesystemConflict(SkillCasFilesystemError):
    """A store entry has unsafe or ambiguous identity/material."""


class SkillCasFilesystemUnsupported(SkillCasFilesystemError):
    """This platform lacks the native primitives required by this actuator."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> FileIdentity:
        return cls(device=metadata.st_dev, inode=metadata.st_ino)


@dataclass(frozen=True, slots=True)
class RootIdentity:
    canonical_root: str
    device: int
    inode: int

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "canonical_root": self.canonical_root,
                    "device": self.device,
                    "inode": self.inode,
                    "schema": "ctx.skill-cas-target-v2",
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ChildInspection:
    state: Literal["exact", "absent", "conflict"]
    identity: FileIdentity | None = None
    link_count: int | None = None
    safely_removable: bool = False


def skill_cas_root_identity(root: Path) -> RootIdentity:
    """Authenticate and bind one owner-private POSIX directory."""

    _require_supported_platform()
    try:
        with secure_directory(root, create=False) as directory:
            descriptor = _directory_fd(directory)
            canonical_root = Path(directory.path)
            metadata = os.fstat(descriptor)
            _validate_private_directory(metadata)
            _validate_protected_ancestry(canonical_root)
            current = os.stat(canonical_root, follow_symlinks=False)
            if not os.path.samestat(metadata, current):
                raise SkillCasFilesystemConflict("skill store changed while it was opened")
            return RootIdentity(
                canonical_root=os.fspath(canonical_root),
                device=metadata.st_dev,
                inode=metadata.st_ino,
            )
    except SkillCasFilesystemError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise SkillCasFilesystemError("skill store root cannot be authenticated") from exc


@contextmanager
def open_skill_cas_directory(
    expected: RootIdentity,
) -> Iterator[PinnedSkillCasDirectory]:
    """Open the exact directory bound by a trusted runtime configuration."""

    _require_supported_platform()
    try:
        with secure_directory(Path(expected.canonical_root), create=False) as directory:
            descriptor = _directory_fd(directory)
            metadata = os.fstat(descriptor)
            _validate_private_directory(metadata)
            if FileIdentity.from_stat(metadata) != FileIdentity(expected.device, expected.inode):
                raise SkillCasFilesystemConflict("skill store target identity changed")
            _validate_protected_ancestry(Path(expected.canonical_root))
            yield PinnedSkillCasDirectory(
                root=Path(expected.canonical_root),
                descriptor=descriptor,
                expected=expected,
            )
    except SkillCasFilesystemError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise SkillCasFilesystemError("skill store directory operation failed") from exc


class PinnedSkillCasDirectory:
    """Names-only access to one authenticated and pinned content-store root."""

    __slots__ = ("_descriptor", "_expected", "_root")

    def __init__(self, *, root: Path, descriptor: int, expected: RootIdentity) -> None:
        self._root = root
        self._descriptor = descriptor
        self._expected = expected

    def inspect_exact_utf8(
        self,
        name: str,
        *,
        expected_sha256: str,
        expected_bytes: int,
        allowed_links: frozenset[int],
        durable: bool = False,
    ) -> ChildInspection:
        _validate_child_name(name)
        try:
            before = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return ChildInspection("absent")
        except OSError as exc:
            raise SkillCasFilesystemError("skill store child cannot be inspected") from exc

        safely_removable = _is_safely_removable(before)
        if not _is_safe_regular(before, allowed_links=allowed_links):
            return ChildInspection(
                "conflict",
                identity=FileIdentity.from_stat(before),
                link_count=before.st_nlink,
                safely_removable=safely_removable,
            )

        descriptor = -1
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=self._descriptor)
            opened = os.fstat(descriptor)
            if not os.path.samestat(before, opened) or not _is_safe_regular(
                opened,
                allowed_links=allowed_links,
            ):
                return ChildInspection(
                    "conflict",
                    identity=FileIdentity.from_stat(opened),
                    link_count=opened.st_nlink,
                    safely_removable=_is_safely_removable(opened),
                )

            remaining = expected_bytes + 1
            chunks: list[bytes] = []
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            if durable:
                os.fsync(descriptor)

            after_handle = os.fstat(descriptor)
            try:
                after_path = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise SkillCasFilesystemError("skill store child changed while read") from exc
            if (
                not _same_file_snapshot(before, opened)
                or not _same_file_snapshot(opened, after_handle)
                or not _same_file_snapshot(after_handle, after_path)
                or not _is_safe_regular(after_path, allowed_links=allowed_links)
            ):
                return ChildInspection(
                    "conflict",
                    identity=FileIdentity.from_stat(after_path),
                    link_count=after_path.st_nlink,
                    safely_removable=_is_safely_removable(after_path),
                )
            try:
                content.decode("utf-8", errors="strict")
            except UnicodeError:
                return ChildInspection(
                    "conflict",
                    identity=FileIdentity.from_stat(after_path),
                    link_count=after_path.st_nlink,
                    safely_removable=_is_safely_removable(after_path),
                )
            if (
                len(content) != expected_bytes
                or hashlib.sha256(content).hexdigest() != expected_sha256
            ):
                return ChildInspection(
                    "conflict",
                    identity=FileIdentity.from_stat(after_path),
                    link_count=after_path.st_nlink,
                    safely_removable=_is_safely_removable(after_path),
                )
            return ChildInspection(
                "exact",
                identity=FileIdentity.from_stat(after_path),
                link_count=after_path.st_nlink,
                safely_removable=_is_safely_removable(after_path),
            )
        except SkillCasFilesystemError:
            raise
        except OSError as exc:
            raise SkillCasFilesystemError("skill store child read failed") from exc
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def read_exact_utf8_bytes(
        self,
        name: str,
        *,
        expected_sha256: str,
        expected_bytes: int,
        allowed_links: frozenset[int],
        durable: bool = False,
    ) -> bytes:
        """Return only bytes re-read from one independently authenticated child."""

        inspection = self.inspect_exact_utf8(
            name,
            expected_sha256=expected_sha256,
            expected_bytes=expected_bytes,
            allowed_links=allowed_links,
            durable=durable,
        )
        if inspection.state != "exact" or inspection.identity is None:
            raise SkillCasFilesystemConflict("skill store child is not exact")

        descriptor = -1
        try:
            before = self._stat_required(name)
            if FileIdentity.from_stat(before) != inspection.identity or not _is_safe_regular(
                before, allowed_links=allowed_links
            ):
                raise SkillCasFilesystemConflict(
                    "skill store child changed before authenticated read"
                )
            descriptor = os.open(name, _READ_FLAGS, dir_fd=self._descriptor)
            opened = os.fstat(descriptor)
            if not _same_file_snapshot(before, opened) or not _is_safe_regular(
                opened,
                allowed_links=allowed_links,
            ):
                raise SkillCasFilesystemConflict(
                    "skill store child changed during authenticated read"
                )

            remaining = expected_bytes + 1
            chunks: list[bytes] = []
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            if durable:
                os.fsync(descriptor)

            after_handle = os.fstat(descriptor)
            after_path = self._stat_required(name)
            if (
                not _same_file_snapshot(opened, after_handle)
                or not _same_file_snapshot(after_handle, after_path)
                or not _is_safe_regular(after_path, allowed_links=allowed_links)
            ):
                raise SkillCasFilesystemConflict(
                    "skill store child changed during authenticated read"
                )
            try:
                content.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise SkillCasFilesystemConflict(
                    "skill store child is not exact UTF-8 material"
                ) from exc
            if (
                len(content) != expected_bytes
                or hashlib.sha256(content).hexdigest() != expected_sha256
            ):
                raise SkillCasFilesystemConflict("skill store child is not exact")
            return content
        except SkillCasFilesystemError:
            raise
        except OSError as exc:
            raise SkillCasFilesystemError("skill store authenticated read failed") from exc
        finally:
            if descriptor != -1:
                os.close(descriptor)

    def create_exact(self, name: str, body: bytes) -> FileIdentity:
        """Create, write and durably sync one new private stage entry."""

        _validate_child_name(name)
        descriptor = -1
        try:
            descriptor = os.open(
                name,
                _WRITE_FLAGS,
                _FILE_MODE,
                dir_fd=self._descriptor,
            )
            os.fchmod(descriptor, _FILE_MODE)
            view = memoryview(body)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("skill store staging write made no progress")
                offset += written
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if not _is_safe_regular(
                metadata, allowed_links=frozenset({1})
            ) or metadata.st_size != len(body):
                raise SkillCasFilesystemConflict("skill store stage metadata is invalid")
            identity = FileIdentity.from_stat(metadata)
        except FileExistsError as exc:
            raise SkillCasFilesystemConflict("skill store staging address is occupied") from exc
        except SkillCasFilesystemError:
            raise
        except OSError as exc:
            raise SkillCasFilesystemError("skill store staging write failed") from exc
        finally:
            if descriptor != -1:
                os.close(descriptor)

        inspection = self.inspect_exact_utf8(
            name,
            expected_sha256=hashlib.sha256(body).hexdigest(),
            expected_bytes=len(body),
            allowed_links=frozenset({1}),
        )
        if inspection.state != "exact" or inspection.identity != identity:
            raise SkillCasFilesystemConflict("skill store stage could not be authenticated")
        return identity

    def link_child_exclusive(
        self,
        source: str,
        destination: str,
        *,
        expected_source: FileIdentity,
    ) -> bool:
        """Hard-link a pinned stage to an absent final name without replacement."""

        _validate_child_name(source)
        _validate_child_name(destination)
        current = self._stat_required(source)
        if FileIdentity.from_stat(current) != expected_source:
            raise SkillCasFilesystemConflict("skill store source changed before publication")
        if not _is_safe_regular(current, allowed_links=frozenset({1})):
            try:
                destination_current = os.stat(
                    destination,
                    dir_fd=self._descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                destination_current = None
            except OSError as exc:
                raise SkillCasFilesystemError(
                    "skill store publication collision cannot be inspected"
                ) from exc
            if (
                destination_current is not None
                and FileIdentity.from_stat(destination_current) == expected_source
                and _is_safe_regular(
                    current,
                    allowed_links=_RECOVERY_LINK_COUNTS,
                )
                and _is_safe_regular(
                    destination_current,
                    allowed_links=_RECOVERY_LINK_COUNTS,
                )
            ):
                return False
            raise SkillCasFilesystemConflict("skill store source changed before publication")
        try:
            os.link(
                source,
                destination,
                src_dir_fd=self._descriptor,
                dst_dir_fd=self._descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            raise SkillCasFilesystemError("skill store publication failed") from exc

        source_after = self._stat_required(source)
        destination_after = self._stat_required(destination)
        if (
            FileIdentity.from_stat(source_after) != expected_source
            or FileIdentity.from_stat(destination_after) != expected_source
            or source_after.st_nlink != 2
            or destination_after.st_nlink != 2
            or not _is_safe_regular(source_after, allowed_links=frozenset({2}))
            or not _is_safe_regular(destination_after, allowed_links=frozenset({2}))
        ):
            raise SkillCasFilesystemConflict("skill store publication identity is ambiguous")
        self.fsync()
        return True

    def unlink_child_if_identity(
        self,
        name: str,
        *,
        expected: FileIdentity,
        allowed_links: frozenset[int],
    ) -> None:
        """Delete only the still-authenticated entry, then sync the directory."""

        _validate_child_name(name)
        try:
            current = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise SkillCasFilesystemConflict(
                "skill store child disappeared before cleanup"
            ) from exc
        except OSError as exc:
            raise SkillCasFilesystemError("skill store child cleanup check failed") from exc
        if FileIdentity.from_stat(current) != expected or not _is_safe_regular(
            current, allowed_links=allowed_links
        ):
            raise SkillCasFilesystemConflict("skill store child changed before cleanup")
        try:
            os.unlink(name, dir_fd=self._descriptor)
            self.fsync()
        except OSError as exc:
            raise SkillCasFilesystemError("skill store child cleanup failed") from exc

    def stage_names(self, *, prefix: str, suffix: str, limit: int) -> tuple[str, ...]:
        """Enumerate a bounded exact action-stage namespace."""

        if not prefix or not suffix or limit < 1:
            raise ValueError("invalid stage enumeration contract")
        try:
            raw_names = os.listdir(self._descriptor)
        except OSError as exc:
            raise SkillCasFilesystemError("skill store cannot enumerate recovery stages") from exc
        names = tuple(
            sorted(
                name
                for name in raw_names
                if isinstance(name, str)
                and name.startswith(prefix)
                and name.endswith(suffix)
                and _is_valid_stage_name(name, prefix=prefix, suffix=suffix)
            )
        )
        if len(names) > limit:
            raise SkillCasFilesystemConflict("skill store has too many recovery stages")
        return names

    def fsync(self) -> None:
        try:
            os.fsync(self._descriptor)
        except OSError as exc:
            raise SkillCasFilesystemError("skill store directory durability failed") from exc

    def revalidate_root(self) -> None:
        current = skill_cas_root_identity(self._root)
        if current != self._expected:
            raise SkillCasFilesystemConflict("skill store path no longer names the pinned root")

    def _stat_required(self, name: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
        except OSError as exc:
            raise SkillCasFilesystemError("skill store child is unavailable") from exc


def _directory_fd(directory: object) -> int:
    descriptor = getattr(directory, "_directory_fd", None)
    if not isinstance(descriptor, int):
        raise SkillCasFilesystemUnsupported("descriptor-relative skill storage is unavailable")
    return descriptor


def _require_supported_platform() -> None:
    if os.name == "nt":
        raise SkillCasFilesystemUnsupported(
            "secure native Windows skill installation is not enabled"
        )
    dir_fd_support: set[object] = getattr(os, "supports_dir_fd", set())
    follow_support: set[object] = getattr(os, "supports_follow_symlinks", set())
    fd_support: set[object] = getattr(os, "supports_fd", set())
    if (
        not supports_secure_directory_fds()
        or os.link not in dir_fd_support
        or os.link not in follow_support
        or os.listdir not in fd_support
        or not getattr(os, "O_NOFOLLOW", 0)
    ):
        raise SkillCasFilesystemUnsupported(
            "secure descriptor-relative skill installation is unavailable"
        )


def _validate_private_directory(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE
    ):
        raise SkillCasFilesystemConflict("skill store root must be owner-private")


def _validate_protected_ancestry(root: Path) -> None:
    """Reject ancestors whose entries can be renamed by unrelated users."""

    current = root
    try:
        current_metadata = os.stat(current, follow_symlinks=False)
        while current != Path(current.anchor):
            parent = current.parent
            parent_metadata = os.stat(parent, follow_symlinks=False)
            if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
                raise SkillCasFilesystemConflict("skill store ancestor is not a real directory")
            if parent_metadata.st_uid not in {0, os.geteuid()}:
                raise SkillCasFilesystemConflict("skill store ancestor has an untrusted owner")
            writable = stat.S_IMODE(parent_metadata.st_mode) & 0o022
            sticky = bool(parent_metadata.st_mode & stat.S_ISVTX)
            if writable and not (sticky and current_metadata.st_uid in {0, os.geteuid()}):
                raise SkillCasFilesystemConflict("skill store ancestor permits unsafe rename")
            current = parent
            current_metadata = parent_metadata
    except SkillCasFilesystemError:
        raise
    except OSError as exc:
        raise SkillCasFilesystemError("skill store ancestry cannot be authenticated") from exc


def _validate_child_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("unsafe skill store child name")


def _is_safe_regular(metadata: os.stat_result, *, allowed_links: frozenset[int]) -> bool:
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == _FILE_MODE
        and metadata.st_nlink in allowed_links
    )


def _is_safely_removable(metadata: os.stat_result) -> bool:
    return _is_safe_regular(metadata, allowed_links=frozenset({1}))


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return bool(
        os.path.samestat(left, right)
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_nlink == right.st_nlink
        and left.st_size == right.st_size
        and getattr(left, "st_mtime_ns", None) == getattr(right, "st_mtime_ns", None)
        and getattr(left, "st_ctime_ns", None) == getattr(right, "st_ctime_ns", None)
    )


def _is_valid_stage_name(name: str, *, prefix: str, suffix: str) -> bool:
    middle = name[len(prefix) : len(name) - len(suffix)]
    return bool(
        len(middle) == 64
        and all(character in "0123456789abcdef" for character in middle)
        and "/" not in name
        and "\\" not in name
    )


__all__ = [
    "ChildInspection",
    "FileIdentity",
    "PinnedSkillCasDirectory",
    "RootIdentity",
    "SkillCasFilesystemConflict",
    "SkillCasFilesystemError",
    "SkillCasFilesystemUnsupported",
    "open_skill_cas_directory",
    "skill_cas_root_identity",
]
