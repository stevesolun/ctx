#!/usr/bin/env python3
"""
usage_tracker.py -- Stop hook handler: sync session usage into the skill wiki.

Called by Claude Code Stop hook:
    python usage_tracker.py --sync

Reads ~/.claude/intent-log.jsonl (today's tool signals) and
~/.claude/skill-manifest.json (what was loaded this session), then:
  - Correlates signals → skills to determine which skills were "used"
  - Increments use_count / session_count on wiki entity pages
  - Marks skills as 'stale' if unseen for >30 sessions
  - Truncates intent-log to keep last 5 session-days
  - Appends summary to wiki log.md
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import SAFE_NAME_RE, parse_frontmatter as _read_frontmatter  # noqa: E402

try:
    from ctx_config import cfg as _cfg
    CLAUDE_DIR = _cfg.claude_dir
    INTENT_LOG = _cfg.intent_log
    MANIFEST_PATH = _cfg.skill_manifest
    WIKI_DIR = _cfg.wiki_dir
    STALE_THRESHOLD = _cfg.stale_threshold_sessions
    KEEP_DAYS = _cfg.keep_log_days
except ImportError:
    CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))
    INTENT_LOG = CLAUDE_DIR / "intent-log.jsonl"
    MANIFEST_PATH = CLAUDE_DIR / "skill-manifest.json"
    WIKI_DIR = CLAUDE_DIR / "skill-wiki"
    STALE_THRESHOLD = 30
    KEEP_DAYS = 5

ENTITIES_DIR = WIKI_DIR / "entities" / "skills"
LOG_PATH = WIKI_DIR / "log.md"

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Minimal signal→skill correlation (mirrors STACK_SKILL_MAP logic)
SIGNAL_SKILL_MAP: dict[str, list[str]] = {
    "docker": ["docker"],
    "kubernetes": ["kubernetes"],
    "terraform": ["terraform"],
    "react": ["react", "frontend-design"],
    "vue": ["vue", "frontend-design"],
    "angular": ["angular", "frontend-design"],
    "nextjs": ["nextjs", "react", "frontend-design"],
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "langchain": ["langchain"],
    "pytorch": ["pytorch"],
    "anthropic-sdk": ["anthropic-sdk"],
    "openai-sdk": ["openai-sdk"],
    "mcp": ["mcp-dev"],
    "pytest": ["pytest"],
    "jest": ["jest"],
    "playwright": ["playwright"],
    "prisma": ["prisma"],
    "sqlalchemy": ["sqlalchemy"],
}

# (STALE_THRESHOLD and KEEP_DAYS loaded from ctx_config above)


def read_today_signals() -> dict[str, int]:
    """Read today's entries from intent-log and count signal occurrences."""
    signal_counts: dict[str, int] = {}
    if not INTENT_LOG.exists():
        return signal_counts

    try:
        with open(INTENT_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("date", "") == TODAY:
                        for sig in entry.get("signals", []):
                            signal_counts[sig] = signal_counts.get(sig, 0) + 1
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        print(f"Warning: failed to read today's signals: {exc}", file=sys.stderr)

    return signal_counts


def signals_to_skills(signal_counts: dict[str, int]) -> set[str]:
    """Map signal names to skill names via SIGNAL_SKILL_MAP."""
    skills: set[str] = set()
    for signal in signal_counts:
        for skill in SIGNAL_SKILL_MAP.get(signal, [signal]):
            skills.add(skill)
    return skills


def read_loaded_skills() -> list[str]:
    """Return list of skill names in current manifest load list."""
    if not MANIFEST_PATH.exists():
        return []
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            manifest = json.load(f)
        return [entry["skill"] for entry in manifest.get("load", [])]
    except Exception as exc:
        print(f"Warning: failed to read loaded skills: {exc}", file=sys.stderr)
        return []



def _set_frontmatter_field(content: str, field: str, value: str) -> str:
    """Replace a frontmatter field value."""
    return re.sub(
        rf"^({field}:\s*)(.+)$",
        lambda m: f"{m.group(1)}{value}",
        content,
        flags=re.MULTILINE,
    )


PENDING_UNLOAD = CLAUDE_DIR / "pending-unload.json"


def _queue_unload_suggestion(skill_name: str, session_count: int, use_count: int) -> None:
    """Add a skill to the pending-unload list for user approval."""
    pending: dict = {"suggestions": [], "generated_at": ""}
    if PENDING_UNLOAD.exists():
        try:
            pending = json.loads(PENDING_UNLOAD.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pending = {"suggestions": [], "generated_at": ""}

    existing_names = {s["name"] for s in pending.get("suggestions", [])}
    if skill_name not in existing_names:
        pending.setdefault("suggestions", []).append({
            "name": skill_name,
            "reason": f"Loaded {session_count} sessions, used {use_count} times",
            "session_count": session_count,
            "use_count": use_count,
        })
    pending["generated_at"] = datetime.now(timezone.utc).isoformat()
    PENDING_UNLOAD.write_text(json.dumps(pending, indent=2), encoding="utf-8")


def update_skill_page(skill_name: str, used: bool, session_count_bump: bool = True) -> bool:
    """
    Update wiki entity page for a skill.
    Returns True if page existed and was updated.
    """
    if not SAFE_NAME_RE.match(skill_name):
        print(f"Warning: skipping invalid skill name: {skill_name!r}", file=sys.stderr)
        return False
    page_path = ENTITIES_DIR / f"{skill_name}.md"
    if not page_path.exists():
        return False

    try:
        content = page_path.read_text(encoding="utf-8")
        meta = _read_frontmatter(content)

        # Bump session_count
        if session_count_bump:
            session_count = int(str(meta.get("session_count", "0"))) + 1
            content = _set_frontmatter_field(content, "session_count", str(session_count))

        if used:
            use_count = int(str(meta.get("use_count", "0"))) + 1
            content = _set_frontmatter_field(content, "use_count", str(use_count))
            content = _set_frontmatter_field(content, "last_used", TODAY)
            content = _set_frontmatter_field(content, "updated", TODAY)
            # Reset stale status if skill was used
            content = _set_frontmatter_field(content, "status", "installed")
        else:
            # Check if stale threshold reached — don't mark stale directly,
            # write to pending-unload.json so the user can approve
            session_count = int(str(meta.get("session_count", "0")))
            use_count = int(str(meta.get("use_count", "0")))
            if session_count >= STALE_THRESHOLD and use_count == 0:
                _queue_unload_suggestion(skill_name, session_count, use_count)

        page_path.write_text(content, encoding="utf-8")
        return True
    except Exception as exc:
        print(f"Warning: failed to update skill page {skill_name}: {exc}", file=sys.stderr)
        return False


def append_wiki_log(loaded_count: int, used_skills: set[str], stale_count: int) -> None:
    """Append session summary to wiki log.md."""
    if not LOG_PATH.exists():
        return

    entry = (
        f"\n## [{TODAY}] session-end | usage-sync\n"
        f"- Skills loaded: {loaded_count}\n"
        f"- Skills actively used (signals): {len(used_skills)}\n"
        f"- Skills marked stale this session: {stale_count}\n"
    )
    if used_skills:
        entry += f"- Used: {', '.join(sorted(used_skills))}\n"

    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(entry)


def truncate_intent_log() -> None:
    """Keep only the last KEEP_DAYS distinct dates in intent-log.jsonl."""
    if not INTENT_LOG.exists():
        return

    lines_by_date: dict[str, list[str]] = {}
    try:
        with open(INTENT_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    date = entry.get("date", "unknown")
                    lines_by_date.setdefault(date, []).append(line)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return

    # Keep last KEEP_DAYS dates
    sorted_dates = sorted(lines_by_date.keys())[-KEEP_DAYS:]
    kept_lines = [
        line
        for date in sorted_dates
        for line in lines_by_date[date]
    ]

    try:
        with open(INTENT_LOG, "w", encoding="utf-8") as f:
            f.writelines(kept_lines)
    except Exception as exc:
        print(f"Warning: failed to truncate intent log: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stop hook: sync usage into skill wiki")
    parser.add_argument("--sync", action="store_true", help="Sync usage data to wiki")
    parser.add_argument("--wiki", default=str(WIKI_DIR), help="Wiki directory path")
    args = parser.parse_args()

    if not args.sync:
        print("usage-tracker: pass --sync to run", file=sys.stderr)
        sys.exit(1)

    # Check wiki exists
    wiki_dir = Path(args.wiki)
    if not wiki_dir.exists():
        # Wiki not initialized yet — silently exit (install.sh sets it up)
        sys.exit(0)

    signal_counts = read_today_signals()
    used_skills = signals_to_skills(signal_counts)
    loaded_skills = read_loaded_skills()

    stale_count = 0
    updated_count = 0

    for skill_name in loaded_skills:
        skill_used = skill_name in used_skills
        if update_skill_page(skill_name, used=skill_used):
            updated_count += 1
            if not skill_used:
                # Check if it got marked stale
                page_path = Path(args.wiki) / "entities" / "skills" / f"{skill_name}.md"
                if page_path.exists():
                    content = page_path.read_text(encoding="utf-8")
                    if "status: stale" in content:
                        stale_count += 1

    append_wiki_log(len(loaded_skills), used_skills, stale_count)
    truncate_intent_log()

    print(
        f"usage-tracker: {updated_count} pages updated, "
        f"{len(used_skills)} skills used, {stale_count} marked stale"
    )


if __name__ == "__main__":
    main()
