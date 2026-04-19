#!/usr/bin/env python3
"""
quality_on_session_end.py -- Stop hook that recomputes quality for the slugs
this session touched.

Designed for ``~/.claude/settings.json``:

    "hooks": {
      "Stop": [
        {
          "hooks": [
            {
              "type": "command",
              "command": "python <repo>/hooks/quality_on_session_end.py"
            }
          ]
        }
      ]
    }

Why incremental instead of ``recompute --all``:

  - Full recompute walks every installed skill + agent (2,000+ pages) and
    runs four signal extractors per page. That's ~30s on a warm cache and
    dominates the tail of every session.
  - The only signals that *changed* since last session are telemetry
    (we logged new loads) and maybe intake (if the user edited a skill
    file). Every other signal moves on a slower clock.
  - So we compute the set of slugs that showed up in the telemetry event
    stream since the last time this hook ran, and rescore just those.

Always exits 0: a hook that blocks session shutdown is worse than a
slightly stale quality score.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# How far back to look for touched slugs if no marker file exists.
# Matches ``recent_window_days`` default in ``QualityConfig`` so a
# freshly-installed system scores every recently-loaded skill on first run.
_DEFAULT_LOOKBACK_HOURS = 24

# State file: stores the ISO timestamp of the last successful run. Lives
# under ~/.claude so it persists across repo clones and venv moves.
_STATE_PATH = Path(os.path.expanduser("~/.claude/skill-quality/.hook-state.json"))
_EVENTS_PATH = Path(os.path.expanduser("~/.claude/skill-events.jsonl"))

# Upper bound on how many slugs we'll hand to the recompute subcommand in
# one invocation. Pathological: a user loads 500 distinct skills in one
# session. We'd rather recompute the top 50 than stall on session-end.
_MAX_SLUGS_PER_RUN = 50


def _load_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _read_cutoff() -> datetime:
    """Return the 'since' cutoff for scanning events."""
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
        print(f"[quality_on_session_end] could not write state: {exc}",
              file=sys.stderr)


def _touched_slugs_since(cutoff: datetime, events_path: Path) -> list[str]:
    """Return a deduplicated list of skill slugs that appear after ``cutoff``."""
    if not events_path.is_file():
        return []
    seen: dict[str, None] = {}
    try:
        with events_path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
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
                if parsed < cutoff:
                    continue
                # Insertion order preserved by dict in Python 3.7+.
                seen.setdefault(slug, None)
    except OSError:
        return []
    return list(seen.keys())[:_MAX_SLUGS_PER_RUN]


def _invoke_recompute(slugs: list[str]) -> int:
    if not slugs:
        return 0
    script = SRC / "skill_quality.py"
    if not script.is_file():
        print(f"[quality_on_session_end] missing {script}", file=sys.stderr)
        return 0
    try:
        result = subprocess.run(
            [sys.executable, str(script), "recompute",
             "--slugs", ",".join(slugs)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[quality_on_session_end] recompute failed: {exc}",
              file=sys.stderr)
        return 0


def main() -> int:
    payload = _load_payload()  # consume stdin even if we don't use it
    now = datetime.now(timezone.utc)
    cutoff = _read_cutoff()
    slugs = _touched_slugs_since(cutoff, _EVENTS_PATH)
    _invoke_recompute(slugs)
    _write_state(now)

    # Unified audit: one line per session boundary + rotate if big.
    # Guarded with try/except because a hook that fails on audit is
    # worse than one that runs without telemetry.
    try:
        # ctx_audit_log lives in src/. Add src/ to path so this hook
        # (which runs out of hooks/) can import it regardless of whether
        # the user is on the editable install or the pip-installed copy.
        _SRC = Path(__file__).parent.parent / "src"
        if str(_SRC) not in sys.path:
            sys.path.insert(0, str(_SRC))
        from ctx_audit_log import log_session_event, rotate_if_needed

        session_id = None
        if isinstance(payload, dict):
            session_id = payload.get("session_id") or payload.get("sessionId")
        if not session_id:
            # Fall back to a synthetic id so the event still carries
            # something investigators can correlate with skill-events.
            session_id = f"session-{now.strftime('%Y%m%dT%H%M%SZ')}"

        log_session_event(
            "session.ended", session_id, actor="hook",
            meta={"recomputed_slugs": len(slugs), "cutoff": cutoff.isoformat()},
        )
        rotate_if_needed()
    except Exception:  # noqa: BLE001 — audit is advisory
        pass

    # Always exit 0: hook errors must not stall session shutdown.
    return 0


if __name__ == "__main__":
    sys.exit(main())
