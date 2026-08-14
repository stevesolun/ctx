"""The authorization the user typed has to reach the code that spends.

``--budget`` was previously consumed entirely by the pre-flight plan: the gate
reported "ready" and then handed every trial an unrelated per-execution default,
with nothing accumulating across the campaign. A plan that fits a budget is not
a budget. These tests pin the wiring end of that promise -- the number reaching
``execute_trials`` and the provider driver -- while the enforcement arithmetic
itself is covered in ``test_execution_recommend``.

The authorization now travels on the resolved experiment rather than alongside
it: :func:`run_experiment` spends under the same number the plan was checked
against, because it reads that number off the plan.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import ctx.fit.execution as execution_module
import ctx.fit.live_runner as live_runner_module
import ctx.fit.providers as providers_module
import ctx.fit.recommend as recommend_module
from ctx.cli.fit import _format_budget_stop
from ctx.fit.execution import ExecutionReport
from ctx.fit.experiment import (
    ResolvedExperiment,
    authorize_experiment,
    resolve_experiment,
    run_experiment,
)
from ctx.fit.profile import build_fit_profile
from ctx.fit.recommend import Recommendation


def _experiment(repo: Path, budget: float | None) -> ResolvedExperiment:
    return resolve_experiment(build_fit_profile(repo), budget_usd=budget)


def _authorize(experiment: ResolvedExperiment) -> ResolvedExperiment:
    return authorize_experiment(experiment, expected_digest=experiment.plan.executable_digest)


@pytest.fixture
def evaluation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run a campaign with every collaborator that costs money stubbed.

    The actual executor is replaced: what is under test is which authorization
    artifact and caps reach it, not what any trial then does.
    """

    recorded: dict[str, Any] = {"per_trial_budget_usd": [], "kwargs": {}}

    def fake_execute_trials(*args: object, **kwargs: object) -> ExecutionReport:
        recorded["kwargs"] = kwargs
        return ExecutionReport()

    def fake_build_agent_driver(**kwargs: object) -> object:
        recorded["per_trial_budget_usd"].append(kwargs.get("per_trial_budget_usd"))
        return object()

    monkeypatch.setattr(execution_module, "execute_trials", fake_execute_trials)
    monkeypatch.setattr(providers_module, "build_agent_driver", fake_build_agent_driver)
    monkeypatch.setattr(live_runner_module, "make_live_runner", lambda *a, **k: object())
    return recorded


def test_the_authorized_budget_reaches_the_executor(
    evaluation: dict[str, Any], repo_with_history
) -> None:
    pytest.importorskip("litellm")
    run_experiment(_authorize(_experiment(repo_with_history(commits=3), 500.0)), live=True)

    assert evaluation["kwargs"]["execution_plan"].budget_usd == 500.0


def test_live_execution_refuses_a_callers_unconfirmed_plan(
    evaluation: dict[str, Any], repo_with_history
) -> None:
    """A public spending API must enforce consent below the CLI boundary."""

    pytest.importorskip("litellm")
    with pytest.raises(PermissionError, match="authorized"):
        run_experiment(_experiment(repo_with_history(commits=3), 500.0), live=True)

    assert evaluation["per_trial_budget_usd"] == []
    assert evaluation["kwargs"] == {}


def test_live_execution_rechecks_current_baseline_after_authorization(
    evaluation: dict[str, Any], repo_with_history
) -> None:
    pytest.importorskip("litellm")
    repo = repo_with_history(commits=3)
    authorized = _authorize(_experiment(repo, 500.0))
    installed = repo / ".claude" / "skills" / "current" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("---\nname: current\n---\n\n# Current\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="current baseline changed"):
        run_experiment(authorized, live=True)

    assert evaluation["per_trial_budget_usd"] == []
    assert evaluation["kwargs"] == {}


def test_baseline_drift_during_campaign_preserves_report_but_forces_no_verdict(
    evaluation: dict[str, Any],
    repo_with_history,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("litellm")
    repo = repo_with_history(commits=3)
    authorized = _authorize(_experiment(repo, 500.0))
    report = ExecutionReport(plan_digest=authorized.plan.executable_digest)

    def drift_during_execution(*_args: object, **_kwargs: object) -> ExecutionReport:
        installed = repo / ".claude" / "skills" / "current" / "SKILL.md"
        installed.parent.mkdir(parents=True)
        installed.write_text("---\nname: current\n---\n\n# Current\n", encoding="utf-8")
        return report

    monkeypatch.setattr(execution_module, "execute_trials", drift_during_execution)
    monkeypatch.setattr(
        recommend_module,
        "recommend",
        lambda *_args, **_kwargs: Recommendation(
            schema="ctx.fit.recommendation-v1",
            verdict="recommend-change",
            winner_id="recommended",
            ranked=(),
            reasoning=("stale recommendation",),
            limitations=(),
            confidence="high",
        ),
    )

    outcome = run_experiment(authorized, live=True)

    assert outcome.report is report
    assert outcome.recommendation.verdict == "no-verdict"
    assert outcome.recommendation.winner_id is None
    assert any("baseline changed during" in line for line in outcome.recommendation.reasoning)


def test_live_execution_refuses_an_authorized_plan_after_campaign_drift(
    evaluation: dict[str, Any], repo_with_history
) -> None:
    pytest.importorskip("litellm")
    authorized = _authorize(_experiment(repo_with_history(commits=3), 500.0))
    assert len(authorized.candidates.candidates) > 1
    drifted = replace(
        authorized,
        candidates=replace(
            authorized.candidates,
            candidates=tuple(reversed(authorized.candidates.candidates)),
        ),
    )

    with pytest.raises(PermissionError, match="authorized, bounded"):
        run_experiment(drifted, live=True)

    assert evaluation["per_trial_budget_usd"] == []
    assert evaluation["kwargs"] == {}


def test_each_trial_is_capped_by_the_authorization_rather_than_a_constant(
    evaluation: dict[str, Any], repo_with_history
) -> None:
    """A trial gets the dollars still unspent, not the provider's fixed default."""

    pytest.importorskip("litellm")
    run_experiment(_authorize(_experiment(repo_with_history(commits=3), 500.0)), live=True)
    build_runner_for = evaluation["kwargs"]["runner_for_budget"]
    assert build_runner_for is not None
    build_runner_for(0.07)

    # The first build is the up-front availability check; the second is the trial.
    assert evaluation["per_trial_budget_usd"] == [None, 0.07]


def test_a_plan_without_a_budget_cannot_be_authorized(
    evaluation: dict[str, Any], repo_with_history
) -> None:
    """No confirmation can turn an unbounded plan into permission to spend."""

    pytest.importorskip("litellm")
    with pytest.raises(PermissionError, match="cannot authorize"):
        _authorize(_experiment(repo_with_history(commits=3), None))

    assert evaluation["per_trial_budget_usd"] == []
    assert evaluation["kwargs"] == {}


def test_a_truncated_campaign_says_so_instead_of_presenting_a_clean_result() -> None:
    """Partial evidence read as complete evidence is the failure to avoid."""

    stopped = _format_budget_stop(
        ExecutionReport(
            trials_run=3,
            trials_skipped=3,
            budget_usd=0.25,
            spent_usd=0.3,
            trials_skipped_budget=3,
            budget_stop="the authorized budget was spent",
        )
    )

    assert "$0.3 of the $0.25" in stopped
    assert "3 trial(s) never ran" in stopped
    assert "truncated evidence" in stopped
    # A campaign that finished must not print a scare message.
    assert _format_budget_stop(ExecutionReport(trials_run=6)) == ""


def test_a_simulated_run_is_not_charged_against_a_real_authorization(
    evaluation: dict[str, Any], tmp_path: Path
) -> None:
    """Simulated dollars are invented; stopping a demo over them would mislead."""

    run_experiment(_experiment(tmp_path, 0.20), live=False)

    assert evaluation["kwargs"]["execution_plan"] is None
    assert evaluation["kwargs"]["runner_for_budget"] is None
