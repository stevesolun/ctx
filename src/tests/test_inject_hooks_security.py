"""
test_inject_hooks_security.py -- Security regression tests for inject_hooks.py.

Verifies:
1. Generated hook commands do NOT contain $CLAUDE_TOOL_INPUT or $CLAUDE_TOOL_NAME
   as literal substrings (shell injection vectors).
2. The Stop array contains BOTH usage_tracker and quality_on_session_end entries.
3. Concurrent/repeated writes to settings.json leave a valid JSON file
   (atomic write correctness).
"""

import json
import sys
import threading
from pathlib import Path

import pytest

# Ensure src/ is importable
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ctx.adapters.claude_code.inject_hooks import make_hooks, merge_hooks, write_settings_atomic  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_commands(hooks_block: dict) -> list[str]:
    """Flatten every 'command' string out of a hooks block dict."""
    cmds: list[str] = []
    for entries in hooks_block.values():
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if "command" in entry:
                cmds.append(entry["command"])
            for sub in entry.get("hooks", []):
                if isinstance(sub, dict) and "command" in sub:
                    cmds.append(sub["command"])
    return cmds


def _run_inject(ctx_dir: str, settings_path: Path) -> None:
    """Run the full inject pipeline (load → merge → atomic write)."""
    from ctx.adapters.claude_code.inject_hooks import load_settings, _remove_stale_hooks

    settings = load_settings(settings_path)
    settings = _remove_stale_hooks(settings)
    new_hooks = make_hooks(ctx_dir)
    updated = merge_hooks(settings, new_hooks)
    write_settings_atomic(settings_path, updated)


# ---------------------------------------------------------------------------
# Fix 1 — No shell-injection env vars in command strings
# ---------------------------------------------------------------------------


class TestNoShellInjectionVars:
    """Hook commands must not embed $CLAUDE_TOOL_INPUT or $CLAUDE_TOOL_NAME."""

    def test_make_hooks_no_tool_input_var(self, tmp_path: Path) -> None:
        ctx_dir = str(tmp_path / "ctx")
        hooks = make_hooks(ctx_dir)
        cmds = _all_commands(hooks)
        for cmd in cmds:
            assert "$CLAUDE_TOOL_INPUT" not in cmd, (
                f"Shell injection vector $CLAUDE_TOOL_INPUT found in command: {cmd!r}"
            )

    def test_make_hooks_no_tool_name_var(self, tmp_path: Path) -> None:
        ctx_dir = str(tmp_path / "ctx")
        hooks = make_hooks(ctx_dir)
        cmds = _all_commands(hooks)
        for cmd in cmds:
            assert "$CLAUDE_TOOL_NAME" not in cmd, (
                f"Shell injection vector $CLAUDE_TOOL_NAME found in command: {cmd!r}"
            )

    def test_generated_settings_no_tool_input_var(self, tmp_path: Path) -> None:
        """End-to-end: the JSON written to disk must not contain the injection vars."""
        settings_path = tmp_path / "settings.json"
        ctx_dir = str(tmp_path / "ctx")
        _run_inject(ctx_dir, settings_path)

        raw = settings_path.read_text(encoding="utf-8")
        assert "$CLAUDE_TOOL_INPUT" not in raw, (
            "$CLAUDE_TOOL_INPUT found in written settings.json"
        )
        assert "$CLAUDE_TOOL_NAME" not in raw, (
            "$CLAUDE_TOOL_NAME found in written settings.json"
        )

    def test_from_stdin_flag_present_in_posttooluse_commands(
        self, tmp_path: Path
    ) -> None:
        """PostToolUse commands that replaced env-var args must use --from-stdin."""
        ctx_dir = str(tmp_path / "ctx")
        hooks = make_hooks(ctx_dir)
        post_tool_entries = hooks.get("PostToolUse", [])
        assert post_tool_entries, "PostToolUse block must not be empty"

        # context_monitor and skill_add_detector commands must carry --from-stdin
        sub_hooks = post_tool_entries[0].get("hooks", [])
        cmds_with_stdin = [
            h["command"] for h in sub_hooks
            if isinstance(h, dict) and "--from-stdin" in h.get("command", "")
        ]
        assert len(cmds_with_stdin) >= 2, (
            f"Expected at least 2 --from-stdin commands; found {len(cmds_with_stdin)}: "
            f"{cmds_with_stdin}"
        )


# ---------------------------------------------------------------------------
# Fix 2 — Stop array contains both usage_tracker and quality_on_session_end
# ---------------------------------------------------------------------------


class TestStopHooks:
    def _stop_commands(self, hooks: dict) -> list[str]:
        """Flatten both legacy (flat) + current ({"hooks":[...]} matcher)
        Stop-hook shapes into a list of command strings. The current
        generator produces the matcher shape (required by Claude Code's
        schema) but we accept both so legacy settings.json files that
        still use the flat shape don't make the assertions drift.
        """
        out: list[str] = []
        for entry in hooks.get("Stop", []):
            if not isinstance(entry, dict):
                continue
            if "command" in entry:
                out.append(entry["command"])
            for sub in entry.get("hooks", []):
                if isinstance(sub, dict) and "command" in sub:
                    out.append(sub["command"])
        return out

    def test_stop_contains_usage_tracker(self, tmp_path: Path) -> None:
        ctx_dir = str(tmp_path / "ctx")
        hooks = make_hooks(ctx_dir)
        stop_cmds = self._stop_commands(hooks)
        assert any("usage_tracker.py" in c for c in stop_cmds), (
            f"usage_tracker.py not found in Stop hooks: {stop_cmds}"
        )

    def test_stop_contains_quality_on_session_end(self, tmp_path: Path) -> None:
        ctx_dir = str(tmp_path / "ctx")
        hooks = make_hooks(ctx_dir)
        stop_cmds = self._stop_commands(hooks)
        assert any("quality_on_session_end.py" in c for c in stop_cmds), (
            f"quality_on_session_end.py not found in Stop hooks: {stop_cmds}"
        )

    def test_stop_contains_both_in_generated_settings(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        ctx_dir = str(tmp_path / "ctx")
        _run_inject(ctx_dir, settings_path)

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        stop_cmds = self._stop_commands(data.get("hooks", {}))

        assert any("usage_tracker.py" in c for c in stop_cmds), (
            f"usage_tracker.py missing from persisted Stop hooks: {stop_cmds}"
        )
        assert any("quality_on_session_end.py" in c for c in stop_cmds), (
            f"quality_on_session_end.py missing from persisted Stop hooks: {stop_cmds}"
        )

    def test_stop_hook_count_is_two(self, tmp_path: Path) -> None:
        """Both Stop hook commands must be present (usage_tracker + quality)."""
        ctx_dir = str(tmp_path / "ctx")
        hooks = make_hooks(ctx_dir)
        stop_cmds = self._stop_commands(hooks)
        assert len(stop_cmds) == 2, (
            f"Expected exactly 2 Stop commands; got {len(stop_cmds)}: {stop_cmds}"
        )


# ---------------------------------------------------------------------------
# Fix 3 — shlex.quote protects ctx_dir with special characters
# ---------------------------------------------------------------------------


class TestCtxDirQuoting:
    def test_path_with_spaces_is_quoted(self, tmp_path: Path) -> None:
        ctx_dir = str(tmp_path / "my ctx dir")
        hooks = make_hooks(ctx_dir)
        cmds = _all_commands(hooks)
        for cmd in cmds:
            # If the path contained spaces, the shell command must quote it —
            # a bare space would split the path across two argv tokens.
            if "my ctx dir" in cmd:
                # shlex.quote wraps in single-quotes: 'my ctx dir'
                assert "my ctx dir" not in cmd or "'" in cmd, (
                    f"Path with space is not quoted in: {cmd!r}"
                )

    def test_path_with_dollar_is_safe(self, tmp_path: Path) -> None:
        """A ctx_dir with a literal $ must be quoted so the shell doesn't expand it."""
        import shlex as _shlex
        ctx_dir = "/home/user/$HOME/ctx"
        hooks = make_hooks(ctx_dir)
        cmds = _all_commands(hooks)
        quoted = _shlex.quote(ctx_dir)
        for cmd in cmds:
            if ctx_dir in cmd or quoted in cmd:
                # The raw unquoted path must not appear unless it's the quoted form
                assert f" {ctx_dir}/" not in cmd, (
                    f"Unquoted $ path found in: {cmd!r}"
                )


# ---------------------------------------------------------------------------
# Fix 4 — Atomic write: concurrent writes leave valid JSON
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_single_write_produces_valid_json(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        ctx_dir = str(tmp_path / "ctx")
        _run_inject(ctx_dir, settings_path)
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "hooks" in data

    def test_repeated_writes_leave_valid_json(self, tmp_path: Path) -> None:
        """Running inject twice (idempotent) must leave a valid, parseable JSON."""
        settings_path = tmp_path / "settings.json"
        ctx_dir = str(tmp_path / "ctx")
        _run_inject(ctx_dir, settings_path)
        _run_inject(ctx_dir, settings_path)

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "hooks" in data

    def test_concurrent_writes_leave_valid_json(self, tmp_path: Path) -> None:
        """Concurrent write_settings_atomic calls must not produce a torn file.

        The fix being tested: write goes to a tempfile then os.replace(), so a
        reader never sees a partially-written JSON.  We call write_settings_atomic
        directly (bypassing load_settings) to isolate the write path from the
        Windows file-lock behaviour on the read side.
        """
        from ctx.adapters.claude_code.inject_hooks import write_settings_atomic

        settings_path = tmp_path / "settings.json"
        payload = {"hooks": {"Stop": [{"type": "command", "command": "python x.py"}]}}

        # Seed the file.
        write_settings_atomic(settings_path, payload)

        errors: list[Exception] = []

        def _writer() -> None:
            try:
                for _ in range(10):
                    write_settings_atomic(settings_path, payload)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_writer)
        t2 = threading.Thread(target=_writer)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Thread errors during concurrent writes: {errors}"

        # File must still be valid JSON after concurrent writes.
        raw = settings_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert "hooks" in data

    def test_write_settings_atomic_cleans_up_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If os.replace() raises, the tempfile is removed and no stale .tmp lingers."""
        import os as _os
        from ctx.adapters.claude_code import inject_hooks as _ih

        settings_path = tmp_path / "settings.json"
        original_replace = _os.replace

        call_count = {"n": 0}

        def _failing_replace(src: str, dst: str) -> None:
            call_count["n"] += 1
            raise OSError("simulated disk full")

        monkeypatch.setattr(_os, "replace", _failing_replace)

        with pytest.raises(OSError, match="simulated disk full"):
            _ih.write_settings_atomic(settings_path, {"hooks": {}})

        # No stale .tmp files should remain
        tmp_files = list(tmp_path.glob("settings.json.*.tmp"))
        assert not tmp_files, f"Stale tempfiles not cleaned up: {tmp_files}"
