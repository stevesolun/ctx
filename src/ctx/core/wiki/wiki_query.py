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
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ctx_config import cfg
from ctx.core.entity_types import (
    ENTITY_TYPE_FOR_SUBJECT_TYPE,
    RECOMMENDABLE_ENTITY_TYPES,
    SUBJECT_TYPE_FOR_ENTITY_TYPE,
    entity_wikilink,
    mcp_shard,
)
from ctx.core.wiki.wiki_packs import load_merged_wiki_pages, write_active_wiki_overlay_pack
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body as _extract_frontmatter
from ctx.utils._safe_name import is_safe_source_name

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class SkillPage:
    name: str
    path: Path
    entity_type: str = "skill"
    wikilink: str = ""
    title: str = ""
    description: str = ""
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
    entity_type: str
    score: float
    tags: list[str]
    status: str
    use_count: int
    has_pipeline: bool
    description: str
    excerpt: str
    wikilink: str


# _extract_frontmatter is imported from wiki_utils


def _parse_list_field(raw: str | list) -> list[str]:
    """Normalise a frontmatter value to a list of strings."""
    if isinstance(raw, list):
        return raw
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [t.strip() for t in raw.split(",") if t.strip()] if raw else []


def _parse_page(
    path: Path,
    *,
    entity_type: str = "skill",
    wikilink: str | None = None,
) -> Optional[SkillPage]:
    """Read and parse one entity page. Returns None on read error."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _parse_page_text(path, content, entity_type=entity_type, wikilink=wikilink)


def _parse_page_text(
    path: Path,
    content: str,
    *,
    entity_type: str = "skill",
    wikilink: str | None = None,
) -> SkillPage:
    """Parse one entity page from markdown text."""
    fields, body = _extract_frontmatter(content)

    def _int(key: str) -> int:
        try:
            return int(fields.get(key, "0"))
        except ValueError:
            return 0

    return SkillPage(
        name=path.stem,
        path=path,
        entity_type=entity_type,
        wikilink=wikilink or f"[[entities/skills/{path.stem}]]",
        title=fields.get("title", path.stem),
        description=fields.get("description", fields.get("summary", "")),
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


def _wikilink(entity_type: str, slug: str) -> str:
    return entity_wikilink(entity_type, slug) or f"[[entities/skills/{slug}]]"


def _load_flat_entity_pages(root: Path, entity_type: str) -> list[SkillPage]:
    if not root.exists():
        return []
    pages: list[SkillPage] = []
    for path in sorted(root.glob("*.md")):
        slug = path.stem
        if not is_safe_source_name(slug):
            continue
        page = _parse_page(path, entity_type=entity_type, wikilink=_wikilink(entity_type, slug))
        if page is not None:
            pages.append(page)
    return pages


def _load_sharded_mcp_pages(root: Path) -> list[SkillPage]:
    if not root.exists():
        return []
    pages: list[SkillPage] = []
    for path in sorted(root.glob("*/*.md")):
        slug = path.stem
        if not is_safe_source_name(slug):
            continue
        if path.parent.name != mcp_shard(slug):
            continue
        page = _parse_page(path, entity_type="mcp-server", wikilink=_wikilink("mcp-server", slug))
        if page is not None:
            pages.append(page)
    return pages


def _pack_page_type_and_slug(relpath: str) -> tuple[str, str] | None:
    path = Path(relpath)
    parts = path.parts
    if len(parts) < 3 or parts[0] != "entities" or path.suffix != ".md":
        return None
    subject_type = parts[1]
    entity_type = ENTITY_TYPE_FOR_SUBJECT_TYPE.get(subject_type)
    if entity_type not in RECOMMENDABLE_ENTITY_TYPES:
        return None
    slug = path.stem
    if not is_safe_source_name(slug):
        return None
    if entity_type == "mcp-server":
        if len(parts) != 4 or parts[2] != mcp_shard(slug):
            return None
    elif len(parts) != 3:
        return None
    return entity_type, slug


def _load_wiki_pack_pages(wiki: Path) -> list[SkillPage]:
    pages: list[SkillPage] = []
    for relpath, content in sorted(load_merged_wiki_pages(wiki / "wiki-packs").items()):
        parsed = _pack_page_type_and_slug(relpath)
        if parsed is None:
            continue
        entity_type, slug = parsed
        page = _parse_page_text(
            wiki / relpath,
            content,
            entity_type=entity_type,
            wikilink=_wikilink(entity_type, slug),
        )
        pages.append(page)
    return pages


def load_all_pages(wiki: Path) -> list[SkillPage]:
    """Load recommendable entity pages from the wiki."""
    if (wiki / "wiki-packs").is_dir():
        return _load_wiki_pack_pages(wiki)
    entities = wiki / "entities"
    pages: list[SkillPage] = []
    for entity_type in RECOMMENDABLE_ENTITY_TYPES:
        subject_type = SUBJECT_TYPE_FOR_ENTITY_TYPE[entity_type]
        if entity_type == "mcp-server":
            pages.extend(_load_sharded_mcp_pages(entities / subject_type))
        else:
            pages.extend(_load_flat_entity_pages(entities / subject_type, entity_type))
    return pages


# --- Scoring / search ---

_STOP_WORDS = {
    "what",
    "which",
    "skills",
    "skill",
    "for",
    "the",
    "a",
    "an",
    "do",
    "does",
    "handle",
    "handles",
    "how",
    "to",
    "and",
    "or",
    "with",
    "that",
    "are",
    "is",
    "in",
    "of",
    "on",
    "use",
    "used",
}


def _score_keyword(page: SkillPage, keywords: list[str]) -> float:
    name_l = page.name.lower()
    title_l = page.title.lower()
    description_l = page.description.lower()
    tags_l = [t.lower() for t in page.tags]
    body_l = page.body.lower()
    score = 0.0
    for kw in keywords:
        if kw in name_l:
            score += 10.0
        if kw in title_l:
            score += 8.0
        if kw in description_l:
            score += 5.0
        if kw in tags_l:
            score += 6.0
        score += sum(2.0 for t in tags_l if kw in t and kw != t)
        score += min(body_l.count(kw) * 0.5, 4.0)
    if score <= 0:
        return 0.0
    if page.status == "installed":
        score += 0.5
    score += min(page.use_count * 0.1, 1.0)
    return score


def search_by_query(pages: list[SkillPage], query: str, top_n: int = 15) -> list[SkillPage]:
    """Keyword search across slug, title, description, tags, and body."""
    keywords = [w for w in re.split(r"\W+", query.lower()) if w and w not in _STOP_WORDS]
    if not keywords:
        keywords = query.lower().split()
    scored: list[SkillPage] = []
    for page in pages:
        score = _score_keyword(page, keywords)
        if score > 0:
            page.score = score
            scored.append(page)
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
        1
        for sec in ("concepts", "comparisons", "queries")
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
        entity_type=page.entity_type,
        score=round(page.score, 2),
        tags=page.tags,
        status=page.status,
        use_count=page.use_count,
        has_pipeline=page.has_transformed,
        description=page.description,
        excerpt=_excerpt(page),
        wikilink=page.wikilink,
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
        cite = "Based on " + " and ".join(cited[:5])
        if len(cited) > 5:
            cite += f" (and {len(cited) - 5} more)"
        lines.append(f"_{cite}_")
    return "\n".join(lines)


def render_stats_markdown(stats: dict) -> str:
    rows = [
        ("Entity pages", stats["total_entity_pages"]),
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


def _read_wiki_page(wiki: Path, relpath: str) -> str | None:
    packs_dir = wiki / "wiki-packs"
    path = wiki / relpath
    if packs_dir.is_dir():
        pages = load_merged_wiki_pages(packs_dir)
        if relpath in pages:
            return pages[relpath]
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
        return None
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _write_wiki_page(wiki: Path, relpath: str, content: str) -> None:
    packs_dir = wiki / "wiki-packs"
    path = wiki / relpath
    if path.exists() or not packs_dir.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if packs_dir.is_dir():
        write_active_wiki_overlay_pack(
            packs_dir=packs_dir,
            pages={relpath: content},
            tombstones=[],
        )


def _append_log(wiki: Path, action: str, subject: str, details: list[str]) -> None:
    entry = f"\n## [{TODAY}] {action} | {subject}\n" + "".join(f"- {d}\n" for d in details)
    content = _read_wiki_page(wiki, "log.md") or ""
    _write_wiki_page(wiki, "log.md", content + entry)


def _update_index_queries(wiki: Path, slug: str, query: str) -> None:
    content = _read_wiki_page(wiki, "index.md")
    if content is None:
        return
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
    _write_wiki_page(wiki, "index.md", "\n".join(lines))


def save_query_page(wiki: Path, query: str, content: str) -> Path:
    """Write synthesis result to queries/, register in index, and log the action."""
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^\w-]", "-", query.lower().strip()))[:60].strip("-")
    relpath = f"queries/{slug}.md"
    page_path = wiki / relpath
    fm = f'---\ntitle: "{query}"\ncreated: {TODAY}\nupdated: {TODAY}\ntype: query\n---\n\n'
    _write_wiki_page(wiki, relpath, fm + content)
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
    parser.add_argument(
        "--wiki", default=str(cfg.wiki_dir), help=f"Wiki root path (default: {cfg.wiki_dir})"
    )
    parser.add_argument("--query", "-q", help="Keyword query: searches name, tags, and body")
    parser.add_argument("--tag", "-t", help="Filter skills by tag")
    parser.add_argument("--related", "-r", help="Find skills related to a given skill name")
    parser.add_argument("--stats", "-s", action="store_true", help="Show wiki statistics")
    parser.add_argument(
        "--save", action="store_true", help="Save --query results as a new page in queries/"
    )
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
        _append_log(
            wiki,
            "query",
            args.query,
            [
                f"Query: {args.query}",
                f"Results: {len(results)}",
                f"Top match: {results[0].name if results else 'none'}",
            ],
        )
    elif args.tag:
        results = sorted(filter_by_tag(pages, args.tag), key=lambda p: p.use_count, reverse=True)[
            : args.top
        ]
        heading = f'Skills tagged "{args.tag}"'
        _append_log(wiki, "tag-filter", args.tag, [f"Results: {len(results)}"])
    elif args.related:
        results = find_related(pages, args.related, top_n=args.top)
        heading = f"Skills related to [[entities/skills/{args.related}]]"
        _append_log(
            wiki,
            "related",
            args.related,
            [
                f"Related found: {len(results)}",
                f"Top: {results[0].name if results else 'none'}",
            ],
        )

    if not results:
        print(json.dumps({"results": [], "total": 0}) if args.json else "No matching skills found.")
        return

    query_results = [_to_result(r) for r in results]

    if args.json:
        print(
            json.dumps(
                {
                    "query": args.query or args.tag or args.related,
                    "mode": "query" if args.query else ("tag" if args.tag else "related"),
                    "total": len(query_results),
                    "results": [
                        {
                            "name": r.name,
                            "score": r.score,
                            "tags": r.tags,
                            "status": r.status,
                            "use_count": r.use_count,
                            "has_pipeline": r.has_pipeline,
                            "description": r.description,
                            "excerpt": r.excerpt,
                            "wikilink": r.wikilink,
                            "entity_type": r.entity_type,
                        }
                        for r in query_results
                    ],
                },
                indent=2,
            )
        )
        return

    cited = [r.wikilink for r in query_results]
    md_output = render_markdown(query_results, heading, cited)
    print(md_output)

    if args.save and args.query:
        saved_path = save_query_page(wiki, args.query, md_output)
        print(f"\n_Saved to {saved_path}_")
    elif args.save:
        print("\nNote: --save only applies to --query mode.", file=sys.stderr)


if __name__ == "__main__":
    main()
