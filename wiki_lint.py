#!/usr/bin/env python3
"""
wiki_lint.py -- Health audit for the Karpathy-style skill wiki.

Usage:
    python wiki_lint.py --wiki ~/.claude/skill-wiki [--fix] [--json]

Checks:
  1.  Orphan pages       -- entity pages with zero inbound [[wikilinks]]
  2.  Broken wikilinks   -- [[target]] references where target doesn't exist
  3.  Missing frontmatter-- pages without required YAML keys
  4.  Stale content      -- pages with updated > 90 days ago
  5.  Index completeness -- on-disk pages missing from index.md
  6.  Tag hygiene        -- tags not defined in SCHEMA.md taxonomy
  7.  Wikilink minimum   -- pages with fewer than 2 outbound [[wikilinks]]
  8.  Log rotation       -- warn if log.md exceeds 500 entries
  9.  Oversized pages    -- pages exceeding 200 lines
  10. Pipeline linkage   -- has_pipeline:true but no converted/<name>/ dir
  11. Contradictions     -- pages with contradictions: frontmatter field set
"""

from __future__ import annotations

import argparse, json, os, re, sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:[|#][^\]]*)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
REQUIRED_FM_KEYS = {"title", "created", "updated", "type", "tags"}
STALE_DAYS = 90
LOG_ENTRY_LIMIT = 500
MAX_PAGE_LINES = 200
MIN_OUTBOUND_LINKS = 2
TODAY = date.today()
ROOT_FILES = {"SCHEMA.md", "index.md", "log.md"}
CHECK_ORDER = [
    "broken_wikilink", "orphan_page", "stale_content", "missing_frontmatter",
    "index_completeness", "tag_hygiene", "wikilink_minimum",
    "log_rotation", "oversized_page", "pipeline_linkage", "contradiction",
]


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # "error" | "warn" | "info"
    page: str
    message: str


@dataclass(frozen=True)
class AuditResult:
    findings: tuple[Finding, ...]
    stats: dict[str, int]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_frontmatter(text: str) -> dict[str, Any]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        fm[key] = val
    return fm


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None

def _wikilinks(text: str) -> list[str]:
    return WIKILINK_RE.findall(text)

def _collect_pages(wiki: Path) -> dict[str, Path]:
    pages: dict[str, Path] = {}
    for p in wiki.rglob("*.md"):
        if p.name in ROOT_FILES and p.parent == wiki:
            continue
        slug = p.relative_to(wiki).as_posix().removesuffix(".md")
        pages[slug] = p
        if p.stem not in pages:
            pages[p.stem] = p
    return pages

def _is_canonical(slug: str) -> bool:
    return "/" in slug

def _schema_tags(wiki: Path) -> set[str]:
    schema = wiki / "SCHEMA.md"
    if not schema.exists():
        return set()
    tags: set[str] = set()
    for line in _read(schema).splitlines():
        if not line.strip().startswith("-") or ":" not in line:
            continue
        _, _, rest = line.partition(":")
        tags.update(t.strip().lower() for t in re.split(r"[,\s]+", rest) if t.strip())
    return tags

def _index_refs(wiki: Path) -> set[str]:
    idx = wiki / "index.md"
    if not idx.exists():
        return set()
    refs: set[str] = set()
    for link in _wikilinks(_read(idx)):
        refs.add(link.strip().removesuffix(".md"))
        refs.add(Path(link.strip()).stem)
    return refs

def _log_entry_count(wiki: Path) -> int:
    log = wiki / "log.md"
    return len(re.findall(r"^##\s+\[", _read(log), re.MULTILINE)) if log.exists() else 0

def _find(check: str, sev: str, page: str, msg: str) -> Finding:
    return Finding(check=check, severity=sev, page=page, message=msg)


def check_broken_wikilinks(pages: dict[str, Path]) -> list[Finding]:
    out: list[Finding] = []
    for slug, path in pages.items():
        if not _is_canonical(slug):
            continue
        for link in _wikilinks(_read(path)):
            lc = link.strip().removesuffix(".md")
            if lc not in pages and Path(lc).stem not in pages:
                out.append(_find("broken_wikilink", "error", slug,
                                 f"[[{link}]] resolves to no existing page"))
    return out


def check_orphan_pages(pages: dict[str, Path]) -> list[Finding]:
    inbound: dict[str, int] = {s: 0 for s in pages}
    for slug, path in pages.items():
        for link in _wikilinks(_read(path)):
            lc = link.strip().removesuffix(".md")
            for key in (lc, Path(lc).stem):
                if key in inbound and key != slug:
                    inbound[key] += 1
    return [
        _find("orphan_page", "warn", slug, "No inbound [[wikilinks]] from any other page")
        for slug, count in inbound.items()
        if count == 0 and _is_canonical(slug)
    ]


def check_missing_frontmatter(pages: dict[str, Path]) -> list[Finding]:
    out: list[Finding] = []
    for slug, path in pages.items():
        if not _is_canonical(slug):
            continue
        fm = _parse_frontmatter(_read(path))
        if not fm:
            out.append(_find("missing_frontmatter", "error", slug, "No YAML frontmatter block found"))
        elif missing := REQUIRED_FM_KEYS - fm.keys():
            out.append(_find("missing_frontmatter", "error", slug,
                             f"Frontmatter missing keys: {sorted(missing)}"))
    return out


def check_stale_content(pages: dict[str, Path]) -> list[Finding]:
    out: list[Finding] = []
    for slug, path in pages.items():
        if not _is_canonical(slug):
            continue
        fm = _parse_frontmatter(_read(path))
        updated = _parse_date(str(fm.get("updated", "")))
        if updated and (age := (TODAY - updated).days) > STALE_DAYS:
            out.append(_find("stale_content", "warn", slug,
                             f"updated {age} days ago (threshold: {STALE_DAYS})"))
    return out


def check_index_completeness(pages: dict[str, Path], wiki: Path) -> list[Finding]:
    refs = _index_refs(wiki)
    return [
        _find("index_completeness", "warn", slug, "Page not listed in index.md")
        for slug in pages
        if _is_canonical(slug) and slug not in refs and Path(slug).stem not in refs
    ]


def check_tag_hygiene(pages: dict[str, Path], wiki: Path) -> list[Finding]:
    allowed = _schema_tags(wiki)
    if not allowed:
        return []
    out: list[Finding] = []
    for slug, path in pages.items():
        if not _is_canonical(slug):
            continue
        raw = _parse_frontmatter(_read(path)).get("tags", [])
        tag_list: list[str] = raw if isinstance(raw, list) else [str(raw)]
        for tag in tag_list:
            t = tag.strip().lower()
            if t and t not in allowed and t != "uncategorized":
                out.append(_find("tag_hygiene", "warn", slug,
                                 f"Tag '{t}' not in SCHEMA.md taxonomy"))
    return out


def check_wikilink_minimum(pages: dict[str, Path]) -> list[Finding]:
    return [
        _find("wikilink_minimum", "warn", slug,
              f"{n} outbound [[wikilinks]] (minimum: {MIN_OUTBOUND_LINKS})")
        for slug, path in pages.items()
        if _is_canonical(slug) and (n := len(_wikilinks(_read(path)))) < MIN_OUTBOUND_LINKS
    ]


def check_log_rotation(wiki: Path) -> list[Finding]:
    n = _log_entry_count(wiki)
    if n > LOG_ENTRY_LIMIT:
        return [_find("log_rotation", "warn", "log.md",
                      f"{n} entries (threshold: {LOG_ENTRY_LIMIT}); consider archiving")]
    return []


def check_oversized_pages(pages: dict[str, Path]) -> list[Finding]:
    return [
        _find("oversized_page", "info", slug, f"{n} lines (threshold: {MAX_PAGE_LINES})")
        for slug, path in pages.items()
        if _is_canonical(slug) and (n := len(_read(path).splitlines())) > MAX_PAGE_LINES
    ]


def check_pipeline_linkage(pages: dict[str, Path], wiki: Path) -> list[Finding]:
    converted = wiki / "converted"
    out: list[Finding] = []
    for slug, path in pages.items():
        if not _is_canonical(slug):
            continue
        fm = _parse_frontmatter(_read(path))
        if str(fm.get("has_pipeline", "")).strip().lower() not in ("true", "yes", "1"):
            continue
        if not (converted / path.stem).is_dir():
            out.append(_find("pipeline_linkage", "error", slug,
                             f"has_pipeline: true but converted/{path.stem}/ not found"))
    return out


def check_contradictions(pages: dict[str, Path]) -> list[Finding]:
    out: list[Finding] = []
    for slug, path in pages.items():
        if not _is_canonical(slug):
            continue
        raw = _parse_frontmatter(_read(path)).get("contradictions", None)
        if raw is not None and str(raw).strip() not in ("", "null", "~", "[]"):
            out.append(_find("contradiction", "info", slug,
                             f"Flagged for contradiction review: {raw}"))
    return out

def fix_index(wiki: Path, missing_slugs: list[str]) -> int:
    idx = wiki / "index.md"
    if not idx.exists() or not missing_slugs:
        return 0
    lines = _read(idx).splitlines()
    content = "\n".join(lines)
    section_map = {
        "skills": "## Skills", "plugins": "## Plugins", "mcp": "## MCP Servers",
        "concepts": "## Concepts", "comparisons": "## Comparisons", "queries": "## Queries",
    }
    added = 0
    for slug in sorted(missing_slugs):
        entry = f"- [[{slug}]]"
        if entry in content:
            continue
        section = next(
            (h for key, h in section_map.items() if key in slug), "## Skills"
        )
        insert_at = next(
            (i + 1 for i, l in enumerate(lines) if l.strip() == section), len(lines)
        )
        lines.insert(insert_at, entry)
        content = "\n".join(lines)
        added += 1
    idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added


def fix_log_rotation(wiki: Path) -> bool:
    log = wiki / "log.md"
    if not log.exists():
        return False
    text = _read(log)
    blocks = re.split(r"(?=^## \[)", text, flags=re.MULTILINE)
    header = blocks[0] if not blocks[0].startswith("## [") else ""
    entries = [b for b in blocks if b.startswith("## [")]
    if len(entries) <= LOG_ENTRY_LIMIT:
        return False
    archive = wiki / f"log-archive-{TODAY.isoformat()}.md"
    archive.write_text("# Skill Wiki Log Archive\n\n" + "".join(entries[:-100]), encoding="utf-8")
    log.write_text(header + "".join(entries[-100:]), encoding="utf-8")
    return True

def run_audit(wiki: Path) -> AuditResult:
    pages = _collect_pages(wiki)
    findings: list[Finding] = (
        check_broken_wikilinks(pages)
        + check_orphan_pages(pages)
        + check_missing_frontmatter(pages)
        + check_stale_content(pages)
        + check_index_completeness(pages, wiki)
        + check_tag_hygiene(pages, wiki)
        + check_wikilink_minimum(pages)
        + check_log_rotation(wiki)
        + check_oversized_pages(pages)
        + check_pipeline_linkage(pages, wiki)
        + check_contradictions(pages)
    )
    canonical = sum(1 for s in pages if _is_canonical(s))
    stats = {
        "total_pages": canonical,
        "errors": sum(1 for f in findings if f.severity == "error"),
        "warnings": sum(1 for f in findings if f.severity == "warn"),
        "info": sum(1 for f in findings if f.severity == "info"),
    }
    return AuditResult(findings=tuple(findings), stats=stats)

def print_report(result: AuditResult) -> None:
    s = result.stats
    print(
        f"\nWiki Lint  {TODAY.isoformat()}\n"
        f"Pages: {s['total_pages']}  Errors: {s['errors']}  "
        f"Warnings: {s['warnings']}  Info: {s['info']}\n" + "-" * 56
    )
    grouped: dict[str, list[Finding]] = {}
    for f in result.findings:
        grouped.setdefault(f.check, []).append(f)
    for check in CHECK_ORDER + [c for c in grouped if c not in CHECK_ORDER]:
        if check not in grouped:
            continue
        items = grouped[check]
        print(f"\n[{items[0].severity.upper()}] {check.replace('_', ' ').title()} ({len(items)})")
        for item in items:
            print(f"  {item.page}\n    {item.message}")
    if not result.findings:
        print("\nNo issues found. Wiki is healthy.")
    print()

def print_json_output(result: AuditResult) -> None:
    print(json.dumps({
        "stats": result.stats,
        "findings": [
            {"check": f.check, "severity": f.severity, "page": f.page, "message": f.message}
            for f in result.findings
        ],
    }, indent=2))

def main() -> None:
    parser = argparse.ArgumentParser(description="Audit skill wiki health.")
    parser.add_argument("--wiki", default=os.path.expanduser("~/.claude/skill-wiki"),
                        help="Wiki root path (default: ~/.claude/skill-wiki)")
    parser.add_argument("--fix", action="store_true",
                        help="Auto-fix: add missing index entries, rotate log")
    parser.add_argument("--json", action="store_true",
                        help="Output findings as JSON")
    args = parser.parse_args()

    wiki = Path(args.wiki).expanduser().resolve()
    if not wiki.is_dir():
        print(f"Error: wiki directory not found: {wiki}", file=sys.stderr)
        sys.exit(1)

    result = run_audit(wiki)

    if args.fix:
        missing = [f.page for f in result.findings if f.check == "index_completeness"]
        added = fix_index(wiki, missing)
        rotated = fix_log_rotation(wiki)
        if not args.json:
            if added:
                print(f"[fix] Added {added} entries to index.md")
            if rotated:
                print(f"[fix] Rotated log.md; archive written to wiki root")

    if args.json:
        print_json_output(result)
    else:
        print_report(result)

    sys.exit(1 if result.stats["errors"] > 0 else 0)


if __name__ == "__main__":
    main()
