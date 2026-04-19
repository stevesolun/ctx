#!/usr/bin/env python3
"""
update_repo_stats.py -- Patch README numbers from authoritative sources.

Run by the pre-commit hook so README badges and inline counts never drift from
reality. Reads only committed files and a live pytest collection, so it's
safe and fast (<1 s).

Sources of truth:
  - graph/communities.json      -> total_communities
  - ~/.claude/skill-wiki/graphify-out/graph.json  -> nodes, edges, skill/agent counts
  - ~/.claude/skill-wiki/entities/{skills,agents}/  -> fallback entity counts
  - pytest --collect-only -q    -> test count

Usage:
  python src/update_repo_stats.py          # patch README.md in place
  python src/update_repo_stats.py --check  # exit 1 if README is stale (for CI)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"


def _read_graph_from_tarball() -> dict[str, int | None] | None:
    """Read graph + counts from the shipped ``graph/wiki-graph.tar.gz``.

    The tarball is the canonical source of the numbers published in
    README + docs — it's what ships in releases, and it doesn't drift
    when the user's local ``~/.claude/skill-wiki/`` gets rebuilt with
    narrower tag extraction. When this function returns a non-None
    value, callers should prefer it over the local wiki.
    """
    import tarfile
    tarball = REPO_ROOT / "graph" / "wiki-graph.tar.gz"
    if not tarball.exists():
        return None
    stats: dict[str, int | None] = {
        "nodes": None, "edges": None,
        "skills": None, "agents": None, "communities": None,
    }
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            members = tf.getnames()
            # Count entity pages directly from the archive index.
            s = a = 0
            for name in members:
                if "entities/skills/" in name and name.endswith(".md"):
                    s += 1
                elif "entities/agents/" in name and name.endswith(".md"):
                    a += 1
            stats["skills"], stats["agents"] = s, a
            # Graph + communities are smaller files — extract to read.
            for path in ("graphify-out/graph.json", "graphify-out/communities.json"):
                member = next((m for m in members if m.rstrip("/").endswith(path)), None)
                if member is None:
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                body = json.loads(f.read().decode("utf-8"))
                if path.endswith("graph.json"):
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
    except (tarfile.TarError, OSError, json.JSONDecodeError):
        return None
    # Require at least nodes + skills to consider the tarball reading
    # authoritative; otherwise fall back to the live wiki.
    if stats["nodes"] and stats["skills"]:
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

    if communities_repo.exists():
        c = json.loads(communities_repo.read_text(encoding="utf-8"))
        if isinstance(c, dict):
            stats["communities"] = c.get("total_communities") or len(c.get("communities", []))
        elif isinstance(c, list):
            stats["communities"] = len(c)

    return stats


def _pytest_collect(interpreter: str) -> int | None:
    """Try to run `<interpreter> -m pytest --collect-only` and parse the count."""
    try:
        result = subprocess.run(
            [interpreter, "-m", "pytest", "tests/", "--collect-only", "-q"],
            cwd=REPO_ROOT / "src",
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in reversed(result.stdout.strip().splitlines()):
        match = re.match(r"(\d+)\s+tests?\s+collected", line.strip())
        if match:
            return int(match.group(1))
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


def read_test_count() -> int | None:
    """Count pytest tests. Tries multiple interpreters, falls back to static count.

    Pytest collection is the authoritative source (accounts for parametrization)
    but requires pytest installed in the chosen interpreter. On mixed-python
    systems (e.g. Windows with pyenv) the hook's default `python3` may not
    have pytest; we try common fallbacks before giving up.
    """
    seen: set[str] = set()
    candidates = [sys.executable, "python", "python3", "py"]
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
        reps.append((re.compile(r"\*\*[\d,]+\s+skills\s+and\s+[\d,]+\s+agents\*\*"),
                     f"**{s:,} skills and {stats['agents']:,} agents**"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+entity pages\s*\(one per skill\)"),
                     f"# {s:,} entity pages (one per skill)"))

    if stats["agents"]:
        a = stats["agents"]
        reps.append((re.compile(r"badge/Agents-[0-9,]+-"), f"badge/Agents-{a}-"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+entity pages\s*\(one per agent\)"),
                     f"# {a} entity pages (one per agent)"))

    if stats["nodes"] and stats["edges"]:
        n = stats["nodes"]
        e = stats["edges"]
        e_fmt = format_edges(e)
        reps.append((re.compile(r"badge/Knowledge_Graph-[\w.]+_edges-"),
                     f"badge/Knowledge_Graph-{e_fmt}_edges-"))
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

    if tests is not None:
        reps.append((re.compile(r"badge/Tests-[0-9]+_passing-"), f"badge/Tests-{tests}_passing-"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+pytest tests"), f"# {tests} pytest tests"))

    if converted is not None:
        reps.append((re.compile(r"\(([\d,]+)\s+converted\)"), f"({converted:,} converted)"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+dual-version skills"), f"# {converted:,} dual-version skills"))
        reps.append((re.compile(r"#\s*([\d,]+)\s+micro-skill pipelines"), f"# {converted:,} micro-skill pipelines"))

    return reps


def patch_readme(check_only: bool = False) -> int:
    stats = read_graph_stats()
    tests = read_test_count()
    converted = read_converted_count()

    missing = [k for k, v in stats.items() if v is None] + (["tests"] if tests is None else [])
    if missing:
        print(f"warning: could not resolve {missing}; those fields will be left untouched", file=sys.stderr)

    original = README.read_text(encoding="utf-8")
    patched = original
    for pattern, replacement in build_replacements(stats, tests, converted):
        patched = pattern.sub(replacement, patched)

    if patched == original:
        print("README is up to date.")
        return 0

    if check_only:
        print("README is STALE — run `python src/update_repo_stats.py` to refresh.", file=sys.stderr)
        diff = [
            (i + 1, o, p) for i, (o, p) in enumerate(zip(original.splitlines(), patched.splitlines())) if o != p
        ]
        for lineno, o, p in diff[:10]:
            print(f"  line {lineno}:\n    - {o}\n    + {p}", file=sys.stderr)
        return 1

    README.write_text(patched, encoding="utf-8")
    print(f"README patched: {sum(1 for o, p in zip(original.splitlines(), patched.splitlines()) if o != p)} lines changed")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if README is stale (for CI)")
    args = parser.parse_args()
    sys.exit(patch_readme(check_only=args.check))


if __name__ == "__main__":
    main()
