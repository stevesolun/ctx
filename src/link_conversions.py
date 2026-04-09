#!/usr/bin/env python3
"""
link_conversions.py -- Link converted micro-skill pipelines to wiki entity pages.

Usage:
    python link_conversions.py \
      --wiki ~/.claude/skill-wiki \
      --skills-dir ~/.claude/skills

Scans ~/.claude/skill-wiki/converted/ for all converted skill directories and:
  1. Updates existing entity pages with pipeline frontmatter fields
  2. Creates new entity pages for skills without one
  3. Updates index.md with any new skill entries
  4. Appends a summary entry to log.md
  5. Generates converted-index.md listing all converted skills
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ctx_config import cfg  # noqa: E402

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConvertedSkill:
    """Represents a single converted micro-skill pipeline directory."""

    name: str
    pipeline_path: str  # relative to wiki root, e.g. "converted/007/"
    abs_dir: Path


@dataclass
class ProcessResult:
    """Aggregate result of a full run."""

    updated: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

_FM_PATTERN = re.compile(r"^---\r?\n(.*?\r?\n)---\r?\n", re.DOTALL)
_FIELD_PATTERN_TMPL = r"^{key}:\s*(.+)$"


def _find_field(content: str, key: str) -> str:
    """Extract a frontmatter field value from page content."""
    match = re.search(_FIELD_PATTERN_TMPL.format(key=re.escape(key)), content, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _set_field(content: str, key: str, value: str) -> str:
    """Set or add a frontmatter field. Adds before the closing --- if not present."""
    pattern = re.compile(_FIELD_PATTERN_TMPL.format(key=re.escape(key)), re.MULTILINE)
    replacement = f"{key}: {value}"

    if pattern.search(content):
        return pattern.sub(replacement, content)

    # Field not present -- insert before closing ---
    fm_match = _FM_PATTERN.match(content)
    if fm_match:
        end = fm_match.end() - len("---\n")
        # Normalise CRLF
        insert_pos = content.index("---\n", fm_match.start() + 4)
        return content[:insert_pos] + f"{replacement}\n" + content[insert_pos:]

    # No frontmatter at all -- prepend minimal block
    return f"---\n{replacement}\n---\n\n" + content


def _inject_pipeline_fields(content: str, pipeline_path: str) -> str:
    """Add/update the three pipeline fields in frontmatter."""
    content = _set_field(content, "has_pipeline", "true")
    content = _set_field(content, "pipeline_path", pipeline_path)
    content = _set_field(content, "pipeline_converted", TODAY)
    return content


# ---------------------------------------------------------------------------
# Skill directory scanning
# ---------------------------------------------------------------------------


def scan_converted(wiki: Path) -> list[ConvertedSkill]:
    """Return sorted list of all converted skill directories."""
    converted_root = wiki / "converted"
    if not converted_root.is_dir():
        return []

    skills: list[ConvertedSkill] = []
    for entry in sorted(converted_root.iterdir()):
        if entry.is_dir():
            skills.append(
                ConvertedSkill(
                    name=entry.name,
                    pipeline_path=f"converted/{entry.name}/",
                    abs_dir=entry,
                )
            )
    return skills


# ---------------------------------------------------------------------------
# Entity page helpers
# ---------------------------------------------------------------------------


def _infer_tags(name: str) -> list[str]:
    """Infer rough tags from a skill name."""
    tag_keywords = [
        "python", "javascript", "typescript", "react", "docker",
        "fastapi", "django", "langchain", "mcp", "testing", "rust",
        "go", "java", "ruby", "swift", "sql", "redis", "kafka",
        "security", "llm", "agents", "api",
    ]
    lowered = name.lower().replace("-", " ").replace("_", " ")
    found = [t for t in tag_keywords if t in lowered]
    return found if found else ["uncategorized"]


def _read_pipeline_description(converted_skill: ConvertedSkill) -> str:
    """Read description from the pipeline's SKILL.md frontmatter if available."""
    skill_md = converted_skill.abs_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    raw = skill_md.read_text(encoding="utf-8", errors="replace")
    match = re.search(r'^description:\s*"?(.+?)"?\s*$', raw, re.MULTILINE)
    return match.group(1).strip().strip('"') if match else ""


def _build_new_entity_page(skill: ConvertedSkill, skills_dir: Path) -> str:
    """Build full content for a brand-new entity page."""
    tags = _infer_tags(skill.name)
    description = _read_pipeline_description(skill)

    # Original skill path (best-effort; may not exist)
    original_path = skills_dir / skill.name / "SKILL.md"
    original_note = (
        f"Original skill file: `{original_path}`"
        if original_path.exists()
        else "Original skill file not found in skills directory."
    )

    overview = description if description else f"Micro-skill pipeline for **{skill.name}**."

    return (
        f"---\n"
        f"title: {skill.name}\n"
        f"created: {TODAY}\n"
        f"updated: {TODAY}\n"
        f"type: skill\n"
        f"status: installed\n"
        f"tags: [{', '.join(tags)}]\n"
        f"source: local\n"
        f"has_pipeline: true\n"
        f"pipeline_path: {skill.pipeline_path}\n"
        f"pipeline_converted: {TODAY}\n"
        f"use_count: 0\n"
        f"session_count: 0\n"
        f"last_used: {TODAY}\n"
        f"---\n"
        f"\n"
        f"# {skill.name}\n"
        f"\n"
        f"## Overview\n"
        f"{overview}\n"
        f"\n"
        f"## Versions\n"
        f"This skill has both an original version and a converted micro-skill pipeline.\n"
        f"\n"
        f"- Pipeline (micro-skills): [[converted/{skill.name}/SKILL.md]]\n"
        f"- {original_note}\n"
        f"\n"
        f"## Pipeline\n"
        f"The pipeline version was converted from a monolithic SKILL.md (>180 lines) into\n"
        f"a gated multi-step pipeline stored under `{skill.pipeline_path}`.\n"
        f"\n"
        f"See [[converted/{skill.name}/SKILL.md]] for the pipeline entry point.\n"
        f"\n"
        f"## Related Skills\n"
        f"<!-- Add [[wikilinks]] to related skills -->\n"
        f"\n"
        f"## Usage History\n"
        f"| Date | Action | Notes |\n"
        f"|------|--------|-------|\n"
        f"| {TODAY} | pipeline-linked | Created by link_conversions.py |\n"
    )


def upsert_entity_page(
    wiki: Path,
    skill: ConvertedSkill,
    skills_dir: Path,
) -> bool:
    """Create or update a skill entity page. Returns True if a new page was created."""
    page_path = wiki / "entities" / "skills" / f"{skill.name}.md"
    is_new = not page_path.exists()

    if is_new:
        content = _build_new_entity_page(skill, skills_dir)
    else:
        content = page_path.read_text(encoding="utf-8", errors="replace")
        content = _inject_pipeline_fields(content, skill.pipeline_path)
        # Bump updated date
        old_updated = _find_field(content, "updated")
        if old_updated and old_updated != TODAY:
            content = re.sub(
                r"^updated:\s*.+$",
                f"updated: {TODAY}",
                content,
                flags=re.MULTILINE,
            )

    page_path.write_text(content, encoding="utf-8")
    return is_new


# ---------------------------------------------------------------------------
# index.md
# ---------------------------------------------------------------------------


def update_index(wiki: Path, new_skills: list[str]) -> None:
    """Add new skill entries to the Skills section of index.md."""
    if not new_skills:
        return

    index_path = wiki / "index.md"
    content = index_path.read_text(encoding="utf-8", errors="replace")
    lines = content.split("\n")

    # Locate the ## Skills insertion point
    insert_idx: int | None = None
    next_section_idx: int | None = None
    in_skills = False
    for i, line in enumerate(lines):
        if line.strip() == "## Skills":
            insert_idx = i + 1
            in_skills = True
        elif in_skills and line.startswith("## "):
            next_section_idx = i
            break

    if insert_idx is None:
        insert_idx = len(lines)

    # Build set of lines already in index
    existing_content = "\n".join(lines)

    added = 0
    for skill_name in sorted(new_skills):
        entry = f"- [[entities/skills/{skill_name}]] - Converted micro-skill pipeline"
        if entry not in existing_content:
            lines.insert(insert_idx, entry)
            insert_idx += 1
            added += 1

    if added == 0:
        return

    # Update header metadata
    skill_count = sum(1 for ln in lines if "[[entities/skills/" in ln)
    for i, line in enumerate(lines):
        if "Total pages:" in line:
            lines[i] = re.sub(r"Total pages: \d+", f"Total pages: {skill_count}", line)
            lines[i] = re.sub(r"Last updated: [\d-]+", f"Last updated: {TODAY}", lines[i])
            break

    index_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# log.md
# ---------------------------------------------------------------------------


def append_log(wiki: Path, action: str, subject: str, details: list[str]) -> None:
    """Append a structured entry to log.md."""
    log_path = wiki / "log.md"
    lines = [f"\n## [{TODAY}] {action} | {subject}"]
    lines.extend(f"- {d}" for d in details)
    entry = "\n".join(lines) + "\n"

    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(entry)


# ---------------------------------------------------------------------------
# converted-index.md
# ---------------------------------------------------------------------------


def generate_converted_index(wiki: Path, skills: list[ConvertedSkill]) -> None:
    """Generate converted-index.md listing every converted skill."""
    out_path = wiki / "converted-index.md"

    header = (
        f"# Converted Micro-Skill Pipelines Index\n"
        f"\n"
        f"> All skills that were converted from monolithic SKILL.md files (>180 lines)\n"
        f"> into gated micro-skill pipelines.\n"
        f">\n"
        f"> Generated: {TODAY} | Total: {len(skills)}\n"
        f"\n"
        f"| Skill | Entity Page | Pipeline Entry |\n"
        f"|-------|-------------|----------------|\n"
    )

    rows: list[str] = []
    for skill in skills:
        entity_link = f"[[entities/skills/{skill.name}]]"
        pipeline_link = f"[[{skill.pipeline_path}SKILL.md]]"
        rows.append(f"| {skill.name} | {entity_link} | {pipeline_link} |")

    content = header + "\n".join(rows) + "\n"
    out_path.write_text(content, encoding="utf-8")
    print(f"  converted-index.md written ({len(skills)} entries)")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run(wiki: Path, skills_dir: Path) -> ProcessResult:
    """Main processing loop."""
    result = ProcessResult()

    if not wiki.is_dir():
        result.errors.append(f"Wiki directory not found: {wiki}")
        return result

    entities_dir = wiki / "entities" / "skills"
    entities_dir.mkdir(parents=True, exist_ok=True)

    converted_skills = scan_converted(wiki)
    if not converted_skills:
        print("No converted skill directories found.")
        return result

    print(f"Found {len(converted_skills)} converted skills.")

    for skill in converted_skills:
        try:
            is_new = upsert_entity_page(wiki, skill, skills_dir)
            if is_new:
                result.created.append(skill.name)
            else:
                result.updated.append(skill.name)
        except Exception as exc:  # noqa: BLE001
            msg = f"{skill.name}: {exc}"
            result.errors.append(msg)
            print(f"  ERROR {msg}", file=sys.stderr)

    # Update index with newly created pages only
    update_index(wiki, result.created)

    # Generate converted-index.md for all converted skills
    generate_converted_index(wiki, converted_skills)

    # Append to log
    details = [
        f"Converted skills found: {len(converted_skills)}",
        f"Entity pages updated: {len(result.updated)}",
        f"Entity pages created: {len(result.created)}",
        f"Errors: {len(result.errors)}",
        f"converted-index.md: {wiki / 'converted-index.md'}",
    ]
    if result.created:
        details.append(f"New pages: {', '.join(sorted(result.created))}")
    if result.errors:
        details.append(f"Error details: {'; '.join(result.errors[:10])}")

    append_log(wiki, "link-conversions", "converted-pipelines", details)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Link converted micro-skill pipelines to wiki entity pages."
    )
    parser.add_argument(
        "--wiki",
        default=str(cfg.wiki_dir),
        help=f"Path to the skill wiki root (default: {cfg.wiki_dir})",
    )
    parser.add_argument(
        "--skills-dir",
        default=str(cfg.skills_dir),
        help=f"Path to the skills directory (default: {cfg.skills_dir})",
    )
    args = parser.parse_args()

    wiki = Path(args.wiki).expanduser().resolve()
    skills_dir = Path(args.skills_dir).expanduser().resolve()

    print(f"Wiki:       {wiki}")
    print(f"Skills dir: {skills_dir}")

    result = run(wiki, skills_dir)

    print(
        f"\nDone. Created: {len(result.created)}  "
        f"Updated: {len(result.updated)}  "
        f"Errors: {len(result.errors)}"
    )

    if result.errors:
        print("\nErrors encountered:", file=sys.stderr)
        for err in result.errors:
            print(f"  {err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
