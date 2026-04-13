#!/usr/bin/env python3
"""
skill_transformer.py -- Convert skills >180 lines to micro-skills pipeline format.

Micro-skill format (from https://github.com/stevesolun/micro-skills):
  skill-name/
    SKILL.md               (orchestrator, ~30 lines)
    check-gates.md         (binary YES/NO questions)
    failure-log.md         (learned error patterns, starts empty)
    original-hash.txt      (SHA256 of original for staleness detection)
    references/
      01-scope.md          (constraints, preconditions, ~40 lines max)
      02-plan.md           (approach, alternatives)
      03-build.md          (execution steps)
      04-check.md          (validation gates)
      05-deliver.md        (output format, handoff)

Usage:
    # Interactive: scan dir and ASK before converting each
    python skill_transformer.py --scan ~/.claude/skills

    # Convert a specific file (still asks for confirmation)
    python skill_transformer.py --file ~/.claude/skills/my-skill/SKILL.md

    # Non-interactive bulk conversion (for automation)
    python skill_transformer.py --scan ~/.claude/skills --auto

    # Dry run: report what would be converted, no changes
    python skill_transformer.py --scan ~/.claude/skills --dry-run

    # Add extra skill directories (future repos)
    python skill_transformer.py --scan ~/.claude/skills --extra-dirs /path/to/more-skills
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from textwrap import dedent
from typing import NamedTuple

try:
    from ctx_config import cfg as _cfg
    LINE_THRESHOLD: int = _cfg.line_threshold
    MAX_STAGE_LINES: int = _cfg.max_stage_lines
except ImportError:
    LINE_THRESHOLD = 180
    MAX_STAGE_LINES = 40


class StageContent(NamedTuple):
    scope: str
    plan: str
    build: str
    check: str
    deliver: str


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_skills_over_threshold(scan_dir: Path) -> list[tuple[Path, int]]:
    """Find all SKILL.md files over LINE_THRESHOLD lines. Returns (path, line_count)."""
    results = []
    for skill_md in sorted(scan_dir.rglob("SKILL.md")):
        try:
            lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > LINE_THRESHOLD:
                results.append((skill_md, len(lines)))
        except Exception as exc:
            print(f"Warning: failed to read skill file {skill_md}: {exc}", file=sys.stderr)
            continue
    return results


def split_by_headers(lines: list[str]) -> list[list[str]]:
    """Split content by top-level markdown headers (## or #)."""
    sections: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("## ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        sections.append(current)

    return sections if len(sections) >= 2 else []


def split_by_numbered_list(lines: list[str]) -> list[list[str]]:
    """Split on top-level numbered list items (1. 2. 3. etc.)."""
    sections: list[list[str]] = []
    current: list[str] = []
    found_any = False

    for line in lines:
        if re.match(r"^\d+\.\s+\S", line) and current:
            if found_any:
                sections.append(current)
            current = [line]
            found_any = True
        else:
            if not found_any:
                current.append(line)
            else:
                current.append(line)

    if current and found_any:
        sections.append(current)

    return sections if len(sections) >= 3 else []


def split_evenly(lines: list[str], n: int = 5) -> list[list[str]]:
    """Fallback: split evenly into n chunks."""
    chunk_size = max(1, len(lines) // n)
    chunks = []
    for i in range(0, len(lines), chunk_size):
        chunks.append(lines[i:i + chunk_size])
    # Merge excess into last chunk
    while len(chunks) > n:
        chunks[-2].extend(chunks[-1])
        chunks.pop()
    return chunks


def assign_stages(sections: list[list[str]], skill_name: str) -> StageContent:
    """
    Map sections to the 5 stages.
    Stage 1 (scope): first ~20%
    Stage 2 (plan): next ~20%
    Stage 3 (build): middle ~30%
    Stage 4 (check): next ~15%
    Stage 5 (deliver): last ~15%
    """
    n = len(sections)

    # Map sections to 5 buckets proportionally
    def get_section(idx: int) -> str:
        if idx < n:
            return "\n".join(sections[idx])
        return ""

    if n >= 5:
        # Distribute: first, second, middle (possibly multiple merged), fourth, last
        scope = get_section(0)
        plan = get_section(1)
        # Middle sections merged into build
        build_parts = sections[2:n - 2] if n > 4 else [sections[2]]
        build = "\n\n".join("\n".join(s) for s in build_parts)
        check = get_section(n - 2)
        deliver = get_section(n - 1)
    elif n == 4:
        scope, plan, build_check, deliver = (get_section(i) for i in range(4))
        half = len(build_check.splitlines()) // 2
        b_lines = build_check.splitlines()
        build = "\n".join(b_lines[:half])
        check = "\n".join(b_lines[half:])
    elif n == 3:
        scope, build, deliver = (get_section(i) for i in range(3))
        plan = ""
        check = ""
    elif n == 2:
        scope, build = get_section(0), get_section(1)
        plan = check = deliver = ""
    else:
        scope = "\n".join(sections[0]) if sections else ""
        plan = build = check = deliver = ""

    def header(stage: str, content: str) -> str:
        if not content.strip():
            return f"# {stage}\n\n_Auto-split from {skill_name}/SKILL.md. Review and edit._\n"
        return f"# {stage}\n\n{content.strip()}\n"

    return StageContent(
        scope=header(f"Stage 1: Scope — {skill_name}", scope),
        plan=header(f"Stage 2: Plan — {skill_name}", plan),
        build=header(f"Stage 3: Build — {skill_name}", build),
        check=header(f"Stage 4: Check — {skill_name}", check),
        deliver=header(f"Stage 5: Deliver — {skill_name}", deliver),
    )


def extract_frontmatter(content: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). frontmatter_block includes --- delimiters."""
    if not content.startswith("---"):
        return "", content
    end = content.find("---", 3)
    if end < 0:
        return "", content
    fm = content[:end + 3]
    body = content[end + 3:].lstrip("\n")
    return fm, body


def make_orchestrator(skill_name: str, original_lines: int, frontmatter: str) -> str:
    """Generate the micro-skill orchestrator SKILL.md (~30 lines)."""
    # Preserve original frontmatter if present, else generate minimal one
    if frontmatter:
        fm = frontmatter
    else:
        fm = dedent(f"""\
            ---
            name: {skill_name}
            type: micro-skill
            converted_from: SKILL.md.original ({original_lines} lines)
            ---""")

    return dedent(f"""\
        {fm}

        # {skill_name}

        > Converted to micro-skill pipeline. Original had {original_lines} lines.
        > Stages must be followed in order. **No skipping.**

        ## Pipeline

        ```
        01-scope → 02-plan → 03-build → 04-check → 05-deliver
        ```

        ## Stage Files

        1. **[01-scope](references/01-scope.md)** — Constraints, preconditions
        2. **[02-plan](references/02-plan.md)** — Approach, alternatives
        3. **[03-build](references/03-build.md)** — Execution steps
        4. **[04-check](references/04-check.md)** — Validation gates
        5. **[05-deliver](references/05-deliver.md)** — Output format

        See [check-gates.md](check-gates.md) for domain-specific YES/NO checks.
        See [failure-log.md](failure-log.md) for known failure patterns.
        """)


def convert_skill(skill_md: Path, dry_run: bool = False) -> bool:
    """
    Convert a SKILL.md to micro-skill format.
    Returns True on success, False on error.
    """
    skill_dir = skill_md.parent
    skill_name = skill_dir.name
    content = skill_md.read_text(encoding="utf-8", errors="replace")
    original_lines = len(content.splitlines())
    content_hash = sha256(content)

    # Check if already converted
    original_backup = skill_dir / "SKILL.md.original"
    if original_backup.exists():
        print(f"  [skip] {skill_name}: already converted (SKILL.md.original exists)")
        return False

    if dry_run:
        print(f"  [dry-run] Would convert: {skill_name} ({original_lines} lines)")
        return True

    # Parse frontmatter + body
    frontmatter, body = extract_frontmatter(content)
    body_lines = body.splitlines()

    # Try splitting strategies in order
    sections = split_by_headers(body_lines)
    if not sections:
        sections = split_by_numbered_list(body_lines)
    if not sections:
        sections = split_evenly(body_lines, n=5)

    stages = assign_stages(sections, skill_name)

    # Create references/ dir
    refs_dir = skill_dir / "references"
    refs_dir.mkdir(exist_ok=True)

    # Write stage files
    (refs_dir / "01-scope.md").write_text(stages.scope, encoding="utf-8")
    (refs_dir / "02-plan.md").write_text(stages.plan, encoding="utf-8")
    (refs_dir / "03-build.md").write_text(stages.build, encoding="utf-8")
    (refs_dir / "04-check.md").write_text(stages.check, encoding="utf-8")
    (refs_dir / "05-deliver.md").write_text(stages.deliver, encoding="utf-8")

    # Write check-gates.md
    gates_path = skill_dir / "check-gates.md"
    if not gates_path.exists():
        gates_path.write_text(
            f"# Check Gates — {skill_name}\n\n"
            "Review and fill in domain-specific YES/NO questions for Stage 4.\n\n"
            "## Gates\n\n"
            "- [ ] (Add gates here based on the skill's domain)\n",
            encoding="utf-8",
        )

    # Write failure-log.md
    failure_log_path = skill_dir / "failure-log.md"
    if not failure_log_path.exists():
        failure_log_path.write_text(
            f"# Failure Log — {skill_name}\n\n"
            "> Append-only. Review before each run.\n\n"
            "## Log\n\n"
            "<!-- entries appended here -->\n",
            encoding="utf-8",
        )

    # Write original-hash.txt
    (skill_dir / "original-hash.txt").write_text(content_hash, encoding="utf-8")

    # Backup original SKILL.md
    skill_md.rename(original_backup)

    # Write new orchestrator SKILL.md
    orchestrator = make_orchestrator(skill_name, original_lines, frontmatter)
    skill_md.write_text(orchestrator, encoding="utf-8")

    print(f"  [ok] {skill_name}: converted ({original_lines} lines → 8 files, {len(sections)} sections)")
    return True


def ask_user(skill_name: str, lines: int, path: Path) -> bool:
    """Interactively ask the user whether to convert this skill."""
    print(f"\n{'─' * 60}")
    print(f"  Skill: {skill_name}")
    print(f"  Lines: {lines}")
    print(f"  Path:  {path}")
    print(f"  This skill exceeds {LINE_THRESHOLD} lines.")
    try:
        answer = input("  Convert to micro-skills pipeline? [y/N/q(uit)]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)

    if answer == "q":
        print("Quitting.")
        sys.exit(0)
    return answer in ("y", "yes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert large skills to micro-skills pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", metavar="DIR", help="Scan directory for skills >180 lines")
    group.add_argument("--file", metavar="PATH", help="Convert a specific SKILL.md file")

    parser.add_argument("--auto", action="store_true", help="Non-interactive: convert all without asking")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be converted, no changes")
    parser.add_argument("--extra-dirs", nargs="*", default=[], help="Additional skill directories to scan")
    args = parser.parse_args()

    if args.dry_run and args.auto:
        print("Error: --dry-run and --auto are mutually exclusive", file=sys.stderr)
        sys.exit(1)

    targets: list[tuple[Path, int]] = []

    if args.file:
        skill_md = Path(args.file)
        if not skill_md.exists():
            print(f"Error: {skill_md} not found", file=sys.stderr)
            sys.exit(1)
        lines = len(skill_md.read_text(encoding="utf-8", errors="replace").splitlines())
        if lines <= LINE_THRESHOLD:
            print(f"{skill_md.parent.name} has only {lines} lines (≤{LINE_THRESHOLD}). No conversion needed.")
            sys.exit(0)
        targets = [(skill_md, lines)]
    else:
        dirs_to_scan = [Path(args.scan)] + [Path(d) for d in args.extra_dirs]
        for d in dirs_to_scan:
            targets.extend(find_skills_over_threshold(d))
        targets.sort(key=lambda x: -x[1])  # largest first

    if not targets:
        print(f"No skills over {LINE_THRESHOLD} lines found.")
        sys.exit(0)

    print(f"\nFound {len(targets)} skills over {LINE_THRESHOLD} lines:")
    if args.dry_run:
        for path, lines in targets:
            print(f"  {path.parent.name}: {lines} lines")
        print(f"\n[dry-run] {len(targets)} skills would be converted.")
        sys.exit(0)

    converted = 0
    skipped = 0

    for skill_md, lines in targets:
        skill_name = skill_md.parent.name

        if not args.auto:
            if not ask_user(skill_name, lines, skill_md):
                print(f"  [skip] {skill_name}")
                skipped += 1
                continue

        if convert_skill(skill_md, dry_run=False):
            converted += 1
        else:
            skipped += 1

    print(f"\n{'─' * 60}")
    print(f"Done: {converted} converted, {skipped} skipped.")


if __name__ == "__main__":
    main()
