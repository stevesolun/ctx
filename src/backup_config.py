"""
backup_config.py -- Schema + loader for the backup mirror system.

Defines what gets backed up, where snapshots go, how they're named, and
retention policy. The schema is loaded from two sources (deep-merged):

    1. src/config.json::backup           — repo default (checked in)
    2. ~/.claude/backup-config.json      — user override (optional)

User values take precedence. Missing keys fall back to dataclass defaults
so the backup system works out-of-the-box even with zero configuration.

ALWAYS_EXCLUDE is a hard-coded safety net: any name in this set is
dropped from ``top_files`` regardless of what the config says. This is
how we prevent ``.credentials.json`` or auth caches from ever ending up
in a snapshot even if a user's config accidentally lists them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Files we refuse to ever copy, even if the user lists them. These
# contain live auth tokens or ephemeral machine state that would either
# leak credentials or become stale the moment a snapshot is taken.
ALWAYS_EXCLUDE: frozenset[str] = frozenset({
    ".credentials.json",
    "mcp-needs-auth-cache.json",
    "stats-cache.json",
    "claude.json",
    ".claude.json",
})

_ALLOWED_SCOPES: frozenset[str] = frozenset({"full", "incremental", "hybrid"})


# ── Schema ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BackupTree:
    """A directory tree to mirror into each snapshot."""

    src: str   # relative to claude_home, e.g. "agents"
    dest: str  # relative to snapshot root, e.g. "agents"


@dataclass(frozen=True)
class BackupRetention:
    """How many snapshots survive automatic pruning."""

    keep_latest: int = 50
    keep_daily: int = 14


@dataclass(frozen=True)
class BackupConfig:
    """Full backup system configuration.

    Path fields store the raw string (``~`` and ``$VARS`` unexpanded) so
    the config round-trips through JSON without mutation. Use
    :meth:`snapshot_dir_resolved` to get a materialized ``Path``.
    """

    snapshot_dir: str = "~/.claude/backups"
    # Two placeholders: ``{timestamp}`` required, ``{reason}`` optional.
    # The Phase 2 CLI will format reason from ``--reason <label>``.
    name_format: str = "{timestamp}"
    timestamp_format: str = "%Y%m%dT%H%M%SZ"
    # scope controls how much each snapshot contains:
    #   full        — every file listed in top_files + trees (default)
    #   incremental — only files whose mtime changed since last snapshot
    #   hybrid      — full weekly, incremental otherwise
    # Phase 2 implements incremental/hybrid. Phase 1 only honours "full".
    scope: str = "full"
    max_file_bytes: int = 5 * 1024 * 1024  # 5 MB
    top_files: tuple[str, ...] = (
        "settings.json",
        "skill-manifest.json",
        "pending-skills.json",
        "CLAUDE.md",
        "AGENTS.md",
        "user-profile.json",
        "skill-system-config.json",
        "skill-registry.json",
    )
    trees: tuple[BackupTree, ...] = (
        BackupTree(src="agents", dest="agents"),
        BackupTree(src="skills", dest="skills"),
    )
    # When True, walk ~/.claude/projects/*/memory and mirror every .md
    # file under memory/<slug>/... in the snapshot. Disabled only if the
    # user explicitly opts out.
    memory_glob: bool = True
    excludes: tuple[str, ...] = ()
    retention: BackupRetention = field(default_factory=BackupRetention)

    def __post_init__(self) -> None:
        if self.scope not in _ALLOWED_SCOPES:
            raise ValueError(
                f"scope must be one of {sorted(_ALLOWED_SCOPES)}, "
                f"got {self.scope!r}"
            )
        if self.max_file_bytes < 0:
            raise ValueError(
                f"max_file_bytes must be >= 0, got {self.max_file_bytes}"
            )
        if self.retention.keep_latest < 0:
            raise ValueError(
                f"retention.keep_latest must be >= 0, "
                f"got {self.retention.keep_latest}"
            )
        if self.retention.keep_daily < 0:
            raise ValueError(
                f"retention.keep_daily must be >= 0, "
                f"got {self.retention.keep_daily}"
            )
        if "{timestamp}" not in self.name_format:
            raise ValueError(
                f"name_format must contain '{{timestamp}}', "
                f"got {self.name_format!r}"
            )
        # Silently drop ALWAYS_EXCLUDE names from top_files. frozen
        # dataclass requires object.__setattr__ for the rewrite.
        filtered_top = tuple(
            name for name in self.top_files
            if Path(name).name not in ALWAYS_EXCLUDE
        )
        if filtered_top != self.top_files:
            object.__setattr__(self, "top_files", filtered_top)

    # ── Path helpers ────────────────────────────────────────────────────────

    def snapshot_dir_resolved(self) -> Path:
        """Return snapshot_dir with ``~`` and ``$VARS`` expanded."""
        return Path(os.path.expanduser(os.path.expandvars(self.snapshot_dir)))

    def is_excluded(self, rel_path: str) -> bool:
        """True if ``rel_path`` is in excludes or ALWAYS_EXCLUDE."""
        name = Path(rel_path).name
        if name in ALWAYS_EXCLUDE:
            return True
        for pattern in self.excludes:
            if pattern == rel_path or Path(pattern).name == name:
                return True
        return False


# ── Loader ──────────────────────────────────────────────────────────────────


def _coerce_trees(raw: Any, default: tuple[BackupTree, ...]) -> tuple[BackupTree, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list):
        return default
    out: list[BackupTree] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        src = str(entry.get("src") or "").strip()
        if not src:
            continue
        dest = str(entry.get("dest") or src).strip()
        out.append(BackupTree(src=src, dest=dest))
    return tuple(out)


def _coerce_retention(raw: Any) -> BackupRetention:
    if not isinstance(raw, dict):
        return BackupRetention()
    return BackupRetention(
        keep_latest=int(raw.get("keep_latest", 50)),
        keep_daily=int(raw.get("keep_daily", 14)),
    )


def load_backup_config(raw: dict[str, Any] | None = None) -> BackupConfig:
    """Build a BackupConfig from a raw dict, applying defaults.

    Missing keys fall back to dataclass defaults. Invalid values raise
    :class:`ValueError` via the dataclass ``__post_init__`` validator.
    Pass ``None`` or ``{}`` to get the full default config.
    """
    if not raw:
        return BackupConfig()

    defaults = BackupConfig()

    top_raw = raw.get("top_files")
    if top_raw is None:
        top_files: tuple[str, ...] = defaults.top_files
    else:
        top_files = tuple(str(x) for x in top_raw)

    return BackupConfig(
        snapshot_dir=str(raw.get("snapshot_dir", defaults.snapshot_dir)),
        name_format=str(raw.get("name_format", defaults.name_format)),
        timestamp_format=str(
            raw.get("timestamp_format", defaults.timestamp_format)
        ),
        scope=str(raw.get("scope", defaults.scope)),
        max_file_bytes=int(raw.get("max_file_bytes", defaults.max_file_bytes)),
        top_files=top_files,
        trees=_coerce_trees(raw.get("trees"), defaults.trees),
        memory_glob=bool(raw.get("memory_glob", defaults.memory_glob)),
        excludes=tuple(str(x) for x in raw.get("excludes", ())),
        retention=_coerce_retention(raw.get("retention")),
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Merge override into base in-place, recursive for nested dicts."""
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def from_ctx_config() -> BackupConfig:
    """Assemble the effective BackupConfig from ctx_config + user overlay.

    Order of precedence (later overrides earlier):
      1. dataclass defaults
      2. ``src/config.json::backup`` (via ``ctx_config.cfg``)
      3. ``~/.claude/backup-config.json`` (user override, optional)

    Never raises: a missing or malformed user file is silently ignored
    so a backup is always possible even when the user's JSON is broken.
    """
    try:
        from ctx_config import cfg  # noqa: PLC0415
        base_raw = cfg.get("backup") or {}
    except Exception:
        base_raw = {}

    merged: dict[str, Any] = dict(base_raw) if isinstance(base_raw, dict) else {}

    user_override = Path(os.path.expanduser("~/.claude/backup-config.json"))
    if user_override.is_file():
        try:
            user_raw = json.loads(user_override.read_text(encoding="utf-8"))
            if isinstance(user_raw, dict):
                _deep_merge(merged, user_raw)
        except (OSError, json.JSONDecodeError):
            pass

    return load_backup_config(merged if merged else None)
