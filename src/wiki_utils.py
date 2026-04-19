#!/usr/bin/env python3
"""
wiki_utils.py -- Shared frontmatter parsing for the skill wiki.

Consolidates the 6 divergent frontmatter parsers into one canonical
implementation used by wiki_sync, wiki_orchestrator, wiki_graphify,
wiki_query, usage_tracker, and wiki_lint.
"""

import re
from typing import Any

__all__ = [
    "SAFE_NAME_RE",
    "FRONTMATTER_RE",
    "validate_skill_name",
    "parse_frontmatter",
    "parse_frontmatter_and_body",
]

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")


def validate_skill_name(name: str) -> str:
    """Tier-1 (lenient) validator for skill names already accepted on disk.

    Accepts the full legacy character set: uppercase, underscores, and dots
    are all permitted so that existing installed skill directories are not
    rejected during re-ingestion or wiki-sync operations.

    Pattern: ``^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$``

    For *user-supplied* slugs (new entries submitted via the hook or CLI)
    use ``skill_add_detector.validate_user_supplied_slug`` instead, which
    enforces the stricter Tier-2 contract (lowercase, hyphens only, 64-char
    max) to minimise attack surface on newly-created entries.
    """
    if not SAFE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid skill name {name!r}: must be 1-128 alphanumeric/underscore/dot/hyphen chars, "
            "starting with alphanumeric"
        )
    return name


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse YAML-style frontmatter from a markdown string.

    Returns a dict of key-value pairs. List values like ``[a, b, c]`` are
    returned as ``list[str]``. All other values are returned as stripped strings.
    Returns an empty dict if no valid frontmatter block is found.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm: dict[str, Any] = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            # Inline list: ``tags: [a, b, c]``
            fm[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            i += 1
            continue
        if val == "" and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("- "):
            # Multi-line YAML list:
            #   tags:
            #     - python
            #     - frontend
            # Real wiki entity pages use this form; the old parser only
            # handled inline lists and silently dropped these, collapsing
            # every graph-edge source to slug tokens alone.
            collected: list[str] = []
            i += 1
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                item = lines[i].lstrip()[2:].strip().strip("'\"")
                if item:
                    collected.append(item)
                i += 1
            fm[key] = collected
            continue
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        fm[key] = val
        i += 1
    return fm


def parse_frontmatter_and_body(text: str) -> tuple[dict[str, Any], str]:
    """Parse frontmatter and return (fields, body_text).

    Like ``parse_frontmatter`` but also returns the content after the closing
    ``---`` delimiter. If no frontmatter is found, returns ``({}, text)``.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm = parse_frontmatter(text)
    body = text[m.end():].strip()
    return fm, body


def get_field(text: str, field: str) -> str:
    """Extract a single frontmatter field value by name.

    Faster than full parse when you only need one field. Returns empty string
    if the field is not found.
    """
    m = re.search(rf"^{re.escape(field)}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""
