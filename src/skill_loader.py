#!/usr/bin/env python3
"""
skill_loader.py -- Load a skill or agent into the current session on user request.

Called by Claude when the user approves a suggestion from skill_suggest.py.

Usage:
    python skill_loader.py --name fastapi-pro          # Load a skill
    python skill_loader.py --name architect-review      # Load an agent
    python skill_loader.py --names "fastapi-pro,docker-expert"  # Load multiple
    python skill_loader.py --show-pending               # Show current pending suggestions

Outputs the skill/agent content path so Claude can read and apply it.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from wiki_utils import validate_skill_name


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically via temp file + os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

SKILLS_DIR = Path(os.path.expanduser("~/.claude/skills"))
AGENTS_DIR = Path(os.path.expanduser("~/.claude/agents"))
WIKI_DIR = Path(os.path.expanduser("~/.claude/skill-wiki"))
PENDING_SKILLS = Path(os.path.expanduser("~/.claude/pending-skills.json"))
MANIFEST_PATH = Path(os.path.expanduser("~/.claude/skill-manifest.json"))


def _resolved_under(candidate: Path, base: Path) -> bool:
    """True only if candidate.resolve() stays under base.resolve()."""
    try:
        candidate.resolve(strict=False).relative_to(base.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


def find_skill(name: str) -> dict | None:
    """Find a skill file by name. Returns {type, name, path} or None.

    Hardened against path traversal (CWE-22): ``name`` is validated against an
    allowlist before any filesystem access, and every candidate path must
    resolve inside its intended base directory.
    """
    try:
        validate_skill_name(name)
    except ValueError:
        return None

    skill_path = SKILLS_DIR / name / "SKILL.md"
    if _resolved_under(skill_path, SKILLS_DIR) and skill_path.exists():
        return {"type": "skill", "name": name, "path": str(skill_path)}

    agent_path = AGENTS_DIR / f"{name}.md"
    if _resolved_under(agent_path, AGENTS_DIR) and agent_path.exists():
        return {"type": "agent", "name": name, "path": str(agent_path)}

    # Nested agents: name is validated (no separators/metachars), so the rglob
    # pattern is a plain filename. Each match is still re-checked for containment.
    for md_file in AGENTS_DIR.rglob(f"{name}.md"):
        if _resolved_under(md_file, AGENTS_DIR):
            return {"type": "agent", "name": name, "path": str(md_file)}

    return None


def update_manifest(name: str) -> None:
    """Add skill to the current session manifest so context-monitor knows it's loaded."""
    manifest = {"load": [], "unload": [], "warnings": []}
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    loaded_names = {e["skill"] for e in manifest.get("load", [])}
    if name not in loaded_names:
        manifest["load"].append({"skill": name, "source": "user-approved"})
        _atomic_write_text(MANIFEST_PATH, json.dumps(manifest, indent=2))


def clear_pending(names: list[str]) -> None:
    """Remove loaded skills from pending-skills.json."""
    if not PENDING_SKILLS.exists():
        return
    try:
        pending = json.loads(PENDING_SKILLS.read_text(encoding="utf-8"))
        graph_suggestions = pending.get("graph_suggestions", [])
        pending["graph_suggestions"] = [
            s for s in graph_suggestions if s["name"] not in names
        ]
        unmatched = pending.get("unmatched_signals", [])
        pending["unmatched_signals"] = [s for s in unmatched if s not in names]
        _atomic_write_text(PENDING_SKILLS, json.dumps(pending, indent=2))
    except Exception as exc:
        print(f"Warning: failed to clear pending: {exc}", file=sys.stderr)


def show_pending() -> None:
    """Display current pending suggestions."""
    if not PENDING_SKILLS.exists():
        print("No pending skill suggestions.")
        return
    try:
        pending = json.loads(PENDING_SKILLS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("No pending skill suggestions.")
        return

    unmatched = pending.get("unmatched_signals", [])
    graph = pending.get("graph_suggestions", [])

    if not unmatched and not graph:
        print("No pending skill suggestions.")
        return

    print(f"Pending suggestions (generated: {pending.get('generated_at', '?')}):\n")
    if unmatched:
        print(f"  Unmatched signals: {', '.join(unmatched)}")
    if graph:
        print(f"\n  Graph-suggested skills/agents:")
        for s in graph:
            tags = ", ".join(s.get("matching_tags", []))
            print(f"    - {s['name']} [{s['type']}] score={s.get('score', '?')} ({tags})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load a skill/agent into the current session")
    parser.add_argument("--name", help="Skill or agent name to load")
    parser.add_argument("--names", help="Comma-separated skill/agent names to load")
    parser.add_argument("--show-pending", action="store_true", help="Show pending suggestions")
    args = parser.parse_args()

    if args.show_pending:
        show_pending()
        return

    if not args.name and not args.names:
        parser.print_help()
        sys.exit(1)

    names = []
    if args.name:
        names.append(args.name)
    if args.names:
        names.extend(n.strip() for n in args.names.split(","))

    loaded: list[dict] = []
    not_found: list[str] = []

    for name in names:
        result = find_skill(name)
        if result:
            update_manifest(name)
            loaded.append(result)
            print(f"  Loaded: {result['name']} [{result['type']}] -> {result['path']}")
        else:
            not_found.append(name)
            print(f"  Not found: {name}", file=sys.stderr)

    # Clear loaded ones from pending
    clear_pending([r["name"] for r in loaded])

    # Output JSON summary for Claude to consume
    output = {
        "loaded": loaded,
        "not_found": not_found,
        "instruction": "Read the file at each 'path' to apply the skill/agent to this session.",
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
