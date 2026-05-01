#!/usr/bin/env python3
"""
install_utils.py -- Shared install/unload primitives for skills, agents, and MCPs.

Three install CLIs (skill_install, agent_install, mcp_install) all need
the same primitives: read/write the shared manifest, flip entity
``status`` frontmatter, emit telemetry, validate slugs. Previously
each CLI carried its own copy, and a dedup-check bug slipped past
review in ``skill_install`` because the copy there had drifted from
``agent_install``. One shared module, one place to fix.

Canonical manifest entry shape::

    {
      "skill": "<slug>",
      "entity_type": "skill" | "agent" | "mcp-server",
      "source": "ctx-skill-install" | "ctx-agent-install" | "ctx-mcp-install",
      # optional install-time metadata:
      "command": "npx -y @pkg/mcp",           # mcp-server only
    }

The ``skill`` key is historical — it predates mixed entity types in
the manifest — and is kept for backward compatibility with readers
that haven't learned about ``entity_type``. New code should disambiguate
on the ``(skill, entity_type)`` tuple.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Literal

from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text
from ctx.utils._file_lock import file_lock

_logger = logging.getLogger(__name__)

EntityType = Literal["skill", "agent", "mcp-server"]

MANIFEST_PATH = Path(os.path.expanduser("~/.claude/skill-manifest.json"))

_FRONTMATTER_HEAD_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _has_symlink_in_path(path: Path) -> bool:
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def safe_copy_file(source: Path, dest: Path, *, dest_root: Path) -> None:
    """Copy one file while refusing symlink write-through paths."""
    if _has_symlink_in_path(source):
        raise ValueError(f"refusing to copy symlinked source: {source}")
    if not source.is_file():
        raise FileNotFoundError(f"copy source missing: {source}")
    if dest_root.exists() and dest_root.is_symlink():
        raise ValueError(f"refusing symlinked destination root: {dest_root}")

    dest_root.mkdir(parents=True, exist_ok=True)
    dest_parent = dest.parent
    if dest_parent.exists() and dest_parent.is_symlink():
        raise ValueError(f"refusing symlinked destination parent: {dest_parent}")
    dest_parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        raise ValueError(f"refusing symlinked destination file: {dest}")

    root_real = dest_root.resolve()
    parent_real = dest_parent.resolve()
    if parent_real != root_real and root_real not in parent_real.parents:
        raise ValueError(f"destination escapes install root: {dest}")
    shutil.copy2(source, dest, follow_symlinks=False)


# ── Manifest I/O ─────────────────────────────────────────────────────────────


def load_manifest() -> dict:
    """Read ``~/.claude/skill-manifest.json``. Return an empty shell on any error.

    The manifest is advisory — the filesystem (installed skills/agents
    dirs, registered MCPs) is authoritative — so a lost/corrupt
    manifest just means the next install rebuilds it entry-by-entry.
    """
    if not MANIFEST_PATH.exists():
        return {"load": [], "unload": [], "warnings": []}
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"load": [], "unload": [], "warnings": []}
    data.setdefault("load", [])
    data.setdefault("unload", [])
    data.setdefault("warnings", [])
    return data


def save_manifest(manifest: dict) -> None:
    """Atomically persist the manifest."""
    _atomic_write_text(MANIFEST_PATH, json.dumps(manifest, indent=2))


def _update_manifest(mutator: Callable[[dict], None]) -> None:
    """Serialize manifest read-modify-write transactions."""
    with file_lock(MANIFEST_PATH):
        manifest = load_manifest()
        mutator(manifest)
        save_manifest(manifest)


# ── Record install / uninstall ───────────────────────────────────────────────


def record_install(
    slug: str,
    *,
    entity_type: EntityType,
    source: str,
    extra: dict | None = None,
) -> None:
    """Add an install entry for ``(slug, entity_type)`` (idempotent).

    If a matching entry already exists (same slug AND same entity_type),
    we leave it alone rather than appending a duplicate. The ``unload``
    list is scrubbed of any matching (slug, entity_type) so a re-install
    clears the previous unload flag.

    Dedup is on the (slug, entity_type) TUPLE. A skill and an agent
    can legitimately share the same slug; the tuple keeps them
    distinct in the manifest. (Pre-install-utils, the skill CLI
    deduped on slug alone, which silently dropped the skill entry
    when an agent with the same slug already existed.)
    """
    def mutate(manifest: dict) -> None:
        loaded: set[tuple[str, str]] = {
            (e.get("skill"), e.get("entity_type", "skill"))
            for e in manifest["load"]
        }
        if (slug, entity_type) not in loaded:
            entry: dict = {
                "skill": slug,
                "entity_type": entity_type,
                "source": source,
            }
            if extra:
                entry.update(extra)
            manifest["load"].append(entry)

        manifest["unload"] = [
            e for e in manifest["unload"]
            if not (
                e.get("skill") == slug
                and e.get("entity_type", "skill") == entity_type
            )
        ]

    _update_manifest(mutate)


def record_uninstall(slug: str, *, entity_type: EntityType, source: str) -> None:
    """Drop the load entry for ``(slug, entity_type)`` and record an unload.

    The unload dedup also keys on (slug, entity_type); re-unloading
    the same pair doesn't produce duplicates.
    """
    def mutate(manifest: dict) -> None:
        manifest["load"] = [
            e for e in manifest["load"]
            if not (
                e.get("skill") == slug
                and e.get("entity_type", "skill") == entity_type
            )
        ]
        unloaded: set[tuple[str, str]] = {
            (e.get("skill"), e.get("entity_type", "skill"))
            for e in manifest["unload"]
        }
        if (slug, entity_type) not in unloaded:
            manifest["unload"].append({
                "skill": slug,
                "entity_type": entity_type,
                "source": source,
            })

    _update_manifest(mutate)


# ── Entity status frontmatter ────────────────────────────────────────────────


def bump_entity_status(
    entity_path: Path, *, status: str, extra_fields: dict | None = None,
) -> bool:
    """Flip the entity's ``status:`` frontmatter field (and optional extras).

    Returns True when the file on disk changed. No-op (returns False)
    when the entity file doesn't exist — the install still succeeded
    in the filesystem sense; the wiki card is a mirror of state, not
    authoritative.

    ``extra_fields`` values are rendered with a conservative YAML
    scalar rule: quote when the value could be misparsed as another
    type. None values become the YAML ``null`` sentinel so callers
    can blank out a field by passing ``{"install_cmd": None}``.
    """
    if not entity_path.is_file():
        return False
    text = entity_path.read_text(encoding="utf-8", errors="replace")
    new_text = _replace_or_insert_field(text, "status", status)
    if extra_fields:
        for field, value in extra_fields.items():
            new_text = _replace_or_insert_field(
                new_text, field, _render_scalar(value),
            )
    if new_text != text:
        _atomic_write_text(entity_path, new_text)
        return True
    return False


def _replace_or_insert_field(text: str, field: str, rendered_value: str) -> str:
    """Replace or insert ``field: rendered_value`` in the frontmatter block.

    - If the field already exists, its value is replaced (first match).
    - If the frontmatter exists but the field doesn't, we append the
      field BEFORE the closing ``---`` delimiter. This keeps install
      state at the end of the frontmatter, which is less jarring than
      jamming it at the top every time a new field appears.
    - If the file has no frontmatter at all, we refuse to invent one.
    """
    escaped = re.escape(field)
    pattern = rf"^{escaped}:[ \t]*.*$"
    repl = f"{field}: {rendered_value}"
    new_text, count = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if count:
        return new_text

    fm_match = _FRONTMATTER_HEAD_RE.match(text)
    if fm_match is None:
        return text
    insert_at = fm_match.end(1)  # end of frontmatter body (before closing ---)
    return text[:insert_at] + f"\n{field}: {rendered_value}" + text[insert_at:]


def _render_scalar(value: object) -> str:
    """YAML scalar rendering for the handful of types install code writes."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # Neutralise ASCII line breaks (\r\n) AND Unicode line separators
        # (U+0085 NEL, U+2028 LS, U+2029 PS). Python's str.splitlines()
        # treats all five as real line boundaries, so downstream parsers
        # (mcp_install._parse_entity_frontmatter, wiki_utils) would
        # otherwise see a quoted scalar as multiple frontmatter lines —
        # Strix vuln-0001 HIGH (CWE-116). The fix closes the injection
        # at the writer; the parsers stay unchanged.
        safe = value.translate(str.maketrans("\r\n\x85\u2028\u2029", "     "))
        # Conservative: quote when the string contains ANY YAML-structural
        # / flow-indicator / reserved character, OR leads with a block-
        # indicator char, OR leads/trails whitespace. The unquoted path is
        # reserved for simple alphanumeric-style values that YAML's plain-
        # scalar scanner parses unambiguously.
        #
        # The full set mirrors the YAML 1.1 reserved-indicator table:
        # flow indicators (,[]{}), block/map indicators (:?-), anchor/
        # alias (&*), tag (!), pipe/fold (|>), comment (#), directive (%),
        # reserved (@`), and both quote marks.
        yaml_structural = set(",[]{}:?#&*!|>%@`=\"'\\")
        needs_quote = (
            any(ch in safe for ch in yaml_structural)
            or (safe and (safe[0] == "-" or safe[0].isspace() or safe[-1].isspace()))
            or safe.startswith(("?", "[", "{"))
        )
        if needs_quote:
            escaped = safe.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return safe
    return f'"{str(value)}"'


# ── Telemetry ────────────────────────────────────────────────────────────────


def emit_load_event(slug: str, session_id: str) -> None:
    """Log a ``load`` telemetry event. Failures are silenced.

    We deliberately don't propagate telemetry errors — a broken
    telemetry sink must not interrupt the install path. Errors go
    to the debug log only.
    """
    try:
        import skill_telemetry  # noqa: PLC0415 — local import avoids a
        # circular-import risk and keeps the cold path cheap.

        skill_telemetry.log_event("load", slug, session_id)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("install_utils: telemetry write failed for %r: %s", slug, exc)
