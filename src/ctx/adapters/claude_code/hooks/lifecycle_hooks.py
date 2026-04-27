"""Packaged Claude Code lifecycle hook commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_DEFAULT_LOOKBACK_HOURS = 24
_MAX_SLUGS_PER_RUN = 50
_STATE_PATH = Path(os.path.expanduser("~/.claude/skill-quality/.hook-state.json"))
_EVENTS_PATH = Path(os.path.expanduser("~/.claude/skill-events.jsonl"))


def _load_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _extract_touched_path(payload: dict[str, Any]) -> Path | None:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    candidate = tool_input.get("file_path")
    if not isinstance(candidate, str) or not candidate:
        return None
    try:
        return Path(candidate).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return None


def _is_tracked(path: Path, claude_home: Path) -> bool:
    try:
        from backup_config import from_ctx_config  # noqa: PLC0415
    except ImportError:
        return False

    cfg = from_ctx_config()
    try:
        rel = path.resolve(strict=False).relative_to(
            claude_home.resolve(strict=False)
        )
    except (OSError, ValueError):
        return False

    rel_posix = rel.as_posix()
    if rel_posix in cfg.top_files:
        return True
    for tree in cfg.trees:
        prefix = tree.src.rstrip("/") + "/"
        if rel_posix == tree.src or rel_posix.startswith(prefix):
            return True
    return bool(
        cfg.memory_glob
        and len(rel.parts) >= 3
        and rel.parts[0] == "projects"
        and rel.parts[2] == "memory"
    )


def cmd_backup_on_change(_args: argparse.Namespace) -> int:
    payload = _load_payload()
    touched = _extract_touched_path(payload)
    if touched is None:
        return 0

    claude_home = Path(os.path.expanduser("~/.claude"))
    if not _is_tracked(touched, claude_home):
        return 0

    tool_name = str(payload.get("tool_name") or "unknown")
    reason = f"{tool_name}:{touched.name}"
    try:
        from backup_mirror import snapshot_if_changed  # noqa: PLC0415

        snapshot_if_changed(reason=reason)
    except Exception as exc:  # noqa: BLE001
        print(f"[lifecycle_hooks] backup failed: {exc}", file=sys.stderr)
    return 0


def _read_cutoff() -> datetime:
    if _STATE_PATH.is_file():
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            ts = data.get("last_run_at")
            if isinstance(ts, str):
                parsed = datetime.fromisoformat(ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    return datetime.now(timezone.utc) - timedelta(hours=_DEFAULT_LOOKBACK_HOURS)


def _write_state(now: datetime) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({"last_run_at": now.isoformat(timespec="seconds")}),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[lifecycle_hooks] could not write state: {exc}", file=sys.stderr)


def _touched_slugs_since(cutoff: datetime) -> list[str]:
    if not _EVENTS_PATH.is_file():
        return []
    seen: dict[str, None] = {}
    try:
        with _EVENTS_PATH.open(encoding="utf-8") as fh:
            for raw in fh:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                slug = obj.get("skill")
                ts_raw = obj.get("timestamp")
                if not isinstance(slug, str) or not isinstance(ts_raw, str):
                    continue
                try:
                    parsed = datetime.fromisoformat(ts_raw)
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed >= cutoff:
                    seen.setdefault(slug, None)
    except OSError:
        return []
    return list(seen.keys())[:_MAX_SLUGS_PER_RUN]


def _invoke_recompute(slugs: list[str], session_id: str) -> int:
    if not slugs:
        return 0
    old_session_id = os.environ.get("CTX_SESSION_ID")
    os.environ["CTX_SESSION_ID"] = session_id
    try:
        from skill_quality import main as skill_quality_main  # noqa: PLC0415

        return int(skill_quality_main(["recompute", "--slugs", ",".join(slugs)]))
    except Exception as exc:  # noqa: BLE001
        print(f"[lifecycle_hooks] recompute failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if old_session_id is None:
            os.environ.pop("CTX_SESSION_ID", None)
        else:
            os.environ["CTX_SESSION_ID"] = old_session_id


def cmd_quality_on_session_end(_args: argparse.Namespace) -> int:
    payload = _load_payload()
    now = datetime.now(timezone.utc)
    cutoff = _read_cutoff()
    slugs = _touched_slugs_since(cutoff)

    session_id = ""
    if isinstance(payload, dict):
        raw = payload.get("session_id") or payload.get("sessionId")
        session_id = raw if isinstance(raw, str) else ""
    if not session_id:
        session_id = f"session-{now.strftime('%Y%m%dT%H%M%SZ')}"

    rc = _invoke_recompute(slugs, session_id=session_id)
    if rc == 0:
        _write_state(now)

    try:
        from ctx_audit_log import log_session_event, rotate_if_needed  # noqa: PLC0415

        log_session_event(
            "session.ended",
            session_id,
            actor="hook",
            meta={"recomputed_slugs": len(slugs), "cutoff": cutoff.isoformat()},
        )
        rotate_if_needed()
    except Exception:  # noqa: BLE001
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    backup = sub.add_parser("backup-on-change")
    backup.set_defaults(func=cmd_backup_on_change)
    quality = sub.add_parser("quality-on-session-end")
    quality.set_defaults(func=cmd_quality_on_session_end)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
