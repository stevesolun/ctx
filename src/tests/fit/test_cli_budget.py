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

from pathlib import Path
from typing import Any

import pytest

import ctx.fit.execution as execution_module
import ctx.fit.live_runner as live_runner_module
import ctx.fit.providers as providers_module
import ctx.fit.release_catalog as release_catalog_module
from ctx.cli.fit import _format_budget_stop
from ctx.fit.execution import ExecutionReport
from ctx.fit.experiment import ResolvedExperiment, resolve_experiment, run_experiment
from ctx.fit.profile import build_fit_profile


def _experiment(repo: Path, budget: float | None) -> ResolvedExperiment:
    return resolve_experiment(build_fit_profile(repo), budget_usd=budget)


@pytest.fixture
def evaluation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run a campaign with every collaborator that costs money stubbed.

    The catalog is stubbed out so no candidate exists and no trial can be
    attempted: what is under test is which numbers are handed to the executor,
    not what the executor then does with them.
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
    monkeypatch.setattr(release_catalog_module, "open_release_candidate_source", lambda: None)
    return recorded


def test_the_authorized_budget_reaches_the_executor(
    evaluation: dict[str, Any], tmp_path: Path
) -> None:
    run_experiment(_experiment(tmp_path, 0.20), live=True)

    assert evaluation["kwargs"]["budget_usd"] == 0.20


def test_each_trial_is_capped_by_the_authorization_rather_than_a_constant(
    evaluation: dict[str, Any], tmp_path: Path
) -> None:
    """A trial gets the dollars still unspent, not the provider's fixed default."""

    run_experiment(_experiment(tmp_path, 0.20), live=True)
    build_runner_for = evaluation["kwargs"]["runner_for_budget"]
    assert build_runner_for is not None
    build_runner_for(0.07)

    # The first build is the up-front availability check; the second is the trial.
    assert evaluation["per_trial_budget_usd"] == [None, 0.07]


def test_without_an_authorization_no_trial_is_handed_a_cap(
    evaluation: dict[str, Any], tmp_path: Path
) -> None:
    """Each cap is the remaining authorization, so with none there is none to derive.

    ``execute_trials`` refuses the pair outright, so a live campaign that lost
    its budget must not offer it a factory it cannot use.
    """

    run_experiment(_experiment(tmp_path, None), live=True)

    assert evaluation["kwargs"]["budget_usd"] is None
    assert evaluation["kwargs"]["runner_for_budget"] is None


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

    assert evaluation["kwargs"]["budget_usd"] is None
    assert evaluation["kwargs"]["runner_for_budget"] is None
