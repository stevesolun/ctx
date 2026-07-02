"""Dashboard entity search, CRUD, and runtime load/unload helpers.

``ctx_monitor`` owns HTTP routing and local paths.  This module owns the
catalog-management behavior so search/upsert/delete/load/unload logic can be
tested and reviewed without the full monitor server surface.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class EntityCrudDeps:
    is_safe_slug: Callable[[str], bool]
    normalize_entity_type: Callable[[object], str | None]
    wiki_entity_detail: Callable[[str, str | None], dict[str, Any] | None]
    wiki_entity_target_path: Callable[[str, str], Path]
    wiki_entity_path: Callable[[str, str | None], Path | None]
    iter_wiki_entity_paths: Callable[[str | None], list[tuple[str, str, Path]]]
    read_manifest: Callable[[], dict[str, Any]]
    perform_unload: Callable[[str, str], tuple[bool, str]]
    queue_entity_refresh: Callable[[str, str, Path, str, str], None]
    file_lock: Callable[[Path], AbstractContextManager[Any]]
    write_entity_text: Callable[[Path, str], None]
    parse_frontmatter: Callable[[str], tuple[dict[str, Any], str]]
    frontmatter_tags: Callable[[Any], list[str]]
    frontmatter_text: Callable[[Any], str]
    display_slug: Callable[[str], str]
    display_label: Callable[[Any], str]
    entity_wiki_href: Callable[[str, str], str]
    scan_skill_content: Callable[[str, str], tuple[bool, str]]


@dataclass(frozen=True)
class EntityRuntimeDeps:
    is_safe_slug: Callable[[str], bool]
    normalize_entity_type: Callable[[object], str | None]
    wiki_dir: Callable[[], Path]
    claude_dir: Callable[[], Path]
    log_dashboard_entity_event: Callable[[str, str, str], None]
    remove_loaded_manifest_entry: Callable[[str, str], list[dict[str, Any]]]


def normalize_entity_tags(raw: Any) -> list[str]:
    if isinstance(raw, list):
        parts = raw
    else:
        parts = re.split(r"[,\n]+", str(raw or ""))
    tags: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = re.sub(r"[^a-z0-9_.+-]+", "-", str(part).lower()).strip("-_.+")
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return '""'
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _./:+@-]*", text):
        return text
    return json.dumps(text, ensure_ascii=False)


def frontmatter_to_text(frontmatter: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in frontmatter.items():
        if value is None or value == "":
            continue
        if isinstance(value, list):
            rendered = ", ".join(yaml_scalar(item) for item in value)
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def entity_content_from_payload(
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
    is_safe_slug: Callable[[str], bool],
    normalize_entity_type: Callable[[object], str | None],
) -> tuple[str, str, str]:
    slug = str(payload.get("slug", "")).strip()
    if not is_safe_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    entity_type = str(payload.get("entity_type", "skill")).strip() or "skill"
    normalized = normalize_entity_type(entity_type)
    if normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    body = str(payload.get("body", "")).strip()
    if not body:
        raise ValueError("body is required")
    title = str(payload.get("title") or slug).strip()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    frontmatter = dict(existing or {})
    frontmatter["title"] = title
    frontmatter["type"] = normalized
    frontmatter.setdefault("created", today)
    frontmatter["updated"] = today
    description = str(payload.get("description") or "").strip()
    if description or "description" in payload:
        frontmatter.pop("description", None)
    if description:
        frontmatter["description"] = description
    tags = normalize_entity_tags(payload.get("tags"))
    if tags or "tags" in payload:
        frontmatter.pop("tags", None)
    if tags:
        frontmatter["tags"] = tags
    source_url = str(payload.get("source_url") or "").strip()
    if source_url or "source_url" in payload:
        frontmatter.pop("source_url", None)
    if source_url:
        frontmatter["source_url"] = source_url
    return slug, normalized, frontmatter_to_text(frontmatter) + body.rstrip() + "\n"


def perform_load(
    slug: str,
    entity_type: str = "skill",
    *,
    command: str | None = None,
    json_config: str | None = None,
    deps: EntityRuntimeDeps,
) -> tuple[bool, str]:
    """Install/load one entity from the wiki. Returns (ok, message)."""
    if not deps.is_safe_slug(slug):
        return False, f"invalid slug: {slug!r}"
    normalized_entity_type = deps.normalize_entity_type(entity_type)
    if normalized_entity_type is None:
        return False, f"unsupported entity_type: {entity_type!r}"
    entity_type = normalized_entity_type
    if entity_type == "harness":
        return (
            False,
            "harness installs are managed by ctx-harness-install; "
            f"run: ctx-harness-install {slug} --dry-run",
        )
    result: Any
    try:
        if entity_type == "agent":
            from ctx.adapters.claude_code.install.agent_install import install_agent

            result = install_agent(
                slug,
                wiki_dir=deps.wiki_dir(),
                agents_dir=deps.claude_dir() / "agents",
            )
        elif entity_type == "mcp-server":
            from ctx.adapters.claude_code.install.mcp_install import install_mcp

            result = install_mcp(
                slug,
                wiki_dir=deps.wiki_dir(),
                command=command,
                json_config=json_config,
                auto=True,
            )
        else:
            from ctx.adapters.claude_code.install.skill_install import install_skill

            result = install_skill(
                slug,
                wiki_dir=deps.wiki_dir(),
                skills_dir=deps.claude_dir() / "skills",
                security_scan=True,
                security_scan_required=True,
            )
    except ImportError as exc:
        return False, f"install import failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if result.status not in ("installed", "skipped-existing"):
        return False, f"load failed: {result.message or result.status}"
    deps.log_dashboard_entity_event(entity_type, "loaded", slug)
    message = result.message or f"loaded {entity_type}:{slug}"
    scan = getattr(result, "security_scan", None)
    scan_output = str(getattr(scan, "output", "") or "").strip()
    if scan_output:
        message = f"{message}\n\nSkillSpector report:\n{scan_output}"
    return True, message


def perform_unload(
    slug: str,
    entity_type: str = "skill",
    *,
    deps: EntityRuntimeDeps,
) -> tuple[bool, str]:
    """Unload one entity by routing to the correct installer/uninstaller."""
    if not deps.is_safe_slug(slug):
        return False, f"invalid slug: {slug!r}"
    normalized_entity_type = deps.normalize_entity_type(entity_type)
    if normalized_entity_type is None:
        return False, f"unsupported entity_type: {entity_type!r}"
    entity_type = normalized_entity_type
    if entity_type == "harness":
        return (
            False,
            "harness installs are managed by ctx-harness-install; "
            f"run: ctx-harness-install {slug} --uninstall --dry-run",
        )
    if entity_type == "mcp-server":
        try:
            from ctx.adapters.claude_code.install.mcp_install import uninstall_mcp
        except ImportError as exc:
            return False, f"mcp_install import failed: {exc}"
        try:
            result = uninstall_mcp(slug, wiki_dir=deps.wiki_dir())
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if result.status not in ("uninstalled",):
            return False, f"uninstall failed: {result.message or result.status}"
        deps.log_dashboard_entity_event("mcp-server", "unloaded", slug)
        return True, f"unloaded mcp:{slug}"

    if entity_type == "agent":
        try:
            removed_entries = deps.remove_loaded_manifest_entry(slug, "agent")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if not removed_entries:
            return False, f"{slug} was not in the loaded set"
        deps.log_dashboard_entity_event("agent", "unloaded", slug)
        return True, f"unloaded {slug}"

    # Skills keep using the existing skill_unload module so skill-events.jsonl
    # remains compatible with older usage and retention analytics.
    try:
        from ctx.adapters.claude_code.install.skill_unload import unload_from_session
    except ImportError as exc:
        return False, f"skill_unload import failed: {exc}"
    try:
        removed = unload_from_session([slug], entity_type=entity_type)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if not removed:
        return False, f"{slug} was not in the loaded set"
    return True, f"unloaded {', '.join(removed)}"


def search_wiki_entities(
    query: str = "",
    entity_type: str | None = None,
    *,
    limit: int = 80,
    deps: EntityCrudDeps,
) -> list[dict[str, Any]]:
    terms = [term for term in re.split(r"\s+", query.lower().strip()) if term]
    results: list[dict[str, Any]] = []
    for slug, current_type, path in deps.iter_wiki_entity_paths(entity_type):
        detail = deps.wiki_entity_detail(slug, current_type)
        if isinstance(detail, dict):
            frontmatter = detail.get("frontmatter")
            body = str(detail.get("body") or "")[:4096]
        else:
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                continue
            frontmatter, body = deps.parse_frontmatter(head)
        if not isinstance(frontmatter, dict):
            frontmatter = {}
        tags = deps.frontmatter_tags(frontmatter.get("tags", ""))
        description = deps.frontmatter_text(frontmatter.get("description", ""))
        display_slug = deps.display_slug(slug)
        title = deps.display_label(
            deps.frontmatter_text(frontmatter.get("title") or frontmatter.get("name") or slug),
        )
        haystack = " ".join(
            [slug, display_slug, current_type, title, description, " ".join(tags), body],
        ).lower()
        if terms and not all(term in haystack for term in terms):
            continue
        results.append(
            {
                "slug": slug,
                "display_slug": display_slug,
                "type": current_type,
                "title": title,
                "description": description,
                "tags": tags[:12],
                "path": str(path),
                "href": deps.entity_wiki_href(slug, current_type),
            }
        )
        if len(results) >= max(1, limit):
            break
    return results


def entity_live_in_manifest(slug: str, entity_type: str, *, deps: EntityCrudDeps) -> bool:
    manifest = deps.read_manifest()
    for entry in manifest.get("load", []):
        if not isinstance(entry, dict):
            continue
        entry_slug = str(entry.get("skill") or entry.get("slug") or "")
        entry_type = deps.normalize_entity_type(
            str(entry.get("entity_type") or entry.get("type") or "skill"),
        )
        if entry_slug == slug and entry_type == entity_type:
            return True
    return False


def upsert_wiki_entity(payload: dict[str, Any], *, deps: EntityCrudDeps) -> tuple[bool, str]:
    try:
        requested_slug = str(payload.get("slug", "")).strip()
        requested_type = str(payload.get("entity_type", "skill")).strip() or "skill"
        existing_detail = deps.wiki_entity_detail(requested_slug, requested_type)
        existing_meta = (
            existing_detail.get("frontmatter") if isinstance(existing_detail, dict) else None
        )
        confirm_update = str(payload.get("confirm_update", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        if existing_detail is not None and not confirm_update:
            return (
                False,
                f"existing {requested_type}:{requested_slug} found; review before "
                "replacing. Benefit: keeps the catalog current. Risk: a lower-quality "
                "manual edit can degrade recommendations. Resubmit with "
                "confirm_update=true to apply.",
            )
        slug, entity_type, content = entity_content_from_payload(
            payload,
            existing=existing_meta if isinstance(existing_meta, dict) else None,
            is_safe_slug=deps.is_safe_slug,
            normalize_entity_type=deps.normalize_entity_type,
        )
        if entity_type == "skill":
            scan_ok, scan_detail = deps.scan_skill_content(slug, content)
            if not scan_ok:
                return False, scan_detail
        path = deps.wiki_entity_target_path(slug, entity_type)
        with deps.file_lock(path):
            deps.write_entity_text(path, content)
        deps.queue_entity_refresh(entity_type, slug, path, content, "upsert")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"saved {entity_type}:{slug} and queued graph refresh"


def delete_wiki_entity(
    slug: str,
    entity_type: str,
    *,
    deps: EntityCrudDeps,
) -> tuple[bool, str]:
    try:
        normalized = deps.normalize_entity_type(entity_type)
        if normalized is None:
            raise ValueError(f"unsupported entity_type: {entity_type!r}")
        if not deps.is_safe_slug(slug):
            raise ValueError(f"invalid slug: {slug!r}")
        path = deps.wiki_entity_path(slug, normalized)
        if path is None:
            return False, f"no wiki entity found for {normalized}:{slug}"
        if entity_live_in_manifest(slug, normalized, deps=deps):
            unloaded, unload_detail = deps.perform_unload(slug, normalized)
            if not unloaded:
                return (
                    False,
                    f"{normalized}:{slug} is loaded; unload before delete failed: {unload_detail}",
                )
        with deps.file_lock(path):
            path.unlink()
        deps.queue_entity_refresh(normalized, slug, path, "", "delete")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"deleted {normalized}:{slug} and queued graph refresh"
