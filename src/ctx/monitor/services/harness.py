"""Read-only harness catalog helpers for ctx-monitor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body
from ctx.monitor.services import sidecars as sidecar_service
from ctx.utils._safe_name import is_safe_source_name


def frontmatter_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return ""
    return str(value)


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


def truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(value) <= limit:
        return value, False
    if limit <= 3:
        return value[:limit], True
    return value[: limit - 3].rstrip() + "...", True


def harness_wizard_sidecar(sidecar_dir: Path, slug: str) -> dict[str, Any] | None:
    """Load harness sidecar candidates without scanning every sidecar file."""
    if not is_safe_source_name(slug):
        return None
    for path in (
        sidecar_dir / f"{slug}.json",
        sidecar_dir / f"{slug}-harness.json",
    ):
        if not path.exists():
            continue
        sidecar = sidecar_service.read_sidecar_file(path)
        if sidecar is None:
            continue
        if (
            sidecar.get("slug") == slug
            and sidecar_service.sidecar_entity_type(sidecar) == "harness"
        ):
            return sidecar
    return None


def harness_wizard_entries(
    wiki_dir: Path,
    sidecar_dir: Path,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Return catalog harness pages for the manual dashboard wizard."""
    harness_dir = wiki_dir / "entities" / "harnesses"
    if not harness_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(harness_dir.glob("*.md"), key=lambda p: p.stem.lower()):
        if len(rows) >= limit:
            break
        slug = path.stem
        if not is_safe_source_name(slug):
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        meta, _body = parse_frontmatter_and_body(head)
        sidecar = harness_wizard_sidecar(sidecar_dir, slug) or {}
        score = float(sidecar.get("raw_score", sidecar.get("score", 0.0)) or 0.0)
        tags = frontmatter_tags(meta.get("tags", ""), limit=None)
        description, _truncated = truncate_text(
            frontmatter_text(meta.get("description", "")),
            260,
        )
        repo_url = frontmatter_text(
            meta.get("repo_url") or meta.get("github_url") or meta.get("homepage_url") or ""
        )
        rows.append(
            {
                "slug": slug,
                "title": frontmatter_text(meta.get("title") or meta.get("name") or slug),
                "description": description,
                "tags": tags[:12],
                "score": score,
                "grade": str(sidecar.get("grade") or ""),
                "repo_url": repo_url,
            }
        )
    return sorted(rows, key=lambda row: (-float(row["score"]), str(row["slug"])))
