"""Quality sidecar loading, indexing, and catalog pagination."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ctx.utils._safe_name import is_safe_source_name


_SIDECAR_INDEX_CACHE_KEY: tuple[tuple[Path, float, int], ...] | None = None
_SIDECAR_INDEX_CACHE_VALUE: dict[tuple[str, str], dict] | None = None
_SIDECAR_FILTER_CACHE_SIGNATURE: tuple[Any, ...] | None = None
_SIDECAR_FILTER_CACHE_VALUE: dict[tuple[Any, ...], list[dict[str, Any]]] = {}


def reset_caches() -> None:
    global _SIDECAR_INDEX_CACHE_KEY, _SIDECAR_INDEX_CACHE_VALUE
    global _SIDECAR_FILTER_CACHE_SIGNATURE, _SIDECAR_FILTER_CACHE_VALUE

    _SIDECAR_INDEX_CACHE_KEY = None
    _SIDECAR_INDEX_CACHE_VALUE = None
    _SIDECAR_FILTER_CACHE_SIGNATURE = None
    _SIDECAR_FILTER_CACHE_VALUE = {}


def sidecar_entity_type(sidecar: dict, fallback: str = "skill") -> str:
    raw = str(
        sidecar.get("entity_type")
        or sidecar.get("subject_type")
        or sidecar.get("type")
        or fallback
    )
    return {
        "skills": "skill",
        "skill": "skill",
        "agents": "agent",
        "agent": "agent",
        "mcp": "mcp-server",
        "mcp-server": "mcp-server",
        "mcp-servers": "mcp-server",
        "harness": "harness",
        "harnesses": "harness",
    }.get(raw, raw)


def sidecar_fallback_type(path: Path) -> str:
    return "mcp-server" if path.parent.name == "mcp" else "skill"


def read_sidecar_file(path: Path) -> dict | None:
    try:
        sidecar = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(sidecar, dict):
        return None
    etype = sidecar_entity_type(sidecar, sidecar_fallback_type(path))
    sidecar.setdefault("slug", path.stem)
    sidecar["subject_type"] = etype
    return sidecar


def load_sidecar(
    sidecar_dir: Path,
    slug: str,
    entity_type: str | None = None,
) -> dict | None:
    if not is_safe_source_name(slug):
        return None
    paths = [
        sidecar_dir / f"{slug}.json",
        sidecar_dir / "mcp" / f"{slug}.json",
    ]
    if entity_type is not None:
        suffixes = [entity_type]
        if entity_type == "mcp-server":
            suffixes.append("mcp")
        for suffix in suffixes:
            paths.append(sidecar_dir / f"{slug}-{suffix}.json")

    for path in paths:
        if not path.exists():
            continue
        sidecar = read_sidecar_file(path)
        if sidecar is None:
            continue
        if entity_type is None or sidecar_entity_type(sidecar) == entity_type:
            return sidecar
    if entity_type is not None and _SIDECAR_INDEX_CACHE_VALUE is not None:
        return sidecar_index(sidecar_dir).get((slug, entity_type))
    return None


def sidecar_files(sidecar_dir: Path) -> list[Path]:
    files: list[Path] = []
    for root in (sidecar_dir, sidecar_dir / "mcp"):
        if not root.is_dir():
            continue
        files.extend(
            p for p in sorted(root.glob("*.json"))
            if not p.name.startswith(".")
            and not p.name.endswith(".lifecycle.json")
        )
    return files


def sidecar_index_cache_key(sidecar_dir: Path) -> tuple[tuple[Path, float, int], ...]:
    keys: list[tuple[Path, float, int]] = []
    for path in sidecar_files(sidecar_dir):
        stat = path.stat()
        keys.append((path.resolve(), stat.st_mtime, stat.st_size))
    if keys:
        return tuple(keys)
    for root in (sidecar_dir, sidecar_dir / "mcp"):
        if not root.is_dir():
            continue
        stat = root.stat()
        keys.append((root.resolve(), stat.st_mtime, stat.st_size))
    return tuple(keys)


def sidecar_index(sidecar_dir: Path) -> dict[tuple[str, str], dict]:
    global _SIDECAR_INDEX_CACHE_KEY, _SIDECAR_INDEX_CACHE_VALUE

    cache_key = sidecar_index_cache_key(sidecar_dir)
    if _SIDECAR_INDEX_CACHE_KEY == cache_key and _SIDECAR_INDEX_CACHE_VALUE is not None:
        return _SIDECAR_INDEX_CACHE_VALUE

    index: dict[tuple[str, str], dict] = {}
    for path in sidecar_files(sidecar_dir):
        sidecar = read_sidecar_file(path)
        if sidecar is None:
            continue
        slug = str(sidecar.get("slug") or path.stem)
        entity_type = sidecar_entity_type(sidecar)
        index.setdefault((slug, entity_type), sidecar)
    _SIDECAR_INDEX_CACHE_KEY = cache_key
    _SIDECAR_INDEX_CACHE_VALUE = index
    return index


def all_sidecars(sidecar_dir: Path) -> list[dict]:
    return list(sidecar_index(sidecar_dir).values())


def skills_page_int(
    value: str | None,
    *,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def skills_query_values(raw: str | None, allowed: set[str]) -> set[str]:
    values = {
        item.strip()
        for item in str(raw or "").split(",")
        if item.strip()
    }
    return {item for item in values if item in allowed}


def sidecar_sort_key(sidecar: dict) -> tuple[str, float, str]:
    return (
        str(sidecar.get("grade") or "F"),
        -float(sidecar.get("raw_score") or sidecar.get("score") or 0.0),
        str(sidecar.get("slug") or ""),
    )


def sidecar_card_payload(sidecar: dict) -> dict[str, Any]:
    slug = str(sidecar.get("slug") or "")
    entity_type = sidecar_entity_type(sidecar)
    return {
        "slug": slug,
        "grade": str(sidecar.get("grade") or "F"),
        "type": entity_type,
        "hard_floor": str(sidecar.get("hard_floor") or ""),
        "raw_score": float(sidecar.get("raw_score") or sidecar.get("score") or 0.0),
        "sidecar_href": f"/skill/{quote(slug)}?type={quote(entity_type)}",
        "wiki_href": f"/wiki/{quote(slug)}?type={quote(entity_type)}",
        "graph_href": f"/graph?slug={quote(slug)}&type={quote(entity_type)}",
    }


def sidecar_filter_signature(
    sidecar_dir: Path,
    files: list[Path],
) -> tuple[Any, ...]:
    signature: list[tuple[str, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((
            str(path.resolve()),
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(stat.st_size),
        ))
    if signature:
        return tuple(signature)
    roots = (sidecar_dir, sidecar_dir / "mcp")
    for root in roots:
        if not root.is_dir():
            signature.append((str(root.resolve()), 0, 0))
            continue
        stat = root.stat()
        signature.append((
            str(root.resolve()),
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            0,
        ))
    return tuple(signature)


def sidecar_candidate_files(
    files: list[Path],
    *,
    q: str,
    types: set[str],
) -> list[Path]:
    q_lower = q.lower()
    candidates = [
        path for path in files
        if not q_lower or q_lower in path.stem.lower()
    ]
    if not types:
        return candidates
    if types == {"mcp-server"}:
        return [path for path in candidates if path.parent.name == "mcp"]
    if "mcp-server" not in types:
        return [path for path in candidates if path.parent.name != "mcp"]
    return candidates


def sidecar_matches_filters(
    sidecar: dict,
    *,
    q: str,
    types: set[str],
    grades: set[str],
    hide_floor: bool,
) -> bool:
    entity_type = sidecar_entity_type(sidecar)
    grade = str(sidecar.get("grade") or "F")
    floor = str(sidecar.get("hard_floor") or "")
    if types and entity_type not in types:
        return False
    if grades and grade not in grades:
        return False
    if hide_floor and floor:
        return False
    if q:
        return q.lower() in str(sidecar.get("slug") or "").lower()
    return True


def filtered_sidecar_records(
    sidecar_dir: Path,
    files: list[Path],
    *,
    q: str,
    types: set[str],
    grades: set[str],
    hide_floor: bool,
) -> list[dict[str, Any]]:
    """Return cached filtered sidecar card records for /skills search."""
    global _SIDECAR_FILTER_CACHE_SIGNATURE, _SIDECAR_FILTER_CACHE_VALUE

    signature = sidecar_filter_signature(sidecar_dir, files)
    if _SIDECAR_FILTER_CACHE_SIGNATURE != signature:
        _SIDECAR_FILTER_CACHE_SIGNATURE = signature
        _SIDECAR_FILTER_CACHE_VALUE = {}
    cache_key = (
        q.lower(),
        tuple(sorted(types)),
        tuple(sorted(grades)),
        hide_floor,
    )
    cached = _SIDECAR_FILTER_CACHE_VALUE.get(cache_key)
    if cached is not None:
        return cached

    records: list[dict[str, Any]] = []
    for path in sidecar_candidate_files(files, q=q, types=types):
        sidecar = read_sidecar_file(path)
        if sidecar is None:
            continue
        if not sidecar_matches_filters(
            sidecar,
            q=q,
            types=types,
            grades=grades,
            hide_floor=hide_floor,
        ):
            continue
        records.append(sidecar_card_payload(sidecar))
    records.sort(key=sidecar_sort_key)
    if len(_SIDECAR_FILTER_CACHE_VALUE) >= 32:
        _SIDECAR_FILTER_CACHE_VALUE.clear()
    _SIDECAR_FILTER_CACHE_VALUE[cache_key] = records
    return records


def sidecar_page_payload(
    sidecar_dir: Path,
    qs: dict[str, str] | None = None,
    *,
    entity_types: tuple[str, ...],
    default_limit: int,
    max_limit: int,
) -> dict[str, Any]:
    """Return a paginated sidecar payload for /skills and its JSON API."""
    qs = qs or {}
    page = skills_page_int(qs.get("page"), default=1)
    limit = skills_page_int(
        qs.get("limit"),
        default=default_limit,
        maximum=max_limit,
    )
    q = str(qs.get("q") or "").strip()
    types = skills_query_values(qs.get("type"), set(entity_types))
    grades = skills_query_values(qs.get("grade"), {"A", "B", "C", "D", "F"})
    hide_floor = str(qs.get("hide_floor") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }

    files = sidecar_files(sidecar_dir)
    catalog_total = len(files)
    has_filters = bool(q or types or grades or hide_floor)
    if has_filters:
        sidecars = filtered_sidecar_records(
            sidecar_dir,
            files,
            q=q,
            types=types,
            grades=grades,
            hide_floor=hide_floor,
        )
        total = len(sidecars)
        start = (page - 1) * limit
        page_sidecars = sidecars[start:start + limit]
    else:
        total = catalog_total
        start = (page - 1) * limit
        selected_files = files[start:start + limit]
        page_sidecars = [
            sidecar
            for path in selected_files
            if (sidecar := read_sidecar_file(path)) is not None
        ]
        if catalog_total <= limit:
            page_sidecars.sort(key=sidecar_sort_key)

    pages = max(1, math.ceil(total / limit)) if total else 1
    if page > pages:
        page = pages
        return sidecar_page_payload(
            sidecar_dir,
            {
                **qs,
                "page": str(page),
                "limit": str(limit),
            },
            entity_types=entity_types,
            default_limit=default_limit,
            max_limit=max_limit,
        )

    return {
        "items": [sidecar_card_payload(sidecar) for sidecar in page_sidecars],
        "total": total,
        "catalog_total": catalog_total,
        "page": page,
        "limit": limit,
        "pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1,
        "filtered": has_filters,
        "q": q,
        "types": sorted(types),
        "grades": sorted(grades),
        "hide_floor": hide_floor,
    }


def grade_distribution(sidecar_dir: Path) -> dict[str, int]:
    dist = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    for sidecar in all_sidecars(sidecar_dir):
        grade = sidecar.get("grade")
        if grade in dist:
            dist[grade] += 1
    return dist


def grade_distribution_payload(sidecar_dir: Path) -> dict[str, Any]:
    grades = grade_distribution(sidecar_dir)
    return {"grades": grades, "total": sum(grades.values())}
