"""
test_toolbox_hooks.py -- Regression tests for the toolbox_hooks trigger handlers.

Covers:
- _trigger_matches() dispatch for every event type.
- file-save glob matching (including platform-normalized separators).
- run_trigger() emission format: one JSON object per line per matching toolbox.
- Unknown trigger returns 1.
- file-save without --path matches nothing (no emission).
- Multiple matching toolboxes produce multiple lines.
- Return code 0 for normal, 2 only when guardrail verdict file is HIGH/CRITICAL.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import council_runner as cr
import toolbox_config as tc
import toolbox_hooks as th


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect council_runner.RUNS_DIR to a tmp folder."""
    runs = tmp_path / "toolbox-runs"
    monkeypatch.setattr(cr, "RUNS_DIR", runs)
    return runs


@pytest.fixture()
def seeded_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Seed a global toolboxes.json with a mix of trigger configurations
    so we can exercise every _trigger_matches() branch.
    """
    tset = tc.ToolboxSet(
        toolboxes={
            "pre-only": tc.Toolbox(
                name="pre-only",
                description="activates on session-start",
                pre=("python-patterns",),
                post=("code-reviewer",),
                scope=tc.Scope(analysis="diff"),
                trigger=tc.Trigger(slash=True),
            ),
            "session-end-box": tc.Toolbox(
                name="session-end-box",
                post=("code-reviewer",),
                scope=tc.Scope(analysis="diff"),
                trigger=tc.Trigger(session_end=True),
            ),
            "commit-box": tc.Toolbox(
                name="commit-box",
                post=("security-reviewer",),
                scope=tc.Scope(analysis="diff"),
                trigger=tc.Trigger(pre_commit=True),
                guardrail=True,
            ),
            "auth-save-box": tc.Toolbox(
                name="auth-save-box",
                post=("security-reviewer",),
                scope=tc.Scope(analysis="diff"),
                trigger=tc.Trigger(file_save="**/auth/**"),
            ),
            "dormant": tc.Toolbox(
                name="dormant",
                post=("code-reviewer",),
                scope=tc.Scope(analysis="diff"),
                # No triggers enabled
                trigger=tc.Trigger(slash=True),
            ),
        },
        active=(
            "pre-only",
            "session-end-box",
            "commit-box",
            "auth-save-box",
            "dormant",
        ),
    )
    g_path = tmp_path / "global.json"
    tc.save_global(tset, g_path)
    monkeypatch.setattr(tc, "global_config_path", lambda: g_path)
    return g_path


@pytest.fixture()
def force_scope(monkeypatch: pytest.MonkeyPatch):
    """Stub resolve_scope so we don't depend on the host git state."""
    monkeypatch.setattr(
        cr,
        "resolve_scope",
        lambda tb, repo_root, explicit_files=None, graph_edges=None: (
            ["dummy.py"],
            "diff",
        ),
    )


# ── _trigger_matches ────────────────────────────────────────────────────────


def test_trigger_matches_session_start_requires_pre():
    tb_with_pre = tc.Toolbox(name="a", pre=("python-patterns",))
    tb_without_pre = tc.Toolbox(name="b")
    assert th._trigger_matches(tb_with_pre, "session-start", None) is True
    assert th._trigger_matches(tb_without_pre, "session-start", None) is False


def test_trigger_matches_session_end_honors_flag():
    tb_on = tc.Toolbox(name="a", trigger=tc.Trigger(session_end=True))
    tb_off = tc.Toolbox(name="b", trigger=tc.Trigger(session_end=False))
    assert th._trigger_matches(tb_on, "session-end", None) is True
    assert th._trigger_matches(tb_off, "session-end", None) is False


def test_trigger_matches_pre_commit_honors_flag():
    tb_on = tc.Toolbox(name="a", trigger=tc.Trigger(pre_commit=True))
    tb_off = tc.Toolbox(name="b", trigger=tc.Trigger(pre_commit=False))
    assert th._trigger_matches(tb_on, "pre-commit", None) is True
    assert th._trigger_matches(tb_off, "pre-commit", None) is False


def test_trigger_matches_file_save_glob():
    tb = tc.Toolbox(name="a", trigger=tc.Trigger(file_save="**/auth/**"))
    assert th._trigger_matches(tb, "file-save", "src/auth/jwt.py") is True
    assert th._trigger_matches(tb, "file-save", "src/auth/deep/x.py") is True
    assert th._trigger_matches(tb, "file-save", "src/other/file.py") is False


def test_trigger_matches_file_save_handles_windows_separators():
    tb = tc.Toolbox(name="a", trigger=tc.Trigger(file_save="**/auth/**"))
    # Simulate a Windows path coming in with backslashes
    windows_path = "src\\auth\\jwt.py"
    assert th._trigger_matches(tb, "file-save", windows_path) is True


def test_trigger_matches_file_save_requires_path_and_glob():
    tb_no_glob = tc.Toolbox(name="a")
    assert th._trigger_matches(tb_no_glob, "file-save", "x.py") is False

    tb_with_glob = tc.Toolbox(name="a", trigger=tc.Trigger(file_save="*.py"))
    assert th._trigger_matches(tb_with_glob, "file-save", None) is False


def test_trigger_matches_unknown_event_returns_false():
    tb = tc.Toolbox(name="a", pre=("x",), trigger=tc.Trigger(session_end=True))
    assert th._trigger_matches(tb, "bogus-event", None) is False


# ── run_trigger ─────────────────────────────────────────────────────────────


def test_run_trigger_unknown_event_returns_1(capsys):
    rc = th.run_trigger("not-a-real-trigger")
    assert rc == 1
    err = capsys.readouterr().err
    assert "Unknown trigger" in err


def test_run_trigger_session_start_emits_matching_toolbox(
    seeded_global, tmp_runs, force_scope
):
    buf = io.StringIO()
    rc = th.run_trigger("session-start", stream=buf)
    assert rc == 0
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    # Only pre-only has a non-empty pre list
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["toolbox"] == "pre-only"
    assert payload["trigger"] == "session-start"
    assert payload["agents"] == ["code-reviewer"]
    assert payload["source"] in {"fresh", "cached"}
    assert payload["guardrail"] is False
    assert Path(payload["plan_file"]).exists()


def test_run_trigger_session_end_matches_only_flagged(
    seeded_global, tmp_runs, force_scope
):
    buf = io.StringIO()
    rc = th.run_trigger("session-end", stream=buf)
    assert rc == 0
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["toolbox"] == "session-end-box"


def test_run_trigger_pre_commit_emits_without_verdict(
    seeded_global, tmp_runs, force_scope
):
    """No verdict file on disk => no guardrail violation, rc == 0."""
    buf = io.StringIO()
    rc = th.run_trigger("pre-commit", stream=buf)
    assert rc == 0
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["toolbox"] == "commit-box"
    assert payload["guardrail"] is True


def test_run_trigger_pre_commit_blocks_on_high_verdict(
    seeded_global, tmp_runs, force_scope
):
    """Pre-existing verdict file flagged HIGH => rc == 2."""
    buf = io.StringIO()
    # First run seeds the plan file so we know where the verdict belongs
    th.run_trigger("pre-commit", stream=buf)
    payload = json.loads(buf.getvalue().splitlines()[0])
    plan_file = Path(payload["plan_file"])
    verdict_file = plan_file.with_suffix(".verdict.json")
    verdict_file.write_text(
        json.dumps({"level": "HIGH", "notes": "found a critical bug"}),
        encoding="utf-8",
    )

    # Second run should honor the verdict and return 2
    buf2 = io.StringIO()
    rc = th.run_trigger("pre-commit", stream=buf2)
    assert rc == 2


def test_run_trigger_pre_commit_ignores_low_verdict(
    seeded_global, tmp_runs, force_scope
):
    buf = io.StringIO()
    th.run_trigger("pre-commit", stream=buf)
    payload = json.loads(buf.getvalue().splitlines()[0])
    verdict_file = Path(payload["plan_file"]).with_suffix(".verdict.json")
    verdict_file.write_text(
        json.dumps({"level": "LOW"}), encoding="utf-8"
    )

    rc = th.run_trigger("pre-commit", stream=io.StringIO())
    assert rc == 0


def test_run_trigger_pre_commit_corrupt_verdict_does_not_block(
    seeded_global, tmp_runs, force_scope
):
    buf = io.StringIO()
    th.run_trigger("pre-commit", stream=buf)
    payload = json.loads(buf.getvalue().splitlines()[0])
    verdict_file = Path(payload["plan_file"]).with_suffix(".verdict.json")
    verdict_file.write_text("not json {{", encoding="utf-8")

    rc = th.run_trigger("pre-commit", stream=io.StringIO())
    assert rc == 0


def test_run_trigger_file_save_matches_glob(
    seeded_global, tmp_runs, force_scope
):
    buf = io.StringIO()
    rc = th.run_trigger("file-save", file_path="src/auth/jwt.py", stream=buf)
    assert rc == 0
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["toolbox"] == "auth-save-box"


def test_run_trigger_file_save_without_path_emits_nothing(
    seeded_global, tmp_runs, force_scope
):
    buf = io.StringIO()
    rc = th.run_trigger("file-save", file_path=None, stream=buf)
    assert rc == 0
    assert buf.getvalue() == ""


def test_run_trigger_file_save_non_matching_path_emits_nothing(
    seeded_global, tmp_runs, force_scope
):
    buf = io.StringIO()
    rc = th.run_trigger("file-save", file_path="README.md", stream=buf)
    assert rc == 0
    assert buf.getvalue() == ""


def test_run_trigger_multiple_matches_emit_multiple_lines(
    tmp_path, tmp_runs, monkeypatch, force_scope
):
    """Two session-end toolboxes both fire => two JSON lines."""
    tset = tc.ToolboxSet(
        toolboxes={
            "a": tc.Toolbox(
                name="a", post=("x",),
                scope=tc.Scope(analysis="diff"),
                trigger=tc.Trigger(session_end=True),
            ),
            "b": tc.Toolbox(
                name="b", post=("y",),
                scope=tc.Scope(analysis="diff"),
                trigger=tc.Trigger(session_end=True),
            ),
        },
        active=("a", "b"),
    )
    g_path = tmp_path / "global.json"
    tc.save_global(tset, g_path)
    monkeypatch.setattr(tc, "global_config_path", lambda: g_path)

    buf = io.StringIO()
    rc = th.run_trigger("session-end", stream=buf)
    assert rc == 0
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    assert len(lines) == 2
    names = {json.loads(ln)["toolbox"] for ln in lines}
    assert names == {"a", "b"}


def test_run_trigger_inactive_toolboxes_are_skipped(
    tmp_path, tmp_runs, monkeypatch, force_scope
):
    """A toolbox in the registry but not in `active` must not fire."""
    tset = tc.ToolboxSet(
        toolboxes={
            "live": tc.Toolbox(
                name="live", post=("x",),
                scope=tc.Scope(analysis="diff"),
                trigger=tc.Trigger(session_end=True),
            ),
            "benched": tc.Toolbox(
                name="benched", post=("y",),
                scope=tc.Scope(analysis="diff"),
                trigger=tc.Trigger(session_end=True),
            ),
        },
        active=("live",),  # benched is not active
    )
    g_path = tmp_path / "global.json"
    tc.save_global(tset, g_path)
    monkeypatch.setattr(tc, "global_config_path", lambda: g_path)

    buf = io.StringIO()
    rc = th.run_trigger("session-end", stream=buf)
    assert rc == 0
    lines = [ln for ln in buf.getvalue().splitlines() if ln]
    assert len(lines) == 1
    assert json.loads(lines[0])["toolbox"] == "live"


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_requires_event(capsys):
    with pytest.raises(SystemExit):
        th.main([])


def test_cli_session_start_runs(
    seeded_global, tmp_runs, force_scope, capsys
):
    rc = th.main(["session-start"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1


def test_cli_file_save_requires_path(capsys):
    with pytest.raises(SystemExit):
        th.main(["file-save"])


def test_cli_file_save_with_path(
    seeded_global, tmp_runs, force_scope, capsys
):
    rc = th.main(["file-save", "--path", "src/auth/x.py"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "auth-save-box" in out
