#!/usr/bin/env python3
"""
update_repo_stats.py -- Patch README/docs numbers from authoritative sources.

Run by the pre-commit hook so README/docs badges and inline counts never drift
from reality. Reads only committed files and a live pytest collection.

Sources of truth:
  - scripts/ci_preflight.py GRAPH_VALIDATE_ARGS -> exact release counts
  - graph/wiki-graph.tar.gz              -> graph/report/entity counts
  - graph/wiki-graph-runtime.tar.gz      -> runtime graph/report counts
  - graph/communities.json               -> current community export
  - graph/skills-sh-catalog.json.gz      -> hydrated skill body counts
  - pytest --collect-only -q             -> collected test count

Usage:
  python src/update_repo_stats.py          # patch README/docs in place
  python src/update_repo_stats.py --check  # exit 1 if README/docs are stale
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
DOCS_INDEX = REPO_ROOT / "docs" / "index.md"
DOCS_KNOWLEDGE_GRAPH = REPO_ROOT / "docs" / "knowledge-graph.md"
DOCS_CATALOG = REPO_ROOT / "docs" / "catalog.md"
DOCS_SKILL_ROUTER = REPO_ROOT / "docs" / "skill-router" / "index.md"
_MAX_TAR_JSON_BYTES = 512 * 1024 * 1024
_MAX_TAR_TEXT_BYTES = 2 * 1024 * 1024
_GRAPH_JSON_MEMBER = "graphify-out/graph.json"
_COMMUNITIES_JSON_MEMBER = "graphify-out/communities.json"
_GRAPH_REPORT_MEMBER = "graphify-out/graph-report.md"
_PYTEST_COLLECT_TIMEOUT_SECONDS = 75
_GITHUB_REPO = os.environ.get("CTX_GITHUB_REPO", "stevesolun/ctx")
_PUBLIC_DOCS_BASE_URL = os.environ.get(
    "CTX_PUBLIC_DOCS_BASE_URL",
    "https://stevesolun.github.io/ctx",
).rstrip("/")
_GRAPH_DERIVED_STATS: dict[str, int] = {
    "tag_edges": 474_837,
    "token_edges": 280_275,
    "hydrated_incident_edges": 1_516_298,
    "hydrated_semantic_incident_edges": 911_922,
    "cross_skill_agent_edges": 52_382,
    "cross_skill_mcp_edges": 30_295,
    "cross_agent_mcp_edges": 229,
    "harness_edges": 5_063,
}


def _extract_flag_int(args: tuple[str, ...], flag: str) -> int | None:
    try:
        index = args.index(flag)
    except ValueError:
        return None
    try:
        return int(args[index + 1])
    except (IndexError, ValueError):
        return None


def _read_graph_contract_stats() -> dict[str, int | None] | None:
    """Read the exact release graph contract used by local/CI preflight."""
    if not (REPO_ROOT / "scripts" / "ci_preflight.py").exists():
        return None
    repo_root = str(REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from scripts.ci_preflight import GRAPH_VALIDATE_ARGS
    except Exception:
        return None

    stats: dict[str, int | None] = {
        "nodes": _extract_flag_int(GRAPH_VALIDATE_ARGS, "--expected-nodes"),
        "edges": _extract_flag_int(GRAPH_VALIDATE_ARGS, "--expected-edges"),
        "semantic_edges": _extract_flag_int(
            GRAPH_VALIDATE_ARGS,
            "--expected-semantic-edges",
        ),
        "skills": _extract_flag_int(GRAPH_VALIDATE_ARGS, "--expected-skill-pages"),
        "agents": _extract_flag_int(GRAPH_VALIDATE_ARGS, "--expected-agent-pages"),
        "mcps": _extract_flag_int(GRAPH_VALIDATE_ARGS, "--expected-mcp-pages"),
        "harnesses": _extract_flag_int(GRAPH_VALIDATE_ARGS, "--expected-harness-pages"),
        "communities": None,
        "skills_sh_entries": _extract_flag_int(
            GRAPH_VALIDATE_ARGS,
            "--expected-skills-sh-catalog-entries",
        ),
        "skills_sh_bodies": _extract_flag_int(
            GRAPH_VALIDATE_ARGS,
            "--expected-skills-sh-converted",
        ),
    }
    if not stats["nodes"] or not stats["skills"]:
        return None
    communities = REPO_ROOT / "graph" / "communities.json"
    if communities.exists():
        try:
            data = json.loads(communities.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            stats["communities"] = data.get("total_communities") or len(
                data.get("communities", []),
            )
        elif isinstance(data, list):
            stats["communities"] = len(data)
    stats.update(_GRAPH_DERIVED_STATS)
    return stats


def _safe_tar_name(name: str) -> str | None:
    """Return a normalized safe tar path, or ``None`` for unsafe names."""
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if not normalized:
        return None
    parts = normalized.split("/")
    first = parts[0]
    if (
        normalized.startswith("/")
        or (len(first) == 2 and first[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    return normalized


def _read_json_member(tf: tarfile.TarFile, expected_name: str) -> object | None:
    matches = [
        member for member in tf.getmembers() if _safe_tar_name(member.name) == expected_name
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"ambiguous tar member: {expected_name}")
    member = matches[0]
    if not member.isfile():
        raise ValueError(f"tar member is not a regular file: {expected_name}")
    if member.size < 0 or member.size > _MAX_TAR_JSON_BYTES:
        raise ValueError(f"tar member exceeds size cap: {expected_name}")
    f = tf.extractfile(member)
    if f is None:
        raise ValueError(f"tar member cannot be read: {expected_name}")
    payload = f.read(_MAX_TAR_JSON_BYTES + 1)
    if len(payload) > _MAX_TAR_JSON_BYTES:
        raise ValueError(f"tar member exceeds read cap: {expected_name}")
    return json.loads(payload.decode("utf-8"))


def _read_text_member(tf: tarfile.TarFile, expected_name: str) -> str | None:
    matches = [
        member for member in tf.getmembers() if _safe_tar_name(member.name) == expected_name
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError(f"ambiguous tar member: {expected_name}")
    member = matches[0]
    if not member.isfile():
        raise ValueError(f"tar member is not a regular file: {expected_name}")
    if member.size < 0 or member.size > _MAX_TAR_TEXT_BYTES:
        raise ValueError(f"tar member exceeds size cap: {expected_name}")
    f = tf.extractfile(member)
    if f is None:
        raise ValueError(f"tar member cannot be read: {expected_name}")
    payload = f.read(_MAX_TAR_TEXT_BYTES + 1)
    if len(payload) > _MAX_TAR_TEXT_BYTES:
        raise ValueError(f"tar member exceeds read cap: {expected_name}")
    return payload.decode("utf-8")


def _parse_graph_report(text: str) -> dict[str, int]:
    match = re.search(
        r"Nodes:\s*([\d,]+)\s*\|\s*Edges:\s*([\d,]+)\s*\|\s*Communities:\s*([\d,]+)",
        text,
    )
    if not match:
        return {}
    return {
        "nodes": int(match.group(1).replace(",", "")),
        "edges": int(match.group(2).replace(",", "")),
        "communities": int(match.group(3).replace(",", "")),
    }


def _read_edge_source_stats(tf: tarfile.TarFile) -> dict[str, int]:
    matches = [
        member for member in tf.getmembers() if _safe_tar_name(member.name) == _GRAPH_JSON_MEMBER
    ]
    if len(matches) != 1 or not matches[0].isfile():
        return {}
    graph_stream = tf.extractfile(matches[0])
    if graph_stream is None:
        return {}
    try:
        graph = json.load(graph_stream)
    except json.JSONDecodeError:
        return {}
    finally:
        graph_stream.close()
    edges = graph.get("edges", []) if isinstance(graph, dict) else []
    totals = {
        "semantic_edges": 0,
        "tag_edges": 0,
        "token_edges": 0,
        "hydrated_incident_edges": 0,
        "hydrated_semantic_incident_edges": 0,
        "cross_skill_agent_edges": 0,
        "cross_skill_mcp_edges": 0,
        "cross_agent_mcp_edges": 0,
        "harness_edges": 0,
    }
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        semantic = float(edge.get("semantic_sim") or 0.0) != 0.0
        if semantic:
            totals["semantic_edges"] += 1
        if float(edge.get("tag_sim") or 0.0) != 0.0:
            totals["tag_edges"] += 1
        if float(edge.get("token_sim") or 0.0) != 0.0:
            totals["token_edges"] += 1
        if "skills-sh-" in source or "skills-sh-" in target:
            totals["hydrated_incident_edges"] += 1
            if semantic:
                totals["hydrated_semantic_incident_edges"] += 1
        source_type = source.split(":", 1)[0]
        target_type = target.split(":", 1)[0]
        edge_types = {source_type, target_type}
        if edge_types == {"skill", "agent"}:
            totals["cross_skill_agent_edges"] += 1
        elif edge_types == {"skill", "mcp-server"}:
            totals["cross_skill_mcp_edges"] += 1
        elif edge_types == {"agent", "mcp-server"}:
            totals["cross_agent_mcp_edges"] += 1
        if source_type == "harness" or target_type == "harness":
            totals["harness_edges"] += 1
    return totals


def _read_skills_sh_catalog_stats() -> dict[str, int]:
    catalog_path = REPO_ROOT / "graph" / "skills-sh-catalog.json.gz"
    if not catalog_path.exists():
        return {}
    try:
        with gzip.open(catalog_path, "rt", encoding="utf-8") as f:
            catalog = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    raw_skills = catalog.get("skills") if isinstance(catalog, dict) else None
    if not isinstance(raw_skills, list):
        return {}
    skills = [item for item in raw_skills if isinstance(item, dict)]
    return {
        "skills_sh_entries": len(skills),
        "skills_sh_bodies": sum(1 for item in skills if item.get("body_available")),
    }


def _read_graph_from_tarball_legacy() -> dict[str, int | None] | None:
    """Read graph + counts from the shipped ``graph/wiki-graph.tar.gz``.

    The tarball is the canonical source of the numbers published in
    README + docs — it's what ships in releases, and it doesn't drift
    when the user's local ``~/.claude/skill-wiki/`` gets rebuilt with
    narrower tag extraction. When this function returns a non-None
    value, callers should prefer it over the local wiki.
    """
    tarball = REPO_ROOT / "graph" / "wiki-graph.tar.gz"
    if not tarball.exists():
        return None
    stats: dict[str, int | None] = {
        "nodes": None, "edges": None,
        "skills": None, "agents": None, "mcps": None, "harnesses": None,
        "communities": None,
    }
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            # Count entity pages directly from the archive index.
            # MCP entities are sharded by first char (entities/mcp-servers/<shard>/)
            # so we match the whole subtree, not just one level.
            s = a = m = h = 0
            for member in tf.getmembers():
                name = _safe_tar_name(member.name)
                if name is None or not member.isfile() or not name.endswith(".md"):
                    continue
                if name.startswith("entities/skills/"):
                    s += 1
                elif name.startswith("entities/agents/"):
                    a += 1
                elif name.startswith("entities/mcp-servers/"):
                    m += 1
                elif name.startswith("entities/harnesses/"):
                    h += 1
            stats["skills"], stats["agents"], stats["mcps"], stats["harnesses"] = s, a, m, h
            # Graph + communities are smaller files — extract to read.
            for path in (_GRAPH_JSON_MEMBER, _COMMUNITIES_JSON_MEMBER):
                body = _read_json_member(tf, path)
                if body is None:
                    continue
                if path == _GRAPH_JSON_MEMBER:
                    if not isinstance(body, dict):
                        raise ValueError("graph member must be a JSON object")
                    stats["nodes"] = len(body.get("nodes", []))
                    edges_key = next((k for k in ("edges", "links") if k in body), None)
                    if edges_key:
                        stats["edges"] = len(body[edges_key])
                else:
                    if isinstance(body, dict):
                        stats["communities"] = (
                            body.get("total_communities")
                            or len(body.get("communities", []))
                        )
                    elif isinstance(body, list):
                        stats["communities"] = len(body)
    except (tarfile.TarError, OSError, json.JSONDecodeError, ValueError):
        return None
    # Require at least nodes + skills to consider the tarball reading
    # authoritative; otherwise fall back to the live wiki.
    if stats["nodes"] and stats["skills"]:
        stats.update(_read_skills_sh_catalog_stats())
        return stats
    return None


def _read_graph_from_tarball() -> dict[str, int | None] | None:
    """Read shipped graph counts without loading graph.json when possible."""
    tarball = REPO_ROOT / "graph" / "wiki-graph.tar.gz"
    if not tarball.exists():
        return None
    stats: dict[str, int | None] = {
        "nodes": None, "edges": None,
        "skills": None, "agents": None, "mcps": None, "harnesses": None,
        "communities": None,
    }
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            s = a = m = h = 0
            for member in tf.getmembers():
                name = _safe_tar_name(member.name)
                if name is None or not member.isfile() or not name.endswith(".md"):
                    continue
                if name.startswith("entities/skills/"):
                    s += 1
                elif name.startswith("entities/agents/"):
                    a += 1
                elif name.startswith("entities/mcp-servers/"):
                    m += 1
                elif name.startswith("entities/harnesses/"):
                    h += 1
            stats["skills"], stats["agents"], stats["mcps"], stats["harnesses"] = s, a, m, h

            report = _read_text_member(tf, _GRAPH_REPORT_MEMBER)
            if report is not None:
                parsed = _parse_graph_report(report)
                for key in ("nodes", "edges", "communities"):
                    if key in parsed:
                        stats[key] = parsed[key]
                stats.update(_read_edge_source_stats(tf))

            if stats["nodes"] is None or stats["edges"] is None:
                body = _read_json_member(tf, _GRAPH_JSON_MEMBER)
                if body is not None:
                    if not isinstance(body, dict):
                        raise ValueError("graph member must be a JSON object")
                    stats["nodes"] = len(body.get("nodes", []))
                    edges_key = next((k for k in ("edges", "links") if k in body), None)
                    if edges_key:
                        stats["edges"] = len(body[edges_key])

            if stats["communities"] is None:
                body = _read_json_member(tf, _COMMUNITIES_JSON_MEMBER)
                if isinstance(body, dict):
                    stats["communities"] = (
                        body.get("total_communities")
                        or len(body.get("communities", []))
                    )
                elif isinstance(body, list):
                    stats["communities"] = len(body)
    except (tarfile.TarError, OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _read_graph_from_tarball_legacy()
    if stats["nodes"] and stats["skills"]:
        stats.update(_read_skills_sh_catalog_stats())
        return stats
    return None


def read_graph_stats() -> dict:
    """Return {nodes, edges, skills, agents, communities} from authoritative sources.

    Priority:
      1. ``graph/wiki-graph.tar.gz`` — the tarball that ships in
         releases. Pinned and canonical.
      2. ``~/.claude/skill-wiki/graphify-out/graph.json`` — the user's
         live wiki. Used only when the tarball isn't present (e.g. a
         bare clone without the release asset downloaded).

    Without this priority the pre-commit hook silently rewrites README
    badges from whatever the user last re-graphified — which can be a
    sparse experimental rebuild, not the published numbers.
    """
    contract_stats = _read_graph_contract_stats()
    if contract_stats is not None:
        return contract_stats

    tarball_stats = _read_graph_from_tarball()
    if tarball_stats is not None:
        return tarball_stats

    home = Path.home()
    graph_json = home / ".claude/skill-wiki/graphify-out/graph.json"
    communities_repo = REPO_ROOT / "graph/communities.json"

    stats: dict[str, int | None] = {
        "nodes": None,
        "edges": None,
        "skills": None,
        "agents": None,
        "mcps": None,
        "harnesses": None,
        "communities": None,
    }

    if graph_json.exists():
        g = json.loads(graph_json.read_text(encoding="utf-8"))
        stats["nodes"] = len(g.get("nodes", []))
        edges_key = next((k for k in ("edges", "links") if k in g), None)
        if edges_key:
            stats["edges"] = len(g[edges_key])
        type_counts: dict[str, int] = {}
        for n in g.get("nodes", []):
            t = n.get("type", "?")
            type_counts[t] = type_counts.get(t, 0) + 1
        stats["skills"] = type_counts.get("skill")
        stats["agents"] = type_counts.get("agent")
        stats["mcps"] = type_counts.get("mcp-server")
        stats["harnesses"] = type_counts.get("harness")

    if communities_repo.exists():
        c = json.loads(communities_repo.read_text(encoding="utf-8"))
        if isinstance(c, dict):
            stats["communities"] = c.get("total_communities") or len(c.get("communities", []))
        elif isinstance(c, list):
            stats["communities"] = len(c)

    stats.update(_read_skills_sh_catalog_stats())
    return stats


def _pytest_collect(interpreter: str) -> int | None:
    """Run pytest collection in-process and parse the reported count."""
    del interpreter  # Kept for read_test_count's existing candidate loop.
    try:
        import pytest
    except ImportError:
        return None
    stdout_buffer = io.StringIO()
    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT / "src")
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stdout_buffer):
            exit_code = pytest.main([
                "tests/",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
            ])
    except OSError:
        return None
    finally:
        os.chdir(cwd)
    if int(exit_code) != 0:
        return None
    stdout = stdout_buffer.getvalue()
    for line in reversed(stdout.strip().splitlines()):
        match = re.match(r"(\d+)\s+tests?\s+collected", line.strip())
        if match:
            return int(match.group(1)) + _uncollected_importorskip_test_count(stdout)
    return None


def _uncollected_importorskip_test_count(collected_stdout: str) -> int:
    """Count tests hidden by module-level pytest.importorskip during collection."""
    tests_dir = REPO_ROOT / "src" / "tests"
    if not tests_dir.exists():
        return 0

    count = 0
    for path in tests_dir.rglob("test_*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "pytest.importorskip(" not in text:
            continue
        repo_rel = path.relative_to(REPO_ROOT).as_posix()
        src_rel = path.relative_to(REPO_ROOT / "src").as_posix()
        if repo_rel in collected_stdout or src_rel in collected_stdout:
            continue
        count += sum(1 for line in text.splitlines() if re.match(r"\s*def\s+test_", line))
    return count


def _read_committed_test_count() -> int | None:
    """Read the checked-in test count from README/docs."""
    for path in (README, DOCS_INDEX):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern in (
            r"Tests-([\d,]+)_collected",
            r"([\d,]+)\s+tests collected",
        ):
            match = re.search(pattern, text)
            if match:
                return int(match.group(1).replace(",", ""))
    return None


def _static_test_count() -> int | None:
    """Fallback: count `def test_` definitions under src/tests/. Imprecise for
    parametrized tests but always works without a runtime interpreter."""
    tests_dir = REPO_ROOT / "src" / "tests"
    if not tests_dir.exists():
        return None
    count = 0
    for f in tests_dir.rglob("test_*.py"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        count += sum(1 for line in text.splitlines() if re.match(r"\s*def\s+test_", line))
    return count or None


def read_test_count(*, live: bool | None = None) -> int | None:
    """Return the test count used for README/docs stats.

    Default to the checked-in count so routine pre-commit/docs checks never
    import the whole test suite. Set ``CTX_UPDATE_REPO_STATS_LIVE_TESTS=1`` to
    refresh from real pytest collection after adding/removing tests.
    """
    override = os.environ.get("CTX_REPO_STATS_TEST_COUNT")
    if override:
        try:
            return int(override.replace(",", ""))
        except ValueError:
            pass

    if live is None:
        live = os.environ.get("CTX_UPDATE_REPO_STATS_LIVE_TESTS") == "1"

    if not live:
        committed = _read_committed_test_count()
        if committed is not None:
            return committed

    seen: set[str] = set()
    candidates = ["python", sys.executable, "python3", "py"]
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        count = _pytest_collect(candidate)
        if count is not None:
            return count

    # Last resort: static scan. Emit a warning so callers know the number may
    # undercount parametrized tests.
    static = _static_test_count()
    if static is not None:
        print(
            f"warning: pytest not resolvable on any interpreter; using static "
            f"def-test_ count ({static}) — may undercount parametrized tests",
            file=sys.stderr,
        )
    return static


def read_converted_count() -> int | None:
    """Count converted micro-skill pipelines in wiki."""
    conv_dir = Path.home() / ".claude/skill-wiki/converted"
    if not conv_dir.exists():
        return None
    return sum(1 for p in conv_dir.iterdir() if p.is_dir())


def format_edges(n: int) -> str:
    """642468 -> '642K', 1200000 -> '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if n >= 1_000:
        return f"{n // 1000}K"
    return str(n)


def _full_wiki_tarball_mib() -> int | None:
    """Return the shipped full wiki tarball size rounded to whole MiB."""
    tarball = REPO_ROOT / "graph" / "wiki-graph.tar.gz"
    if not tarball.exists():
        return None
    return round(tarball.stat().st_size / (1024 * 1024))


Replacement = tuple[re.Pattern[str], str]

_ENTITY_COUNT_REPLACEMENTS: tuple[tuple[str, str, str, str], ...] = (
    ("skills", "Skills", "skill", r"badge/Skills-[0-9A-Fa-f%,]+-"),
    ("agents", "Agents", "agent", r"badge/Agents-[0-9A-Fa-f%,]+-"),
    ("mcps", "MCPs", "mcp-server", r"badge/MCPs-[0-9A-Fa-f%,]+-"),
    ("harnesses", "Harnesses", "harness", r"badge/Harnesses-[0-9A-Fa-f%,]+-"),
)
_CATALOG_BEGIN = "<!-- ctx-catalog:begin -->"
_CATALOG_END = "<!-- ctx-catalog:end -->"
_CATALOG_FILTER_CARDS: tuple[tuple[str, str, str, str, str], ...] = (
    ("skill", "Skills", "Code review skills", "code review review pr diff quality bug tests", "code+review"),
    ("skill", "Skills", "Testing skills", "testing pytest unit browser smoke regression", "testing"),
    ("skill", "Skills", "Frontend skills", "frontend ui dashboard css react browser", "frontend"),
    ("agent", "Agents", "Architecture agents", "architecture design refactor planning", "architecture"),
    ("agent", "Agents", "Security agents", "security audit supply chain secrets", "security"),
    ("mcp-server", "MCPs", "GitHub MCPs", "github repo issues pull requests graphql", "github"),
    ("mcp-server", "MCPs", "Cloud MCPs", "cloud google cloud aws azure deploy", "cloud"),
    ("mcp-server", "MCPs", "Browser MCPs", "browser automation web scraping", "browser"),
    ("harness", "Harnesses", "Local/API model harnesses", "local api openai ollama vllm model harness", "local+model"),
    ("harness", "Harnesses", "Verification harnesses", "harness test eval guardrail validate verification", "verification"),
    ("harness", "Harnesses", "Tool-access harnesses", "harness tools sandbox filesystem cloud tool access", "tool+access"),
)


def _catalog_card(
    *,
    entity_type: str,
    pill: str,
    title: str,
    search: str,
    query_href: str,
    muted: str,
) -> str:
    return "\n".join([
        f'    <article class="ctx-catalog-card" data-type="{entity_type}" data-search="{search}">',
        f"      <span class=\"ctx-catalog-pill\">{pill}</span>",
        f"      <h3>{title}</h3>",
        f"      <p class=\"ctx-catalog-muted\">{muted}</p>",
        f"      <a class=\"md-button\" href=\"{query_href}\">Filter tiles</a>",
        "      <a class=\"md-button\" href=\"../dashboard/#catalog-badge-links\">Open full catalog locally</a>",
        "    </article>",
    ])


def render_catalog_cards(stats: Mapping[str, int | None]) -> str | None:
    """Render the generated public catalog tile block from graph stats."""
    required = {
        "skills": stats.get("skills"),
        "agents": stats.get("agents"),
        "mcps": stats.get("mcps"),
        "harnesses": stats.get("harnesses"),
    }
    if any(value is None for value in required.values()):
        return None
    count_cards = (
        ("skill", "Skills", "Skills", "skill prompt workflow testing code review frontend backend security research", "skills"),
        ("agent", "Agents", "Agents", "agent reviewer planner architect debugger security research", "agents"),
        ("mcp-server", "MCPs", "MCP servers", "mcp server github filesystem browser database api cloud", "mcps"),
        ("harness", "Harnesses", "Harnesses", "harness local model api model llm orchestration verification", "harnesses"),
    )
    cards: list[str] = []
    for entity_type, pill, title, search, key in count_cards:
        cards.append(_catalog_card(
            entity_type=entity_type,
            pill=pill,
            title=title,
            search=search,
            query_href=f"./?type={entity_type}",
            muted=f"{int(required[key] or 0):,} entities",
        ))
    for entity_type, pill, title, search, query in _CATALOG_FILTER_CARDS:
        cards.append(_catalog_card(
            entity_type=entity_type,
            pill=pill,
            title=title,
            search=search,
            query_href=f"./?type={entity_type}&q={query}",
            muted="Filtered catalog launcher",
        ))
    return _CATALOG_BEGIN + "\n" + "\n".join(cards) + "\n" + _CATALOG_END


def _append_badge_target_replacements(reps: list[Replacement]) -> None:
    # README badges are clicked from GitHub/Hugging Face, so they must point at
    # public documentation. The live searchable catalog remains
    # http://127.0.0.1:8765/wiki?type=... after `ctx-monitor serve`.
    badge_targets = {
        "Tests": f"https://github.com/{_GITHUB_REPO}/actions/workflows/test.yml",
        "Graph": f"{_PUBLIC_DOCS_BASE_URL}/knowledge-graph/",
        "Skills": f"{_PUBLIC_DOCS_BASE_URL}/catalog/?type=skill",
        "Agents": f"{_PUBLIC_DOCS_BASE_URL}/catalog/?type=agent",
        "MCPs": f"{_PUBLIC_DOCS_BASE_URL}/catalog/?type=mcp-server",
        "Harnesses": f"{_PUBLIC_DOCS_BASE_URL}/catalog/?type=harness",
    }
    for badge, href in badge_targets.items():
        reps.append((
            re.compile(
                rf"(\[!\[{re.escape(badge)}\]\(https://img\.shields\.io/badge/"
                rf"{re.escape(badge)}-[^)]+\.svg\)\])\([^)]+\)"
            ),
            rf"\1({href})",
        ))


def _append_catalog_card_count(
    reps: list[Replacement],
    *,
    entity_type: str,
    count: int,
) -> None:
    reps.append((
        re.compile(
            rf'(<article class="ctx-catalog-card" data-type="{re.escape(entity_type)}"'
            r'[\s\S]*?<p class="ctx-catalog-muted">)[\d,]+ entities(</p>)'
        ),
        rf"\g<1>{count:,} entities\2",
    ))


def _append_entity_count_replacements(
    reps: list[Replacement],
    stats: Mapping[str, int | None],
) -> None:
    for key, badge, entity_type, pattern in _ENTITY_COUNT_REPLACEMENTS:
        value = stats.get(key)
        if not value:
            continue
        count = int(value)
        reps.append((
            re.compile(pattern),
            f"badge/{badge}-{count:,}-".replace(",", "%2C"),
        ))
        _append_catalog_card_count(reps, entity_type=entity_type, count=count)

    catalog_cards = render_catalog_cards(stats)
    if catalog_cards is not None:
        reps.append((
            re.compile(rf"{re.escape(_CATALOG_BEGIN)}[\s\S]*?{re.escape(_CATALOG_END)}"),
            catalog_cards,
        ))


def build_replacements(
    stats: Mapping[str, int | None],
    tests: int | None,
    converted: int | None,
) -> list[Replacement]:
    """Return (regex, replacement) pairs for every stat."""
    reps: list[Replacement] = []
    _append_badge_target_replacements(reps)
    _append_entity_count_replacements(reps, stats)

    if stats["skills"]:
        s = stats["skills"]
        # 4-type pattern: "92,815 skills, 464 agents, 10,787 MCP servers,
        # and 13 harnesses". Keep this before the 3-type fallback
        # so the README's harness-aware lead sentence stays machine-owned.
        if stats["agents"] and stats["mcps"] and stats["harnesses"]:
            reps.append((
                re.compile(
                    r"\*\*[\d,]+\s+skills,\s+[\d,]+\s+agents,\s+"
                    r"[\d,]+\s+MCP\s+servers,\s+and\s+[\d,]+\s+"
                    r"(?:cataloged\s+)?harnesses\*\*"
                ),
                f"**{s:,} skills, {stats['agents']:,} agents, "
                f"{stats['mcps']:,} MCP servers, and "
                f"{stats['harnesses']:,} harnesses**",
            ))
            reps.append((
                re.compile(
                    r"\*\*[\d,]+\s+skill pages,\s+[\d,]+\s+agents,\s+"
                    r"[\d,]+\s+MCP\s+servers,\s+and\s+[\d,]+\s+"
                    r"(?:cataloged\s+)?harnesses\*\*"
                ),
                f"**{s:,} skill pages, {stats['agents']:,} agents, "
                f"{stats['mcps']:,} MCP servers, and "
                f"{stats['harnesses']:,} cataloged harnesses**",
            ))
        # 3-type pattern: "1,789 skills, 464 agents, and 10,786 MCP servers"
        # Order matters — this regex is more specific than the 2-type one
        # below, so match it first. Handles the MCP-aware tagline that
        # lands in the README after the Phase 7 MCP-first rewrite.
        if stats["agents"] and stats["mcps"]:
            reps.append((
                re.compile(
                    r"\*\*[\d,]+\s+skills,\s+[\d,]+\s+agents,\s+and\s+"
                    r"[\d,]+\s+MCP\s+servers\*\*"
                ),
                f"**{s:,} skills, {stats['agents']:,} agents, "
                f"and {stats['mcps']:,} MCP servers**",
            ))
        # 2-type fallback pattern for legacy phrasing. Only fires on
        # READMEs that haven't adopted the 3-type wording yet.
        reps.append((re.compile(r"\*\*[\d,]+\s+skills\s+and\s+[\d,]+\s+agents\*\*"),
                     f"**{s:,} skills and {stats['agents']:,} agents**"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+entity pages\s*\(one per skill\)"),
                     f"# {s:,} entity pages (one per skill)"))

    if stats["agents"]:
        a = stats["agents"]
        reps.append((re.compile(r"#\s*([\d,]+)\s+entity pages\s*\(one per agent\)"),
                     f"# {a} entity pages (one per agent)"))

    if stats["nodes"] and stats["edges"]:
        n = stats["nodes"]
        e = stats["edges"]
        e_fmt = format_edges(e)
        reps.append((re.compile(r"badge/Knowledge_Graph-[\w.]+_edges-"),
                     f"badge/Knowledge_Graph-{e_fmt}_edges-"))
        # Graph badge introduced in v0.5.0: "Graph-2,211_nodes_/_642K_edges-"
        # where the comma is URL-encoded as %2C and slash is %2F / literal.
        reps.append((re.compile(r"badge/Graph-[\w.%,/_-]+_edges-"),
                     (
                         f"badge/Graph-{n:,}_nodes_/"
                         f"_{e:,}_edges-"
                     ).replace(",", "%2C")))
        reps.append((
            re.compile(r"\*\*[\d,]+-node\*\*\s+graph"),
            f"**{n:,}-node** graph",
        ))
        reps.append((
            re.compile(r"\*\*[\d,]+\s+graph nodes\*\*"),
            f"**{n:,} graph nodes**",
        ))
        reps.append((
            re.compile(r"\*\*[\d,.]+[KM]?\s+graph edges\*\*"),
            f"**{e:,} graph edges**",
        ))
        # "A pre-built knowledge graph of 2,211 nodes and 642K edges"
        # style phrasing. Caught a stale v0.6.0 README sentence that
        # the older regex only matched on "nodes, edges, communities".
        reps.append((
            re.compile(r"([\d,]+)\s+nodes\s+and\s+[\d,.]+[KM]?\s+edges"),
            f"{n:,} nodes and {e_fmt} edges",
        ))
        reps.append((
            re.compile(r"\(([\d,]+)\s+nodes,\s+[\d,]+\s+edges\)"),
            f"({n:,} nodes, {e:,} edges)",
        ))
        reps.append((
            re.compile(r"\*\*[\d,]+\s+nodes\s*/\s*[\d,]+\s+edges\s*/\s*[\d,]+\s+Louvain communities\*\*"),
            f"**{n:,} nodes / {e:,} edges / {stats['communities']:,} Louvain communities**",
        ))
        # Graph.json inline Python example: "# 2,211 nodes, 642,468 edges"
        reps.append((
            re.compile(r"#\s*([\d,]+)\s+nodes,\s*([\d,]+)\s+edges"),
            f"# {n:,} nodes, {e:,} edges",
        ))
        reps.append((
            re.compile(r"[\d,]+ weighted edges and [\d,]+ Louvain communities"),
            f"{e:,} weighted edges and {stats['communities']:,} Louvain communities",
        ))
        # "2,211 nodes, 642K edges, 865 communities"
        reps.append((re.compile(r"([\d,]+)\s+nodes,\s+[\w.]+\s+edges,\s+([\d,]+)\s+communities"),
                     f"{n:,} nodes, {e_fmt} edges, {stats['communities']:,} communities"))
        # "2,211 nodes, 642K edges" (without communities)
        reps.append((re.compile(r"full graph \(([\d,]+)\s+nodes,\s+[\w.]+\s+edges\)"),
                     f"full graph ({n:,} nodes, {e_fmt} edges)"))
        reps.append((re.compile(r"The full graph \(([\d,]+)\s+nodes,\s+[\w.]+\s+edges\)"),
                     f"The full graph ({n:,} nodes, {e_fmt} edges)"))
        tarball_mib = _full_wiki_tarball_mib()
        if tarball_mib is not None:
            reps.append((
                re.compile(r"full ~[\d,]+\s+MiB wiki tarball"),
                f"full ~{tarball_mib:,} MiB wiki tarball",
            ))
        # "all 2,211 entities"
        reps.append((re.compile(r"all\s+[\d,]+\s+entities"), f"all {n:,} entities"))
        # "**2,211 entity pages** (1,768 skills + 443 agents)"
        if stats["skills"] and stats["agents"]:
            reps.append((
                re.compile(r"\*\*[\d,]+\s+entity pages\*\*\s*\([\d,]+\s+skills\s*\+\s*[\d,]+\s+agents\)"),
                f"**{n:,} entity pages** ({stats['skills']:,} skills + {stats['agents']:,} agents)",
            ))

        bodies_for_core = stats.get("skills_sh_bodies")
        if (
            bodies_for_core is not None
            and stats.get("skills")
            and stats.get("agents")
            and stats.get("mcps")
            and stats.get("harnesses")
        ):
            core_nodes = int(n) - int(bodies_for_core)
            curated_skills = int(stats["skills"] or 0) - int(bodies_for_core)
            reps.append((
                re.compile(
                    r"[\d,]+-node core plus [\d,]+ body-backed skill nodes"
                ),
                f"{core_nodes:,}-node core plus {int(bodies_for_core):,} "
                "body-backed skill nodes",
            ))
            reps.append((
                re.compile(
                    r"[\d,]+ shipped graph nodes: [\d,]+ curated "
                    r"skill/agent/MCP/harness\s+nodes plus [\d,]+ "
                    r"body-backed skill nodes"
                ),
                f"{n:,} shipped graph nodes: {core_nodes:,} curated "
                "skill/agent/MCP/harness nodes plus "
                f"{int(bodies_for_core):,} body-backed skill nodes",
            ))
            reps.append((
                re.compile(
                    r"\*\*[\d,]+\*\* \([\d,]+ skills \+ [\d,]+ agents "
                    r"\+ [\d,]+ MCP servers \+ [\d,]+ harnesses\)"
                ),
                f"**{core_nodes:,}** ({curated_skills:,} skills + "
                f"{stats['agents']:,} agents + {stats['mcps']:,} MCP servers "
                f"+ {stats['harnesses']:,} harnesses)",
            ))
            reps.append((
                re.compile(r"including [\d,]+ skill pages"),
                f"including {stats['skills']:,} skill pages",
            ))
            reps.append((
                re.compile(
                    r"is \*\*[\d,]+ nodes\*\* \([\d,]+ curated skills "
                    r"\+ [\d,]+ agents \+ [\d,]+ MCP servers\s+\+ "
                    r"[\d,]+ harnesses\)"
                ),
                f"is **{core_nodes:,} nodes** ({curated_skills:,} curated skills "
                f"+ {stats['agents']:,} agents + {stats['mcps']:,} MCP servers "
                f"+ {stats['harnesses']:,} harnesses)",
            ))

        if stats.get("skills") and stats.get("agents") and stats.get("mcps"):
            reps.append((
                re.compile(
                    r"from the\s+[\d,]+K?\+?\s+skills,\s+[\d,]+\+?\s+agents,\s+"
                    r"and\s+[\d,]+K?\+?\s+MCP servers"
                ),
                f"from the {stats['skills']:,} skills, {stats['agents']:,} "
                f"agents, and {stats['mcps']:,} MCP servers",
            ))

        if (
            stats.get("skills")
            and stats.get("agents")
            and stats.get("mcps")
            and stats.get("harnesses")
        ):
            reps.append((
                re.compile(
                    r"with\s+[\d,]+K?\+?\s+skill pages,\s+[\d,]+\+?\s+agents,\s+"
                    r"[\d,]+K?\+?\s+MCP servers,\s+and\s+[\d,]+\s+harnesses"
                ),
                f"with {stats['skills']:,} skill pages, "
                f"{stats['agents']:,} agents, {stats['mcps']:,} MCP servers, "
                f"and {stats['harnesses']:,} harnesses",
            ))

        table_values = {
            "Total nodes": n,
            "Total edges": e,
            "Harness edges": stats.get("harness_edges"),
        }
        for label, value in table_values.items():
            if value is not None:
                reps.append((
                    re.compile(rf"\| {re.escape(label)} \| \*\*[\d,]+\*\* \|"),
                    f"| {label} | **{int(value):,}** |",
                ))

        if stats.get("semantic_edges") and stats.get("tag_edges") and stats.get("token_edges"):
            reps.append((
                re.compile(
                    r"semantic [\d,]+ - tag [\d,]+ - token [\d,]+"
                ),
                f"semantic {stats['semantic_edges']:,} - "
                f"tag {stats['tag_edges']:,} - token {stats['token_edges']:,}",
            ))

        for label, key in (
            ("Hydrated skill incident edges", "hydrated_incident_edges"),
            ("Hydrated skill semantic incident edges", "hydrated_semantic_incident_edges"),
        ):
            value = stats.get(key)
            if value is not None:
                reps.append((
                    re.compile(rf"\| {re.escape(label)} \| \*\*?[\d,]+\*?\*? \|"),
                    f"| {label} | **{int(value):,}** |",
                ))

        for label, key in (
            ("Cross-type edges \\(skill <-> agent\\)", "cross_skill_agent_edges"),
            ("Cross-type edges \\(skill <-> MCP\\)", "cross_skill_mcp_edges"),
            ("Cross-type edges \\(agent <-> MCP\\)", "cross_agent_mcp_edges"),
        ):
            value = stats.get(key)
            if value is not None:
                display_label = label.replace("\\", "")
                reps.append((
                    re.compile(rf"\| {label} \| ~?[\d,.]+[KM]? \|"),
                    f"| {display_label} | ~{int(value):,} |",
                ))

    skills_sh_entries = stats.get("skills_sh_entries")
    skills_sh_bodies = stats.get("skills_sh_bodies")
    if skills_sh_entries is not None and skills_sh_bodies is not None:
        entries = int(skills_sh_entries)
        bodies = int(skills_sh_bodies)
        skill_pages = int(stats.get("skills") or entries)
        reps.append((
            re.compile(
                r"\*\*[\d,]+\s+(?:skills|skill entity pages)\*\*"
                r"(?:,\s+with\s+\*\*[\d,]+\*\*)?\s+hydrated installable "
                r"`SKILL\.md` bodies\."
            ),
            f"**{skill_pages:,} skill entity pages**, with **{bodies:,}** "
            "hydrated installable `SKILL.md` bodies.",
        ))
        reps.append((
            re.compile(
                r"[\d,]+ skill entity pages under `entities/skills/`, "
                r"[\d,]+ hydrated"
            ),
            f"{skill_pages:,} skill entity pages under `entities/skills/`, "
            f"{bodies:,} hydrated",
        ))
        reps.append((
            re.compile(r"\*\*[\d,]+ skill pages\*\*; \*\*[\d,]+\*\*"),
            f"**{skill_pages:,} skill pages**; **{bodies:,}**",
        ))
        reps.append((
            re.compile(
                r"\*\*[\d,]+\*\*\s+hydrated installable skill entries"
            ),
            f"**{bodies:,}** hydrated installable skill entries",
        ))
        reps.append((
            re.compile(
                r"\| Body-backed skill nodes \| \*\*[\d,]+\*\* "
                r"hydrated installable skill entries \|"
            ),
            f"| Body-backed skill nodes | **{bodies:,}** "
            "hydrated installable skill entries |",
        ))
        reps.append((
            re.compile(r"\*\*[\d,]+\*\*\s+observed body-backed skill entries"),
            f"**{bodies:,}** observed body-backed skill entries",
        ))
        reps.append((
            re.compile(r"\*\*[\d,]+\*\*\s+have hydrated catalog bodies"),
            f"**{bodies:,}** have hydrated catalog bodies",
        ))
        reps.append((
            re.compile(
                r"The shipped wiki includes [\d,]+(?: Skills\.sh)? entries, "
                r"[\d,]+ hydrated installable `SKILL\.md` bodies"
            ),
            "The shipped wiki includes "
            f"{entries:,} skill entries, {bodies:,} hydrated installable "
            "`SKILL.md` bodies",
        ))
        reps.append((
            re.compile(
                r"includes `external-catalogs/skills-sh/catalog\.json`, "
                r"[\d,]+ (?:remote-cataloged|body-backed|skill) "
                r"(?:Skills\.sh )?skill pages under "
                r"`entities/skills/skills-sh-\*\.md`, "
                r"[\d,]+ hydrated installable (?:Skills\.sh )?`SKILL\.md` files"
            ),
            "includes the shipped skill index, "
            f"{entries:,} skill pages, "
            f"{bodies:,} hydrated installable `SKILL.md` files",
        ))

    if tests is not None:
        reps.append((
            re.compile(r"badge/Tests-[0-9]+_(?:passing|collected)-"),
            f"badge/Tests-{tests}_collected-",
        ))
        reps.append((re.compile(r"#\s*([\d,]+)\s+pytest tests"), f"# {tests} pytest tests"))

    if converted is not None:
        reps.append((re.compile(r"\(([\d,]+)\s+converted\)"), f"({converted:,} converted)"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+dual-version skills"), f"# {converted:,} dual-version skills"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+micro-skill pipelines"), f"# {converted:,} micro-skill pipelines"))

    return reps


def build_docs_replacements(
    stats: Mapping[str, int | None],
    tests: int | None,
    converted: int | None,
) -> list[tuple[re.Pattern[str], str]]:
    reps = build_replacements(stats, tests, converted)
    if tests is None:
        return reps
    reps.append((
        re.compile(r"[\d,]+\s+tests collected"),
        f"{tests:,} tests collected",
    ))
    return reps


def build_github_about_description(stats: Mapping[str, int | None]) -> str:
    """Return the GitHub/HF one-line repo description from graph stats."""
    nodes = int(stats.get("nodes") or 0)
    skills = int(stats.get("skills") or 0)
    agents = int(stats.get("agents") or 0)
    mcps = int(stats.get("mcps") or 0)
    harnesses = int(stats.get("harnesses") or 0)
    if not all((nodes, skills, agents, mcps, harnesses)):
        raise ValueError("missing graph stats for GitHub About description")
    return (
        "Skill, agent, MCP, and harness recommendations for Claude Code/custom "
        f"LLMs: {nodes:,}-node LLM-wiki graph, {skills:,} skills, "
        f"{agents:,} agents, {mcps:,} MCPs, {harnesses:,} harnesses, "
        "and capped execution recommendations."
    )


def read_github_about_description(repo: str = _GITHUB_REPO) -> str:
    """Read the current GitHub repository About description via gh."""
    result = subprocess.run(
        ["gh", "repo", "view", repo, "--json", "description", "-q", ".description"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def sync_github_about(*, check_only: bool = False, repo: str = _GITHUB_REPO) -> int:
    """Check or update GitHub About so it matches README/docs graph stats."""
    expected = build_github_about_description(read_graph_stats())
    current = read_github_about_description(repo)
    if current == expected:
        print("GitHub About description is up to date.")
        return 0
    if check_only:
        print("GitHub About description is STALE.", file=sys.stderr)
        print(f"  repo: {repo}", file=sys.stderr)
        print(f"  current:  {current}", file=sys.stderr)
        print(f"  expected: {expected}", file=sys.stderr)
        return 1
    subprocess.run(
        ["gh", "repo", "edit", repo, "--description", expected],
        cwd=REPO_ROOT,
        check=True,
    )
    print(f"GitHub About description updated for {repo}.")
    return 0


def _target_is_under_repo(target: Path) -> bool:
    try:
        target.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return False
    return True


def _target_label(target: Path) -> Path:
    try:
        return target.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return target


def patch_readme(check_only: bool = False) -> int:
    stats = read_graph_stats()
    tests = read_test_count(live=True)
    converted = read_converted_count()

    missing = [k for k, v in stats.items() if v is None] + (["tests"] if tests is None else [])
    if missing:
        print(f"warning: could not resolve {missing}; those fields will be left untouched", file=sys.stderr)

    changes: list[tuple[Path, str, str]] = []
    for target in (
        README,
        DOCS_INDEX,
        DOCS_KNOWLEDGE_GRAPH,
        DOCS_CATALOG,
        DOCS_SKILL_ROUTER,
    ):
        if not _target_is_under_repo(target) or not target.exists():
            continue
        replacements = (
            build_replacements(stats, tests, converted)
            if target == README else build_docs_replacements(stats, tests, converted)
        )
        original = target.read_text(encoding="utf-8")
        patched = original
        for pattern, replacement in replacements:
            patched = pattern.sub(replacement, patched)
        if patched != original:
            changes.append((target, original, patched))

    if not changes:
        print("README/docs stats are up to date.")
        return 0

    if check_only:
        print(
            "README/docs stats are STALE -- run `python src/update_repo_stats.py` "
            "to refresh.",
            file=sys.stderr,
        )
        for target, original, patched in changes:
            diff = [
                (i + 1, o, p)
                for i, (o, p) in enumerate(
                    zip(original.splitlines(), patched.splitlines())
                )
                if o != p
            ]
            for lineno, o, p in diff[:10]:
                rel = _target_label(target)
                print(f"  {rel}:{lineno}:\n    - {o}\n    + {p}", file=sys.stderr)
        return 1

    total = 0
    for target, original, patched in changes:
        target.write_text(patched, encoding="utf-8")
        changed_lines = sum(
            1 for o, p in zip(original.splitlines(), patched.splitlines()) if o != p
        )
        total += changed_lines
        print(f"{_target_label(target)} patched: {changed_lines} lines changed")
    print(f"Repository stats patched: {total} lines changed")
    return 0

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if README is stale (for CI)")
    parser.add_argument(
        "--check-github-about",
        action="store_true",
        help="exit 1 if GitHub About description is stale",
    )
    parser.add_argument(
        "--sync-github-about",
        action="store_true",
        help="update GitHub About description via gh repo edit",
    )
    parser.add_argument(
        "--print-github-description",
        action="store_true",
        help="print the generated GitHub/HF one-line description",
    )
    parser.add_argument("--github-repo", default=_GITHUB_REPO, help="GitHub repo owner/name")
    args = parser.parse_args()
    if args.print_github_description:
        print(build_github_about_description(read_graph_stats()))
        return
    if args.check_github_about:
        sys.exit(sync_github_about(check_only=True, repo=args.github_repo))
    if args.sync_github_about:
        sys.exit(sync_github_about(check_only=False, repo=args.github_repo))
    sys.exit(patch_readme(check_only=args.check))


if __name__ == "__main__":
    main()
