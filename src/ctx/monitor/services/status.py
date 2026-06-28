"""Queue and artifact status helpers for ctx-monitor."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import json
import os
from pathlib import Path
from typing import Any

from ctx.core.wiki import wiki_queue
from ctx.telemetry import DEFAULT_PRIVACY_MODE, DEFAULT_TELEMETRY_PATH, TelemetryEvent


def queue_job_summary(job: wiki_queue.QueueJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "worker_id": job.worker_id,
        "leased_until": job.leased_until,
        "available_at": job.available_at,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "source": job.payload.get("source"),
        "payload_keys": sorted(str(key) for key in job.payload),
    }


def queue_status(wiki_dir: Path) -> dict[str, Any]:
    """Return durable wiki/graph queue state without creating the DB."""
    db_path = wiki_queue.queue_db_path(wiki_dir)
    counts = {
        wiki_queue.STATUS_PENDING: 0,
        wiki_queue.STATUS_RUNNING: 0,
        wiki_queue.STATUS_SUCCEEDED: 0,
        wiki_queue.STATUS_FAILED: 0,
        wiki_queue.STATUS_CANCELLED: 0,
    }
    if not db_path.exists():
        return {
            "available": False,
            "db_path": str(db_path),
            "total": 0,
            "counts": counts,
            "recent_jobs": [],
        }
    try:
        raw_counts = wiki_queue.count_jobs_by_status(db_path)
        recent = wiki_queue.list_recent_jobs(db_path, limit=20)
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "db_path": str(db_path),
            "total": 0,
            "counts": counts,
            "recent_jobs": [],
            "error": str(exc),
        }
    for status, count in raw_counts.items():
        counts[status] = count
    return {
        "available": True,
        "db_path": str(db_path),
        "total": sum(raw_counts.values()),
        "counts": counts,
        "recent_jobs": [queue_job_summary(job) for job in recent],
    }


def file_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "size": 0, "mtime": None}
    try:
        stat = path.stat()
    except OSError as exc:
        return {
            "path": str(path),
            "exists": False,
            "size": 0,
            "mtime": None,
            "error": str(exc),
        }
    return {
        "path": str(path),
        "exists": path.is_file(),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def pack_dir_status(packs_dir: Path, *, manifest_name: str) -> dict[str, Any]:
    """Return summary state for a modular base/overlay pack directory."""
    if not packs_dir.exists():
        return {
            "path": str(packs_dir),
            "exists": False,
            "size": 0,
            "mtime": None,
            "pack_count": 0,
            "base_count": 0,
            "overlay_count": 0,
            "pack_ids": [],
        }
    if not packs_dir.is_dir():
        return {
            "path": str(packs_dir),
            "exists": False,
            "size": 0,
            "mtime": None,
            "pack_count": 0,
            "base_count": 0,
            "overlay_count": 0,
            "pack_ids": [],
            "error": "pack path is not a directory",
        }
    total_size = 0
    newest = 0.0
    pack_ids: list[str] = []
    base_count = 0
    overlay_count = 0
    errors: list[str] = []
    try:
        files = [path for path in packs_dir.rglob("*") if path.is_file()]
    except OSError as exc:
        return {
            "path": str(packs_dir),
            "exists": False,
            "size": 0,
            "mtime": None,
            "pack_count": 0,
            "base_count": 0,
            "overlay_count": 0,
            "pack_ids": [],
            "error": str(exc),
        }
    for path in files:
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        total_size += stat.st_size
        newest = max(newest, stat.st_mtime)
        if path.name != manifest_name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{path.name}: manifest is not an object")
            continue
        pack_id = str(payload.get("pack_id") or path.parent.name)
        pack_ids.append(pack_id)
        pack_type = payload.get("pack_type")
        if pack_type == "base":
            base_count += 1
        elif pack_type == "overlay":
            overlay_count += 1
        else:
            errors.append(f"{pack_id}: unknown pack_type {pack_type!r}")
    status: dict[str, Any] = {
        "path": str(packs_dir),
        "exists": True,
        "size": total_size,
        "mtime": newest or None,
        "pack_count": len(pack_ids),
        "base_count": base_count,
        "overlay_count": overlay_count,
        "pack_ids": sorted(pack_ids)[:25],
    }
    if errors:
        status["error"] = "; ".join(errors[:5])
    return status


def graph_store_status(graph_dir: Path) -> dict[str, Any]:
    """Return SQLite operational-store state for the active graph directory."""
    db_path = graph_dir / "graph-store.sqlite3"
    status = file_status(db_path)
    try:
        from ctx.core.graph.graph_store import validate_graph_store  # noqa: PLC0415

        validation = validate_graph_store(db_path, graph_dir)
    except (OSError, ValueError) as exc:
        validation = {
            "ok": False,
            "fresh": False,
            "nodes": 0,
            "edges": 0,
            "errors": [str(exc)],
        }
    node_count = validation.get("nodes")
    edge_count = validation.get("edges")
    status.update({
        "ok": bool(validation.get("ok")),
        "fresh": bool(validation.get("fresh")),
        "nodes": node_count if isinstance(node_count, int) else 0,
        "edges": edge_count if isinstance(edge_count, int) else 0,
        "errors": validation.get("errors") if isinstance(validation.get("errors"), list) else [],
    })
    return status


def pack_compaction_artifact_status(wiki_dir: Path) -> dict[str, Any]:
    """Return coordinated graph/wiki pack compaction state for /status."""
    try:
        from ctx.core.wiki.pack_compaction import pack_compaction_status  # noqa: PLC0415

        status = pack_compaction_status(wiki_path=wiki_dir, validate=False)
    except Exception as exc:  # noqa: BLE001 - status should render degraded state.
        return {
            "path": str(wiki_dir),
            "exists": False,
            "size": 0,
            "mtime": None,
            "error": str(exc),
        }
    graph_pack_count = status.get("graph_pack_count")
    wiki_pack_count = status.get("wiki_pack_count")
    return {
        "path": str(wiki_dir),
        "exists": bool(
            (graph_pack_count if isinstance(graph_pack_count, int) else 0)
            or (wiki_pack_count if isinstance(wiki_pack_count, int) else 0)
        ),
        "size": 0,
        "mtime": None,
        **status,
    }


def first_existing_file_status(*paths: Path) -> dict[str, Any]:
    for path in paths:
        if path.exists():
            return file_status(path)
    return file_status(paths[0])


def graph_stats_file_status(path: Path) -> dict[str, Any]:
    status = file_status(path)
    if not status.get("exists"):
        return status
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        status["error"] = str(exc)
        return status
    counts = payload.get("counts") if isinstance(payload, dict) else None
    if not isinstance(counts, dict):
        status["error"] = "missing counts"
        return status
    normalized: dict[str, int] = {}
    for key in ("nodes", "edges", "skills", "agents", "mcps", "harnesses"):
        value = counts.get(key)
        if isinstance(value, int):
            normalized[key] = value
    status["counts"] = normalized
    return status


def telemetry_status(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return read-only local telemetry health for operator status pages."""
    raw = config if config is not None else _telemetry_config()
    export = _mapping_or_empty(raw.get("export"))
    spool_path = _configured_path(raw.get("path"), DEFAULT_TELEMETRY_PATH)
    checkpoint_path = _configured_path(
        export.get("checkpoint_path"),
        Path(str(spool_path) + ".export-checkpoint.json"),
    )
    status_path = _configured_path(
        export.get("status_path"),
        Path(str(spool_path) + ".export-status.json"),
    )
    return {
        "enabled": bool(raw.get("enabled", True)),
        "mode": str(raw.get("mode", DEFAULT_PRIVACY_MODE)),
        "export_enabled": bool(export.get("enabled", False)),
        "export_sink": str(export.get("sink", "otlp_http")),
        "spool": telemetry_spool_status(spool_path),
        "checkpoint": file_status(checkpoint_path),
        "export_status": telemetry_export_status(status_path),
    }


def telemetry_spool_status(path: Path) -> dict[str, Any]:
    status = file_status(path)
    status.update({
        "event_count": 0,
        "malformed_records": 0,
        "outcomes": {},
        "sources": {},
        "latest_event": None,
    })
    if not status.get("exists"):
        return status
    outcomes: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    latest: TelemetryEvent | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = TelemetryEvent(**json.loads(line))
                except (TypeError, ValueError, json.JSONDecodeError):
                    status["malformed_records"] += 1
                    continue
                status["event_count"] += 1
                outcomes[event.outcome] += 1
                sources[event.source] += 1
                if latest is None or event.ts > latest.ts:
                    latest = event
    except OSError as exc:
        status["error"] = str(exc)
        return status
    status["outcomes"] = dict(sorted(outcomes.items()))
    status["sources"] = dict(sources.most_common(5))
    if latest is not None:
        status["latest_event"] = {
            "ts": latest.ts,
            "event_name": latest.event_name,
            "source": latest.source,
            "outcome": latest.outcome,
        }
    return status


def telemetry_export_status(path: Path) -> dict[str, Any]:
    status = file_status(path)
    if not status.get("exists"):
        status["status"] = "never_exported"
        return status
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        status.update({"status": "unreadable", "error": str(exc)})
        return status
    if not isinstance(payload, dict):
        status.update({"status": "unreadable", "error": "status file is not an object"})
        return status
    for key in (
        "schema_version",
        "status",
        "sink",
        "destination_hash",
        "attempted",
        "exported",
        "failed",
        "error_kind",
        "checkpoint_advanced",
        "malformed_records",
        "malformed_pending_records",
        "updated_at",
        "finished_at",
        "last_success_at",
    ):
        if key in payload:
            status[key] = payload[key]
    return status


def _telemetry_config() -> Mapping[str, Any]:
    try:
        from ctx_config import cfg  # noqa: PLC0415

        raw = cfg.get("telemetry", {})
    except Exception:  # noqa: BLE001 - status rendering must stay best-effort.
        return {}
    return raw if isinstance(raw, Mapping) else {}


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _configured_path(value: Any, default: Path) -> Path:
    raw = str(value or default)
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def promotion_status(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    previous = _dict_or_empty(data.get("previous"))
    candidate = _dict_or_empty(data.get("candidate"))
    current = _dict_or_empty(data.get("current"))
    return {
        "path": str(path),
        "status": data.get("status"),
        "target": data.get("target"),
        "started_at": data.get("started_at"),
        "promoted_at": data.get("promoted_at"),
        "previous_sha256": previous.get("sha256"),
        "previous_size": previous.get("size"),
        "candidate_sha256": candidate.get("sha256"),
        "candidate_size": candidate.get("size"),
        "current_sha256": current.get("sha256"),
        "current_size": current.get("size"),
    }


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def artifact_status(
    *,
    wiki_dir: Path,
    claude_dir: Path,
    repo_graph_dir: Path,
) -> dict[str, Any]:
    """Return shipped graph/wiki artifact file state and promotion metadata."""
    graph_dir = wiki_dir / "graphify-out"
    claude_graph_dir = claude_dir / "graph"
    promotion_paths = sorted(
        {
            *graph_dir.glob("*.promotion.json"),
            *wiki_dir.glob("*.promotion.json"),
            *claude_graph_dir.glob("*.promotion.json"),
        },
        key=lambda path: str(path),
    )
    promotions = [
        promotion
        for promotion in (promotion_status(path) for path in promotion_paths)
        if promotion is not None
    ]
    wiki_graph_tar = first_existing_file_status(
        claude_graph_dir / "wiki-graph.tar.gz",
        repo_graph_dir / "wiki-graph.tar.gz",
    )
    published_graph_stats = graph_stats_file_status(repo_graph_dir / "wiki-graph-stats.json")
    if published_graph_stats.get("counts"):
        wiki_graph_tar["stats_path"] = published_graph_stats.get("path")
        wiki_graph_tar["counts"] = published_graph_stats["counts"]
    return {
        "graph_json": file_status(graph_dir / "graph.json"),
        "graph_packs": pack_dir_status(
            graph_dir / "packs",
            manifest_name="graph-pack-manifest.json",
        ),
        "graph_delta_json": file_status(graph_dir / "graph-delta.json"),
        "communities_json": file_status(graph_dir / "communities.json"),
        "graph_store": graph_store_status(graph_dir),
        "wiki_packs": pack_dir_status(
            wiki_dir / "wiki-packs",
            manifest_name="wiki-pack-manifest.json",
        ),
        "pack_compaction": pack_compaction_artifact_status(wiki_dir),
        "wiki_graph_tar": wiki_graph_tar,
        "published_graph_stats": published_graph_stats,
        "skills_sh_catalog": first_existing_file_status(
            wiki_dir / "external-catalogs" / "skills-sh" / "catalog.json",
            claude_graph_dir / "skills-sh-catalog.json.gz",
            repo_graph_dir / "skills-sh-catalog.json.gz",
        ),
        "promotion_count": len(promotions),
        "promotions": promotions,
    }


def status_payload(
    *,
    wiki_dir: Path,
    claude_dir: Path,
    repo_graph_dir: Path,
) -> dict[str, Any]:
    return {
        "queue": queue_status(wiki_dir),
        "telemetry": telemetry_status(),
        "artifacts": artifact_status(
            wiki_dir=wiki_dir,
            claude_dir=claude_dir,
            repo_graph_dir=repo_graph_dir,
        ),
    }
