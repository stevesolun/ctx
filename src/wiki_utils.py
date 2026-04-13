#!/usr/bin/env python3
"""
wiki_utils.py -- Shared frontmatter parsing for the skill wiki.

Consolidates the 6 divergent frontmatter parsers into one canonical
implementation used by wiki_sync, wiki_orchestrator, wiki_graphify,
wiki_query, usage_tracker, and wiki_lint.
"""

import re
from typing import Any

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$")


def validate_skill_name(name: str) -> str:
    """Validate a skill name is safe for path construction. Returns the name or raises."""
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
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        elif (val.startswith('"') and val.endswith('"')) or \
             (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        fm[key] = val
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
