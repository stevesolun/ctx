#!/usr/bin/env python3
"""
flatten_agents.py -- Promote every nested agent to a top-level sibling.

Claude Code's /agents Library tab auto-discovers ONLY top-level .md files in
~/.claude/agents/. Agents living in category subdirs (design/, engineering/,
game-development/, etc.) are invisible to the Library — there are 169 such
orphans across 15 folders.

This script walks ~/.claude/agents/ and for every nested .md file with YAML
frontmatter containing a `name:` field (i.e. a real agent, not a reference
note), it writes a sibling copy at the top level. The original nested file is
left untouched so existing references still resolve.

Safety:
- Skips files with no frontmatter (reference notes, includes).
- Skips if a sibling with the same basename already exists (no collision risk
  — verified at time of writing, but re-checked here).
- Dry-run by default; pass --apply to actually write.

Usage:
  python src/flatten_agents.py              # dry run, print plan
  python src/flatten_agents.py --apply      # perform the flatten
  python src/flatten_agents.py --apply -v   # verbose
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

AGENTS_DIR = Path.home() / ".claude" / "agents"


def has_name_frontmatter(path: Path) -> bool:
    """Return True if the file opens with YAML frontmatter containing `name:`."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            first = fh.readline().rstrip("\r\n")
            if first != "---":
                return False
            for line in fh:
                stripped = line.rstrip("\r\n")
                if stripped == "---":
                    return False
                if stripped.startswith("name:"):
                    return True
        return False
    except OSError:
        return False


def plan_flatten(agents_dir: Path) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Return (copy_plan, warnings). copy_plan is list of (src, dst) pairs."""
    copy_plan: list[tuple[Path, Path]] = []
    warnings: list[str] = []

    if not agents_dir.exists():
        warnings.append(f"agents_dir does not exist: {agents_dir}")
        return copy_plan, warnings

    for md in agents_dir.rglob("*.md"):
        # Top-level files are already discoverable.
        if md.parent == agents_dir:
            continue
        if not has_name_frontmatter(md):
            continue
        dst = agents_dir / md.name
        if dst.exists():
            # Don't clobber — flag so humans can resolve.
            if dst.read_bytes() != md.read_bytes():
                warnings.append(f"collision (different content): {md} -> {dst}")
            continue
        copy_plan.append((md, dst))

    return copy_plan, warnings


def apply_plan(plan: list[tuple[Path, Path]], verbose: bool) -> int:
    copied = 0
    for src, dst in plan:
        try:
            shutil.copy2(src, dst)
            copied += 1
            if verbose:
                print(f"  copied {src.relative_to(AGENTS_DIR)} -> {dst.name}")
        except OSError as e:
            print(f"  FAILED {src}: {e}", file=sys.stderr)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually copy files (default: dry run)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print each action")
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=AGENTS_DIR,
        help="override agents dir (default: ~/.claude/agents)",
    )
    args = parser.parse_args()

    plan, warnings = plan_flatten(args.agents_dir)

    print(f"flatten_agents: {len(plan)} files to promote, {len(warnings)} warnings")
    for w in warnings:
        print(f"  warn: {w}", file=sys.stderr)

    if not args.apply:
        if args.verbose:
            for src, dst in plan:
                print(f"  would copy {src.relative_to(args.agents_dir)} -> {dst.name}")
        print("dry run — pass --apply to perform the flatten")
        sys.exit(0)

    copied = apply_plan(plan, args.verbose)
    print(f"done: {copied} agents promoted to top-level siblings")
    sys.exit(0 if copied == len(plan) else 1)


if __name__ == "__main__":
    main()
