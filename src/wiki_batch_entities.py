#!/usr/bin/env python3
"""
wiki_batch_entities.py -- Batch-generate entity pages for all skills and agents.

Usage:
    python wiki_batch_entities.py --skills       # Generate missing skill pages
    python wiki_batch_entities.py --agents       # Generate missing agent pages
    python wiki_batch_entities.py --all          # Both
    python wiki_batch_entities.py --dry-run      # Preview without writing
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

SKILLS_DIR = Path(os.path.expanduser("~/.claude/skills"))
AGENTS_DIR = Path(os.path.expanduser("~/.claude/agents"))
WIKI_DIR = Path(os.path.expanduser("~/.claude/skill-wiki"))
SKILL_ENTITIES = WIKI_DIR / "entities" / "skills"
AGENT_ENTITIES = WIKI_DIR / "entities" / "agents"

# Tag inference from name and content
TAG_KEYWORDS: dict[str, list[str]] = {
    "python": ["python", "django", "fastapi", "flask", "pytest", "pip", "pydantic"],
    "typescript": ["typescript", "ts", "nextjs", "angular", "vue", "react"],
    "javascript": ["javascript", "js", "nodejs", "express", "npm", "bun", "deno"],
    "rust": ["rust", "cargo", "tokio", "axum"],
    "go": ["golang", "go-", "-go"],
    "java": ["java", "spring", "maven", "gradle", "kotlin"],
    "csharp": ["csharp", "dotnet", ".net", "aspnet", "blazor"],
    "ruby": ["ruby", "rails"],
    "swift": ["swift", "swiftui", "ios"],
    "php": ["php", "laravel", "symfony"],
    "docker": ["docker", "container", "dockerfile"],
    "kubernetes": ["k8s", "kubernetes", "helm", "istio"],
    "terraform": ["terraform", "terragrunt", "iac"],
    "aws": ["aws", "lambda", "s3", "dynamo", "cloudformation"],
    "azure": ["azure", "entra"],
    "gcp": ["gcp", "google-cloud"],
    "database": ["sql", "postgres", "mysql", "mongo", "redis", "database", "db"],
    "ai": ["ai", "llm", "ml", "agent", "rag", "embedding", "prompt"],
    "testing": ["test", "tdd", "e2e", "jest", "pytest", "playwright"],
    "security": ["security", "pentest", "vuln", "auth", "owasp", "cve"],
    "devops": ["ci", "cd", "deploy", "devops", "pipeline", "github-actions"],
    "frontend": ["frontend", "ui", "ux", "css", "tailwind", "design"],
    "api": ["api", "rest", "graphql", "grpc", "openapi"],
    "docs": ["doc", "readme", "wiki", "technical-writ"],
    "automation": ["automat", "workflow", "n8n", "zapier"],
}


def infer_tags(name: str, content: str = "") -> list[str]:
    """Infer tags from skill/agent name and content."""
    tags: list[str] = []
    combined = f"{name} {content}".lower()
    for tag, keywords in TAG_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            tags.append(tag)
    return tags if tags else ["uncategorized"]


def extract_description(content: str) -> str:
    """Extract description from YAML frontmatter or first paragraph."""
    # Try YAML frontmatter description field
    match = re.search(r'^description:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
    if match:
        desc = match.group(1).strip().rstrip('"').rstrip("'")
        # Truncate long descriptions
        if len(desc) > 300:
            desc = desc[:297] + "..."
        return desc

    # Try first non-heading, non-empty line after frontmatter
    in_frontmatter = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if stripped and not stripped.startswith("#") and not stripped.startswith("!"):
            if len(stripped) > 300:
                stripped = stripped[:297] + "..."
            return stripped

    return "No description available."


def extract_name_field(content: str) -> str:
    """Extract name from YAML frontmatter."""
    match = re.search(r'^name:\s*(.+)$', content, re.MULTILINE)
    return match.group(1).strip().strip('"').strip("'") if match else ""


def count_lines(filepath: Path) -> int:
    """Count lines in a file."""
    try:
        return len(filepath.read_text(encoding="utf-8", errors="replace").split("\n"))
    except Exception as exc:
        print(f"Warning: failed to count lines in {filepath}: {exc}", file=sys.stderr)
        return 0


def generate_skill_page(skill_name: str) -> str:
    """Generate entity page content for a skill."""
    skill_dir = SKILLS_DIR / skill_name
    skill_file = skill_dir / "SKILL.md"
    original_file = skill_dir / "SKILL.md.original"

    has_original = original_file.exists()
    has_transformed = skill_file.exists() and has_original
    has_pipeline = (WIKI_DIR / "converted" / skill_name).exists()

    # Read content for metadata extraction
    content = ""
    if skill_file.exists():
        try:
            content = skill_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"Warning: failed to read skill file {skill_file}: {exc}", file=sys.stderr)
            content = ""

    original_content = ""
    if original_file.exists():
        try:
            original_content = original_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"Warning: failed to read original skill file {original_file}: {exc}", file=sys.stderr)
            original_content = ""

    # Use original content for description if available (it's the unmodified version)
    desc_source = original_content if original_content else content
    description = extract_description(desc_source)
    tags = infer_tags(skill_name, desc_source[:2000])

    original_lines = count_lines(original_file) if has_original else count_lines(skill_file)

    # Determine version info
    if has_transformed:
        preferred = "transformed"
        version_note = "Dual-version skill. Default: transformed (micro-skills pipeline)."
    elif has_pipeline:
        preferred = "pipeline"
        version_note = "Has micro-skills pipeline conversion."
    else:
        preferred = "original"
        version_note = "Original skill (not yet converted to micro-skills pipeline)."

    # Build paths
    orig_path = str(original_file) if has_original else str(skill_file)
    trans_path = str(skill_file) if has_transformed else ""

    page = f"""---
title: {skill_name}
created: {TODAY}
updated: {TODAY}
type: skill
status: installed
tags: [{', '.join(tags)}]
has_original: {'true' if has_original else 'false'}
has_transformed: {'true' if has_transformed else 'false'}
preferred_version: {preferred}
original_path: {orig_path}
transformed_path: {trans_path}
original_lines: {original_lines}
use_count: 0
session_count: 0
last_used: {TODAY}
has_pipeline: {'true' if has_pipeline else 'false'}
pipeline_path: {'converted/' + skill_name + '/' if has_pipeline else ''}
pipeline_converted: {TODAY if has_pipeline else ''}
---

# {skill_name}

{version_note}

## Description

{description}

## Related Skills
"""
    return page


def generate_agent_page(agent_name: str, agent_file: Path | None = None) -> str:
    """Generate entity page content for an agent."""
    if agent_file is None:
        agent_file = AGENTS_DIR / f"{agent_name}.md"

    content = ""
    if agent_file.exists():
        try:
            content = agent_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"Warning: failed to read agent file {agent_file}: {exc}", file=sys.stderr)
            content = ""

    description = extract_description(content)
    display_name = extract_name_field(content) or agent_name
    tags = infer_tags(agent_name, content[:2000])
    line_count = count_lines(agent_file)

    # Extract model field
    model_match = re.search(r'^model:\s*(.+)$', content, re.MULTILINE)
    model = model_match.group(1).strip() if model_match else "inherit"

    page = f"""---
title: {display_name}
created: {TODAY}
updated: {TODAY}
type: agent
status: installed
tags: [{', '.join(tags)}]
source_path: {agent_file}
lines: {line_count}
model: {model}
use_count: 0
session_count: 0
last_used: {TODAY}
---

# {display_name}

Agent definition file.

## Description

{description}

## Configuration

- **Model**: {model}
- **Lines**: {line_count}

## Related Agents
"""
    return page


def generate_missing_skills(dry_run: bool = False) -> int:
    """Generate entity pages for all skills missing from the wiki."""
    SKILL_ENTITIES.mkdir(parents=True, exist_ok=True)

    installed = {d.name for d in SKILLS_DIR.iterdir() if d.is_dir()}
    existing = {p.stem for p in SKILL_ENTITIES.glob("*.md")}
    missing = sorted(installed - existing)

    if not missing:
        print("All skills already have entity pages.")
        return 0

    print(f"Generating {len(missing)} missing skill entity pages...")
    created = 0
    for name in missing:
        if dry_run:
            print(f"  [DRY RUN] Would create: {name}.md")
        else:
            page_content = generate_skill_page(name)
            (SKILL_ENTITIES / f"{name}.md").write_text(page_content, encoding="utf-8")
            created += 1
            if created % 100 == 0:
                print(f"  ... {created}/{len(missing)} created")

    print(f"Skills: {created} pages created ({len(missing)} were missing)")
    return created


def generate_missing_agents(dry_run: bool = False) -> int:
    """Generate entity pages for all agents missing from the wiki."""
    AGENT_ENTITIES.mkdir(parents=True, exist_ok=True)

    # Collect all agent .md files: top-level AND nested in subdirectories
    agent_files: dict[str, Path] = {}
    for p in AGENTS_DIR.glob("*.md"):
        agent_files[p.stem] = p
    for p in AGENTS_DIR.rglob("*.md"):
        if p.parent != AGENTS_DIR:
            agent_files[p.stem] = p

    installed = set(agent_files.keys())
    existing = {p.stem for p in AGENT_ENTITIES.glob("*.md")}
    missing = sorted(installed - existing)

    if not missing:
        print("All agents already have entity pages.")
        return 0

    print(f"Generating {len(missing)} missing agent entity pages...")
    created = 0
    for name in missing:
        if dry_run:
            print(f"  [DRY RUN] Would create: {name}.md")
        else:
            page_content = generate_agent_page(name, agent_files[name])
            (AGENT_ENTITIES / f"{name}.md").write_text(page_content, encoding="utf-8")
            created += 1
            if created % 50 == 0:
                print(f"  ... {created}/{len(missing)} created")

    print(f"Agents: {created} pages created ({len(missing)} were missing)")
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-generate wiki entity pages")
    parser.add_argument("--skills", action="store_true", help="Generate missing skill pages")
    parser.add_argument("--agents", action="store_true", help="Generate missing agent pages")
    parser.add_argument("--all", action="store_true", help="Generate both skills and agents")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    if not (args.skills or args.agents or args.all):
        parser.print_help()
        sys.exit(1)

    total = 0
    if args.skills or args.all:
        total += generate_missing_skills(args.dry_run)
    if args.agents or args.all:
        total += generate_missing_agents(args.dry_run)

    print(f"\nTotal: {total} entity pages generated")


if __name__ == "__main__":
    main()
