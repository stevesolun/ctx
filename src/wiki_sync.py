#!/usr/bin/env python3
"""
wiki_sync.py -- Sync scan results and manifest into the skill wiki.

Usage:
    python wiki_sync.py \
      --profile /tmp/stack-profile.json \
      --manifest /tmp/skill-manifest.json \
      --wiki ~/skill-wiki

Creates the wiki if it doesn't exist. Updates entity pages, index, and log.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import SAFE_NAME_RE, get_field as _find_field  # noqa: E402

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_wiki(wiki_path: str) -> None:
    """Initialize wiki structure if it doesn't exist."""
    wiki = Path(wiki_path)

    dirs = [
        wiki,
        wiki / "raw" / "scans",
        wiki / "raw" / "marketplace-dumps",
        wiki / "entities" / "skills",
        wiki / "entities" / "plugins",
        wiki / "entities" / "mcp-servers",
        wiki / "concepts",
        wiki / "comparisons",
        wiki / "queries",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # SCHEMA.md
    schema_path = wiki / "SCHEMA.md"
    if not schema_path.exists():
        schema_path.write_text(f"""# Skill Wiki Schema

## Domain
Catalog and management of all available skills, plugins, MCP servers, and
marketplace sources for the agent development environment.

## Conventions
- File names: lowercase, hyphens, no spaces
- Every page starts with YAML frontmatter
- Use [[wikilinks]] between pages (min 2 outbound per page)
- Bump `updated` on every change
- Every new page goes in index.md
- Every action appends to log.md

## Tag Taxonomy
- Stack: python, javascript, typescript, rust, go, java, ruby, swift, kotlin
- Framework: react, vue, angular, nextjs, fastapi, django, express, flask
- Infra: docker, kubernetes, terraform, ci-cd, aws, gcp, azure
- Data: sql, nosql, redis, kafka, spark, dbt, airflow
- AI: llm, agents, mcp, langchain, embeddings, fine-tuning, rag
- Quality: testing, linting, typing, security, performance
- Docs: documentation, api-spec, markdown, diagrams
- Meta: comparison, decision, pattern, troubleshooting
- Management: marketplace, registry, versioning, compatibility

## Page Thresholds
- Create a page when a skill/plugin/MCP server is discovered
- Update when used, configured, or when a new version is found
- Archive when deprecated or superseded

## Update Policy
- New info conflicting with existing: note both claims with dates
- Mark contradictions in frontmatter
- Flag for user review in lint report

Created: {TODAY}
""", encoding="utf-8")

    # index.md
    index_path = wiki / "index.md"
    if not index_path.exists():
        index_path.write_text(f"""# Skill Wiki Index

> Content catalog. Every wiki page listed under its type with a one-line summary.
> Last updated: {TODAY} | Total pages: 0

## Skills

## Plugins

## MCP Servers

## Concepts

## Comparisons

## Queries
""", encoding="utf-8")

    # log.md
    log_path = wiki / "log.md"
    if not log_path.exists():
        log_path.write_text(f"""# Skill Wiki Log

> Chronological record of all wiki actions. Append-only.
> Format: `## [YYYY-MM-DD] action | subject`

## [{TODAY}] create | Wiki initialized
- Domain: Skills, plugins, and MCP server catalog
- Structure created with SCHEMA.md, index.md, log.md
""", encoding="utf-8")


def save_scan(wiki_path: str, profile: dict) -> str:
    """Save scan result to raw/scans/."""
    repo_name = Path(profile["repo_path"]).name
    filename = f"scan-{TODAY}-{repo_name}.json"
    scan_path = Path(wiki_path) / "raw" / "scans" / filename

    with open(scan_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)

    return str(scan_path)


def _sanitize_yaml_value(value: str) -> str:
    """Sanitize a string for safe inclusion in unquoted YAML frontmatter.

    Strips newlines (prevents key injection) and replaces leading
    colons/hashes (prevents YAML comment/structure ambiguity).
    Does NOT add surrounding quotes so values round-trip cleanly
    through parse_frontmatter/get_field.
    """
    sanitized = str(value).replace("\r", "").replace("\n", " ").strip()
    # Prevent YAML structural characters at start of value
    if sanitized.startswith(":"):
        sanitized = sanitized.lstrip(":")
    if sanitized.startswith("#"):
        sanitized = sanitized.lstrip("#")
    return sanitized.strip()


def upsert_skill_page(wiki_path: str, skill_name: str, skill_info: dict) -> bool:
    """Create or update a skill entity page. Returns True if created new."""
    if not SAFE_NAME_RE.match(skill_name):
        raise ValueError(f"Invalid skill name: {skill_name!r}")
    page_path = Path(wiki_path) / "entities" / "skills" / f"{skill_name}.md"
    is_new = not page_path.exists()

    if is_new:
        # Infer tags from reason
        tags = []
        reason = skill_info.get("reason", "").lower()
        for tag in ["python", "javascript", "typescript", "react", "docker",
                     "fastapi", "django", "langchain", "mcp", "testing"]:
            if tag in reason or tag in skill_name:
                tags.append(tag)
        if not tags:
            tags = ["uncategorized"]

        safe_path = _sanitize_yaml_value(skill_info.get('path', 'unknown'))
        safe_reason = _sanitize_yaml_value(skill_info.get('reason', 'Unknown'))
        safe_repo = _sanitize_yaml_value(skill_info.get('repo', 'unknown'))

        content = f"""---
title: {skill_name}
created: {TODAY}
updated: {TODAY}
type: skill
status: installed
tags: [{', '.join(tags)}]
source: local
path: {safe_path}
stacks: [{', '.join(tags)}]
always_load: false
never_load: false
last_used: {TODAY}
use_count: 1
avg_session_rating: null
notes: ""
---

# {skill_name}

## Overview
Detected and loaded by skill-router.

## Detection Reason
{safe_reason}

## Priority Score
{skill_info.get('priority', 0)}

## Related Skills
<!-- Add [[wikilinks]] to related skills -->

## Usage History
| Date | Repo | Outcome |
|------|------|---------|
| {TODAY} | {safe_repo} | Loaded by router |
"""
        page_path.write_text(content, encoding="utf-8")
    else:
        # Update existing page: bump updated date and use_count
        content = page_path.read_text(encoding="utf-8")
        content = re.sub(
            r"^updated: .+$", f"updated: {TODAY}",
            content, count=1, flags=re.MULTILINE,
        )
        # Increment use_count
        old_count = _find_field(content, "use_count")
        if old_count:
            try:
                new_count = int(old_count) + 1
                content = re.sub(
                    r"^use_count: .+$", f"use_count: {new_count}",
                    content, count=1, flags=re.MULTILINE,
                )
            except ValueError:
                pass

        content = re.sub(
            r"^last_used: .+$", f"last_used: {TODAY}",
            content, count=1, flags=re.MULTILINE,
        )
        page_path.write_text(content, encoding="utf-8")

    return is_new



def update_index(wiki_path: str, new_skills: list[str]) -> None:
    """Add new skill entries to index.md."""
    if not new_skills:
        return

    index_path = Path(wiki_path) / "index.md"
    content = index_path.read_text(encoding="utf-8")

    # Find the ## Skills section and append
    lines = content.split("\n")
    insert_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## Skills":
            insert_idx = i + 1
        elif insert_idx and line.startswith("## "):
            # Found next section
            break

    if insert_idx is None:
        insert_idx = len(lines)

    for skill in sorted(new_skills):
        entry = f"- [[entities/skills/{skill}]] - Auto-discovered by skill-router"
        # Check not already present
        if entry not in content:
            lines.insert(insert_idx, entry)
            insert_idx += 1

    # Update total count
    import re
    skill_count = sum(1 for l in lines if "[[entities/skills/" in l)
    for i, line in enumerate(lines):
        if "Total pages:" in line:
            lines[i] = re.sub(r"Total pages: \d+", f"Total pages: {skill_count}", line)
            lines[i] = re.sub(r"Last updated: [\d-]+", f"Last updated: {TODAY}", lines[i])
            break

    index_path.write_text("\n".join(lines), encoding="utf-8")


def append_log(wiki_path: str, action: str, subject: str, details: list[str]) -> None:
    """Append an entry to log.md."""
    log_path = Path(wiki_path) / "log.md"
    entry = f"\n## [{TODAY}] {action} | {subject}\n"
    for detail in details:
        entry += f"- {detail}\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


def upsert_usage(wiki_path: str, skill_name: str, session_date: str, used: bool) -> None:
    """Update use_count and session_count for a skill page. Called by usage-tracker."""
    page_path = Path(wiki_path) / "entities" / "skills" / f"{skill_name}.md"
    if not page_path.exists():
        return
    content = page_path.read_text(encoding="utf-8")

    # session_count
    old_session = _find_field(content, "session_count")
    if old_session:
        try:
            content = content.replace(
                f"session_count: {old_session}",
                f"session_count: {int(old_session) + 1}",
            )
        except ValueError:
            pass
    else:
        # Add field after use_count if missing
        content = re.sub(r"(use_count: \d+)", r"\1\nsession_count: 1", content, count=1)

    if used:
        old_count = _find_field(content, "use_count")
        if old_count:
            try:
                content = re.sub(
                    r"^use_count: .+$", f"use_count: {int(old_count) + 1}",
                    content, count=1, flags=re.MULTILINE,
                )
            except ValueError:
                pass
        content = re.sub(
            r"^last_used: .+$", f"last_used: {session_date}",
            content, count=1, flags=re.MULTILINE,
        )

    page_path.write_text(content, encoding="utf-8")


def mark_stale(wiki_path: str, skill_name: str) -> None:
    """Mark a skill entity page as stale."""
    page_path = Path(wiki_path) / "entities" / "skills" / f"{skill_name}.md"
    if not page_path.exists():
        return
    content = page_path.read_text(encoding="utf-8")
    old_status = _find_field(content, "status")
    if old_status:
        content = content.replace(f"status: {old_status}", "status: stale")
    page_path.write_text(content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Sync scan results into skill wiki")
    parser.add_argument("--init", action="store_true", help="Initialize wiki structure only (no profile/manifest needed)")
    parser.add_argument("--profile", help="Path to stack-profile.json")
    parser.add_argument("--manifest", help="Path to skill-manifest.json")
    parser.add_argument("--wiki", default=os.path.expanduser("~/.claude/skill-wiki"), help="Wiki path")
    args = parser.parse_args()

    # --init mode: just create wiki structure
    if args.init:
        ensure_wiki(args.wiki)
        print(f"Wiki initialized at {args.wiki}")
        return

    if not args.profile or not args.manifest:
        print("Error: --profile and --manifest required (or use --init)", file=sys.stderr)
        sys.exit(1)

    with open(args.profile) as f:
        profile = json.load(f)
    with open(args.manifest) as f:
        manifest = json.load(f)

    # Ensure wiki exists
    ensure_wiki(args.wiki)

    # Save raw scan
    scan_file = save_scan(args.wiki, profile)

    # Upsert skill pages
    new_skills = []
    for skill_entry in manifest["load"]:
        skill_name = skill_entry["skill"]
        skill_info = {**skill_entry, "repo": Path(profile["repo_path"]).name}
        is_new = upsert_skill_page(args.wiki, skill_name, skill_info)
        if is_new:
            new_skills.append(skill_name)

    # Update index
    update_index(args.wiki, new_skills)

    # Log
    repo_name = Path(profile["repo_path"]).name
    details = [
        f"Repo: {profile['repo_path']}",
        f"Type: {profile.get('project_type', 'unknown')}",
        f"Skills loaded: {len(manifest['load'])}",
        f"Skills unloaded: {len(manifest['unload'])}",
        f"New wiki pages: {len(new_skills)}",
        f"Warnings: {len(manifest.get('warnings', []))}",
        f"Scan saved: {scan_file}",
    ]
    if new_skills:
        details.append(f"New pages: {', '.join(new_skills)}")

    append_log(args.wiki, "scan", repo_name, details)

    print(f"Wiki synced: {len(new_skills)} new pages, {len(manifest['load'])} skills tracked")


if __name__ == "__main__":
    main()
