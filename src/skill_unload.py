#!/usr/bin/env python3
"""
skill_unload.py -- Unload skills/agents from the current session or permanently suppress them.

Usage:
    python skill_unload.py --name fastapi-pro              # Unload from current session
    python skill_unload.py --name fastapi-pro --permanent   # Set never_load: true in wiki
    python skill_unload.py --names "fastapi-pro,docker-expert"
    python skill_unload.py --stale                          # Unload all stale skills
    python skill_unload.py --list-loaded                    # Show currently loaded skills
    python skill_unload.py --list-never                     # Show permanently suppressed skills
    python skill_unload.py --restore fastapi-pro            # Remove never_load flag
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))
MANIFEST_PATH = CLAUDE_DIR / "skill-manifest.json"
PENDING_UNLOAD = CLAUDE_DIR / "pending-unload.json"
WIKI_DIR = CLAUDE_DIR / "skill-wiki"
SKILL_ENTITIES = WIKI_DIR / "entities" / "skills"
AGENT_ENTITIES = WIKI_DIR / "entities" / "agents"


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"load": [], "unload": [], "warnings": []}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def set_frontmatter_field(filepath: Path, field: str, value: str) -> bool:
    """Set a YAML frontmatter field in a wiki entity page. Returns True if changed."""
    if not filepath.exists():
        return False
    content = filepath.read_text(encoding="utf-8", errors="replace")
    pattern = rf"^{field}:\s*.+$"
    replacement = f"{field}: {value}"
    new_content, count = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if count == 0:
        # Field doesn't exist — add it after the last frontmatter field
        new_content = re.sub(r"(---\n)", rf"\1{field}: {value}\n", content, count=1)
    if new_content != content:
        filepath.write_text(new_content, encoding="utf-8")
        return True
    return False


def find_entity_page(name: str) -> Path | None:
    """Find entity page for a skill or agent by name."""
    skill_page = SKILL_ENTITIES / f"{name}.md"
    if skill_page.exists():
        return skill_page
    agent_page = AGENT_ENTITIES / f"{name}.md"
    if agent_page.exists():
        return agent_page
    return None


def clear_pending_unload(names: list[str]) -> None:
    """Remove unloaded skills from pending-unload.json."""
    if not PENDING_UNLOAD.exists():
        return
    try:
        data = json.loads(PENDING_UNLOAD.read_text(encoding="utf-8"))
        data["suggestions"] = [s for s in data.get("suggestions", []) if s["name"] not in names]
        PENDING_UNLOAD.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def unload_from_session(names: list[str]) -> list[str]:
    """Remove skills/agents from the current session manifest."""
    manifest = load_manifest()
    removed: list[str] = []
    remaining = []
    for entry in manifest.get("load", []):
        if entry["skill"] in names:
            removed.append(entry["skill"])
            manifest.setdefault("unload", []).append(entry)
        else:
            remaining.append(entry)
    manifest["load"] = remaining
    save_manifest(manifest)
    return removed


def set_never_load(names: list[str]) -> list[str]:
    """Set never_load: true in wiki entity pages."""
    updated: list[str] = []
    for name in names:
        page = find_entity_page(name)
        if page and set_frontmatter_field(page, "never_load", "true"):
            updated.append(name)
            print(f"  {name}: never_load set to true")
        elif page:
            print(f"  {name}: already set to never_load")
        else:
            print(f"  {name}: entity page not found", file=sys.stderr)
    return updated


def restore_load(names: list[str]) -> list[str]:
    """Remove never_load flag from wiki entity pages."""
    restored: list[str] = []
    for name in names:
        page = find_entity_page(name)
        if page and set_frontmatter_field(page, "never_load", "false"):
            restored.append(name)
            print(f"  {name}: never_load removed, skill can be recommended again")
        elif page:
            print(f"  {name}: was not suppressed")
        else:
            print(f"  {name}: entity page not found", file=sys.stderr)
    return restored


def get_stale_skills() -> list[str]:
    """Find all skills with status: stale in their entity pages."""
    stale: list[str] = []
    for entity_dir in [SKILL_ENTITIES, AGENT_ENTITIES]:
        if not entity_dir.exists():
            continue
        for page in entity_dir.glob("*.md"):
            content = page.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^status:\s*stale", content, re.MULTILINE):
                stale.append(page.stem)
    return stale


def list_loaded() -> None:
    """Show currently loaded skills/agents."""
    manifest = load_manifest()
    loaded = manifest.get("load", [])
    if not loaded:
        print("No skills/agents currently loaded in this session.")
        return
    print(f"Currently loaded ({len(loaded)}):\n")
    for entry in loaded:
        source = entry.get("source", "unknown")
        print(f"  - {entry['skill']}  (source: {source})")


def list_never_load() -> None:
    """Show permanently suppressed skills/agents."""
    suppressed: list[str] = []
    for entity_dir in [SKILL_ENTITIES, AGENT_ENTITIES]:
        if not entity_dir.exists():
            continue
        for page in entity_dir.glob("*.md"):
            content = page.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^never_load:\s*true", content, re.MULTILINE):
                suppressed.append(page.stem)
    if not suppressed:
        print("No skills/agents are permanently suppressed.")
        return
    print(f"Permanently suppressed ({len(suppressed)}):\n")
    for name in sorted(suppressed):
        print(f"  - {name}")
    print(f"\nTo restore: python src/skill_unload.py --restore <name>")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unload skills/agents from session or suppress permanently")
    parser.add_argument("--name", help="Skill or agent name to unload")
    parser.add_argument("--names", help="Comma-separated names to unload")
    parser.add_argument("--permanent", action="store_true", help="Set never_load: true (won't be recommended again)")
    parser.add_argument("--stale", action="store_true", help="Unload all stale skills")
    parser.add_argument("--restore", help="Remove never_load flag from a skill/agent")
    parser.add_argument("--list-loaded", action="store_true", help="Show currently loaded skills")
    parser.add_argument("--list-never", action="store_true", help="Show permanently suppressed skills")
    args = parser.parse_args()

    if args.list_loaded:
        list_loaded()
        return

    if args.list_never:
        list_never_load()
        return

    if args.restore:
        names = [n.strip() for n in args.restore.split(",")]
        restore_load(names)
        return

    names: list[str] = []
    if args.name:
        names.append(args.name)
    if args.names:
        names.extend(n.strip() for n in args.names.split(","))
    if args.stale:
        stale = get_stale_skills()
        print(f"Found {len(stale)} stale skills")
        names.extend(stale)

    if not names:
        parser.print_help()
        sys.exit(1)

    # Unload from current session manifest
    removed = unload_from_session(names)
    if removed:
        print(f"Unloaded from session: {', '.join(removed)}")

    # Mark as stale in wiki (so they drop in priority next session)
    not_removed = [n for n in names if n not in removed]
    if not_removed:
        for name in not_removed:
            page = find_entity_page(name)
            if page:
                set_frontmatter_field(page, "status", "stale")
                print(f"  {name}: marked stale (lower priority next session)")

    # Always clear from pending-unload
    clear_pending_unload(names)

    # Permanently suppress if requested
    if args.permanent:
        print("Setting never_load: true (will not be recommended again):")
        set_never_load(names)


if __name__ == "__main__":
    main()
