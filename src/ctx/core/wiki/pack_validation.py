"""Validation gates for modular graph/wiki pack promotion."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx

from ctx.core.entity_types import RECOMMENDABLE_ENTITY_TYPES, entity_relpath
from ctx.core.graph.graph_packs import GraphPackManifestError, discover_pack_manifests
from ctx.core.wiki.wiki_packs import WikiPackManifestError, discover_wiki_pack_manifests

PACK_COMPACTION_MANIFEST = "pack-compaction-manifest.json"
PACK_COMPACTION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GraphWikiConsistencyReport:
    """Graph/wiki consistency report for one merged pack view."""

    missing_wiki_pages: list[dict[str, object]]
    orphan_wiki_pages: list[dict[str, str]]
    stale_wiki_links: list[dict[str, str]]

    @property
    def ok(self) -> bool:
        """Return whether the merged graph and wiki entity views agree."""
        return (
            not self.missing_wiki_pages and not self.orphan_wiki_pages and not self.stale_wiki_links
        )

    def errors(self) -> list[str]:
        """Return human-readable validation errors."""
        errors: list[str] = []
        if self.missing_wiki_pages:
            errors.append(f"missing wiki pages: {len(self.missing_wiki_pages)}")
        if self.orphan_wiki_pages:
            errors.append(f"orphan wiki pages: {len(self.orphan_wiki_pages)}")
        if self.stale_wiki_links:
            errors.append(f"stale wiki links: {len(self.stale_wiki_links)}")
        return errors


def validate_graph_wiki_consistency(
    graph: nx.Graph,
    pages: dict[str, str],
) -> GraphWikiConsistencyReport:
    """Validate known graph entity nodes against merged wiki entity pages."""
    normalised_pages = {_normalise_relpath(path) for path in pages}
    graph_nodes = _graph_entity_nodes(graph)
    missing: list[dict[str, object]] = []
    for node_id, entity_type, slug in graph_nodes:
        expected_paths = _entity_page_candidates(entity_type, slug)
        if expected_paths & normalised_pages:
            continue
        missing.append(
            {
                "node_id": node_id,
                "expected_paths": sorted(expected_paths),
            }
        )
    graph_node_ids = {node_id for node_id, _entity_type, _slug in graph_nodes}
    orphan_pages = [
        {"path": page, "expected_node_id": node_id}
        for page in sorted(normalised_pages)
        for node_id in [_node_id_for_entity_page(page)]
        if node_id is not None and node_id not in graph_node_ids
    ]
    return GraphWikiConsistencyReport(
        missing_wiki_pages=missing,
        orphan_wiki_pages=orphan_pages,
        stale_wiki_links=_stale_entity_wikilinks(pages, normalised_pages, graph_node_ids),
    )


def validate_pack_compaction_manifest(
    *,
    staged_graph_packs_dir: Path,
    staged_wiki_packs_dir: Path,
) -> dict[str, object]:
    """Validate the top-level manifest tying staged graph/wiki packs together."""
    graph_dir = Path(staged_graph_packs_dir)
    wiki_dir = Path(staged_wiki_packs_dir)
    if graph_dir.parent != wiki_dir.parent:
        raise ValueError("staged graph/wiki pack dirs must share one staging root")
    manifest_path = graph_dir.parent / PACK_COMPACTION_MANIFEST
    if not manifest_path.is_file():
        raise ValueError(f"{PACK_COMPACTION_MANIFEST} is missing")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{PACK_COMPACTION_MANIFEST} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{PACK_COMPACTION_MANIFEST} must contain an object")
    if payload.get("schema_version") != PACK_COMPACTION_SCHEMA_VERSION:
        raise ValueError("pack compaction manifest schema_version is not supported")
    if payload.get("operation") != "pack-compaction-stage":
        raise ValueError("pack compaction manifest operation is not pack-compaction-stage")
    _require_path(payload, "staged_graph_packs_dir", graph_dir)
    _require_path(payload, "staged_wiki_packs_dir", wiki_dir)
    base_export_id = _require_str(payload, "base_export_id")
    graph_section = _require_mapping(payload, "graph")
    wiki_section = _require_mapping(payload, "wiki")
    if graph_section.get("base_export_id") != base_export_id:
        raise ValueError("graph base_export_id does not match compaction manifest")
    if wiki_section.get("base_export_id") != base_export_id:
        raise ValueError("wiki base_export_id does not match compaction manifest")
    if graph_section != _single_graph_manifest(graph_dir):
        raise ValueError("graph manifest does not match staged graph base pack")
    if wiki_section != _single_wiki_manifest(wiki_dir):
        raise ValueError("wiki manifest does not match staged wiki base pack")
    return payload


def _single_graph_manifest(graph_dir: Path) -> dict[str, object]:
    try:
        entries = discover_pack_manifests(graph_dir)
    except GraphPackManifestError as exc:
        raise ValueError(f"staged graph packs are invalid: {exc}") from exc
    if len(entries) != 1 or entries[0].manifest.pack_type != "base":
        raise ValueError("staged graph packs must contain exactly one base pack")
    return entries[0].manifest.to_mapping()


def _single_wiki_manifest(wiki_dir: Path) -> dict[str, object]:
    try:
        entries = discover_wiki_pack_manifests(wiki_dir)
    except WikiPackManifestError as exc:
        raise ValueError(f"staged wiki packs are invalid: {exc}") from exc
    if len(entries) != 1 or entries[0].manifest.pack_type != "base":
        raise ValueError("staged wiki packs must contain exactly one base pack")
    return entries[0].manifest.to_mapping()


def _graph_entity_nodes(graph: nx.Graph) -> list[tuple[str, str, str]]:
    nodes: list[tuple[str, str, str]] = []
    for raw_node_id, attrs in graph.nodes(data=True):
        if not isinstance(raw_node_id, str):
            continue
        parsed = _node_parts(raw_node_id, attrs)
        if parsed is not None:
            nodes.append((raw_node_id, *parsed))
    return sorted(nodes)


def _node_parts(node_id: str, attrs: dict[str, Any]) -> tuple[str, str] | None:
    if ":" not in node_id:
        return None
    entity_type, slug = node_id.split(":", 1)
    if entity_type not in RECOMMENDABLE_ENTITY_TYPES or not slug:
        return None
    attr_type = attrs.get("type")
    if isinstance(attr_type, str) and attr_type in RECOMMENDABLE_ENTITY_TYPES:
        entity_type = attr_type
    return entity_type, slug


def _entity_page_candidates(entity_type: str, slug: str) -> set[str]:
    relpath = entity_relpath(entity_type, slug)
    candidates = {_normalise_relpath(relpath.as_posix())} if relpath is not None else set()
    if entity_type == "mcp-server":
        candidates.add(f"entities/mcp-servers/{slug}.md")
    return candidates


def _node_id_for_entity_page(relpath: str) -> str | None:
    parts = _pure_parts(relpath)
    if len(parts) < 3 or parts[0] != "entities":
        return None
    subject = parts[1]
    filename = parts[-1]
    if not filename.endswith(".md"):
        return None
    slug = filename[:-3]
    if subject == "skills" and len(parts) == 3:
        return f"skill:{slug}"
    if subject == "agents" and len(parts) == 3:
        return f"agent:{slug}"
    if subject == "harnesses" and len(parts) == 3:
        return f"harness:{slug}"
    if subject == "mcp-servers" and len(parts) in {3, 4}:
        return f"mcp-server:{slug}"
    return None


_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def _stale_entity_wikilinks(
    pages: dict[str, str],
    known_pages: set[str],
    known_node_ids: set[str],
) -> list[dict[str, str]]:
    stale: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for source_path, text in sorted(pages.items()):
        normalised_source = _normalise_relpath(source_path)
        for match in _WIKILINK_RE.finditer(text):
            target = _normalise_wikilink_target(match.group(1))
            node_id = _node_id_for_entity_page(target)
            if node_id is None:
                continue
            if target not in known_pages:
                reason = "missing page"
            elif node_id not in known_node_ids:
                reason = "missing graph node"
            else:
                continue
            key = (normalised_source, target, reason)
            if key in seen:
                continue
            seen.add(key)
            stale.append(
                {
                    "source_path": normalised_source,
                    "target": target,
                    "expected_node_id": node_id,
                    "reason": reason,
                }
            )
    return stale


def _normalise_wikilink_target(target: str) -> str:
    relpath = _normalise_relpath(target)
    return relpath if relpath.endswith(".md") else f"{relpath}.md"


def _normalise_relpath(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _pure_parts(path: str) -> tuple[str, ...]:
    """Return POSIX parts without touching the local filesystem."""
    return tuple(part for part in path.replace("\\", "/").split("/") if part)


def _require_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"pack compaction manifest {key} must be a non-empty string")
    return value


def _require_mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"pack compaction manifest {key} must be an object")
    return value


def _require_path(payload: dict[str, object], key: str, expected: Path) -> None:
    raw_value = _require_str(payload, key)
    if not _same_path(Path(raw_value), expected):
        raise ValueError(f"pack compaction manifest {key} does not match staged path")


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()
