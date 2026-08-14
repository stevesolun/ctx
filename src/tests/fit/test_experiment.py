from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import ctx.fit.execution as execution_module
from ctx.cli.run import main as ctx_main
from ctx.engine.planner import BoundedCapabilityPlanner
from ctx.fit.candidates import (
    CandidateSet,
    CapabilityMaterial,
    CandidateConfiguration,
    generate_candidates,
)
from ctx.fit.execution import ExecutionReport
from ctx.fit.experiment import (
    DEFAULT_TRIALS_PER_TASK,
    EXPERIMENT_PLAN_SCHEMA,
    ModelPrice,
    authorize_experiment,
    plan_experiment,
    resolve_experiment,
    run_experiment,
)
from ctx.fit.profile import build_fit_profile
from ctx.fit.release_catalog import open_release_candidate_source
from ctx.fit.tasks import FitTask

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


def _tasks(count: int) -> tuple[FitTask, ...]:
    """``count`` tasks to plan over. The plan takes tasks, never a bare count."""

    return tuple(
        FitTask(
            task_id=f"revert-{index}",
            title=f"reimplement thing {index}",
            source="historical-revert",
            provenance=f"commit {index}",
            source_paths=("src/calc.py",),
            test_paths=("tests/test_calc.py",),
            verify_command=("python", "-m", "pytest", "-q"),
        )
        for index in range(count)
    )


def _plan(tmp_path: Path, *, task_count: int = 0, **kwargs: object):
    profile = build_fit_profile(_repo(tmp_path))
    return plan_experiment(profile, _candidates(profile), _tasks(task_count), **kwargs)  # type: ignore[arg-type]


def test_authorization_digest_detects_budget_and_campaign_size_tampering(
    tmp_path: Path,
) -> None:
    plan = _plan(
        tmp_path,
        task_count=1,
        budget_usd=1000,
        price=PRICE,
    )
    authorized = replace(
        plan,
        authorized=True,
        authorization_digest=plan.executable_digest,
    )

    assert authorized.can_execute is True
    assert replace(authorized, budget_usd=1001).can_execute is False
    changed_size = replace(
        authorized,
        trials_per_task=100,
        executions=authorized.candidate_count * authorized.task_count * 100,
    )

    assert changed_size.can_authorize is True
    assert changed_size.can_execute is False


def test_resolve_uses_the_applied_model_for_every_arm_and_for_pricing(
    repo_with_history,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repo_with_history(commits=3)
    material = CapabilityMaterial.from_content(
        capability_id="skill:ctx-python-testing",
        delivery_mode="task-user-context",
        source_identity="package:catalog#skill:ctx-python-testing",
        catalog_entry_digest="a" * 64,
        content="# Current applied capability\n",
    )
    candidate = CandidateConfiguration(
        candidate_id="prior-winner",
        role="lean",
        capability_ids=(material.capability_id,),
        model="openai/gpt-5.5",
        instructions=(),
        selection_reason="The content-addressed winner currently active in this repository.",
        capability_materials=(material,),
    )
    target = repo / ".ctx" / "fit-configuration.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema": "ctx.fit.applied-configuration-v1",
                "configuration_hash": candidate.configuration_hash,
                "candidate": candidate.to_dict(),
            }
        ),
        encoding="utf-8",
    )
    priced: list[str] = []

    def price(model: str) -> ModelPrice:
        priced.append(model)
        return PRICE

    monkeypatch.setattr(ModelPrice, "from_litellm", staticmethod(price))

    experiment = resolve_experiment(
        build_fit_profile(repo), budget_usd=500.0, model="different-model"
    )

    assert experiment.model == candidate.model
    assert priced == [candidate.model]
    assert all(item.model == candidate.model for item in experiment.candidates.candidates)


def test_resolve_prices_the_single_model_in_the_generated_candidate_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    profile = build_fit_profile(repo)
    generated = _candidates(profile)
    field = replace(
        generated,
        candidates=tuple(replace(candidate, model="model-b") for candidate in generated.candidates),
    )
    priced: list[str] = []

    def price_model(model: str) -> ModelPrice:
        priced.append(model)
        return PRICE

    monkeypatch.setattr("ctx.fit.candidates.generate_candidates", lambda *_a, **_k: field)
    monkeypatch.setattr(
        ModelPrice,
        "from_litellm",
        staticmethod(price_model),
    )

    experiment = resolve_experiment(profile, budget_usd=500.0, model="model-a")

    assert experiment.model == "model-b"
    assert priced == ["model-b"]
    assert experiment.plan_matches is True


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
    assert plan.can_authorize is True
    assert plan.can_execute is False


def test_unevaluable_repository_is_blocked_before_cost_is_considered(tmp_path: Path) -> None:
    profile = build_fit_profile(_repo(tmp_path, tests=False))
    plan = plan_experiment(profile, _candidates(profile), _tasks(2), budget_usd=1000.0, price=PRICE)

    assert plan.decision == "blocked-not-evaluable"


def test_a_nan_budget_is_refused_rather_than_waved_through(tmp_path: Path) -> None:
    """The gate before real spend must not fail open on a number it cannot use.

    Every IEEE comparison against NaN is False, so an unchecked NaN budget slips
    past the over-budget test and out the other side as "ready" — `--budget nan`
    is accepted by argparse's `type=float` and authorizes an unbounded campaign.
    """

    plan = _plan(tmp_path, task_count=2, budget_usd=float("nan"), price=PRICE)

    assert plan.cost.is_known  # the cost is fine; it is the budget that is not
    assert plan.decision == "blocked-invalid-budget"
    assert plan.can_execute is False


def test_an_infinite_budget_is_not_a_budget(tmp_path: Path) -> None:
    """A limit no cost can exceed places no limit on spending."""

    plan = _plan(tmp_path, task_count=2, budget_usd=float("inf"), price=PRICE)

    assert plan.decision == "blocked-invalid-budget"
    assert plan.can_execute is False


def test_a_negative_budget_is_not_an_authorization(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=2, budget_usd=-1.0, price=PRICE)

    assert plan.decision == "blocked-invalid-budget"
    assert plan.can_authorize is False


def test_a_non_finite_cost_estimate_is_not_treated_as_known(tmp_path: Path) -> None:
    invalid_price = ModelPrice(
        model="broken-price",
        usd_per_million_input=float("nan"),
        usd_per_million_output=1.0,
    )

    plan = _plan(tmp_path, task_count=2, budget_usd=1000.0, price=invalid_price)

    assert plan.cost.is_known is False
    assert plan.decision == "blocked-unknown-cost"
    assert plan.can_authorize is False


def test_a_finite_budget_still_passes_the_validity_check(tmp_path: Path) -> None:
    """The NaN guard must reject NaN, not budgets in general."""

    assert _plan(tmp_path, task_count=2, budget_usd=1000.0, price=PRICE).decision == "ready"


# --------------------------------------------------------------------------
# A refusal names the cause it actually observed.
# --------------------------------------------------------------------------


def test_abstaining_candidates_do_not_blame_the_repository_for_missing_tests(
    tmp_path: Path,
) -> None:
    """A broken install must not be reported as a repository without tests.

    Collapsing "no candidates" into "not evaluable" made the output contradict
    itself: the same run said the repository has deterministic tests and that it
    has none, and sent the user to add tests that were already there.
    """

    profile = build_fit_profile(_repo(tmp_path))
    unopenable_catalog = CandidateSet(
        abstained=True, abstention_reason="the capability catalog could not be opened"
    )

    plan = plan_experiment(profile, unopenable_catalog, _tasks(2), budget_usd=1000.0, price=PRICE)

    assert profile.is_fit_evaluable is True
    assert plan.decision == "blocked-no-candidates"
    assert plan.can_execute is False
    assert plan.explanation == "the capability catalog could not be opened"
    # The cause has to survive into the machine-readable payload too.
    assert plan.to_dict()["explanation"] == "the capability catalog could not be opened"


def test_an_abstention_without_a_reason_still_explains_itself(tmp_path: Path) -> None:
    profile = build_fit_profile(_repo(tmp_path))

    plan = plan_experiment(
        profile, CandidateSet(abstained=True), _tasks(2), budget_usd=1000.0, price=PRICE
    )

    assert plan.decision == "blocked-no-candidates"
    assert "nothing to compare" in plan.explanation


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


def test_plan_discloses_the_repository_verifier_trust_boundary(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=2, budget_usd=1000.0, price=PRICE)

    assert any(
        "does not prove that code under test cannot deliberately" in warning
        for warning in plan.warnings
    )


def test_plan_discloses_native_verification_dependency_boundary(tmp_path: Path) -> None:
    plan = _plan(tmp_path, task_count=2, budget_usd=1000.0, price=PRICE)

    assert any(
        "already available in the repository" in warning
        and "isolated HOME" in warning
        and "without network access" in warning
        for warning in plan.warnings
    )


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
# The plan and the campaign are one experiment.
# --------------------------------------------------------------------------


def test_the_campaign_runs_the_experiment_the_plan_priced(
    monkeypatch, tmp_path: Path, repo_with_history
) -> None:
    """--budget gates on the plan, so the campaign has to be the same experiment.

    Priced from one derivation and run from another, the two agreed only by
    convention: the plan's trial count came from a constant and the campaign's
    from a literal, so a change to either silently approved one experiment and
    ran a different one.
    """

    pytest.importorskip("litellm")
    recorded: dict[str, Any] = {}

    def capture(candidates, tasks, _runner, **kwargs):
        recorded["candidates"] = candidates
        recorded["tasks"] = tasks
        recorded.update(kwargs)
        return ExecutionReport()

    monkeypatch.setattr(execution_module, "execute_trials", capture)

    # The recommendation is told the trial count too, and that read was
    # protected by nothing: a literal here would leave the whole suite green
    # while the confidence denominator diverged from what the campaign ran.
    import ctx.fit.recommend as recommend_module

    real_recommend = recommend_module.recommend

    def capture_recommend(report, candidates, **kwargs):
        recorded["recommend_trials_per_task"] = kwargs.get("trials_per_task")
        return real_recommend(report, candidates, **kwargs)

    monkeypatch.setattr(recommend_module, "recommend", capture_recommend)

    experiment = resolve_experiment(
        build_fit_profile(repo_with_history(commits=3)), budget_usd=500.0, trials_per_task=2
    )
    run_experiment(experiment, live=False)
    plan = experiment.plan

    assert plan.task_count == len(recorded["tasks"]) > 0
    assert plan.candidate_count == len(recorded["candidates"]) > 0
    assert plan.trials_per_task == recorded["trials_per_task"] == 2
    assert plan.executions == plan.candidate_count * len(recorded["tasks"]) * 2
    assert recorded["recommend_trials_per_task"] == 2


def test_the_campaign_runs_the_model_the_plan_was_priced_against(
    tmp_path: Path, repo_with_history
) -> None:
    """A price quoted for one model while the trials run another is not a gate."""

    pytest.importorskip("litellm")

    experiment = resolve_experiment(
        build_fit_profile(repo_with_history()), budget_usd=500.0, model="gpt-4o"
    )

    assert experiment.model == "gpt-4o"
    assert {candidate.model for candidate in experiment.candidates.candidates} == {"gpt-4o"}
    assert "gpt-4o rates" in experiment.plan.cost.basis


def test_the_campaign_verifies_with_the_command_the_plan_named(
    tmp_path: Path, repo_with_history
) -> None:
    """Two hand-copied fallbacks are two chances to verify with the wrong thing."""

    experiment = resolve_experiment(build_fit_profile(repo_with_history()))

    assert experiment.verify_command == ("python", "-m", "pytest", "-q")
    assert all(task.verify_command == experiment.verify_command for task in experiment.tasks.tasks)


def test_plan_summary_names_the_verifier_its_tasks_will_actually_run(tmp_path: Path) -> None:
    profile = build_fit_profile(_repo(tmp_path))
    task = replace(_tasks(1)[0], verify_command=("npm", "run", "test"))

    plan = plan_experiment(
        profile,
        _candidates(profile),
        (task,),
        budget_usd=1000.0,
        price=PRICE,
    )

    assert plan.verification == ("npm run test",)


def test_the_fallback_verify_command_is_the_one_the_module_declares(
    monkeypatch, repo_with_history
) -> None:
    """The fallback branch, exercised for real rather than by coincidence.

    ``repo_with_history`` ships a tests/ directory, so discovery returns a
    command that happens to equal the fallback literal. That made the guard
    above pass without ever reaching the ``else`` branch: changing
    FALLBACK_VERIFY_COMMAND left the whole suite green. Force discovery to find
    nothing, so the fallback is the only thing that can supply the command.
    """

    from ctx.fit import experiment as experiment_module

    profile = build_fit_profile(repo_with_history())
    monkeypatch.setattr(type(profile.verification), "best", lambda self, kind: None)

    experiment = resolve_experiment(profile)

    # Pin the VALUE, not the constant. Asserting against
    # FALLBACK_VERIFY_COMMAND itself is a tautology: change the constant and
    # both sides move together, which is how the previous guard stayed green
    # while the fallback was mutated.
    assert experiment.verify_command == ("python", "-m", "pytest", "-q")
    assert experiment_module.FALLBACK_VERIFY_COMMAND == ("python", "-m", "pytest", "-q")
    assert all(task.verify_command == experiment.verify_command for task in experiment.tasks.tasks)


def test_resolving_an_experiment_executes_nothing(tmp_path: Path, repo_with_history) -> None:
    """ADR-013: planning is free, read-only, and spends nothing."""

    pytest.importorskip("litellm")
    repo = repo_with_history()

    experiment = resolve_experiment(build_fit_profile(repo), budget_usd=500.0)

    assert experiment.plan.can_authorize is True
    assert experiment.plan.can_execute is False
    # Nothing ran, so no task has been observed to start red -- and a task that
    # has not been observed is not evidence of anything yet.
    assert all(task.starts_red is None for task in experiment.tasks.tasks)
    assert subprocess_status(repo) == ""


def test_authorization_changes_only_the_confirmation_state(
    repo_with_history,
) -> None:
    pytest.importorskip("litellm")
    experiment = resolve_experiment(
        build_fit_profile(repo_with_history(commits=3)), budget_usd=500.0
    )

    authorized = authorize_experiment(experiment, expected_digest=experiment.plan.executable_digest)

    assert authorized.plan.authorized is True
    assert authorized.plan.can_execute is True
    assert authorized.plan.budget_usd == experiment.plan.budget_usd
    assert authorized.plan.candidates == experiment.plan.candidates
    assert authorized.plan.tasks == experiment.plan.tasks
    assert authorized.plan.executions == experiment.plan.executions


def test_authorization_refuses_when_current_ai_configuration_changed_after_preview(
    repo_with_history,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = repo_with_history(commits=3)
    monkeypatch.setattr(ModelPrice, "from_litellm", staticmethod(lambda _model: PRICE))
    experiment = resolve_experiment(build_fit_profile(repo), budget_usd=500.0)
    installed = repo / ".claude" / "skills" / "current" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("---\nname: current\n---\n\n# Current\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="current baseline changed"):
        authorize_experiment(experiment, expected_digest=experiment.plan.executable_digest)


def subprocess_status(repo: Path) -> str:
    """The repository is untouched: resolution reads Git, it never writes it."""

    return subprocess.run(
        ("git", "-C", str(repo), "status", "--porcelain"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# --------------------------------------------------------------------------
# CLI wiring.
# --------------------------------------------------------------------------


def test_zero_tasks_is_never_reported_as_runnable(tmp_path: Path) -> None:
    """A plan with no tasks fits any budget while proving nothing."""

    plan = _plan(tmp_path, task_count=0, budget_usd=1000.0, price=PRICE)

    assert plan.executions == 0
    assert plan.decision == "blocked-no-tasks"
    assert plan.can_execute is False


def test_budget_flag_renders_a_plan_and_never_spends(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)

    assert ctx_main(["fit", str(repo), "--budget", "20"]) == 0

    output = capsys.readouterr().out
    assert "Experiment plan" in output
    # A scratch repository has no derivable history, so the run is refused.
    assert "Not runnable" in output


def test_plan_appears_in_json_when_budget_given(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)

    assert ctx_main(["fit", str(repo), "--budget", "20", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["schema"] == EXPERIMENT_PLAN_SCHEMA
    assert payload["plan"]["can_execute"] is False
