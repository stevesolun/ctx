"""Modular LLM-wiki page packs.

Wiki packs are the page-level counterpart to graph packs: a base pack contains
an immutable snapshot of wiki markdown pages, and overlay packs contain small
page upserts plus tombstones. Consumers can read the merged view without
rewriting or extracting the full shipped wiki tarball for every entity update.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ctx.utils._fs_utils import atomic_write_text

WIKI_PACK_MANIFEST = "wiki-pack-manifest.json"
WIKI_PACK_SCHEMA_VERSION = 1
WIKI_PACK_TYPES = frozenset({"base", "overlay"})

WikiPackType = Literal["base", "overlay"]


class WikiPackManifestError(ValueError):
    """Raised when a wiki pack manifest or artifact is malformed."""


@dataclass(frozen=True)
class WikiPackManifest:
    """Validated manifest for one wiki page pack."""

    pack_id: str
    pack_type: WikiPackType
    base_export_id: str
    parent_export_id: str | None
    page_count: int
    tombstone_count: int
    checksums: dict[str, str]
    created_at: str | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "WikiPackManifest":
        if payload.get("schema_version") != WIKI_PACK_SCHEMA_VERSION:
            raise WikiPackManifestError("wiki pack manifest schema_version must be 1")
        pack_type = payload.get("pack_type")
        if pack_type not in WIKI_PACK_TYPES:
            raise WikiPackManifestError("wiki pack manifest pack_type must be base or overlay")
        manifest = cls(
            pack_id=_required_str(payload, "pack_id"),
            pack_type=pack_type,
            base_export_id=_required_str(payload, "base_export_id"),
            parent_export_id=_optional_str(payload, "parent_export_id"),
            page_count=_nonnegative_int(payload, "page_count"),
            tombstone_count=_nonnegative_int(payload, "tombstone_count", default=0),
            checksums=_checksums(payload.get("checksums")),
            created_at=_optional_str(payload, "created_at"),
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        _validate_relative_name(self.pack_id, "pack_id")
        if self.pack_type == "base" and self.parent_export_id:
            raise WikiPackManifestError("base wiki packs must not set parent_export_id")
        if self.pack_type == "overlay" and not self.parent_export_id:
            raise WikiPackManifestError("overlay wiki packs must set parent_export_id")
        if not self.checksums:
            raise WikiPackManifestError("wiki pack manifest checksums must not be empty")

    def to_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": WIKI_PACK_SCHEMA_VERSION,
            "pack_id": self.pack_id,
            "pack_type": self.pack_type,
            "base_export_id": self.base_export_id,
            "parent_export_id": self.parent_export_id,
            "page_count": self.page_count,
            "tombstone_count": self.tombstone_count,
            "checksums": dict(sorted(self.checksums.items())),
        }
        if self.created_at is not None:
            payload["created_at"] = self.created_at
        return payload


@dataclass(frozen=True)
class WikiPackEntry:
    """A validated wiki pack and its directory."""

    path: Path
    manifest: WikiPackManifest


@dataclass(frozen=True)
class WikiPackPromotion:
    """Result of promoting a staged wiki pack set into the active location."""

    active_packs_dir: Path
    backup_packs_dir: Path | None
    rollback_metadata_path: Path
    promoted_pack_ids: list[str]
    replaced_pack_ids: list[str]
    replaced_validation_error: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": WIKI_PACK_SCHEMA_VERSION,
            "operation": "wiki-pack-promote",
            "active_packs_dir": str(self.active_packs_dir),
            "backup_packs_dir": str(self.backup_packs_dir) if self.backup_packs_dir else None,
            "rollback_metadata_path": str(self.rollback_metadata_path),
            "promoted_pack_ids": self.promoted_pack_ids,
            "replaced_pack_ids": self.replaced_pack_ids,
            "replaced_validation_error": self.replaced_validation_error,
        }


def write_wiki_base_pack(
    *,
    pack_dir: Path,
    pack_id: str,
    base_export_id: str,
    pages: dict[str, str],
    created_at: str | None = None,
) -> WikiPackManifest:
    """Write an immutable base wiki page pack."""
    return _write_wiki_pack(
        pack_dir=pack_dir,
        pack_id=pack_id,
        pack_type="base",
        base_export_id=base_export_id,
        parent_export_id=None,
        pages=pages,
        tombstones=[],
        created_at=created_at,
    )


def write_wiki_overlay_pack(
    *,
    pack_dir: Path,
    pack_id: str,
    base_export_id: str,
    parent_export_id: str,
    pages: dict[str, str],
    tombstones: list[str],
    created_at: str | None = None,
) -> WikiPackManifest:
    """Write a small wiki overlay pack containing page upserts and tombstones."""
    return _write_wiki_pack(
        pack_dir=pack_dir,
        pack_id=pack_id,
        pack_type="overlay",
        base_export_id=base_export_id,
        parent_export_id=parent_export_id,
        pages=pages,
        tombstones=tombstones,
        created_at=created_at,
    )


def write_active_wiki_overlay_pack(
    *,
    packs_dir: Path,
    pages: dict[str, str] | None = None,
    tombstones: list[str] | None = None,
    created_at: str | None = None,
) -> WikiPackManifest | None:
    """Append a small overlay to the active base wiki pack, if one exists."""
    page_map = {_normalise_page_path(path): text for path, text in (pages or {}).items()}
    tombstone_paths = [_normalise_page_path(path) for path in (tombstones or [])]
    if not page_map and not tombstone_paths:
        return None

    entries = discover_wiki_pack_manifests(packs_dir)
    if not entries:
        return None

    base = entries[0].manifest
    base_pack_id = _active_overlay_pack_id(page_map, tombstone_paths)
    for suffix in ["", *[f"-{index}" for index in range(1, 1000)]]:
        pack_id = f"{base_pack_id}{suffix}"
        pack_dir = packs_dir / pack_id
        if pack_dir.exists():
            continue
        return write_wiki_overlay_pack(
            pack_dir=pack_dir,
            pack_id=pack_id,
            base_export_id=base.base_export_id,
            parent_export_id=base.base_export_id,
            pages=page_map,
            tombstones=tombstone_paths,
            created_at=created_at,
        )
    raise WikiPackManifestError("could not allocate unique wiki overlay pack id")


def read_wiki_pack_manifest(path: Path) -> WikiPackManifest:
    """Read and validate ``wiki-pack-manifest.json``."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WikiPackManifestError(f"wiki pack manifest is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise WikiPackManifestError("wiki pack manifest must be a JSON object")
    return WikiPackManifest.from_mapping(payload)


def discover_wiki_pack_manifests(packs_dir: Path) -> list[WikiPackEntry]:
    """Discover one base wiki pack plus overlays under ``packs_dir``."""
    if not packs_dir.is_dir():
        return []
    entries: list[WikiPackEntry] = []
    for child in sorted(packs_dir.iterdir(), key=lambda item: item.name):
        manifest_path = child / WIKI_PACK_MANIFEST
        if not child.is_dir() or not manifest_path.is_file():
            continue
        manifest = read_wiki_pack_manifest(manifest_path)
        _verify_pack_checksums(child, manifest)
        entries.append(WikiPackEntry(path=child, manifest=manifest))

    base_entries = [entry for entry in entries if entry.manifest.pack_type == "base"]
    overlay_entries = [entry for entry in entries if entry.manifest.pack_type == "overlay"]
    if len(base_entries) > 1:
        raise WikiPackManifestError("wiki packs must contain at most one base pack")
    if not base_entries and overlay_entries:
        raise WikiPackManifestError("wiki overlay packs require a base pack")
    if not base_entries:
        return []
    base = base_entries[0]
    for overlay in overlay_entries:
        if overlay.manifest.parent_export_id != base.manifest.base_export_id:
            raise WikiPackManifestError(
                f"overlay {overlay.manifest.pack_id} parent_export_id "
                f"{overlay.manifest.parent_export_id!r} does not match base export "
                f"{base.manifest.base_export_id!r}"
            )
        if overlay.manifest.base_export_id != base.manifest.base_export_id:
            raise WikiPackManifestError(
                f"overlay {overlay.manifest.pack_id} base_export_id "
                f"{overlay.manifest.base_export_id!r} does not match active base "
                f"{base.manifest.base_export_id!r}"
            )
    return [base, *sorted(overlay_entries, key=_overlay_sort_key)]


def _overlay_sort_key(entry: WikiPackEntry) -> tuple[str, str]:
    return entry.manifest.created_at or "", entry.manifest.pack_id


def load_merged_wiki_pages(packs_dir: Path) -> dict[str, str]:
    """Return wiki-relative markdown pages after applying overlay packs."""
    entries = discover_wiki_pack_manifests(packs_dir)
    if not entries:
        return {}
    pages: dict[str, str] = {}
    for entry in entries:
        page_rows = _read_jsonl_objects(entry.path / "pages.jsonl")
        tombstone_rows = _read_jsonl_objects(entry.path / "tombstones.jsonl")
        _validate_pack_count(
            entry.manifest.pack_id,
            "page_count",
            actual=len(page_rows),
            expected=entry.manifest.page_count,
        )
        _validate_pack_count(
            entry.manifest.pack_id,
            "tombstone_count",
            actual=len(tombstone_rows),
            expected=entry.manifest.tombstone_count,
        )
        for row in page_rows:
            relpath = _normalise_page_path(_required_str(row, "path"))
            text = _required_str(row, "text")
            expected_sha = row.get("sha256")
            if isinstance(expected_sha, str) and expected_sha != _sha256_text(text):
                raise WikiPackManifestError(f"wiki page checksum mismatch: {relpath}")
            pages[relpath] = text
        for row in tombstone_rows:
            pages.pop(_normalise_page_path(_required_str(row, "path")), None)
    return pages


def compact_wiki_packs(
    *,
    packs_dir: Path,
    compacted_pack_dir: Path,
    base_export_id: str,
    created_at: str | None = None,
) -> WikiPackManifest:
    """Merge active base+overlay wiki packs into one staged immutable base pack."""
    entries = discover_wiki_pack_manifests(packs_dir)
    if len(entries) <= 1:
        raise WikiPackManifestError("wiki pack compaction requires at least one overlay pack")
    pages = load_merged_wiki_pages(packs_dir)
    return write_wiki_base_pack(
        pack_dir=compacted_pack_dir,
        pack_id=compacted_pack_dir.name,
        base_export_id=base_export_id,
        pages=pages,
        created_at=created_at,
    )


def promote_wiki_pack_set(
    *,
    staged_packs_dir: Path,
    active_packs_dir: Path,
    backup_packs_dir: Path | None = None,
) -> WikiPackPromotion:
    """Promote a validated staged wiki pack set into the active packs directory."""
    if _paths_same(staged_packs_dir, active_packs_dir):
        raise WikiPackManifestError("staged and active wiki pack directories must differ")

    staged_entries = discover_wiki_pack_manifests(staged_packs_dir)
    if not staged_entries:
        raise WikiPackManifestError("staged wiki pack set does not contain a valid base pack")
    load_merged_wiki_pages(staged_packs_dir)
    promoted_pack_ids = [entry.manifest.pack_id for entry in staged_entries]

    replaced_pack_ids: list[str] = []
    replaced_validation_error: str | None = None
    active_exists = active_packs_dir.exists()
    if active_exists:
        if not active_packs_dir.is_dir():
            raise WikiPackManifestError("active wiki packs path exists but is not a directory")
        try:
            replaced_pack_ids = [
                entry.manifest.pack_id for entry in discover_wiki_pack_manifests(active_packs_dir)
            ]
        except WikiPackManifestError as exc:
            replaced_validation_error = str(exc)

    backup_dir = backup_packs_dir if active_exists else None
    if backup_dir is None and active_exists:
        backup_dir = _next_rollback_dir(active_packs_dir)
    if backup_dir is not None:
        if _paths_same(backup_dir, active_packs_dir) or _paths_same(backup_dir, staged_packs_dir):
            raise WikiPackManifestError("backup wiki packs directory must be distinct")
        if backup_dir.exists():
            raise WikiPackManifestError(f"backup wiki packs directory already exists: {backup_dir}")
        backup_dir.parent.mkdir(parents=True, exist_ok=True)

    active_packs_dir.parent.mkdir(parents=True, exist_ok=True)
    moved_active = False
    try:
        if active_exists and backup_dir is not None:
            active_packs_dir.replace(backup_dir)
            moved_active = True
        staged_packs_dir.replace(active_packs_dir)
    except OSError as exc:
        if (
            moved_active
            and backup_dir is not None
            and backup_dir.exists()
            and not active_packs_dir.exists()
        ):
            backup_dir.replace(active_packs_dir)
        raise WikiPackManifestError(f"failed to promote wiki pack set: {exc}") from exc

    metadata_path = active_packs_dir.with_name(f"{active_packs_dir.name}.rollback.json")
    result = WikiPackPromotion(
        active_packs_dir=active_packs_dir,
        backup_packs_dir=backup_dir,
        rollback_metadata_path=metadata_path,
        promoted_pack_ids=promoted_pack_ids,
        replaced_pack_ids=replaced_pack_ids,
        replaced_validation_error=replaced_validation_error,
    )
    metadata = result.to_mapping()
    metadata["created_at"] = datetime.now(UTC).isoformat()
    atomic_write_text(
        metadata_path,
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ctx.core.wiki.wiki_packs",
        description="Manage ctx LLM-wiki base and overlay packs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    compact = sub.add_parser(
        "compact",
        help="Merge active base+overlay wiki packs into one staged base pack.",
    )
    compact.add_argument("--packs-dir", required=True, help="Active wiki packs directory")
    compact.add_argument(
        "--staged-pack-dir",
        required=True,
        help="Destination directory for the compacted base pack",
    )
    compact.add_argument("--base-export-id", required=True, help="New compacted wiki export id")
    compact.add_argument("--created-at", help="Optional created_at value for the new manifest")
    compact.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    promote = sub.add_parser(
        "promote",
        help="Promote a staged wiki pack set into the active packs directory.",
    )
    promote.add_argument(
        "--staged-packs-dir",
        required=True,
        help="Validated staged wiki packs root to promote",
    )
    promote.add_argument("--active-packs-dir", required=True, help="Active wiki packs root")
    promote.add_argument("--backup-packs-dir", help="Optional rollback directory for old packs")
    promote.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    if args.command == "compact":
        try:
            manifest = compact_wiki_packs(
                packs_dir=Path(args.packs_dir),
                compacted_pack_dir=Path(args.staged_pack_dir),
                base_export_id=args.base_export_id,
                created_at=args.created_at,
            )
        except WikiPackManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        payload = manifest.to_mapping()
        payload["pack_dir"] = str(Path(args.staged_pack_dir))
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"compacted {manifest.pack_id}: {manifest.page_count} pages")
        return 0
    if args.command == "promote":
        try:
            result = promote_wiki_pack_set(
                staged_packs_dir=Path(args.staged_packs_dir),
                active_packs_dir=Path(args.active_packs_dir),
                backup_packs_dir=Path(args.backup_packs_dir) if args.backup_packs_dir else None,
            )
        except WikiPackManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        payload = result.to_mapping()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            backup = result.backup_packs_dir or "<none>"
            print(f"promoted {', '.join(result.promoted_pack_ids)}; backup: {backup}")
        return 0
    return 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_wiki_pack(
    *,
    pack_dir: Path,
    pack_id: str,
    pack_type: WikiPackType,
    base_export_id: str,
    parent_export_id: str | None,
    pages: dict[str, str],
    tombstones: list[str],
    created_at: str | None,
) -> WikiPackManifest:
    _validate_relative_name(pack_id, "pack_id")
    manifest_path = pack_dir / WIKI_PACK_MANIFEST
    if manifest_path.exists():
        raise WikiPackManifestError(f"wiki pack already exists: {pack_id}")
    pack_dir.mkdir(parents=True, exist_ok=True)
    page_rows = [
        {
            "path": relpath,
            "sha256": _sha256_text(text),
            "text": text,
        }
        for relpath, text in sorted(
            (_normalise_page_path(path), value) for path, value in pages.items()
        )
    ]
    tombstone_rows = [{"path": _normalise_page_path(path)} for path in sorted(tombstones)]
    artifact_paths: list[str] = []
    _write_jsonl(pack_dir / "pages.jsonl", page_rows)
    artifact_paths.append("pages.jsonl")
    _write_jsonl(pack_dir / "tombstones.jsonl", tombstone_rows)
    artifact_paths.append("tombstones.jsonl")
    manifest = WikiPackManifest(
        pack_id=pack_id,
        pack_type=pack_type,
        base_export_id=base_export_id,
        parent_export_id=parent_export_id,
        page_count=len(page_rows),
        tombstone_count=len(tombstone_rows),
        checksums={name: sha256_file(pack_dir / name) for name in artifact_paths},
        created_at=created_at,
    )
    manifest.validate()
    atomic_write_text(
        manifest_path,
        json.dumps(manifest.to_mapping(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _verify_pack_checksums(pack_dir: Path, manifest: WikiPackManifest) -> None:
    for name, expected in manifest.checksums.items():
        path = pack_dir / name
        if not path.is_file():
            raise WikiPackManifestError(
                f"wiki pack {manifest.pack_id} checksum target missing: {name}"
            )
        if sha256_file(path) != expected:
            raise WikiPackManifestError(
                f"wiki pack {manifest.pack_id} checksum mismatch for {name}"
            )


def _validate_pack_count(
    pack_id: str,
    field_name: str,
    *,
    actual: int,
    expected: int,
) -> None:
    if actual != expected:
        raise WikiPackManifestError(
            f"wiki pack {pack_id} {field_name} mismatch: expected {expected}, got {actual}"
        )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WikiPackManifestError(f"{path} line {lineno} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise WikiPackManifestError(f"{path} line {lineno} did not contain a JSON object")
        rows.append(payload)
    return rows


def _normalise_page_path(value: str) -> str:
    normalised = value.replace("\\", "/").strip()
    _validate_relative_name(normalised, "page path")
    if not normalised.endswith(".md"):
        raise WikiPackManifestError("wiki pack page path must end with .md")
    return normalised


def _active_overlay_pack_id(pages: dict[str, str], tombstones: list[str]) -> str:
    paths = sorted([*pages, *tombstones])
    first_path = paths[0] if paths else "empty.md"
    stem = first_path.removesuffix(".md").replace("/", "-").replace("\\", "-")
    stem = stem[:80].strip("-") or "wiki"
    action = "delete" if tombstones and not pages else "upsert"
    digest_source = json.dumps(
        {
            "pages": {path: _sha256_text(text) for path, text in sorted(pages.items())},
            "tombstones": sorted(tombstones),
        },
        sort_keys=True,
    )
    digest = _sha256_text(digest_source)[:12]
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"overlay-{timestamp}-{stem}-{action}-{digest}"


def _validate_relative_name(value: str, label: str) -> None:
    path = Path(value)
    if path.is_absolute() or value.startswith(("/", "\\")):
        raise WikiPackManifestError(f"wiki pack manifest {label} must be relative")
    parts = value.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WikiPackManifestError(f"wiki pack manifest {label} is unsafe")


def _paths_same(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def _next_rollback_dir(active_packs_dir: Path) -> Path:
    first = active_packs_dir.with_name(f"{active_packs_dir.name}.rollback")
    if not first.exists():
        return first
    for index in range(2, 1000):
        candidate = active_packs_dir.with_name(f"{active_packs_dir.name}.rollback-{index}")
        if not candidate.exists():
            return candidate
    raise WikiPackManifestError("could not allocate wiki packs rollback directory")


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WikiPackManifestError(f"wiki pack manifest {key} must be a non-empty string")
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WikiPackManifestError(f"wiki pack manifest {key} must be a string or null")
    return value


def _nonnegative_int(payload: dict[str, Any], key: str, *, default: int | None = None) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or value < 0:
        raise WikiPackManifestError(f"wiki pack manifest {key} must be a non-negative integer")
    return value


def _checksums(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WikiPackManifestError("wiki pack manifest checksums must be an object")
    result: dict[str, str] = {}
    for raw_name, raw_digest in value.items():
        if not isinstance(raw_name, str):
            raise WikiPackManifestError("wiki pack manifest checksum names must be strings")
        name = raw_name.replace("\\", "/").strip()
        _validate_relative_name(name, "checksum name")
        if not isinstance(raw_digest, str) or len(raw_digest) != 64:
            raise WikiPackManifestError(
                f"wiki pack manifest checksum for {name} must be a SHA-256 hex digest"
            )
        result[name] = raw_digest
    return result


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":  # pragma: no cover - exercised through main() tests.
    raise SystemExit(main())
