"""
Process-kill crash-consistency tests for ctx state files.

These tests intentionally terminate subprocesses while they are inside the
write/lock boundaries used by ctx. The invariant is not "the last requested
mutation always appears" after SIGKILL/TerminateProcess. The invariant is that
surviving state remains parseable and subsequent writers are not wedged by
stale advisory locks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[1]


def _python_env(home: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
    return env


def _start_child(code: str, args: list[str], *, home: Path | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_python_env(home),
    )


def _wait_for_file(path: Path, proc: subprocess.Popen[str], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=1)
            pytest.fail(
                f"child exited before {path.name} appeared\n"
                f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.025)
    proc.kill()
    stdout, stderr = proc.communicate(timeout=5)
    pytest.fail(
        f"timed out waiting for {path.name}\n"
        f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
    )


def _kill_and_wait(proc: subprocess.Popen[str]) -> None:
    proc.kill()
    proc.communicate(timeout=10)
    assert proc.returncode is not None
    assert proc.returncode != 0


def test_atomic_json_kill_before_replace_preserves_previous_complete_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"version": "old"}) + "\n", encoding="utf-8")
    ready = tmp_path / "before-replace.ready"

    proc = _start_child(
        """
        import json
        import time
        from pathlib import Path

        from ctx.utils import _fs_utils

        target = Path(__import__("sys").argv[1])
        ready = Path(__import__("sys").argv[2])
        real_replace = _fs_utils._replace_with_retry

        def pause_before_replace(src, dst, *, attempts=10, delay=0.05):
            ready.write_text("ready", encoding="utf-8")
            time.sleep(30)
            real_replace(src, dst, attempts=attempts, delay=delay)

        _fs_utils._replace_with_retry = pause_before_replace
        _fs_utils.atomic_write_json(target, {"version": "new", "payload": list(range(2000))})
        """,
        [str(target), str(ready)],
    )

    _wait_for_file(ready, proc)
    _kill_and_wait(proc)

    assert json.loads(target.read_text(encoding="utf-8")) == {"version": "old"}


def test_atomic_json_kill_after_replace_leaves_complete_new_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "state.json"
    target.write_text(json.dumps({"version": "old"}) + "\n", encoding="utf-8")
    ready = tmp_path / "after-replace.ready"
    expected = {"version": "new", "payload": list(range(2000))}

    proc = _start_child(
        """
        import time
        from pathlib import Path

        from ctx.utils import _fs_utils

        target = Path(__import__("sys").argv[1])
        ready = Path(__import__("sys").argv[2])
        real_replace = _fs_utils._replace_with_retry

        def replace_then_pause(src, dst, *, attempts=10, delay=0.05):
            real_replace(src, dst, attempts=attempts, delay=delay)
            ready.write_text("ready", encoding="utf-8")
            time.sleep(30)

        _fs_utils._replace_with_retry = replace_then_pause
        _fs_utils.atomic_write_json(target, {"version": "new", "payload": list(range(2000))})
        """,
        [str(target), str(ready)],
    )

    _wait_for_file(ready, proc)
    _kill_and_wait(proc)

    assert json.loads(target.read_text(encoding="utf-8")) == expected


def test_manifest_lock_releases_after_killed_writer(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    manifest_path = home / ".claude" / "skill-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps({
            "load": [{"skill": "baseline", "entity_type": "skill"}],
            "unload": [],
            "warnings": [],
        }),
        encoding="utf-8",
    )
    ready = tmp_path / "manifest-write.ready"

    proc = _start_child(
        """
        import time
        from pathlib import Path

        from ctx.adapters.claude_code import skill_loader

        ready = Path(__import__("sys").argv[1])
        real_write = skill_loader._atomic_write_text

        def pause_before_manifest_write(path, text, encoding="utf-8"):
            ready.write_text("ready", encoding="utf-8")
            time.sleep(30)
            real_write(path, text, encoding=encoding)

        skill_loader._atomic_write_text = pause_before_manifest_write
        skill_loader.update_manifest("killed-before-write", entity_type="skill")
        """,
        [str(ready)],
        home=home,
    )

    _wait_for_file(ready, proc)
    _kill_and_wait(proc)

    follow_up = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ctx.adapters.claude_code import skill_loader; "
            "skill_loader.update_manifest('survivor', entity_type='skill')",
        ],
        env=_python_env(home),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert follow_up.returncode == 0, follow_up.stderr

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    loaded = {(entry["skill"], entry.get("entity_type", "skill")) for entry in data["load"]}
    assert ("baseline", "skill") in loaded
    assert ("survivor", "skill") in loaded
    assert ("killed-before-write", "skill") not in loaded


def test_wiki_page_lock_releases_after_killed_writer(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    ready = tmp_path / "wiki-write.ready"

    setup = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from ctx.core.wiki import wiki_sync; "
            f"wiki_sync.ensure_wiki(Path({str(wiki)!r}))",
        ],
        env=_python_env(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert setup.returncode == 0, setup.stderr

    proc = _start_child(
        """
        import time
        from pathlib import Path

        from ctx.core.wiki import wiki_sync

        wiki = Path(__import__("sys").argv[1])
        ready = Path(__import__("sys").argv[2])
        real_write = wiki_sync.atomic_write_text

        def pause_before_wiki_write(path, text, encoding="utf-8"):
            if path.name == "crash-skill.md":
                ready.write_text("ready", encoding="utf-8")
                time.sleep(30)
            real_write(path, text, encoding=encoding)

        wiki_sync.atomic_write_text = pause_before_wiki_write
        wiki_sync.upsert_skill_page(
            wiki,
            "crash-skill",
            {"reason": "crash test", "confidence": 1.0, "repo": "repo"},
            subject_type="skills",
        )
        """,
        [str(wiki), str(ready)],
    )

    _wait_for_file(ready, proc)
    _kill_and_wait(proc)

    follow_up = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from ctx.core.wiki import wiki_sync; "
            f"wiki_sync.upsert_skill_page(Path({str(wiki)!r}), 'crash-skill', "
            "{'reason': 'after kill', 'confidence': 1.0, 'repo': 'repo'}, "
            "subject_type='skills')",
        ],
        env=_python_env(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert follow_up.returncode == 0, follow_up.stderr

    page = wiki / "entities" / "skills" / "crash-skill.md"
    content = page.read_text(encoding="utf-8")
    assert "title: crash-skill" in content
    assert "status: installed" in content
