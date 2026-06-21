"""Read-only wiki entity helpers for ctx-monitor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ctx.core import entity_types as core_entity_types
from ctx.core.wiki.wiki_packs import load_merged_wiki_pages
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body
from ctx.utils._safe_name import is_safe_source_name


_WIKI_PACK_CACHE_KEY: tuple[Any, ...] | None = None
_WIKI_PACK_CACHE_VALUE: dict[str, str] | None = None
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


def wiki_pack_pages(wiki_dir: Path) -> dict[str, str] | None:
    """Return merged wiki-pack pages, or None when packs are not installed."""
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

    pages = load_merged_wiki_pages(packs_dir)
    _WIKI_PACK_CACHE_KEY = cache_key
    _WIKI_PACK_CACHE_VALUE = pages
    return pages


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
    pack_pages = wiki_pack_pages(wiki_dir)
    for _sub, current_type, _recursive in _DASHBOARD_ENTITY_SOURCES:
        if normalized is not None and normalized != current_type:
            continue
        path = core_entity_types.entity_page_path(wiki_dir, current_type, slug)
        if path is None:
            continue
        if pack_pages is not None:
            relpath = core_entity_types.entity_relpath(current_type, slug)
            if relpath is not None and relpath.as_posix() in pack_pages:
                return path
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
    normalized = normalize_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    pack_pages = wiki_pack_pages(wiki_dir)
    if pack_pages is not None:
        pack_rows: list[tuple[str, str, Path]] = []
        for relpath in sorted(pack_pages):
            parsed = pack_entity_from_relpath(relpath)
            if parsed is None:
                continue
            slug, current_type = parsed
            if normalized is not None and normalized != current_type:
                continue
            path = core_entity_types.entity_page_path(wiki_dir, current_type, slug)
            if path is not None:
                pack_rows.append((slug, current_type, path))
        return sorted(pack_rows, key=lambda row: (row[1], row[0].lower(), row[2].as_posix()))
    base = wiki_dir / "entities"
    if not base.is_dir():
        return []
    file_rows: list[tuple[str, str, Path]] = []
    for sub, current_type, recursive in _DASHBOARD_ENTITY_SOURCES:
        if normalized is not None and normalized != current_type:
            continue
        root = base / sub
        if not root.is_dir():
            continue
        paths = root.rglob("*.md") if recursive else root.glob("*.md")
        for path in paths:
            slug = path.stem
            if is_safe_slug(slug):
                file_rows.append((slug, current_type, path))
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
        "path": str(path),
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
    pack_pages = wiki_pack_pages(wiki_dir)
    if pack_pages is not None:
        entity_types = (
            [entity_type] if entity_type is not None else list(_DASHBOARD_ENTITY_TYPES)
        )
        for current_type in entity_types:
            relpath = core_entity_types.entity_relpath(current_type, slug)
            if relpath is not None and relpath.as_posix() in pack_pages:
                return pack_pages[relpath.as_posix()]
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
