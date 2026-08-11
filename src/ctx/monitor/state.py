"""Filesystem state locations for the ctx dashboard."""

from __future__ import annotations

import os
from pathlib import Path


def claude_dir() -> Path:
    return Path(os.path.expanduser("~/.claude"))


def audit_log_path(base: Path | None = None) -> Path:
    # Avoid importing ctx_audit_log here so the monitor can run even if
    # ctx_audit_log is absent for some reason.
    return (base or claude_dir()) / "ctx-audit.jsonl"


def events_jsonl_path(base: Path | None = None) -> Path:
    return (base or claude_dir()) / "skill-events.jsonl"


def runtime_lifecycle_path() -> Path:
    from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore

    return RuntimeLifecycleStore().events_path


def manifest_path(base: Path | None = None) -> Path:
    return (base or claude_dir()) / "skill-manifest.json"


def sidecar_dir(base: Path | None = None) -> Path:
    return (base or claude_dir()) / "skill-quality"


def wiki_dir(base: Path | None = None) -> Path:
    return (base or claude_dir()) / "skill-wiki"


def user_config_path(base: Path | None = None) -> Path:
    return (base or claude_dir()) / "skill-system-config.json"
