#!/usr/bin/env python3
"""import_designdotmd_skills.py -- Deploy designdotmd.directory designs as skills.

Reads imported-skills/designdotmd/MANIFEST.json. Each entry creates
``~/.claude/skills/designdotmd-<slug>/SKILL.md`` with:

  * The upstream YAML frontmatter (name, description, colors, typography,
    spacing, components, etc.) preserved, with invalid plain description
    scalars quoted for YAML compatibility.
  * A ``tags:`` field injected if missing (the upstream .md doesn't carry
    tags, but the listing API does — they're loaded from MANIFEST.json).
  * An attribution HTML comment prepended above the frontmatter so the
    upstream URL is visible inline.

Idempotent. Re-running updates existing deployments in place.

Usage:
    python src/import_designdotmd_skills.py --dry-run
    python src/import_designdotmd_skills.py --install
    python src/import_designdotmd_skills.py --install --target ./custom-skills-dir
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Iterator, NoReturn

import yaml  # type: ignore[import-untyped]

from ctx.core.source_registry import (
    ExternalSourceRecord,
    entity_provenance,
    get_external_source,
    validate_ingestion_manifest,
)
from ctx_config import cfg

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORT_ROOT = REPO_ROOT / "imported-skills" / "designdotmd"
MANIFEST_PATH = IMPORT_ROOT / "MANIFEST.json"
DESIGNDOTMD_SOURCE = get_external_source("designdotmd")

_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def load_manifest() -> dict:
    try:
        raw_manifest = MANIFEST_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"Manifest not found: {MANIFEST_PATH}", file=sys.stderr)
        print("Run: python imported-skills/designdotmd/build_manifest.py", file=sys.stderr)
        sys.exit(1)
    return json.loads(raw_manifest)


def _validate(field: str, value: object, *, regex: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}: expected non-empty string, got {type(value).__name__}")
    if regex is not None and not regex.fullmatch(value):
        raise ValueError(f"{field}: {value!r} failed strict format check")
    return value


def _validate_sha256(value: object) -> str:
    return _validate("sha256", value, regex=_SHA256_RE).lower()


def _resolve_within(root: Path, candidate_rel: str, *, field: str) -> Path:
    if ".." in Path(candidate_rel).parts or candidate_rel.startswith(("/", "\\")):
        raise ValueError(f"{field}: path traversal denied in {candidate_rel!r}")
    resolved = (root / candidate_rel).resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{field}: {candidate_rel!r} resolves outside import root") from exc
    return resolved


def _render_attribution(manifest: dict, entry: dict) -> str:
    def value(field: str, raw: object) -> str:
        text = _validate(field, raw)
        if "\r" in text or "\n" in text or "-->" in text:
            raise ValueError(f"{field}: unsafe attribution value")
        return text

    upstream = value("manifest.upstream", manifest.get("upstream"))
    slug = value("slug", entry.get("slug"))
    fetched_on = value("manifest.fetched_on", manifest.get("fetched_on"))
    author = value("author", entry.get("author", "?"))
    return (
        f"<!-- designdotmd-import: upstream={upstream} "
        f"id={slug} fetched={fetched_on} author={author} -->\n"
    )


_FM_OPEN_RE = re.compile(r"\A---[^\S\r\n]*\r?\n(.*?)\r?\n---(?=\r?\n|\Z)", re.DOTALL)
_DESCRIPTION_RE = re.compile(r"^description\s*:")


def _has_top_level_tags(fm_text: str) -> bool:
    """True if the frontmatter has a ``tags:`` line at the top level
    (not nested under typography or other keys).

    The upstream design files are richly indented YAML; we only need to
    avoid double-injecting the tags block. Top-level keys are unindented.
    """
    for raw in fm_text.splitlines():
        if raw.startswith("tags:") or raw.startswith("tags ") or raw == "tags":
            return True
    return False


def _inject_tags(text: str, tags: list[str]) -> str:
    """Insert a ``tags: [...]`` line into the YAML frontmatter.

    Inserts after the complete ``description`` YAML node when present
    (including block scalars and indentless sequences); otherwise appends
    just before the closing ``---``.

    No-op when the frontmatter already has top-level tags.
    """
    if not tags:
        return text
    m = _FM_OPEN_RE.match(text)
    if not m:
        return text
    fm = m.group(1)
    if _has_top_level_tags(fm):
        return text

    tags_line = "tags: [" + ", ".join(json.dumps(tag, ensure_ascii=False) for tag in tags) + "]"
    insert_at = len(fm)
    try:
        frontmatter = yaml.compose(fm)
    except yaml.YAMLError:
        lines = fm.splitlines()
        insert_line = len(lines)
        for index, raw in enumerate(lines):
            if _DESCRIPTION_RE.match(raw):
                insert_line = index + 1
                while insert_line < len(lines) and (
                    not lines[insert_line] or lines[insert_line][0].isspace()
                ):
                    insert_line += 1
                break
        new_fm = "\n".join(lines[:insert_line] + [tags_line] + lines[insert_line:])
        return text[: m.start(1)] + new_fm + text[m.end(1) :]
    if not isinstance(frontmatter, yaml.MappingNode):
        return text
    for key_node, value_node in frontmatter.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == "description":
            insert_at = value_node.end_mark.index
            if value_node.end_mark.column != 0:
                line_end = fm.find("\n", insert_at)
                insert_at = len(fm) if line_end == -1 else line_end + 1
            break

    newline = "\r\n" if "\r\n" in fm else "\n"
    prefix = "" if insert_at == 0 or fm[insert_at - 1] in "\r\n" else newline
    suffix = "" if insert_at == len(fm) or fm[insert_at] in "\r\n" else newline
    new_fm = fm[:insert_at] + prefix + tags_line + suffix + fm[insert_at:]
    return text[: m.start(1)] + new_fm + text[m.end(1) :]


def _inject_provenance(
    text: str,
    provenance: dict[str, object],
) -> str:
    """Add validated provenance fields without accepting upstream overrides."""

    match = _FM_OPEN_RE.match(text)
    if not match:
        return text
    try:
        frontmatter_node = yaml.compose(match.group(1))
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return text
    if not isinstance(frontmatter_node, yaml.MappingNode) or not isinstance(frontmatter, dict):
        return text

    provenance_keys: set[str] = set()
    for key_node, _value_node in frontmatter_node.value:
        if not isinstance(key_node, yaml.ScalarNode) or key_node.value not in provenance:
            continue
        if key_node.value in provenance_keys:
            raise ValueError(f"upstream frontmatter duplicates {key_node.value!r} provenance")
        provenance_keys.add(key_node.value)

    additions: dict[str, object] = {}
    for field, expected in provenance.items():
        if field in frontmatter:
            if frontmatter[field] != expected:
                raise ValueError(f"upstream frontmatter conflicts with {field!r} provenance")
            continue
        additions[field] = expected
    if not additions:
        return text

    rendered = yaml.safe_dump(
        additions,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip("\n")
    newline = "\r\n" if "\r\n" in match.group(0) else "\n"
    new_frontmatter = rendered + newline + match.group(1)
    return text[: match.start(1)] + new_frontmatter + text[match.end(1) :]


def _strip_prior_attribution(body: str, *, source: Path) -> str:
    if not body.startswith("<!-- designdotmd-import:"):
        return body

    first_line, separator, remainder = body.partition("\n")
    comment_end = first_line.find("-->")
    if comment_end == -1 or first_line[comment_end + 3 :].strip():
        raise ValueError(f"{source}: malformed designdotmd attribution comment")
    return remainder if separator else ""


def _quote_invalid_description_scalar(text: str) -> str:
    """Quote the upstream corpus' YAML-invalid plain descriptions.

    The API snapshots use unquoted, single-line descriptions whose values
    sometimes contain ``: ``. YAML treats that token as a mapping separator.
    Valid YAML is returned byte-for-byte. Repair is limited to the checked-in
    corpus shape when PyYAML points at that line's second colon; all other
    malformed YAML still reaches validation unchanged.
    """
    match = _FM_OPEN_RE.match(text)
    if not match:
        return text

    try:
        yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None)
    else:
        return text

    if mark is None or problem != "mapping values are not allowed here":
        return text

    lines = match.group(1).splitlines()
    if mark.line >= len(lines):
        return text

    raw = lines[mark.line]
    if not _DESCRIPTION_RE.match(raw):
        return text

    key, separator, raw_value = raw.partition(":")
    value = raw_value.strip()
    invalid_colon = raw.find(": ", len(key) + len(separator))
    if not value or not value[0].isalnum() or invalid_colon == -1 or mark.column != invalid_colon:
        return text

    lines[mark.line] = f"{key}{separator} {json.dumps(value, ensure_ascii=False)}"
    frontmatter = "\n".join(lines)
    return text[: match.start(1)] + frontmatter + text[match.end(1) :]


def _validate_frontmatter(text: str, *, source: Path) -> None:
    match = _FM_OPEN_RE.match(text)
    if not match:
        raise ValueError(f"{source}: missing YAML frontmatter delimiters")

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        location = ""
        if mark is not None:
            location = f" at line {mark.line + 2}, column {mark.column + 1}"
        raise ValueError(f"{source}: invalid YAML frontmatter{location}: {problem}") from None

    if not isinstance(frontmatter, dict):
        raise ValueError(f"{source}: YAML frontmatter must be a mapping")


def _resolve_target_root(target_dir: Path) -> Path:
    try:
        return target_dir.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"target directory {target_dir} could not be resolved: {exc}") from None


def _require_target_containment(candidate: Path, target_dir: Path, *, label: str) -> None:
    try:
        relative = candidate.relative_to(target_dir)
    except ValueError as exc:
        raise ValueError(f"{label} resolves outside target_dir") from exc

    current = target_dir
    for component in relative.parts:
        current /= component
        try:
            metadata = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            break
        except NotADirectoryError:
            break
        if _is_reparse_point(current, metadata):
            raise ValueError(f"{label} resolves through a reparse point beneath target_dir")
        if not stat.S_ISLNK(metadata.st_mode):
            continue
        try:
            candidate.resolve().relative_to(target_dir)
        except (OSError, RuntimeError, ValueError):
            raise ValueError(f"{label} resolves outside target_dir") from None
        raise ValueError(f"{label} resolves through a symlink beneath target_dir")


_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _supports_directory_fds() -> bool:
    supported = getattr(os, "supports_dir_fd", ())
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in supported
        and os.mkdir in supported
        and os.stat in supported
        and os.unlink in supported
    )


def _open_anchored_directory(path: Path, *, create: bool) -> int | None:
    """Open ``path`` one lexical component at a time without following links."""
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
                    os.mkdir(component, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _verified_source_text(source: Path, raw: bytes, expected_sha256: str) -> str:
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if not secrets.compare_digest(actual_sha256, expected_sha256):
        raise ValueError(
            f"{source}: sha256 mismatch; manifest={expected_sha256}, actual={actual_sha256}",
        )
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise ValueError(f"{source}: source is not valid UTF-8") from None


def _read_source_text(source_rel: str, *, expected_sha256: str) -> tuple[Path, str]:
    _resolve_within(IMPORT_ROOT, source_rel, field="source_path")
    trusted_root = IMPORT_ROOT.resolve()
    source = trusted_root.joinpath(*Path(source_rel).parts)

    if _supports_directory_fds():
        parent_fd = _open_anchored_directory(trusted_root, create=False)
        if parent_fd is None:
            raise FileNotFoundError(f"Source design missing: {source}")
        try:
            for component in Path(source_rel).parts[:-1]:
                next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            try:
                fd = os.open(Path(source_rel).name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                raise FileNotFoundError(f"Source design missing: {source}") from None
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"{source}: source is not a regular file")
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    return source, _verified_source_text(
                        source,
                        handle.read(),
                        expected_sha256,
                    )
            finally:
                if fd != -1:
                    os.close(fd)
        except FileNotFoundError:
            raise FileNotFoundError(f"Source design missing: {source}") from None
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
                raise FileNotFoundError(f"Source design missing: {source}")
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
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    return source, _verified_source_text(
                        source,
                        handle.read(),
                        expected_sha256,
                    )
            finally:
                if fd != -1:
                    os.close(fd)

    raise RuntimeError(
        "secure source read unavailable: this platform must provide directory-relative "
        "filesystem operations or Windows directory handles"
    )


def _open_skill_directory(skill_dir: Path, target_dir: Path, *, create: bool) -> int | None:
    try:
        target_fd = _open_anchored_directory(target_dir, create=create)
    except OSError as exc:
        raise ValueError(f"target directory {target_dir} must be a real directory: {exc}") from None
    if target_fd is None:
        return None

    try:
        try:
            return os.open(skill_dir.name, _DIRECTORY_OPEN_FLAGS, dir_fd=target_fd)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(skill_dir.name, dir_fd=target_fd)
            except FileExistsError:
                pass
            return os.open(skill_dir.name, _DIRECTORY_OPEN_FLAGS, dir_fd=target_fd)
        except OSError as exc:
            raise ValueError(
                f"destination parent {skill_dir} must be a real directory: {exc}"
            ) from None
    finally:
        os.close(target_fd)


def _read_destination_text(parent_fd: int, destination: Path) -> tuple[str | None, int]:
    try:
        fd = os.open(destination.name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None, 0
    except OSError as exc:
        raise ValueError(f"destination {destination} must be a regular file: {exc}") from None

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"destination {destination} must be a regular file")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read(), metadata.st_nlink
    finally:
        if fd != -1:
            os.close(fd)


def _destination_mode_at(parent_fd: int, destination_name: str) -> int | None:
    try:
        metadata = os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"destination {destination_name} must be a regular file")
    return stat.S_IMODE(metadata.st_mode)


def _atomic_write_text(parent_fd: int, destination_name: str, content: str) -> None:
    temp_name = f".{destination_name}.{secrets.token_hex(8)}"
    existing_mode = _destination_mode_at(parent_fd, destination_name)
    fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o666 if existing_mode is None else existing_mode,
        dir_fd=parent_fd,
    )
    try:
        if existing_mode is not None:
            os.fchmod(fd, existing_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temp_name,
            destination_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temp_name = ""
    finally:
        if fd != -1:
            os.close(fd)
        if temp_name:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass


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


def _read_destination_text_path(
    skill_dir: Path,
    target_dir: Path,
    destination: Path,
) -> tuple[str | None, int]:
    if not _validate_real_directory(target_dir, label="target directory"):
        return None, 0
    if not _validate_real_directory(skill_dir, label="destination parent"):
        return None, 0
    _require_target_containment(destination, target_dir, label=f"SKILL.md {destination}")
    try:
        metadata = _lstat_optional(destination)
    except OSError as exc:
        raise ValueError(f"destination {destination} must be a regular file: {exc}") from None
    if metadata is None:
        return None, 0
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(destination, metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ValueError(f"destination {destination} must be a regular file")
    try:
        fd = os.open(destination, os.O_RDONLY)
    except OSError as exc:
        raise ValueError(f"destination {destination} must be a regular file: {exc}") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(metadata, opened):
            raise ValueError(f"destination {destination} changed while opening")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read(), opened.st_nlink
    finally:
        if fd != -1:
            os.close(fd)


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
    """Pin every parent without DELETE sharing before a path-based replace."""
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


def _atomic_write_text_windows(destination: Path, content: str) -> None:
    metadata = _lstat_optional(destination)
    if metadata is not None and (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(destination, metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ValueError(f"destination {destination} must be a regular file")
    mode = 0o666 if metadata is None else stat.S_IMODE(metadata.st_mode)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            if metadata is not None:
                os.chmod(temporary, mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_deployed_text(
    skill_dir: Path,
    target_dir: Path,
    destination: Path,
) -> tuple[str | None, int]:
    if not _supports_directory_fds():
        return _read_destination_text_path(skill_dir, target_dir, destination)

    skill_fd = _open_skill_directory(skill_dir, target_dir, create=False)
    try:
        return (None, 0) if skill_fd is None else _read_destination_text(skill_fd, destination)
    finally:
        if skill_fd is not None:
            os.close(skill_fd)


def _install_if_changed(
    skill_dir: Path,
    target_dir: Path,
    destination: Path,
    content: str,
) -> tuple[bool, bool]:
    if _supports_directory_fds():
        skill_fd = _open_skill_directory(skill_dir, target_dir, create=True)
        if skill_fd is None:
            raise RuntimeError(f"could not create destination parent {skill_dir}")
        try:
            existing, link_count = _read_destination_text(skill_fd, destination)
            existed = existing is not None
            if existing == content and link_count <= 1:
                return False, existed
            _atomic_write_text(skill_fd, destination.name, content)
            return True, existed
        finally:
            os.close(skill_fd)
    if _supports_windows_path_guards():
        with _guard_windows_directories(target_dir, skill_dir):
            existing, link_count = _read_destination_text_path(skill_dir, target_dir, destination)
            existed = existing is not None
            if existing == content and link_count <= 1:
                return False, existed
            _atomic_write_text_windows(destination, content)
            return True, existed
    raise RuntimeError(
        "secure install unavailable: this platform must provide directory-relative "
        "filesystem operations or Windows directory handles"
    )


def _prepare_entry(entry: dict, manifest: dict, target_dir: Path) -> tuple[Path, Path, str]:
    source_record: ExternalSourceRecord = validate_ingestion_manifest(
        DESIGNDOTMD_SOURCE,
        manifest,
        import_mode="full-body",
    )
    slug = _validate("slug", entry.get("slug"), regex=_SAFE_SLUG_RE)
    attribution = _render_attribution(manifest, entry)
    manifest_entries = manifest.get("entries")
    if not isinstance(manifest_entries, list) or not any(
        candidate is entry or candidate == entry for candidate in manifest_entries
    ):
        raise ValueError(f"{slug}: entry is not part of the registered full manifest")
    source_rel = _validate("source_path", entry.get("source_path"))
    source_sha256 = _validate_sha256(entry.get("sha256"))
    source, body = _read_source_text(source_rel, expected_sha256=source_sha256)

    tags = entry.get("tags", []) or []
    if not isinstance(tags, list):
        raise ValueError(f"{slug}: tags must be a list, got {type(tags).__name__}")
    tags = [str(tag).strip().lower() for tag in tags if str(tag).strip()]

    skill_dir = target_dir / f"designdotmd-{slug}"
    _require_target_containment(skill_dir, target_dir, label=f"skill dir {skill_dir}")
    destination = skill_dir / "SKILL.md"
    _require_target_containment(destination, target_dir, label=f"SKILL.md {destination}")
    body = _inject_tags(
        _quote_invalid_description_scalar(_strip_prior_attribution(body, source=source)), tags
    )
    provenance = entity_provenance(source_record)
    provenance["source_sha256"] = source_sha256
    body = _inject_provenance(body, provenance)
    _validate_frontmatter(body, source=source)
    return skill_dir, destination, attribution + body


def _deploy_prepared(
    prepared: tuple[Path, Path, str],
    target_dir: Path,
    dry_run: bool,
) -> tuple[Path, bool, bool]:
    skill_dir, destination, content = prepared
    if dry_run:
        existing, link_count = _read_deployed_text(skill_dir, target_dir, destination)
        existed = existing is not None
        return destination, existing != content or link_count > 1, existed
    changed, existed = _install_if_changed(skill_dir, target_dir, destination, content)
    return destination, changed, existed


def _deploy_entry(
    entry: dict,
    manifest: dict,
    target_dir: Path,
    dry_run: bool,
) -> tuple[Path, bool]:
    destination, changed, _ = _deploy_prepared(
        _prepare_entry(entry, manifest, target_dir), target_dir, dry_run
    )
    return destination, changed


def deploy_entry(
    entry: dict,
    manifest: dict,
    target_dir: Path,
    dry_run: bool,
) -> tuple[Path, bool]:
    return _deploy_entry(entry, manifest, _resolve_target_root(target_dir), dry_run)


def _entry_label(index: int, entry: object) -> str:
    if isinstance(entry, dict):
        for field in ("slug", "name"):
            identifier = entry.get(field)
            if isinstance(identifier, str) and identifier:
                return f"entry {index} ({identifier!r})"
    return f"entry {index}"


def _fail(message: str) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _fail_entry(index: int, entry: object, exc: BaseException) -> NoReturn:
    detail = str(exc)
    if isinstance(exc, KeyError) and exc.args:
        detail = f"missing required field {exc.args[0]!r}"
    if isinstance(entry, dict):
        for field in ("slug", "name"):
            identifier = entry.get(field)
            if isinstance(identifier, str) and detail.startswith(f"{identifier}: "):
                detail = detail[len(identifier) + 2 :]
                break
    _fail(f"{_entry_label(index, entry)}: {detail}")


def _preflight_manifest(
    manifest: dict,
    target_dir: Path,
) -> list[tuple[dict, tuple[Path, Path, str]]]:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        _fail(f"manifest entries must be a list, got {type(entries).__name__}")

    planned: list[tuple[dict, tuple[Path, Path, str]]] = []
    destinations: dict[Path, str] = {}
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            _fail(f"{_entry_label(index, entry)}: expected an object, got {type(entry).__name__}")
        try:
            prepared = _prepare_entry(entry, manifest, target_dir)
            skill_dir, dest, _ = prepared
            if _validate_real_directory(target_dir, label="target directory"):
                _validate_real_directory(skill_dir, label="destination parent")
                _read_deployed_text(skill_dir, target_dir, dest)
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            _fail_entry(index, entry, exc)

        destination = dest
        prior_entry = destinations.get(destination)
        if prior_entry is not None:
            _fail(
                f"{_entry_label(index, entry)}: duplicate destination {dest}; "
                f"already used by {prior_entry}"
            )
        destinations[destination] = _entry_label(index, entry)
        planned.append((entry, prepared))
    return planned


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--target",
        default=str(cfg.skills_dir),
        help=f"Target skills dir (default: {cfg.skills_dir})",
    )
    args = parser.parse_args()
    if args.install and args.dry_run:
        parser.error("--install and --dry-run cannot be combined")
    if not args.install and not args.dry_run:
        parser.error("Pass either --install or --dry-run")

    try:
        manifest = load_manifest()
    except UnicodeError:
        _fail(f"manifest {MANIFEST_PATH} is not valid UTF-8")
    except OSError as exc:
        _fail(f"could not read manifest {MANIFEST_PATH}: {exc}")
    except json.JSONDecodeError as exc:
        _fail(
            f"Malformed JSON in manifest {MANIFEST_PATH} at "
            f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
        )
    if not isinstance(manifest, dict):
        _fail(f"manifest root must be an object, got {type(manifest).__name__}")

    try:
        target_dir = _resolve_target_root(Path(args.target).expanduser())
    except ValueError as exc:
        _fail(str(exc))
    planned = _preflight_manifest(manifest, target_dir)

    new_or_updated = 0
    unchanged = 0
    entry_count = len(planned)
    for index, (entry, prepared) in enumerate(planned, start=1):
        try:
            dest, changed, destination_existed = _deploy_prepared(
                prepared, target_dir, dry_run=args.dry_run
            )
            marker = "UPD" if destination_existed else "NEW"
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            _fail_entry(index, entry, exc)
        if changed:
            new_or_updated += 1
        else:
            unchanged += 1
            marker = "   "
        # Show the first five and final entry while keeping long runs concise.
        if index <= 5 or index == entry_count:
            print(f"  [{marker}] {dest.relative_to(target_dir.parent)}")
        elif index == 6:
            print(f"  ... ({entry_count - 6} entries omitted) ...")

    mode = "dry-run" if args.dry_run else "install"
    print()
    print(f"Mode: {mode}  target: {target_dir}")
    print(f"Entries: {entry_count}  new/updated: {new_or_updated}  unchanged: {unchanged}")
    if args.install:
        print()
        print("Next steps:")
        print("  python src/catalog_builder.py")
        print("  python src/wiki_batch_entities.py --all")
        print("  python -m ctx.core.wiki.wiki_graphify")


if __name__ == "__main__":
    main()
