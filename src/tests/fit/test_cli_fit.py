"""What ``ctx fit`` says about a run has to match the run.

Every case here is a place where the command's own account diverged from what
happened: a refusal that arrived as a traceback, an exit code of 0 for work that
was silently dropped, a JSON document whose keys depended on which branch built
it, a real and paid run announcing itself as a simulation, an evidence count
that included tasks which produced no evidence, and a pull request announced but
never opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import ctx.fit.apply as apply_module
import ctx.fit.execution as execution_module
import ctx.fit.experiment as experiment_module
import ctx.fit.live_runner as live_runner_module
import ctx.fit.providers as providers_module
import ctx.fit.recommend as recommend_module
import ctx.fit.tasks as tasks_module
from ctx.cli.fit import (
    _banner,
    _format_dry_run,
    _format_plan,
    _handle_apply,
    _provider_available,
    cmd_fit,
    default_namespace,
)
from ctx.fit.apply import CommandResult
from ctx.fit.candidates import CapabilityMaterial, CandidateConfiguration
from ctx.fit.execution import CandidateOutcome, ExecutionReport, TrialResult
from ctx.fit.experiment import (
    DEFAULT_MODEL,
    EXCHANGE_INPUT_TOKENS,
    EXCHANGE_OUTPUT_TOKENS,
    ModelPrice,
    authorize_experiment,
    resolve_experiment,
    run_experiment,
)
from ctx.fit.profile import build_fit_profile
from ctx.fit.providers import DEFAULT_MAX_ITERATIONS, ProviderUnavailable
from ctx.fit.recommend import RankedCandidate, Recommendation


def _args(repo: Path, **overrides: Any) -> argparse.Namespace:
    args = default_namespace(str(repo))
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


@pytest.fixture
def stubbed_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run a campaign with every collaborator that costs money replaced."""

    monkeypatch.setattr(providers_module, "build_agent_driver", lambda **k: object())
    monkeypatch.setattr(live_runner_module, "make_live_runner", lambda *a, **k: object())


def _winning_recommendation() -> Recommendation:
    return Recommendation(
        schema="ctx.fit.recommendation-v1",
        verdict="recommend-change",
        winner_id="lean",
        ranked=(
            RankedCandidate(
                candidate_id="lean",
                reliability=1.0,
                verified=9,
                scored=9,
                total_cost_usd=0.45,
                capability_count=1,
                qualified=True,
            ),
        ),
        reasoning=("lean verified 9/9 trials at $0.45.",),
        limitations=(),
        confidence="medium",
        simulated=False,
    )


def _winning_candidate() -> CandidateConfiguration:
    material = CapabilityMaterial.from_content(
        capability_id="skill:ctx-python-testing",
        delivery_mode="task-user-context",
        source_identity="test-catalog#skill:ctx-python-testing",
        catalog_entry_digest=hashlib.sha256(b"test-catalog-entry").hexdigest(),
        content="Use the repository's Python test conventions and focused pytest feedback loops.",
    )
    return CandidateConfiguration(
        candidate_id="lean",
        role="recommended",
        capability_ids=("skill:ctx-python-testing",),
        model=DEFAULT_MODEL,
        instructions=(),
        selection_reason="the single highest-ranked capability",
        capability_materials=(material,),
    )


@pytest.mark.parametrize(
    ("credential", "expected_live"),
    (
        ("ANTHROPIC_API_KEY", False),
        ("CTX_FIT_API_KEY", False),
        ("OPENAI_API_KEY", True),
    ),
)
def test_default_model_uses_only_its_matching_credential_for_live_selection(
    monkeypatch: pytest.MonkeyPatch,
    credential: str,
    expected_live: bool,
) -> None:
    """A key for another provider must not silently choose a paid/live run."""

    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CTX_FIT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(credential, "configured-not-authenticated")

    assert _provider_available(DEFAULT_MODEL) is expected_live


def test_live_selection_uses_the_applied_model_resolved_for_the_experiment(
    monkeypatch: pytest.MonkeyPatch,
    repo_with_history,
) -> None:
    """Applied Anthropic + Anthropic key is live even though the default is GPT."""

    pytest.importorskip("litellm")
    repo = repo_with_history(commits=3)
    selected = replace(_winning_candidate(), model="claude-sonnet-4-20250514")
    target = repo / ".ctx" / "fit-configuration.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema": "ctx.fit.applied-configuration-v1",
                "configuration_hash": selected.configuration_hash,
                "candidate": selected.to_dict(),
            }
        ),
        encoding="utf-8",
    )
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CTX_FIT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "configured-not-authenticated")

    experiment = resolve_experiment(build_fit_profile(repo), budget_usd=500.0)

    assert experiment.model == selected.model
    assert _provider_available(experiment.model) is True


def test_cli_binds_live_selection_to_the_resolved_applied_model(
    monkeypatch: pytest.MonkeyPatch,
    repo_with_history,
) -> None:
    """The CLI must not re-decide provider availability from its default model."""

    pytest.importorskip("litellm")
    repo = repo_with_history(commits=3)
    selected = replace(_winning_candidate(), model="claude-sonnet-4-20250514")
    target = repo / ".ctx" / "fit-configuration.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema": "ctx.fit.applied-configuration-v1",
                "configuration_hash": selected.configuration_hash,
                "candidate": selected.to_dict(),
            }
        ),
        encoding="utf-8",
    )
    observed: list[str] = []

    def availability(model: str) -> bool:
        observed.append(model)
        return False

    monkeypatch.setattr("ctx.cli.fit._provider_available", availability)
    monkeypatch.setattr(
        experiment_module,
        "run_experiment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ProviderUnavailable("stop after probe")),
    )

    assert cmd_fit(_args(repo, test=True, budget=500.0, yes=True)) == 1
    assert observed == [selected.model]


def test_json_mode_refuses_apply_rather_than_reporting_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--apply was unreachable in JSON mode, and the exit code still said success."""

    assert cmd_fit(_args(tmp_path, json=True, apply=True, yes=True)) == 2

    assert "--apply and --pr cannot be combined with --json" in capsys.readouterr().err


def test_a_blocked_plan_emits_the_same_json_keys_as_a_planned_one(
    repo_with_history, capsys: pytest.CaptureFixture[str]
) -> None:
    """A consumer's key set must not depend on which branch produced the answer."""

    pytest.importorskip("litellm")
    repo = repo_with_history()

    assert cmd_fit(_args(repo, json=True, budget=50.0)) == 0
    planned = set(json.loads(capsys.readouterr().out))
    # Too small a budget: an ordinary CI situation, not an error.
    assert cmd_fit(_args(repo, json=True, test=True, budget=0.001)) == 1
    blocked = set(json.loads(capsys.readouterr().out))

    assert {"readiness", "dry_run"} <= blocked
    assert planned == blocked


def test_an_unusable_harness_is_a_refusal_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch, repo_with_history, capsys: pytest.CaptureFixture[str]
) -> None:
    """Credentials present but no harness: the product refuses, it does not crash."""

    pytest.importorskip("litellm")

    def unavailable(**_: object) -> object:
        raise ProviderUnavailable(
            "the `ctx` harness is not on PATH, so no real agent can be driven"
        )

    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda _model: True)
    monkeypatch.setattr(providers_module, "build_agent_driver", unavailable)

    exit_code = cmd_fit(_args(repo_with_history(), test=True, budget=500.0, yes=True))

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "the `ctx` harness is not on PATH" in err
    assert "nothing was spent" in err.lower()


def test_the_budget_gate_prices_the_whole_agent_loop_not_one_exchange(repo_with_history) -> None:
    """One execution is a bounded agent loop, and the gate has to cover all of it.

    Priced as a single exchange, a budget two orders of magnitude short of the
    real cost passed the gate and the campaign was truncated mid-comparison.
    """

    pytest.importorskip("litellm")
    price = ModelPrice.from_litellm(DEFAULT_MODEL)
    assert price is not None
    profile = build_fit_profile(repo_with_history())

    executions = resolve_experiment(profile).plan.executions
    one_exchange_each = round(
        price.estimate(EXCHANGE_INPUT_TOKENS, EXCHANGE_OUTPUT_TOKENS) * executions * 1.6, 2
    )
    plan = resolve_experiment(profile, budget_usd=one_exchange_each).plan

    assert executions > 0
    assert plan.decision == "blocked-over-budget"
    assert (
        f"~{(EXCHANGE_INPUT_TOKENS + EXCHANGE_OUTPUT_TOKENS) * DEFAULT_MAX_ITERATIONS} tokens"
        in plan.cost.basis
    )


def test_noninteractive_evaluation_previews_the_exact_plan_before_refusing_without_yes(
    monkeypatch: pytest.MonkeyPatch,
    repo_with_history,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Automation gets a complete preview and cannot accidentally start spending."""

    pytest.importorskip("litellm")
    repo = repo_with_history(commits=3)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        experiment_module,
        "run_experiment",
        lambda *_a, **_k: pytest.fail("execution started before confirmation"),
    )

    assert cmd_fit(_args(repo, test=True, budget=500.0)) == 1

    output = capsys.readouterr().out
    assert "Experiment plan" in output
    assert "baseline" in output
    assert "revert-" in output
    assert "Total executions:" in output
    assert "Estimated cost:" in output
    assert "Budget ceiling:    $500.0" in output
    assert "does not prove that code under test cannot deliberately" in output
    assert "--yes" in output
    assert "Nothing was run and nothing was spent" in output


def test_json_evaluation_never_prompts_and_requires_yes_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    repo_with_history,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON stays one machine-readable document and fails closed without consent."""

    pytest.importorskip("litellm")
    repo = repo_with_history(commits=3)
    monkeypatch.setattr(
        experiment_module,
        "run_experiment",
        lambda *_a, **_k: pytest.fail("JSON execution started without --yes"),
    )

    assert cmd_fit(_args(repo, json=True, test=True, budget=500.0)) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["plan"]["authorized"] is False
    assert payload["plan"]["candidate_ids"]
    assert payload["plan"]["tasks"]
    assert any(
        "does not prove that code under test cannot deliberately" in warning
        for warning in payload["plan"]["warnings"]
    )


def test_yes_authorizes_the_displayed_plan_but_does_not_request_apply(
    monkeypatch: pytest.MonkeyPatch,
    repo_with_history,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--yes confirms requested work; it never adds a write request of its own."""

    pytest.importorskip("litellm")
    repo = repo_with_history(commits=3)
    observed: dict[str, object] = {}

    def stop_after_boundary(experiment, *, live: bool):
        observed["authorized"] = experiment.plan.authorized
        observed["preview"] = capsys.readouterr().out
        observed["live"] = live
        raise ProviderUnavailable("intentional boundary probe")

    monkeypatch.setattr(experiment_module, "run_experiment", stop_after_boundary)
    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda _model: True)

    assert cmd_fit(_args(repo, test=True, budget=500.0, yes=True)) == 1

    assert observed["authorized"] is True
    preview = str(observed["preview"])
    assert "Experiment plan" in preview
    assert "Authorization:     confirmation required" in preview
    assert not (repo / "AGENTS.md").exists()


def test_json_yes_still_emits_only_a_zero_spend_plan(
    monkeypatch: pytest.MonkeyPatch,
    repo_with_history,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("litellm")
    repo = repo_with_history(commits=3)
    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda _model: False)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: pytest.fail("JSON must never prompt"))

    monkeypatch.setattr(
        experiment_module,
        "run_experiment",
        lambda *_a, **_k: pytest.fail("JSON must never cross the spending boundary"),
    )

    assert cmd_fit(_args(repo, json=True, test=True, budget=500.0, yes=True)) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["authorized"] is False
    assert "plan-only" in payload["execution_refusal"]
    assert "execution" not in payload


def test_plan_render_names_every_candidate_and_task(repo_with_history) -> None:
    pytest.importorskip("litellm")
    experiment = resolve_experiment(
        build_fit_profile(repo_with_history(commits=3)), budget_usd=500.0
    )

    rendered = _format_plan(experiment.plan)

    assert all(candidate.candidate_id in rendered for candidate in experiment.candidates.candidates)
    assert all(task.title in rendered for task in experiment.tasks.tasks)


def test_the_dry_run_script_describes_what_the_product_actually_does(repo_with_history) -> None:
    """The rehearsal has to match the performance: --pr now opens the pull request."""

    script = _format_dry_run(build_fit_profile(repo_with_history()))

    assert "not implemented yet" not in script  # tasks are derived from history today
    assert "no branch is created" not in script  # --pr creates one (FITBUG-036)
    assert "open the pull request" in script
    assert "nothing is ever merged" in script
    candidate_line = next(line for line in script.splitlines() if "bounded candidates" in line)
    assert candidate_line.endswith("ctx-capability-set")
    assert "repository-instructions" not in candidate_line
    assert "model" not in candidate_line
    assert "already available in the repository" in script
    assert "isolated HOME" in script
    assert "without network access" in script


# --------------------------------------------------------------------------
# FITBUG-036: --apply writes files and runs no git; --pr opens the PR, after
# announcing every command it will run.
# --------------------------------------------------------------------------

#: The probes the gate is allowed to run before the user has said yes.
_READ_ONLY = (
    "git rev-parse",
    "git status",
    "git show",
    "git remote get-url",
    "gh auth status",
)


class _Commands:
    """Stands in for the one subprocess seam in `ctx.fit.apply`, recording calls."""

    def __init__(self, replies: dict[str, CommandResult] | None = None) -> None:
        self.replies = dict(replies or {})
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, command: Sequence[str], *, cwd: Path, stdin: str | None = None
    ) -> CommandResult:
        command = tuple(command)
        self.calls.append(command)
        for prefix, reply in self.replies.items():
            if " ".join(command).startswith(prefix):
                return reply
        return CommandResult(0)

    @property
    def mutating(self) -> tuple[tuple[str, ...], ...]:
        """Calls that could change something — everything but the probes."""

        return tuple(call for call in self.calls if not " ".join(call).startswith(_READ_ONLY))


def _healthy(root: Path, overrides: dict[str, CommandResult] | None = None) -> _Commands:
    """A repository where every gate passes, unless a test spoils one."""

    replies = {
        "git rev-parse --show-toplevel": CommandResult(0, f"{root}\n"),
        "git status": CommandResult(0, ""),
        "gh auth status": CommandResult(0, "Logged in to github.com account octocat"),
        "git rev-parse --verify": CommandResult(1),
        "git remote get-url": CommandResult(0, "git@github.com:octocat/repo.git\n"),
        "git rev-parse --abbrev-ref": CommandResult(0, "main\n"),
        "gh pr create": CommandResult(0, "https://github.com/octocat/repo/pull/7\n"),
    }
    replies.update(overrides or {})
    return _Commands(replies)


def test_apply_writes_the_configuration_and_runs_no_git_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--apply's whole contract is a working-tree write the user reviews themselves."""

    commands = _Commands()
    monkeypatch.setattr(apply_module, "run_command", commands)

    exit_code = _handle_apply(
        _winning_recommendation(), (_winning_candidate(),), _args(tmp_path, apply=True, yes=True)
    )

    assert exit_code == 0
    assert (tmp_path / ".ctx" / "fit-configuration.json").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert commands.calls == []  # no branch, no commit, no push
    out = capsys.readouterr().out
    assert "no branch was created" in out
    assert "git diff" in out
    assert "git checkout -- .ctx/fit-configuration.json" in out


def test_pr_announces_every_command_before_running_any_of_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --yes the user reads the whole sequence and nothing has happened yet."""

    commands = _healthy(tmp_path)
    monkeypatch.setattr(apply_module, "run_command", commands)

    exit_code = _handle_apply(
        _winning_recommendation(), (_winning_candidate(),), _args(tmp_path, pr=True)
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    # Per-run, so a second `ctx fit --pr` cannot collide with the first branch.
    match = re.search(r"ctx-fit/\d{8}-\d{6}", out)
    assert match is not None
    branch = match.group(0)
    assert f"git checkout -b {branch}" in out
    assert "git add -- .ctx/fit-configuration.json" in out
    assert "git commit -m 'Optimize AI coding configuration using CTX Fit'" in out
    assert f"git push --set-upstream origin {branch}" in out
    assert "gh pr create --title" in out
    assert "Re-run with --yes" in out
    assert commands.mutating == ()
    assert not (tmp_path / "AGENTS.md").exists()


def test_pr_with_yes_opens_the_pull_request_it_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The defect was a branch name printed and never created."""

    commands = _healthy(tmp_path)
    monkeypatch.setattr(apply_module, "run_command", commands)

    exit_code = _handle_apply(
        _winning_recommendation(), (_winning_candidate(),), _args(tmp_path, pr=True, yes=True)
    )

    assert exit_code == 0
    assert [call[:2] for call in commands.mutating] == [
        ("git", "checkout"),
        ("git", "add"),
        ("git", "commit"),
        ("git", "push"),
        ("gh", "pr"),
    ]
    assert (tmp_path / ".ctx" / "fit-configuration.json").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    out = capsys.readouterr().out
    assert "https://github.com/octocat/repo/pull/7" in out
    assert "never merges" in out


def test_an_unauthenticated_gh_is_refused_with_the_fix_in_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The common failure, and --yes does not get past it."""

    commands = _healthy(
        tmp_path,
        {"gh auth status": CommandResult(1, stderr="You are not logged into any GitHub hosts.")},
    )
    monkeypatch.setattr(apply_module, "run_command", commands)

    exit_code = _handle_apply(
        _winning_recommendation(), (_winning_candidate(),), _args(tmp_path, pr=True, yes=True)
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "gh auth login" in err
    assert "no branch, no commit, no push" in err
    assert commands.mutating == ()
    assert not (tmp_path / "AGENTS.md").exists()


def test_the_users_own_work_inside_agents_md_stops_the_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal has to name the file, because the user cannot see the gate.

    `git add -- AGENTS.md` stages every byte of it. A gate that exempted the
    path rather than the content pushed the line below to the user's remote.
    """

    (tmp_path / "AGENTS.md").write_text(
        "# House rules\nTODO(me): drop the customer database.\n", encoding="utf-8"
    )
    commands = _healthy(
        tmp_path,
        {
            "git status": CommandResult(0, " M AGENTS.md\0"),
            "git show HEAD:AGENTS.md": CommandResult(0, "# House rules\n"),
        },
    )
    monkeypatch.setattr(apply_module, "run_command", commands)

    exit_code = _handle_apply(
        _winning_recommendation(), (_winning_candidate(),), _args(tmp_path, pr=True, yes=True)
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "AGENTS.md" in err
    assert "changes CTX Fit did not write" in err
    assert commands.mutating == ()
    assert "drop the customer database" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_a_command_that_fails_is_reported_as_it_was_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two renderings of one command read as two commands, and the file is written."""

    commands = _healthy(tmp_path, {"git commit": CommandResult(1, stderr="nothing to commit")})
    monkeypatch.setattr(apply_module, "run_command", commands)

    exit_code = _handle_apply(
        _winning_recommendation(), (_winning_candidate(),), _args(tmp_path, pr=True, yes=True)
    )

    captured = capsys.readouterr()
    announced = next(
        line.strip() for line in captured.out.splitlines() if line.strip().startswith("git commit")
    )

    assert exit_code == 1
    assert f"Stopped at `{announced}`" in captured.err
    assert "2 of 5 commands ran" in captured.err
    # The write happens before the first command, so the undo has to name it.
    assert ".ctx/fit-configuration.json is written into the working tree" in captured.err
    assert "git checkout -- .ctx/fit-configuration.json" in captured.err
    assert (tmp_path / ".ctx" / "fit-configuration.json").exists()


def test_asking_for_both_apply_and_pr_says_which_one_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--pr supersedes --apply. Silently dropping a flag the user typed is not an answer."""

    commands = _healthy(tmp_path)
    monkeypatch.setattr(apply_module, "run_command", commands)

    exit_code = _handle_apply(
        _winning_recommendation(),
        (_winning_candidate(),),
        _args(tmp_path, apply=True, pr=True, yes=True),
    )

    assert exit_code == 0
    assert "--apply adds nothing here" in capsys.readouterr().out


def test_a_real_run_does_not_announce_itself_as_a_simulation(
    stubbed_evaluation: None,
    monkeypatch: pytest.MonkeyPatch,
    repo_with_history,
) -> None:
    """The banner used to contradict the report and the JSON of the same run."""

    monkeypatch.setattr(
        execution_module,
        "execute_trials",
        lambda *a, **k: ExecutionReport(trials_run=3, budget_usd=0.20, spent_usd=0.14),
    )

    pytest.importorskip("litellm")
    outcome = run_experiment(
        (
            lambda experiment: authorize_experiment(
                experiment, expected_digest=experiment.plan.executable_digest
            )
        )(resolve_experiment(build_fit_profile(repo_with_history(commits=3)), budget_usd=500.0)),
        live=True,
    )
    banner = _banner(outcome)

    assert "simulated" not in banner.lower()
    assert "3 trial(s)" in banner
    assert "$0.14" in banner


def test_recommendation_receives_the_full_declared_task_field(
    stubbed_evaluation: None, monkeypatch: pytest.MonkeyPatch, repo_with_history
) -> None:
    """Partial evidence cannot shrink the authorized comparison after execution."""

    report = ExecutionReport(
        outcomes=(
            CandidateOutcome(
                candidate_id="lean",
                trials=(
                    TrialResult("lean", "task-a", 0, "verified"),
                    TrialResult("lean", "task-b", 0, "infrastructure-failure"),
                ),
                reliability_floor=1.0,
            ),
        ),
        trials_run=2,
    )
    recorded: dict[str, int] = {}

    def fake_recommend(
        *_: object, task_count: int, trials_per_task: int, **_identity: object
    ) -> object:
        recorded["task_count"] = task_count
        return object()

    monkeypatch.setattr(execution_module, "execute_trials", lambda *a, **k: report)
    monkeypatch.setattr(recommend_module, "recommend", fake_recommend)

    repo = repo_with_history(commits=3)
    run_experiment(
        (
            lambda experiment: authorize_experiment(
                experiment, expected_digest=experiment.plan.executable_digest
            )
        )(resolve_experiment(build_fit_profile(repo), budget_usd=500.0)),
        live=True,
    )

    # Three tasks were authorized; an infrastructure failure cannot silently
    # shrink that field to the one task that happened to produce scored evidence.
    assert recorded["task_count"] == 3


def test_a_test_run_derives_its_tasks_once(
    monkeypatch: pytest.MonkeyPatch, repo_with_history
) -> None:
    """The plan and the campaign are one experiment, so it is derived one time.

    Task derivation walks Git history and is the slow half of a Fit run. It used
    to happen twice per ``--test`` invocation -- once to price the experiment and
    once to run it -- which is both a delay the user pays for and two chances for
    the two to disagree about what the experiment is.
    """

    pytest.importorskip("litellm")
    derivations: list[str] = []
    real_derive = tasks_module.derive_tasks

    def counting(repo_path, **kwargs):
        derivations.append(str(repo_path))
        return real_derive(repo_path, **kwargs)

    monkeypatch.setattr(tasks_module, "derive_tasks", counting)
    # Simulated, so the real catalog can supply candidates and the plan can
    # reach "ready" without any provider stack being involved.
    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda _model: False)
    monkeypatch.setattr(execution_module, "execute_trials", lambda *a, **k: ExecutionReport())

    assert cmd_fit(_args(repo_with_history(), test=True, budget=500.0, yes=True)) == 0

    assert len(derivations) == 1
