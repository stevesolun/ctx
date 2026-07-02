"""Worker for durable wiki maintenance queue jobs."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from ctx.core.graph.entity_overlays import append_overlay_tombstone
from ctx.core.graph.graph_packs import (
    GRAPH_PACK_MANIFEST,
    GraphPackManifestError,
    discover_pack_manifests,
    write_overlay_pack,
)
from ctx.core.graph.graph_store import ensure_graph_store
from ctx.core.graph.incremental_attach import attach_entity
from ctx.core.wiki.artifact_promotion import promote_staged_artifact
from ctx.core.wiki import wiki_queue
from ctx.core.wiki.pack_compaction import (
    compact_active_pack_sets,
    pack_compaction_status,
    promote_staged_pack_sets,
)
from ctx.core.wiki.wiki_packs import load_merged_wiki_pages, write_active_wiki_overlay_pack
from ctx.core.wiki.wiki_sync import update_index
from ctx.utils._fs_utils import reject_symlink_path
from ctx_config import cfg

_ENTITY_SUBJECT_TYPES = {
    "skill": "skills",
    "agent": "agents",
    "mcp-server": "mcp-servers",
    "harness": "harnesses",
}
_DEFAULT_ATTACH_MIN_FINAL_WEIGHT = 0.03
_VECTOR_INDEX_META_NAME = "vector-index.meta.json"
MaintenanceHandler = Callable[[Path, dict[str, Any]], str]


@dataclass(frozen=True)
class ProcessResult:
    job_id: int
    kind: str
    status: str
    message: str


@dataclass(frozen=True)
class _AttachOutcome:
    message: str
    graph_pack_attached: bool = False


def process_next(
    wiki_path: Path,
    *,
    worker_id: str,
    lease_seconds: float = 60.0,
    retry_delay_seconds: float = 5.0,
    now: float | None = None,
) -> ProcessResult | None:
    """Lease and process one ready wiki maintenance job."""
    db_path = wiki_queue.queue_db_path(wiki_path)
    job = wiki_queue.lease_next(
        db_path,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        kinds=wiki_queue.WORKER_JOB_KINDS,
        now=now,
    )
    if job is None:
        return None

    try:
        message = _process_job(wiki_path, job)
    except Exception as exc:  # noqa: BLE001 - failures are persisted into queue state.
        failed = wiki_queue.mark_failed(
            db_path,
            job.id,
            worker_id=worker_id,
            error=str(exc),
            retry=True,
            delay_seconds=retry_delay_seconds,
            now=now,
        )
        return ProcessResult(
            job_id=failed.id,
            kind=failed.kind,
            status=failed.status,
            message=str(failed.last_error or exc),
        )

    succeeded = wiki_queue.mark_succeeded(db_path, job.id, worker_id=worker_id, now=now)
    return ProcessResult(
        job_id=succeeded.id,
        kind=succeeded.kind,
        status=succeeded.status,
        message=message,
    )


def drain_queue(
    wiki_path: Path,
    *,
    worker_id: str,
    limit: int | None = None,
    lease_seconds: float = 60.0,
    retry_delay_seconds: float = 5.0,
    now: float | None = None,
) -> list[ProcessResult]:
    """Process ready queue jobs until empty or *limit* is reached."""
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0 (got {limit})")
    results: list[ProcessResult] = []
    while limit is None or len(results) < limit:
        result = process_next(
            wiki_path,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_delay_seconds=retry_delay_seconds,
            now=now,
        )
        if result is None:
            break
        results.append(result)
    return results


def _process_job(wiki_path: Path, job: wiki_queue.QueueJob) -> str:
    if job.kind == wiki_queue.ENTITY_UPSERT_JOB:
        return _process_entity_upsert(wiki_path, job.payload)
    handler = MAINTENANCE_HANDLERS.get(job.kind)
    if handler is None:
        raise ValueError(f"unsupported wiki queue job kind: {job.kind}")
    return handler(wiki_path, job.payload)


def _process_entity_upsert(wiki_path: Path, payload: dict[str, Any]) -> str:
    entity_type = _required_string(payload, "entity_type")
    slug = _required_string(payload, "slug")
    action = str(payload.get("action", "upsert")).strip() or "upsert"
    expected_hash = _required_string(payload, "content_hash")
    subject_type = _ENTITY_SUBJECT_TYPES.get(entity_type)
    if subject_type is None:
        raise ValueError(f"unsupported entity_type for entity-upsert: {entity_type}")

    entity_path = _resolve_entity_path(wiki_path, _required_string(payload, "entity_path"))
    if action == "delete":
        node_id = f"{entity_type}:{slug}"
        append_overlay_tombstone(
            wiki_path / "graphify-out" / "entity-overlays.jsonl",
            node_id=node_id,
            source="entity-delete",
        )
        _emit_wiki_page_tombstone(wiki_path, _wiki_relative_path(wiki_path, entity_path))
        if _try_graph_pack_tombstone(wiki_path, node_id):
            wiki_queue.enqueue_maintenance_job(
                wiki_path,
                kind=wiki_queue.GRAPH_STORE_REFRESH_JOB,
                payload={},
                source="entity-delete",
            )
            suffix = _pack_compaction_suffix_if_due(wiki_path)
            return f"queued graph store refresh for deleted {subject_type} entity {slug}{suffix}"
        else:
            wiki_queue.enqueue_maintenance_job(
                wiki_path,
                kind=wiki_queue.GRAPH_EXPORT_JOB,
                payload={"graph_only": True, "incremental": False},
                source="entity-delete",
            )
            return f"queued full graph refresh for deleted {subject_type} entity {slug}"

    page_relpath = _wiki_relative_path(wiki_path, entity_path)
    text = _read_entity_text(wiki_path, entity_path, page_relpath)
    actual_hash = sha256(text.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(
            "content hash mismatch for "
            f"{entity_type}:{slug}: expected {expected_hash}, got {actual_hash}"
        )

    update_index(str(wiki_path), [slug], subject_type=subject_type)
    _emit_wiki_page_upsert(wiki_path, page_relpath, text)
    attach_outcome = _try_incremental_attach(
        wiki_path=wiki_path,
        entity_type=entity_type,
        slug=slug,
        entity_path=entity_path,
        text=text,
    )
    if attach_outcome.graph_pack_attached:
        wiki_queue.enqueue_maintenance_job(
            wiki_path,
            kind=wiki_queue.GRAPH_STORE_REFRESH_JOB,
            payload={},
            source="entity-upsert",
        )
        suffix = _pack_compaction_suffix_if_due(wiki_path)
    else:
        wiki_queue.enqueue_maintenance_job(
            wiki_path,
            kind=wiki_queue.GRAPH_EXPORT_JOB,
            payload={"graph_only": True, "incremental": True},
            source="entity-upsert",
        )
        suffix = ""
    return f"refreshed {subject_type} index for {slug}; {attach_outcome.message}{suffix}"


def _pack_compaction_suffix_if_due(wiki_path: Path) -> str:
    return "; queued pack compaction" if _enqueue_pack_compaction_if_due(wiki_path) else ""


def _enqueue_pack_compaction_if_due(wiki_path: Path) -> bool:
    threshold = int(cfg.graph_pack_compaction_overlay_threshold)
    try:
        status = pack_compaction_status(
            wiki_path=wiki_path,
            overlay_threshold=threshold,
            validate=False,
        )
        if not (bool(status.get("needs_compaction")) and bool(status.get("can_compact_now"))):
            return False
        wiki_queue.enqueue_maintenance_job(
            wiki_path,
            kind=wiki_queue.PACK_COMPACTION_JOB,
            payload={"overlay_threshold": threshold},
            source="pack-threshold",
        )
    except Exception:  # noqa: BLE001 - compaction is derived maintenance, not source of truth.
        return False
    return True


def _emit_wiki_page_upsert(wiki_path: Path, relpath: str, text: str) -> None:
    write_active_wiki_overlay_pack(
        packs_dir=wiki_path / "wiki-packs",
        pages={relpath: text},
        tombstones=[],
    )


def _emit_wiki_page_tombstone(wiki_path: Path, relpath: str) -> None:
    write_active_wiki_overlay_pack(
        packs_dir=wiki_path / "wiki-packs",
        pages={},
        tombstones=[relpath],
    )


def _read_entity_text(wiki_path: Path, entity_path: Path, relpath: str) -> str:
    packs_dir = wiki_path / "wiki-packs"
    if packs_dir.is_dir():
        pages = load_merged_wiki_pages(packs_dir)
        if relpath in pages:
            return pages[relpath]
    return entity_path.read_text(encoding="utf-8")


def _try_graph_pack_tombstone(wiki_path: Path, node_id: str) -> bool:
    packs_dir = wiki_path / "graphify-out" / "packs"
    try:
        entries = discover_pack_manifests(packs_dir)
    except GraphPackManifestError:
        return False
    if not entries:
        return False
    base = entries[0].manifest
    node_hash = sha256(node_id.encode("utf-8")).hexdigest()[:16]
    pack_id = f"overlay-delete-{node_hash}"
    pack_dir = packs_dir / pack_id
    if (pack_dir / GRAPH_PACK_MANIFEST).is_file():
        return True
    write_overlay_pack(
        pack_dir=pack_dir,
        pack_id=pack_id,
        base_export_id=base.base_export_id,
        parent_export_id=base.base_export_id,
        config_hash=base.config_hash,
        model_id=base.model_id,
        nodes=[],
        edges=[],
        tombstones=[{"node_id": node_id, "source": "entity-delete"}],
    )
    return True


def _wiki_relative_path(wiki_path: Path, entity_path: Path) -> str:
    return entity_path.relative_to(Path(wiki_path).resolve()).as_posix()


def _resolve_entity_path(wiki_path: Path, raw_path: str) -> Path:
    wiki_root = Path(wiki_path).resolve()
    candidate_path = Path(raw_path)
    candidate = (
        candidate_path.resolve()
        if candidate_path.is_absolute()
        else (wiki_root / candidate_path).resolve()
    )
    if not candidate.is_relative_to(wiki_root):
        raise ValueError(f"entity_path escapes wiki root: {raw_path}")
    reject_symlink_path(candidate)
    return candidate


def _try_incremental_attach(
    *,
    wiki_path: Path,
    entity_type: str,
    slug: str,
    entity_path: Path,
    text: str,
) -> _AttachOutcome:
    node_id = f"{entity_type}:{slug}"
    index_dir = _semantic_vector_index_dir(wiki_path)
    if not (index_dir / _VECTOR_INDEX_META_NAME).is_file():
        node_pack_status = _try_graph_pack_node_upsert(
            wiki_path=wiki_path,
            node_id=node_id,
            entity_type=entity_type,
            slug=slug,
            text=text,
        )
        if node_pack_status:
            return _AttachOutcome(
                f"incremental attach skipped (no vector index); "
                f"node overlay pack {node_pack_status}",
                graph_pack_attached=True,
            )
        return _AttachOutcome("incremental attach skipped (no vector index)")
    try:
        result = attach_entity(
            index_dir=index_dir,
            overlay_path=wiki_path / "graphify-out" / "entity-overlays.jsonl",
            node_id=node_id,
            entity_type=entity_type,
            label=slug,
            tags=_extract_frontmatter_tags(text),
            text=text,
            vector_json=None,
            model_id=None,
            top_k=int(cfg.graph_semantic_top_k),
            min_score=float(cfg.graph_semantic_build_floor),
            min_final_weight=_DEFAULT_ATTACH_MIN_FINAL_WEIGHT,
            delta_index_dirs=_semantic_vector_delta_index_dirs(wiki_path),
            delta_index_write_dir=_semantic_vector_delta_write_dir(
                wiki_path,
                entity_type,
            ),
            **_graph_pack_attach_kwargs(wiki_path),
        )
    except Exception as exc:  # noqa: BLE001 - attach is derived, not source of truth.
        node_pack_status = _try_graph_pack_node_upsert(
            wiki_path=wiki_path,
            node_id=node_id,
            entity_type=entity_type,
            slug=slug,
            text=text,
        )
        if node_pack_status:
            return _AttachOutcome(
                f"incremental attach skipped ({exc}); node overlay pack {node_pack_status}",
                graph_pack_attached=True,
            )
        return _AttachOutcome(f"incremental attach skipped ({exc})")
    status = result.get("status", "unknown")
    overlay_pack = result.get("overlay_pack")
    if isinstance(overlay_pack, dict):
        pack_status = overlay_pack.get("status", "unknown")
        return _AttachOutcome(
            f"incremental attach {status}; overlay pack {pack_status}",
            graph_pack_attached=True,
        )
    return _AttachOutcome(f"incremental attach {status}")


def _try_graph_pack_node_upsert(
    *,
    wiki_path: Path,
    node_id: str,
    entity_type: str,
    slug: str,
    text: str,
) -> str | None:
    packs_dir = wiki_path / "graphify-out" / "packs"
    try:
        entries = discover_pack_manifests(packs_dir)
    except GraphPackManifestError:
        return None
    if not entries:
        return None
    base = entries[0].manifest
    content_hash = sha256(text.encode("utf-8")).hexdigest()
    pack_hash = sha256(f"{node_id}:{content_hash}".encode("utf-8")).hexdigest()[:16]
    pack_id = f"overlay-node-{pack_hash}"
    pack_dir = packs_dir / pack_id
    if (pack_dir / GRAPH_PACK_MANIFEST).is_file():
        return "unchanged"
    write_overlay_pack(
        pack_dir=pack_dir,
        pack_id=pack_id,
        base_export_id=base.base_export_id,
        parent_export_id=base.base_export_id,
        config_hash=base.config_hash,
        model_id=base.model_id,
        nodes=[
            {
                "id": node_id,
                "label": slug,
                "title": slug,
                "type": entity_type,
                "tags": _extract_frontmatter_tags(text),
                "source": "entity-upsert",
                "content_hash": content_hash,
            }
        ],
        edges=[],
        tombstones=[{"node_id": node_id, "source": "entity-upsert"}],
    )
    return "inserted"


def _graph_pack_attach_kwargs(wiki_path: Path) -> dict[str, Any]:
    packs_dir = wiki_path / "graphify-out" / "packs"
    try:
        entries = discover_pack_manifests(packs_dir)
    except GraphPackManifestError:
        return {}
    if not entries:
        return {}
    base = entries[0].manifest
    return {
        "pack_root": packs_dir,
        "base_export_id": base.base_export_id,
        "parent_export_id": base.base_export_id,
        "config_hash": base.config_hash,
    }


def _semantic_vector_index_dir(wiki_path: Path) -> Path:
    configured = Path(cfg.graph_semantic_cache_dir).expanduser()
    default_cache = Path(os.path.expanduser("~/.claude/skill-wiki/.embedding-cache/graph"))
    try:
        wiki_resolved = Path(wiki_path).expanduser().resolve()
        cfg_wiki_resolved = Path(cfg.wiki_dir).expanduser().resolve()
        configured_resolved = configured.resolve()
        default_resolved = default_cache.resolve()
    except OSError:
        return configured / "vector-index"
    if wiki_resolved != cfg_wiki_resolved and configured_resolved == default_resolved:
        return Path(wiki_path) / ".embedding-cache" / "graph" / "vector-index"
    return configured / "vector-index"


def _semantic_vector_delta_index_dirs(wiki_path: Path) -> list[Path]:
    delta_root = _semantic_vector_index_dir(wiki_path).with_name("vector-index-deltas")
    if not delta_root.is_dir():
        return []
    return sorted(
        path
        for path in delta_root.iterdir()
        if path.is_dir() and (path / _VECTOR_INDEX_META_NAME).is_file()
    )


def _semantic_vector_delta_write_dir(wiki_path: Path, entity_type: str) -> Path:
    safe_type = (
        "".join(
            char if char.isalnum() or char in {"-", "_"} else "-" for char in entity_type
        ).strip("-_")
        or "entity"
    )
    return (
        _semantic_vector_index_dir(wiki_path).with_name("vector-index-deltas")
        / f"local-{safe_type}"
    )


def _extract_frontmatter_tags(text: str) -> list[str]:
    if not text.startswith("---"):
        return []
    parts = text.split("---", 2)
    if len(parts) < 3:
        return []
    try:
        import yaml  # type: ignore[import-untyped]  # noqa: PLC0415

        parsed = yaml.safe_load(parts[1]) or {}
    except Exception:  # noqa: BLE001 - malformed metadata just means no tag hint.
        return []
    if not isinstance(parsed, dict):
        return []
    tags = parsed.get("tags")
    if isinstance(tags, str):
        values: list[Any] = tags.split(",")
    elif isinstance(tags, list):
        values = list(tags)
    else:
        return []
    return [str(tag).strip() for tag in values if str(tag).strip()]


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"entity-upsert payload requires non-empty {key}")
    return value.strip()


def _handle_graph_export(wiki_path: Path, payload: dict[str, Any]) -> str:
    args = [
        sys.executable,
        "-m",
        "ctx.core.wiki.wiki_graphify",
        "--wiki-dir",
        str(wiki_path),
    ]
    args.append("--full" if payload.get("incremental") is False else "--incremental")
    if payload.get("graph_only", True):
        args.append("--graph-only")
    if payload.get("dry_run"):
        args.append("--dry-run")
    _run_checked(args, label="graph export")
    return "graph export completed"


def _handle_graph_store_refresh(wiki_path: Path, payload: dict[str, Any]) -> str:
    graph_dir = wiki_path / "graphify-out"
    db_path = graph_dir / "graph-store.sqlite3"
    result = ensure_graph_store(
        graph_dir,
        db_path,
        apply_runtime_filter=not payload.get("no_runtime_filter", False),
    )
    action = "rebuilt" if result["rebuilt"] else "reused"
    return f"graph store {action}: {result['nodes']} nodes, {result['edges']} edges"


def _handle_catalog_refresh(_wiki_path: Path, payload: dict[str, Any]) -> str:
    args = _catalog_refresh_args(payload, update_wiki_tar=False)
    _run_checked(args, label="catalog refresh")
    return "catalog refresh completed"


def _handle_tar_refresh(_wiki_path: Path, payload: dict[str, Any]) -> str:
    args = _catalog_refresh_args(payload, update_wiki_tar=True)
    _run_checked(args, label="tar refresh")
    return "tar refresh completed"


def _handle_artifact_promotion(_wiki_path: Path, payload: dict[str, Any]) -> str:
    staged = Path(_required_payload_string(payload, "staged_path"))
    target = Path(_required_payload_string(payload, "target_path"))
    validator = payload.get("validator")
    validate = None
    if validator == "wiki-tar":
        from import_skills_sh_catalog import _validate_wiki_tarball_candidate  # noqa: PLC0415

        validate = _validate_wiki_tarball_candidate
    elif validator not in (None, "", "none"):
        raise ValueError(f"unsupported artifact validator: {validator}")
    result = promote_staged_artifact(staged, target, validate=validate)
    return f"promoted artifact to {result.target}"


def _handle_pack_compaction(wiki_path: Path, payload: dict[str, Any]) -> str:
    threshold = _optional_payload_int(
        payload,
        "overlay_threshold",
        default=int(cfg.graph_pack_compaction_overlay_threshold),
    )
    status = pack_compaction_status(
        wiki_path=wiki_path,
        overlay_threshold=threshold,
    )
    if not status["needs_compaction"]:
        return (
            "pack compaction not needed: "
            f"{status['max_overlay_count']} overlays below threshold "
            f"{status['overlay_threshold']}"
        )
    if not status["can_compact_now"]:
        return (
            "pack compaction skipped: active graph/wiki packs are not "
            "ready for coordinated compaction"
        )
    base_export_id = (
        _optional_payload_string(payload, "base_export_id")
        or f"export-compacted-{status['max_overlay_count']}"
    )
    compacted = compact_active_pack_sets(
        wiki_path=wiki_path,
        base_export_id=base_export_id,
        staging_dir=_optional_payload_path(payload, "staging_dir"),
        graph_config_hash=_optional_payload_string(payload, "graph_config_hash"),
        graph_model_id=_optional_payload_string(payload, "graph_model_id"),
        created_at=_optional_payload_string(payload, "created_at"),
    )
    promoted = promote_staged_pack_sets(
        wiki_path=wiki_path,
        staged_graph_packs_dir=compacted.staged_graph_packs_dir,
        staged_wiki_packs_dir=compacted.staged_wiki_packs_dir,
        graph_backup_packs_dir=_optional_payload_path(payload, "graph_backup_packs_dir"),
        wiki_backup_packs_dir=_optional_payload_path(payload, "wiki_backup_packs_dir"),
        refresh_graph_store=not bool(payload.get("no_graph_store_refresh", False)),
        graph_store_db_path=_optional_payload_path(payload, "graph_store_db"),
    )
    return (
        f"pack compaction promoted {base_export_id}: "
        f"{', '.join(promoted.graph.promoted_pack_ids)} / "
        f"{', '.join(promoted.wiki.promoted_pack_ids)}"
    )


def _catalog_refresh_args(payload: dict[str, Any], *, update_wiki_tar: bool) -> list[str]:
    args = [sys.executable, "-m", "import_skills_sh_catalog"]
    if payload.get("fetch"):
        args.append("--fetch")
    else:
        from_catalog = payload.get("from_catalog") or payload.get("catalog")
        from_api_union = payload.get("from_api_union")
        source_flag = "--from-catalog" if from_catalog else "--from-api-union"
        source_value = from_catalog or from_api_union
        if not isinstance(source_value, str) or not source_value.strip():
            raise ValueError(
                "catalog maintenance payload requires fetch=true, from_catalog, "
                "from_api_union, or catalog"
            )
        args.extend([source_flag, source_value.strip()])
    if catalog_out := _optional_payload_string(payload, "catalog_out"):
        args.extend(["--catalog-out", catalog_out])
    if wiki_tar := _optional_payload_string(payload, "wiki_tar"):
        args.extend(["--wiki-tar", wiki_tar])
    if payload.get("drop_body_unavailable"):
        args.append("--drop-body-unavailable")
    if update_wiki_tar:
        args.append("--update-wiki-tar")
    return args


def _run_checked(args: list[str], *, label: str) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"{label} failed with exit {exc.returncode}{suffix}") from exc


def _required_payload_string(payload: dict[str, Any], key: str) -> str:
    value = _optional_payload_string(payload, key)
    if value is None:
        raise ValueError(f"maintenance payload requires non-empty {key}")
    return value


def _optional_payload_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"maintenance payload {key} must be a non-empty string")
    return value.strip()


def _optional_payload_path(payload: dict[str, Any], key: str) -> Path | None:
    value = _optional_payload_string(payload, key)
    return Path(value) if value is not None else None


def _optional_payload_int(
    payload: dict[str, Any],
    key: str,
    *,
    default: int,
) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"maintenance payload {key} must be an integer >= 1")
    return value


MAINTENANCE_HANDLERS: dict[str, MaintenanceHandler] = {
    wiki_queue.GRAPH_EXPORT_JOB: _handle_graph_export,
    wiki_queue.GRAPH_STORE_REFRESH_JOB: _handle_graph_store_refresh,
    wiki_queue.CATALOG_REFRESH_JOB: _handle_catalog_refresh,
    wiki_queue.TAR_REFRESH_JOB: _handle_tar_refresh,
    wiki_queue.ARTIFACT_PROMOTION_JOB: _handle_artifact_promotion,
    wiki_queue.PACK_COMPACTION_JOB: _handle_pack_compaction,
}


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Drain ctx wiki maintenance queue jobs")
    parser.add_argument("--wiki", default=str(cfg.wiki_dir), help="Wiki root path")
    parser.add_argument("--worker-id", default=_default_worker_id(), help="Queue worker ID")
    parser.add_argument("--limit", type=int, default=None, help="Maximum jobs to process")
    parser.add_argument("--once", action="store_true", help="Process at most one job")
    parser.add_argument("--lease-seconds", type=float, default=60.0, help="Lease duration")
    parser.add_argument("--retry-delay-seconds", type=float, default=5.0, help="Retry delay")
    args = parser.parse_args(argv)

    if args.once and args.limit is not None:
        parser.error("use either --once or --limit, not both")
    limit = 1 if args.once else args.limit

    try:
        results = drain_queue(
            Path(os.path.expanduser(args.wiki)),
            worker_id=args.worker_id,
            limit=limit,
            lease_seconds=args.lease_seconds,
            retry_delay_seconds=args.retry_delay_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should surface queue failures cleanly.
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("No ready wiki queue jobs.")
        return

    failed = False
    for result in results:
        print(f"{result.status}: {result.kind}#{result.job_id} - {result.message}")
        if result.status != wiki_queue.STATUS_SUCCEEDED:
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
