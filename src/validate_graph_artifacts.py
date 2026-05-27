#!/usr/bin/env python3
"""Validate shipped ctx graph/wiki artifacts."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import tarfile
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

GIT_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
DEFAULT_HARNESSES = {
    "agentops",
    "autogen",
    "crewai",
    "google-adk",
    "haystack",
    "langfuse",
    "langgraph",
    "litellm",
    "mastra",
    "mirage",
    "openai-agents-sdk",
    "optillm",
    "pydantic-ai",
    "semantic-kernel",
    "text-to-cad",
}
_NODE_ID_RE = re.compile(rb'"id"\s*:')
_EDGE_TARGET_RE = re.compile(rb'"target"\s*:')
_SOURCE_SKILLS_SH_RE = re.compile(rb'"source_catalog"\s*:\s*"skills\.sh"')
_HARNESS_TYPE_RE = re.compile(rb'"type"\s*:\s*"harness"')
_GRAPH_KEY_RE = re.compile(rb'"graph"\s*:\s*\{')
_REPORT_EXPORT_ID_RE = re.compile(r"^>\s*Export ID:\s*(\S+)\s*$", re.MULTILINE)
_PREVIEW_EXPORT_ID_RE = re.compile(
    r'<meta\s+name=["\']ctx-graph-export-id["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_SKILL_BUNDLE_REF_RE = re.compile(
    r"""(?:^|[\s`'"\[(])(?:\./)?"""
    r"""((?:references|reference|resources|scripts|assets)/[^\s`'"\])<>]+)""",
    re.IGNORECASE,
)
_SKILL_BUNDLE_REF_MARKERS = (
    b"references/",
    b"reference/",
    b"resources/",
    b"scripts/",
    b"assets/",
)
_SEMANTIC_SIM_RE = re.compile(
    rb'"semantic_sim"\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)',
)
_EDGE_SCORE_VALUE_RE = re.compile(
    rb'"(weight|final_weight|similarity_score|semantic_sim|tag_sim|token_sim)"'
    rb'\s*:\s*("[^"]*"|-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)',
)
_EDGE_OBJECT_RE = re.compile(rb'\{[^{}]*"source"\s*:[^{}]*"target"\s*:[^{}]*\}')
_EDGE_SCORE_FIELDS = (
    "weight",
    "final_weight",
    "similarity_score",
    "semantic_sim",
    "tag_sim",
    "token_sim",
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_PREVIEW_HTML_FILES = (
    "sample-top60.html",
    "viz-ai-agents.html",
    "viz-overview.html",
    "viz-python.html",
    "viz-security.html",
)
_GRAPH_RUNTIME_REQUIRED_NAMES = {
    "index.md",
    "graphify-out/graph.json",
    "graphify-out/graph-delta.json",
    "graphify-out/communities.json",
    "graphify-out/graph-report.md",
    "graphify-out/graph-export-manifest.json",
    "graphify-out/dashboard-neighborhoods.sqlite3",
    "external-catalogs/skills-sh/catalog.json",
}
_DASHBOARD_INDEX_REQUIRED_TABLES = {"meta", "nodes", "slug_index", "neighbors"}
_DASHBOARD_INDEX_REQUIRED_META = {
    "export_id",
    "nodes_count",
    "edges_count",
    "max_degree",
    "top_k",
}
_DASHBOARD_INDEX_REQUIRED_COLUMNS = {
    "meta": {"key", "value"},
    "nodes": {
        "id",
        "label",
        "type",
        "tags",
        "description",
        "quality_score",
        "usage_score",
        "degree",
    },
    "slug_index": {"slug", "type", "node_id"},
    "neighbors": {"source", "payload"},
}


class GraphArtifactError(RuntimeError):
    """Raised when a shipped graph artifact is inconsistent or unsafe."""


@dataclass(frozen=True)
class GraphArtifactStats:
    tar_members: int
    graph_nodes: int
    graph_edges: int
    graph_semantic_edges: int
    harness_nodes: int
    skills_sh_nodes: int
    skills_sh_catalog_entries: int
    skills_sh_converted: int
    skill_pages: int
    agent_pages: int
    mcp_pages: int
    harness_pages: int


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_gzip_json(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise GraphArtifactError(f"{path} did not contain a JSON object")
    return data


def _validate_root_entity_overlay(path: Path) -> None:
    records = 0
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GraphArtifactError(
                f"graph/entity-overlays.jsonl line {lineno} is invalid JSON: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise GraphArtifactError(
                f"graph/entity-overlays.jsonl line {lineno} must be a JSON object",
            )
        nodes = payload.get("nodes", [])
        edges = payload.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise GraphArtifactError(
                f"graph/entity-overlays.jsonl line {lineno} must contain nodes/edges lists",
            )
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                raise GraphArtifactError(
                    f"graph/entity-overlays.jsonl line {lineno} node {index} "
                    "must contain id",
                )
        for index, edge in enumerate(edges, 1):
            if not isinstance(edge, dict):
                raise GraphArtifactError(
                    f"graph/entity-overlays.jsonl line {lineno} edge {index} "
                    "must be an object",
                )
            if not isinstance(edge.get("source"), str) or not isinstance(
                edge.get("target"), str
            ):
                raise GraphArtifactError(
                    f"graph/entity-overlays.jsonl line {lineno} edge {index} "
                    "must contain source/target",
                )
            numeric_scores: dict[str, float] = {}
            for field in _EDGE_SCORE_FIELDS:
                value = edge.get(field)
                if value is not None and (
                    not isinstance(value, int | float) or not 0 <= float(value) <= 1
                ):
                    raise GraphArtifactError(
                        f"graph/entity-overlays.jsonl line {lineno} edge {index} "
                        f"{field} must be 0..1",
                    )
                if value is not None:
                    numeric_scores[field] = float(value)
            if (
                "weight" in numeric_scores
                and "final_weight" in numeric_scores
                and abs(numeric_scores["weight"] - numeric_scores["final_weight"]) > 1e-9
            ):
                raise GraphArtifactError(
                    f"graph/entity-overlays.jsonl line {lineno} edge {index} "
                    "weight must equal final_weight",
                )
        records += 1
    if records == 0:
        raise GraphArtifactError("graph/entity-overlays.jsonl has no overlay records")


def _require_real_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise GraphArtifactError(f"missing or empty graph artifact: {path}")
    with path.open("rb") as f:
        prefix = f.read(len(GIT_LFS_POINTER_PREFIX))
    if prefix == GIT_LFS_POINTER_PREFIX:
        raise GraphArtifactError(f"{path} is a Git LFS pointer, not hydrated content")


def _copy_tar_member_to_path(
    tf: tarfile.TarFile,
    member: tarfile.TarInfo,
    target: Path,
) -> None:
    source = tf.extractfile(member)
    if source is None:
        raise GraphArtifactError(f"{member.name} could not be read")
    try:
        with target.open("wb") as out:
            while chunk := source.read(1024 * 1024):
                out.write(chunk)
    finally:
        source.close()


def _validate_dashboard_index(
    path: Path,
    *,
    expected_export_id: str,
    context: str,
) -> None:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise GraphArtifactError(f"{context} dashboard index is not valid SQLite: {exc}") from exc
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        if quick is None or str(quick[0]).lower() != "ok":
            raise GraphArtifactError(f"{context} dashboard index quick_check failed: {quick}")
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing_tables = sorted(_DASHBOARD_INDEX_REQUIRED_TABLES - tables)
        if missing_tables:
            raise GraphArtifactError(
                f"{context} dashboard index missing tables: {missing_tables}",
            )
        for table, expected_columns in _DASHBOARD_INDEX_REQUIRED_COLUMNS.items():
            columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
            missing_columns = sorted(expected_columns - columns)
            if missing_columns:
                raise GraphArtifactError(
                    f"{context} dashboard index table {table} missing columns: "
                    f"{missing_columns}",
                )
        meta_rows = conn.execute("SELECT key,value FROM meta").fetchall()
        meta = {str(row["key"]): json.loads(str(row["value"])) for row in meta_rows}
        missing_meta = sorted(_DASHBOARD_INDEX_REQUIRED_META - set(meta))
        if missing_meta:
            raise GraphArtifactError(
                f"{context} dashboard index missing meta keys: {missing_meta}",
            )
        if meta.get("export_id") != expected_export_id:
            raise GraphArtifactError(
                f"{context} dashboard index export_id mismatch: expected "
                f"{expected_export_id}, got {meta.get('export_id') or 'missing'}",
            )
        nodes_count = int(meta.get("nodes_count") or 0)
        if nodes_count != int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]):
            raise GraphArtifactError(f"{context} dashboard index nodes_count mismatch")
        if nodes_count > 0 and int(conn.execute("SELECT COUNT(*) FROM slug_index").fetchone()[0]) == 0:
            raise GraphArtifactError(f"{context} dashboard index slug_index is empty")
        payload = conn.execute("SELECT payload FROM neighbors LIMIT 1").fetchone()
        if payload is not None:
            decoded = json.loads(zlib.decompress(payload["payload"]).decode("utf-8"))
            if not isinstance(decoded, list):
                raise GraphArtifactError(
                    f"{context} dashboard index neighbor payload is not a list",
                )
    except (sqlite3.Error, json.JSONDecodeError, ValueError, TypeError, zlib.error) as exc:
        raise GraphArtifactError(f"{context} dashboard index validation failed: {exc}") from exc
    finally:
        conn.close()


def _safe_tar_name(raw_name: str) -> str:
    name = raw_name.replace("\\", "/")
    if (
        not name
        or name.startswith("/")
        or _WINDOWS_DRIVE_RE.match(name)
        or "\x00" in name
    ):
        raise GraphArtifactError(f"unsafe archive member path: {raw_name}")
    while name.startswith("./"):
        name = name[2:]
    parts = name.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise GraphArtifactError(f"unsafe archive member path: {raw_name}")
    return "/".join(parts)


def _count_lines(payload: bytes) -> int:
    return len(payload.decode("utf-8", errors="replace").splitlines())


def _is_converted_skill_page(name: str) -> bool:
    return name.startswith("converted/") and name.endswith("/SKILL.md")


def _iter_skill_bundle_refs(payload: bytes) -> list[str]:
    lower_payload = payload.lower()
    if not any(marker in lower_payload for marker in _SKILL_BUNDLE_REF_MARKERS):
        return []
    text = payload.decode("utf-8", errors="replace")
    refs: set[str] = set()
    for match in _SKILL_BUNDLE_REF_RE.finditer(text):
        ref = match.group(1).replace("\\", "/")
        ref = ref.split("#", 1)[0].split("?", 1)[0].rstrip(".,;:")
        parts = ref.split("/")
        if ref and all(part not in ("", ".", "..") for part in parts):
            refs.add(ref)
    return sorted(refs)


def _skill_bundle_target_name(skill_page: str, ref: str) -> str:
    return f"{skill_page.rsplit('/', 1)[0]}/{ref}"


def _scan_graph_json(stream: IO[bytes]) -> tuple[int, int, int, int, int, str | None]:
    nodes = edges = semantic_edges = skills_sh_nodes = harness_nodes = 0
    export_id: str | None = None
    tail = b""
    graph_probe = b""
    while chunk := stream.read(1024 * 1024):
        old_tail = tail
        data = tail + chunk
        _validate_graph_edge_score_fields(data)
        _validate_graph_edge_weight_drift(data)
        if export_id is None:
            graph_probe = (graph_probe + chunk)[-1024 * 1024:]
            export_id = _extract_graph_export_id(graph_probe)
        nodes += len(_NODE_ID_RE.findall(data)) - len(_NODE_ID_RE.findall(old_tail))
        edges += len(_EDGE_TARGET_RE.findall(data)) - len(_EDGE_TARGET_RE.findall(old_tail))
        semantic_edges += (
            _count_nonzero_semantic_matches(data)
            - _count_nonzero_semantic_matches(old_tail)
        )
        skills_sh_nodes += (
            len(_SOURCE_SKILLS_SH_RE.findall(data))
            - len(_SOURCE_SKILLS_SH_RE.findall(old_tail))
        )
        harness_nodes += (
            len(_HARNESS_TYPE_RE.findall(data))
            - len(_HARNESS_TYPE_RE.findall(old_tail))
        )
        tail = data[-65536:]
    return nodes, edges, semantic_edges, skills_sh_nodes, harness_nodes, export_id


def _scan_graph_export_id(stream: IO[bytes], *, max_bytes: int = 1024 * 1024) -> str | None:
    payload = stream.read(max_bytes)
    return _extract_graph_export_id(payload)


def _extract_graph_export_id(payload: bytes) -> str | None:
    match = _GRAPH_KEY_RE.search(payload)
    if match is None:
        return None
    start = match.end() - 1
    end = _json_object_end(payload, start)
    if end is None:
        return None
    try:
        graph_meta = json.loads(payload[start : end + 1].decode("utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(graph_meta, dict):
        return None
    raw = graph_meta.get("export_id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _json_object_end(payload: bytes, start: int) -> int | None:
    if start >= len(payload) or payload[start:start + 1] != b"{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(payload)):
        char = payload[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == 0x5C:  # backslash
                escaped = True
            elif char == 0x22:  # double quote
                in_string = False
            continue
        if char == 0x22:
            in_string = True
        elif char == 0x7B:  # {
            depth += 1
        elif char == 0x7D:  # }
            depth -= 1
            if depth == 0:
                return idx
    return None


def _count_nonzero_semantic_matches(data: bytes) -> int:
    count = 0
    for match in _SEMANTIC_SIM_RE.finditer(data):
        try:
            if float(match.group(1)) != 0.0:
                count += 1
        except ValueError:
            continue
    return count


def _validate_graph_edge_score_fields(data: bytes) -> None:
    for match in _EDGE_SCORE_VALUE_RE.finditer(data):
        field = match.group(1).decode("ascii")
        raw_value = match.group(2)
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise GraphArtifactError(f"graph.json edge {field} must be numeric") from exc
        if not 0 <= value <= 1:
            raise GraphArtifactError(f"graph.json edge {field} must be 0..1")


def _validate_graph_edge_weight_drift(data: bytes) -> None:
    for match in _EDGE_OBJECT_RE.finditer(data):
        edge = match.group(0)
        values: dict[str, float] = {}
        for field_match in _EDGE_SCORE_VALUE_RE.finditer(edge):
            field = field_match.group(1).decode("ascii")
            if field in {"weight", "final_weight"}:
                values[field] = float(field_match.group(2))
        if (
            "weight" in values
            and "final_weight" in values
            and abs(values["weight"] - values["final_weight"]) > 1e-9
        ):
            raise GraphArtifactError("graph.json edge weight must equal final_weight")


def _catalog_skills(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    raw = catalog.get("skills", [])
    return [item for item in raw if isinstance(item, dict)]


def validate_graph_artifacts(
    graph_dir: Path,
    *,
    deep: bool = False,
    min_nodes: int = 100_000,
    min_edges: int = 2_000_000,
    min_skills_sh_nodes: int = 89_000,
    min_semantic_edges: int = 1_000_000,
    expected_harnesses: set[str] | None = None,
    line_threshold: int = 180,
    max_stage_lines: int = 40,
    expected_nodes: int | None = None,
    expected_edges: int | None = None,
    expected_semantic_edges: int | None = None,
    expected_harness_nodes: int | None = None,
    expected_skills_sh_nodes: int | None = None,
    expected_skills_sh_catalog_entries: int | None = None,
    expected_skills_sh_converted: int | None = None,
    expected_skill_pages: int | None = None,
    expected_agent_pages: int | None = None,
    expected_mcp_pages: int | None = None,
    expected_harness_pages: int | None = None,
) -> GraphArtifactStats:
    graph_dir = Path(graph_dir)
    tarball = graph_dir / "wiki-graph.tar.gz"
    runtime_tarball = graph_dir / "wiki-graph-runtime.tar.gz"
    catalog_path = graph_dir / "skills-sh-catalog.json.gz"
    communities_path = graph_dir / "communities.json"
    overlay_path = graph_dir / "entity-overlays.jsonl"
    for path in (tarball, runtime_tarball, catalog_path, communities_path, overlay_path):
        _require_real_file(path)

    expected_harnesses = DEFAULT_HARNESSES if expected_harnesses is None else expected_harnesses
    runtime_export_id = _validate_runtime_graph_archive(
        runtime_tarball,
        expected_harnesses=expected_harnesses,
    )
    _validate_root_entity_overlay(overlay_path)
    catalog = _load_gzip_json(catalog_path)
    root_communities = _load_json(communities_path)
    if not isinstance(root_communities, dict):
        raise GraphArtifactError("graph/communities.json did not contain a JSON object")
    skills = _catalog_skills(catalog)
    body_unavailable = [
        str(item.get("ctx_slug") or item.get("id") or "")
        for item in skills
        if item.get("body_available") is False
    ]
    if body_unavailable:
        raise GraphArtifactError(
            "Skills.sh catalog contains body-unavailable records: "
            f"{body_unavailable[:5]}",
        )
    available_converted_paths = {
        str(item.get("converted_path") or "")
        for item in skills
        if item.get("body_available") and str(item.get("converted_path") or "")
    }
    required_skill_pages = {
        str(item.get("entity_path") or "")
        for item in skills
        if str(item.get("entity_path") or "")
    }

    names: set[str] = set()
    graph_nodes = graph_edges = graph_semantic_edges = skills_sh_nodes = 0
    harness_nodes = 0
    skill_pages = agent_pages = mcp_pages = harness_pages = skills_sh_converted = 0
    export_ids: dict[str, str] = {}
    manifest: dict[str, Any] | None = None
    archive_communities: dict[str, Any] | None = None
    dashboard_index_path: Path | None = None
    skill_bundle_refs: list[tuple[str, str, str]] = []

    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf:
            name = _safe_tar_name(member.name)
            names.add(name)
            if not (member.isfile() or member.isdir()):
                raise GraphArtifactError(f"archive member is not a regular file/dir: {member.name}")
            if name.endswith(".original"):
                raise GraphArtifactError(f"archive contains raw backup member: {member.name}")
            if name.endswith(".lock"):
                raise GraphArtifactError(f"archive contains lock member: {member.name}")
            if name == ".ctx" or name.startswith(".ctx/"):
                raise GraphArtifactError(
                    f"archive contains transient queue state: {member.name}",
                )
            if name.startswith("entities/skills/") and name.endswith(".md"):
                skill_pages += 1
            elif name.startswith("entities/agents/") and name.endswith(".md"):
                agent_pages += 1
            elif name.startswith("entities/mcp-servers/") and name.endswith(".md"):
                mcp_pages += 1
            elif name.startswith("entities/harnesses/") and name.endswith(".md"):
                harness_pages += 1
            if name.startswith("converted/skills-sh-") and name.endswith("/SKILL.md"):
                skills_sh_converted += 1
            if member.isfile() and _is_converted_skill_page(name):
                f = tf.extractfile(member)
                if f is None:
                    raise GraphArtifactError(f"{member.name} could not be read")
                payload = f.read()
                for ref in _iter_skill_bundle_refs(payload):
                    skill_bundle_refs.append((name, ref, _skill_bundle_target_name(name, ref)))
                if deep and name.startswith("converted/skills-sh-"):
                    lines = _count_lines(payload)
                    if lines > line_threshold:
                        raise GraphArtifactError(
                            f"{member.name} has {lines} lines, above limit {line_threshold}",
                        )
                continue
            if member.isfile() and name == "graphify-out/graph.json":
                f = tf.extractfile(member)
                if f is None:
                    raise GraphArtifactError("graphify-out/graph.json could not be read")
                if deep:
                    (
                        graph_nodes,
                        graph_edges,
                        graph_semantic_edges,
                        skills_sh_nodes,
                        harness_nodes,
                        graph_export_id,
                    ) = _scan_graph_json(f)
                else:
                    graph_export_id = _scan_graph_export_id(f)
                _record_export_id(export_ids, name, graph_export_id)
            elif member.isfile() and name == "graphify-out/graph-delta.json":
                data = _read_tar_json(tf, member, name)
                _record_export_id(export_ids, name, _export_id_from_json(data, name))
            elif member.isfile() and name == "graphify-out/communities.json":
                data = _read_tar_json(tf, member, name)
                if not isinstance(data, dict):
                    raise GraphArtifactError(f"{name} did not contain a JSON object")
                archive_communities = data
                _record_export_id(
                    export_ids,
                    name,
                    _export_id_from_json(data, name),
                )
            elif member.isfile() and name == "graphify-out/graph-export-manifest.json":
                data = _read_tar_json(tf, member, name)
                if not isinstance(data, dict):
                    raise GraphArtifactError(f"{name} did not contain a JSON object")
                manifest = data
            elif member.isfile() and name == "graphify-out/graph-report.md":
                f = tf.extractfile(member)
                if f is None:
                    raise GraphArtifactError(f"{member.name} could not be read")
                _record_export_id(export_ids, name, _export_id_from_report(f.read()))
            elif member.isfile() and name == "graphify-out/dashboard-neighborhoods.sqlite3":
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
                tmp.close()
                dashboard_index_path = Path(tmp.name)
                _copy_tar_member_to_path(tf, member, dashboard_index_path)
            elif member.isfile() and deep and name.startswith("converted/skills-sh-"):
                if name.endswith("/SKILL.md") or "/references/" in name:
                    f = tf.extractfile(member)
                    if f is None:
                        raise GraphArtifactError(f"{member.name} could not be read")
                    lines = _count_lines(f.read())
                    limit = line_threshold if name.endswith("/SKILL.md") else max_stage_lines
                    if lines > limit:
                        raise GraphArtifactError(
                            f"{member.name} has {lines} lines, above limit {limit}",
                        )

    missing_bundle_refs = [
        (skill_page, ref, target)
        for skill_page, ref, target in skill_bundle_refs
        if target not in names
    ]
    if missing_bundle_refs:
        sample = "; ".join(
            f"{skill_page} references {ref} but {target} is absent"
            for skill_page, ref, target in missing_bundle_refs[:5]
        )
        raise GraphArtifactError(f"missing bundled skill file: {sample}")

    required_names = _GRAPH_RUNTIME_REQUIRED_NAMES
    missing_required = sorted(required_names - names)
    if missing_required:
        raise GraphArtifactError(f"wiki graph archive is missing: {missing_required}")
    manifest_export_id = _validate_graph_export_manifest(manifest, names)
    if runtime_export_id != manifest_export_id:
        raise GraphArtifactError(
            "runtime graph archive export_id mismatch: expected "
            f"{manifest_export_id}, got {runtime_export_id}",
        )
    _record_export_id(
        export_ids,
        "graphify-out/graph-export-manifest.json",
        manifest_export_id,
    )
    if dashboard_index_path is None:
        raise GraphArtifactError("graphify-out/dashboard-neighborhoods.sqlite3 is missing")
    try:
        _validate_dashboard_index(
            dashboard_index_path,
            expected_export_id=manifest_export_id,
            context="full archive",
        )
    finally:
        dashboard_index_path.unlink(missing_ok=True)
    if "graphify-out/graph.json" not in export_ids:
        raise GraphArtifactError("graphify-out/graph.json is missing export_id")
    _validate_export_ids(export_ids, expected=manifest_export_id)
    _validate_root_communities(root_communities, archive_communities)
    _validate_graph_previews(graph_dir, export_id=manifest_export_id, manifest=manifest)
    missing_pages = sorted(required_skill_pages - names)
    if missing_pages:
        raise GraphArtifactError(f"missing Skills.sh entity pages: {missing_pages[:5]}")
    missing_converted = sorted(available_converted_paths - names)
    if missing_converted:
        raise GraphArtifactError(f"missing converted Skills.sh body: {missing_converted[0]}")
    missing_harnesses = sorted(
        f"entities/harnesses/{slug}.md"
        for slug in expected_harnesses
        if f"entities/harnesses/{slug}.md" not in names
    )
    if missing_harnesses:
        raise GraphArtifactError(f"missing harness entity pages: {missing_harnesses}")

    if deep:
        if graph_nodes < min_nodes:
            raise GraphArtifactError(f"graph node count {graph_nodes} below floor {min_nodes}")
        if graph_edges < min_edges:
            raise GraphArtifactError(f"graph edge count {graph_edges} below floor {min_edges}")
        if skills_sh_nodes < min_skills_sh_nodes:
            raise GraphArtifactError(
                f"Skills.sh node count {skills_sh_nodes} below floor {min_skills_sh_nodes}",
            )
        if graph_semantic_edges < min_semantic_edges:
            raise GraphArtifactError(
                f"semantic edge count {graph_semantic_edges} below floor {min_semantic_edges}",
            )

    stats = GraphArtifactStats(
        tar_members=len(names),
        graph_nodes=graph_nodes,
        graph_edges=graph_edges,
        graph_semantic_edges=graph_semantic_edges,
        harness_nodes=harness_nodes,
        skills_sh_nodes=skills_sh_nodes,
        skills_sh_catalog_entries=len(skills),
        skills_sh_converted=skills_sh_converted,
        skill_pages=skill_pages,
        agent_pages=agent_pages,
        mcp_pages=mcp_pages,
        harness_pages=harness_pages,
    )
    if not deep and any(
        value is not None
        for value in (
            expected_nodes,
            expected_edges,
            expected_semantic_edges,
            expected_harness_nodes,
            expected_skills_sh_nodes,
        )
    ):
        raise GraphArtifactError("deep=True is required for exact graph node/edge counts")
    expected_counts = {
        "graph_nodes": expected_nodes,
        "graph_edges": expected_edges,
        "graph_semantic_edges": expected_semantic_edges,
        "harness_nodes": expected_harness_nodes,
        "skills_sh_nodes": expected_skills_sh_nodes,
        "skills_sh_catalog_entries": expected_skills_sh_catalog_entries,
        "skills_sh_converted": expected_skills_sh_converted,
        "skill_pages": expected_skill_pages,
        "agent_pages": expected_agent_pages,
        "mcp_pages": expected_mcp_pages,
        "harness_pages": expected_harness_pages,
    }
    for field_name, expected in expected_counts.items():
        if expected is None:
            continue
        actual = getattr(stats, field_name)
        if actual != expected:
            raise GraphArtifactError(
                f"{field_name} exact count mismatch: expected {expected}, got {actual}",
            )
    return stats


def _validate_runtime_graph_archive(
    tarball: Path,
    *,
    expected_harnesses: set[str],
    expected_export_id: str | None = None,
) -> str:
    names: set[str] = set()
    export_ids: dict[str, str] = {}
    manifest: dict[str, Any] | None = None
    dashboard_index_path: Path | None = None
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf:
            name = _safe_tar_name(member.name)
            names.add(name)
            if not (member.isfile() or member.isdir()):
                raise GraphArtifactError(
                    f"runtime archive member is not a regular file/dir: {member.name}",
                )
            if name.endswith(".original"):
                raise GraphArtifactError(
                    f"runtime archive contains raw backup member: {member.name}",
                )
            if name.endswith(".lock"):
                raise GraphArtifactError(
                    f"runtime archive contains lock member: {member.name}",
                )
            if name == ".ctx" or name.startswith(".ctx/"):
                raise GraphArtifactError(
                    f"runtime archive contains transient queue state: {member.name}",
                )
            if member.isfile() and name == "graphify-out/graph.json":
                f = tf.extractfile(member)
                if f is None:
                    raise GraphArtifactError("runtime graph.json could not be read")
                _record_export_id(export_ids, name, _scan_graph_export_id(f))
            elif member.isfile() and name == "graphify-out/graph-delta.json":
                data = _read_tar_json(tf, member, name)
                _record_export_id(export_ids, name, _export_id_from_json(data, name))
            elif member.isfile() and name == "graphify-out/communities.json":
                data = _read_tar_json(tf, member, name)
                _record_export_id(export_ids, name, _export_id_from_json(data, name))
            elif member.isfile() and name == "graphify-out/graph-export-manifest.json":
                data = _read_tar_json(tf, member, name)
                if not isinstance(data, dict):
                    raise GraphArtifactError(f"{name} did not contain a JSON object")
                manifest = data
            elif member.isfile() and name == "graphify-out/graph-report.md":
                f = tf.extractfile(member)
                if f is None:
                    raise GraphArtifactError(f"{member.name} could not be read")
                _record_export_id(export_ids, name, _export_id_from_report(f.read()))
            elif member.isfile() and name == "graphify-out/dashboard-neighborhoods.sqlite3":
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
                tmp.close()
                dashboard_index_path = Path(tmp.name)
                _copy_tar_member_to_path(tf, member, dashboard_index_path)

    missing_required = sorted(_GRAPH_RUNTIME_REQUIRED_NAMES - names)
    if missing_required:
        raise GraphArtifactError(
            f"runtime graph archive is missing: {missing_required}",
        )
    missing_harnesses = sorted(
        f"entities/harnesses/{slug}.md"
        for slug in expected_harnesses
        if f"entities/harnesses/{slug}.md" not in names
    )
    if missing_harnesses:
        raise GraphArtifactError(
            f"runtime graph archive is missing harness pages: {missing_harnesses}",
        )
    manifest_export_id = _validate_graph_export_manifest(manifest, names)
    _record_export_id(
        export_ids,
        "graphify-out/graph-export-manifest.json",
        manifest_export_id,
    )
    if dashboard_index_path is None:
        raise GraphArtifactError(
            "runtime graph archive is missing dashboard-neighborhoods.sqlite3",
        )
    try:
        _validate_dashboard_index(
            dashboard_index_path,
            expected_export_id=manifest_export_id,
            context="runtime archive",
        )
    finally:
        dashboard_index_path.unlink(missing_ok=True)
    if "graphify-out/graph.json" not in export_ids:
        raise GraphArtifactError("runtime graph.json is missing export_id")
    _validate_export_ids(export_ids, expected=manifest_export_id)
    if expected_export_id is not None and manifest_export_id != expected_export_id:
        raise GraphArtifactError(
            "runtime graph archive export_id mismatch: expected "
            f"{expected_export_id}, got {manifest_export_id}",
        )
    return manifest_export_id


def _validate_root_communities(
    root_communities: dict[str, Any],
    archive_communities: dict[str, Any] | None,
) -> None:
    if archive_communities is None:
        raise GraphArtifactError("graphify-out/communities.json is missing")
    if root_communities != archive_communities:
        root_export = root_communities.get("export_id") or "missing"
        archive_export = archive_communities.get("export_id") or "missing"
        raise GraphArtifactError(
            "stale graph/communities.json: root artifact must match "
            "wiki-graph.tar.gz graphify-out/communities.json "
            f"(root export_id={root_export}, archive export_id={archive_export})",
        )


def _validate_graph_previews(
    graph_dir: Path,
    *,
    export_id: str,
    manifest: dict[str, Any] | None,
) -> None:
    counts = manifest.get("counts") if isinstance(manifest, dict) else None
    source_nodes = counts.get("nodes") if isinstance(counts, dict) else None
    source_edges = counts.get("edges") if isinstance(counts, dict) else None
    for filename in _PREVIEW_HTML_FILES:
        path = graph_dir / filename
        if not path.is_file() or path.stat().st_size == 0:
            raise GraphArtifactError(f"missing graph preview: {filename}")
        text = path.read_text(encoding="utf-8", errors="replace")
        match = _PREVIEW_EXPORT_ID_RE.search(text)
        actual_export = match.group(1).strip() if match else ""
        if actual_export != export_id:
            raise GraphArtifactError(
                f"stale graph preview {filename}: expected export_id {export_id}, "
                f"got {actual_export or 'missing'}",
            )
        if isinstance(source_nodes, int) and not re.search(
            rf'"source_graph_nodes"\s*:\s*{source_nodes}\b',
            text,
        ):
            raise GraphArtifactError(
                f"stale graph preview {filename}: missing source_graph_nodes {source_nodes}",
            )
        if isinstance(source_edges, int) and not re.search(
            rf'"source_graph_edges"\s*:\s*{source_edges}\b',
            text,
        ):
            raise GraphArtifactError(
                f"stale graph preview {filename}: missing source_graph_edges {source_edges}",
            )


def _read_tar_json(tf: tarfile.TarFile, member: tarfile.TarInfo, name: str) -> Any:
    f = tf.extractfile(member)
    if f is None:
        raise GraphArtifactError(f"{member.name} could not be read")
    try:
        return json.loads(f.read().decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GraphArtifactError(f"{name} is not valid JSON: {exc}") from exc


def _export_id_from_json(data: Any, name: str) -> str:
    if not isinstance(data, dict):
        raise GraphArtifactError(f"{name} did not contain a JSON object")
    raw = data.get("export_id")
    if not isinstance(raw, str) or not raw.strip():
        raise GraphArtifactError(f"{name} is missing export_id")
    return raw.strip()


def _export_id_from_report(payload: bytes) -> str:
    text = payload.decode("utf-8", errors="replace")
    match = _REPORT_EXPORT_ID_RE.search(text)
    if match is None:
        raise GraphArtifactError("graphify-out/graph-report.md is missing Export ID")
    return match.group(1).strip()


def _record_export_id(export_ids: dict[str, str], key: str, export_id: str | None) -> None:
    if not export_id:
        raise GraphArtifactError(f"{key} is missing export_id")
    export_ids[key] = export_id


def _validate_graph_export_manifest(
    manifest: dict[str, Any] | None,
    names: set[str],
) -> str:
    if manifest is None:
        raise GraphArtifactError("graphify-out/graph-export-manifest.json is missing")
    if manifest.get("version") != 1:
        raise GraphArtifactError("graph export manifest version must be 1")
    export_id = _export_id_from_json(manifest, "graphify-out/graph-export-manifest.json")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise GraphArtifactError("graph export manifest missing artifacts map")
    expected = {
        "graph": "graph.json",
        "delta": "graph-delta.json",
        "communities": "communities.json",
        "report": "graph-report.md",
    }
    if set(artifacts) != set(expected):
        raise GraphArtifactError(
            "graph export manifest artifacts map must contain exactly "
            f"{sorted(expected)}",
        )
    for key, filename in expected.items():
        actual = artifacts.get(key)
        if actual != filename:
            raise GraphArtifactError(
                f"graph export manifest artifact {key!r} expected {filename!r}, got {actual!r}",
            )
        archive_name = f"graphify-out/{filename}"
        if archive_name not in names:
            raise GraphArtifactError(f"graph export manifest references missing {archive_name}")
    return export_id


def _validate_export_ids(export_ids: dict[str, str], *, expected: str) -> None:
    mismatches = {
        key: value
        for key, value in sorted(export_ids.items())
        if value != expected
    }
    if mismatches:
        raise GraphArtifactError(
            f"graph export_id mismatch: expected {expected}, mismatches={mismatches}",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-dir", type=Path, default=Path("graph"))
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--min-nodes", type=int, default=100_000)
    parser.add_argument("--min-edges", type=int, default=2_000_000)
    parser.add_argument("--min-skills-sh-nodes", type=int, default=89_000)
    parser.add_argument("--min-semantic-edges", type=int, default=1_000_000)
    parser.add_argument("--line-threshold", type=int, default=180)
    parser.add_argument("--max-stage-lines", type=int, default=40)
    parser.add_argument("--expected-nodes", type=int)
    parser.add_argument("--expected-edges", type=int)
    parser.add_argument("--expected-semantic-edges", type=int)
    parser.add_argument("--expected-harness-nodes", type=int)
    parser.add_argument("--expected-skills-sh-nodes", type=int)
    parser.add_argument("--expected-skills-sh-catalog-entries", type=int)
    parser.add_argument("--expected-skills-sh-converted", type=int)
    parser.add_argument("--expected-skill-pages", type=int)
    parser.add_argument("--expected-agent-pages", type=int)
    parser.add_argument("--expected-mcp-pages", type=int)
    parser.add_argument("--expected-harness-pages", type=int)
    args = parser.parse_args()
    deep_expected = (
        args.expected_nodes,
        args.expected_edges,
        args.expected_semantic_edges,
        args.expected_harness_nodes,
        args.expected_skills_sh_nodes,
    )
    if not args.deep and any(value is not None for value in deep_expected):
        parser.error("--deep is required for exact graph node/edge count checks")
    stats = validate_graph_artifacts(
        args.graph_dir,
        deep=args.deep,
        min_nodes=args.min_nodes,
        min_edges=args.min_edges,
        min_skills_sh_nodes=args.min_skills_sh_nodes,
        min_semantic_edges=args.min_semantic_edges,
        line_threshold=args.line_threshold,
        max_stage_lines=args.max_stage_lines,
        expected_nodes=args.expected_nodes,
        expected_edges=args.expected_edges,
        expected_semantic_edges=args.expected_semantic_edges,
        expected_harness_nodes=args.expected_harness_nodes,
        expected_skills_sh_nodes=args.expected_skills_sh_nodes,
        expected_skills_sh_catalog_entries=args.expected_skills_sh_catalog_entries,
        expected_skills_sh_converted=args.expected_skills_sh_converted,
        expected_skill_pages=args.expected_skill_pages,
        expected_agent_pages=args.expected_agent_pages,
        expected_mcp_pages=args.expected_mcp_pages,
        expected_harness_pages=args.expected_harness_pages,
    )
    print(json.dumps(stats.__dict__, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
