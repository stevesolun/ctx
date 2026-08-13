"""Stable, path-private identity for one local workspace directory."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ctx.utils._fs_utils import reject_symlink_path


_FACTORY_TOKEN = object()


class WorkspaceIdentityError(ValueError):
    """A workspace is unsafe, unavailable, or no longer the captured directory."""


@dataclass(frozen=True, slots=True, repr=False)
class WorkspaceIdentity:
    """One captured directory identity shared by every CTX host boundary."""

    digest: str
    _path: Path = field(repr=False, compare=True)
    _device: int = field(repr=False, compare=True)
    _inode: int = field(repr=False, compare=True)
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _FACTORY_TOKEN:
            raise TypeError("workspace identities are issued only by the capture factory")
        expected = _identity_digest(self._device, self._inode)
        if self.digest != expected:
            raise WorkspaceIdentityError("workspace identity digest is invalid")

    def assert_current(self) -> None:
        """Fail unless the original lexical workspace still names this directory."""

        try:
            current = capture_workspace_identity(self._path)
        except WorkspaceIdentityError:
            raise WorkspaceIdentityError("workspace identity changed or became unsafe") from None
        if current != self:
            raise WorkspaceIdentityError("workspace identity changed or became unsafe")

    def __repr__(self) -> str:
        return f"WorkspaceIdentity(digest={self.digest!r})"

    def __copy__(self) -> WorkspaceIdentity:
        raise TypeError("WorkspaceIdentity cannot be copied")

    def __deepcopy__(self, _memo: object) -> WorkspaceIdentity:
        raise TypeError("WorkspaceIdentity cannot be copied")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("WorkspaceIdentity cannot be serialized")


def capture_workspace_identity(workspace: Path) -> WorkspaceIdentity:
    """Capture an existing directory without accepting symlink ancestry."""

    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a Path")
    lexical = Path(os.path.normcase(os.path.abspath(os.fspath(workspace))))
    try:
        reject_symlink_path(lexical)
    except ValueError:
        raise WorkspaceIdentityError("workspace ancestry must not contain symlinks") from None
    try:
        canonical = Path(os.path.normcase(os.path.realpath(lexical)))
        current = os.stat(canonical, follow_symlinks=False)
    except OSError:
        raise WorkspaceIdentityError("workspace must be an existing stable directory") from None
    if not stat.S_ISDIR(current.st_mode):
        raise WorkspaceIdentityError("workspace must be an existing stable directory")
    return WorkspaceIdentity(
        digest=_identity_digest(current.st_dev, current.st_ino),
        _path=lexical,
        _device=current.st_dev,
        _inode=current.st_ino,
        _token=_FACTORY_TOKEN,
    )


def _identity_digest(device: int, inode: int) -> str:
    """Identify a directory by the pair the kernel uses for the same purpose.

    Known limit, and it is a real one. This cannot detect a directory that was
    deleted and recreated when the filesystem reuses the freed inode, which
    ext4 routinely does -- so on Linux a recreated workspace can hash to the
    identity of the one it replaced. APFS allocates inodes monotonically, which
    is why this reads as sound on macOS.

    Neither creation nor change time closes it. ``st_ctime_ns`` moves on any
    metadata write, which breaks the property that two captures of one
    workspace agree; ``st_birthtime`` is second-granular and unchanged by a
    recreate inside the same second, and is absent on many Linux builds.
    Detecting recreation reliably needs a marker written inside the workspace,
    which capture deliberately does not do -- capturing an identity must not
    mutate the thing being identified. Tracked in GOOD_FIRST_ISSUES.md.
    """

    return hashlib.sha256(f"ctx-workspace-v1\0{device}\0{inode}".encode()).hexdigest()


__all__ = [
    "WorkspaceIdentity",
    "WorkspaceIdentityError",
    "capture_workspace_identity",
]
