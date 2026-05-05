#!/usr/bin/env python3
"""Validate shipped ctx graph/wiki artifacts."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import tarfile
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
    "openai-agents-sdk",
    "pydantic-ai",
    "semantic-kernel",
    "text-to-cad",
}
_NODE_ID_RE = re.compile(rb'"id"\s*:')
_EDGE_TARGET_RE = re.compile(rb'"target"\s*:')
_SOURCE_SKILLS_SH_RE = re.compile(rb'"source_catalog"\s*:\s*"skills\.sh"')
_HARNESS_TYPE_RE = re.compile(rb'"type"\s*:\s*"harness"')
_SEMANTIC_SIM_RE = re.compile(
    rb'"semantic_sim"\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)',
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class GraphArtifactError(RuntimeError):
    """Raised when a shipped graph artifact is inconsistent or unsafe."""


@dataclass(frozen=True)
class GraphArtifactStats:
    tar_members: int
    graph_nodes: int
    graph_edges: int
    graph_semantic_edges: int
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


def _require_real_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise GraphArtifactError(f"missing or empty graph artifact: {path}")
    with path.open("rb") as f:
        prefix = f.read(len(GIT_LFS_POINTER_PREFIX))
    if prefix == GIT_LFS_POINTER_PREFIX:
        raise GraphArtifactError(f"{path} is a Git LFS pointer, not hydrated content")


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


def _scan_graph_json(stream: IO[bytes]) -> tuple[int, int, int, int, int]:
    nodes = edges = semantic_edges = skills_sh_nodes = harness_nodes = 0
    tail = b""
    while chunk := stream.read(1024 * 1024):
        old_tail = tail
        data = tail + chunk
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
        tail = data[-512:]
    return nodes, edges, semantic_edges, skills_sh_nodes, harness_nodes


def _count_nonzero_semantic_matches(data: bytes) -> int:
    count = 0
    for match in _SEMANTIC_SIM_RE.finditer(data):
        try:
            if float(match.group(1)) != 0.0:
                count += 1
        except ValueError:
            continue
    return count


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
) -> GraphArtifactStats:
    graph_dir = Path(graph_dir)
    tarball = graph_dir / "wiki-graph.tar.gz"
    catalog_path = graph_dir / "skills-sh-catalog.json.gz"
    communities_path = graph_dir / "communities.json"
    for path in (tarball, catalog_path, communities_path):
        _require_real_file(path)

    catalog = _load_gzip_json(catalog_path)
    _load_json(communities_path)
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
    skill_pages = agent_pages = mcp_pages = harness_pages = skills_sh_converted = 0
    expected_harnesses = DEFAULT_HARNESSES if expected_harnesses is None else expected_harnesses

    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf:
            name = _safe_tar_name(member.name)
            names.add(name)
            if not (member.isfile() or member.isdir()):
                raise GraphArtifactError(f"archive member is not a regular file/dir: {member.name}")
            if name.endswith(".original"):
                raise GraphArtifactError(f"archive contains raw backup member: {member.name}")
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
            if member.isfile() and deep and name == "graphify-out/graph.json":
                f = tf.extractfile(member)
                if f is None:
                    raise GraphArtifactError("graphify-out/graph.json could not be read")
                (
                    graph_nodes,
                    graph_edges,
                    graph_semantic_edges,
                    skills_sh_nodes,
                    _harness_nodes,
                ) = _scan_graph_json(f)
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

    required_names = {
        "index.md",
        "graphify-out/graph.json",
        "graphify-out/communities.json",
        "external-catalogs/skills-sh/catalog.json",
    }
    missing_required = sorted(required_names - names)
    if missing_required:
        raise GraphArtifactError(f"wiki graph archive is missing: {missing_required}")
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

    return GraphArtifactStats(
        tar_members=len(names),
        graph_nodes=graph_nodes,
        graph_edges=graph_edges,
        graph_semantic_edges=graph_semantic_edges,
        skills_sh_nodes=skills_sh_nodes,
        skills_sh_catalog_entries=len(skills),
        skills_sh_converted=skills_sh_converted,
        skill_pages=skill_pages,
        agent_pages=agent_pages,
        mcp_pages=mcp_pages,
        harness_pages=harness_pages,
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
    args = parser.parse_args()
    stats = validate_graph_artifacts(
        args.graph_dir,
        deep=args.deep,
        min_nodes=args.min_nodes,
        min_edges=args.min_edges,
        min_skills_sh_nodes=args.min_skills_sh_nodes,
        min_semantic_edges=args.min_semantic_edges,
        line_threshold=args.line_threshold,
        max_stage_lines=args.max_stage_lines,
    )
    print(json.dumps(stats.__dict__, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
