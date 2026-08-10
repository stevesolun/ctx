"""The authorization the user typed has to reach the code that spends.

``--budget`` was previously consumed entirely by the pre-flight plan: the gate
reported "ready" and then handed every trial an unrelated per-execution default,
with nothing accumulating across the campaign. A plan that fits a budget is not
a budget. These tests pin the wiring end of that promise -- the number reaching
``execute_trials`` and the provider driver -- while the enforcement arithmetic
itself is covered in ``test_execution_recommend``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

import ctx.fit.execution as execution_module
import ctx.fit.live_runner as live_runner_module
import ctx.fit.providers as providers_module
import ctx.fit.release_catalog as release_catalog_module
from ctx.cli.fit import _format_budget_stop, _run_evaluation
from ctx.fit.execution import ExecutionReport
from ctx.fit.profile import build_fit_profile


def _args(budget: float | None) -> argparse.Namespace:
    return argparse.Namespace(
        repo=".",
        json=False,
        test=True,
        apply=False,
        pr=False,
        yes=False,
        dry_run=False,
        budget=budget,
        max_depth=4,
    )


@pytest.fixture
def evaluation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the evaluation loop with every collaborator that costs money stubbed.

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
    evaluation: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda: True)

    _run_evaluation(build_fit_profile(tmp_path), _args(0.20))

    assert evaluation["kwargs"]["budget_usd"] == 0.20


def test_each_trial_is_capped_by_the_authorization_rather_than_a_constant(
    evaluation: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A trial gets the dollars still unspent, not the provider's fixed default."""

    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda: True)

    _run_evaluation(build_fit_profile(tmp_path), _args(0.20))
    build_runner_for = evaluation["kwargs"]["runner_for_budget"]
    assert build_runner_for is not None
    build_runner_for(0.07)

    # The first build is the up-front availability check; the second is the trial.
    assert evaluation["per_trial_budget_usd"] == [None, 0.07]


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
    evaluation: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Simulated dollars are invented; stopping a demo over them would mislead."""

    monkeypatch.setattr("ctx.cli.fit._provider_available", lambda: False)

    _run_evaluation(build_fit_profile(tmp_path), _args(0.20))

    assert evaluation["kwargs"]["budget_usd"] is None
    assert evaluation["kwargs"]["runner_for_budget"] is None
