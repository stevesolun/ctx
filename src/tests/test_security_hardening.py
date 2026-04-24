"""
Security-hardening tests covering:

1. plan_hash path-traversal rejection in council_runner + toolbox_verdict.
2. Cross-process advisory file-lock behavior.
3. YAML validate-path misroute (toolbox.validate now reads the given file,
   not .toolbox.yaml in its parent directory).

Each test targets a specific fix from the Phase 4b-6 code-review pass.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import council_runner as cr  # noqa: E402
import toolbox as tb  # noqa: E402
import toolbox_verdict as tv  # noqa: E402
from ctx.utils._file_lock import file_lock  # noqa: E402


# ── plan_hash validation ────────────────────────────────────────────────────

BAD_HASHES = [
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "a/b",
    "a\\b",
    "foo.bar",
    "foo:bar",
    "",
    "x" * 65,           # too long
    "bad hash",
    "has\nnewline",
]


@pytest.mark.parametrize("bad", BAD_HASHES)
def test_council_runner_rejects_bad_plan_hash(bad):
    with pytest.raises(ValueError, match="invalid plan_hash"):
        cr._validate_plan_hash(bad)


@pytest.mark.parametrize("bad", BAD_HASHES)
def test_toolbox_verdict_rejects_bad_plan_hash(bad):
    with pytest.raises(ValueError, match="invalid plan_hash"):
        tv._validate_plan_hash(bad)


@pytest.mark.parametrize("good", ["a", "abc123", "plan-1", "plan_2",
                                   "0123456789abcdef", "A" * 64])
def test_good_plan_hashes_accepted(good):
    assert cr._validate_plan_hash(good) == good
    assert tv._validate_plan_hash(good) == good


def test_verdict_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(tv, "RUNS_DIR", tmp_path)
    with pytest.raises(ValueError):
        tv.verdict_path("../../etc/passwd")


def test_council_find_cached_rejects_traversal():
    with pytest.raises(ValueError):
        cr._find_cached_plan("../escape", window_seconds=60)


def test_verdict_from_dict_rejects_traversal():
    with pytest.raises(ValueError):
        tv.Verdict.from_dict({"plan_hash": "../oops", "findings": []})


def test_plan_from_dict_rejects_traversal():
    with pytest.raises(ValueError):
        cr._plan_from_dict({"toolbox": "t", "plan_hash": "../x"}, source="s")


# ── file_lock cross-thread serialization ────────────────────────────────────

def test_file_lock_serializes_concurrent_writers(tmp_path):
    """Two threads both incrementing under the lock must see +2, not +1."""
    target = tmp_path / "counter.json"
    target.write_text(json.dumps({"n": 0}))

    def bump():
        with file_lock(target):
            data = json.loads(target.read_text())
            current = data["n"]
            time.sleep(0.05)  # widen the race window
            target.write_text(json.dumps({"n": current + 1}))

    t1 = threading.Thread(target=bump)
    t2 = threading.Thread(target=bump)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert json.loads(target.read_text())["n"] == 2


def test_file_lock_creates_parent(tmp_path):
    target = tmp_path / "nested" / "deep" / "file.json"
    with file_lock(target):
        pass
    # Only the lock file and its parent dir need exist.
    assert target.parent.exists()


# ── toolbox.validate no longer misroutes YAML paths ─────────────────────────

def test_validate_reads_supplied_yaml_file(tmp_path, capsys, monkeypatch):
    """
    Prior behavior: `toolbox validate <path>.yaml` silently validated
    <path.parent>/.toolbox.yaml instead of the supplied path. This test
    pins the new behavior: the supplied file is the one validated.
    """
    # Create a valid-looking YAML file at a non-default location.
    yaml_path = tmp_path / "custom.yaml"
    yaml_path.write_text(
        "version: 1\n"
        "toolboxes:\n"
        "  ship-it:\n"
        "    description: 'Test'\n"
        "    post: ['code-reviewer']\n"
        "active: ['ship-it']\n",
        encoding="utf-8",
    )

    # Ensure no stray .toolbox.yaml exists in the parent directory.
    stray = tmp_path / ".toolbox.yaml"
    assert not stray.exists()

    args = tb.argparse.Namespace(path=str(yaml_path))
    rc = tb.cmd_validate(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 toolbox(es)" in out
