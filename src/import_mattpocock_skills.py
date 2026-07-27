#!/usr/bin/env python3
"""import_mattpocock_skills.py -- Deploy mattpocock/skills into ~/.claude/skills.

Reads imported-skills/mattpocock/MANIFEST.json. Each entry creates a directory
named ``mattpocock-<slug>``, copies SKILL.md (with attribution header
prepended), and copies any support files (ADR-FORMAT.md, deep-modules.md,
scripts/, etc.) verbatim.

Idempotent. Safe to re-run.

Usage:
    python src/import_mattpocock_skills.py --dry-run
    python src/import_mattpocock_skills.py --install
    python src/import_mattpocock_skills.py --install --target ./custom-skills-dir
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ctx_config import cfg

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORT_ROOT = REPO_ROOT / "imported-skills" / "mattpocock"
MANIFEST_PATH = IMPORT_ROOT / "MANIFEST.json"

_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_NAME_SURROGATE_REPARSE_BIT = 0x20000000


@dataclass(frozen=True)
class _DestinationState:
    identity: tuple[int, int]
    mode: int
    link_count: int
    digest: bytes
    content: bytes


@dataclass(frozen=True)
class _ParentState:
    path: Path
    identity: tuple[int, int] | None


@dataclass(frozen=True)
class _CandidateWrite:
    field: str
    destination: Path
    content: bytes
    mode: int


@dataclass(frozen=True)
class _PreparedWrite:
    destination: Path
    content: bytes
    mode: int
    state: _DestinationState | None


@dataclass(frozen=True)
class _PreparedEntry:
    destination: Path
    destination_existed: bool
    changed: bool
    support_paths: tuple[Path, ...]
    target_chain: tuple[_ParentState, ...]
    parent_states: tuple[_ParentState, ...]
    writes: tuple[_PreparedWrite, ...]


@dataclass(frozen=True)
class _OpenedDirectory:
    fd: int
    identity: tuple[int, int]


@dataclass(frozen=True)
class _StagedWrite:
    write: _PreparedWrite
    temporary_name: str
    parent_fd: int | None
    state: _DestinationState

    @property
    def temporary_path(self) -> Path:
        return self.write.destination.parent / self.temporary_name


@dataclass(frozen=True)
class _RecoverySnapshot:
    name: str
    state: _DestinationState


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        print(f"Manifest not found: {MANIFEST_PATH}", file=sys.stderr)
        print("Run: python imported-skills/mattpocock/build_manifest.py", file=sys.stderr)
        sys.exit(1)
    try:
        raw_manifest = MANIFEST_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"Unable to read manifest: {MANIFEST_PATH}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        print(
            f"Invalid manifest JSON: {MANIFEST_PATH}:{exc.lineno}:{exc.colno}: {exc.msg}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if not isinstance(manifest, dict):
        print(
            f"Invalid manifest: {MANIFEST_PATH}: expected JSON object, "
            f"got {type(manifest).__name__}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    return manifest


def _validate(field: str, value: object, *, regex: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}: expected non-empty string, got {type(value).__name__}")
    if regex is not None and not regex.fullmatch(value):
        raise ValueError(f"{field}: {value!r} failed strict format check")
    return value


def _resolve_within(root: Path, candidate_rel: str, *, field: str) -> Path:
    if ".." in Path(candidate_rel).parts or candidate_rel.startswith(("/", "\\")):
        raise ValueError(f"{field}: path traversal denied in {candidate_rel!r}")
    resolved = (root / candidate_rel).resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{field}: {candidate_rel!r} resolves outside {root}") from exc
    return resolved


def _portable_destination_key(relative_path: str | Path) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", unicodedata.normalize("NFC", part).casefold())
        for part in Path(relative_path).parts
        if part not in ("", ".")
    )


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None


def _destination_parent_paths(target_dir: Path, destinations: list[Path]) -> list[Path]:
    parents = {target_dir}
    for destination in destinations:
        parent = destination.parent
        try:
            parent.relative_to(target_dir)
        except ValueError as exc:
            raise ValueError(f"destination parent {parent} is outside target_dir") from exc
        while parent != target_dir:
            parents.add(parent)
            parent = parent.parent
    return sorted(parents, key=lambda path: len(path.relative_to(target_dir).parts))


def _is_name_surrogate_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_tag = getattr(metadata, "st_reparse_tag", 0)
    return bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT and reparse_tag & _NAME_SURROGATE_REPARSE_BIT
    )


def _validate_parent_metadata(
    path: Path,
    metadata: os.stat_result,
    *,
    label: str = "destination parent",
) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label}: {path} is a symlink")
    if _is_name_surrogate_reparse_point(metadata):
        raise ValueError(f"{label}: {path} is a junction or name-surrogate reparse point")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label}: {path} is not a directory")


def _preflight_target_root(target_dir: Path) -> tuple[_ParentState, tuple[_ParentState, ...]]:
    missing: list[Path] = []
    current = target_dir
    while True:
        try:
            metadata = _lstat_optional(current)
        except NotADirectoryError as exc:
            raise ValueError(f"target: {target_dir} is not a directory") from exc
        if metadata is not None:
            break
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ValueError(f"target: no existing ancestor for {target_dir}")
        current = parent

    if current == target_dir:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"target: {target_dir} is not a directory")
        if _is_name_surrogate_reparse_point(metadata):
            raise ValueError(f"target: {target_dir} is a junction or name-surrogate reparse point")
    else:
        _validate_parent_metadata(current, metadata)

    chain = [_ParentState(current, _identity(metadata))]
    chain.extend(_ParentState(path, None) for path in reversed(missing))
    return chain[-1], tuple(chain)


def _preflight_destination_parents(
    target_root: _ParentState,
    destinations: list[Path],
) -> tuple[_ParentState, ...]:
    states = [target_root]
    for parent in _destination_parent_paths(target_root.path, destinations)[1:]:
        try:
            metadata = _lstat_optional(parent)
        except NotADirectoryError as exc:
            raise ValueError(f"destination parent: {parent.parent} is not a directory") from exc
        if metadata is None:
            states.append(_ParentState(parent, None))
            continue
        _validate_parent_metadata(parent, metadata)
        states.append(_ParentState(parent, _identity(metadata)))
    return tuple(states)


def _read_destination(
    destination: Path,
    *,
    field: str,
) -> tuple[_DestinationState | None, bytes | None]:
    metadata = _lstat_optional(destination)
    if metadata is None:
        return None, None
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{field}: {destination} must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{field}: {destination} is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(destination, flags)
    except OSError as exc:
        raise ValueError(f"{field}: {destination} changed during preflight") from exc
    opened_metadata = os.fstat(fd)
    if not stat.S_ISREG(opened_metadata.st_mode) or _identity(opened_metadata) != _identity(
        metadata
    ):
        os.close(fd)
        raise ValueError(f"{field}: {destination} changed during preflight")
    with os.fdopen(fd, "rb") as handle:
        existing = handle.read()
    return (
        _DestinationState(
            _identity(opened_metadata),
            stat.S_IMODE(opened_metadata.st_mode),
            opened_metadata.st_nlink,
            hashlib.sha256(existing).digest(),
            existing,
        ),
        existing,
    )


def render_attribution_header(manifest: dict) -> str:
    def value(field: str) -> str:
        text = _validate(f"manifest.{field}", manifest.get(field))
        if "\r" in text or "\n" in text or "-->" in text:
            raise ValueError(f"manifest.{field}: unsafe attribution value")
        return text

    upstream = value("upstream")
    upstream_revision = value("upstream_revision")
    license_name = value("license")
    return (
        f"<!-- mattpocock-import: upstream={upstream} "
        f"rev={upstream_revision[:12]} license={license_name} -->\n"
    )


def _prepare_entry(
    entry: dict,
    manifest: dict,
    target_dir: Path,
) -> _PreparedEntry:
    target_root, target_chain = _preflight_target_root(target_dir)
    slug = _validate("slug", entry.get("slug"), regex=_SAFE_SLUG_RE)
    source_path_raw = _validate("source_path", entry.get("source_path"))
    source = _resolve_within(IMPORT_ROOT, source_path_raw, field="source_path")
    try:
        source_content, source_mode = _read_source_payload(
            source,
            field=f"source_path: {source_path_raw!r}",
        )
    except FileNotFoundError:
        raise FileNotFoundError(f"Source skill missing: {source}") from None
    source_dir = source.parent

    skill_dir = target_dir / f"mattpocock-{slug}"
    dest_resolved = skill_dir.resolve()
    try:
        dest_resolved.relative_to(target_dir)
    except ValueError as exc:
        raise ValueError(f"skill dir {skill_dir} resolves outside target_dir") from exc

    dest_skill = skill_dir / "SKILL.md"
    dest_skill_resolved = _resolve_within(skill_dir, "SKILL.md", field="skill destination")
    header = render_attribution_header(manifest)
    body = source_content.decode("utf-8")
    if body.startswith("<!-- mattpocock-import:"):
        _, separator, imported_body = body.partition("-->")
        if not separator:
            raise ValueError(
                f"source_path: {source_path_raw!r} has unterminated attribution header"
            )
        body = imported_body.lstrip("\r\n")
    content = header + body

    support_paths: list[Path] = []
    candidates = [
        _CandidateWrite(
            field="skill destination",
            destination=dest_skill,
            content=content.encode("utf-8"),
            mode=source_mode,
        )
    ]
    support_files = entry.get("support_files", [])
    if not isinstance(support_files, list):
        raise ValueError(f"support_files: expected list, got {type(support_files).__name__}")

    destination_owners = {dest_skill_resolved: "SKILL.md"}
    portable_destination_owners = {_portable_destination_key("SKILL.md"): "SKILL.md"}
    for raw_rel in support_files:
        rel = _validate("support_files", raw_rel)
        source_support = _resolve_within(source_dir, rel, field="support_files")
        dest_support = skill_dir / Path(rel)
        dest_support_resolved = _resolve_within(
            skill_dir,
            rel,
            field="support_files destination",
        )
        previous = destination_owners.get(dest_support_resolved)
        portable_key = _portable_destination_key(Path(rel))
        if previous is None:
            previous = portable_destination_owners.get(portable_key)
        if previous is not None:
            raise ValueError(
                f"support_files: {rel!r} has duplicate destination "
                f"{dest_support.relative_to(skill_dir)!s} (already used by {previous!r})"
            )
        destination_owners[dest_support_resolved] = rel
        portable_destination_owners[portable_key] = rel

        try:
            support_content, support_mode = _read_source_payload(
                source_support,
                field=f"support_files: {rel!r}",
            )
        except FileNotFoundError:
            raise ValueError(f"support_files: {rel!r} is not a regular file") from None
        support_paths.append(source_support)
        candidates.append(
            _CandidateWrite(
                field="support_files destination",
                destination=dest_support,
                content=support_content,
                mode=support_mode,
            )
        )

    parent_states = _preflight_destination_parents(
        target_root,
        [candidate.destination for candidate in candidates],
    )
    writes: list[_PreparedWrite] = []
    destination_existed = False
    manage_modes = getattr(os, "fchmod", None) is not None
    for candidate in candidates:
        state, existing = _read_destination(candidate.destination, field=candidate.field)
        if candidate.destination == dest_skill:
            destination_existed = state is not None
        if (
            existing != candidate.content
            or (state is not None and state.link_count > 1)
            or (manage_modes and state is not None and state.mode != candidate.mode)
        ):
            writes.append(
                _PreparedWrite(
                    destination=candidate.destination,
                    content=candidate.content,
                    mode=candidate.mode,
                    state=state,
                )
            )

    return _PreparedEntry(
        destination=dest_skill,
        destination_existed=destination_existed,
        changed=bool(writes),
        support_paths=tuple(support_paths),
        target_chain=target_chain,
        parent_states=parent_states,
        writes=tuple(writes),
    )


def _metadata_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validate_expected_destination(
    write: _PreparedWrite,
    metadata: os.stat_result | None,
) -> None:
    current_identity = None
    if metadata is not None:
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"destination {write.destination} changed after preflight")
        current_identity = _identity(metadata)
    expected_identity = None if write.state is None else write.state.identity
    if current_identity != expected_identity:
        raise ValueError(f"destination {write.destination} changed after preflight")


def _open_target_directory(prepared: _PreparedEntry) -> _OpenedDirectory:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    anchor = prepared.target_chain[0]
    metadata = _lstat_optional(anchor.path)
    if metadata is None or anchor.identity is None:
        raise ValueError(f"destination parent {anchor.path} changed after preflight")
    _validate_parent_metadata(anchor.path, metadata)

    fd = os.open(anchor.path, directory_flags)
    try:
        opened_metadata = os.fstat(fd)
        if _identity(metadata) != anchor.identity or _identity(opened_metadata) != anchor.identity:
            raise ValueError(f"destination parent {anchor.path} changed after preflight")

        current_path = anchor.path
        for state in prepared.target_chain[1:]:
            if state.path.parent != current_path:
                raise ValueError(f"invalid target creation chain for {state.path}")
            child_metadata = _metadata_at(fd, state.path.name)
            if child_metadata is not None or state.identity is not None:
                raise ValueError(f"destination parent {state.path} changed after preflight")
            os.mkdir(state.path.name, mode=0o755, dir_fd=fd)
            child_metadata = os.stat(
                state.path.name,
                dir_fd=fd,
                follow_symlinks=False,
            )
            _validate_parent_metadata(state.path, child_metadata)
            child_fd = os.open(state.path.name, directory_flags, dir_fd=fd)
            child_opened_metadata = os.fstat(child_fd)
            if _identity(child_opened_metadata) != _identity(child_metadata):
                os.close(child_fd)
                raise ValueError(f"destination parent {state.path} changed during creation")
            os.close(fd)
            fd = child_fd
            current_path = state.path

        if current_path != prepared.parent_states[0].path:
            raise ValueError(f"target creation ended at unexpected path {current_path}")
        final_metadata = os.fstat(fd)
        return _OpenedDirectory(fd=fd, identity=_identity(final_metadata))
    except BaseException:
        os.close(fd)
        raise


def _open_parent_directories(prepared: _PreparedEntry) -> dict[Path, _OpenedDirectory]:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directories: dict[Path, _OpenedDirectory] = {}
    try:
        target_state = prepared.parent_states[0]
        directories[target_state.path] = _open_target_directory(prepared)
        for parent_state in prepared.parent_states[1:]:
            path = parent_state.path
            created = False
            parent_directory = directories[path.parent]
            metadata = _metadata_at(parent_directory.fd, path.name)
            if metadata is None:
                if parent_state.identity is not None:
                    raise ValueError(f"destination parent {path} changed after preflight")
                os.mkdir(path.name, mode=0o755, dir_fd=parent_directory.fd)
                created = True
                metadata = os.stat(
                    path.name,
                    dir_fd=parent_directory.fd,
                    follow_symlinks=False,
                )
            elif parent_state.identity is None:
                raise ValueError(f"destination parent {path} changed after preflight")
            _validate_parent_metadata(path, metadata)
            fd = os.open(path.name, directory_flags, dir_fd=parent_directory.fd)

            opened_metadata = os.fstat(fd)
            opened_identity = _identity(opened_metadata)
            expected_identity = opened_identity if created else parent_state.identity
            if opened_identity != _identity(metadata) or opened_identity != expected_identity:
                os.close(fd)
                raise ValueError(f"destination parent {path} changed after preflight")
            directories[path] = _OpenedDirectory(fd=fd, identity=opened_identity)
        return directories
    except BaseException:
        for directory in reversed(directories.values()):
            os.close(directory.fd)
        raise


def _write_staged_payload(fd: int, write: _PreparedWrite) -> _DestinationState:
    with os.fdopen(fd, "wb") as handle:
        handle.write(write.content)
        handle.flush()
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(handle.fileno(), write.mode)
        # Windows has no descriptor chmod and only limited path chmod semantics.
        # Keep mkstemp's private mode there instead of reopening a mutable path.
        os.fsync(handle.fileno())
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("staged payload is not a regular file")
        return _DestinationState(
            identity=_identity(metadata),
            mode=stat.S_IMODE(metadata.st_mode),
            link_count=metadata.st_nlink,
            digest=hashlib.sha256(write.content).digest(),
            content=write.content,
        )


def _stage_writes(
    prepared: _PreparedEntry,
    directories: dict[Path, _OpenedDirectory],
) -> list[_StagedWrite]:
    staged_writes: list[_StagedWrite] = []
    try:
        for write in prepared.writes:
            parent_fd = directories[write.destination.parent].fd
            temporary_name = f".{write.destination.name}.{secrets.token_hex(8)}.tmp"
            temp_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            temp_fd = os.open(temporary_name, temp_flags, 0o600, dir_fd=parent_fd)
            try:
                state = _write_staged_payload(temp_fd, write)
            except BaseException:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except OSError:
                    pass
                raise
            staged_writes.append(_StagedWrite(write, temporary_name, parent_fd, state))
        return staged_writes
    except BaseException:
        _cleanup_staged_writes(staged_writes)
        raise


def _metadata_for_name(staged: _StagedWrite, name: str) -> os.stat_result | None:
    if staged.parent_fd is None:
        return _lstat_optional(staged.write.destination.parent / name)
    return _metadata_at(staged.parent_fd, name)


def _read_named_payload(staged: _StagedWrite, name: str) -> tuple[os.stat_result, bytes]:
    path = staged.write.destination.parent / name
    metadata = _metadata_for_name(staged, name)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"staged payload {path} changed after staging")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        if staged.parent_fd is None:
            fd = os.open(path, flags)
        else:
            fd = os.open(name, flags, dir_fd=staged.parent_fd)
    except OSError as exc:
        raise ValueError(f"staged payload {path} changed after staging") from exc

    opened_metadata = os.fstat(fd)
    if not stat.S_ISREG(opened_metadata.st_mode) or _identity(opened_metadata) != _identity(
        metadata
    ):
        os.close(fd)
        raise ValueError(f"staged payload {path} changed after staging")
    with os.fdopen(fd, "rb") as handle:
        content = handle.read()
    return opened_metadata, content


def _validate_staged_payload(staged: _StagedWrite, name: str) -> None:
    metadata, content = _read_named_payload(staged, name)
    if (
        _identity(metadata) != staged.state.identity
        or stat.S_IMODE(metadata.st_mode) != staged.state.mode
        or content != staged.write.content
    ):
        path = staged.write.destination.parent / name
        raise ValueError(f"staged payload {path} changed after staging")


def _validate_recovery_payload(
    staged: _StagedWrite,
    name: str,
    expected: _DestinationState,
) -> None:
    path = staged.write.destination.parent / name
    try:
        metadata, content = _read_named_payload(staged, name)
    except (OSError, ValueError) as exc:
        raise ValueError(f"recovery payload {path} changed after staging") from exc
    if (
        _identity(metadata) != expected.identity
        or stat.S_IMODE(metadata.st_mode) != expected.mode
        or hashlib.sha256(content).digest() != expected.digest
    ):
        raise ValueError(f"recovery payload {path} changed after staging")


def _revalidate_staged_writes(
    prepared: _PreparedEntry,
    directories: dict[Path, _OpenedDirectory],
    staged_writes: list[_StagedWrite],
) -> None:
    for index, parent_state in enumerate(prepared.parent_states):
        opened = directories[parent_state.path]
        if index == 0:
            metadata = _lstat_optional(parent_state.path)
        else:
            metadata = _metadata_at(
                directories[parent_state.path.parent].fd, parent_state.path.name
            )
        if metadata is None:
            raise ValueError(f"destination parent {parent_state.path} changed after preflight")
        _validate_parent_metadata(parent_state.path, metadata)
        if _identity(metadata) != opened.identity:
            raise ValueError(f"destination parent {parent_state.path} changed after preflight")

    for write in prepared.writes:
        parent_fd = directories[write.destination.parent].fd
        _validate_expected_destination(write, _metadata_at(parent_fd, write.destination.name))
    for staged in staged_writes:
        _validate_staged_payload(staged, staged.temporary_name)


def _replace_name(staged: _StagedWrite, source_name: str, destination_name: str) -> None:
    if staged.parent_fd is None:
        parent = staged.write.destination.parent
        os.replace(parent / source_name, parent / destination_name)
        return
    os.rename(
        source_name,
        destination_name,
        src_dir_fd=staged.parent_fd,
        dst_dir_fd=staged.parent_fd,
    )


def _unlink_name(staged: _StagedWrite, name: str) -> None:
    if staged.parent_fd is None:
        (staged.write.destination.parent / name).unlink()
        return
    os.unlink(name, dir_fd=staged.parent_fd)


def _open_exclusive_name(staged: _StagedWrite, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    if staged.parent_fd is None:
        return os.open(staged.write.destination.parent / name, flags, 0o600)
    return os.open(name, flags, 0o600, dir_fd=staged.parent_fd)


def _chmod_name(staged: _StagedWrite, name: str, mode: int) -> None:
    if staged.parent_fd is not None and os.chmod in os.supports_dir_fd:
        os.chmod(
            name,
            mode,
            dir_fd=staged.parent_fd,
            follow_symlinks=False,
        )
        return
    os.chmod(
        staged.write.destination.parent / name,
        mode,
        follow_symlinks=False,
    )


def _snapshot_state(staged: _StagedWrite, name: str) -> _DestinationState:
    metadata, content = _read_named_payload(staged, name)
    return _DestinationState(
        identity=_identity(metadata),
        mode=stat.S_IMODE(metadata.st_mode),
        link_count=metadata.st_nlink,
        digest=hashlib.sha256(content).digest(),
        content=content,
    )


def _create_recovery_snapshot(staged: _StagedWrite) -> _RecoverySnapshot | None:
    destination_name = staged.write.destination.name
    expected = staged.write.state
    if expected is None:
        return None
    current = _metadata_for_name(staged, destination_name)
    _validate_expected_destination(staged.write, current)
    try:
        _validate_recovery_payload(staged, destination_name, expected)
    except ValueError as exc:
        raise ValueError(f"destination {staged.write.destination} changed before commit") from exc

    recovery_name = f".{destination_name}.{secrets.token_hex(8)}.recovery"
    try:
        recovery_fd = _open_exclusive_name(staged, recovery_name)
        recovery_write = _PreparedWrite(
            destination=staged.write.destination,
            content=expected.content,
            mode=expected.mode,
            state=None,
        )
        state = _write_staged_payload(recovery_fd, recovery_write)
        if state.mode != expected.mode:
            _chmod_name(staged, recovery_name, expected.mode)
            state = _snapshot_state(staged, recovery_name)
        if state.mode != expected.mode or state.digest != expected.digest:
            raise ValueError(f"recovery snapshot {recovery_name} changed while creating")
        snapshot = _RecoverySnapshot(recovery_name, state)
        _validate_recovery_payload(staged, recovery_name, snapshot.state)
        return snapshot
    except BaseException:
        try:
            _unlink_name(staged, recovery_name)
        except OSError:
            pass
        raise


def _remove_name_if_present(staged: _StagedWrite, name: str) -> bool:
    metadata = _metadata_for_name(staged, name)
    if metadata is None:
        return True
    try:
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            if staged.parent_fd is None:
                (staged.write.destination.parent / name).rmdir()
            else:
                os.rmdir(name, dir_fd=staged.parent_fd)
        else:
            _unlink_name(staged, name)
    except OSError:
        pass
    try:
        return _metadata_for_name(staged, name) is None
    except OSError:
        return False


def _require_canonical_absent(staged: _StagedWrite) -> None:
    destination_name = staged.write.destination.name
    if not _remove_name_if_present(staged, destination_name):
        raise RuntimeError(
            f"CRITICAL: untrusted canonical content remains at {staged.write.destination}"
        )


def _recover_failed_commit(
    staged: _StagedWrite,
    recovery: _RecoverySnapshot | None,
) -> None:
    destination_name = staged.write.destination.name
    if recovery is None:
        _require_canonical_absent(staged)
        return

    try:
        _validate_recovery_payload(staged, recovery.name, recovery.state)
    except ValueError:
        _require_canonical_absent(staged)
        raise

    try:
        _replace_name(staged, recovery.name, destination_name)
    except BaseException:
        if _metadata_for_name(staged, recovery.name) is None:
            try:
                _validate_recovery_payload(staged, destination_name, recovery.state)
            except ValueError:
                _require_canonical_absent(staged)
                raise
            return
        _require_canonical_absent(staged)
        _replace_name(staged, recovery.name, destination_name)

    try:
        _validate_recovery_payload(staged, destination_name, recovery.state)
    except ValueError:
        _require_canonical_absent(staged)
        raise


def _commit_staged_write(staged: _StagedWrite) -> None:
    _validate_staged_payload(staged, staged.temporary_name)
    recovery = _create_recovery_snapshot(staged)
    try:
        _replace_name(staged, staged.temporary_name, staged.write.destination.name)
        _validate_staged_payload(staged, staged.write.destination.name)
    except BaseException:
        try:
            _recover_failed_commit(staged, recovery)
        except BaseException as recovery_error:
            if recovery is not None and _metadata_for_name(staged, recovery.name) is not None:
                recovery_path = staged.write.destination.parent / recovery.name
                try:
                    _validate_recovery_payload(staged, recovery.name, recovery.state)
                except ValueError:
                    raise RuntimeError(
                        f"failed to restore destination {staged.write.destination}; "
                        f"untrusted recovery payload quarantined at {recovery_path}; "
                        f"recovery error: {recovery_error}"
                    ) from recovery_error
                raise RuntimeError(
                    f"failed to restore destination {staged.write.destination}; "
                    f"prior destination preserved at {recovery_path}; "
                    f"recovery error: {recovery_error}"
                ) from recovery_error
            raise
        raise
    else:
        if recovery is not None:
            try:
                _unlink_name(staged, recovery.name)
            except OSError:
                pass


def _commit_staged_writes(staged_writes: list[_StagedWrite]) -> None:
    for staged in staged_writes:
        _commit_staged_write(staged)


def _cleanup_staged_writes(staged_writes: list[_StagedWrite]) -> None:
    for staged in staged_writes:
        try:
            if staged.parent_fd is None:
                staged.temporary_path.unlink()
            else:
                os.unlink(staged.temporary_name, dir_fd=staged.parent_fd)
        except OSError:
            pass


def _write_via_directory_fds(prepared: _PreparedEntry) -> None:
    directories = _open_parent_directories(prepared)
    staged_writes: list[_StagedWrite] = []
    try:
        staged_writes = _stage_writes(prepared, directories)
        _revalidate_staged_writes(prepared, directories, staged_writes)
        _commit_staged_writes(staged_writes)
        for directory in directories.values():
            try:
                os.fsync(directory.fd)
            except OSError:
                pass
    finally:
        _cleanup_staged_writes(staged_writes)
        for directory in reversed(directories.values()):
            os.close(directory.fd)


def _supports_windows_path_guards() -> bool:
    return os.name == "nt"


def _open_windows_directory_guard(path: Path) -> int:  # pragma: no cover - Windows only
    import ctypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80,  # FILE_READ_ATTRIBUTES
        0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deliberately no DELETE share
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        error = getattr(ctypes, "get_last_error")()
        message = getattr(ctypes, "FormatError")(error)
        raise OSError(error, f"cannot guard directory {path}: {message}")
    return int(handle)


def _close_windows_directory_guard(handle: int) -> None:  # pragma: no cover - Windows only
    import ctypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))


def _read_source_fd(
    fd: int,
    source: Path,
    *,
    field: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[bytes, int]:
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{field}: {source} is not a regular file")
        if expected_identity is not None and _identity(metadata) != expected_identity:
            raise ValueError(f"{field}: {source} changed while opening")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        return handle.read(), stat.S_IMODE(metadata.st_mode)


def _read_source_via_directory_fds(
    root: Path,
    relative: Path,
    source: Path,
    *,
    field: str,
) -> tuple[bytes, int]:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = (
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    )
    current_fd = os.open(root.anchor, directory_flags)
    try:
        for component in (*root.parts[1:], *relative.parts[:-1]):
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        fd = os.open(relative.name, file_flags, dir_fd=current_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"{field}: {source} changed or is not a regular file: {exc}") from None
    finally:
        os.close(current_fd)
    return _read_source_fd(fd, source, field=field)


def _source_parent_paths(root: Path, source: Path) -> list[Path]:
    try:
        relative_parent = source.parent.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"source {source} is outside import root {root}") from exc
    paths = [*reversed(root.parents), root]
    current = root
    for component in relative_parent.parts:
        current /= component
        paths.append(current)
    return paths


@contextmanager
def _guard_source_parents(root: Path, source: Path) -> Iterator[None]:
    handles: list[int] = []
    try:
        for path in _source_parent_paths(root, source):
            metadata = _lstat_optional(path)
            if metadata is None:
                raise FileNotFoundError(source)
            _validate_parent_metadata(path, metadata, label="source parent")
            identity = _identity(metadata)
            handles.append(_open_windows_directory_guard(path))
            guarded_metadata = _lstat_optional(path)
            if guarded_metadata is None or _identity(guarded_metadata) != identity:
                raise ValueError(f"source parent: {path} changed while acquiring guard")
        yield
    finally:
        for handle in reversed(handles):
            _close_windows_directory_guard(handle)


def _read_source_via_checked_paths(
    root: Path,
    source: Path,
    *,
    field: str,
) -> tuple[bytes, int]:
    with _guard_source_parents(root, source):
        metadata = _lstat_optional(source)
        if metadata is None:
            raise FileNotFoundError(source)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or _is_name_surrogate_reparse_point(metadata)
            or not stat.S_ISREG(metadata.st_mode)
        ):
            raise ValueError(f"{field}: {source} is not a regular file")
        try:
            fd = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except OSError as exc:
            raise ValueError(f"{field}: {source} changed while opening: {exc}") from None
        return _read_source_fd(fd, source, field=field, expected_identity=_identity(metadata))


def _read_source_payload(source: Path, *, field: str) -> tuple[bytes, int]:
    root = IMPORT_ROOT.resolve()
    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field}: {source} is outside import root {root}") from exc
    if _supports_directory_fds():
        return _read_source_via_directory_fds(root, relative, source, field=field)
    if _supports_windows_path_guards():
        return _read_source_via_checked_paths(root, source, field=field)
    raise RuntimeError(
        "secure source read unavailable: this platform must provide directory-relative "
        "filesystem operations or Windows directory handles"
    )


@contextmanager
def _guard_checked_parents(
    prepared: _PreparedEntry,
) -> Iterator[dict[Path, tuple[int, int]]]:
    """Pin fallback paths; platforms without native pinning fail closed."""
    if not _supports_windows_path_guards():
        raise RuntimeError(
            "secure checked-path fallback requires Windows directory handles; "
            "this platform must provide directory-relative filesystem operations"
        )

    target_dir = prepared.parent_states[0].path
    expected_states = {state.path: state.identity for state in prepared.target_chain}
    expected_states.update({state.path: state.identity for state in prepared.parent_states})
    guard_paths = [
        *reversed(target_dir.parents),
        *(state.path for state in prepared.parent_states),
    ]
    identities: dict[Path, tuple[int, int]] = {}
    handles: list[int] = []
    seen: set[Path] = set()
    try:
        for path in guard_paths:
            if path in seen:
                continue
            seen.add(path)
            metadata = _lstat_optional(path)
            expected = expected_states.get(path)
            if path in expected_states and expected is None:
                if metadata is not None:
                    raise ValueError(f"destination parent {path} changed after preflight")
                try:
                    path.mkdir(mode=0o755)
                except FileExistsError:
                    raise ValueError(f"destination parent {path} changed after preflight") from None
                metadata = _lstat_optional(path)
            if metadata is None:
                raise ValueError(f"destination parent {path} changed after preflight")
            _validate_parent_metadata(path, metadata)
            identity = _identity(metadata)
            if expected is not None and identity != expected:
                raise ValueError(f"destination parent {path} changed after preflight")

            handles.append(_open_windows_directory_guard(path))
            guarded_metadata = _lstat_optional(path)
            if guarded_metadata is None or _identity(guarded_metadata) != identity:
                raise ValueError(f"destination parent {path} changed while acquiring guard")
            if path in expected_states:
                identities[path] = identity
        yield identities
    finally:
        for handle in reversed(handles):
            _close_windows_directory_guard(handle)


def _write_via_checked_paths(prepared: _PreparedEntry) -> None:
    with _guard_checked_parents(prepared) as parent_identities:
        staged_writes: list[_StagedWrite] = []
        try:
            for write in prepared.writes:
                fd, temporary_path = tempfile.mkstemp(
                    prefix=f".{write.destination.name}.",
                    suffix=".tmp",
                    dir=write.destination.parent,
                )
                temporary_name = Path(temporary_path).name
                try:
                    state = _write_staged_payload(fd, write)
                except BaseException:
                    try:
                        Path(temporary_path).unlink()
                    except OSError:
                        pass
                    raise
                staged_writes.append(_StagedWrite(write, temporary_name, None, state))

            for parent_state in prepared.parent_states:
                metadata = _lstat_optional(parent_state.path)
                if metadata is None:
                    raise ValueError(
                        f"destination parent {parent_state.path} changed after preflight"
                    )
                _validate_parent_metadata(parent_state.path, metadata)
                if _identity(metadata) != parent_identities[parent_state.path]:
                    raise ValueError(
                        f"destination parent {parent_state.path} changed after preflight"
                    )
            for write in prepared.writes:
                _validate_expected_destination(write, _lstat_optional(write.destination))
            for staged in staged_writes:
                _validate_staged_payload(staged, staged.temporary_name)
            _commit_staged_writes(staged_writes)
        finally:
            _cleanup_staged_writes(staged_writes)


def _supports_directory_fds() -> bool:
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )


def _write_prepared_entry(prepared: _PreparedEntry) -> None:
    if not prepared.writes:
        return
    if _supports_directory_fds():
        _write_via_directory_fds(prepared)
    else:
        _write_via_checked_paths(prepared)


def _deploy_entry_with_status(
    entry: dict, manifest: dict, target_dir: Path, dry_run: bool
) -> tuple[Path, bool, list[Path], bool]:
    prepared = _prepare_entry(entry, manifest, target_dir)
    if not dry_run:
        _write_prepared_entry(prepared)
    return (
        prepared.destination,
        prepared.changed,
        list(prepared.support_paths),
        prepared.destination_existed,
    )


def _deploy_entry_at_root(
    entry: dict, manifest: dict, target_dir: Path, dry_run: bool
) -> tuple[Path, bool, list[Path]]:
    destination, changed, support_paths, _ = _deploy_entry_with_status(
        entry, manifest, target_dir, dry_run
    )
    return destination, changed, support_paths


def deploy_entry(
    entry: dict, manifest: dict, target_dir: Path, dry_run: bool
) -> tuple[Path, bool, list[Path]]:
    return _deploy_entry_at_root(entry, manifest, target_dir.resolve(), dry_run)


_DeployResult = tuple[Path, bool, list[Path]]


def _entry_label(entry: object) -> str:
    if isinstance(entry, dict):
        slug = entry.get("slug")
        if isinstance(slug, str) and slug:
            return repr(slug)
    return "<unnamed>"


def _format_entry_error(index: int, entry: object, error: BaseException) -> str:
    return f"entry {index} ({_entry_label(entry)}): {error}"


def _preflight_manifest(manifest: dict, target_dir: Path) -> list[tuple[dict, _DeployResult]]:
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest: expected object, got {type(manifest).__name__}")
    for field in ("upstream", "upstream_revision", "license"):
        _validate(f"manifest.{field}", manifest.get(field))

    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"manifest.entries: expected list, got {type(raw_entries).__name__}")
    target_root = target_dir.resolve()
    if target_root.exists() and not target_root.is_dir():
        raise ValueError(f"target: {target_root} is not a directory")

    results: list[tuple[dict, _DeployResult]] = []
    destination_entries: dict[Path, tuple[int, str]] = {}
    for index, raw_entry in enumerate(raw_entries, start=1):
        try:
            if not isinstance(raw_entry, dict):
                raise ValueError(f"expected object, got {type(raw_entry).__name__}")
            result = _deploy_entry_at_root(raw_entry, manifest, target_root, dry_run=True)
            destination_name = result[0].parent.name
            destination_key = result[0].parent.resolve()
            previous = destination_entries.get(destination_key)
            if previous is not None:
                previous_index, previous_label = previous
                raise ValueError(
                    f"duplicate destination {destination_name!r}; "
                    f"entry {previous_index} ({previous_label}) already uses it"
                )
            entry_label = _entry_label(raw_entry)
            destination_entries[destination_key] = (index, entry_label)
            results.append((raw_entry, result))
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            raise ValueError(_format_entry_error(index, raw_entry, exc)) from exc
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--install", action="store_true")
    mode_group.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--target",
        default=str(cfg.skills_dir),
        help=f"Target skills dir (default: {cfg.skills_dir})",
    )
    args = parser.parse_args()
    if not args.install and not args.dry_run:
        parser.error("Pass either --install or --dry-run")

    manifest = load_manifest()
    try:
        target_dir = Path(args.target).expanduser().resolve()
        preflight = _preflight_manifest(manifest, target_dir)
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    if args.install and not target_dir.exists():
        print(f"Creating target dir: {target_dir}")

    new = 0
    updated = 0
    unchanged = 0
    for index, (entry, preview) in enumerate(preflight, start=1):
        if args.dry_run:
            dest, changed, support = preview
            destination_existed = dest.exists()
        else:
            try:
                dest, changed, support, destination_existed = _deploy_entry_with_status(
                    entry, manifest, target_dir, dry_run=False
                )
            except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
                print(f"Import failed: {_format_entry_error(index, entry, exc)}", file=sys.stderr)
                raise SystemExit(1) from None
        if not changed:
            marker = "   "
            unchanged += 1
        elif destination_existed:
            marker = "UPD"
            updated += 1
        else:
            marker = "NEW"
            new += 1
        suffix = f"  (+{len(support)} support)" if support else ""
        print(f"  [{marker}] {dest.relative_to(target_dir.parent)}{suffix}")

    mode = "dry-run" if args.dry_run else "install"
    print()
    print(f"Mode: {mode}  target: {target_dir}")
    print(f"Entries: {len(preflight)}  new: {new}  updated: {updated}  unchanged: {unchanged}")
    if args.install:
        print()
        print("Next steps:")
        print(f"  ctx-catalog-builder --wiki {cfg.wiki_dir} --skills-dir {target_dir} \\")
        print(f"      --agents-dir {cfg.agents_dir}")
        print("  ctx-wiki-batch-entities --all")
        print("  ctx-wiki-graphify")


if __name__ == "__main__":
    main()
