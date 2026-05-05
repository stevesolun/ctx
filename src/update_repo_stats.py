#!/usr/bin/env python3
"""
update_repo_stats.py -- Patch README/docs numbers from authoritative sources.

Run by the pre-commit hook so README/docs badges and inline counts never drift
from reality. Reads only committed files and a live pytest collection.

Sources of truth:
  - graph/communities.json      -> total_communities
  - ~/.claude/skill-wiki/graphify-out/graph.json  -> nodes, edges, skill/agent counts
  - ~/.claude/skill-wiki/entities/{skills,agents}/  -> fallback entity counts
  - pytest --collect-only -q    -> collected test count

Usage:
  python src/update_repo_stats.py          # patch README/docs in place
  python src/update_repo_stats.py --check  # exit 1 if README/docs are stale
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
DOCS_INDEX = REPO_ROOT / "docs" / "index.md"
_MAX_TAR_JSON_BYTES = 512 * 1024 * 1024
_MAX_TAR_TEXT_BYTES = 2 * 1024 * 1024
_GRAPH_JSON_MEMBER = "graphify-out/graph.json"
_COMMUNITIES_JSON_MEMBER = "graphify-out/communities.json"
_GRAPH_REPORT_MEMBER = "graphify-out/graph-report.md"
_PYTEST_COLLECT_TIMEOUT_SECONDS = 180


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
    """Try to run `<interpreter> -m pytest --collect-only` and parse the count."""
    try:
        result = subprocess.run(
            [interpreter, "-m", "pytest", "tests/", "--collect-only", "-q"],
            cwd=REPO_ROOT / "src",
            capture_output=True,
            text=True,
            timeout=_PYTEST_COLLECT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in reversed(result.stdout.strip().splitlines()):
        match = re.match(r"(\d+)\s+tests?\s+collected", line.strip())
        if match:
            return int(match.group(1)) + _uncollected_importorskip_test_count(result.stdout)
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


def read_test_count() -> int | None:
    """Count collected pytest tests. Tries interpreters, falls back to static count.

    Pytest collection is the authoritative source (accounts for parametrization)
    but it is not a pass count because collect-only never executes tests.
    On mixed-python systems (e.g. Windows with pyenv) the hook's default
    `python3` may not have pytest; we try common fallbacks before giving up.
    """
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


def build_replacements(stats: dict, tests: int | None, converted: int | None) -> list[tuple[re.Pattern, str]]:
    """Return (regex, replacement) pairs for every stat."""
    reps: list[tuple[re.Pattern, str]] = []

    if stats["skills"]:
        s = stats["skills"]
        reps.append((re.compile(r"badge/Skills-[0-9%,]+-"), f"badge/Skills-{s:,}-".replace(",", "%2C")))
        # 4-type pattern: "92,815 skills, 464 agents, 10,787 MCP servers,
        # and 13 cataloged harnesses". Keep this before the 3-type fallback
        # so the README's harness-aware lead sentence stays machine-owned.
        if stats["agents"] and stats["mcps"] and stats["harnesses"]:
            reps.append((
                re.compile(
                    r"\*\*[\d,]+\s+skills,\s+[\d,]+\s+agents,\s+"
                    r"[\d,]+\s+MCP\s+servers,\s+and\s+[\d,]+\s+"
                    r"cataloged\s+harnesses\*\*"
                ),
                f"**{s:,} skills, {stats['agents']:,} agents, "
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
        reps.append((re.compile(r"badge/Agents-[0-9,]+-"), f"badge/Agents-{a}-"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+entity pages\s*\(one per agent\)"),
                     f"# {a} entity pages (one per agent)"))

    if stats["mcps"]:
        m = stats["mcps"]
        reps.append((re.compile(r"badge/MCPs-[0-9,%]+-"),
                     f"badge/MCPs-{m:,}-".replace(",", "%2C")))

    if stats["nodes"] and stats["edges"]:
        n = stats["nodes"]
        e = stats["edges"]
        e_fmt = format_edges(e)
        reps.append((re.compile(r"badge/Knowledge_Graph-[\w.]+_edges-"),
                     f"badge/Knowledge_Graph-{e_fmt}_edges-"))
        # Graph badge introduced in v0.5.0: "Graph-2,211_nodes_/_642K_edges-"
        # where the comma is URL-encoded as %2C and slash is %2F / literal.
        reps.append((re.compile(r"badge/Graph-[\w.%,/_-]+_edges-"),
                     f"badge/Graph-{n:,}_nodes_/_{e_fmt}_edges-".replace(",", "%2C")))
        reps.append((
            re.compile(r"\*\*[\d,]+-node\*\*\s+graph"),
            f"**{n:,}-node** graph",
        ))
        # "A pre-built knowledge graph of 2,211 nodes and 642K edges"
        # style phrasing. Caught a stale v0.6.0 README sentence that
        # the older regex only matched on "nodes, edges, communities".
        reps.append((
            re.compile(r"([\d,]+)\s+nodes\s+and\s+[\d,.]+[KM]?\s+edges"),
            f"{n:,} nodes and {e_fmt} edges",
        ))
        # Graph.json inline Python example: "# 2,211 nodes, 642,468 edges"
        reps.append((
            re.compile(r"#\s*([\d,]+)\s+nodes,\s*([\d,]+)\s+edges"),
            f"# {n:,} nodes, {e:,} edges",
        ))
        # "2,211 nodes, 642K edges, 865 communities"
        reps.append((re.compile(r"([\d,]+)\s+nodes,\s+[\w.]+\s+edges,\s+([\d,]+)\s+communities"),
                     f"{n:,} nodes, {e_fmt} edges, {stats['communities']:,} communities"))
        # "2,211 nodes, 642K edges" (without communities)
        reps.append((re.compile(r"full graph \(([\d,]+)\s+nodes,\s+[\w.]+\s+edges\)"),
                     f"full graph ({n:,} nodes, {e_fmt} edges)"))
        reps.append((re.compile(r"The full graph \(([\d,]+)\s+nodes,\s+[\w.]+\s+edges\)"),
                     f"The full graph ({n:,} nodes, {e_fmt} edges)"))
        # "all 2,211 entities"
        reps.append((re.compile(r"all\s+[\d,]+\s+entities"), f"all {n:,} entities"))
        # "**2,211 entity pages** (1,768 skills + 443 agents)"
        if stats["skills"] and stats["agents"]:
            reps.append((
                re.compile(r"\*\*[\d,]+\s+entity pages\*\*\s*\([\d,]+\s+skills\s*\+\s*[\d,]+\s+agents\)"),
                f"**{n:,} entity pages** ({stats['skills']:,} skills + {stats['agents']:,} agents)",
            ))

    if stats.get("skills_sh_entries") and stats.get("skills_sh_bodies"):
        entries = int(stats["skills_sh_entries"])
        bodies = int(stats["skills_sh_bodies"])
        reps.append((
            re.compile(
                r"The shipped wiki includes [\d,]+ Skills\.sh entries, "
                r"[\d,]+ hydrated installable `SKILL\.md` bodies"
            ),
            "The shipped wiki includes "
            f"{entries:,} Skills.sh entries, {bodies:,} hydrated installable "
            "`SKILL.md` bodies",
        ))
        reps.append((
            re.compile(
                r"includes `external-catalogs/skills-sh/catalog\.json`, "
                r"[\d,]+ (?:remote-cataloged|body-backed) Skills\.sh skill pages under "
                r"`entities/skills/skills-sh-\*\.md`, "
                r"[\d,]+ hydrated installable Skills\.sh `SKILL\.md` files"
            ),
            "includes `external-catalogs/skills-sh/catalog.json`, "
            f"{entries:,} body-backed Skills.sh skill pages under "
            "`entities/skills/skills-sh-*.md`, "
            f"{bodies:,} hydrated installable Skills.sh `SKILL.md` files",
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


def build_docs_replacements(tests: int | None) -> list[tuple[re.Pattern[str], str]]:
    if tests is None:
        return []
    return [(
        re.compile(r"[\d,]+\s+tests collected"),
        f"{tests:,} tests collected",
    )]


def patch_readme(check_only: bool = False) -> int:
    stats = read_graph_stats()
    tests = read_test_count()
    converted = read_converted_count()

    missing = [k for k, v in stats.items() if v is None] + (["tests"] if tests is None else [])
    if missing:
        print(f"warning: could not resolve {missing}; those fields will be left untouched", file=sys.stderr)

    changes: list[tuple[Path, str, str]] = []
    for target in (README, DOCS_INDEX):
        if not target.exists():
            continue
        replacements = (
            build_replacements(stats, tests, converted)
            if target == README else build_docs_replacements(tests)
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
                rel = target.relative_to(REPO_ROOT)
                print(f"  {rel}:{lineno}:\n    - {o}\n    + {p}", file=sys.stderr)
        return 1

    total = 0
    for target, original, patched in changes:
        target.write_text(patched, encoding="utf-8")
        changed_lines = sum(
            1 for o, p in zip(original.splitlines(), patched.splitlines()) if o != p
        )
        total += changed_lines
        print(f"{target.relative_to(REPO_ROOT)} patched: {changed_lines} lines changed")
    print(f"Repository stats patched: {total} lines changed")
    return 0

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if README is stale (for CI)")
    args = parser.parse_args()
    sys.exit(patch_readme(check_only=args.check))


if __name__ == "__main__":
    main()
