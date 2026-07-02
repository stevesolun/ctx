"""Graph pack manifest contract.

Graph packs are the planned modular graph artifact unit:

``base-*`` packs hold a complete graph export, while ``overlay-*`` packs hold
incremental nodes, edges, and tombstones that can be merged over a base pack.
This module defines the pack manifest contract plus the small reader/writer
primitives used to stage overlay packs and periodic compacted base packs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any, Literal

import networkx as nx

from ctx.utils._fs_utils import atomic_write_text

GRAPH_PACK_MANIFEST = "graph-pack-manifest.json"
GRAPH_PACK_SCHEMA_VERSION = 1
PACK_TYPES = frozenset({"base", "overlay"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

PackType = Literal["base", "overlay"]


class GraphPackManifestError(ValueError):
    """Raised when a graph pack manifest is malformed."""


@dataclass(frozen=True)
class GraphPackEntry:
    """A validated graph pack manifest and its directory."""

    path: Path
    manifest: "GraphPackManifest"


@dataclass(frozen=True)
class GraphPackPromotion:
    """Result of promoting a staged graph pack set into the active location."""

    active_packs_dir: Path
    backup_packs_dir: Path | None
    rollback_metadata_path: Path
    promoted_pack_ids: list[str]
    replaced_pack_ids: list[str]
    replaced_validation_error: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        """Serialise promotion metadata for CLI output and rollback records."""
        return {
            "schema_version": GRAPH_PACK_SCHEMA_VERSION,
            "operation": "graph-pack-promote",
            "active_packs_dir": str(self.active_packs_dir),
            "backup_packs_dir": str(self.backup_packs_dir) if self.backup_packs_dir else None,
            "rollback_metadata_path": str(self.rollback_metadata_path),
            "promoted_pack_ids": self.promoted_pack_ids,
            "replaced_pack_ids": self.replaced_pack_ids,
            "replaced_validation_error": self.replaced_validation_error,
        }


@dataclass(frozen=True)
class GraphPackManifest:
    """Validated manifest for one graph pack directory."""

    pack_id: str
    pack_type: PackType
    base_export_id: str
    parent_export_id: str | None
    config_hash: str
    model_id: str
    node_count: int
    edge_count: int
    checksums: dict[str, str]
    tombstone_count: int = 0
    created_at: str | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "GraphPackManifest":
        """Build and validate a manifest from JSON-decoded data."""
        if payload.get("schema_version") != GRAPH_PACK_SCHEMA_VERSION:
            raise GraphPackManifestError("graph pack manifest schema_version must be 1")
        pack_type = payload.get("pack_type")
        if pack_type not in PACK_TYPES:
            raise GraphPackManifestError("graph pack manifest pack_type must be base or overlay")
        manifest = cls(
            pack_id=_required_str(payload, "pack_id"),
            pack_type=pack_type,
            base_export_id=_required_str(payload, "base_export_id"),
            parent_export_id=_optional_str(payload, "parent_export_id"),
            config_hash=_required_str(payload, "config_hash"),
            model_id=_required_str(payload, "model_id"),
            node_count=_nonnegative_int(payload, "node_count"),
            edge_count=_nonnegative_int(payload, "edge_count"),
            checksums=_checksums(payload.get("checksums")),
            tombstone_count=_nonnegative_int(payload, "tombstone_count", default=0),
            created_at=_optional_str(payload, "created_at"),
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        """Validate cross-field invariants."""
        _validate_relative_manifest_name(self.pack_id, "pack_id")
        if self.pack_type == "base" and self.parent_export_id:
            raise GraphPackManifestError("base graph packs must not set parent_export_id")
        if self.pack_type == "overlay" and not self.parent_export_id:
            raise GraphPackManifestError("overlay graph packs must set parent_export_id")
        if not self.checksums:
            raise GraphPackManifestError("graph pack manifest checksums must not be empty")

    def to_mapping(self) -> dict[str, Any]:
        """Return deterministic JSON-serialisable manifest data."""
        payload: dict[str, Any] = {
            "schema_version": GRAPH_PACK_SCHEMA_VERSION,
            "pack_id": self.pack_id,
            "pack_type": self.pack_type,
            "base_export_id": self.base_export_id,
            "parent_export_id": self.parent_export_id,
            "config_hash": self.config_hash,
            "model_id": self.model_id,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "tombstone_count": self.tombstone_count,
            "checksums": dict(sorted(self.checksums.items())),
        }
        if self.created_at is not None:
            payload["created_at"] = self.created_at
        return payload


def build_pack_manifest(
    *,
    pack_dir: Path,
    pack_id: str,
    pack_type: PackType,
    base_export_id: str,
    parent_export_id: str | None,
    config_hash: str,
    model_id: str,
    node_count: int,
    edge_count: int,
    artifact_paths: list[str],
    tombstone_count: int = 0,
    created_at: str | None = None,
) -> GraphPackManifest:
    """Create a manifest and compute SHA-256 checksums for pack artifacts."""
    checksums = {
        _normalise_artifact_name(name): sha256_file(pack_dir / name) for name in artifact_paths
    }
    return GraphPackManifest(
        pack_id=pack_id,
        pack_type=pack_type,
        base_export_id=base_export_id,
        parent_export_id=parent_export_id,
        config_hash=config_hash,
        model_id=model_id,
        node_count=node_count,
        edge_count=edge_count,
        checksums=checksums,
        tombstone_count=tombstone_count,
        created_at=created_at,
    )


def read_pack_manifest(path: Path) -> GraphPackManifest:
    """Read and validate ``graph-pack-manifest.json``."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GraphPackManifestError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphPackManifestError(f"{path} did not contain a JSON object")
    return GraphPackManifest.from_mapping(payload)


def write_pack_manifest(path: Path, manifest: GraphPackManifest) -> None:
    """Atomically write a graph pack manifest."""
    manifest.validate()
    atomic_write_text(
        path,
        json.dumps(manifest.to_mapping(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_overlay_pack(
    *,
    pack_dir: Path,
    pack_id: str,
    base_export_id: str,
    parent_export_id: str,
    config_hash: str,
    model_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    tombstones: list[dict[str, Any]],
    created_at: str | None = None,
) -> GraphPackManifest:
    """Write a first-class overlay pack with JSONL payload artifacts."""
    _validate_relative_manifest_name(pack_id, "pack_id")
    created_at = created_at or datetime.now(UTC).isoformat()
    artifact_paths: list[str] = []
    if nodes:
        artifact_paths.append("nodes.jsonl")
    if edges:
        artifact_paths.append("edges.jsonl")
    if tombstones:
        artifact_paths.append("tombstones.jsonl")
    if not artifact_paths:
        raise GraphPackManifestError("empty overlay pack cannot be written")

    manifest_path = pack_dir / GRAPH_PACK_MANIFEST
    if manifest_path.exists():
        raise GraphPackManifestError(f"graph overlay pack already exists: {pack_id}")

    pack_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in ("nodes.jsonl", "edges.jsonl", "tombstones.jsonl"):
        (pack_dir / stale_name).unlink(missing_ok=True)
    if nodes:
        _write_jsonl(pack_dir / "nodes.jsonl", nodes)
    if edges:
        _write_jsonl(pack_dir / "edges.jsonl", edges)
    if tombstones:
        _write_jsonl(pack_dir / "tombstones.jsonl", tombstones)

    manifest = build_pack_manifest(
        pack_dir=pack_dir,
        pack_id=pack_id,
        pack_type="overlay",
        base_export_id=base_export_id,
        parent_export_id=parent_export_id,
        config_hash=config_hash,
        model_id=model_id,
        node_count=len(nodes),
        edge_count=len(edges),
        artifact_paths=artifact_paths,
        tombstone_count=len(tombstones),
        created_at=created_at,
    )
    write_pack_manifest(manifest_path, manifest)
    return manifest


def write_base_pack(
    *,
    pack_dir: Path,
    pack_id: str,
    base_export_id: str,
    config_hash: str,
    model_id: str,
    graph: nx.Graph,
    created_at: str | None = None,
) -> GraphPackManifest:
    """Write an immutable base graph pack from a NetworkX graph."""
    _validate_relative_manifest_name(pack_id, "pack_id")
    manifest_path = pack_dir / GRAPH_PACK_MANIFEST
    if manifest_path.exists():
        raise GraphPackManifestError(f"graph base pack already exists: {pack_id}")

    pack_dir.mkdir(parents=True, exist_ok=True)
    graph_copy = graph.copy()
    graph_copy.graph["export_id"] = base_export_id
    graph_data = _node_link_payload(graph_copy)
    atomic_write_text(
        pack_dir / "graph.json",
        json.dumps(graph_data, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    manifest = build_pack_manifest(
        pack_dir=pack_dir,
        pack_id=pack_id,
        pack_type="base",
        base_export_id=base_export_id,
        parent_export_id=None,
        config_hash=config_hash,
        model_id=model_id,
        node_count=graph_copy.number_of_nodes(),
        edge_count=graph_copy.number_of_edges(),
        artifact_paths=["graph.json"],
        created_at=created_at,
    )
    write_pack_manifest(manifest_path, manifest)
    return manifest


def compact_graph_packs(
    *,
    packs_dir: Path,
    compacted_pack_dir: Path,
    base_export_id: str,
    config_hash: str | None = None,
    model_id: str | None = None,
    created_at: str | None = None,
) -> GraphPackManifest:
    """Merge active base+overlay packs into one staged immutable base pack."""
    entries = discover_pack_manifests(packs_dir)
    if len(entries) <= 1:
        raise GraphPackManifestError("graph pack compaction requires at least one overlay pack")

    source_base = entries[0].manifest
    graph = load_merged_pack_graph(packs_dir)
    graph.graph["ctx_compacted_from_base_export_id"] = source_base.base_export_id
    graph.graph["ctx_compacted_pack_ids"] = [entry.manifest.pack_id for entry in entries]
    graph.graph["ctx_compacted_overlay_count"] = len(entries) - 1
    return write_base_pack(
        pack_dir=compacted_pack_dir,
        pack_id=compacted_pack_dir.name,
        base_export_id=base_export_id,
        config_hash=config_hash or source_base.config_hash,
        model_id=model_id or source_base.model_id,
        graph=graph,
        created_at=created_at,
    )


def promote_graph_pack_set(
    *,
    staged_packs_dir: Path,
    active_packs_dir: Path,
    backup_packs_dir: Path | None = None,
) -> GraphPackPromotion:
    """Promote a validated staged pack set into the active packs directory.

    The swap is a same-filesystem directory rename: the previous active pack set
    is moved to a rollback directory before the staged set is moved into place.
    If the final move fails after the active directory was backed up, the old
    active directory is restored before returning an error.
    """
    if _paths_same(staged_packs_dir, active_packs_dir):
        raise GraphPackManifestError("staged and active graph pack directories must differ")

    staged_entries = discover_pack_manifests(staged_packs_dir)
    if not staged_entries:
        raise GraphPackManifestError("staged graph pack set does not contain a valid base pack")
    # Force endpoint/tombstone validation before the active directory is touched.
    load_merged_pack_graph(staged_packs_dir)
    promoted_pack_ids = [entry.manifest.pack_id for entry in staged_entries]

    replaced_pack_ids: list[str] = []
    replaced_validation_error: str | None = None
    active_exists = active_packs_dir.exists()
    if active_exists:
        if not active_packs_dir.is_dir():
            raise GraphPackManifestError("active graph packs path exists but is not a directory")
        try:
            replaced_pack_ids = [
                entry.manifest.pack_id for entry in discover_pack_manifests(active_packs_dir)
            ]
        except GraphPackManifestError as exc:
            replaced_validation_error = str(exc)

    backup_dir = backup_packs_dir if active_exists else None
    if backup_dir is None and active_exists:
        backup_dir = _next_rollback_dir(active_packs_dir)
    if backup_dir is not None:
        if _paths_same(backup_dir, active_packs_dir) or _paths_same(backup_dir, staged_packs_dir):
            raise GraphPackManifestError("backup graph packs directory must be distinct")
        if backup_dir.exists():
            raise GraphPackManifestError(
                f"backup graph packs directory already exists: {backup_dir}"
            )
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
        raise GraphPackManifestError(f"failed to promote graph pack set: {exc}") from exc

    metadata_path = active_packs_dir.with_name(f"{active_packs_dir.name}.rollback.json")
    result = GraphPackPromotion(
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
        prog="python -m ctx.core.graph.graph_packs",
        description="Manage ctx graph base and overlay packs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    compact = sub.add_parser(
        "compact",
        help="Merge active base+overlay packs into one staged base pack.",
    )
    compact.add_argument("--packs-dir", required=True, help="Active graph packs directory")
    compact.add_argument(
        "--staged-pack-dir",
        required=True,
        help="Destination directory for the compacted base pack",
    )
    compact.add_argument("--base-export-id", required=True, help="New compacted base export id")
    compact.add_argument("--config-hash", help="Override config hash; defaults to source base")
    compact.add_argument("--model-id", help="Override model id; defaults to source base")
    compact.add_argument("--created-at", help="Optional created_at value for the new manifest")
    compact.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    promote = sub.add_parser(
        "promote",
        help="Promote a staged graph pack set into the active packs directory.",
    )
    promote.add_argument(
        "--staged-packs-dir",
        required=True,
        help="Validated staged graph packs root to promote",
    )
    promote.add_argument("--active-packs-dir", required=True, help="Active graph packs root")
    promote.add_argument(
        "--backup-packs-dir", help="Optional rollback directory for old active packs"
    )
    promote.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    if args.command == "compact":
        try:
            manifest = compact_graph_packs(
                packs_dir=Path(args.packs_dir),
                compacted_pack_dir=Path(args.staged_pack_dir),
                base_export_id=args.base_export_id,
                config_hash=args.config_hash,
                model_id=args.model_id,
                created_at=args.created_at,
            )
        except GraphPackManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        payload = manifest.to_mapping()
        payload["pack_dir"] = str(Path(args.staged_pack_dir))
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                "compacted "
                f"{manifest.pack_id}: {manifest.node_count} nodes, "
                f"{manifest.edge_count} edges"
            )
        return 0
    if args.command == "promote":
        try:
            result = promote_graph_pack_set(
                staged_packs_dir=Path(args.staged_packs_dir),
                active_packs_dir=Path(args.active_packs_dir),
                backup_packs_dir=Path(args.backup_packs_dir) if args.backup_packs_dir else None,
            )
        except GraphPackManifestError as exc:
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


def discover_pack_manifests(packs_dir: Path) -> list[GraphPackEntry]:
    """Discover and validate graph pack manifests under ``packs_dir``.

    The returned order is always the active base pack first, followed by
    overlay packs sorted by creation time, then pack id. This keeps immutable
    overlay application deterministic while preserving "latest pack wins"
    semantics for repeated updates to the same node or edge.
    """
    if not packs_dir.is_dir():
        return []
    entries: list[GraphPackEntry] = []
    for child in sorted(packs_dir.iterdir(), key=lambda item: item.name):
        manifest_path = child / GRAPH_PACK_MANIFEST
        if not child.is_dir() or not manifest_path.is_file():
            continue
        manifest = read_pack_manifest(manifest_path)
        _verify_pack_checksums(child, manifest)
        entries.append(GraphPackEntry(path=child, manifest=manifest))

    base_entries = [entry for entry in entries if entry.manifest.pack_type == "base"]
    overlay_entries = [entry for entry in entries if entry.manifest.pack_type == "overlay"]
    if len(base_entries) > 1:
        raise GraphPackManifestError("graph packs must contain at most one base pack")
    if not base_entries and overlay_entries:
        raise GraphPackManifestError("graph overlay packs require a base pack")
    if not base_entries:
        return []

    base = base_entries[0]
    for overlay in overlay_entries:
        if overlay.manifest.parent_export_id != base.manifest.base_export_id:
            raise GraphPackManifestError(
                f"overlay {overlay.manifest.pack_id} parent_export_id "
                f"{overlay.manifest.parent_export_id!r} does not match base export "
                f"{base.manifest.base_export_id!r}"
            )
        if overlay.manifest.base_export_id != base.manifest.base_export_id:
            raise GraphPackManifestError(
                f"overlay {overlay.manifest.pack_id} base_export_id "
                f"{overlay.manifest.base_export_id!r} does not match active base "
                f"{base.manifest.base_export_id!r}"
            )
    return [base, *sorted(overlay_entries, key=_overlay_sort_key)]


def _overlay_sort_key(entry: GraphPackEntry) -> tuple[str, str]:
    return entry.manifest.created_at or "", entry.manifest.pack_id


def load_merged_pack_graph(packs_dir: Path) -> nx.Graph:
    """Load one base graph pack plus active overlay packs into a NetworkX graph."""
    entries = discover_pack_manifests(packs_dir)
    if not entries:
        return nx.Graph()
    base = entries[0]
    graph = _load_base_graph(base.path / "graph.json", base.manifest)
    pack_ids = [base.manifest.pack_id]
    for overlay in entries[1:]:
        _apply_overlay_pack(graph, overlay)
        pack_ids.append(overlay.manifest.pack_id)
    graph.graph["ctx_pack_ids"] = pack_ids
    graph.graph["ctx_pack_base_export_id"] = base.manifest.base_export_id
    return graph


def sha256_file(path: Path) -> str:
    """Return SHA-256 hex digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_pack_checksums(pack_dir: Path, manifest: GraphPackManifest) -> None:
    for name, expected in manifest.checksums.items():
        path = pack_dir / name
        if not path.is_file():
            raise GraphPackManifestError(
                f"graph pack {manifest.pack_id} checksum target missing: {name}"
            )
        actual = sha256_file(path)
        if actual != expected:
            raise GraphPackManifestError(
                f"graph pack {manifest.pack_id} checksum mismatch for {name}"
            )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _node_link_payload(graph: nx.Graph) -> dict[str, Any]:
    try:
        payload = nx.node_link_data(graph, edges="edges")
    except TypeError:  # pragma: no cover - networkx < 3 compatibility.
        payload = nx.node_link_data(graph)
        payload["edges"] = payload.pop("links", payload.get("edges", []))
    if not isinstance(payload, dict):
        raise GraphPackManifestError("node-link export did not produce an object")
    return payload


def _load_base_graph(path: Path, manifest: GraphPackManifest) -> nx.Graph:
    payload = _read_json_object(path)
    graph = nx.Graph()
    graph_meta = payload.get("graph")
    if isinstance(graph_meta, dict):
        graph.graph.update(graph_meta)
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise GraphPackManifestError(f"{path} missing nodes list")
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            raise GraphPackManifestError(f"{path} contains non-object node")
        node_id = raw_node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise GraphPackManifestError(f"{path} contains node without id")
        graph.add_node(node_id, **{key: value for key, value in raw_node.items() if key != "id"})
    raw_edges = payload.get("edges", payload.get("links", []))
    if not isinstance(raw_edges, list):
        raise GraphPackManifestError(f"{path} edges must be a list")
    for raw_edge in raw_edges:
        _add_edge(graph, raw_edge, context=str(path))
    _validate_pack_count(
        manifest.pack_id,
        "node_count",
        actual=graph.number_of_nodes(),
        expected=manifest.node_count,
    )
    _validate_pack_count(
        manifest.pack_id,
        "edge_count",
        actual=graph.number_of_edges(),
        expected=manifest.edge_count,
    )
    return graph


def _apply_overlay_pack(graph: nx.Graph, overlay: GraphPackEntry) -> None:
    overlay_dir = overlay.path
    manifest = overlay.manifest
    tombstones = _read_jsonl_objects(overlay_dir / "tombstones.jsonl")
    nodes = _read_jsonl_objects(overlay_dir / "nodes.jsonl")
    edges = _read_jsonl_objects(overlay_dir / "edges.jsonl")
    _validate_pack_count(
        manifest.pack_id,
        "node_count",
        actual=len(nodes),
        expected=manifest.node_count,
    )
    _validate_pack_count(
        manifest.pack_id,
        "edge_count",
        actual=len(edges),
        expected=manifest.edge_count,
    )
    _validate_pack_count(
        manifest.pack_id,
        "tombstone_count",
        actual=len(tombstones),
        expected=manifest.tombstone_count,
    )
    for tombstone in tombstones:
        node_id = tombstone.get("node_id", tombstone.get("id"))
        if not isinstance(node_id, str) or not node_id:
            raise GraphPackManifestError(f"{overlay_dir} tombstone missing node_id")
        if node_id in graph:
            graph.remove_node(node_id)
    for raw_node in nodes:
        node_id = raw_node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise GraphPackManifestError(f"{overlay_dir} node overlay missing id")
        graph.add_node(node_id, **{key: value for key, value in raw_node.items() if key != "id"})
    for raw_edge in edges:
        _add_edge(graph, raw_edge, context=str(overlay_dir))


def _validate_pack_count(
    pack_id: str,
    field_name: str,
    *,
    actual: int,
    expected: int,
) -> None:
    if actual != expected:
        raise GraphPackManifestError(
            f"graph pack {pack_id} {field_name} mismatch: expected {expected}, got {actual}"
        )


def _add_edge(graph: nx.Graph, raw_edge: object, *, context: str) -> None:
    if not isinstance(raw_edge, dict):
        raise GraphPackManifestError(f"{context} contains non-object edge")
    source = raw_edge.get("source")
    target = raw_edge.get("target")
    if not isinstance(source, str) or not isinstance(target, str) or not source or not target:
        raise GraphPackManifestError(f"{context} contains edge without source/target")
    if source not in graph or target not in graph:
        raise GraphPackManifestError(f"{context} contains edge with unknown endpoint")
    graph.add_edge(
        source,
        target,
        **{key: value for key, value in raw_edge.items() if key not in {"source", "target"}},
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GraphPackManifestError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphPackManifestError(f"{path} did not contain a JSON object")
    return payload


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
            raise GraphPackManifestError(f"{path} line {lineno} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise GraphPackManifestError(f"{path} line {lineno} did not contain a JSON object")
        rows.append(payload)
    return rows


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GraphPackManifestError(f"graph pack manifest {key} must be a non-empty string")
    return value


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GraphPackManifestError(f"graph pack manifest {key} must be a string or null")
    return value


def _nonnegative_int(payload: dict[str, Any], key: str, *, default: int | None = None) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or value < 0:
        raise GraphPackManifestError(f"graph pack manifest {key} must be a non-negative integer")
    return value


def _checksums(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise GraphPackManifestError("graph pack manifest checksums must be an object")
    result: dict[str, str] = {}
    for raw_name, raw_digest in value.items():
        if not isinstance(raw_name, str):
            raise GraphPackManifestError("graph pack manifest checksum names must be strings")
        name = _normalise_artifact_name(raw_name)
        if not isinstance(raw_digest, str) or not _SHA256_RE.match(raw_digest):
            raise GraphPackManifestError(
                f"graph pack manifest checksum for {name} must be a SHA-256 hex digest"
            )
        result[name] = raw_digest
    return result


def _normalise_artifact_name(name: str) -> str:
    normalised = name.replace("\\", "/").strip()
    _validate_relative_manifest_name(normalised, "artifact name")
    return normalised


def _validate_relative_manifest_name(value: str, label: str) -> None:
    path = Path(value)
    if path.is_absolute() or value.startswith(("/", "\\")):
        raise GraphPackManifestError(f"graph pack manifest {label} must be relative")
    parts = value.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GraphPackManifestError(f"graph pack manifest {label} is unsafe")


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
    raise GraphPackManifestError("could not allocate graph packs rollback directory")


if __name__ == "__main__":  # pragma: no cover - exercised through main() tests.
    raise SystemExit(main())
