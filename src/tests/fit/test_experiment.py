from __future__ import annotations

import json
from pathlib import Path

from ctx.cli.run import main as ctx_main
from ctx.engine.planner import BoundedCapabilityPlanner
from ctx.fit.candidates import CandidateSet, generate_candidates
from ctx.fit.experiment import (
    DEFAULT_TRIALS_PER_TASK,
    EXPERIMENT_PLAN_SCHEMA,
    ModelPrice,
    plan_experiment,
)
from ctx.fit.profile import build_fit_profile
from ctx.fit.release_catalog import open_release_candidate_source

PRICE = ModelPrice(model="test-model", usd_per_million_input=3.0, usd_per_million_output=15.0)


def _repo(tmp_path: Path, *, tests: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\n\n[tool.pytest.ini_options]\ntestpaths=['tests']\n",
        encoding="utf-8",
    )
    if tests:
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", "utf-8")
    return repo


def _candidates(profile: object) -> CandidateSet:
    source = open_release_candidate_source()
    assert source is not None
    return generate_candidates(profile, BoundedCapabilityPlanner(source=source))  # type: ignore[arg-type]


def _plan(tmp_path: Path, **kwargs: object):
    profile = build_fit_profile(_repo(tmp_path))
    return plan_experiment(profile, _candidates(profile), **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The catalog is real, and relevance is not fabricated.
# --------------------------------------------------------------------------


def test_shipped_catalog_opens_and_matches_on_signals(tmp_path: Path) -> None:
    profile = build_fit_profile(_repo(tmp_path))

    result = _candidates(profile)

    assert result.abstained is False
    recommended = next(item for item in result.candidates if item.role == "recommended")
    # A Python repository must not be offered the Rust capability.
    assert not any("rust" in item for item in recommended.capability_ids)
    assert any("python" in item for item in recommended.capability_ids)


# --------------------------------------------------------------------------
# The budget gate must fail closed.
# --------------------------------------------------------------------------


def test_unknown_cost_with_a_budget_is_blocked_not_allowed(tmp_path: Path) -> None:
    """A budget cannot be enforced against a number that does not exist."""

    plan = _plan(tmp_path, budget_usd=20.0, task_count=2)

    assert plan.cost.completeness == "unknown"
    assert plan.decision == "blocked-unknown-cost"
    assert plan.can_execute is False


def test_no_budget_is_blocked(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=2)

    assert plan.decision == "blocked-no-budget"
    assert plan.can_execute is False


def test_over_budget_is_blocked(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=4, budget_usd=0.01, price=PRICE)

    assert plan.cost.is_known
    assert plan.decision == "blocked-over-budget"
    assert plan.can_execute is False


def test_a_priced_plan_within_budget_is_ready(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=2, budget_usd=1000.0, price=PRICE)

    assert plan.cost.is_known
    assert plan.decision == "ready"
    assert plan.can_execute is True


def test_unevaluable_repository_is_blocked_before_cost_is_considered(tmp_path: Path) -> None:
    profile = build_fit_profile(_repo(tmp_path, tests=False))
    plan = plan_experiment(
        profile, _candidates(profile), task_count=2, budget_usd=1000.0, price=PRICE
    )

    assert plan.decision == "blocked-not-evaluable"


# --------------------------------------------------------------------------
# Reliability and arithmetic.
# --------------------------------------------------------------------------


def test_executions_multiply_candidates_tasks_and_trials(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=5, trials_per_task=3, budget_usd=1000.0, price=PRICE)

    assert plan.executions == plan.candidate_count * 5 * 3
    assert plan.trials_per_task == DEFAULT_TRIALS_PER_TASK


def test_too_few_trials_warns_that_reliability_is_unproven(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=2, trials_per_task=1, budget_usd=1000.0, price=PRICE)

    assert any("below the" in warning for warning in plan.warnings)


def test_cost_is_a_range_not_false_precision(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=3, budget_usd=1000.0, price=PRICE)

    assert plan.cost.low_usd is not None and plan.cost.high_usd is not None
    assert plan.cost.low_usd < plan.cost.high_usd
    assert plan.cost.basis


def test_plan_is_serializable_and_versioned(tmp_path: Path) -> None:
    payload = _plan(tmp_path, task_count=2, budget_usd=5.0, price=PRICE).to_dict()

    assert json.loads(json.dumps(payload, sort_keys=True))["schema"] == EXPERIMENT_PLAN_SCHEMA
    assert payload["explanation"]


# --------------------------------------------------------------------------
# CLI wiring.
# --------------------------------------------------------------------------


def test_budget_flag_renders_a_plan_and_never_spends(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)

    assert ctx_main(["fit", str(repo), "--budget", "20"]) == 0

    output = capsys.readouterr().out
    assert "Experiment plan" in output
    assert "Not runnable" in output
    # No cost may be invented when none can be derived.
    assert "Estimated cost:    unknown" in output


def test_plan_appears_in_json_when_budget_given(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)

    assert ctx_main(["fit", str(repo), "--budget", "20", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["schema"] == EXPERIMENT_PLAN_SCHEMA
    assert payload["plan"]["can_execute"] is False
