#!/usr/bin/env python3
"""
skill_category.py -- Closed-set category inference + frontmatter backfill.

Phase 4 of the skill-quality plan. The KPI dashboard groups scores by
*category* (closed set) as distinct from *tags* (free-form). This module:

  - Defines the closed taxonomy: framework / language / tool / pattern /
    workflow / meta.
  - Infers a category from existing tags using a precedence-ordered
    mapping (first match wins — more specific categories first so a
    "python" + "pattern" skill lands in 'language' rather than 'pattern').
  - Exposes a CLI that scans skill + agent frontmatter and backfills the
    ``category:`` field in-place when it's missing or empty.

Design rule: we *never* overwrite an existing category. Human edits win
over inference. The backfill prints a list of unresolved entries so the
user can curate them by hand.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from ctx.utils._fs_utils import atomic_write_text as _atomic_write
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body

_logger = logging.getLogger(__name__)

CATEGORIES: tuple[str, ...] = (
    "framework",
    "language",
    "tool",
    "pattern",
    "workflow",
    "meta",
)

# Precedence-ordered mapping. The first category whose tag set intersects
# the skill's tags wins. Put narrower / more definitive categories
# earlier so "python + pattern" → language, not pattern.
_CATEGORY_TAGS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "language",
        frozenset({
            "python", "javascript", "typescript", "rust", "go", "golang",
            "java", "ruby", "swift", "kotlin", "c", "cpp", "c++", "csharp",
            "c#", "php", "elixir", "scala", "haskell", "bash", "shell",
            "sql", "html", "css", "dart",
        }),
    ),
    (
        "framework",
        frozenset({
            "react", "vue", "angular", "svelte", "solid", "nextjs", "nuxt",
            "remix", "astro", "fastapi", "django", "flask", "express",
            "nestjs", "rails", "laravel", "spring", "spring-boot", "dotnet",
            "aspnetcore", "pytorch", "tensorflow", "langchain", "llamaindex",
            "tailwind",
        }),
    ),
    (
        "tool",
        frozenset({
            "docker", "kubernetes", "k8s", "terraform", "ansible", "helm",
            "aws", "gcp", "azure", "git", "github-actions", "gitlab-ci",
            "jenkins", "ci-cd", "linting", "pytest", "jest", "cypress",
            "playwright", "webpack", "vite", "rollup", "esbuild",
            "prometheus", "grafana", "redis", "postgres", "mysql", "kafka",
            "rabbitmq", "elasticsearch", "mongodb", "sqlite", "dbt",
            "airflow", "spark",
        }),
    ),
    (
        "pattern",
        frozenset({
            "pattern", "testing", "security", "performance", "typing",
            "troubleshooting", "comparison", "decision", "architecture",
            "refactoring", "clean-code", "design-patterns",
        }),
    ),
    (
        "workflow",
        frozenset({
            "documentation", "api-spec", "agents", "llm", "rag",
            "fine-tuning", "embeddings", "mcp", "prompt-engineering",
            "code-review", "release", "incident-response", "onboarding",
        }),
    ),
    (
        "meta",
        frozenset({
            "marketplace", "registry", "versioning", "compatibility",
            "meta", "skill-router", "taxonomy",
        }),
    ),
)


def _normalize(tag: str) -> str:
    return tag.strip().lower()


def infer_category(tags: Iterable[str]) -> str | None:
    """Return the first matching category, or None if nothing matches."""
    normalized = {_normalize(t) for t in tags if isinstance(t, str) and t.strip()}
    for category, tag_set in _CATEGORY_TAGS:
        if normalized & tag_set:
            return category
    return None


# ────────────────────────────────────────────────────────────────────
# Frontmatter read/write
# ────────────────────────────────────────────────────────────────────


_CATEGORY_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)category:[ \t]*(?P<value>.*)$", re.MULTILINE,
)


def _extract_frontmatter_block(raw: str) -> tuple[str, str, str] | None:
    """Split raw markdown into (front_open, fm_body, after). None if no FM."""
    if not raw.startswith("---"):
        return None
    end_idx = raw.find("\n---", 3)
    if end_idx == -1:
        return None
    fm_block = raw[3 : end_idx + 1]
    after = raw[end_idx + 4 :]
    return "---", fm_block, after


def set_category(raw_md: str, category: str) -> tuple[str, bool]:
    """Set ``category:`` in frontmatter. Returns (new_text, changed).

    Rules:
      - If no frontmatter block, returns unchanged.
      - If ``category`` key exists and already has a non-empty value,
        returns unchanged (human edits win).
      - If key exists but is empty, fills it in.
      - If key does not exist, appends it.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category!r}")
    parts = _extract_frontmatter_block(raw_md)
    if parts is None:
        return raw_md, False
    head, fm_body, after = parts

    match = _CATEGORY_LINE_RE.search(fm_body)
    if match is not None:
        existing = match.group("value").strip()
        if existing:
            return raw_md, False
        # Empty value — fill it in preserving indent.
        new_line = f"{match.group('indent')}category: {category}"
        new_fm = (
            fm_body[: match.start()] + new_line + fm_body[match.end():]
        )
        return head + new_fm + "---" + after, True

    # Append new line. Trim any trailing blank lines inside fm_body.
    lines = fm_body.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(f"category: {category}")
    new_fm = "\n".join(lines) + "\n"
    # fm_body must remain `\n`-prefixed (starts after the opening ---).
    if not new_fm.startswith("\n"):
        new_fm = "\n" + new_fm
    return head + new_fm + "---" + after, True


def read_existing_category(raw_md: str) -> str | None:
    parts = _extract_frontmatter_block(raw_md)
    if parts is None:
        return None
    _, fm_body, _ = parts
    match = _CATEGORY_LINE_RE.search(fm_body)
    if match is None:
        return None
    value = match.group("value").strip()
    return value or None


# ────────────────────────────────────────────────────────────────────
# Corpus walk
# ────────────────────────────────────────────────────────────────────


def _tags_from_frontmatter(fm: dict[str, Any]) -> list[str]:
    raw = fm.get("tags", [])
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str)]
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return []


def _iter_skill_files(skills_dir: Path) -> list[Path]:
    if not skills_dir.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        candidate = entry / "SKILL.md"
        if candidate.is_file():
            out.append(candidate)
    return out


def _iter_agent_files(agents_dir: Path) -> list[Path]:
    if not agents_dir.is_dir():
        return []
    return [p for p in sorted(agents_dir.glob("*.md"))
            if not p.name.startswith("_")]


def backfill_file(path: Path, *, dry_run: bool) -> str:
    """Return one of: 'skipped' / 'already-set' / 'filled:<cat>' / 'unresolved'."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _logger.warning("skill_category: could not read %s: %s", path, exc)
        return "skipped"

    existing = read_existing_category(raw)
    if existing:
        return "already-set"

    fm, _body = parse_frontmatter_and_body(raw)
    tags = _tags_from_frontmatter(fm)
    category = infer_category(tags)
    if category is None:
        return "unresolved"

    new_text, changed = set_category(raw, category)
    if not changed:
        return "already-set"

    if not dry_run:
        _atomic_write(path, new_text)
    return f"filled:{category}"


def backfill_corpus(
    *,
    skills_dir: Path,
    agents_dir: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Walk skills + agents and backfill category. Returns a summary dict."""
    files = _iter_skill_files(skills_dir) + _iter_agent_files(agents_dir)
    counts = {"already-set": 0, "filled": 0, "unresolved": 0, "skipped": 0}
    unresolved_paths: list[str] = []
    category_counts: dict[str, int] = {}
    for p in files:
        result = backfill_file(p, dry_run=dry_run)
        if result == "already-set":
            counts["already-set"] += 1
        elif result == "unresolved":
            counts["unresolved"] += 1
            unresolved_paths.append(str(p))
        elif result == "skipped":
            counts["skipped"] += 1
        elif result.startswith("filled:"):
            counts["filled"] += 1
            cat = result.split(":", 1)[1]
            category_counts[cat] = category_counts.get(cat, 0) + 1
    return {
        "counts": counts,
        "category_counts": category_counts,
        "unresolved": unresolved_paths[:100],  # cap to keep CLI output sane
        "total_files": len(files),
    }


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def cmd_backfill(args: argparse.Namespace) -> int:
    from ctx_config import cfg
    summary = backfill_corpus(
        skills_dir=cfg.skills_dir,
        agents_dir=cfg.agents_dir,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    c = summary["counts"]
    print(f"Scanned {summary['total_files']} files")
    print(f"  already-set: {c['already-set']}")
    print(f"  filled:      {c['filled']}")
    print(f"  unresolved:  {c['unresolved']}")
    if summary["category_counts"]:
        print("\nBy category:")
        for cat, n in sorted(summary["category_counts"].items()):
            print(f"  {cat}: {n}")
    if summary["unresolved"] and args.verbose:
        print(f"\nUnresolved (first {len(summary['unresolved'])}):")
        for p in summary["unresolved"]:
            print(f"  {p}")
    if args.dry_run:
        print("\n(dry-run: no files written)")
    return 0


def cmd_infer(args: argparse.Namespace) -> int:
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    category = infer_category(tags)
    print(category or "unresolved")
    return 0 if category else 1


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skill_category",
        description="Closed-set category inference + frontmatter backfill.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backfill", help="Walk corpus and fill missing category fields")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--json", action="store_true")
    b.add_argument("--verbose", action="store_true",
                   help="list unresolved files at the end")
    b.set_defaults(func=cmd_backfill)

    i = sub.add_parser("infer", help="Infer a category from a comma-separated tag list")
    i.add_argument("tags")
    i.set_defaults(func=cmd_infer)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CATEGORIES",
    "backfill_corpus",
    "backfill_file",
    "infer_category",
    "main",
    "read_existing_category",
    "set_category",
]
