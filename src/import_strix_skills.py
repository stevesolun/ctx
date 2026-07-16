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
    return (
        f"<!-- strix-import: upstream={manifest['upstream']} "
        f"rev={manifest['upstream_revision'][:12]} "
        f"license={manifest['license']} category={entry['category']} -->\n"
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
    resolved = (root / candidate_rel).resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{field}: {candidate_rel!r} resolves outside import root") from exc
    return resolved


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _prepare_entry(entry: dict, manifest: dict, target_dir: Path) -> PreparedEntry:
    # Manifest fields are untrusted input (the repo's imported-skills/
    # MANIFEST.json is checked-in today, but the path from parsing to
    # filesystem write must still be defensible). Validate category
    # against a strict allowlist, contain source_path inside IMPORT_ROOT.
    category = _validate_manifest_field("category", entry.get("category"), regex=_SAFE_CATEGORY_RE)
    source_path_raw = _validate_manifest_field("source_path", entry.get("source_path"))
    source = _resolve_within(IMPORT_ROOT, source_path_raw, field="source_path")

    if not source.exists():
        raise FileNotFoundError(f"Source skill missing: {source}")

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
    target_resolved = target_dir.resolve()
    dest_resolved = skill_dir.resolve()
    try:
        dest_resolved.relative_to(target_resolved)
    except ValueError as exc:
        raise ValueError(f"skill dir {skill_dir} resolves outside target_dir") from exc
    dest = skill_dir / "SKILL.md"
    try:
        canonical_destination = dest.resolve()
        canonical_destination.relative_to(target_resolved)
    except ValueError as exc:
        raise ValueError(f"destination {dest} resolves outside target_dir") from exc

    if dest.is_symlink():
        raise ValueError(f"destination {dest} must not be a symlink")

    header = render_attribution_header(entry, manifest)
    body = source.read_text(encoding="utf-8")
    if body.startswith("<!-- strix-import:"):
        body = body.split("-->", 1)[1].lstrip("\n")
    content = header + body

    parent_is_symlink = skill_dir.is_symlink()
    parent_identity = None
    if skill_dir.exists():
        parent_identity = _identity(skill_dir.stat(follow_symlinks=False))

    destination_identity = None
    destination_link_count = 0
    existed = dest.exists()
    changed = True
    if existed:
        destination_metadata = dest.stat(follow_symlinks=False)
        if not stat.S_ISREG(destination_metadata.st_mode):
            raise ValueError(f"destination {dest} must be a regular file")
        destination_identity = _identity(destination_metadata)
        destination_link_count = destination_metadata.st_nlink
        existing = dest.read_text(encoding="utf-8")
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
    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"destination {prepared.destination} must not be a symlink")
    if metadata is not None and metadata.st_nlink > 1:
        raise ValueError(f"destination {prepared.destination} is hard-linked")
    current_identity = None if metadata is None else _identity(metadata)
    if current_identity != prepared.destination_identity:
        raise ValueError(f"destination {prepared.destination} changed after preflight")


def _write_via_directory_fd(prepared: PreparedEntry) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    target_fd = os.open(prepared.target_root, directory_flags)
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

        parent_fd = os.open(parent_name, directory_flags, dir_fd=target_fd)
        current_parent_identity = _identity(os.fstat(parent_fd))
        if created_parent:
            if prepared.parent_identity is not None:
                raise ValueError(f"skill dir {prepared.destination.parent} changed after preflight")
        elif current_parent_identity != prepared.parent_identity:
            raise ValueError(f"skill dir {prepared.destination.parent} changed after preflight")

        _validate_write_state(
            prepared,
            _destination_state_at(parent_fd, prepared.destination.name),
        )

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
    finally:
        if temp_name is not None and parent_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(target_fd)


def _write_via_checked_paths(prepared: PreparedEntry) -> None:
    parent = prepared.destination.parent
    if parent.is_symlink():
        raise ValueError(f"skill dir {parent} is a symlink")
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise ValueError(f"skill dir {parent} changed after preflight")
    if _identity(parent.stat(follow_symlinks=False)) != prepared.parent_identity and (
        prepared.parent_identity is not None
    ):
        raise ValueError(f"skill dir {parent} changed after preflight")

    current = None
    try:
        current = prepared.destination.stat(follow_symlinks=False)
    except FileNotFoundError:
        pass
    _validate_write_state(prepared, current)

    fd, temp_path = tempfile.mkstemp(prefix=f".{prepared.destination.name}.", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(prepared.content)
            handle.flush()
            os.fsync(handle.fileno())
        if parent.is_symlink():
            raise ValueError(f"skill dir {parent} changed after preflight")
        current = None
        try:
            current = prepared.destination.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        _validate_write_state(prepared, current)
        os.replace(temp_path, prepared.destination)
        temp_path = ""
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _write_prepared_entry(prepared: PreparedEntry) -> None:
    if not prepared.changed:
        return
    if prepared.parent_is_symlink or prepared.destination.parent.is_symlink():
        raise ValueError(f"skill dir {prepared.destination.parent} is a symlink")
    supports_directory_fds = (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.rename in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )
    if supports_directory_fds:
        _write_via_directory_fd(prepared)
    else:
        _write_via_checked_paths(prepared)


def _deploy_entry_with_status(
    entry: dict, manifest: dict, target_dir: Path, dry_run: bool
) -> tuple[Path, bool, bool]:
    prepared = _prepare_entry(entry, manifest, target_dir)
    if not dry_run:
        prepared.target_root.mkdir(parents=True, exist_ok=True)
        _write_prepared_entry(prepared)
    return prepared.destination, prepared.changed, prepared.existed


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
        except (OSError, UnicodeError, ValueError) as exc:
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
    target_dir = Path(args.target).expanduser()

    try:
        prepared_entries = _preflight_manifest(manifest, target_dir)

        if args.install and not target_dir.exists():
            print(f"Creating target dir: {target_dir}")
            target_dir.mkdir(parents=True, exist_ok=True)

        created = updated = unchanged = 0
        for prepared in prepared_entries:
            if args.install:
                _write_prepared_entry(prepared)
            dest = prepared.destination
            changed = prepared.changed
            existed = prepared.existed
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
    except (OSError, UnicodeError, ValueError) as exc:
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
