#!/usr/bin/env python3
"""
versions_catalog.py -- Build the dual-version sub-catalog in the wiki.

Scans skill directories for transformed skills (directories with both
SKILL.md.original and SKILL.md). Creates/updates:
  - wiki/versions-catalog.md  (sub-catalog of dual-version skills)
  - wiki/entities/skills/<name>.md  (adds version metadata to entity pages)

Called after skill-transformer runs.

Usage:
    python versions_catalog.py \
      --wiki ~/.claude/skill-wiki \
      --skills-dir ~/.claude/skills
"""

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def find_dual_version_skills(skills_dir: Path) -> list[dict]:
    """Find skills that have both original and transformed SKILL.md."""
    results = []
    if not skills_dir.exists():
        return results

    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        skill_md = d / "SKILL.md"
        original_md = d / "SKILL.md.original"
        if skill_md.exists() and original_md.exists():
            try:
                transformed_lines = len(skill_md.read_text(encoding="utf-8", errors="replace").splitlines())
                original_lines = len(original_md.read_text(encoding="utf-8", errors="replace").splitlines())
            except Exception:
                transformed_lines = original_lines = 0

            results.append({
                "name": d.name,
                "transformed_path": str(skill_md),
                "original_path": str(original_md),
                "transformed_lines": transformed_lines,
                "original_lines": original_lines,
            })

    return results


def build_versions_catalog(wiki_dir: Path, dual_version_skills: list[dict]) -> str:
    """Write versions-catalog.md and return its path."""
    catalog_path = wiki_dir / "versions-catalog.md"

    lines = [
        "# Skill Versions Catalog",
        "",
        f"> Dual-version skills: both original and micro-skills-transformed versions available.",
        f"> Default: **transformed** (micro-skills pipeline). Users can switch per-skill.",
        f"> Last updated: {TODAY} | Total dual-version skills: {len(dual_version_skills)}",
        "",
        "## How to Change Version Preference",
        "",
        "Edit the skill's wiki entity page (`entities/skills/<name>.md`) and set:",
        "```yaml",
        "preferred_version: original   # or: transformed",
        "```",
        "The skill router reads this field and loads the appropriate SKILL.md.",
        "",
        "## Dual-Version Skills",
        "",
        "| Skill | Original Lines | Transformed Lines | Default | Original | Transformed |",
        "|-------|---------------|-------------------|---------|----------|-------------|",
    ]

    for skill in dual_version_skills:
        lines.append(
            f"| {skill['name']} "
            f"| {skill['original_lines']} "
            f"| {skill['transformed_lines']} "
            f"| transformed "
            f"| `{skill['original_path']}` "
            f"| `{skill['transformed_path']}` |"
        )

    catalog_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(catalog_path)


def upsert_entity_page_versions(wiki_dir: Path, skill: dict) -> None:
    """Add/update version metadata in the skill's wiki entity page."""
    page_path = wiki_dir / "entities" / "skills" / f"{skill['name']}.md"
    if not page_path.exists():
        # Create minimal entity page if missing
        page_path.write_text(
            f"---\n"
            f"title: {skill['name']}\n"
            f"created: {TODAY}\n"
            f"updated: {TODAY}\n"
            f"type: skill\n"
            f"status: installed\n"
            f"tags: []\n"
            f"has_original: true\n"
            f"has_transformed: true\n"
            f"preferred_version: transformed\n"
            f"original_path: {skill['original_path']}\n"
            f"transformed_path: {skill['transformed_path']}\n"
            f"original_lines: {skill['original_lines']}\n"
            f"use_count: 0\n"
            f"session_count: 0\n"
            f"last_used: {TODAY}\n"
            f"---\n\n"
            f"# {skill['name']}\n\n"
            f"Dual-version skill. Default: transformed (micro-skills pipeline).\n",
            encoding="utf-8",
        )
        return

    content = page_path.read_text(encoding="utf-8")

    # Add version fields if missing
    def ensure_field(text: str, field: str, value: str) -> str:
        if f"{field}:" in text:
            return re.sub(rf"^{field}:.*$", f"{field}: {value}", text, flags=re.MULTILINE)
        # Insert before closing ---
        end = text.find("---", 3)
        if end > 0:
            return text[:end] + f"{field}: {value}\n" + text[end:]
        return text + f"\n{field}: {value}\n"

    content = ensure_field(content, "has_original", "true")
    content = ensure_field(content, "has_transformed", "true")
    content = ensure_field(content, "preferred_version", "transformed")
    content = ensure_field(content, "original_path", skill["original_path"])
    content = ensure_field(content, "transformed_path", skill["transformed_path"])
    content = ensure_field(content, "original_lines", str(skill["original_lines"]))
    content = re.sub(r"^updated:.*$", f"updated: {TODAY}", content, flags=re.MULTILINE)

    page_path.write_text(content, encoding="utf-8")


def update_wiki_index(wiki_dir: Path, count: int) -> None:
    """Add versions-catalog reference to index.md."""
    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        return

    content = index_path.read_text(encoding="utf-8")
    ref = "- [[versions-catalog]] - Dual-version skills (original + micro-skills pipeline)"

    if "[[versions-catalog]]" not in content:
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if line.strip() == "## Skills":
                lines.insert(i + 1, ref)
                break
        content = "\n".join(lines)
        index_path.write_text(content, encoding="utf-8")


def append_log(wiki_dir: Path, count: int, catalog_path: str) -> None:
    log_path = wiki_dir / "log.md"
    if not log_path.exists():
        return
    entry = (
        f"\n## [{TODAY}] versions-catalog | dual-version-skills\n"
        f"- Dual-version skills found: {count}\n"
        f"- Versions catalog: {catalog_path}\n"
        f"- Default preference: transformed\n"
    )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dual-version skill sub-catalog")
    parser.add_argument("--wiki", default=os.path.expanduser("~/.claude/skill-wiki"))
    parser.add_argument("--skills-dir", default=os.path.expanduser("~/.claude/skills"))
    parser.add_argument("--agents-dir", default=os.path.expanduser("~/.claude/agents"))
    parser.add_argument("--extra-dirs", nargs="*", default=[])
    args = parser.parse_args()

    wiki_dir = Path(args.wiki)
    all_dirs = [Path(args.skills_dir), Path(args.agents_dir)] + [Path(d) for d in args.extra_dirs]

    dual_skills: list[dict] = []
    for d in all_dirs:
        dual_skills.extend(find_dual_version_skills(d))

    if not dual_skills:
        print("No dual-version skills found.")
        return

    catalog_path = build_versions_catalog(wiki_dir, dual_skills)

    entities_dir = wiki_dir / "entities" / "skills"
    entities_dir.mkdir(parents=True, exist_ok=True)
    for skill in dual_skills:
        upsert_entity_page_versions(wiki_dir, skill)

    update_wiki_index(wiki_dir, len(dual_skills))
    append_log(wiki_dir, len(dual_skills), catalog_path)

    print(f"Versions catalog: {len(dual_skills)} dual-version skills")
    print(f"Written to: {catalog_path}")


if __name__ == "__main__":
    main()
