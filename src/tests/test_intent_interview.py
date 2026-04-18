"""
test_intent_interview.py -- Regression tests for intent_interview.

Covers:
- RepoState detection: blank non-git, empty git repo, populated git repo.
- Language scoring: extension map + marker map bumps.
- Question construction: starters always; suggestions only when profile has them.
- Answer parsing: comma-separated lists, unknown starters dropped with notes.
- compose_result: skipped path, analysis override patches proposed suggestions.
- run_interactive: canned input_fn drives the flow and honours 'skip'.
- run_noninteractive: dict answers fold through compose_result cleanly.
- Presets: resolve answers; unknown preset raises KeyError.
- apply_result: activates starters via template load, registers suggestions.
- apply_result: respects existing toolboxes (no overwrite, activation only).
- CLI: detect + init subcommands emit JSON.
- CLI: --skip short-circuits cleanly and reports applied=False.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import intent_interview as ii
import toolbox_config as tc
import behavior_miner as bm


# ── Helpers ─────────────────────────────────────────────────────────────────


def _mkfile(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _init_git(repo: Path, commits: int = 0) -> None:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", "/dev/null"],
        check=True, env=env,
    )
    for i in range(commits):
        f = repo / f"f{i}.txt"
        f.write_text(str(i), encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", f.name], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "--no-verify",
             "-m", f"chore: c{i}"],
            check=True, env=env,
        )


@pytest.fixture(autouse=True)
def isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Redirect the global toolbox config path into tmp_path for every test so
    nothing in the suite touches the user's real ~/.claude/toolboxes.json.
    """
    cfg = tmp_path / "global-toolboxes.json"
    monkeypatch.setattr(tc, "global_config_path", lambda: cfg)
    return cfg


@pytest.fixture()
def blank_repo(tmp_path: Path) -> Path:
    """A directory with no git init at all."""
    d = tmp_path / "blank"
    d.mkdir()
    return d


@pytest.fixture()
def empty_git_repo(tmp_path: Path) -> Path:
    """A git repo with zero commits."""
    d = tmp_path / "empty_git"
    d.mkdir()
    _init_git(d, commits=0)
    return d


@pytest.fixture()
def python_repo(tmp_path: Path) -> Path:
    """A git repo with python files + pyproject marker + a few commits."""
    d = tmp_path / "py_repo"
    d.mkdir()
    _mkfile(d / "pyproject.toml", "[project]\nname='x'\n")
    _mkfile(d / "src" / "a.py", "print('a')\n")
    _mkfile(d / "src" / "b.py", "print('b')\n")
    _mkfile(d / "Dockerfile", "FROM python:3.12\n")
    _init_git(d, commits=2)
    # Also track the pre-existing files
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(d), "commit", "-q", "--no-verify",
         "-m", "feat: seed"], check=True, env=env,
    )
    return d


@pytest.fixture()
def profile_with_suggestions() -> bm.BehaviorProfile:
    return bm.BehaviorProfile(
        total_intent_events=10,
        total_commits=5,
        co_invocation_pairs=(),
        skill_cadence=(),
        file_types=(),
        commit_types=(),
        suggestions=(
            bm.Suggestion(
                kind="co-invocation",
                rationale="a+b seen 4x",
                evidence=4,
                proposed={
                    "name": "a-b-bundle",
                    "description": "bundle",
                    "pre": [],
                    "post": [],
                    "scope": {"signals": ["a", "b"], "analysis": "dynamic"},
                },
            ),
            bm.Suggestion(
                kind="commit-type",
                rationale="feat 50%",
                evidence=5,
                proposed={
                    "name": "feat-review",
                    "description": "feat council",
                    "pre": [],
                    "post": [],
                    "scope": {"analysis": "diff"},
                    "trigger": {"pre_commit": True},
                },
            ),
        ),
        generated_at=1.0,
    )


# ── State detection ─────────────────────────────────────────────────────────


def test_detect_state_non_git_is_blank(blank_repo: Path):
    state = ii.detect_state(blank_repo)
    assert state.is_git_repo is False
    assert state.commit_count == 0
    assert state.is_blank is True


def test_detect_state_empty_git_repo_is_blank(empty_git_repo: Path):
    state = ii.detect_state(empty_git_repo)
    assert state.is_git_repo is True
    assert state.commit_count == 0
    assert state.is_blank is True


def test_detect_state_python_repo_is_populated(python_repo: Path):
    state = ii.detect_state(python_repo)
    assert state.is_git_repo is True
    assert state.commit_count >= 1
    langs = dict(state.top_languages)
    assert "python" in langs
    assert "docker" in langs  # marker bumped
    assert "pyproject.toml" in state.detected_markers
    assert "Dockerfile" in state.detected_markers
    assert state.is_blank is False


def test_detect_state_respects_existing_toolbox_config(python_repo: Path,
                                                       tmp_path: Path,
                                                       monkeypatch):
    cfg = tmp_path / "global.json"
    tset = tc.ToolboxSet(
        toolboxes={"x": tc.Toolbox(name="x")},
        active=("x",),
    )
    tc.save_global(tset, cfg)
    monkeypatch.setattr(tc, "global_config_path", lambda: cfg)

    state = ii.detect_state(python_repo)
    assert state.has_toolbox_config is True
    assert state.existing_active == ("x",)


def test_score_languages_counts_extensions():
    c = ii._score_languages(["a.py", "b.py", "c.ts", "noext"])
    assert c["python"] == 2
    assert c["typescript"] == 1


# ── Question construction ───────────────────────────────────────────────────


def test_build_questions_always_includes_starters(blank_repo: Path):
    state = ii.detect_state(blank_repo)
    qs = ii.build_questions(state, profile=None)
    ids = [q.id for q in qs]
    assert "starters" in ids
    assert "analysis" in ids
    # No profile => no suggestions question
    assert "suggestions" not in ids


def test_build_questions_includes_suggestions_when_profile_has_any(
    blank_repo: Path, profile_with_suggestions: bm.BehaviorProfile,
):
    state = ii.detect_state(blank_repo)
    qs = ii.build_questions(state, profile=profile_with_suggestions)
    ids = [q.id for q in qs]
    assert "suggestions" in ids
    sugg_q = next(q for q in qs if q.id == "suggestions")
    # 2 suggestions => 2 choices, labels mention name and evidence
    assert len(sugg_q.choices) == 2
    assert any("a-b-bundle" in label for _, label in sugg_q.choices)


def test_starter_choices_marks_active_toolboxes():
    choices = ii._starter_choices(("ship-it",))
    d = dict(choices)
    assert "already active" in d["ship-it"]
    assert "already active" not in d["security-sweep"]


# ── compose_result ──────────────────────────────────────────────────────────


def test_compose_result_filters_unknown_starters(blank_repo: Path):
    state = ii.detect_state(blank_repo)
    result = ii.compose_result(
        state, profile=None,
        answers={"starters": "ship-it,bogus,security-sweep"},
    )
    assert result.activated == ("ship-it", "security-sweep")
    assert any("bogus" in n for n in result.notes)


def test_compose_result_skipped_short_circuits(blank_repo: Path):
    state = ii.detect_state(blank_repo)
    result = ii.compose_result(state, None, {}, skipped=True)
    assert result.skipped is True
    assert result.activated == ()
    assert result.accepted_suggestions == ()


def test_compose_result_resolves_suggestion_indices(
    blank_repo: Path, profile_with_suggestions: bm.BehaviorProfile,
):
    state = ii.detect_state(blank_repo)
    result = ii.compose_result(
        state, profile_with_suggestions,
        answers={"starters": "", "suggestions": "1,99", "analysis": "dynamic"},
    )
    # 1 valid index, 99 is out of range -> dropped silently
    assert len(result.accepted_suggestions) == 1
    assert result.accepted_suggestions[0]["name"] == "a-b-bundle"


def test_compose_result_applies_analysis_override(
    blank_repo: Path, profile_with_suggestions: bm.BehaviorProfile,
):
    state = ii.detect_state(blank_repo)
    result = ii.compose_result(
        state, profile_with_suggestions,
        answers={"starters": "", "suggestions": "2", "analysis": "full"},
    )
    assert result.accepted_suggestions[0]["scope"]["analysis"] == "full"


# ── Interactive driver ──────────────────────────────────────────────────────


def test_run_interactive_with_canned_input(blank_repo: Path, capsys):
    state = ii.detect_state(blank_repo)
    answers_iter = iter(["ship-it,security-sweep", "dynamic"])

    def fake_input(prompt: str) -> str:
        return next(answers_iter)

    result = ii.run_interactive(state, None, input_fn=fake_input)
    assert result.skipped is False
    assert result.activated == ("ship-it", "security-sweep")


def test_run_interactive_honors_skip_keyword(blank_repo: Path):
    state = ii.detect_state(blank_repo)

    def fake_input(prompt: str) -> str:
        return "skip"

    result = ii.run_interactive(state, None, input_fn=fake_input)
    assert result.skipped is True
    assert result.activated == ()


def test_run_interactive_handles_eof(blank_repo: Path):
    state = ii.detect_state(blank_repo)

    def fake_input(prompt: str) -> str:
        raise EOFError

    # With EOF, all answers default to None/defaults; the default on
    # "starters" for a blank repo is "ship-it,security-sweep".
    result = ii.run_interactive(state, None, input_fn=fake_input)
    assert result.skipped is False
    assert "ship-it" in result.activated


# ── Non-interactive driver ──────────────────────────────────────────────────


def test_run_noninteractive_passes_answers_through(blank_repo: Path):
    state = ii.detect_state(blank_repo)
    result = ii.run_noninteractive(
        state, None,
        {"starters": "docs-review", "analysis": "diff"},
    )
    assert result.activated == ("docs-review",)
    assert result.skipped is False


# ── Presets ─────────────────────────────────────────────────────────────────


def test_preset_answers_blank():
    ans = ii.preset_answers("blank")
    assert "ship-it" in ans["starters"]
    assert ans["analysis"] == "dynamic"


def test_preset_answers_unknown_raises():
    with pytest.raises(KeyError):
        ii.preset_answers("does-not-exist")


# ── apply_result ────────────────────────────────────────────────────────────


def test_apply_result_skipped_returns_base_unchanged():
    base = tc.ToolboxSet.empty()
    result = ii.InterviewResult(
        activated=(), accepted_suggestions=(), skipped=True, notes=(),
    )
    out = ii.apply_result(result, base)
    assert out is base


def test_apply_result_loads_starter_templates():
    base = tc.ToolboxSet.empty()
    result = ii.InterviewResult(
        activated=("ship-it", "security-sweep"),
        accepted_suggestions=(),
        skipped=False,
    )
    out = ii.apply_result(result, base)
    assert "ship-it" in out.toolboxes
    assert "security-sweep" in out.toolboxes
    assert "ship-it" in out.active
    assert "security-sweep" in out.active


def test_apply_result_registers_new_suggestions():
    base = tc.ToolboxSet.empty()
    result = ii.InterviewResult(
        activated=(),
        accepted_suggestions=(
            {"name": "a-b-bundle", "description": "x",
             "scope": {"analysis": "dynamic"}},
        ),
        skipped=False,
    )
    out = ii.apply_result(result, base)
    assert "a-b-bundle" in out.toolboxes
    assert "a-b-bundle" in out.active


def test_apply_result_respects_existing_toolbox_names():
    pre = tc.Toolbox(name="ship-it", description="user-customised")
    base = tc.ToolboxSet(toolboxes={"ship-it": pre}, active=())
    result = ii.InterviewResult(
        activated=("ship-it",),
        accepted_suggestions=(),
        skipped=False,
    )
    out = ii.apply_result(result, base)
    # The user's description is preserved -- template not re-loaded over it.
    assert out.toolboxes["ship-it"].description == "user-customised"
    assert "ship-it" in out.active


def test_apply_result_drops_nameless_suggestions():
    base = tc.ToolboxSet.empty()
    result = ii.InterviewResult(
        activated=(),
        accepted_suggestions=({"description": "no name here"},),
        skipped=False,
    )
    out = ii.apply_result(result, base)
    assert out.toolboxes == {}


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_detect_emits_json(blank_repo: Path, capsys):
    code = ii.main(["detect", "--repo", str(blank_repo)])
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["is_git_repo"] is False


def test_cli_init_skip_emits_empty_result(blank_repo: Path, capsys):
    code = ii.main(["init", "--repo", str(blank_repo), "--skip"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["skipped"] is True
    assert payload["applied"] is False


def test_cli_init_preset_writes_nothing_without_apply(blank_repo: Path, capsys):
    code = ii.main(
        ["init", "--repo", str(blank_repo), "--preset", "blank"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert "ship-it" in payload["result"]["activated"]


def test_cli_init_apply_persists_to_global_config(
    blank_repo: Path, capsys, isolate_global_config: Path,
):
    code = ii.main([
        "init", "--repo", str(blank_repo),
        "--non-interactive", "--starters", "ship-it",
        "--analysis", "dynamic", "--apply",
    ])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    # Config file exists and contains ship-it
    written = json.loads(isolate_global_config.read_text(encoding="utf-8"))
    assert "ship-it" in written["toolboxes"]
    assert "ship-it" in written["active"]
