#!/usr/bin/env python3
"""
skill_add_detector.py -- PostToolUse hook: detect when a new skill is written to a skill dir.

When Claude writes a SKILL.md file (via Write/Edit tools), this detects it and
registers the new skill in the wiki catalog and index automatically.

Called by PostToolUse hook:
    python skill_add_detector.py --tool <name> --input <json>

If >180 lines: prints a prompt asking the user if they want to convert.
(The message appears in Claude's console output — Claude will see it and can relay to user.)
"""

import json
import os
import re
import sys
from pathlib import Path

try:
    from ctx_config import cfg as _cfg
    CLAUDE_DIR = _cfg.claude_dir
    WIKI_DIR = _cfg.wiki_dir
    REGISTRY_PATH = _cfg.skill_registry
    CATALOG_PATH = _cfg.catalog
    LINE_THRESHOLD = _cfg.line_threshold
except ImportError:
    CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))
    WIKI_DIR = CLAUDE_DIR / "skill-wiki"
    REGISTRY_PATH = CLAUDE_DIR / "skill-registry.json"
    CATALOG_PATH = WIKI_DIR / "catalog.md"
    LINE_THRESHOLD = 180

SKILL_TRIGGERS = {"Write", "Edit"}  # Tools that could create a skill file


def load_registry() -> list[str]:
    """Load registered skill directories from skill-registry.json."""
    if not REGISTRY_PATH.exists():
        return [str(CLAUDE_DIR / "skills"), str(CLAUDE_DIR / "agents")]
    try:
        return json.loads(REGISTRY_PATH.read_text())["skill_dirs"]
    except Exception:
        return []


def extract_written_path(tool_name: str, tool_input: dict) -> str | None:
    """Extract the file path being written from tool input."""
    if tool_name == "Write":
        return tool_input.get("file_path")
    elif tool_name == "Edit":
        return tool_input.get("file_path")
    return None


def is_in_skill_dir(file_path: str, skill_dirs: list[str]) -> bool:
    """Check if a file path is inside one of the registered skill directories."""
    p = Path(file_path).resolve()
    for d in skill_dirs:
        try:
            p.relative_to(Path(d).resolve())
            return True
        except ValueError:
            continue
    return False


def count_lines(file_path: str) -> int:
    """Count lines in a file safely."""
    try:
        return len(Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception:
        return 0


def register_skill_in_catalog(file_path: str, skill_name: str, lines: int) -> None:
    """Append a new skill entry to catalog.md."""
    if not CATALOG_PATH.exists():
        return
    content = CATALOG_PATH.read_text(encoding="utf-8")
    # Check if already in catalog
    if skill_name in content:
        return
    over_flag = "⚠" if lines > 180 else ""
    entry = f"| {skill_name} | skill | {lines} | {over_flag} | `{file_path}` |"
    # Insert before the last line
    lines_list = content.splitlines()
    lines_list.append(entry)
    CATALOG_PATH.write_text("\n".join(lines_list) + "\n", encoding="utf-8")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", default="unknown")
    parser.add_argument("--input", default="{}")
    args = parser.parse_args()

    if args.tool not in SKILL_TRIGGERS:
        sys.exit(0)

    try:
        tool_input = json.loads(args.input)
    except json.JSONDecodeError:
        sys.exit(0)

    file_path = extract_written_path(args.tool, tool_input)
    if not file_path:
        sys.exit(0)

    # Only care about SKILL.md files
    if not file_path.endswith("SKILL.md"):
        sys.exit(0)

    skill_dirs = load_registry()
    if not is_in_skill_dir(file_path, skill_dirs):
        sys.exit(0)

    # New skill detected in a skill directory
    skill_name = Path(file_path).parent.name
    lines = count_lines(file_path)

    # Register in catalog
    register_skill_in_catalog(file_path, skill_name, lines)

    # If over 180 lines, emit a notice (Claude will see this in hook output)
    if lines > LINE_THRESHOLD:
        print(
            f"\n[skill-system] New skill '{skill_name}' has {lines} lines (>{180}).\n"
            f"  Consider converting to micro-skills pipeline (>={LINE_THRESHOLD} lines):\n"
            f"  python {Path(__file__).parent}/skill-transformer.py --file \"{file_path}\"\n"
        )


if __name__ == "__main__":
    main()
