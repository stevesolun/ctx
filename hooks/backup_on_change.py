#!/usr/bin/env python3
"""
backup_on_change.py -- PostToolUse hook that snapshots on config changes.

Designed to be registered in ``~/.claude/settings.json`` under:

    "hooks": {
      "PostToolUse": [
        {
          "matcher": "Edit|Write|MultiEdit",
          "hooks": [
            {
              "type": "command",
              "command": "python <repo>/hooks/backup_on_change.py"
            }
          ]
        }
      ]
    }

Claude Code delivers each PostToolUse event as a JSON payload on stdin.
This script:

  1. Parses the payload.
  2. Checks whether the tool edited a file that BackupConfig tracks
     (top_files, trees, or projects/*/memory when memory_glob is on).
  3. If so, shells out to ``python src/backup_mirror.py snapshot-if-changed
     --reason <tool>:<basename>`` so the snapshot name records what fired it.
  4. Never blocks the tool: any error is logged to stderr and the hook
     exits 0 so a bug here can't stall the user's session.

Snapshots only happen when content *actually* changed (SHA diff against
the last snapshot's manifest) — so a no-op Edit won't create a folder.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_payload() -> dict[str, Any]:
    """Read the PostToolUse payload from stdin. Empty on error."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _extract_touched_path(payload: dict[str, Any]) -> Path | None:
    """Pull the file path out of an Edit / Write / MultiEdit payload."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    # Edit, Write, MultiEdit all use ``file_path``.
    candidate = tool_input.get("file_path")
    if isinstance(candidate, str) and candidate:
        try:
            return Path(candidate).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            return None
    return None


def _is_tracked(path: Path, claude_home: Path) -> bool:
    """True when ``path`` is one of the files BackupConfig mirrors."""
    # Lazy import: hook must still function even when the rest of the
    # repo's dependency graph is in a weird state (e.g. during install).
    try:
        from backup_config import from_ctx_config  # noqa: PLC0415
    except ImportError:
        return False

    cfg = from_ctx_config()

    try:
        path_resolved = path.resolve(strict=False)
        home_resolved = claude_home.resolve(strict=False)
    except OSError:
        return False

    try:
        rel = path_resolved.relative_to(home_resolved)
    except ValueError:
        return False

    rel_posix = rel.as_posix()

    # Top-level files: match by basename against cfg.top_files.
    if rel_posix in cfg.top_files:
        return True

    # Trees: match any file under a tracked tree's src prefix.
    for tree in cfg.trees:
        prefix = tree.src.rstrip("/") + "/"
        if rel_posix == tree.src or rel_posix.startswith(prefix):
            return True

    # Memory glob: projects/<slug>/memory/...
    if cfg.memory_glob:
        parts = rel.parts
        if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "memory":
            return True

    return False


def _invoke_snapshot(reason: str) -> int:
    """Shell out to snapshot-if-changed. Returns child exit code (or 0)."""
    mirror = SRC / "backup_mirror.py"
    if not mirror.is_file():
        print(f"[backup_on_change] missing {mirror}", file=sys.stderr)
        return 0
    try:
        result = subprocess.run(
            [sys.executable, str(mirror), "snapshot-if-changed",
             "--reason", reason],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.stdout.strip():
            print(result.stdout.strip(), file=sys.stderr)
        if result.returncode != 0 and result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[backup_on_change] snapshot failed: {exc}", file=sys.stderr)
        return 0


def main() -> int:
    payload = _load_payload()
    tool_name = str(payload.get("tool_name") or "unknown")

    touched = _extract_touched_path(payload)
    if touched is None:
        return 0

    claude_home = Path(os.path.expanduser("~/.claude"))
    if not _is_tracked(touched, claude_home):
        return 0

    reason = f"{tool_name}:{touched.name}"
    _invoke_snapshot(reason)
    # Always exit 0: hook failures must not block the user's tool.
    return 0


if __name__ == "__main__":
    sys.exit(main())
