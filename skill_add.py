#!/usr/bin/env python3
"""
skill_add.py -- Add new skills with automatic micro-skill conversion and wiki ingestion.

Usage:
    # Single skill
    python skill_add.py --skill-path /path/to/SKILL.md --name my-skill \
        --wiki ~/.claude/skill-wiki --skills-dir ~/.claude/skills

    # Batch from directory
    python skill_add.py --scan-dir /path/to/new-skills/ \
        --wiki ~/.claude/skill-wiki --skills-dir ~/.claude/skills
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from the same directory as this script
sys.path.insert(0, str(Path(__file__).parent))

from batch_convert import convert_skill  # noqa: E402
from wiki_sync import append_log, ensure_wiki, update_index, upsert_skill_page  # noqa: E402

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Tags the taxonomy recognises (mirrors wiki_sync SCHEMA.md)
_ALL_TAGS = [
    "python", "javascript", "typescript", "rust", "go", "java", "ruby", "swift", "kotlin",
    "react", "vue", "angular", "nextjs", "fastapi", "django", "express", "flask",
    "docker", "kubernetes", "terraform", "ci-cd", "aws", "gcp", "azure",
    "sql", "nosql", "redis", "kafka", "spark", "dbt", "airflow",
    "llm", "agents", "mcp", "langchain", "embeddings", "fine-tuning", "rag",
    "testing", "linting", "typing", "security", "performance",
    "documentation", "api-spec", "markdown", "diagrams",
    "comparison", "decision", "pattern", "troubleshooting",
    "marketplace", "registry", "versioning", "compatibility",
]


# ── Tag inference ─────────────────────────────────────────────────────────────

def infer_tags(name: str, content: str) -> list[str]:
    """Infer taxonomy tags from skill name and file content."""
    combined = f"{name} {content}".lower()
    found = [tag for tag in _ALL_TAGS if re.search(rf"\b{re.escape(tag)}\b", combined)]
    return found if found else ["uncategorized"]


# ── Skills-dir helpers ────────────────────────────────────────────────────────

def install_skill(source: Path, skills_dir: Path, name: str) -> Path:
    """Copy SKILL.md into skills_dir/<name>/SKILL.md. Returns the installed path."""
    dest_dir = skills_dir / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "SKILL.md"
    shutil.copy2(source, dest)
    return dest


# ── Conversion ────────────────────────────────────────────────────────────────

def maybe_convert(
    installed_path: Path,
    name: str,
    converted_root: Path,
    line_count: int,
) -> tuple[bool, Path | None]:
    """Convert skill to micro-skill pipeline if >180 lines.

    Args:
        installed_path: Path to the installed SKILL.md (original, never touched).
        name: Skill name.
        converted_root: ~/.claude/skill-wiki/converted/
        line_count: Pre-computed line count of the source file.

    Returns:
        (was_converted, output_dir | None)
    """
    if line_count <= 180:
        return False, None

    output_dir = converted_root / name
    output_dir.mkdir(parents=True, exist_ok=True)

    # batch_convert.convert_skill operates on the source path and writes to output_dir
    result = convert_skill(installed_path, output_dir)

    if result.get("status") == "converted":
        return True, output_dir

    return False, None


# ── Wiki entity page ──────────────────────────────────────────────────────────

def build_entity_page(
    *,
    name: str,
    tags: list[str],
    line_count: int,
    has_pipeline: bool,
    original_path: Path,
    pipeline_path: Path | None,
    related: list[str],
    scan_sources: list[str],
) -> str:
    """Render the full entity page markdown for a skill."""
    pipeline_path_str = (
        f"converted/{name}/" if has_pipeline else "null"
    )

    frontmatter_lines = [
        "---",
        f"title: {name}",
        f"created: {TODAY}",
        f"updated: {TODAY}",
        "type: skill",
        "status: installed",
        f"tags: [{', '.join(tags)}]",
        "source: local",
        f"original_path: {original_path}",
        f"original_lines: {line_count}",
        f"has_pipeline: {'true' if has_pipeline else 'false'}",
        f"pipeline_path: {pipeline_path_str}",
        "always_load: false",
        "never_load: false",
        f"last_used: {TODAY}",
        "use_count: 0",
        "avg_session_rating: null",
        'notes: ""',
    ]

    if scan_sources:
        frontmatter_lines.append(f"sources: [{', '.join(scan_sources)}]")

    frontmatter_lines.append("---")

    related_links = "\n".join(f"- [[entities/skills/{r}]]" for r in related[:6])
    if not related_links:
        related_links = "<!-- No related skills found yet -->"

    pipeline_note = (
        f"Pipeline converted to `{pipeline_path_str}` (original: {line_count} lines)."
        if has_pipeline
        else f"Skill is {line_count} lines — under the 180-line threshold, no pipeline generated."
    )

    return "\n".join(frontmatter_lines) + f"""

# {name}

## Overview

{pipeline_note}

## Tags

{', '.join(f'`{t}`' for t in tags)}

## Related Skills

{related_links}

## Usage History

| Date | Action | Notes |
|------|--------|-------|
| {TODAY} | Added | Ingested via skill_add.py |
"""


def write_entity_page(wiki_path: Path, name: str, content: str) -> bool:
    """Write entity page. Returns True if newly created."""
    page = wiki_path / "entities" / "skills" / f"{name}.md"
    is_new = not page.exists()
    page.write_text(content, encoding="utf-8")
    return is_new


# ── Wikilink backfill ─────────────────────────────────────────────────────────

def find_related_skills(wiki_path: Path, name: str, tags: list[str]) -> list[str]:
    """Scan existing entity pages for skills that share at least one tag."""
    skills_dir = wiki_path / "entities" / "skills"
    related: list[str] = []
    tag_set = set(tags) - {"uncategorized"}

    for page in sorted(skills_dir.glob("*.md")):
        if page.stem == name:
            continue
        content = page.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^tags:\s*\[([^\]]*)\]", content, re.MULTILINE)
        if not m:
            continue
        page_tags = {t.strip() for t in m.group(1).split(",")}
        if tag_set & page_tags:
            related.append(page.stem)

    return related


def _add_backlink(wiki_path: Path, target_name: str, source_name: str) -> None:
    """Add a [[wikilink]] from target page back to source if not already present."""
    page = wiki_path / "entities" / "skills" / f"{target_name}.md"
    if not page.exists():
        return
    content = page.read_text(encoding="utf-8", errors="replace")
    link = f"[[entities/skills/{source_name}]]"
    if link in content:
        return
    # Append under Related Skills section if present, else end of file
    if "## Related Skills" in content:
        content = content.replace(
            "## Related Skills\n",
            f"## Related Skills\n- {link}\n",
            1,
        )
    else:
        content = content.rstrip() + f"\n\n- {link}\n"
    page.write_text(content, encoding="utf-8")


def wire_backlinks(wiki_path: Path, name: str, related: list[str]) -> None:
    """Bidirectionally add wikilinks between name and each related skill."""
    for target in related:
        _add_backlink(wiki_path, target, name)


# ── Scan-source detection ─────────────────────────────────────────────────────

def detect_scan_sources(wiki_path: Path, name: str) -> list[str]:
    """Return filenames in raw/scans/ that reference this skill name."""
    scans_dir = wiki_path / "raw" / "scans"
    if not scans_dir.exists():
        return []
    sources: list[str] = []
    for scan in sorted(scans_dir.glob("*.json")):
        try:
            text = scan.read_text(encoding="utf-8", errors="replace")
            if name in text:
                sources.append(scan.name)
        except OSError:
            pass
    return sources


# ── Core orchestration ────────────────────────────────────────────────────────

def add_skill(
    *,
    source_path: Path,
    name: str,
    wiki_path: Path,
    skills_dir: Path,
) -> dict:
    """Add a single skill: install, convert if needed, ingest into wiki.

    Returns a result dict with keys: name, installed, converted, is_new_page.
    """
    content = source_path.read_text(encoding="utf-8", errors="replace")
    line_count = len(content.splitlines())
    tags = infer_tags(name, content)

    # 1. Install original into skills-dir (never modified after this)
    installed_path = install_skill(source_path, skills_dir, name)

    # 2. Convert if above threshold
    converted_root = wiki_path / "converted"
    converted, pipeline_path = maybe_convert(installed_path, name, converted_root, line_count)

    # 3. Detect related skills and scan sources (before writing new page)
    related = find_related_skills(wiki_path, name, tags)
    scan_sources = detect_scan_sources(wiki_path, name)

    # Ensure at least 2 wikilinks (pad with first two related even if no tag match)
    all_entity_pages = sorted(
        (p.stem for p in (wiki_path / "entities" / "skills").glob("*.md") if p.stem != name)
    )
    while len(related) < 2 and len(all_entity_pages) > len(related):
        candidate = all_entity_pages[len(related)]
        if candidate not in related:
            related.append(candidate)

    # 4. Write entity page
    page_content = build_entity_page(
        name=name,
        tags=tags,
        line_count=line_count,
        has_pipeline=converted,
        original_path=installed_path,
        pipeline_path=pipeline_path,
        related=related,
        scan_sources=scan_sources,
    )
    is_new = write_entity_page(wiki_path, name, page_content)

    # 5. Bidirectional wikilinks
    wire_backlinks(wiki_path, name, related)

    # 6. Index + log
    if is_new:
        update_index(str(wiki_path), [name])

    log_details = [
        f"Source: {source_path}",
        f"Installed: {installed_path}",
        f"Lines: {line_count}",
        f"Tags: {', '.join(tags)}",
        f"Converted: {converted}",
        f"Related: {', '.join(related) if related else 'none'}",
    ]
    if converted and pipeline_path:
        log_details.append(f"Pipeline: {pipeline_path}")
    append_log(str(wiki_path), "add-skill", name, log_details)

    return {"name": name, "installed": str(installed_path), "converted": converted, "is_new_page": is_new}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Add new skills with wiki ingestion")
    parser.add_argument("--skill-path", help="Path to a single SKILL.md to add")
    parser.add_argument("--name", help="Skill name (required with --skill-path)")
    parser.add_argument("--scan-dir", help="Directory of skills to batch-add (each subdir with SKILL.md)")
    parser.add_argument("--wiki", default=os.path.expanduser("~/.claude/skill-wiki"), help="Wiki path")
    parser.add_argument("--skills-dir", default=os.path.expanduser("~/.claude/skills"), help="Skills install path")
    args = parser.parse_args()

    wiki_path = Path(os.path.expanduser(args.wiki))
    skills_dir = Path(os.path.expanduser(args.skills_dir))

    ensure_wiki(str(wiki_path))
    skills_dir.mkdir(parents=True, exist_ok=True)

    if args.skill_path and args.scan_dir:
        print("Error: use --skill-path or --scan-dir, not both.", file=sys.stderr)
        sys.exit(1)

    if not args.skill_path and not args.scan_dir:
        print("Error: --skill-path or --scan-dir is required.", file=sys.stderr)
        sys.exit(1)

    # Build the list of (source_path, name) pairs to process
    candidates: list[tuple[Path, str]] = []

    if args.skill_path:
        if not args.name:
            print("Error: --name is required with --skill-path.", file=sys.stderr)
            sys.exit(1)
        source = Path(os.path.expanduser(args.skill_path))
        if not source.exists():
            print(f"Error: {source} does not exist.", file=sys.stderr)
            sys.exit(1)
        candidates.append((source, args.name))

    if args.scan_dir:
        scan_root = Path(os.path.expanduser(args.scan_dir))
        if not scan_root.exists():
            print(f"Error: {scan_root} does not exist.", file=sys.stderr)
            sys.exit(1)
        for skill_md in sorted(scan_root.rglob("SKILL.md")):
            skill_name = skill_md.parent.name
            candidates.append((skill_md, skill_name))

        if not candidates:
            print(f"No SKILL.md files found under {scan_root}.", file=sys.stderr)
            sys.exit(0)

    added = converted = errors = 0
    for source_path, name in candidates:
        try:
            result = add_skill(
                source_path=source_path,
                name=name,
                wiki_path=wiki_path,
                skills_dir=skills_dir,
            )
            added += 1
            if result["converted"]:
                converted += 1
            status = "converted" if result["converted"] else "installed"
            print(f"  [{status}] {name} -> {result['installed']}")
        except Exception as exc:
            errors += 1
            print(f"  ERROR: {name}: {exc}", file=sys.stderr)

    print(f"\nDone: {added} added, {converted} converted to pipeline, {errors} errors")


if __name__ == "__main__":
    main()
