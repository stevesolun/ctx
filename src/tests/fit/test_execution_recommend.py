from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import cast

import pytest

from ctx.fit.candidates import CapabilityMaterial, CandidateConfiguration
from ctx.fit.execution import (
    CandidateOutcome,
    ExecutionReport,
    TrialResult,
    TrialRunner,
    TrialOutcome,
    execute_trials,
    make_simulated_runner,
)
from ctx.fit.experiment import (
    CostEstimate,
    ExperimentPlan,
    PlannedCandidate,
    PlannedTask,
)
from ctx.fit.recommend import Recommendation, recommend as _recommend
from ctx.fit.tasks import FitTask

VERIFY = ("python", "-m", "pytest", "-q")


def recommend(
    report: ExecutionReport,
    candidates: tuple[CandidateConfiguration, ...],
    *,
    task_count: int,
    trials_per_task: int,
    expected_plan_digest: str | None = None,
    expected_task_ids: tuple[str, ...] | None = None,
    expected_reliability_floor: float | None = None,
) -> Recommendation:
    return _recommend(
        report,
        candidates,
        task_count=task_count,
        trials_per_task=trials_per_task,
        expected_plan_digest=expected_plan_digest or report.plan_digest,
        expected_task_ids=expected_task_ids or report.declared_task_ids,
        expected_reliability_floor=(
            report.reliability_floor
            if expected_reliability_floor is None
            else expected_reliability_floor
        ),
    )


def _task(name: str, *, valid: bool = True) -> FitTask:
    return FitTask(
        task_id=name,
        title=name,
        source="historical-revert",
        provenance=f"commit {'0' * 40}",
        source_paths=("src/a.py",),
        test_paths=("tests/test_a.py",),
        verify_command=VERIFY,
        starts_red=True if valid else None,
    )


def _candidate(name: str, *, capabilities: int = 0) -> CandidateConfiguration:
    materials = tuple(
        CapabilityMaterial.from_content(
            capability_id=f"skill:c{index}",
            delivery_mode="task-user-context",
            source_identity=f"test-catalog#skill:c{index}",
            catalog_entry_digest=hashlib.sha256(f"entry-{index}".encode()).hexdigest(),
            content=f"test material {index}",
        )
        for index in range(capabilities)
    )
    return CandidateConfiguration(
        candidate_id=name,
        role="baseline" if name == "baseline" else "recommended",
        capability_ids=tuple(f"skill:c{index}" for index in range(capabilities)),
        model=None,
        instructions=(),
        selection_reason="test fixture candidate used to exercise the selection rule",
        capability_materials=materials,
    )


def _fixed_runner(outcomes: dict[str, str], cost: float | None = 0.10):
    return make_simulated_runner(
        outcome_by_candidate={
            candidate_id: cast(TrialOutcome, outcome) for candidate_id, outcome in outcomes.items()
        },
        default_verified_rate=1.0,
        cost_per_trial=cost,
    )


def _authorized_plan(
    candidates: tuple[CandidateConfiguration, ...],
    tasks: tuple[FitTask, ...],
    *,
    trials_per_task: int,
    budget_usd: float,
) -> ExperimentPlan:
    """The exact bounded artifact required by a non-simulated executor call."""

    plan = ExperimentPlan(
        schema="ctx.fit.experiment-plan-v1",
        repository="/test/repository",
        candidate_count=len(candidates),
        task_count=len(tasks),
        candidates=tuple(
            PlannedCandidate(
                candidate.candidate_id,
                candidate.role,
                candidate.capability_ids,
                candidate.model,
                candidate.instructions,
                candidate.configuration_hash,
            )
            for candidate in candidates
        ),
        tasks=tuple(
            PlannedTask(
                task.task_id,
                task.title,
                task.provenance,
                task.source_paths,
                task.test_paths,
                task.verify_command,
                _task_hash(task),
            )
            for task in tasks
        ),
        trials_per_task=trials_per_task,
        executions=len(candidates) * len(tasks) * trials_per_task,
        verification=("python -m pytest -q",),
        cost=CostEstimate("known", low_usd=0.0, high_usd=budget_usd),
        budget_usd=budget_usd,
        decision="ready",
    )
    return replace(
        plan,
        authorized=True,
        authorization_digest=plan.executable_digest,
    )


def _task_hash(task: FitTask) -> str:
    payload = json.dumps(
        {
            "task_id": task.task_id,
            "title": task.title,
            "source": task.source,
            "provenance": task.provenance,
            "source_paths": list(task.source_paths),
            "test_paths": list(task.test_paths),
            "verify_command": list(task.verify_command),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _report(
    candidates: tuple[CandidateConfiguration, ...],
    outcomes: tuple[CandidateOutcome, ...],
    *,
    task_count: int = 1,
    trials_per_task: int = 2,
    **kwargs: object,
) -> ExecutionReport:
    """A manually assembled report that still binds its declared field."""

    return ExecutionReport(
        outcomes=outcomes,
        declared_candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
        declared_task_ids=tuple(f"task-{index}" for index in range(task_count)),
        trials_per_task=trials_per_task,
        plan_digest="test-plan-digest",
        **kwargs,  # type: ignore[arg-type]
    )


def _execute_authorized(
    candidates: tuple[CandidateConfiguration, ...],
    tasks: tuple[FitTask, ...],
    runner: TrialRunner,
    *,
    trials_per_task: int,
    budget_usd: float,
    runner_for_budget=None,
) -> ExecutionReport:
    field = candidates
    if not any(candidate.candidate_id == "baseline" for candidate in field):
        field = (*field, _candidate("baseline", capabilities=1))
    if len(field) == 1:
        field = (*field, _candidate("plan-challenger", capabilities=1))
    plan = _authorized_plan(field, tasks, trials_per_task=trials_per_task, budget_usd=budget_usd)
    return execute_trials(
        field,
        tasks,
        runner,
        trials_per_task=trials_per_task,
        execution_plan=plan,
        runner_for_budget=runner_for_budget or (lambda _remaining: runner),
    )


# --------------------------------------------------------------------------
# Reliability is a constraint, checked before cost.
# --------------------------------------------------------------------------


def test_unreliable_candidate_is_excluded_before_cost_is_considered() -> None:
    candidates = (_candidate("baseline"), _candidate("cheap-but-flaky"))
    report = execute_trials(
        candidates,
        (_task("t1"),),
        _fixed_runner({"baseline": "verified", "cheap-but-flaky": "failed"}, cost=0.01),
        trials_per_task=2,
        simulated=True,
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)
    flaky = next(item for item in result.ranked if item.candidate_id == "cheap-but-flaky")

    # It was the cheapest, and it still lost.
    assert flaky.qualified is False
    assert result.winner_id == "baseline"


def test_adaptive_stopping_abandons_a_candidate_that_cannot_qualify() -> None:
    candidates = (_candidate("doomed"),)
    report = execute_trials(
        candidates,
        (_task("t1"), _task("t2")),
        _fixed_runner({"doomed": "failed"}),
        trials_per_task=3,
        reliability_floor=1.0,
        simulated=True,
    )

    assert report.trials_skipped > 0
    assert report.trials_run < 6  # would have been 2 tasks x 3 trials


def test_adaptive_stopping_never_cuts_off_a_candidate_that_could_still_win() -> None:
    candidates = (_candidate("perfect"),)
    report = execute_trials(
        candidates,
        (_task("t1"), _task("t2")),
        _fixed_runner({"perfect": "verified"}),
        trials_per_task=3,
        reliability_floor=1.0,
        simulated=True,
    )

    assert report.trials_skipped == 0
    assert report.trials_run == 6


def test_infrastructure_failures_do_not_count_against_the_candidate() -> None:
    candidates = (_candidate("victim"),)
    report = execute_trials(
        candidates,
        (_task("t1"),),
        _fixed_runner({"victim": "infrastructure-failure"}),
        trials_per_task=2,
        simulated=True,
    )

    outcome = report.outcomes[0]
    assert outcome.scored_trials == ()
    assert outcome.reliability is None


def test_tasks_not_proven_red_are_skipped_with_a_warning() -> None:
    report = execute_trials(
        (_candidate("a"),),
        (_task("unproven", valid=False),),
        _fixed_runner({}),
        trials_per_task=2,
        simulated=True,
    )

    assert report.trials_run == 0
    assert any("not proven to start red" in warning for warning in report.warnings)


# --------------------------------------------------------------------------
# Cost honesty.
# --------------------------------------------------------------------------


def test_unknown_cost_poisons_the_total_and_blocks_ranking() -> None:
    """A candidate must never win by having less cost data."""

    candidates = (_candidate("mystery"),)
    report = execute_trials(
        candidates,
        (_task("t1"),),
        _fixed_runner({"mystery": "verified"}, cost=None),
        trials_per_task=2,
        simulated=True,
    )

    outcome = report.outcomes[0]
    assert outcome.is_reliable is True
    assert outcome.total_cost_usd is None
    assert outcome.cost_is_complete is False

    result = recommend(report, candidates, task_count=1, trials_per_task=2)
    entry = next(item for item in result.ranked if item.candidate_id == "mystery")
    assert entry.qualified is False
    assert entry.exclusion_reason is not None
    assert "cost is incomplete" in entry.exclusion_reason


# --------------------------------------------------------------------------
# The authorization bounds the campaign, not one trial.
# --------------------------------------------------------------------------


def _outcomes_of(report: ExecutionReport, candidate_id: str) -> list[str]:
    outcome = next(item for item in report.outcomes if item.candidate_id == candidate_id)
    return [trial.outcome for trial in outcome.trials]


def test_campaign_stops_once_cumulative_spend_reaches_the_authorization() -> None:
    """Six trials at $0.10 must not run under a $0.25 authorization."""

    candidates = (_candidate("spender"),)
    tasks = (_task("t1"), _task("t2"))
    report = _execute_authorized(
        candidates,
        tasks,
        _fixed_runner({"spender": "verified"}, cost=0.10),
        trials_per_task=3,
        budget_usd=0.25,
    )

    assert report.trials_run == 3
    assert report.trials_skipped_budget == 9
    assert report.spent_usd == pytest.approx(0.3)
    assert report.budget_stop == "a trial exceeded its remaining authorized cap"
    assert any("did not run" in warning for warning in report.warnings)


@pytest.mark.parametrize("reported", [float("nan"), float("inf"), -0.01])
def test_invalid_runtime_cost_stops_without_entering_the_total(
    reported: float,
) -> None:
    report = _execute_authorized(
        (_candidate("spender"),),
        (_task("t1"),),
        _fixed_runner({"spender": "verified"}, cost=reported),
        trials_per_task=2,
        budget_usd=0.25,
    )

    assert report.trials_run == 1
    assert report.spent_usd == 0.0
    assert report.budget_stop == "a trial reported an invalid cost"
    assert _outcomes_of(report, "spender") == ["inconclusive", "skipped-budget"]


def test_unrepresentably_large_runtime_cost_fails_closed_without_traceback() -> None:
    report = _execute_authorized(
        (_candidate("spender"),),
        (_task("t1"),),
        _fixed_runner({"spender": "verified"}, cost=10**400),
        trials_per_task=2,
        budget_usd=0.25,
    )

    assert report.trials_run == 1
    assert report.spent_usd == 0.0
    assert report.budget_stop == "a trial reported an invalid cost"
    assert report.outcomes[0].total_cost_usd is None


def test_observed_over_cap_cost_is_reported_even_though_it_breached_authorization() -> None:
    """Post-return accounting cannot erase a bill the provider already reported."""

    report = _execute_authorized(
        (_candidate("spender"),),
        (_task("t1"),),
        _fixed_runner({"spender": "verified"}, cost=0.30),
        trials_per_task=2,
        budget_usd=0.25,
    )

    assert report.trials_run == 1
    assert report.spent_usd == 0.30
    assert report.budget_stop == "a trial exceeded its remaining authorized cap"
    assert _outcomes_of(report, "spender") == ["inconclusive", "skipped-budget"]
    assert report.outcomes[0].trials[0].cost_usd == 0.30


def test_arbitrary_runner_cannot_hide_live_work_behind_the_simulated_flag() -> None:
    called = False

    def untrusted(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        nonlocal called
        called = True
        return TrialResult(candidate.candidate_id, task.task_id, index, "verified")

    with pytest.raises(PermissionError, match="built-in CTX Fit simulator"):
        execute_trials(
            (_candidate("baseline"),),
            (_task("t1"),),
            untrusted,
            trials_per_task=1,
            simulated=True,
        )

    assert called is False


@pytest.mark.parametrize(
    ("trials_per_task", "reliability_floor"),
    [(True, 1.0), (1, True), (0, 1.0), (1, float("nan"))],
)
def test_executor_refuses_invalid_trial_shape_before_calling_a_runner(
    trials_per_task: object, reliability_floor: object
) -> None:
    called = False

    def runner(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        nonlocal called
        called = True
        return TrialResult(candidate.candidate_id, task.task_id, index, "verified")

    with pytest.raises(ValueError):
        execute_trials(
            (_candidate("baseline"),),
            (_task("t1"),),
            runner,
            trials_per_task=trials_per_task,  # type: ignore[arg-type]
            reliability_floor=reliability_floor,  # type: ignore[arg-type]
            simulated=True,
        )

    assert called is False


def test_non_simulated_execution_without_an_authorized_plan_is_refused() -> None:
    """The lower public executor must not expose an unbounded spending path."""

    with pytest.raises(PermissionError, match="authorized"):
        execute_trials(
            (_candidate("spender"),),
            (_task("t1"), _task("t2")),
            _fixed_runner({"spender": "verified"}, cost=0.10),
            trials_per_task=3,
        )


def test_authorization_is_bound_to_the_exact_candidate_configuration() -> None:
    candidate = _candidate("baseline", capabilities=1)
    changed_material = CapabilityMaterial.from_content(
        capability_id="skill:changed",
        delivery_mode="task-user-context",
        source_identity="test-catalog#skill:changed",
        catalog_entry_digest=hashlib.sha256(b"changed-entry").hexdigest(),
        content="changed material",
    )
    changed = replace(
        candidate,
        capability_ids=("skill:changed",),
        capability_materials=(changed_material,),
    )
    challenger = _candidate("challenger", capabilities=2)
    tasks = (_task("t1"),)
    plan = _authorized_plan((candidate, challenger), tasks, trials_per_task=1, budget_usd=1.0)
    called = False

    def runner(*_args: object) -> TrialResult:
        nonlocal called
        called = True
        raise AssertionError("a mismatched plan reached the runner")

    with pytest.raises(PermissionError, match="candidate field differs"):
        execute_trials(
            (changed, challenger),
            tasks,
            runner,  # type: ignore[arg-type]
            trials_per_task=1,
            execution_plan=plan,
            runner_for_budget=lambda _remaining: runner,  # type: ignore[arg-type]
        )

    assert called is False


def test_authorization_is_bound_to_the_exact_task_definition() -> None:
    candidates = (_candidate("baseline"), _candidate("challenger", capabilities=1))
    task = _task("t1")
    changed = replace(task, verify_command=("python", "unsafe_judge.py"))
    plan = _authorized_plan(candidates, (task,), trials_per_task=1, budget_usd=1.0)

    with pytest.raises(PermissionError, match="task field differs"):
        execute_trials(
            candidates,
            (changed,),
            _fixed_runner({}),
            trials_per_task=1,
            execution_plan=plan,
            runner_for_budget=lambda _remaining: _fixed_runner({}),
        )


def test_a_trial_stopped_by_the_budget_is_not_blamed_on_the_candidate() -> None:
    """Running out of money says nothing about whether a configuration works."""

    candidates = (_candidate("spender"),)
    tasks = (_task("t1"), _task("t2"))
    report = _execute_authorized(
        candidates,
        tasks,
        _fixed_runner({"spender": "verified"}, cost=0.10),
        trials_per_task=3,
        budget_usd=0.25,
    )
    outcome = report.outcomes[0]

    assert _outcomes_of(report, "spender").count("skipped-budget") == 3
    assert "failed" not in _outcomes_of(report, "spender")
    # Unrun trials are neither scored against reliability nor counted as
    # unmeasured spend: they are absent spend.
    assert len(outcome.scored_trials) == 2
    assert outcome.reliability == 1.0
    assert outcome.total_cost_usd == pytest.approx(0.3)


def test_the_authorization_covers_the_campaign_not_each_candidate() -> None:
    """A spent budget cannot be renewed by moving on to the next candidate."""

    candidates = (
        _candidate("baseline"),
        _candidate("second", capabilities=1),
        _candidate("third", capabilities=2),
    )
    tasks = (_task("t1"),)
    report = _execute_authorized(
        candidates,
        tasks,
        _fixed_runner({}, cost=0.10),
        trials_per_task=2,
        budget_usd=0.20,
    )

    assert report.trials_run == 2
    assert _outcomes_of(report, "second") == ["skipped-budget", "skipped-budget"]
    assert _outcomes_of(report, "third") == ["skipped-budget", "skipped-budget"]

    result = recommend(report, candidates, task_count=1, trials_per_task=2)
    unrun = next(item for item in result.ranked if item.candidate_id == "third")
    assert unrun.qualified is False


def test_each_trial_is_capped_by_what_the_authorization_has_left() -> None:
    """The cap must shrink as the campaign spends, not repeat a constant."""

    caps: list[float] = []

    def runner_for_budget(remaining: float) -> TrialRunner:
        caps.append(remaining)
        # Spend exactly half of whatever this trial was authorized.
        return _fixed_runner({}, cost=round(remaining / 2, 6))

    candidates = (_candidate("halver"),)
    tasks = (_task("t1"), _task("t2"))
    report = _execute_authorized(
        candidates,
        tasks,
        _fixed_runner({}, cost=0.10),
        trials_per_task=3,
        budget_usd=1.0,
        runner_for_budget=runner_for_budget,
    )

    assert caps == pytest.approx(
        [
            1.0,
            0.5,
            0.25,
            0.125,
            0.0625,
            0.03125,
            0.015625,
            0.007813,
            0.003907,
            0.001954,
            0.000977,
            0.000488,
        ]
    )
    assert report.trials_run == 12
    # Every planned slot ran, and the authorization was never exceeded.
    assert report.spent_usd is not None and report.spent_usd < 1.0


def test_a_trial_with_unknown_cost_halts_the_campaign() -> None:
    """Spend that cannot be measured cannot be counted; continuing is blind."""

    candidates = (_candidate("mystery"),)
    tasks = (_task("t1"), _task("t2"))
    report = _execute_authorized(
        candidates,
        tasks,
        _fixed_runner({}, cost=None),
        trials_per_task=3,
        budget_usd=100.0,
    )

    assert report.trials_run == 1
    assert report.trials_skipped_budget == 11
    assert "no cost" in report.budget_stop


def test_an_authorization_of_nothing_runs_nothing() -> None:
    """$0 approved means $0 spendable, not "the first trial is free"."""

    candidates = (_candidate("spender"),)
    tasks = (_task("t1"),)
    report = _execute_authorized(
        candidates,
        tasks,
        _fixed_runner({}, cost=0.10),
        trials_per_task=2,
        budget_usd=0.0,
    )

    assert report.trials_run == 0
    assert report.trials_skipped_budget == 4
    assert report.spent_usd == 0.0


def test_a_per_trial_cap_without_an_authorization_is_refused() -> None:
    with pytest.raises(PermissionError, match="authorized"):
        execute_trials(
            (_candidate("a"),),
            (_task("t1"),),
            _fixed_runner({}),
            runner_for_budget=lambda remaining: _fixed_runner({}),
        )


def test_the_report_states_what_was_authorized_and_what_was_spent() -> None:
    candidates = (_candidate("spender"),)
    tasks = (_task("t1"), _task("t2"))
    report = _execute_authorized(
        candidates,
        tasks,
        _fixed_runner({"spender": "verified"}, cost=0.10),
        trials_per_task=3,
        budget_usd=0.25,
    )
    payload = json.loads(json.dumps(report.to_dict(), sort_keys=True))

    assert payload["budget_usd"] == 0.25
    assert payload["spent_usd"] == pytest.approx(0.3)
    assert payload["trials_skipped_budget"] == 9
    assert payload["budget_stop"]


def test_trial_stop_reason_and_logs_are_preserved_in_serialization() -> None:
    """Stop attribution must survive the execution boundary for later audit."""

    trial = TrialResult(
        "baseline",
        "task-a",
        0,
        "inconclusive",
        cost_usd=0.25,
        stop_reason="cost_budget",
        logs="agent stopped after reaching the per-trial cap",
    )

    payload = json.loads(json.dumps(trial.to_dict(), sort_keys=True))

    assert payload["stop_reason"] == "cost_budget"
    assert payload["logs"] == "agent stopped after reaching the per-trial cap"


# --------------------------------------------------------------------------
# The lexicographic rule.
# --------------------------------------------------------------------------


def test_cheapest_reliable_candidate_wins() -> None:
    candidates = (_candidate("baseline"), _candidate("cheaper"))
    runner = make_simulated_runner(
        outcome_by_candidate={"baseline": "verified", "cheaper": "verified"},
        cost_by_candidate={"baseline": 0.50, "cheaper": 0.10},
    )

    report = execute_trials(candidates, (_task("t1"),), runner, trials_per_task=2, simulated=True)
    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "recommend-change"
    assert result.winner_id == "cheaper"
    assert any("less than the baseline" in line for line in result.reasoning)
    assert any(
        "does not prove that code under test cannot deliberately" in limitation
        for limitation in result.limitations
    )
    assert any(
        "already available in the repository" in limitation
        and "isolated HOME" in limitation
        and "without network access" in limitation
        for limitation in result.limitations
    )


def test_ties_break_toward_the_simpler_configuration() -> None:
    candidates = (_candidate("baseline", capabilities=5), _candidate("simple", capabilities=1))
    report = execute_trials(
        candidates, (_task("t1"),), _fixed_runner({}), trials_per_task=2, simulated=True
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.winner_id == "simple"


def test_keeping_the_current_setup_is_a_valid_verdict() -> None:
    candidates = (_candidate("baseline"), _candidate("pricier"))
    runner = make_simulated_runner(
        outcome_by_candidate={"baseline": "verified", "pricier": "verified"},
        cost_by_candidate={"baseline": 0.10, "pricier": 0.90},
    )

    report = execute_trials(candidates, (_task("t1"),), runner, trials_per_task=2, simulated=True)
    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "keep-current"
    assert result.winner_id == "baseline"


def test_no_qualifying_candidate_yields_no_verdict() -> None:
    candidates = (_candidate("a"), _candidate("b"))
    report = execute_trials(
        candidates,
        (_task("t1"),),
        _fixed_runner({"a": "failed", "b": "failed"}),
        trials_per_task=2,
        simulated=True,
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "no-verdict"
    assert result.winner_id is None


def test_one_sided_partial_or_undeclared_evidence_never_yields_a_verdict() -> None:
    baseline = _candidate("baseline")
    challenger = _candidate("challenger", capabilities=1)

    one_sided = recommend(
        _report((baseline,), (_outcome("baseline", [("verified", 0.1)]),), trials_per_task=1),
        (baseline,),
        task_count=1,
        trials_per_task=1,
    )
    partial = recommend(
        _report(
            (baseline, challenger),
            (
                _outcome("baseline", [("verified", 0.1), ("verified", 0.1)]),
                _outcome("challenger", [("verified", 0.01)]),
            ),
        ),
        (baseline, challenger),
        task_count=1,
        trials_per_task=2,
    )
    injected = recommend(
        _report(
            (baseline, challenger),
            (
                _outcome("baseline", [("verified", 0.1)]),
                _outcome("challenger", [("verified", 0.2)]),
                _outcome("injected", [("verified", 0.0)]),
            ),
            trials_per_task=1,
        ),
        (baseline, challenger),
        task_count=1,
        trials_per_task=1,
    )

    assert {one_sided.verdict, partial.verdict, injected.verdict} == {"no-verdict"}


def test_cost_budget_stop_is_campaign_incompletion_even_without_a_skipped_row() -> None:
    candidates = (_candidate("baseline"), _candidate("challenger", capabilities=1))
    report = _report(
        candidates,
        (
            CandidateOutcome(
                "baseline",
                (
                    TrialResult("baseline", "task-0", 0, "verified", cost_usd=0.1),
                    TrialResult(
                        "baseline",
                        "task-0",
                        1,
                        "inconclusive",
                        cost_usd=0.1,
                        stop_reason="cost_budget",
                    ),
                ),
                reliability_floor=1.0,
            ),
            _outcome("challenger", [("verified", 0.2), ("verified", 0.2)]),
        ),
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "no-verdict"
    assert any("did not complete" in line for line in result.reasoning)


# --------------------------------------------------------------------------
# A failure of ours is not the candidate's, in cost as well as in reliability.
# --------------------------------------------------------------------------


def _outcome(candidate_id: str, rows: list[tuple[str, float | None]]) -> CandidateOutcome:
    return CandidateOutcome(
        candidate_id=candidate_id,
        trials=tuple(
            TrialResult(candidate_id, "task-0", index, outcome, cost_usd=cost)  # type: ignore[arg-type]
            for index, (outcome, cost) in enumerate(rows)
        ),
        reliability_floor=1.0,
    )


def _by_task_runner(unusable_task: str, cost_by_candidate: dict[str, float]):
    """Every candidate verifies, except on one task nothing can prepare."""
    return make_simulated_runner(
        outcome_by_task={unusable_task: "infrastructure-failure"},
        cost_by_candidate=cost_by_candidate,
        default_verified_rate=1.0,
        known_zero_infrastructure=True,
    )


def test_one_unusable_task_does_not_make_every_cost_unknown() -> None:
    """FITBUG-012: a root commit no one can revert must not disqualify the field."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = execute_trials(
        candidates,
        (_task("t1"), _task("unusable")),
        _by_task_runner("unusable", {"baseline": 0.50, "lean": 0.10}),
        trials_per_task=3,
        simulated=True,
    )

    costs = {outcome.candidate_id: outcome.total_cost_usd for outcome in report.outcomes}
    assert costs["baseline"] == 1.5
    assert costs["lean"] == pytest.approx(0.3)

    result = recommend(report, candidates, task_count=2, trials_per_task=3)
    assert result.verdict == "no-verdict"
    assert result.winner_id is None
    assert any("infrastructure reasons" in item for item in result.limitations)


def test_an_infrastructure_failure_does_not_halt_a_budgeted_campaign() -> None:
    """It spent nothing, so there is no untracked spend to stop for."""

    candidates = (_candidate("baseline"),)
    tasks = (_task("t1"), _task("unusable"))
    report = _execute_authorized(
        candidates,
        tasks,
        _by_task_runner("unusable", {"baseline": 0.10}),
        trials_per_task=3,
        budget_usd=10.0,
    )

    assert report.budget_stop == ""
    assert report.trials_run == 12
    assert report.trials_skipped_budget == 0
    assert report.spent_usd == pytest.approx(0.45)


def test_an_inconclusive_trial_is_not_scored_against_the_candidate() -> None:
    """FITBUG-013: a timeout taught us nothing, so it cannot count as a failure."""

    candidates = (_candidate("baseline"),)
    runner = make_simulated_runner(
        outcome_by_trial={("baseline", "t1", 0): "inconclusive"},
        default_verified_rate=1.0,
        cost_per_trial=0.10,
    )

    report = execute_trials(
        candidates,
        (_task("t1"), _task("t2"), _task("t3")),
        runner,
        trials_per_task=3,
        simulated=True,
    )
    outcome = report.outcomes[0]

    # The eight trials after the timeout still ran, and all of them verified.
    assert report.trials_run == 9
    assert report.trials_skipped == 0
    assert outcome.reliability == 1.0
    assert outcome.is_reliable is True

    result = recommend(report, candidates, task_count=3, trials_per_task=3)
    assert result.verdict == "no-verdict"
    # Evidence was still lost, and the report says so rather than quietly
    # reweighting.
    assert any("inconclusively" in item for item in result.limitations)


# --------------------------------------------------------------------------
# Candidates are only compared when they were given the same chance.
# --------------------------------------------------------------------------


def test_a_budget_truncated_candidate_cannot_win_on_fewer_trials() -> None:
    """One trial at $0.50 is not cheaper than nine at $0.50; it is less work."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    tasks = (_task("t1"), _task("t2"), _task("t3"))
    report = _execute_authorized(
        candidates,
        tasks,
        _fixed_runner({}, cost=0.50),
        trials_per_task=3,
        budget_usd=5.0,
    )
    lean = next(item for item in report.outcomes if item.candidate_id == "lean")
    assert lean.budget_truncated is True

    result = recommend(report, candidates, task_count=3, trials_per_task=3)
    ranked_lean = next(item for item in result.ranked if item.candidate_id == "lean")

    assert ranked_lean.qualified is False
    assert ranked_lean.exclusion_reason is not None
    assert "budget ran out" in ranked_lean.exclusion_reason
    assert result.winner_id is None
    assert result.verdict == "no-verdict"
    assert any("did not finish inside the authorized budget" in i for i in result.limitations)
    assert not any("across the same tasks" in line for line in result.reasoning)


def test_budget_truncation_cannot_keep_current_after_a_partial_failure() -> None:
    """A failed first turn does not make an unfinished comparison complete."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = _report(
        candidates,
        (
            _outcome("baseline", [("verified", 0.10), ("verified", 0.10)]),
            _outcome("lean", [("failed", 0.10), ("skipped-budget", None)]),
        ),
        trials_skipped_budget=1,
        budget_stop="the authorized budget was spent",
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "no-verdict"
    assert result.winner_id is None
    assert any("budget-truncated" in line for line in result.reasoning)


def test_a_one_sided_campaign_cannot_keep_the_only_candidate_that_ran() -> None:
    """Missing evidence for a contender invalidates the comparison as a whole."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = _report(
        candidates,
        (_outcome("baseline", [("verified", 0.10), ("verified", 0.10)]),),
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "no-verdict"
    assert result.winner_id is None
    assert any("lean" in line and "not evaluated" in line for line in result.reasoning)


# --------------------------------------------------------------------------
# The report says which check actually failed, and prints in ranking order.
# --------------------------------------------------------------------------


def test_no_verdict_names_the_exclusion_that_actually_fired() -> None:
    """FITBUG-046: every candidate verified every trial; reliability is not the reason."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = _report(
        candidates,
        (
            _outcome("baseline", [("verified", 0.10), ("verified", None)]),
            _outcome("lean", [("verified", 0.02), ("verified", None)]),
        ),
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "no-verdict"
    assert all(item.reliability == 1.0 for item in result.ranked)
    joined = " ".join(result.reasoning)
    assert "never measured" in joined
    assert "reliability floor" not in joined


def test_an_unreliable_field_is_still_reported_as_unreliable() -> None:
    """The reliability sentence must survive: it is right when it is right."""

    candidates = (_candidate("a"),)
    report = _report(candidates, (_outcome("a", [("failed", 0.10), ("verified", 0.10)]),))

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "no-verdict"
    assert any("reliability floor" in line for line in result.reasoning)


def test_the_table_is_ordered_by_the_rule_that_chose_the_winner() -> None:
    """FITBUG-069: the winner cannot be printed below candidates it beat."""

    free_field = (_candidate("baseline"), _candidate("local-free", capabilities=1))
    free_report = _report(
        free_field,
        (
            _outcome("baseline", [("verified", 0.50), ("verified", 0.50)]),
            _outcome("local-free", [("verified", 0.0), ("verified", 0.0)]),
        ),
    )
    free = recommend(free_report, free_field, task_count=1, trials_per_task=2)

    assert free.winner_id == "local-free"
    assert [item.candidate_id for item in free.ranked] == ["local-free", "baseline"]

    tied_field = (_candidate("baseline", capabilities=5), _candidate("lean", capabilities=1))
    tied_report = _report(
        tied_field,
        (
            _outcome("baseline", [("verified", 0.09), ("verified", 0.09)]),
            _outcome("lean", [("verified", 0.09), ("verified", 0.09)]),
        ),
    )
    tied = recommend(tied_report, tied_field, task_count=1, trials_per_task=2)

    assert tied.winner_id == "lean"
    assert [item.candidate_id for item in tied.ranked] == ["lean", "baseline"]


def test_keep_current_names_the_setup_that_is_being_kept() -> None:
    """FITBUG-070: a winner that is not the baseline contradicts the headline."""

    candidates = (_candidate("baseline", capabilities=3), _candidate("lean", capabilities=3))
    report = _report(
        candidates,
        (
            _outcome("baseline", [("verified", 0.10), ("verified", 0.10)]),
            _outcome("lean", [("verified", 0.10), ("verified", 0.10)]),
        ),
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "keep-current"
    assert result.winner_id == "baseline"


# --------------------------------------------------------------------------
# Simulation must never masquerade as evidence.
# --------------------------------------------------------------------------


def test_a_single_simulated_trial_quarantines_the_whole_recommendation() -> None:
    """FITBUG-065: the per-trial flag is the durable one, so it must be read."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = _report(
        candidates,
        (
            CandidateOutcome(
                candidate_id="baseline",
                trials=(TrialResult("baseline", "task-0", 0, "verified", cost_usd=0.50),),
                reliability_floor=1.0,
            ),
            CandidateOutcome(
                candidate_id="lean",
                trials=(
                    TrialResult("lean", "task-0", 0, "verified", cost_usd=0.01, simulated=True),
                ),
                reliability_floor=1.0,
            ),
        ),
        trials_per_task=1,
        simulated=False,  # the caller's flag disagrees with the evidence
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=1)

    assert result.simulated is True
    assert result.headline.startswith("SIMULATED")
    assert result.confidence == "low"


def test_candidate_level_simulation_marker_cannot_masquerade_as_real_evidence() -> None:
    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = _report(
        candidates,
        (
            replace(
                _outcome("baseline", [("verified", 0.50), ("verified", 0.50)]),
                simulated=True,
            ),
            _outcome("lean", [("verified", 0.10), ("verified", 0.10)]),
        ),
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.simulated is True
    assert result.headline.startswith("SIMULATED")
    assert result.confidence == "low"


def test_boolean_trial_index_cannot_impersonate_slot_zero() -> None:
    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = _report(
        candidates,
        (
            CandidateOutcome(
                "baseline",
                (TrialResult("baseline", "task-0", False, "verified", cost_usd=0.50),),
                1.0,
            ),
            CandidateOutcome(
                "lean",
                (TrialResult("lean", "task-0", False, "verified", cost_usd=0.10),),
                1.0,
            ),
        ),
        trials_per_task=1,
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=1)

    assert result.verdict == "no-verdict"
    assert result.winner_id is None
    assert any("trial slot" in line for line in result.reasoning)


def test_boolean_requested_shape_cannot_impersonate_one_slot() -> None:
    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = _report(
        candidates,
        (
            _outcome("baseline", [("verified", 0.50)]),
            _outcome("lean", [("verified", 0.10)]),
        ),
        trials_per_task=1,
    )

    result = recommend(
        report,
        candidates,
        task_count=True,  # type: ignore[arg-type]
        trials_per_task=True,  # type: ignore[arg-type]
    )

    assert result.verdict == "no-verdict"
    assert result.winner_id is None


def test_simulated_results_are_labelled_everywhere() -> None:
    candidates = (_candidate("a"),)
    report = execute_trials(
        candidates, (_task("t1"),), _fixed_runner({}), trials_per_task=2, simulated=True
    )
    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert report.simulated is True
    assert any("SIMULATED RUN" in warning for warning in report.warnings)
    assert result.simulated is True
    assert result.headline.startswith("SIMULATED")
    assert any("says nothing about which configuration" in item for item in result.limitations)
    # A simulated run can never claim high confidence.
    assert result.confidence == "low"


def test_simulator_is_deterministic() -> None:
    runner = make_simulated_runner(verified_rate_by_candidate={"a": 0.5})
    candidate, task = _candidate("a"), _task("t1")

    first = runner(candidate, task, 0)
    second = runner(candidate, task, 0)

    assert first.outcome == second.outcome


def test_recommendation_is_serializable_and_versioned() -> None:
    candidates = (_candidate("a"),)
    report = execute_trials(
        candidates, (_task("t1"),), _fixed_runner({}), trials_per_task=2, simulated=True
    )
    payload = recommend(report, candidates, task_count=1, trials_per_task=2).to_dict()

    encoded = json.loads(json.dumps(payload, sort_keys=True))
    assert encoded["schema"] == "ctx.fit.recommendation-v1"
    assert encoded["simulated"] is True
