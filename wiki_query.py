#!/usr/bin/env python3
"""
wiki_query.py -- Query interface for the skill wiki (Karpathy LLM wiki pattern).

Usage:
    python wiki_query.py --wiki ~/.claude/skill-wiki --query "what skills handle docker?"
    python wiki_query.py --wiki ~/.claude/skill-wiki --tag python
    python wiki_query.py --wiki ~/.claude/skill-wiki --related fastapi-pro
    python wiki_query.py --wiki ~/.claude/skill-wiki --stats
    python wiki_query.py --wiki ~/.claude/skill-wiki --query "docker vs kubernetes" --save
    python wiki_query.py --wiki ~/.claude/skill-wiki --query "auth skills" --json
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class SkillPage:
    name: str
    path: Path
    title: str = ""
    tags: list[str] = field(default_factory=list)
    status: str = ""
    use_count: int = 0
    has_original: bool = False
    has_transformed: bool = False
    preferred_version: str = ""
    original_lines: int = 0
    body: str = ""
    score: float = 0.0


@dataclass
class QueryResult:
    name: str
    score: float
    tags: list[str]
    status: str
    use_count: int
    has_pipeline: bool
    excerpt: str
    wikilink: str


# --- Frontmatter parsing ---

def _extract_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Split YAML frontmatter block from body. Tolerates missing/malformed blocks."""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    fields: dict[str, str] = {}
    for line in content[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields, content[end + 4:].strip()


def _parse_list_field(raw: str) -> list[str]:
    """Parse inline YAML list ``[a, b]`` or bare ``a, b``."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [t.strip() for t in raw.split(",") if t.strip()] if raw else []


def _parse_page(path: Path) -> Optional[SkillPage]:
    """Read and parse one entity page. Returns None on read error."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields, body = _extract_frontmatter(content)
    def _int(key: str) -> int:
        try:
            return int(fields.get(key, "0"))
        except ValueError:
            return 0
    return SkillPage(
        name=path.stem,
        path=path,
        title=fields.get("title", path.stem),
        tags=_parse_list_field(fields.get("tags", "")),
        status=fields.get("status", ""),
        use_count=_int("use_count"),
        has_original=fields.get("has_original", "false").lower() == "true",
        has_transformed=fields.get("has_transformed", "false").lower() == "true",
        preferred_version=fields.get("preferred_version", ""),
        original_lines=_int("original_lines"),
        body=body,
    )


# --- Wiki loading ---

def load_all_pages(wiki: Path) -> list[SkillPage]:
    """Load every .md file under entities/skills/."""
    skills_dir = wiki / "entities" / "skills"
    if not skills_dir.exists():
        return []
    return [p for path in sorted(skills_dir.glob("*.md")) if (p := _parse_page(path)) is not None]


# --- Scoring / search ---

_STOP_WORDS = {
    "what", "which", "skills", "skill", "for", "the", "a", "an", "do", "does",
    "handle", "handles", "how", "to", "and", "or", "with", "that", "are", "is",
    "in", "of", "on", "use", "used",
}


def _score_keyword(page: SkillPage, keywords: list[str]) -> float:
    name_l = page.name.lower()
    tags_l = [t.lower() for t in page.tags]
    body_l = page.body.lower()
    score = 0.0
    for kw in keywords:
        if kw in name_l:
            score += 10.0
        if kw in tags_l:
            score += 6.0
        score += sum(2.0 for t in tags_l if kw in t and kw != t)
        score += min(body_l.count(kw) * 0.5, 4.0)
    if page.status == "installed":
        score += 0.5
    score += min(page.use_count * 0.1, 1.0)
    return score


def search_by_query(pages: list[SkillPage], query: str, top_n: int = 15) -> list[SkillPage]:
    """Keyword search across name, tags, and body. Returns top_n scored pages."""
    keywords = [w for w in re.split(r"\W+", query.lower()) if w and w not in _STOP_WORDS]
    if not keywords:
        keywords = query.lower().split()
    scored = [page for page in pages if (s := _score_keyword(page, keywords)) > 0 and setattr(page, "score", s) is None]  # type: ignore[func-returns-value]
    scored.sort(key=lambda p: p.score, reverse=True)
    return scored[:top_n]


def filter_by_tag(pages: list[SkillPage], tag: str) -> list[SkillPage]:
    """Return all pages whose tags contain *tag* (case-insensitive substring match)."""
    tl = tag.lower()
    return [p for p in pages if any(tl in t.lower() for t in p.tags)]


def find_related(pages: list[SkillPage], skill_name: str, top_n: int = 12) -> list[SkillPage]:
    """Find pages sharing tags with the named skill; fuzzy name fallback."""
    target = next((p for p in pages if p.name == skill_name), None)
    if target is None:
        hits = [p for p in pages if skill_name.lower() in p.name.lower()]
        target = hits[0] if hits else None
    if target is None or not target.tags:
        return []
    ttags = {t.lower() for t in target.tags}
    scored: list[SkillPage] = []
    for page in pages:
        if page.name == target.name:
            continue
        shared = len(ttags & {t.lower() for t in page.tags})
        if shared:
            page.score = float(shared)
            scored.append(page)
    scored.sort(key=lambda p: p.score, reverse=True)
    return scored[:top_n]


# --- Stats ---

def compute_stats(wiki: Path, pages: list[SkillPage]) -> dict:
    """Aggregate wiki-wide statistics."""
    tag_counts: dict[str, int] = {}
    for page in pages:
        for tag in page.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    extra = sum(
        1 for sec in ("concepts", "comparisons", "queries")
        for _ in (wiki / sec).glob("*.md")
        if (wiki / sec).exists()
    )
    high_use = sorted(pages, key=lambda p: p.use_count, reverse=True)[:10]
    return {
        "total_entity_pages": len(pages),
        "installed": sum(1 for p in pages if p.status == "installed"),
        "stale": sum(1 for p in pages if p.status == "stale"),
        "with_pipeline": sum(1 for p in pages if p.has_transformed),
        "with_original": sum(1 for p in pages if p.has_original),
        "extra_pages": extra,
        "unique_tags": len(tag_counts),
        "top_tags": sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:15],
        "high_use_skills": [(p.name, p.use_count) for p in high_use if p.use_count > 0],
    }


# --- Output rendering ---

def _excerpt(page: SkillPage, max_chars: int = 120) -> str:
    lines = [ln for ln in page.body.strip().splitlines() if ln.strip() and not ln.startswith("#")]
    text = " ".join(lines[:3])
    return text[:max_chars].rstrip() + "..." if len(text) > max_chars else text


def _to_result(page: SkillPage) -> QueryResult:
    return QueryResult(
        name=page.name,
        score=round(page.score, 2),
        tags=page.tags,
        status=page.status,
        use_count=page.use_count,
        has_pipeline=page.has_transformed,
        excerpt=_excerpt(page),
        wikilink=f"[[entities/skills/{page.name}]]",
    )


def render_markdown(results: list[QueryResult], heading: str, cited: list[str]) -> str:
    lines: list[str] = [f"## {heading}", ""]
    for r in results:
        tags_str = ", ".join(r.tags) if r.tags else "_none_"
        pipeline = " `pipeline`" if r.has_pipeline else ""
        lines += [
            f"### {r.wikilink}{pipeline}",
            f"- **Tags**: {tags_str}",
            f"- **Status**: {r.status or '_unknown_'} | **Uses**: {r.use_count}",
            *([] if not r.excerpt else [f"- {r.excerpt}"]),
            "",
        ]
    if cited:
        cite = "Based on " + " and ".join(f"[[entities/skills/{c}]]" for c in cited[:5])
        if len(cited) > 5:
            cite += f" (and {len(cited) - 5} more)"
        lines.append(f"_{cite}_")
    return "\n".join(lines)


def render_stats_markdown(stats: dict) -> str:
    rows = [
        ("Entity pages (skills)", stats["total_entity_pages"]),
        ("Installed", stats["installed"]),
        ("Stale", stats["stale"]),
        ("With micro-skill pipeline", stats["with_pipeline"]),
        ("With original backup", stats["with_original"]),
        ("Concept/comparison/query pages", stats["extra_pages"]),
        ("Unique tags", stats["unique_tags"]),
    ]
    lines = ["## Wiki Statistics", "", "| Metric | Count |", "|--------|-------|"]
    lines += [f"| {label} | {val} |" for label, val in rows]
    lines += ["", "### Top Tags", "", "| Tag | Pages |", "|-----|-------|"]
    lines += [f"| {tag} | {count} |" for tag, count in stats["top_tags"]]
    if stats["high_use_skills"]:
        lines += ["", "### Most Used Skills", ""]
        lines += [f"- [[entities/skills/{n}]] — {c} uses" for n, c in stats["high_use_skills"]]
    return "\n".join(lines)


# --- Wiki persistence ---

def _append_log(wiki: Path, action: str, subject: str, details: list[str]) -> None:
    entry = f"\n## [{TODAY}] {action} | {subject}\n" + "".join(f"- {d}\n" for d in details)
    with open(wiki / "log.md", "a", encoding="utf-8") as fh:
        fh.write(entry)


def _update_index_queries(wiki: Path, slug: str, query: str) -> None:
    index_path = wiki / "index.md"
    if not index_path.exists():
        return
    content = index_path.read_text(encoding="utf-8", errors="replace")
    entry = f"- [[queries/{slug}]] - {query}"
    if entry in content:
        return
    lines = content.splitlines()
    insert_idx, in_q = len(lines), False
    for i, line in enumerate(lines):
        if line.strip() == "## Queries":
            in_q, insert_idx = True, i + 1
        elif in_q and line.startswith("## "):
            insert_idx = i
            break
    lines.insert(insert_idx, entry)
    index_path.write_text("\n".join(lines), encoding="utf-8")


def save_query_page(wiki: Path, query: str, content: str) -> Path:
    """Write synthesis result to queries/, register in index, and log the action."""
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^\w-]", "-", query.lower().strip()))[:60].strip("-")
    queries_dir = wiki / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)
    page_path = queries_dir / f"{slug}.md"
    fm = f'---\ntitle: "{query}"\ncreated: {TODAY}\nupdated: {TODAY}\ntype: query\n---\n\n'
    page_path.write_text(fm + content, encoding="utf-8")
    _update_index_queries(wiki, slug, query)
    _append_log(wiki, "query", query, [f"Saved to queries/{slug}.md"])
    return page_path


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query the skill wiki (Karpathy wiki pattern)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--wiki", default=os.path.expanduser("~/.claude/skill-wiki"),
                        help="Wiki root path (default: ~/.claude/skill-wiki)")
    parser.add_argument("--query", "-q", help="Keyword query: searches name, tags, and body")
    parser.add_argument("--tag",   "-t", help="Filter skills by tag")
    parser.add_argument("--related", "-r", help="Find skills related to a given skill name")
    parser.add_argument("--stats", "-s", action="store_true", help="Show wiki statistics")
    parser.add_argument("--save", action="store_true",
                        help="Save --query results as a new page in queries/")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--top", type=int, default=15, help="Max results (default: 15)")
    args = parser.parse_args()

    wiki = Path(args.wiki)
    if not wiki.exists():
        print(f"Error: wiki not found at {wiki}", file=sys.stderr)
        sys.exit(1)

    mode_count = sum([bool(args.query), bool(args.tag), bool(args.related), args.stats])
    if mode_count == 0:
        parser.print_help()
        sys.exit(0)
    if mode_count > 1:
        print("Error: specify only one of --query, --tag, --related, --stats", file=sys.stderr)
        sys.exit(1)

    if args.stats:
        pages = load_all_pages(wiki)
        stats = compute_stats(wiki, pages)
        print(json.dumps(stats, indent=2) if args.json else render_stats_markdown(stats))
        _append_log(wiki, "stats", "wiki-stats", [f"Pages counted: {stats['total_entity_pages']}"])
        return

    pages = load_all_pages(wiki)
    if not pages:
        print("No entity pages found. Run wiki_sync.py to populate the wiki.", file=sys.stderr)
        sys.exit(1)

    results: list[SkillPage] = []
    heading = ""

    if args.query:
        results = search_by_query(pages, args.query, top_n=args.top)
        heading = f'Skills matching "{args.query}"'
        _append_log(wiki, "query", args.query, [
            f"Query: {args.query}",
            f"Results: {len(results)}",
            f"Top match: {results[0].name if results else 'none'}",
        ])
    elif args.tag:
        results = sorted(filter_by_tag(pages, args.tag), key=lambda p: p.use_count, reverse=True)[:args.top]
        heading = f'Skills tagged "{args.tag}"'
        _append_log(wiki, "tag-filter", args.tag, [f"Results: {len(results)}"])
    elif args.related:
        results = find_related(pages, args.related, top_n=args.top)
        heading = f'Skills related to [[entities/skills/{args.related}]]'
        _append_log(wiki, "related", args.related, [
            f"Related found: {len(results)}",
            f"Top: {results[0].name if results else 'none'}",
        ])

    if not results:
        print(json.dumps({"results": [], "total": 0}) if args.json else "No matching skills found.")
        return

    query_results = [_to_result(r) for r in results]

    if args.json:
        print(json.dumps({
            "query": args.query or args.tag or args.related,
            "mode": "query" if args.query else ("tag" if args.tag else "related"),
            "total": len(query_results),
            "results": [
                {"name": r.name, "score": r.score, "tags": r.tags, "status": r.status,
                 "use_count": r.use_count, "has_pipeline": r.has_pipeline,
                 "excerpt": r.excerpt, "wikilink": r.wikilink}
                for r in query_results
            ],
        }, indent=2))
        return

    cited = [r.name for r in results]
    md_output = render_markdown(query_results, heading, cited)
    print(md_output)

    if args.save and args.query:
        saved_path = save_query_page(wiki, args.query, md_output)
        print(f"\n_Saved to {saved_path}_")
    elif args.save:
        print("\nNote: --save only applies to --query mode.", file=sys.stderr)


if __name__ == "__main__":
    main()
