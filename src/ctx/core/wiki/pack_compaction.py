"""Coordinated graph/wiki pack compaction.

This module stages a new immutable graph base pack and matching wiki base pack
from the active base+overlay sets. Promotion remains a separate step so callers
can validate both staged artifacts before replacing the active packs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ctx.core.graph.graph_packs import (
    GraphPackEntry,
    GraphPackManifest,
    GraphPackManifestError,
    GraphPackPromotion,
    compact_graph_packs,
    discover_pack_manifests,
    load_merged_pack_graph,
    promote_graph_pack_set,
)
from ctx.core.graph.graph_store import ensure_graph_store
from ctx.core.wiki.wiki_packs import (
    WikiPackEntry,
    WikiPackManifest,
    WikiPackManifestError,
    WikiPackPromotion,
    compact_wiki_packs,
    discover_wiki_pack_manifests,
    load_merged_wiki_pages,
    promote_wiki_pack_set,
)
from ctx.core.wiki.pack_validation import (
    PACK_COMPACTION_MANIFEST,
    PACK_COMPACTION_SCHEMA_VERSION,
    validate_graph_wiki_consistency,
    validate_pack_compaction_manifest,
)
from ctx.utils._fs_utils import atomic_write_text


class PackCompactionError(ValueError):
    """Raised when coordinated graph/wiki pack compaction cannot be staged."""


@dataclass(frozen=True)
class PackCompactionResult:
    """Staged graph/wiki compaction result."""

    wiki_path: Path
    staging_dir: Path
    graph_packs_dir: Path
    wiki_packs_dir: Path
    staged_graph_packs_dir: Path
    staged_wiki_packs_dir: Path
    manifest_path: Path
    graph_manifest: GraphPackManifest
    wiki_manifest: WikiPackManifest

    def to_mapping(self) -> dict[str, object]:
        """Return deterministic JSON-serialisable compaction metadata."""
        return {
            "schema_version": PACK_COMPACTION_SCHEMA_VERSION,
            "operation": "pack-compaction-stage",
            "wiki_path": str(self.wiki_path),
            "staging_dir": str(self.staging_dir),
            "graph_packs_dir": str(self.graph_packs_dir),
            "wiki_packs_dir": str(self.wiki_packs_dir),
            "staged_graph_packs_dir": str(self.staged_graph_packs_dir),
            "staged_wiki_packs_dir": str(self.staged_wiki_packs_dir),
            "manifest_path": str(self.manifest_path),
            "base_export_id": self.graph_manifest.base_export_id,
            "graph": self.graph_manifest.to_mapping(),
            "wiki": self.wiki_manifest.to_mapping(),
        }


@dataclass(frozen=True)
class PackPromotionResult:
    """Coordinated graph/wiki pack promotion result."""

    wiki_path: Path
    graph: GraphPackPromotion
    wiki: WikiPackPromotion
    graph_store: dict[str, bool | int] | None = None

    def to_mapping(self) -> dict[str, object]:
        """Return deterministic JSON-serialisable promotion metadata."""
        return {
            "wiki_path": str(self.wiki_path),
            "graph": self.graph.to_mapping(),
            "wiki": self.wiki.to_mapping(),
            "graph_store": self.graph_store,
        }


def pack_compaction_status(
    *,
    wiki_path: Path,
    overlay_threshold: int | None = None,
    validate: bool = True,
) -> dict[str, object]:
    """Return read-only operational status for active graph/wiki pack sets."""
    threshold = _normalise_overlay_threshold(
        overlay_threshold if overlay_threshold is not None else _default_overlay_threshold()
    )
    wiki_root = Path(wiki_path)
    graph_packs_dir = wiki_root / "graphify-out" / "packs"
    wiki_packs_dir = wiki_root / "wiki-packs"
    try:
        graph_entries = discover_pack_manifests(graph_packs_dir)
        wiki_entries = discover_wiki_pack_manifests(wiki_packs_dir)
    except (GraphPackManifestError, WikiPackManifestError) as exc:
        raise PackCompactionError(str(exc)) from exc

    graph_overlays = _overlay_count(graph_entries)
    wiki_overlays = _overlay_count(wiki_entries)
    max_overlays = max(graph_overlays, wiki_overlays)
    validation_result: dict[str, object] | None = None
    if validate and graph_entries and wiki_entries:
        validation_result = validate_pack_sets(
            graph_packs_dir=graph_packs_dir,
            wiki_packs_dir=wiki_packs_dir,
        )

    graph_base_export_id = graph_entries[0].manifest.base_export_id if graph_entries else None
    wiki_base_export_id = wiki_entries[0].manifest.base_export_id if wiki_entries else None
    base_export_id = graph_base_export_id if graph_base_export_id == wiki_base_export_id else None
    can_compact_now = bool(
        graph_entries
        and wiki_entries
        and graph_overlays > 0
        and wiki_overlays > 0
        and base_export_id is not None
    )
    return {
        "wiki_path": str(wiki_root),
        "graph_packs_dir": str(graph_packs_dir),
        "wiki_packs_dir": str(wiki_packs_dir),
        "base_export_id": base_export_id,
        "graph_base_export_id": graph_base_export_id,
        "wiki_base_export_id": wiki_base_export_id,
        "graph_pack_ids": [entry.manifest.pack_id for entry in graph_entries],
        "wiki_pack_ids": [entry.manifest.pack_id for entry in wiki_entries],
        "graph_pack_count": len(graph_entries),
        "wiki_pack_count": len(wiki_entries),
        "graph_overlay_count": graph_overlays,
        "wiki_overlay_count": wiki_overlays,
        "max_overlay_count": max_overlays,
        "overlay_threshold": threshold,
        "needs_compaction": max_overlays >= threshold,
        "can_compact_now": can_compact_now,
        "validation": validation_result,
    }


def compact_active_pack_sets(
    *,
    wiki_path: Path,
    base_export_id: str,
    staging_dir: Path | None = None,
    graph_config_hash: str | None = None,
    graph_model_id: str | None = None,
    created_at: str | None = None,
) -> PackCompactionResult:
    """Stage matching compacted graph and wiki base packs.

    The active pack directories are not mutated. Staged roots are validated
    before returning so a successful result is promotable by construction.
    """
    if not base_export_id.strip():
        raise PackCompactionError("base_export_id must be non-empty")
    wiki_root = Path(wiki_path)
    graph_packs_dir = wiki_root / "graphify-out" / "packs"
    wiki_packs_dir = wiki_root / "wiki-packs"
    stage_root = (
        Path(staging_dir)
        if staging_dir is not None
        else (wiki_root / "graphify-out" / "pack-compaction-staging" / _pack_id(base_export_id))
    )
    if stage_root.exists():
        raise PackCompactionError(f"staging directory already exists: {stage_root}")

    staged_graph_packs_dir = stage_root / "graph-packs"
    staged_wiki_packs_dir = stage_root / "wiki-packs"
    manifest_path = stage_root / PACK_COMPACTION_MANIFEST
    pack_id = _pack_id(base_export_id)
    try:
        graph_manifest = compact_graph_packs(
            packs_dir=graph_packs_dir,
            compacted_pack_dir=staged_graph_packs_dir / pack_id,
            base_export_id=base_export_id,
            config_hash=graph_config_hash,
            model_id=graph_model_id,
            created_at=created_at,
        )
        wiki_manifest = compact_wiki_packs(
            packs_dir=wiki_packs_dir,
            compacted_pack_dir=staged_wiki_packs_dir / pack_id,
            base_export_id=base_export_id,
            created_at=created_at,
        )
        result = PackCompactionResult(
            wiki_path=wiki_root,
            staging_dir=stage_root,
            graph_packs_dir=graph_packs_dir,
            wiki_packs_dir=wiki_packs_dir,
            staged_graph_packs_dir=staged_graph_packs_dir,
            staged_wiki_packs_dir=staged_wiki_packs_dir,
            manifest_path=manifest_path,
            graph_manifest=graph_manifest,
            wiki_manifest=wiki_manifest,
        )
        _write_compaction_manifest(result, created_at=created_at)
        _validate_staged_pack_roots(staged_graph_packs_dir, staged_wiki_packs_dir)
    except (GraphPackManifestError, WikiPackManifestError, PackCompactionError, OSError) as exc:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise PackCompactionError(str(exc)) from exc

    return result


def promote_staged_pack_sets(
    *,
    wiki_path: Path,
    staged_graph_packs_dir: Path,
    staged_wiki_packs_dir: Path,
    graph_backup_packs_dir: Path | None = None,
    wiki_backup_packs_dir: Path | None = None,
    refresh_graph_store: bool = True,
    graph_store_db_path: Path | None = None,
) -> PackPromotionResult:
    """Promote staged graph/wiki pack sets into the active wiki.

    Both staged roots are validated before any active directory is touched. If
    graph promotion succeeds but wiki promotion fails, the previous graph pack
    directory is restored from the graph backup.
    """
    wiki_root = Path(wiki_path)
    graph_stage = Path(staged_graph_packs_dir)
    wiki_stage = Path(staged_wiki_packs_dir)
    active_graph_packs = wiki_root / "graphify-out" / "packs"
    active_wiki_packs = wiki_root / "wiki-packs"
    _validate_staged_pack_roots(graph_stage, wiki_stage)

    graph_result: GraphPackPromotion | None = None
    try:
        graph_result = promote_graph_pack_set(
            staged_packs_dir=graph_stage,
            active_packs_dir=active_graph_packs,
            backup_packs_dir=Path(graph_backup_packs_dir) if graph_backup_packs_dir else None,
        )
        wiki_result = promote_wiki_pack_set(
            staged_packs_dir=wiki_stage,
            active_packs_dir=active_wiki_packs,
            backup_packs_dir=Path(wiki_backup_packs_dir) if wiki_backup_packs_dir else None,
        )
    except (GraphPackManifestError, WikiPackManifestError, OSError) as exc:
        if graph_result is not None:
            _restore_graph_packs_after_partial_promotion(graph_result)
        raise PackCompactionError(str(exc)) from exc

    graph_store = None
    if refresh_graph_store:
        try:
            graph_store = ensure_graph_store(
                wiki_root / "graphify-out",
                Path(graph_store_db_path)
                if graph_store_db_path
                else _default_graph_store_db(wiki_root),
            )
        except (OSError, ValueError) as exc:
            raise PackCompactionError(f"graph store refresh failed: {exc}") from exc

    return PackPromotionResult(
        wiki_path=wiki_root,
        graph=graph_result,
        wiki=wiki_result,
        graph_store=graph_store,
    )


def validate_pack_sets(
    *,
    graph_packs_dir: Path,
    wiki_packs_dir: Path,
    require_compaction_manifest: bool = False,
) -> dict[str, object]:
    """Validate merged graph/wiki packs without staging or promotion."""
    graph_dir = Path(graph_packs_dir)
    wiki_dir = Path(wiki_packs_dir)
    try:
        if require_compaction_manifest:
            validate_pack_compaction_manifest(
                staged_graph_packs_dir=graph_dir,
                staged_wiki_packs_dir=wiki_dir,
            )
        graph = load_merged_pack_graph(graph_dir)
        pages = load_merged_wiki_pages(wiki_dir)
    except (GraphPackManifestError, WikiPackManifestError, ValueError) as exc:
        raise PackCompactionError(str(exc)) from exc

    errors: list[str] = []
    if graph.number_of_nodes() == 0:
        errors.append("graph packs do not contain a graph")
    if not pages:
        errors.append("wiki packs do not contain pages")
    consistency = validate_graph_wiki_consistency(graph, pages)
    errors.extend(consistency.errors())
    if errors:
        raise PackCompactionError("graph/wiki pack validation failed: " + "; ".join(errors))

    pack_ids = graph.graph.get("ctx_pack_ids", [])
    return {
        "graph_packs_dir": str(graph_dir),
        "wiki_packs_dir": str(wiki_dir),
        "graph_nodes": graph.number_of_nodes(),
        "graph_edges": graph.number_of_edges(),
        "wiki_pages": len(pages),
        "graph_pack_ids": pack_ids if isinstance(pack_ids, list) else [],
        "base_export_id": graph.graph.get("ctx_pack_base_export_id"),
        "missing_wiki_pages": len(consistency.missing_wiki_pages),
        "orphan_wiki_pages": len(consistency.orphan_wiki_pages),
        "stale_wiki_links": len(consistency.stale_wiki_links),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI for staging coordinated graph/wiki pack compaction."""
    parser = argparse.ArgumentParser(
        prog="python -m ctx.core.wiki.pack_compaction",
        description="Stage compacted ctx graph and LLM-wiki base packs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser(
        "status",
        help="Report active graph/wiki overlay counts and compaction readiness.",
    )
    status.add_argument("--wiki-path", required=True, help="Path to the ctx wiki root")
    status.add_argument(
        "--overlay-threshold",
        type=int,
        help="Override graph.pack_compaction.overlay_threshold for this check",
    )
    status.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip merged graph/wiki validation and report counts only",
    )
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    compact = sub.add_parser(
        "compact",
        help="Stage compacted graph/wiki base packs without mutating active packs.",
    )
    compact.add_argument("--wiki-path", required=True, help="Path to the ctx wiki root")
    compact.add_argument("--base-export-id", required=True, help="New compacted export id")
    compact.add_argument("--staging-dir", help="Destination staging root")
    compact.add_argument("--graph-config-hash", help="Override graph config hash")
    compact.add_argument("--graph-model-id", help="Override graph model id")
    compact.add_argument("--created-at", help="Optional created_at value for staged manifests")
    compact.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    compact_promote = sub.add_parser(
        "compact-promote",
        help="Stage, validate, promote, and refresh graph store in one operation.",
    )
    compact_promote.add_argument("--wiki-path", required=True, help="Path to the ctx wiki root")
    compact_promote.add_argument("--base-export-id", required=True, help="New compacted export id")
    compact_promote.add_argument("--staging-dir", help="Destination staging root")
    compact_promote.add_argument("--graph-config-hash", help="Override graph config hash")
    compact_promote.add_argument("--graph-model-id", help="Override graph model id")
    compact_promote.add_argument(
        "--created-at", help="Optional created_at value for staged manifests"
    )
    compact_promote.add_argument("--graph-backup-packs-dir", help="Optional graph backup directory")
    compact_promote.add_argument("--wiki-backup-packs-dir", help="Optional wiki backup directory")
    compact_promote.add_argument("--graph-store-db", help="Optional SQLite graph store path")
    compact_promote.add_argument(
        "--no-graph-store-refresh",
        action="store_true",
        help="Skip SQLite graph store refresh after pack promotion",
    )
    compact_promote.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    promote = sub.add_parser(
        "promote",
        help="Promote validated staged graph/wiki packs into the active wiki.",
    )
    promote.add_argument("--wiki-path", required=True, help="Path to the ctx wiki root")
    promote.add_argument(
        "--staged-graph-packs-dir",
        required=True,
        help="Validated staged graph packs root",
    )
    promote.add_argument(
        "--staged-wiki-packs-dir",
        required=True,
        help="Validated staged wiki packs root",
    )
    promote.add_argument("--graph-backup-packs-dir", help="Optional graph backup directory")
    promote.add_argument("--wiki-backup-packs-dir", help="Optional wiki backup directory")
    promote.add_argument("--graph-store-db", help="Optional SQLite graph store path")
    promote.add_argument(
        "--no-graph-store-refresh",
        action="store_true",
        help="Skip SQLite graph store refresh after pack promotion",
    )
    promote.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    validate = sub.add_parser(
        "validate",
        help="Validate active or staged graph/wiki packs without mutating them.",
    )
    validate.add_argument("--wiki-path", help="Path to the ctx wiki root for active packs")
    validate.add_argument("--staged-graph-packs-dir", help="Staged graph packs root")
    validate.add_argument("--staged-wiki-packs-dir", help="Staged wiki packs root")
    validate.add_argument(
        "--require-compaction-manifest",
        action="store_true",
        help="Require and validate pack-compaction-manifest.json beside staged roots",
    )
    validate.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    if args.command == "status":
        try:
            status_result = pack_compaction_status(
                wiki_path=Path(args.wiki_path),
                overlay_threshold=args.overlay_threshold,
                validate=not args.no_validate,
            )
        except PackCompactionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(status_result, indent=2, sort_keys=True))
        else:
            state = "recommended" if status_result["needs_compaction"] else "not needed"
            print(
                "graph/wiki pack compaction status: "
                f"{status_result['max_overlay_count']} overlays "
                f"(threshold {status_result['overlay_threshold']}); "
                f"compaction {state}"
            )
        return 0
    if args.command == "compact":
        try:
            compact_result = compact_active_pack_sets(
                wiki_path=Path(args.wiki_path),
                base_export_id=args.base_export_id,
                staging_dir=Path(args.staging_dir) if args.staging_dir else None,
                graph_config_hash=args.graph_config_hash,
                graph_model_id=args.graph_model_id,
                created_at=args.created_at,
            )
        except PackCompactionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        payload = compact_result.to_mapping()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                "staged graph/wiki compaction: "
                f"{compact_result.graph_manifest.node_count} graph nodes, "
                f"{compact_result.graph_manifest.edge_count} graph edges, "
                f"{compact_result.wiki_manifest.page_count} wiki pages"
            )
        return 0
    if args.command == "compact-promote":
        try:
            compact_result = compact_active_pack_sets(
                wiki_path=Path(args.wiki_path),
                base_export_id=args.base_export_id,
                staging_dir=Path(args.staging_dir) if args.staging_dir else None,
                graph_config_hash=args.graph_config_hash,
                graph_model_id=args.graph_model_id,
                created_at=args.created_at,
            )
            promotion_result = promote_staged_pack_sets(
                wiki_path=Path(args.wiki_path),
                staged_graph_packs_dir=compact_result.staged_graph_packs_dir,
                staged_wiki_packs_dir=compact_result.staged_wiki_packs_dir,
                graph_backup_packs_dir=(
                    Path(args.graph_backup_packs_dir) if args.graph_backup_packs_dir else None
                ),
                wiki_backup_packs_dir=(
                    Path(args.wiki_backup_packs_dir) if args.wiki_backup_packs_dir else None
                ),
                refresh_graph_store=not args.no_graph_store_refresh,
                graph_store_db_path=Path(args.graph_store_db) if args.graph_store_db else None,
            )
        except PackCompactionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        payload = {
            "compaction": compact_result.to_mapping(),
            "promotion": promotion_result.to_mapping(),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                "compacted and promoted graph/wiki packs: "
                f"{', '.join(promotion_result.graph.promoted_pack_ids)} / "
                f"{', '.join(promotion_result.wiki.promoted_pack_ids)}"
            )
        return 0
    if args.command == "promote":
        try:
            promotion_result = promote_staged_pack_sets(
                wiki_path=Path(args.wiki_path),
                staged_graph_packs_dir=Path(args.staged_graph_packs_dir),
                staged_wiki_packs_dir=Path(args.staged_wiki_packs_dir),
                graph_backup_packs_dir=(
                    Path(args.graph_backup_packs_dir) if args.graph_backup_packs_dir else None
                ),
                wiki_backup_packs_dir=(
                    Path(args.wiki_backup_packs_dir) if args.wiki_backup_packs_dir else None
                ),
                refresh_graph_store=not args.no_graph_store_refresh,
                graph_store_db_path=Path(args.graph_store_db) if args.graph_store_db else None,
            )
        except PackCompactionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        payload = promotion_result.to_mapping()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                "promoted graph/wiki packs: "
                f"{', '.join(promotion_result.graph.promoted_pack_ids)} / "
                f"{', '.join(promotion_result.wiki.promoted_pack_ids)}"
            )
        return 0
    if args.command == "validate":
        try:
            if args.staged_graph_packs_dir or args.staged_wiki_packs_dir:
                if not args.staged_graph_packs_dir or not args.staged_wiki_packs_dir:
                    parser.error(
                        "--staged-graph-packs-dir and --staged-wiki-packs-dir are required together"
                    )
                graph_packs_dir = Path(args.staged_graph_packs_dir)
                wiki_packs_dir = Path(args.staged_wiki_packs_dir)
            elif args.wiki_path:
                wiki_root = Path(args.wiki_path)
                graph_packs_dir = wiki_root / "graphify-out" / "packs"
                wiki_packs_dir = wiki_root / "wiki-packs"
            else:
                parser.error("validate requires --wiki-path or both staged pack dirs")
            validation_result = validate_pack_sets(
                graph_packs_dir=graph_packs_dir,
                wiki_packs_dir=wiki_packs_dir,
                require_compaction_manifest=args.require_compaction_manifest,
            )
        except PackCompactionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(validation_result, indent=2, sort_keys=True))
        else:
            print(
                "validated graph/wiki packs: "
                f"{validation_result['graph_nodes']} graph nodes, "
                f"{validation_result['graph_edges']} graph edges, "
                f"{validation_result['wiki_pages']} wiki pages"
            )
        return 0
    return 1


def _pack_id(base_export_id: str) -> str:
    value = base_export_id.strip()
    return value if value.startswith("base-") else f"base-{value}"


def _default_overlay_threshold() -> int:
    from ctx_config import cfg  # noqa: PLC0415

    return int(cfg.graph_pack_compaction_overlay_threshold)


def _normalise_overlay_threshold(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PackCompactionError(f"overlay_threshold must be an integer >= 1 (got {value!r})")
    return value


def _overlay_count(entries: Iterable[GraphPackEntry | WikiPackEntry]) -> int:
    return sum(1 for entry in entries if entry.manifest.pack_type == "overlay")


def _default_graph_store_db(wiki_path: Path) -> Path:
    return wiki_path / "graphify-out" / "graph-store.sqlite3"


def _write_compaction_manifest(
    result: PackCompactionResult,
    *,
    created_at: str | None,
) -> None:
    payload = result.to_mapping()
    payload["created_at"] = created_at or datetime.now(UTC).isoformat()
    atomic_write_text(
        result.manifest_path,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _validate_staged_pack_roots(
    staged_graph_packs_dir: Path,
    staged_wiki_packs_dir: Path,
) -> None:
    validate_pack_sets(
        graph_packs_dir=staged_graph_packs_dir,
        wiki_packs_dir=staged_wiki_packs_dir,
        require_compaction_manifest=True,
    )


def _restore_graph_packs_after_partial_promotion(result: GraphPackPromotion) -> None:
    active = result.active_packs_dir
    backup = result.backup_packs_dir
    _remove_path(active)
    if backup is not None and backup.exists():
        backup.replace(active)


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
