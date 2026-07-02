"""Shared entity-type helpers for ctx wiki and graph surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Collection


ENTITY_TYPES: tuple[str, ...] = (
    "skill",
    "agent",
    "mcp-server",
    "plugin",
    "harness",
)

RECOMMENDABLE_ENTITY_TYPES: tuple[str, ...] = (
    "skill",
    "agent",
    "mcp-server",
    "harness",
)

SUBJECT_TYPE_FOR_ENTITY_TYPE: dict[str, str] = {
    "skill": "skills",
    "agent": "agents",
    "mcp-server": "mcp-servers",
    "plugin": "plugins",
    "harness": "harnesses",
}

ENTITY_TYPE_FOR_SUBJECT_TYPE: dict[str, str] = {
    subject: entity_type for entity_type, subject in SUBJECT_TYPE_FOR_ENTITY_TYPE.items()
}

INDEX_SECTION_FOR_SUBJECT: dict[str, str] = {
    "skills": "## Skills",
    "agents": "## Agents",
    "mcp-servers": "## MCP Servers",
    "plugins": "## Plugins",
    "harnesses": "## Harnesses",
}

RELATED_SECTION_FOR_ENTITY_TYPE: dict[str, str] = {
    "skill": "## Related Skills",
    "agent": "## Related Agents",
    "mcp-server": "## Related MCP Servers",
    "harness": "## Related Harnesses",
}

ENTITY_TYPE_ALIASES: dict[str, str] = {
    "skills": "skill",
    "skill": "skill",
    "agents": "agent",
    "agent": "agent",
    "mcp": "mcp-server",
    "mcp-server": "mcp-server",
    "mcp-servers": "mcp-server",
    "plugins": "plugin",
    "plugin": "plugin",
    "harness": "harness",
    "harnesses": "harness",
}


def normalize_entity_type(
    raw: object,
    *,
    allowed: Collection[str] = ENTITY_TYPES,
) -> str | None:
    """Return a canonical entity type for user/API aliases."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    normalized = ENTITY_TYPE_ALIASES.get(value.lower(), value)
    return normalized if normalized in allowed else None


def entity_source_specs(
    entity_types: Collection[str] = RECOMMENDABLE_ENTITY_TYPES,
) -> tuple[tuple[str, str, bool], ...]:
    """Return ``(subject_type, entity_type, recursive)`` wiki source specs."""
    specs: list[tuple[str, str, bool]] = []
    for entity_type in RECOMMENDABLE_ENTITY_TYPES:
        if entity_type not in entity_types:
            continue
        subject_type = SUBJECT_TYPE_FOR_ENTITY_TYPE[entity_type]
        specs.append((subject_type, entity_type, entity_type == "mcp-server"))
    return tuple(specs)


def mcp_shard(slug: str) -> str:
    """Return the shard segment for an MCP slug."""
    first = slug[0].lower() if slug else ""
    return first if first.isalpha() else "0-9"


def entity_relpath(entity_type: str, slug: str) -> Path | None:
    """Return the wiki-relative markdown path for an entity."""
    subject_type = SUBJECT_TYPE_FOR_ENTITY_TYPE.get(entity_type)
    if subject_type is None:
        return None
    if entity_type == "mcp-server":
        return Path("entities") / subject_type / mcp_shard(slug) / f"{slug}.md"
    return Path("entities") / subject_type / f"{slug}.md"


def entity_page_path(wiki: Path, entity_type: str, slug: str) -> Path | None:
    """Return the absolute wiki page path for an entity."""
    relpath = entity_relpath(entity_type, slug)
    return wiki / relpath if relpath is not None else None


def entity_index_link(subject_type: str, slug: str) -> str | None:
    """Return the extensionless wiki index target for a subject/slug pair."""
    entity_type = ENTITY_TYPE_FOR_SUBJECT_TYPE.get(subject_type)
    if entity_type is None:
        return None
    if entity_type == "mcp-server" and not slug:
        return f"entities/{subject_type}/{mcp_shard(slug)}/"
    relpath = entity_relpath(entity_type, slug)
    return relpath.with_suffix("").as_posix() if relpath is not None else None


def entity_wikilink(entity_type: str, slug: str) -> str | None:
    """Return an Obsidian-style wikilink for an entity."""
    relpath = entity_relpath(entity_type, slug)
    if relpath is None:
        return None
    return f"[[{relpath.with_suffix('').as_posix()}]]"
