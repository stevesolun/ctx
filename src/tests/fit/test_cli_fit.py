"""What ``ctx fit`` says about a run has to match the run.

Every case here is a place where the command's own account diverged from what
happened: a refusal that arrived as a traceback, an exit code of 0 for work that
was silently dropped, a JSON document whose keys depended on which branch built
it, a real and paid run announcing itself as a simulation, and an evidence count
that included tasks which produced no evidence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import ctx.fit.execution as execution_module
import ctx.fit.live_runner as live_runner_module
import ctx.fit.providers as providers_module
import ctx.fit.recommend as recommend_module
import ctx.fit.release_catalog as release_catalog_module
from ctx.cli.fit import (
    DEFAULT_MODEL,
    EXCHANGE_INPUT_TOKENS,
    EXCHANGE_OUTPUT_TOKENS,
    _build_plan,
    _format_dry_run,
    _handle_apply,
    _run_evaluation,
    cmd_fit,
    default_namespace,
)
from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import CandidateOutcome, ExecutionReport, TrialResult
from ctx.fit.experiment import ModelPrice
from ctx.fit.profile import build_fit_profile
from ctx.fit.providers import DEFAULT_MAX_ITERATIONS, ProviderUnavailable
from ctx.fit.recommend import RankedCandidate, Recommendation


def _args(repo: Path, **overrides: Any) -> argparse.Namespace:
    args = default_namespace(str(repo))
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(repo), *args), check=True, capture_output=True, text=True)


def _repo_with_history(tmp_path: Path, *, commits: int = 1) -> Path:
    """A repository ``derive_tasks`` accepts: paired source and test changes.

    ``commits`` counts the derivable ones. The scaffolding commit that lands the
    module is extra: a task reverts to the commit before its own, so the source
    file has to already exist there.
    """

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    source = repo / "src" / "calc.py"
    source.write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: scaffold the calc module")

    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: add addition helper")

    for index in range(1, commits):
        with source.open("a", encoding="utf-8") as handle:
            handle.write(f"\n\ndef op{index}(a, b):\n    return a + b + {index}\n")
        (repo / "tests" / f"test_op{index}.py").write_text(
            f"from src.calc import op{index}\n\n\n"
            f"def test_op{index}():\n    assert op{index}(1, 1) == {2 + index}\n",
            encoding="utf-8",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"feat: add op{index}")
    return repo


@pytest.fixture
def stubbed_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the evaluation loop with every collaborator that costs money replaced."""

    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda: True)
    monkeypatch.setattr(providers_module, "build_agent_driver", lambda **k: object())
    monkeypatch.setattr(live_runner_module, "make_live_runner", lambda *a, **k: object())
    monkeypatch.setattr(release_catalog_module, "open_release_candidate_source", lambda: None)


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
    return CandidateConfiguration(
        candidate_id="lean",
        role="recommended",
        capability_ids=("skill:ctx-python-testing",),
        model=DEFAULT_MODEL,
        instructions=(),
        selection_reason="the single highest-ranked capability",
    )


def test_json_mode_refuses_apply_rather_than_reporting_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--apply was unreachable in JSON mode, and the exit code still said success."""

    assert cmd_fit(_args(tmp_path, json=True, apply=True, yes=True)) == 2

    assert "--apply and --pr cannot be combined with --json" in capsys.readouterr().err


def test_a_blocked_plan_emits_the_same_json_keys_as_a_planned_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A consumer's key set must not depend on which branch produced the answer."""

    pytest.importorskip("litellm")
    repo = _repo_with_history(tmp_path)

    assert cmd_fit(_args(repo, json=True, budget=50.0)) == 0
    planned = set(json.loads(capsys.readouterr().out))
    # Too small a budget: an ordinary CI situation, not an error.
    assert cmd_fit(_args(repo, json=True, test=True, budget=0.001)) == 1
    blocked = set(json.loads(capsys.readouterr().out))

    assert {"readiness", "dry_run"} <= blocked
    assert planned == blocked


def test_an_unusable_harness_is_a_refusal_rather_than_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Credentials present but no harness: the product refuses, it does not crash."""

    pytest.importorskip("litellm")

    def unavailable(**_: object) -> object:
        raise ProviderUnavailable(
            "the `ctx` harness is not on PATH, so no real agent can be driven"
        )

    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda: True)
    monkeypatch.setattr(providers_module, "build_agent_driver", unavailable)

    exit_code = cmd_fit(_args(_repo_with_history(tmp_path), test=True, budget=500.0))

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "the `ctx` harness is not on PATH" in err
    assert "nothing was spent" in err.lower()


def test_the_budget_gate_prices_the_whole_agent_loop_not_one_exchange(tmp_path: Path) -> None:
    """One execution is a bounded agent loop, and the gate has to cover all of it.

    Priced as a single exchange, a budget two orders of magnitude short of the
    real cost passed the gate and the campaign was truncated mid-comparison.
    """

    pytest.importorskip("litellm")
    price = ModelPrice.from_litellm(DEFAULT_MODEL)
    assert price is not None
    repo = _repo_with_history(tmp_path)
    profile = build_fit_profile(repo)

    executions = _build_plan(profile, None).executions  # type: ignore[attr-defined]
    one_exchange_each = round(
        price.estimate(EXCHANGE_INPUT_TOKENS, EXCHANGE_OUTPUT_TOKENS) * executions * 1.6, 2
    )
    plan = _build_plan(profile, one_exchange_each)

    assert executions > 0
    assert plan.decision == "blocked-over-budget"  # type: ignore[attr-defined]
    assert (
        f"~{(EXCHANGE_INPUT_TOKENS + EXCHANGE_OUTPUT_TOKENS) * DEFAULT_MAX_ITERATIONS} tokens"
        in (
            plan.cost.basis  # type: ignore[attr-defined]
        )
    )


def test_the_dry_run_script_describes_what_the_product_actually_does(tmp_path: Path) -> None:
    """The rehearsal has to match the performance: no PR is opened, and tasks are derived."""

    script = _format_dry_run(build_fit_profile(_repo_with_history(tmp_path)))

    assert "not implemented yet" not in script  # tasks are derived from history today
    assert "open a PR" not in script
    assert "no branch is created and nothing is merged" in script


def test_pr_output_does_not_claim_a_branch_was_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing in the apply path runs git, and --apply writes to the current branch."""

    exit_code = _handle_apply(
        _winning_recommendation(), (_winning_candidate(),), _args(tmp_path, pr=True)
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Suggested branch (not created)" in out
    assert "land in your working tree on the current branch" in out


def test_a_real_run_does_not_announce_itself_as_a_simulation(
    stubbed_evaluation: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The banner used to contradict the report and the JSON of the same run."""

    monkeypatch.setattr(
        execution_module,
        "execute_trials",
        lambda *a, **k: ExecutionReport(trials_run=3, budget_usd=0.20, spent_usd=0.14),
    )

    _, _, banner, _ = _run_evaluation(
        build_fit_profile(tmp_path), _args(tmp_path, test=True, budget=0.20)
    )

    assert "simulated" not in banner.lower()
    assert "3 trial(s)" in banner
    assert "$0.14" in banner


def test_only_tasks_that_produced_evidence_are_counted(
    stubbed_evaluation: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A task abandoned or lost to infrastructure is not evidence about anything."""

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

    def fake_recommend(*_: object, task_count: int, trials_per_task: int) -> object:
        recorded["task_count"] = task_count
        return object()

    monkeypatch.setattr(execution_module, "execute_trials", lambda *a, **k: report)
    monkeypatch.setattr(recommend_module, "recommend", fake_recommend)

    repo = _repo_with_history(tmp_path, commits=3)
    _run_evaluation(build_fit_profile(repo), _args(repo, test=True, budget=0.20))

    # Three tasks were derived from this history; one produced a scored trial.
    assert recorded["task_count"] == 1
