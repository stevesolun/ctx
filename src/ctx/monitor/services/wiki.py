"""Read-only wiki entity helpers for the ctx dashboard."""

from __future__ import annotations

import json
import re
import sqlite3
import hashlib
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from ctx.core import entity_types as core_entity_types
from ctx.core.wiki import wiki_queue
from ctx.core.wiki.wiki_packs import WikiPackState, load_merged_wiki_pack_state
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body
from ctx.monitor.services import graph as graph_service
from ctx.utils._safe_name import is_safe_source_name


_WIKI_PACK_CACHE_KEY: tuple[Any, ...] | None = None
_WIKI_PACK_CACHE_VALUE: WikiPackState | None = None
_DASHBOARD_ENTITY_SOURCES: tuple[tuple[str, str, bool], ...] = (
    core_entity_types.entity_source_specs()
)
_DASHBOARD_ENTITY_TYPES: tuple[str, ...] = tuple(
    entity_type for _, entity_type, _ in _DASHBOARD_ENTITY_SOURCES
)


def reset_caches() -> None:
    global _WIKI_PACK_CACHE_KEY, _WIKI_PACK_CACHE_VALUE

    _WIKI_PACK_CACHE_KEY = None
    _WIKI_PACK_CACHE_VALUE = None


def normalize_entity_type(raw: object) -> str | None:
    return core_entity_types.normalize_entity_type(raw, allowed=_DASHBOARD_ENTITY_TYPES)


def is_safe_slug(slug: str) -> bool:
    return is_safe_source_name(slug)


def queue_entity_refresh(
    wiki_dir: Path,
    *,
    entity_type: str,
    slug: str,
    entity_path: Path,
    content: str,
    action: str,
) -> None:
    wiki_queue.enqueue_entity_upsert(
        wiki_dir,
        entity_type=entity_type,
        slug=slug,
        entity_path=entity_path,
        content=content,
        action=action,
        source="ctx-monitor",
    )
    if action == "delete":
        return
    wiki_queue.enqueue_maintenance_job(
        wiki_dir,
        kind=wiki_queue.GRAPH_EXPORT_JOB,
        payload={"reason": f"entity-{action}", "entity_type": entity_type, "slug": slug},
        source="ctx-monitor",
    )


def wiki_pack_state(wiki_dir: Path) -> WikiPackState | None:
    """Return cached merged wiki-pack state, preserving active tombstones."""
    global _WIKI_PACK_CACHE_KEY, _WIKI_PACK_CACHE_VALUE

    packs_dir = wiki_dir / "wiki-packs"
    if not packs_dir.is_dir():
        reset_caches()
        return None
    key: list[tuple[str, float, int]] = []
    for path in sorted(packs_dir.rglob("*")):
        if not path.is_file() or path.name not in {
            "wiki-pack-manifest.json",
            "pages.jsonl",
            "tombstones.jsonl",
        }:
            continue
        stat = path.stat()
        key.append((path.relative_to(packs_dir).as_posix(), stat.st_mtime, stat.st_size))
    cache_key = (str(packs_dir.resolve()), tuple(key))
    if _WIKI_PACK_CACHE_KEY == cache_key and _WIKI_PACK_CACHE_VALUE is not None:
        return _WIKI_PACK_CACHE_VALUE

    state = load_merged_wiki_pack_state(packs_dir)
    _WIKI_PACK_CACHE_KEY = cache_key
    _WIKI_PACK_CACHE_VALUE = state
    return state


def wiki_pack_pages(wiki_dir: Path) -> dict[str, str] | None:
    """Return merged wiki-pack pages, or None when packs are not installed."""
    state = wiki_pack_state(wiki_dir)
    return state.pages if state is not None else None


def entity_path(
    wiki_dir: Path,
    slug: str,
    entity_type: str | None = None,
) -> Path | None:
    """Resolve a slug to its wiki entity page."""
    if not is_safe_slug(slug):
        return None
    normalized = normalize_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        return None
    pack_state = wiki_pack_state(wiki_dir)
    for _sub, current_type, _recursive in _DASHBOARD_ENTITY_SOURCES:
        if normalized is not None and normalized != current_type:
            continue
        path = core_entity_types.entity_page_path(wiki_dir, current_type, slug)
        if path is None:
            continue
        if pack_state is not None:
            relpath = core_entity_types.entity_relpath(current_type, slug)
            if relpath is not None:
                relpath_text = relpath.as_posix()
                if relpath_text in pack_state.pages:
                    return path
                if relpath_text in pack_state.tombstones:
                    continue
        if path.exists():
            return path
    return None


def entity_target_path(wiki_dir: Path, slug: str, entity_type: str) -> Path:
    """Return the canonical wiki entity path for a new/updated entity."""
    if not is_safe_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    normalized = normalize_entity_type(entity_type)
    if normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    path = core_entity_types.entity_page_path(wiki_dir, normalized, slug)
    if path is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    return path


def iter_entity_paths(
    wiki_dir: Path,
    entity_type: str | None = None,
) -> list[tuple[str, str, Path]]:
    """List pack-backed entities plus local files not shadowed by packs."""
    normalized = normalize_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    base = wiki_dir / "entities"
    file_rows: list[tuple[str, str, Path]] = []
    if base.is_dir():
        for sub, current_type, recursive in _DASHBOARD_ENTITY_SOURCES:
            if normalized is not None and normalized != current_type:
                continue
            root = base / sub
            if not root.is_dir():
                continue
            paths = root.rglob("*.md") if recursive else root.glob("*.md")
            for file_path in paths:
                slug = file_path.stem
                if is_safe_slug(slug):
                    file_rows.append((slug, current_type, file_path))

    pack_state = wiki_pack_state(wiki_dir)
    if pack_state is not None:
        pack_pages = pack_state.pages
        rows: list[tuple[str, str, Path]] = []
        pack_relpaths = set(pack_pages) | set(pack_state.tombstones)
        for relpath in sorted(pack_pages):
            parsed = pack_entity_from_relpath(relpath)
            if parsed is None:
                continue
            slug, current_type = parsed
            if normalized is not None and normalized != current_type:
                continue
            pack_path = core_entity_types.entity_page_path(wiki_dir, current_type, slug)
            if pack_path is not None:
                rows.append((slug, current_type, pack_path))
        for slug, current_type, file_path in file_rows:
            file_relpath = core_entity_types.entity_relpath(current_type, slug)
            if file_relpath is None or file_relpath.as_posix() not in pack_relpaths:
                rows.append((slug, current_type, file_path))
        return sorted(rows, key=lambda row: (row[1], row[0].lower(), row[2].as_posix()))
    return sorted(file_rows, key=lambda row: (row[1], row[0].lower(), row[2].as_posix()))


def entity_detail(
    wiki_dir: Path,
    slug: str,
    entity_type: str | None = None,
) -> dict[str, Any] | None:
    normalized = normalize_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    path = entity_path(wiki_dir, slug, entity_type=normalized)
    if path is None:
        return None
    text = read_entity_text(wiki_dir, slug, normalized, path)
    if text is None:
        return None
    frontmatter, body = parse_frontmatter_and_body(text)
    detected_type = normalized or normalize_entity_type(frontmatter.get("type")) or "skill"
    return {
        "slug": slug,
        "type": detected_type,
        "path": wiki_relative_path(wiki_dir, path) or "",
        "frontmatter": frontmatter,
        "body": body,
    }


def pack_entity_from_relpath(relpath: str) -> tuple[str, str] | None:
    path = Path(relpath)
    parts = path.parts
    if len(parts) < 3 or parts[0] != "entities" or path.suffix != ".md":
        return None
    entity_type = core_entity_types.ENTITY_TYPE_FOR_SUBJECT_TYPE.get(parts[1])
    if entity_type not in _DASHBOARD_ENTITY_TYPES:
        return None
    slug = path.stem
    if not is_safe_slug(slug):
        return None
    if entity_type == "mcp-server":
        if len(parts) != 4 or parts[2] != core_entity_types.mcp_shard(slug):
            return None
    elif len(parts) != 3:
        return None
    return slug, entity_type


def read_entity_text(
    wiki_dir: Path,
    slug: str,
    entity_type: str | None,
    path: Path,
) -> str | None:
    pack_state = wiki_pack_state(wiki_dir)
    if pack_state is not None:
        relpath = wiki_relative_path(wiki_dir, path)
        if relpath is not None:
            if relpath in pack_state.pages:
                return pack_state.pages[relpath]
            if relpath in pack_state.tombstones:
                return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def wiki_relative_path(wiki_dir: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(wiki_dir.resolve()).as_posix()
    except (OSError, ValueError):
        try:
            return path.relative_to(wiki_dir).as_posix()
        except ValueError:
            return None


def wiki_stats_from_dashboard_index(
    index_path: Path,
    *,
    index_matches_manifest: Callable[[Path], bool],
) -> dict[str, int | bool] | None:
    return graph_service.dashboard_index_wiki_stats(
        index_path,
        index_matches_manifest=index_matches_manifest,
    )


def wiki_stats(
    wiki_dir: Path,
    index_path: Path,
    *,
    index_matches_manifest: Callable[[Path], bool],
    graph_node_total: int | None = None,
) -> dict[str, int | bool]:
    """Return entity counts across all dashboard-supported entity types."""
    indexed = wiki_stats_from_dashboard_index(
        index_path,
        index_matches_manifest=index_matches_manifest,
    )
    if indexed is not None:
        return indexed

    if wiki_pack_pages(wiki_dir) is not None:
        stats = {"skills": 0, "agents": 0, "mcps": 0, "harnesses": 0}
        for _slug, entity_type, _path in iter_entity_paths(wiki_dir):
            if entity_type == "skill":
                stats["skills"] += 1
            elif entity_type == "agent":
                stats["agents"] += 1
            elif entity_type == "mcp-server":
                stats["mcps"] += 1
            elif entity_type == "harness":
                stats["harnesses"] += 1
        stats["total"] = sum(stats.values())
        stats["split_known"] = True
        return stats

    base = wiki_dir / "entities"
    graph_out = wiki_dir / "graphify-out"
    if graph_out.is_dir() and (graph_out / "graph-report.md").is_file():
        return {
            "skills": 0,
            "agents": 0,
            "mcps": 0,
            "harnesses": 0,
            "total": int(graph_node_total or 0),
            "split_known": False,
        }
    skills = len(list((base / "skills").glob("*.md"))) if (base / "skills").is_dir() else 0
    agents = len(list((base / "agents").glob("*.md"))) if (base / "agents").is_dir() else 0
    mcp_dir = base / "mcp-servers"
    mcps = len(list(mcp_dir.rglob("*.md"))) if mcp_dir.is_dir() else 0
    harnesses = len(list((base / "harnesses").glob("*.md"))) if (base / "harnesses").is_dir() else 0
    return {
        "skills": skills,
        "agents": agents,
        "mcps": mcps,
        "harnesses": harnesses,
        "total": skills + agents + mcps + harnesses,
        "split_known": True,
    }


def wiki_render_cache_key(
    index_path: Path,
    selected_type: str | None,
    query: str,
    *,
    source_path: Path,
    css_text: str,
    manifest_export_id: str,
    index_matches_manifest: Callable[[Path], bool],
) -> tuple[Any, ...] | None:
    if not index_path.is_file() or not index_matches_manifest(index_path):
        return None
    try:
        index_stat = index_path.stat()
        source_stat = source_path.stat()
    except OSError:
        return None
    try:
        css_hash = hashlib.sha256(css_text.encode("utf-8")).hexdigest()
    except Exception:
        css_hash = ""
    return (
        "wiki-index-v1",
        selected_type or "",
        query,
        str(index_path.resolve()),
        index_stat.st_mtime_ns,
        index_stat.st_size,
        manifest_export_id,
        source_stat.st_mtime_ns,
        source_stat.st_size,
        css_hash,
    )


def wiki_render_disk_cache_path(claude_dir: Path) -> Path:
    return claude_dir / ".ctx-monitor-wiki-cache.json"


def search_entities_from_index(
    index_path: Path,
    query: str = "",
    entity_type: str | None = None,
    *,
    limit: int = 80,
    index_matches_manifest: Callable[[Path], bool],
) -> list[dict[str, Any]] | None:
    terms = [term for term in re.split(r"\s+", query.lower().strip()) if term]
    normalized = normalize_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    if not index_path.is_file() or not index_matches_manifest(index_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        where: list[str] = []
        params: list[object] = []
        if normalized is not None:
            where.append("type = ?")
            params.append(normalized)
        for term in terms:
            where.append(
                "lower(id || ' ' || coalesce(label,'') || ' ' || "
                "coalesce(type,'') || ' ' || coalesce(tags,'') || ' ' || "
                "coalesce(description,'')) LIKE ?"
            )
            params.append(f"%{term}%")
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        if terms:
            first = terms[0]
            order_sql = (
                " ORDER BY CASE "
                "WHEN lower(label) = ? THEN 0 "
                "WHEN lower(id) = ? THEN 1 "
                "WHEN lower(label) LIKE ? THEN 2 "
                "WHEN lower(id) LIKE ? THEN 3 "
                "ELSE 4 END, degree DESC, label COLLATE NOCASE"
            )
            params.extend([first, first, f"{first}%", f"%:{first}%"])
        else:
            order_sql = " ORDER BY degree DESC, label COLLATE NOCASE"
        sql = (
            "SELECT id,label,type,tags,description,quality_score,usage_score,degree "
            f"FROM nodes{where_sql}{order_sql} LIMIT ?"
        )
        params.append(max(1, limit))
        rows = conn.execute(sql, params).fetchall()
    except (sqlite3.Error, TypeError):
        return None
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for node_id, label, row_type, raw_tags, description, quality, usage, degree in rows:
        current_type = normalize_entity_type(row_type) or graph_type_from_node_id(str(node_id))
        slug = graph_slug_from_node_id(str(node_id))
        try:
            tags = json.loads(raw_tags or "[]")
        except json.JSONDecodeError:
            tags = []
        if not isinstance(tags, list):
            tags = []
        results.append(
            {
                "slug": slug,
                "display_slug": display_slug(slug),
                "type": current_type,
                "title": display_label(label, fallback_slug=slug),
                "description": str(description or ""),
                "tags": [str(tag) for tag in tags[:12]],
                "path": "",
                "href": entity_wiki_href(slug, current_type),
                "quality_score": quality,
                "usage_score": usage,
                "degree": int(degree or 0),
            }
        )
    return results


def index_entries(
    wiki_dir: Path,
    index_path: Path,
    *,
    limit_per_type: int | None,
    index_matches_manifest: Callable[[Path], bool],
) -> list[dict[str, Any]]:
    """Return sampled wiki catalog rows for the dashboard index page."""
    indexed = index_entries_from_dashboard_index(
        index_path,
        limit_per_type=limit_per_type,
        index_matches_manifest=index_matches_manifest,
    )
    if indexed is not None:
        return indexed

    paths = iter_entity_paths(wiki_dir)
    if not paths:
        return []
    out: list[dict[str, Any]] = []
    for _sub, entity_type, _recursive in _DASHBOARD_ENTITY_SOURCES:
        seen_for_type = 0
        for slug, current_type, path in paths:
            if current_type != entity_type:
                continue
            if limit_per_type is not None and seen_for_type >= limit_per_type:
                break
            text = read_entity_text(wiki_dir, slug, current_type, path)
            if text is None:
                continue
            head = text[:2048]
            meta, _ = parse_frontmatter_and_body(head)
            all_tags = frontmatter_tags(meta.get("tags", ""), limit=None)
            description, _truncated = truncate_text(
                frontmatter_text(meta.get("description", "")),
                200,
            )
            out.append(
                {
                    "slug": slug,
                    "display_slug": display_slug(slug),
                    "type": entity_type,
                    "tags": all_tags[:6],
                    "search_tags": all_tags,
                    "description": description,
                }
            )
            seen_for_type += 1
    return out


def index_entries_from_dashboard_index(
    index_path: Path,
    *,
    limit_per_type: int | None,
    index_matches_manifest: Callable[[Path], bool],
) -> list[dict[str, Any]] | None:
    if not index_path.is_file() or not index_matches_manifest(index_path):
        return None

    out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
        try:
            for _sub, entity_type, _recursive in _DASHBOARD_ENTITY_SOURCES:
                params: list[Any] = [entity_type]
                limit_sql = ""
                if limit_per_type is not None:
                    limit_sql = " LIMIT ?"
                    params.append(max(0, int(limit_per_type)))
                rows = conn.execute(
                    "SELECT id,label,type,tags,description,quality_score FROM nodes "
                    "WHERE type=? ORDER BY lower(label), id" + limit_sql,
                    params,
                )
                for (
                    node_id,
                    label,
                    row_type,
                    tags_raw,
                    description_raw,
                    quality_score,
                ) in rows:
                    node_id_text = str(node_id)
                    slug = node_id_text.split(":", 1)[1] if ":" in node_id_text else str(label)
                    if not is_safe_slug(slug):
                        continue
                    try:
                        parsed_tags = json.loads(str(tags_raw or "[]"))
                    except json.JSONDecodeError:
                        parsed_tags = []
                    all_tags = [str(tag) for tag in parsed_tags if isinstance(tag, str)]
                    description, _truncated = truncate_text(
                        frontmatter_text(description_raw),
                        200,
                    )
                    out.append(
                        {
                            "slug": slug,
                            "display_slug": display_slug(str(label or slug)),
                            "type": str(row_type or entity_type),
                            "tags": all_tags[:6],
                            "search_tags": all_tags,
                            "description": description,
                            "grade": grade_from_quality_score(quality_score),
                        }
                    )
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
    return out


def graph_slug_from_node_id(node_id: str) -> str:
    return node_id.split(":", 1)[-1]


def graph_type_from_node_id(node_id: str, fallback: str = "skill") -> str:
    prefix = node_id.split(":", 1)[0] if ":" in node_id else ""
    return {
        "skill": "skill",
        "agent": "agent",
        "mcp-server": "mcp-server",
        "harness": "harness",
    }.get(prefix, fallback)


def display_slug(slug: str) -> str:
    return str(slug or "").removeprefix("skills-sh-")


def display_label(value: Any, *, fallback_slug: str = "") -> str:
    return display_slug(str(value or fallback_slug or ""))


def entity_wiki_href(slug: str, entity_type: str | None = None) -> str:
    suffix = f"?type={quote(entity_type)}" if entity_type in _DASHBOARD_ENTITY_TYPES else ""
    return f"/wiki/{quote(slug)}{suffix}"


def frontmatter_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return ""
    return str(value)


def truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(value) <= limit:
        return value, False
    if limit <= 3:
        return value[:limit], True
    return value[: limit - 3].rstrip() + "...", True


def frontmatter_tags(value: Any, *, limit: int | None = 6) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw = frontmatter_text(value)
        raw_items = raw.replace("[", "").replace("]", "").split(",")
    out: list[str] = []
    for item in raw_items:
        tok = str(item).strip().strip("'\"")
        if tok:
            out.append(tok)
        if limit is not None and len(out) >= limit:
            break
    return out


def grade_from_quality_score(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return ""
    if score >= 0.80:
        return "A"
    if score >= 0.60:
        return "B"
    if score >= 0.40:
        return "C"
    if score >= 0.0:
        return "D"
    return ""
