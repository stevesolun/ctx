#!/usr/bin/env python3
"""import_strix_skills.py -- Deploy imported Strix skills into ~/.claude/skills.

Reads imported-skills/strix/MANIFEST.json and creates one skill directory per
entry in `cfg.skills_dir`, following the naming convention:

    <skills_dir>/strix-<category>-<slug>/SKILL.md

Each deployed SKILL.md prepends an attribution header so provenance remains
visible inline when the skill is loaded.

This script is idempotent. Re-running updates existing deployments in place.

Usage:
    python src/import_strix_skills.py --dry-run        # preview
    python src/import_strix_skills.py --install        # deploy to ~/.claude/skills
    python src/import_strix_skills.py --install \\
        --target ./custom-skills-dir                   # deploy elsewhere
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ctx_config import cfg

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORT_ROOT = REPO_ROOT / "imported-skills" / "strix"
MANIFEST_PATH = IMPORT_ROOT / "MANIFEST.json"

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return SLUG_RE.sub("-", name.lower()).strip("-")


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        print(f"Manifest not found: {MANIFEST_PATH}", file=sys.stderr)
        print("Run: python imported-skills/strix/build_manifest.py", file=sys.stderr)
        sys.exit(1)
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(
            f"Invalid manifest JSON: {MANIFEST_PATH}: "
            f"line {exc.lineno} column {exc.colno}: {exc.msg}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (OSError, UnicodeError) as exc:
        print(f"Unable to read manifest: {MANIFEST_PATH}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if not isinstance(manifest, dict):
        print(f"Invalid manifest: {MANIFEST_PATH}: expected a JSON object", file=sys.stderr)
        raise SystemExit(1)
    return manifest


def render_attribution_header(entry: dict, manifest: dict) -> str:
    upstream = _validate_attribution_value("manifest.upstream", manifest.get("upstream"))
    revision = _validate_attribution_value(
        "manifest.upstream_revision", manifest.get("upstream_revision")
    )
    license_name = _validate_attribution_value("manifest.license", manifest.get("license"))
    category = _validate_attribution_value(
        "category", entry.get("category"), regex=_SAFE_CATEGORY_RE
    )
    return (
        f"<!-- strix-import: upstream={upstream} "
        f"rev={revision[:12]} "
        f"license={license_name} category={category} -->\n"
    )


_SAFE_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class PreparedEntry:
    destination: Path
    target_root: Path
    canonical_destination: Path
    parent_identity: tuple[int, int] | None
    parent_is_symlink: bool
    destination_identity: tuple[int, int] | None
    destination_link_count: int
    content: str
    changed: bool
    existed: bool


def _validate_manifest_field(
    field: str, value: object, *, regex: re.Pattern[str] | None = None
) -> str:
    """Reject manifest values that could escape the intended trust boundary."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}: expected non-empty string, got {type(value).__name__}")
    if regex is not None and not regex.fullmatch(value):
        raise ValueError(f"{field}: {value!r} failed strict format check")
    return value


def _validate_attribution_value(
    field: str, value: object, *, regex: re.Pattern[str] | None = None
) -> str:
    text = _validate_manifest_field(field, value, regex=regex)
    if "\r" in text or "\n" in text or "-->" in text:
        raise ValueError(f"{field}: unsafe attribution value")
    return text


def _resolve_path(path: Path, *, field: str) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{field}: {path} could not be resolved: {exc}") from None


def _resolve_within(root: Path, candidate_rel: str, *, field: str) -> Path:
    """Join ``candidate_rel`` onto ``root`` and fail hard if the result escapes root.

    Strix finding vuln-0001 (Path Traversal in Strix Skill Import): the
    manifest's ``source_path`` was concatenated directly onto IMPORT_ROOT,
    so a crafted value like ``../../etc/passwd`` would be happily read
    and re-written into the target skills tree. Resolve both sides and
    enforce ``relative_to`` containment before we touch the filesystem.
    """
    if ".." in Path(candidate_rel).parts or candidate_rel.startswith(("/", "\\")):
        raise ValueError(f"{field}: path traversal denied in {candidate_rel!r}")
    resolved = _resolve_path(root / candidate_rel, field=field)
    root_resolved = _resolve_path(root, field=f"{field} root")
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{field}: {candidate_rel!r} resolves outside import root") from exc
    return resolved


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _supports_directory_fds() -> bool:
    supported = getattr(os, "supports_dir_fd", ())
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in supported
        and os.mkdir in supported
        and os.rename in supported
        and os.stat in supported
        and os.unlink in supported
    )


def _open_anchored_directory(path: Path, *, create: bool) -> int | None:
    absolute = Path(os.path.abspath(path))
    current_fd = os.open(absolute.anchor, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    os.close(current_fd)
                    return None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _read_source_text(source_rel: str) -> tuple[Path, str]:
    resolved_source = _resolve_within(IMPORT_ROOT, source_rel, field="source_path")
    trusted_root = _resolve_path(IMPORT_ROOT, field="import root")
    try:
        anchored_source_rel = resolved_source.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError("source_path: import root changed during validation") from exc
    source = trusted_root.joinpath(*anchored_source_rel.parts)

    if _supports_directory_fds():
        parent_fd = _open_anchored_directory(trusted_root, create=False)
        if parent_fd is None:
            raise FileNotFoundError(f"Source skill missing: {source}")
        try:
            for component in anchored_source_rel.parts[:-1]:
                next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            try:
                fd = os.open(anchored_source_rel.name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                raise FileNotFoundError(f"Source skill missing: {source}") from None
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"{source}: source is not a regular file")
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    return source, handle.read()
            except UnicodeError:
                raise ValueError(f"{source}: source is not valid UTF-8") from None
            finally:
                if fd != -1:
                    os.close(fd)
        except FileNotFoundError:
            raise FileNotFoundError(f"Source skill missing: {source}") from None
        except OSError as exc:
            raise ValueError(
                f"{source}: source path changed or is not a real file: {exc}"
            ) from None
        finally:
            os.close(parent_fd)

    if _supports_windows_path_guards():
        with _guard_windows_directories(trusted_root, source.parent, create_missing=False):
            source_metadata = _lstat_optional(source)
            if source_metadata is None:
                raise FileNotFoundError(f"Source skill missing: {source}")
            if (
                stat.S_ISLNK(source_metadata.st_mode)
                or _is_reparse_point(source, source_metadata)
                or not stat.S_ISREG(source_metadata.st_mode)
            ):
                raise ValueError(f"{source}: source is not a regular file")
            fd = os.open(source, os.O_RDONLY)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(
                    source_metadata, opened
                ):
                    raise ValueError(f"{source}: source path changed while opening")
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    return source, handle.read()
            except UnicodeError:
                raise ValueError(f"{source}: source is not valid UTF-8") from None
            finally:
                if fd != -1:
                    os.close(fd)

    raise RuntimeError(
        "secure source read unavailable: this platform must provide directory-relative "
        "filesystem operations or Windows directory handles"
    )


def _read_preflight_destination(
    target_root: Path,
    destination: Path,
) -> tuple[tuple[int, int] | None, os.stat_result | None, str | None]:
    if _supports_directory_fds():
        target_fd = _open_anchored_directory(target_root, create=False)
        if target_fd is None:
            return None, None, None
        parent_fd: int | None = None
        try:
            try:
                parent_metadata = os.stat(
                    destination.parent.name,
                    dir_fd=target_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None, None, None
            if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
                raise ValueError(f"skill dir {destination.parent} must be a real directory")
            parent_fd = os.open(
                destination.parent.name,
                _DIRECTORY_OPEN_FLAGS,
                dir_fd=target_fd,
            )
            if not os.path.samestat(parent_metadata, os.fstat(parent_fd)):
                raise ValueError(f"skill dir {destination.parent} changed while opening")

            metadata = _destination_state_at(parent_fd, destination.name)
            if metadata is None:
                return _identity(parent_metadata), None, None
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"destination {destination} must be a regular file")
            fd = os.open(destination.name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(metadata, opened):
                    raise ValueError(f"destination {destination} changed while opening")
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    return _identity(parent_metadata), metadata, handle.read()
            finally:
                if fd != -1:
                    os.close(fd)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            os.close(target_fd)

    if _supports_windows_path_guards():
        if not _validate_real_directory(target_root, label="target directory"):
            return None, None, None
        parent_metadata = _lstat_optional(destination.parent)
        if parent_metadata is None:
            return None, None, None
        with _guard_windows_directories(
            target_root,
            destination.parent,
            create_missing=False,
        ):
            guarded_parent = destination.parent.stat(follow_symlinks=False)
            if not os.path.samestat(parent_metadata, guarded_parent):
                raise ValueError(f"skill dir {destination.parent} changed while opening")
            metadata = _lstat_optional(destination)
            if metadata is None:
                return _identity(parent_metadata), None, None
            if (
                stat.S_ISLNK(metadata.st_mode)
                or _is_reparse_point(destination, metadata)
                or not stat.S_ISREG(metadata.st_mode)
            ):
                raise ValueError(f"destination {destination} must be a regular file")
            fd = os.open(destination, os.O_RDONLY)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(metadata, opened):
                    raise ValueError(f"destination {destination} changed while opening")
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    return _identity(parent_metadata), metadata, handle.read()
            finally:
                if fd != -1:
                    os.close(fd)

    raise RuntimeError(
        "secure destination read unavailable: this platform must provide "
        "directory-relative filesystem operations or Windows directory handles"
    )


def _prepare_entry(entry: dict, manifest: dict, target_dir: Path) -> PreparedEntry:
    # Manifest fields are untrusted input (the repo's imported-skills/
    # MANIFEST.json is checked-in today, but the path from parsing to
    # filesystem write must still be defensible). Validate category
    # against a strict allowlist, contain source_path inside IMPORT_ROOT.
    category = _validate_manifest_field("category", entry.get("category"), regex=_SAFE_CATEGORY_RE)
    source_path_raw = _validate_manifest_field("source_path", entry.get("source_path"))
    _, body = _read_source_text(source_path_raw)

    name = _validate_manifest_field("name", entry.get("name"))
    name_parts = name.replace("\\", "/").split("/")
    if ".." in name_parts or name.startswith(("/", "\\")):
        raise ValueError(f"name: path traversal denied in {name!r}")
    name_slug = slugify(name)
    if not name_slug:
        raise ValueError(f"name: {name!r} does not produce a valid slug")

    dir_name = f"strix-{category}-{name_slug}"
    skill_dir = target_dir / dir_name
    # Resolve both the directory and final file so existing symlinks cannot
    # redirect an install outside target_dir.
    target_resolved = _resolve_path(target_dir, field="target directory")
    dest_resolved = _resolve_path(skill_dir, field="skill directory")
    try:
        dest_resolved.relative_to(target_resolved)
    except ValueError as exc:
        raise ValueError(f"skill dir {skill_dir} resolves outside target_dir") from exc
    dest = skill_dir / "SKILL.md"
    try:
        canonical_destination = _resolve_path(dest, field="destination")
        canonical_destination.relative_to(target_resolved)
    except ValueError as exc:
        raise ValueError(f"destination {dest} resolves outside target_dir") from exc

    if dest.is_symlink():
        raise ValueError(f"destination {dest} must not be a symlink")

    header = render_attribution_header(entry, manifest)
    if body.startswith("<!-- strix-import:"):
        body = body.split("-->", 1)[1].lstrip("\n")
    content = header + body

    parent_is_symlink = skill_dir.is_symlink()
    parent_identity, destination_metadata, existing = _read_preflight_destination(
        target_resolved,
        dest,
    )
    existed = destination_metadata is not None
    destination_identity = None if destination_metadata is None else _identity(destination_metadata)
    destination_link_count = 0 if destination_metadata is None else destination_metadata.st_nlink
    changed = existing != content

    return PreparedEntry(
        destination=dest,
        target_root=target_resolved,
        canonical_destination=canonical_destination,
        parent_identity=parent_identity,
        parent_is_symlink=parent_is_symlink,
        destination_identity=destination_identity,
        destination_link_count=destination_link_count,
        content=content,
        changed=changed,
        existed=existed,
    )


def _destination_state_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validate_write_state(prepared: PreparedEntry, metadata: os.stat_result | None) -> None:
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(
            prepared.canonical_destination, metadata
        ):
            raise ValueError(f"destination {prepared.destination} must not be a symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"destination {prepared.destination} must be a regular file")
        if metadata.st_nlink > 1:
            raise ValueError(f"destination {prepared.destination} is hard-linked")
    current_identity = None if metadata is None else _identity(metadata)
    if current_identity != prepared.destination_identity:
        raise ValueError(f"destination {prepared.destination} changed after preflight")


def _read_destination_at(parent_fd: int, prepared: PreparedEntry) -> str | None:
    metadata = _destination_state_at(parent_fd, prepared.destination.name)
    _validate_write_state(prepared, metadata)
    if metadata is None:
        return None
    fd = os.open(prepared.destination.name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(metadata, opened):
            raise ValueError(f"destination {prepared.destination} changed while opening")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd != -1:
            os.close(fd)


def _write_via_directory_fd(prepared: PreparedEntry) -> tuple[bool, bool]:
    target_fd = _open_anchored_directory(prepared.target_root, create=True)
    if target_fd is None:
        raise RuntimeError(f"could not create target directory {prepared.target_root}")
    parent_fd: int | None = None
    temp_name: str | None = None
    try:
        parent_name = prepared.destination.parent.name
        created_parent = False
        try:
            os.mkdir(parent_name, mode=0o700, dir_fd=target_fd)
            created_parent = True
        except FileExistsError:
            pass

        parent_fd = os.open(parent_name, _DIRECTORY_OPEN_FLAGS, dir_fd=target_fd)
        current_parent_identity = _identity(os.fstat(parent_fd))
        if created_parent:
            if prepared.parent_identity is not None:
                raise ValueError(f"skill dir {prepared.destination.parent} changed after preflight")
        elif current_parent_identity != prepared.parent_identity:
            raise ValueError(f"skill dir {prepared.destination.parent} changed after preflight")

        existing = _read_destination_at(parent_fd, prepared)
        existed = existing is not None
        if existing == prepared.content:
            _validate_write_state(
                prepared,
                _destination_state_at(parent_fd, prepared.destination.name),
            )
            return False, existed

        temp_name = f".{prepared.destination.name}.{secrets.token_hex(8)}.tmp"
        temp_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        temp_fd = os.open(temp_name, temp_flags, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                handle.write(prepared.content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(temp_fd)
            except OSError:
                pass
            raise

        _validate_write_state(
            prepared,
            _destination_state_at(parent_fd, prepared.destination.name),
        )
        os.rename(
            temp_name,
            prepared.destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_name = None
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        return True, existed
    finally:
        if temp_name is not None and parent_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(target_fd)


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None


def _is_reparse_point(path: Path, metadata: os.stat_result) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(getattr(metadata, "st_file_attributes", 0) & 0x400) or (
        callable(is_junction) and is_junction()
    )


def _validate_real_directory(path: Path, *, label: str) -> bool:
    try:
        metadata = _lstat_optional(path)
    except OSError as exc:
        raise ValueError(f"{label} {path} must be a real directory: {exc}") from None
    if metadata is None:
        return False
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(path, metadata)
    ):
        raise ValueError(f"{label} {path} must be a real directory")
    return True


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
        0x80,
        0x1 | 0x2,
        None,
        3,
        0x02000000 | 0x00200000,
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


def _windows_guard_paths(target_dir: Path, skill_dir: Path) -> list[Path]:
    absolute_target = Path(os.path.abspath(target_dir))
    paths = [Path(absolute_target.anchor)]
    for component in absolute_target.parts[1:]:
        paths.append(paths[-1] / component)
    try:
        relative_skill = skill_dir.relative_to(absolute_target)
    except ValueError as exc:
        raise ValueError(f"guarded directory {skill_dir} is outside {absolute_target}") from exc
    for component in relative_skill.parts:
        paths.append(paths[-1] / component)
    return paths


@contextmanager
def _guard_windows_directories(
    target_dir: Path,
    skill_dir: Path,
    *,
    create_missing: bool = True,
) -> Iterator[None]:
    handles: list[int] = []
    try:
        for path in _windows_guard_paths(target_dir, skill_dir):
            if not _validate_real_directory(path, label="target directory"):
                if not create_missing:
                    raise ValueError(f"target directory {path} must be a real directory")
                path.mkdir()
                _validate_real_directory(path, label="target directory")
            handles.append(_open_windows_directory_guard(path))
            _validate_real_directory(path, label="target directory")
        yield
    finally:
        for handle in reversed(handles):
            _close_windows_directory_guard(handle)


def _read_destination_path(prepared: PreparedEntry) -> str | None:
    destination = prepared.canonical_destination
    metadata = _lstat_optional(destination)
    _validate_write_state(prepared, metadata)
    if metadata is None:
        return None
    fd = os.open(destination, os.O_RDONLY)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(metadata, opened):
            raise ValueError(f"destination {prepared.destination} changed while opening")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read()
    finally:
        if fd != -1:
            os.close(fd)


def _write_via_checked_paths(prepared: PreparedEntry) -> tuple[bool, bool]:
    destination = prepared.canonical_destination
    parent = destination.parent
    with _guard_windows_directories(prepared.target_root, parent):
        current_parent_identity = _identity(parent.stat(follow_symlinks=False))
        if (
            prepared.parent_identity is not None
            and current_parent_identity != prepared.parent_identity
        ):
            raise ValueError(f"skill dir {prepared.destination.parent} changed after preflight")

        existing = _read_destination_path(prepared)
        existed = existing is not None
        if existing == prepared.content:
            _validate_write_state(prepared, _lstat_optional(destination))
            return False, existed

        fd, temp_path = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(prepared.content)
                handle.flush()
                os.fsync(handle.fileno())
            _validate_write_state(prepared, _lstat_optional(destination))
            os.replace(temp_path, destination)
            temp_path = ""
            return True, existed
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass


def _write_prepared_entry(prepared: PreparedEntry) -> tuple[bool, bool]:
    if prepared.parent_is_symlink or prepared.destination.parent.is_symlink():
        raise ValueError(f"skill dir {prepared.destination.parent} is a symlink")
    if _supports_directory_fds():
        return _write_via_directory_fd(prepared)
    if _supports_windows_path_guards():
        return _write_via_checked_paths(prepared)
    raise RuntimeError(
        "secure install unavailable: this platform must provide directory-relative "
        "filesystem operations or Windows directory handles"
    )


def _deploy_entry_with_status(
    entry: dict, manifest: dict, target_dir: Path, dry_run: bool
) -> tuple[Path, bool, bool]:
    prepared = _prepare_entry(entry, manifest, target_dir)
    if dry_run:
        changed, existed = prepared.changed, prepared.existed
    else:
        changed, existed = _write_prepared_entry(prepared)
    return prepared.destination, changed, existed


def _entry_label(index: int, entry: object) -> str:
    if isinstance(entry, dict) and "name" in entry:
        return f"entry {index} ({entry['name']!r})"
    return f"entry {index} (<unnamed>)"


def _preflight_manifest(manifest: dict, target_dir: Path) -> list[PreparedEntry]:
    for field in ("upstream", "upstream_revision", "license"):
        _validate_manifest_field(f"manifest.{field}", manifest.get(field))

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("manifest.entries: expected a list")

    prepared_entries: list[PreparedEntry] = []
    seen_slugs: dict[str, str] = {}
    seen_destinations: dict[str, str] = {}
    seen_inodes: dict[tuple[int, int], str] = {}
    labels: list[str] = []
    for index, raw_entry in enumerate(entries, start=1):
        label = _entry_label(index, raw_entry)
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{label}: expected an object, got {type(raw_entry).__name__}")
        try:
            prepared = _prepare_entry(raw_entry, manifest, target_dir)
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            raise ValueError(f"{label}: {exc}") from exc

        destination_slug = prepared.destination.parent.name
        previous_label = seen_slugs.get(destination_slug)
        if previous_label is not None:
            raise ValueError(
                f"{label}: duplicate destination slug {destination_slug!r}; "
                f"already used by {previous_label}"
            )
        seen_slugs[destination_slug] = label

        canonical_key = os.path.normcase(str(prepared.canonical_destination))
        previous_label = seen_destinations.get(canonical_key)
        if previous_label is not None:
            raise ValueError(
                f"{label}: duplicate canonical destination "
                f"{str(prepared.canonical_destination)!r}; already used by {previous_label}"
            )
        seen_destinations[canonical_key] = label

        if prepared.destination_identity is not None:
            previous_label = seen_inodes.get(prepared.destination_identity)
            if previous_label is not None:
                raise ValueError(
                    f"{label}: duplicate destination inode; already used by {previous_label}"
                )
            seen_inodes[prepared.destination_identity] = label

        prepared_entries.append(prepared)
        labels.append(label)

    for label, prepared in zip(labels, prepared_entries, strict=True):
        if prepared.parent_is_symlink:
            raise ValueError(f"{label}: skill dir {prepared.destination.parent} is a symlink")
        if prepared.destination_link_count > 1:
            raise ValueError(f"{label}: destination {prepared.destination} is hard-linked")

    return prepared_entries


def deploy_entry(entry: dict, manifest: dict, target_dir: Path, dry_run: bool) -> tuple[Path, bool]:
    dest, changed, _ = _deploy_entry_with_status(entry, manifest, target_dir, dry_run)
    return dest, changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Write to target dir")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument(
        "--target",
        default=str(cfg.skills_dir),
        help=f"Target skills dir (default: {cfg.skills_dir})",
    )
    args = parser.parse_args()

    if args.install and args.dry_run:
        parser.error("Pass only one of --install or --dry-run")
    if not args.install and not args.dry_run:
        parser.error("Pass either --install or --dry-run")

    manifest = load_manifest()
    try:
        target_dir = Path(args.target).expanduser()
        prepared_entries = _preflight_manifest(manifest, target_dir)

        if args.install and not target_dir.exists():
            print(f"Creating target dir: {target_dir}")

        created = updated = unchanged = 0
        for prepared in prepared_entries:
            if args.install:
                changed, existed = _write_prepared_entry(prepared)
            else:
                changed, existed = prepared.changed, prepared.existed
            dest = prepared.destination
            if changed:
                if existed:
                    updated += 1
                    marker = "UPD"
                else:
                    created += 1
                    marker = "NEW"
            else:
                unchanged += 1
                marker = "   "
            print(f"  [{marker}] {dest.relative_to(target_dir.parent)}")
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    mode = "dry-run" if args.dry_run else "install"
    print()
    print(f"Mode: {mode}  target: {target_dir}")
    print(
        f"Entries: {len(prepared_entries)}  new/updated: {created + updated}  unchanged: {unchanged}"
    )

    if args.install:
        print()
        print("Next steps:")
        print(f"  python src/catalog_builder.py --wiki {cfg.wiki_dir} --skills-dir {target_dir} \\")
        print(f"      --agents-dir {cfg.agents_dir}")
        print("  python src/wiki_batch_entities.py --all")
        print("  python -m ctx.core.wiki.wiki_graphify")


if __name__ == "__main__":
    main()
