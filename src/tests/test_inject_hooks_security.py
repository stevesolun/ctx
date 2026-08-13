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
import os
import sys
import threading
import time
from pathlib import Path

import pytest

# Ensure src/ is importable
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ctx.adapters.claude_code import inject_hooks  # noqa: E402
from ctx.adapters.claude_code.inject_hooks import make_hooks, merge_hooks  # noqa: E402


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


def _all_modules(hooks_block: dict) -> list[str]:
    """Return every module invoked through ``python -m`` hook commands."""
    modules: list[str] = []
    for cmd in _all_commands(hooks_block):
        parts = cmd.split()
        if "-m" in parts:
            idx = parts.index("-m")
            if idx + 1 < len(parts):
                modules.append(parts[idx + 1])
    return modules


def _run_inject(ctx_dir: str, settings_path: Path) -> None:
    """Run the full inject pipeline (load → merge → atomic write)."""
    from ctx.adapters.claude_code.inject_hooks import install_hooks_file

    install_hooks_file(settings_path, ctx_dir)


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
        assert "$CLAUDE_TOOL_INPUT" not in raw, "$CLAUDE_TOOL_INPUT found in written settings.json"
        assert "$CLAUDE_TOOL_NAME" not in raw, "$CLAUDE_TOOL_NAME found in written settings.json"

    def test_from_stdin_flag_present_in_posttooluse_commands(self, tmp_path: Path) -> None:
        """PostToolUse commands that replaced env-var args must use --from-stdin."""
        ctx_dir = str(tmp_path / "ctx")
        hooks = make_hooks(ctx_dir)
        post_tool_entries = hooks.get("PostToolUse", [])
        assert post_tool_entries, "PostToolUse block must not be empty"

        # context_monitor and skill_add_detector commands must carry --from-stdin
        sub_hooks = post_tool_entries[0].get("hooks", [])
        cmds_with_stdin = [
            h["command"]
            for h in sub_hooks
            if isinstance(h, dict) and "--from-stdin" in h.get("command", "")
        ]
        assert len(cmds_with_stdin) >= 2, (
            f"Expected at least 2 --from-stdin commands; found {len(cmds_with_stdin)}: "
            f"{cmds_with_stdin}"
        )

    def test_merge_hooks_repairs_partial_posttooluse_matcher(
        self,
        tmp_path: Path,
    ) -> None:
        new_hooks = make_hooks(str(tmp_path / "ctx"))
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [new_hooks["PostToolUse"][0]["hooks"][0]],
                    },
                ],
            },
        }

        merged = merge_hooks(existing, new_hooks)

        commands = _all_commands({"PostToolUse": merged["hooks"]["PostToolUse"]})
        assert any("context_monitor" in command for command in commands)
        assert any("skill_add_detector" in command for command in commands)
        assert any("bundle_orchestrator" in command for command in commands)


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
        assert any("-m usage_tracker" in c for c in stop_cmds), (
            f"usage_tracker module not found in Stop hooks: {stop_cmds}"
        )

    def test_stop_contains_quality_on_session_end(self, tmp_path: Path) -> None:
        ctx_dir = str(tmp_path / "ctx")
        hooks = make_hooks(ctx_dir)
        stop_cmds = self._stop_commands(hooks)
        assert any(
            "ctx.adapters.claude_code.hooks.lifecycle_hooks quality-on-session-end" in c
            for c in stop_cmds
        ), f"quality hook module not found in Stop hooks: {stop_cmds}"

    def test_stop_contains_both_in_generated_settings(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        ctx_dir = str(tmp_path / "ctx")
        _run_inject(ctx_dir, settings_path)

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        stop_cmds = self._stop_commands(data.get("hooks", {}))

        assert any("-m usage_tracker" in c for c in stop_cmds), (
            f"usage_tracker module missing from persisted Stop hooks: {stop_cmds}"
        )
        assert any(
            "ctx.adapters.claude_code.hooks.lifecycle_hooks quality-on-session-end" in c
            for c in stop_cmds
        ), f"quality hook module missing from persisted Stop hooks: {stop_cmds}"

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
                assert f" {ctx_dir}/" not in cmd, f"Unquoted $ path found in: {cmd!r}"

    def test_python_path_with_spaces_uses_posix_shell_quoting(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            inject_hooks.sys,
            "executable",
            "/opt/Python Runtime/bin/python",
        )

        cmd = inject_hooks._module_cmd("usage_tracker", "--sync")

        assert cmd == "'/opt/Python Runtime/bin/python' -m usage_tracker --sync"


class TestPackagedHookCommands:
    def test_commands_use_importable_modules_not_repo_paths(self, tmp_path: Path) -> None:
        hooks = make_hooks(str(tmp_path / "ctx"))
        cmds = _all_commands(hooks)

        assert cmds
        assert all(".py" not in cmd for cmd in cmds)
        assert all("/../hooks/" not in cmd and "\\..\\hooks\\" not in cmd for cmd in cmds)
        assert all(" 2>/dev/null" not in cmd and "|| true" not in cmd for cmd in cmds)

        modules = _all_modules(hooks)
        assert {
            "ctx.adapters.claude_code.query_handler",
            "ctx.adapters.claude_code.hooks.context_monitor",
            "skill_add_detector",
            "ctx.adapters.claude_code.hooks.bundle_orchestrator",
            "usage_tracker",
            "ctx.adapters.claude_code.hooks.lifecycle_hooks",
        } <= set(modules)

    def test_user_prompt_submit_hook_is_bounded_and_has_no_matcher(
        self,
        tmp_path: Path,
    ) -> None:
        entry = make_hooks(str(tmp_path / "ctx"))["UserPromptSubmit"]

        assert entry == [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": inject_hooks._module_cmd(
                            "ctx.adapters.claude_code.query_handler"
                        ),
                        "timeout": 10,
                    }
                ]
            }
        ]


class TestStrictLockedUpdate:
    @pytest.mark.parametrize(
        "raw",
        [
            b"{",
            b"[]",
            b'{"hooks":{},"hooks":{}}',
            b'{"hooks":[]}',
            b'{"hooks":{"Stop":[42]}}',
            b'{"hooks":{"Stop":[{"hooks":[42]}]}}',
            b'{"hooks":{"Stop":[{"hooks":[{}]}]}}',
            b'{"hooks":{"Stop":[{"hooks":[{"type":"command"}]}]}}',
            b"\xff",
        ],
    )
    def test_malformed_settings_are_preserved_byte_for_byte(
        self,
        tmp_path: Path,
        raw: bytes,
    ) -> None:
        path = tmp_path / "settings.json"
        path.write_bytes(raw)

        with pytest.raises(ValueError):
            inject_hooks.install_hooks_file(path, str(tmp_path / "ctx"))

        assert path.read_bytes() == raw

    def test_second_install_does_not_rewrite_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        _run_inject(str(tmp_path / "ctx"), path)
        before = (path.read_bytes(), path.stat().st_mtime_ns)

        _run_inject(str(tmp_path / "ctx"), path)

        assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    def test_install_replaces_prior_query_handler_interpreter_and_fields(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "http",
                                        "command": (
                                            "/old/python -m ctx.adapters.claude_code.query_handler"
                                        ),
                                        "timeout": 999_999,
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        installed = inject_hooks.install_hooks_file(path, str(tmp_path / "ctx"))

        assert (
            installed["hooks"]["UserPromptSubmit"]
            == make_hooks(  # type: ignore[index]
                str(tmp_path / "ctx")
            )["UserPromptSubmit"]
        )

    def test_locked_read_modify_write_preserves_concurrent_union(self, tmp_path: Path) -> None:
        from ctx.adapters.hook_config import update_json_object_locked

        path = tmp_path / "settings.json"

        def slow_update(value: dict[str, object]) -> dict[str, object]:
            value["first"] = True
            time.sleep(0.05)
            return value

        def second_update(value: dict[str, object]) -> dict[str, object]:
            value["second"] = True
            return value

        first = threading.Thread(target=update_json_object_locked, args=(path, slow_update))
        second = threading.Thread(target=update_json_object_locked, args=(path, second_update))
        first.start()
        time.sleep(0.01)
        second.start()
        first.join()
        second.join()

        assert json.loads(path.read_text()) == {"first": True, "second": True}

    def test_lock_companion_replacement_cannot_bypass_mutual_exclusion(
        self,
        tmp_path: Path,
    ) -> None:
        from ctx.adapters.hook_config import update_json_object_locked

        path = tmp_path / "settings.json"
        first_inside = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        errors: list[BaseException] = []

        def first_update(value: dict[str, object]) -> dict[str, object]:
            value["first"] = True
            first_inside.set()
            assert release_first.wait(timeout=5)
            return value

        def run_first() -> None:
            try:
                update_json_object_locked(path, first_update)
            except BaseException as error:
                errors.append(error)

        def run_second() -> None:
            try:
                update_json_object_locked(
                    path,
                    lambda value: {**value, "second": True},
                )
            except BaseException as error:
                errors.append(error)
            finally:
                second_done.set()

        first = threading.Thread(target=run_first)
        first.start()
        assert first_inside.wait(timeout=5)
        lock_path = path.with_suffix(path.suffix + ".lock")
        replacement = tmp_path / "replacement.lock"
        replacement.write_bytes(b"")
        try:
            os.replace(replacement, lock_path)
        except PermissionError as error:
            release_first.set()
            first.join(timeout=5)
            pytest.skip(f"platform prevents replacement of an open lock: {error}")

        second = threading.Thread(target=run_second)
        second.start()
        assert not second_done.wait(timeout=0.05)
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert not errors
        assert json.loads(path.read_text()) == {"first": True, "second": True}

    def test_live_parent_replacement_cannot_bypass_path_stable_lock(
        self,
        tmp_path: Path,
    ) -> None:
        from ctx.adapters.hook_config import update_json_object_locked

        parent = tmp_path / "config"
        parent.mkdir()
        path = parent / "settings.json"
        first_inside = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        errors: list[BaseException] = []

        def first_update(value: dict[str, object]) -> dict[str, object]:
            value["first"] = True
            first_inside.set()
            assert release_first.wait(timeout=5)
            return value

        def run_first() -> None:
            try:
                update_json_object_locked(path, first_update)
            except BaseException as error:
                errors.append(error)

        def run_second() -> None:
            try:
                update_json_object_locked(
                    path,
                    lambda value: {**value, "second": True},
                )
            except BaseException as error:
                errors.append(error)
            finally:
                second_done.set()

        first = threading.Thread(target=run_first)
        first.start()
        assert first_inside.wait(timeout=5)
        parent.rename(tmp_path / "moved-config")
        parent.mkdir()
        second = threading.Thread(target=run_second)
        second.start()
        assert not second_done.wait(timeout=0.05)
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

        assert not errors
        assert json.loads(path.read_text()) == {"first": True, "second": True}

    def test_symlinked_parent_is_rejected_before_lock_side_effect(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        parent = tmp_path / "linked"
        try:
            parent.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"directory symlinks unavailable: {error}")

        with pytest.raises((OSError, ValueError)):
            inject_hooks.install_hooks_file(parent / "settings.json", str(tmp_path / "ctx"))

        assert not (target / "settings.json").exists()
        assert not (target / "settings.json.lock").exists()

    def test_official_http_and_mcp_tool_handlers_are_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        http_handler = {
            "type": "http",
            "url": "http://localhost:8080/hooks/pre-tool-use",
            "timeout": 30,
            "headers": {"Authorization": "Bearer $MY_TOKEN"},
            "allowedEnvVars": ["MY_TOKEN"],
        }
        mcp_handler = {
            "type": "mcp_tool",
            "server": "my_server",
            "tool": "security_scan",
            "input": {"file_path": "${tool_input.file_path}"},
        }
        original_group = {
            "matcher": "Write|Edit",
            "hooks": [http_handler, mcp_handler],
        }
        path.write_text(
            json.dumps({"hooks": {"PostToolUse": [original_group]}}),
            encoding="utf-8",
        )

        installed = inject_hooks.install_hooks_file(path, str(tmp_path / "ctx"))

        post_tool = installed["hooks"]["PostToolUse"]  # type: ignore[index]
        assert original_group in post_tool


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
        directly (bypassing load_settings) to isolate the write path.
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

        call_count = {"n": 0}

        def _failing_replace(src: str, dst: str, **_kwargs: object) -> None:
            call_count["n"] += 1
            raise OSError("simulated disk full")

        monkeypatch.setattr(_os, "replace", _failing_replace)

        with pytest.raises(OSError, match="simulated disk full"):
            _ih.write_settings_atomic(settings_path, {"hooks": {}})

        # No stale .tmp files should remain
        tmp_files = list(tmp_path.glob("*settings.json*.tmp"))
        assert not tmp_files, f"Stale tempfiles not cleaned up: {tmp_files}"


def test_a_concurrently_unlinked_target_is_absent_not_a_hardlink(tmp_path, monkeypatch) -> None:
    """A zero link count means the file is gone, not that it is multiply linked.

    Two writers racing through ``write_json_object_atomic`` both call
    ``os.replace``. A stat landing inside another writer's replace window sees
    the old inode with ``st_nlink == 0``. The guard tested ``!= 1``, so each
    writer rejected the other with "cannot be a symlink or hardlink" -- the
    flake that failed on macOS and then on Linux CI. A real hardlink still has
    to be refused, which the test below this one pins.
    """

    from ctx.adapters.hook_config import write_json_object_atomic

    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    real_stat = Path.stat

    def unlinked_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == target:
            return os.stat_result(
                (
                    result.st_mode,
                    result.st_ino,
                    result.st_dev,
                    0,
                    result.st_uid,
                    result.st_gid,
                    result.st_size,
                    result.st_atime,
                    result.st_mtime,
                    result.st_ctime,
                )
            )
        return result

    monkeypatch.setattr(Path, "stat", unlinked_stat)

    write_json_object_atomic(target, {"ok": True})

    monkeypatch.undo()
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_a_real_hardlink_is_still_refused(tmp_path) -> None:
    """The guard's actual purpose, pinned so the fix above cannot erode it."""

    from ctx.adapters.hook_config import write_json_object_atomic

    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    os.link(target, tmp_path / "second-name.json")

    with pytest.raises(ValueError, match="symlink or hardlink"):
        write_json_object_atomic(target, {"ok": True})
