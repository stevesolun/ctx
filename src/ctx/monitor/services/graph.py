"""Read-only graph artifact loading helpers for ctx-monitor."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tarfile
import zlib
from pathlib import Path
from typing import Any, Callable

from ctx.core import entity_types as core_entity_types
from ctx.utils._safe_name import is_safe_source_name


_GRAPH_CACHE_KEY: tuple[Any, ...] | None = None
_GRAPH_CACHE_VALUE: Any | None = None
_OVERLAY_INDEX_COVERAGE_CACHE_KEY: tuple[Any, ...] | None = None
_OVERLAY_INDEX_COVERAGE_CACHE_VALUE: bool | None = None
_PACKAGED_GRAPH_EXPORT_ID_CACHE: str | None | bool = None
_DASHBOARD_ENTITY_TYPES: tuple[str, ...] = tuple(
    entity_type for _, entity_type, _ in core_entity_types.entity_source_specs()
)


def reset_caches() -> None:
    global _GRAPH_CACHE_KEY, _GRAPH_CACHE_VALUE
    global _OVERLAY_INDEX_COVERAGE_CACHE_KEY, _OVERLAY_INDEX_COVERAGE_CACHE_VALUE
    global _PACKAGED_GRAPH_EXPORT_ID_CACHE

    _GRAPH_CACHE_KEY = None
    _GRAPH_CACHE_VALUE = None
    _OVERLAY_INDEX_COVERAGE_CACHE_KEY = None
    _OVERLAY_INDEX_COVERAGE_CACHE_VALUE = None
    _PACKAGED_GRAPH_EXPORT_ID_CACHE = None


def cached_dashboard_graph() -> Any | None:
    return _GRAPH_CACHE_VALUE


def dashboard_file_cache_key(path: Path) -> tuple[str, float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), stat.st_mtime, stat.st_size)


def dashboard_graph_pack_cache_key(
    packs_dir: Path,
) -> tuple[tuple[str, float, int], ...]:
    if not packs_dir.is_dir():
        return ()
    try:
        files = sorted(path for path in packs_dir.rglob("*") if path.is_file())
    except OSError:
        return (("<unreadable>", 0.0, 0),)
    rows: list[tuple[str, float, int]] = []
    for path in files:
        try:
            stat = path.stat()
            relpath = path.relative_to(packs_dir).as_posix()
        except OSError:
            rows.append((path.name, 0.0, 0))
            continue
        rows.append((relpath, stat.st_mtime, stat.st_size))
    return tuple(rows)


def dashboard_graph_source_cache_key(
    graph_path: Path,
    overlay_path: Path,
) -> tuple[Any, ...] | None:
    graph_key = dashboard_file_cache_key(graph_path)
    overlay_key = dashboard_file_cache_key(overlay_path)
    pack_key = dashboard_graph_pack_cache_key(graph_path.parent / "packs")
    if graph_key is None and not pack_key:
        return None
    return (graph_key, overlay_key, pack_key)


def dashboard_graph_index_path(wiki_dir: Path) -> Path:
    return wiki_dir / "graphify-out" / "dashboard-neighborhoods.sqlite3"


def _slugish(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _display_slug(slug: str) -> str:
    return str(slug or "").removeprefix("skills-sh-")


def _display_label(value: Any, *, fallback_slug: str = "") -> str:
    return _display_slug(str(value or fallback_slug or ""))


def graph_slug_from_node_id(node_id: str) -> str:
    return node_id.split(":", 1)[-1]


def resolve_index_center(
    conn: sqlite3.Connection,
    raw_query: str,
    entity_type: str | None,
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    raw_query = str(raw_query or "").strip()
    if not raw_query or "/" in raw_query or "\\" in raw_query or ".." in raw_query:
        return None, None, []
    normalized_query = _slugish(raw_query)
    if not normalized_query or not is_safe_source_name(normalized_query):
        return None, None, []

    normalized_type = core_entity_types.normalize_entity_type(
        entity_type,
        allowed=_DASHBOARD_ENTITY_TYPES,
    )
    if entity_type is not None and normalized_type is None:
        return None, None, []
    entity_types = (normalized_type,) if normalized_type is not None else _DASHBOARD_ENTITY_TYPES

    candidates: list[str] = []
    for candidate in (raw_query, normalized_query):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for current_type in entity_types:
        for candidate_slug in candidates:
            row = conn.execute(
                "SELECT node_id FROM slug_index WHERE slug=? AND type=? LIMIT 1",
                (candidate_slug, current_type),
            ).fetchone()
            if row is not None:
                return str(row["node_id"]), None, [candidate_slug]

    where = ""
    params: list[Any] = []
    if normalized_type is not None:
        where = "WHERE s.type=?"
        params.append(normalized_type)
    rows = conn.execute(
        "SELECT s.slug,s.type,s.node_id,n.label,n.tags,n.degree "
        "FROM slug_index s JOIN nodes n ON n.id=s.node_id "
        f"{where}",
        params,
    )
    matches: list[tuple[tuple[int, int, int], str, str]] = []
    query_tokens = set(normalized_query.split("-"))
    for row in rows:
        node_slug = str(row["slug"] or "")
        label = _display_label(row["label"], fallback_slug=node_slug)
        haystacks = {
            _slugish(node_slug),
            _slugish(_display_slug(node_slug)),
            _slugish(label),
        }
        try:
            tags = json.loads(row["tags"] or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        if isinstance(tags, list):
            haystacks.update(_slugish(str(tag)) for tag in tags[:12])
        rank = None
        if normalized_query in haystacks:
            rank = 0
        elif any(h.startswith(normalized_query) for h in haystacks):
            rank = 1
        elif any(normalized_query in h for h in haystacks):
            rank = 2
        elif query_tokens and all(
            any(token in h for h in haystacks) for token in query_tokens
        ):
            rank = 3
        if rank is None:
            continue
        try:
            degree = int(row["degree"] or 0)
        except (TypeError, ValueError):
            degree = 0
        matches.append(((rank, len(node_slug), -degree), str(row["node_id"]), node_slug))

    matches.sort(key=lambda item: item[0])
    suggestions: list[str] = []
    for _, _node_id, suggestion in matches[:8]:
        display_suggestion = _display_slug(suggestion)
        if display_suggestion not in suggestions:
            suggestions.append(display_suggestion)
    if not matches:
        return None, None, suggestions
    center = matches[0][1]
    return (
        center,
        {"query": raw_query, "slug": graph_slug_from_node_id(center), "id": center},
        suggestions,
    )


def dashboard_graph_manifest_export_id(wiki_dir: Path) -> str | None:
    manifest_path = wiki_dir / "graphify-out" / "graph-export-manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    export_id = data.get("export_id") if isinstance(data, dict) else None
    if not isinstance(export_id, str) or not export_id.strip():
        return None
    return export_id.strip()


def dashboard_index_meta(index_path: Path) -> dict[str, Any] | None:
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute("SELECT key,value FROM meta").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    try:
        return {str(key): json.loads(str(value)) for key, value in rows}
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def dashboard_index_matches_manifest(index_path: Path, wiki_dir: Path) -> bool:
    manifest_export_id = dashboard_graph_manifest_export_id(wiki_dir)
    if manifest_export_id is None:
        return False
    meta = dashboard_index_meta(index_path)
    if meta is None:
        return False
    return meta.get("export_id") == manifest_export_id


def dashboard_graph_has_runtime_overlays(wiki_dir: Path) -> bool:
    overlay = wiki_dir / "graphify-out" / "entity-overlays.jsonl"
    try:
        return overlay.is_file() and overlay.stat().st_size > 0
    except OSError:
        return False


def dashboard_overlay_index_coverage_key(
    index_path: Path,
    overlay: Path,
    manifest_export_id: str | None,
) -> tuple[Any, ...] | None:
    try:
        index_stat = index_path.stat()
        overlay_stat = overlay.stat()
    except OSError:
        return None
    return (
        index_path.resolve(),
        index_stat.st_mtime,
        index_stat.st_size,
        overlay.resolve(),
        overlay_stat.st_mtime,
        overlay_stat.st_size,
        manifest_export_id,
    )


def active_dashboard_overlay_records(overlay: Path) -> list[dict[str, Any]] | None:
    try:
        from ctx.core.graph.entity_overlays import active_overlay_records

        rows = []
        for line in overlay.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                return None
            rows.append(payload)
        return [dict(row) for row in active_overlay_records(rows)]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def dashboard_overlay_matches_known_release(overlay: Path) -> bool:
    try:
        from ctx_init import _GRAPH_ENTITY_OVERLAY_SHA256
    except (ImportError, AttributeError):
        return False
    if not isinstance(_GRAPH_ENTITY_OVERLAY_SHA256, str) or not _GRAPH_ENTITY_OVERLAY_SHA256:
        return False
    try:
        data = overlay.read_bytes().replace(b"\r\n", b"\n")
        return hashlib.sha256(data).hexdigest() == _GRAPH_ENTITY_OVERLAY_SHA256
    except OSError:
        return False


def dashboard_index_uncovered_overlay_nodes(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    *,
    require_edges: bool,
) -> set[str] | None:
    uncovered: set[str] = set()
    neighbor_targets: dict[str, set[str]] = {}

    def node_exists(node_id: str) -> bool:
        return bool(conn.execute(
            "SELECT 1 FROM nodes WHERE id=? LIMIT 1",
            (node_id,),
        ).fetchone())

    def indexed_neighbors(node_id: str) -> set[str]:
        cached = neighbor_targets.get(node_id)
        if cached is not None:
            return cached
        row = conn.execute(
            "SELECT payload FROM neighbors WHERE source=?",
            (node_id,),
        ).fetchone()
        targets: set[str] = set()
        if row is not None:
            try:
                payload = json.loads(zlib.decompress(row["payload"]).decode("utf-8"))
            except (TypeError, json.JSONDecodeError, zlib.error):
                payload = []
            if isinstance(payload, list):
                targets = {
                    str(edge.get("target"))
                    for edge in payload
                    if isinstance(edge, dict) and isinstance(edge.get("target"), str)
                }
        neighbor_targets[node_id] = targets
        return targets

    for record in records:
        nodes = record.get("nodes", [])
        edges = record.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            return None
        if not nodes and edges:
            return None
        for node in nodes:
            if not isinstance(node, dict):
                return None
            node_id = node.get("id")
            if not isinstance(node_id, str):
                return None
            if not node_exists(node_id):
                uncovered.add(node_id)
        if not require_edges:
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                return None
            source = edge.get("source")
            target = edge.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                return None
            source_exists = node_exists(source)
            target_exists = node_exists(target)
            if not source_exists:
                uncovered.add(source)
            if not target_exists:
                uncovered.add(target)
            if not source_exists or not target_exists:
                uncovered.update((source, target))
                continue
            if target not in indexed_neighbors(source) and source not in indexed_neighbors(target):
                uncovered.update((source, target))
    return uncovered


def dashboard_uncovered_runtime_overlay_nodes(
    index_path: Path,
    wiki_dir: Path,
    *,
    require_edges: bool,
) -> set[str] | None:
    overlay = wiki_dir / "graphify-out" / "entity-overlays.jsonl"
    try:
        if not overlay.is_file() or overlay.stat().st_size == 0:
            return set()
    except OSError:
        return set()
    if not index_path.is_file() or not dashboard_index_matches_manifest(index_path, wiki_dir):
        return None
    records = active_dashboard_overlay_records(overlay)
    if records is None:
        return None
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return dashboard_index_uncovered_overlay_nodes(
                conn,
                records,
                require_edges=require_edges,
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error, KeyError, TypeError):
        return None


def dashboard_index_covers_runtime_overlays(
    index_path: Path,
    wiki_dir: Path,
    *,
    require_edges: bool,
) -> bool:
    """Return True when the SQLite dashboard index already includes overlays."""
    overlay = wiki_dir / "graphify-out" / "entity-overlays.jsonl"
    try:
        if not overlay.is_file() or overlay.stat().st_size == 0:
            return True
    except OSError:
        return True
    if not index_path.is_file() or not dashboard_index_matches_manifest(index_path, wiki_dir):
        return False

    global _OVERLAY_INDEX_COVERAGE_CACHE_KEY, _OVERLAY_INDEX_COVERAGE_CACHE_VALUE
    cache_key = dashboard_overlay_index_coverage_key(
        index_path,
        overlay,
        dashboard_graph_manifest_export_id(wiki_dir),
    )
    if cache_key is not None:
        cache_key = (*cache_key, require_edges)
    if (
        cache_key is not None
        and _OVERLAY_INDEX_COVERAGE_CACHE_KEY == cache_key
        and _OVERLAY_INDEX_COVERAGE_CACHE_VALUE is not None
    ):
        return _OVERLAY_INDEX_COVERAGE_CACHE_VALUE

    uncovered = dashboard_uncovered_runtime_overlay_nodes(
        index_path,
        wiki_dir,
        require_edges=require_edges,
    )
    coverage = uncovered == set()

    if cache_key is not None:
        _OVERLAY_INDEX_COVERAGE_CACHE_KEY = cache_key
        _OVERLAY_INDEX_COVERAGE_CACHE_VALUE = coverage
    return coverage


def dashboard_graph_index_archives(module_root: Path) -> list[Path]:
    roots = (module_root,)
    names = ("wiki-graph-runtime.tar.gz", "wiki-graph.tar.gz")
    seen: set[Path] = set()
    archives: list[Path] = []
    for root in roots:
        for name in names:
            candidate = (root / "graph" / name).resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                archives.append(candidate)
    return archives


def packaged_graph_export_id(module_root: Path) -> str | None:
    global _PACKAGED_GRAPH_EXPORT_ID_CACHE
    if isinstance(_PACKAGED_GRAPH_EXPORT_ID_CACHE, bool):
        return None
    if isinstance(_PACKAGED_GRAPH_EXPORT_ID_CACHE, str):
        return _PACKAGED_GRAPH_EXPORT_ID_CACHE
    try:
        data = json.loads(
            (module_root / "graph" / "communities.json").read_text(
                encoding="utf-8",
            )
        )
    except (OSError, json.JSONDecodeError):
        _PACKAGED_GRAPH_EXPORT_ID_CACHE = False
        return None
    export_id = data.get("export_id") if isinstance(data, dict) else None
    if isinstance(export_id, str) and export_id.strip():
        _PACKAGED_GRAPH_EXPORT_ID_CACHE = export_id.strip()
        return export_id.strip()
    _PACKAGED_GRAPH_EXPORT_ID_CACHE = False
    return None


def archive_graph_export_id(archive: Path) -> str | None:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                member = tar.getmember("./graphify-out/graph-export-manifest.json")
            except KeyError:
                member = tar.getmember("graphify-out/graph-export-manifest.json")
            source = tar.extractfile(member)
            if source is None:
                return None
            try:
                data = json.loads(source.read().decode("utf-8", errors="replace"))
            finally:
                source.close()
    except (KeyError, OSError, tarfile.TarError, json.JSONDecodeError):
        return None
    export_id = data.get("export_id") if isinstance(data, dict) else None
    return export_id.strip() if isinstance(export_id, str) and export_id.strip() else None


def ensure_dashboard_graph_index(
    *,
    target: Path,
    manifest_export_id: Callable[[], str | None],
    packaged_export_id: Callable[[], str | None],
    archives: Callable[[], list[Path]],
    archive_export_id: Callable[[Path], str | None],
    index_matches_manifest: Callable[[Path], bool],
    index_member: str,
) -> Path | None:
    if target.is_file():
        if index_matches_manifest(target):
            return target
        try:
            target.unlink()
        except OSError:
            return None

    manifest_id = manifest_export_id()
    packaged_id = packaged_export_id()
    if (
        manifest_id is not None
        and packaged_id is not None
        and manifest_id != packaged_id
    ):
        return None

    archive_paths = archives()
    if not archive_paths:
        return None
    if manifest_id is None:
        return None

    from ctx.utils._file_lock import file_lock

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(target):
            if target.is_file():
                if index_matches_manifest(target):
                    return target
                try:
                    target.unlink()
                except OSError:
                    return None
            for archive in archive_paths:
                archive_id = packaged_id or archive_export_id(archive)
                if manifest_id and archive_id and archive_id != manifest_id:
                    continue
                try:
                    with tarfile.open(archive, "r:gz") as tar:
                        try:
                            member = tar.getmember(f"./{index_member}")
                        except KeyError:
                            member = tar.getmember(index_member)
                        if not member.isfile():
                            continue
                        source = tar.extractfile(member)
                        if source is None:
                            continue
                        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                        try:
                            with tmp.open("wb") as out:
                                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                    out.write(chunk)
                            if not index_matches_manifest(tmp):
                                continue
                            os.replace(tmp, target)
                            return target
                        finally:
                            source.close()
                            if tmp.exists():
                                tmp.unlink()
                except (KeyError, OSError, tarfile.TarError):
                    continue
    except TimeoutError:
        return None
    return target if target.is_file() else None


def load_dashboard_graph(
    wiki_dir: Path,
    load_graph: Callable[..., Any],
) -> Any:
    """Load the dashboard graph once per graph artifact version."""
    global _GRAPH_CACHE_KEY, _GRAPH_CACHE_VALUE

    graph_path = wiki_dir / "graphify-out" / "graph.json"
    overlay_path = graph_path.with_name("entity-overlays.jsonl")
    source_key = dashboard_graph_source_cache_key(graph_path, overlay_path)
    if source_key is None:
        _GRAPH_CACHE_KEY = None
        _GRAPH_CACHE_VALUE = None
        return load_graph(graph_path)

    cache_key = (id(load_graph), source_key)
    if _GRAPH_CACHE_KEY == cache_key and _GRAPH_CACHE_VALUE is not None:
        return _GRAPH_CACHE_VALUE

    try:
        graph = load_graph(graph_path, apply_runtime_filter=False)
    except TypeError:
        graph = load_graph(graph_path)
    _GRAPH_CACHE_KEY = cache_key
    _GRAPH_CACHE_VALUE = graph
    return graph
