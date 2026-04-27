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

from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text
from ctx.core.wiki.wiki_utils import SAFE_NAME_RE, parse_frontmatter as _read_frontmatter

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

def _today() -> str:
    """Today's date in UTC, computed fresh per call.

    Prior impl used a module-level ``TODAY`` constant computed at import
    time. If the process was long-running (test runner, persistent
    daemon, hook server) and crossed midnight, every downstream date
    comparison — ``entry.get("date") == TODAY``, ``last_used: TODAY``,
    log headers — kept using yesterday's date. That made staleness
    decisions (session_count >= STALE_THRESHOLD AND use_count == 0)
    fire on the wrong calendar day. Code-reviewer HIGH, fixed here.

    Note: preserved ``TODAY`` as a module-level alias for backward
    compatibility with any external callers (and one existing test
    that imports it), but every in-module use goes through the
    function so it evaluates at call-time.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Backward-compat alias for external importers. Internal uses call _today().
# This value IS frozen at import time but downstream code no longer relies
# on it — all date comparisons below go through _today().
TODAY = _today()

# Signal -> skill correlation. Lives in ``stack_skill_map`` alongside
# the resolver's copy so both modules can't drift. Pre-P2.4, a local
# "minimal subset" was maintained here (20 entries vs resolve_skills'
# 40) — skills in stacks like angular/django/docker/pytest-cousins
# never got use_count bumped, which then fooled ctx_lifecycle into
# flagging them as stale. Code-reviewer HIGH, fixed by consolidation.
from ctx.core.resolve.stack_skill_map import STACK_SKILL_MAP as _SHARED_MAP  # noqa: E402

# Re-exported under the original name for backward-compat with any
# external caller that imports ``usage_tracker.SIGNAL_SKILL_MAP``.
# It's a MappingProxyType (read-only); mutations belong in
# ``stack_skill_map._RAW``, not here.
SIGNAL_SKILL_MAP = _SHARED_MAP

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
                    if entry.get("date", "") == _today():
                        for sig in entry.get("signals", []):
                            signal_counts[sig] = signal_counts.get(sig, 0) + 1
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        print(f"Warning: failed to read today's signals: {exc}", file=sys.stderr)

    return signal_counts


def signals_to_skills(signal_counts: dict[str, int]) -> set[str]:
    """Map signal names to skill names via SIGNAL_SKILL_MAP.

    Unmapped signals return NO skills. Prior impl fell through to
    ``[signal]`` — the raw signal string — which then became a skill
    slug passed into ``update_skill_page``. Effect: a signal like
    ``javascript`` (no entry in SIGNAL_SKILL_MAP) would poke a
    nonexistent ``javascript`` wiki page, or worse, corrupt the
    ``use_count`` on an unrelated wiki page whose slug happened to
    match. Code-reviewer HIGH, fixed by empty-list default.

    If a signal needs to map to itself (common for stack names that
    ARE skill names — e.g. ``docker`` → ``[docker]``), add it
    explicitly to SIGNAL_SKILL_MAP. The explicit map is the single
    source of truth; silent passthrough hides the mapping gap.
    """
    skills: set[str] = set()
    for signal in signal_counts:
        for skill in SIGNAL_SKILL_MAP.get(signal, []):
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
    """Set a frontmatter field, inserting it if missing.

    The previous regex-only implementation silently no-op'd when the
    field wasn't already present in frontmatter — which meant that a
    wiki entity page without ``session_count: 0`` pre-written could
    never accumulate the session_count bump, and the staleness →
    pending-unload gate at
    ``session_count >= STALE_THRESHOLD and use_count == 0`` never
    fired. The random-load-unload playbook caught this directly.

    If the field is already in frontmatter → replace its value.
    If not → insert ``field: value`` at the end of the frontmatter
    block (before the closing ``---``). If there's no frontmatter
    block at all → return content unchanged (callers expect this to
    be a no-op on pages without frontmatter).
    """
    escaped = re.escape(field)
    safe_value = str(value).replace("\r", " ").replace("\n", " ")
    pattern = rf"^({escaped}:\s*)(.+)$"

    def replace_value(match: re.Match[str]) -> str:
        return f"{match.group(1)}{safe_value}"

    # Replace first — the common path once the field exists.
    new_content, n = re.subn(
        pattern,
        replace_value,
        content,
        flags=re.MULTILINE,
    )
    if n > 0:
        return new_content

    # Field missing — try to insert it inside the first frontmatter block.
    # Frontmatter in our wiki is ``---\n...yaml...\n---\n`` at the top of
    # the file. Match only the FIRST such block (re.DOTALL-limited to
    # the initial chunk).
    fm_match = re.match(r"(^---\n)(.*?)(\n---\s*\n)", content, flags=re.DOTALL)
    if fm_match is None:
        return content  # no frontmatter to extend
    prefix, body, suffix = fm_match.group(1), fm_match.group(2), fm_match.group(3)
    new_body = body + (f"\n{field}: {safe_value}" if body else f"{field}: {safe_value}")
    return prefix + new_body + suffix + content[fm_match.end():]


PENDING_UNLOAD = CLAUDE_DIR / "pending-unload.json"


def _queue_unload_suggestion(skill_name: str, session_count: int, use_count: int) -> bool:
    """Add a skill to the pending-unload list. Returns True if newly queued."""
    pending: dict = {"suggestions": [], "generated_at": ""}
    if PENDING_UNLOAD.exists():
        try:
            pending = json.loads(PENDING_UNLOAD.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pending = {"suggestions": [], "generated_at": ""}

    existing_names = {s["name"] for s in pending.get("suggestions", [])}
    newly_queued = skill_name not in existing_names
    if newly_queued:
        pending.setdefault("suggestions", []).append({
            "name": skill_name,
            "reason": f"Loaded {session_count} sessions, used {use_count} times",
            "session_count": session_count,
            "use_count": use_count,
        })
    pending["generated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_text(PENDING_UNLOAD, json.dumps(pending, indent=2))
    return newly_queued


def update_skill_page(
    skill_name: str, used: bool, session_count_bump: bool = True,
    *, wiki_dir: Path | None = None,
) -> tuple[bool, bool]:
    """
    Update wiki entity page for a skill.

    Returns (updated, queued_for_unload):
      - updated: True if the wiki page existed and was rewritten.
      - queued_for_unload: True if this call newly queued a stale-suggestion
        into pending-unload.json (so callers can count stale detections
        without re-reading the page).

    Pass ``wiki_dir`` to target a non-default wiki; falls back to the
    module-level ``ENTITIES_DIR`` for backward compatibility. Strix
    vuln-0004: previously the ``--wiki`` CLI flag only gated the
    existence check, then this function ignored it and silently wrote
    into ``WIKI_DIR``.
    """
    if not SAFE_NAME_RE.match(skill_name):
        print(f"Warning: skipping invalid skill name: {skill_name!r}", file=sys.stderr)
        return False, False
    entities_dir = (wiki_dir / "entities" / "skills") if wiki_dir else ENTITIES_DIR
    page_path = entities_dir / f"{skill_name}.md"
    if not page_path.exists():
        return False, False

    queued = False
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
            today = _today()
            content = _set_frontmatter_field(content, "last_used", today)
            content = _set_frontmatter_field(content, "updated", today)
            # Reset stale status if skill was used
            content = _set_frontmatter_field(content, "status", "installed")
        else:
            # Check if stale threshold reached — don't mark stale directly,
            # write to pending-unload.json so the user can approve
            session_count = int(str(meta.get("session_count", "0")))
            use_count = int(str(meta.get("use_count", "0")))
            if session_count >= STALE_THRESHOLD and use_count == 0:
                queued = _queue_unload_suggestion(skill_name, session_count, use_count)

        _atomic_write_text(page_path, content)
        return True, queued
    except Exception as exc:
        print(f"Warning: failed to update skill page {skill_name}: {exc}", file=sys.stderr)
        return False, False


def append_wiki_log(loaded_count: int, used_skills: set[str], stale_count: int,
                    *, wiki_dir: Path | None = None) -> None:
    """Append session summary to wiki log.md.

    Honors ``wiki_dir`` if provided so Strix vuln-0004 (``--wiki`` flag
    silently ignored) is closed end-to-end.
    """
    log_path = (wiki_dir / "log.md") if wiki_dir else LOG_PATH
    if not log_path.exists():
        return

    entry = (
        f"\n## [{_today()}] session-end | usage-sync\n"
        f"- Skills loaded: {loaded_count}\n"
        f"- Skills actively used (signals): {len(used_skills)}\n"
        f"- Skills marked stale this session: {stale_count}\n"
    )
    if used_skills:
        entry += f"- Used: {', '.join(sorted(used_skills))}\n"

    with open(log_path, "a", encoding="utf-8") as f:
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

    # Pass wiki_dir through so --wiki actually routes writes to the
    # caller-chosen tree instead of silently landing in WIKI_DIR. A
    # non-default --wiki is now honored end-to-end (Strix vuln-0004).
    wiki_override = wiki_dir if wiki_dir != WIKI_DIR else None
    for skill_name in loaded_skills:
        skill_used = skill_name in used_skills
        updated, queued = update_skill_page(skill_name, used=skill_used,
                                            wiki_dir=wiki_override)
        if updated:
            updated_count += 1
            if queued:
                stale_count += 1

    append_wiki_log(len(loaded_skills), used_skills, stale_count,
                    wiki_dir=wiki_override)
    truncate_intent_log()

    print(
        f"usage-tracker: {updated_count} pages updated, "
        f"{len(used_skills)} skills used, {stale_count} marked stale"
    )


if __name__ == "__main__":
    main()
