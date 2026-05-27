"""Overlay explicit local wiki entities onto shipped graph tarballs.

This is a narrow release-maintenance tool.  It starts from the current shipped
tarball, copies every existing member except graph export files and explicit
page replacements, then appends selected nodes/pages from a local wiki graph.
It deliberately does not rebuild Skills.sh semantic topology.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ctx.core.wiki.artifact_promotion import promote_staged_artifact  # noqa: E402
from ctx.utils._fs_utils import atomic_write_text, reject_symlink_path  # noqa: E402
from scripts.build_dashboard_graph_index import build_dashboard_index  # noqa: E402

GRAPH_EXPORT_NAMES = {
    "graphify-out/graph.json",
    "graphify-out/graph-delta.json",
    "graphify-out/communities.json",
    "graphify-out/graph-report.md",
    "graphify-out/graph-export-manifest.json",
}
OVERLAY_GZIP_COMPRESSLEVEL = 3


@dataclass(frozen=True)
class OverlayStats:
    node_count: int
    edge_count: int
    added_nodes: int
    added_edges: int
    export_id: str


def overlay_entities(
    *,
    source_wiki: Path,
    tarball: Path,
    entity_ids: list[str],
    skills_root: Path | None = None,
    root_communities: Path | None = None,
    runtime: bool = False,
    now: datetime | None = None,
) -> OverlayStats:
    if not entity_ids:
        raise ValueError("at least one entity id is required")
    source_graph = _read_json(source_wiki / "graphify-out" / "graph.json")
    source_nodes = _nodes_by_id(source_graph)
    selected = list(dict.fromkeys(entity_ids))
    missing = [node_id for node_id in selected if node_id not in source_nodes]
    if missing:
        raise ValueError(f"source graph is missing selected nodes: {missing}")

    graph, communities = _read_tar_graph_artifacts(tarball)
    graph, added_nodes, added_edges = _merge_graph(graph, source_graph, selected)
    timestamp = _timestamp(now)
    export_id = f"ctx-graph-overlay-{timestamp}-{len(graph['nodes'])}-{len(graph['edges'])}"
    graph.setdefault("graph", {})["export_id"] = export_id
    graph["graph"]["generated"] = timestamp
    graph["graph"]["overlay_entities"] = selected
    communities = _merge_communities(
        communities,
        graph=graph,
        selected=selected,
        export_id=export_id,
        generated=timestamp,
    )
    if root_communities is not None:
        atomic_write_text(root_communities, json.dumps(communities, indent=2) + "\n")

    replacements = _collect_replacements(
        source_wiki=source_wiki,
        entity_ids=selected,
        skills_root=skills_root,
        runtime=runtime,
    )
    replacements.update({
        "graphify-out/graph.json": _json_bytes(graph, compact=True),
        "graphify-out/dashboard-neighborhoods.sqlite3": _dashboard_index_bytes(graph),
        "graphify-out/graph-delta.json": _json_bytes(
            _render_delta(graph, selected, export_id=export_id, generated=timestamp),
            compact=False,
        ),
        "graphify-out/communities.json": _json_bytes(communities, compact=False),
        "graphify-out/graph-report.md": _render_report(
            graph,
            communities,
            selected=selected,
            export_id=export_id,
            generated=timestamp,
        ).encode("utf-8"),
        "graphify-out/graph-export-manifest.json": _json_bytes(
            _render_manifest(graph, communities, export_id=export_id, generated=timestamp),
            compact=False,
        ),
    })
    _rewrite_tarball(tarball, replacements)
    return OverlayStats(
        node_count=len(graph["nodes"]),
        edge_count=len(graph["edges"]),
        added_nodes=added_nodes,
        added_edges=added_edges,
        export_id=export_id,
    )


def _merge_graph(
    graph: dict[str, Any],
    source_graph: dict[str, Any],
    selected: list[str],
) -> tuple[dict[str, Any], int, int]:
    nodes = _list_field(graph, "nodes")
    edges = _list_field(graph, "edges")
    source_nodes = _nodes_by_id(source_graph)
    dest_ids = {str(node.get("id")) for node in nodes if isinstance(node, dict)}
    by_id = {
        str(node.get("id")): index
        for index, node in enumerate(nodes)
        if isinstance(node, dict) and node.get("id")
    }
    added_nodes = 0
    for node_id in selected:
        node = dict(source_nodes[node_id])
        if node_id in by_id:
            nodes[by_id[node_id]] = node
        else:
            nodes.append(node)
            dest_ids.add(node_id)
            added_nodes += 1

    edge_keys = {_edge_key(edge) for edge in edges if isinstance(edge, dict)}
    added_edges = 0
    for edge in _list_field(source_graph, "edges"):
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in selected and target not in selected:
            continue
        if source not in dest_ids or target not in dest_ids:
            continue
        key = _edge_key(edge)
        if key in edge_keys:
            continue
        edges.append(dict(edge))
        edge_keys.add(key)
        added_edges += 1
    graph["nodes"] = nodes
    graph["edges"] = edges
    return graph, added_nodes, added_edges


def _merge_communities(
    communities: dict[str, Any],
    *,
    graph: dict[str, Any],
    selected: list[str],
    export_id: str,
    generated: str,
) -> dict[str, Any]:
    raw = communities.get("communities")
    if not isinstance(raw, dict) or not raw:
        raw = {"0": {"label": "Overlay", "members": []}}
    node_to_community: dict[str, str] = {}
    for cid, payload in raw.items():
        members = payload.get("members") if isinstance(payload, dict) else []
        if not isinstance(members, list):
            continue
        for member in members:
            node_to_community[str(member)] = str(cid)
    edge_pairs = [
        (str(edge.get("source") or ""), str(edge.get("target") or ""))
        for edge in _list_field(graph, "edges")
        if isinstance(edge, dict)
    ]
    for node_id in selected:
        if node_id in node_to_community:
            continue
        counts: Counter[str] = Counter()
        for source, target in edge_pairs:
            other = target if source == node_id else source if target == node_id else ""
            if other and other in node_to_community:
                counts[node_to_community[other]] += 1
        cid = counts.most_common(1)[0][0] if counts else sorted(raw)[0]
        payload = raw.setdefault(cid, {"label": "Overlay", "members": []})
        members = payload.setdefault("members", [])
        if isinstance(members, list):
            members.append(node_id)
            node_to_community[node_id] = cid
    communities["export_id"] = export_id
    communities["generated"] = generated
    communities["total_communities"] = len(raw)
    communities["communities"] = raw
    return communities


def _collect_replacements(
    *,
    source_wiki: Path,
    entity_ids: list[str],
    skills_root: Path | None,
    runtime: bool,
) -> dict[str, bytes]:
    replacements: dict[str, bytes] = {}
    for node_id in entity_ids:
        entity_type, slug = _split_node_id(node_id)
        page = _entity_page(source_wiki, entity_type, slug)
        if page is not None and (not runtime or entity_type == "harness"):
            replacements[page.relative_to(source_wiki).as_posix()] = _read_safe_bytes(page)
        if not runtime and entity_type == "skill":
            replacements.update(_skill_replacements(source_wiki, slug, skills_root=skills_root))
    return replacements


def _dashboard_index_bytes(graph: dict[str, Any]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="ctx-overlay-index-") as tmp:
        tmp_path = Path(tmp)
        graph_path = tmp_path / "graph.json"
        index_path = tmp_path / "dashboard-neighborhoods.sqlite3"
        graph_path.write_bytes(_json_bytes(graph, compact=True))
        build_dashboard_index(graph_path, index_path)
        return index_path.read_bytes()


def _entity_page(source_wiki: Path, entity_type: str, slug: str) -> Path | None:
    candidates = {
        "skill": [source_wiki / "entities" / "skills" / f"{slug}.md"],
        "agent": [source_wiki / "entities" / "agents" / f"{slug}.md"],
        "harness": [source_wiki / "entities" / "harnesses" / f"{slug}.md"],
        "mcp-server": [
            source_wiki / "entities" / "mcp-servers" / slug[:1].lower() / f"{slug}.md",
        ],
    }.get(entity_type, [])
    for candidate in candidates:
        if _is_safe_file(candidate):
            return candidate
    if entity_type == "mcp-server":
        matches = list((source_wiki / "entities" / "mcp-servers").rglob(f"{slug}.md"))
        for match in matches:
            if _is_safe_file(match):
                return match
    return None


def _skill_replacements(source_wiki: Path, slug: str, *, skills_root: Path | None) -> dict[str, bytes]:
    root = skills_root or Path.home() / ".claude" / "skills"
    candidates = [
        source_wiki / "converted" / slug / "SKILL.md",
        root / slug / "SKILL.md",
    ]
    for candidate in candidates:
        if _is_safe_file(candidate):
            skill_dir = candidate.parent
            return {
                f"converted/{slug}/{path.relative_to(skill_dir).as_posix()}": _read_safe_bytes(path)
                for path in sorted(skill_dir.rglob("*"))
                if _is_safe_file(path) and not path.name.endswith((".original", ".lock"))
            }
    return {}


def _is_safe_file(path: Path) -> bool:
    reject_symlink_path(path)
    return path.is_file()


def _read_safe_bytes(path: Path) -> bytes:
    reject_symlink_path(path)
    return path.read_bytes()


def _rewrite_tarball(tarball: Path, replacements: dict[str, bytes]) -> None:
    reject_symlink_path(tarball)
    staged = tarball.with_name(f"{tarball.name}.staged")
    reject_symlink_path(staged)
    skip_names = set(replacements)
    with tarfile.open(tarball, "r:gz") as src, tarfile.open(
        staged,
        "w:gz",
        compresslevel=OVERLAY_GZIP_COMPRESSLEVEL,
    ) as dst:
        for member in src:
            safe_name = _safe_tar_name(member.name)
            if safe_name is None:
                continue
            if safe_name in GRAPH_EXPORT_NAMES or safe_name in skip_names:
                continue
            if safe_name.endswith(".original") or safe_name.endswith(".lock"):
                continue
            if member.isfile():
                f = src.extractfile(member)
                if f is not None:
                    dst.addfile(member, f)
            elif member.isdir():
                dst.addfile(member)
        for name, payload in sorted(replacements.items()):
            _add_bytes(dst, name=f"./{name}", payload=payload)
    promote_staged_artifact(staged, tarball, validate=_validate_tarball)


def _validate_tarball(candidate: Path) -> None:
    seen: set[str] = set()
    with tarfile.open(candidate, "r:gz") as tf:
        for member in tf:
            safe_name = _safe_tar_name(member.name)
            if safe_name is None:
                raise ValueError(f"unsafe tar member: {member.name}")
            if safe_name.endswith(".original") or safe_name.endswith(".lock"):
                raise ValueError(f"transient member leaked: {safe_name}")
            seen.add(safe_name)
    missing = sorted(GRAPH_EXPORT_NAMES - seen)
    if missing:
        raise ValueError(f"candidate tarball missing graph exports: {missing}")


def _read_tar_graph_artifacts(tarball: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    graph: dict[str, Any] | None = None
    communities: dict[str, Any] | None = None
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf:
            safe_name = _safe_tar_name(member.name)
            if safe_name not in {"graphify-out/graph.json", "graphify-out/communities.json"}:
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            data = json.loads(f.read().decode("utf-8"))
            if safe_name.endswith("graph.json"):
                graph = data
            else:
                communities = data
    if graph is None or communities is None:
        raise ValueError("tarball is missing graph.json or communities.json")
    return graph, communities


def _render_delta(
    graph: dict[str, Any],
    selected: list[str],
    *,
    export_id: str,
    generated: str,
) -> dict[str, Any]:
    selected_set = set(selected)
    nodes = [
        node for node in _list_field(graph, "nodes")
        if isinstance(node, dict) and node.get("id") in selected_set
    ]
    edges = [
        edge for edge in _list_field(graph, "edges")
        if isinstance(edge, dict)
        and (edge.get("source") in selected_set or edge.get("target") in selected_set)
    ]
    return {
        "version": 1,
        "full_rebuild": False,
        "export_id": export_id,
        "generated": generated,
        "node_count": len(graph["nodes"]),
        "edge_count": len(graph["edges"]),
        "delta_node_count": len(nodes),
        "delta_edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def _render_manifest(
    graph: dict[str, Any],
    communities: dict[str, Any],
    *,
    export_id: str,
    generated: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "export_id": export_id,
        "generated": generated,
        "artifacts": {
            "graph": "graph.json",
            "delta": "graph-delta.json",
            "communities": "communities.json",
            "report": "graph-report.md",
        },
        "counts": {
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "communities": communities.get("total_communities", 0),
        },
    }


def _render_report(
    graph: dict[str, Any],
    communities: dict[str, Any],
    *,
    selected: list[str],
    export_id: str,
    generated: str,
) -> str:
    degree: Counter[str] = Counter()
    for edge in _list_field(graph, "edges"):
        if not isinstance(edge, dict):
            continue
        degree[str(edge.get("source") or "")] += 1
        degree[str(edge.get("target") or "")] += 1
    node_by_id = _nodes_by_id(graph)
    lines = [
        "# Graph Report",
        "",
        f"> Generated: {generated}",
        f"> Export ID: {export_id}",
        (
            f"> Nodes: {len(graph['nodes'])} | Edges: {len(graph['edges'])} | "
            f"Communities: {communities.get('total_communities', 0)}"
        ),
        "",
        "## Overlay Entities",
        "",
    ]
    for node_id in selected:
        node = node_by_id.get(node_id, {})
        lines.append(f"- **{node.get('label', node_id)}** ({node_id})")
    lines.extend(["", "## Most Connected Nodes", ""])
    for node_id, count in degree.most_common(20):
        if not node_id:
            continue
        node = node_by_id.get(node_id, {})
        lines.append(f"- **{node.get('label', node_id)}** ({count} connections)")
    return "\n".join(lines) + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _nodes_by_id(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(node.get("id")): node
        for node in _list_field(graph, "nodes")
        if isinstance(node, dict) and node.get("id")
    }


def _list_field(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ValueError(f"graph field {key!r} must be a list")
    return value


def _edge_key(edge: dict[str, Any]) -> tuple[str, str]:
    source = str(edge.get("source") or "")
    target = str(edge.get("target") or "")
    return (source, target) if source <= target else (target, source)


def _split_node_id(node_id: str) -> tuple[str, str]:
    if ":" not in node_id:
        raise ValueError(f"invalid node id: {node_id}")
    entity_type, slug = node_id.split(":", 1)
    return entity_type, slug


def _safe_tar_name(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/") or normalized.startswith("../"):
        return None
    if "/../" in normalized or normalized == "..":
        return None
    return normalized


def _add_bytes(
    tf: tarfile.TarFile,
    *,
    name: str,
    payload: bytes,
    mode: int = 0o644,
) -> None:
    import io

    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.mtime = 0
    tf.addfile(info, io.BytesIO(payload))


def _json_bytes(data: dict[str, Any], *, compact: bool) -> bytes:
    if compact:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _timestamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-wiki", type=Path, default=Path.home() / ".claude" / "skill-wiki")
    parser.add_argument("--tarball", type=Path, required=True)
    parser.add_argument("--entity", action="append", required=True, help="Node id, e.g. skill:foo")
    parser.add_argument("--root-communities", type=Path)
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args()
    stats = overlay_entities(
        source_wiki=args.source_wiki,
        tarball=args.tarball,
        entity_ids=args.entity,
        skills_root=Path.home() / ".claude" / "skills",
        root_communities=args.root_communities,
        runtime=args.runtime,
    )
    print(json.dumps(stats.__dict__, indent=2))


if __name__ == "__main__":
    main()
