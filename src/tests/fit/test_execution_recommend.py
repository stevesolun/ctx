from __future__ import annotations

import json

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import (
    TrialResult,
    execute_trials,
    make_simulated_runner,
)
from ctx.fit.recommend import recommend
from ctx.fit.tasks import FitTask

VERIFY = ("python", "-m", "pytest", "-q")


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
    return CandidateConfiguration(
        candidate_id=name,
        role="baseline" if name == "baseline" else "recommended",
        capability_ids=tuple(f"skill:c{index}" for index in range(capabilities)),
        model=None,
        instructions=(),
        selection_reason="test fixture candidate used to exercise the selection rule",
    )


def _fixed_runner(outcomes: dict[str, str], cost: float | None = 0.10):
    def run(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        return TrialResult(
            candidate_id=candidate.candidate_id,
            task_id=task.task_id,
            trial_index=index,
            outcome=outcomes.get(candidate.candidate_id, "verified"),  # type: ignore[arg-type]
            cost_usd=cost,
            simulated=True,
        )

    return run


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
# The lexicographic rule.
# --------------------------------------------------------------------------


def test_cheapest_reliable_candidate_wins() -> None:
    candidates = (_candidate("baseline"), _candidate("cheaper"))

    def run(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        cost = 0.50 if candidate.candidate_id == "baseline" else 0.10
        return TrialResult(
            candidate_id=candidate.candidate_id,
            task_id=task.task_id,
            trial_index=index,
            outcome="verified",
            cost_usd=cost,
            simulated=True,
        )

    report = execute_trials(candidates, (_task("t1"),), run, trials_per_task=2, simulated=True)
    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.verdict == "recommend-change"
    assert result.winner_id == "cheaper"
    assert any("less than the baseline" in line for line in result.reasoning)


def test_ties_break_toward_the_simpler_configuration() -> None:
    candidates = (_candidate("complex", capabilities=5), _candidate("simple", capabilities=1))
    report = execute_trials(
        candidates, (_task("t1"),), _fixed_runner({}), trials_per_task=2, simulated=True
    )

    result = recommend(report, candidates, task_count=1, trials_per_task=2)

    assert result.winner_id == "simple"


def test_keeping_the_current_setup_is_a_valid_verdict() -> None:
    candidates = (_candidate("baseline"), _candidate("pricier"))

    def run(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        cost = 0.10 if candidate.candidate_id == "baseline" else 0.90
        return TrialResult(
            candidate_id=candidate.candidate_id,
            task_id=task.task_id,
            trial_index=index,
            outcome="verified",
            cost_usd=cost,
            simulated=True,
        )

    report = execute_trials(candidates, (_task("t1"),), run, trials_per_task=2, simulated=True)
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


# --------------------------------------------------------------------------
# Simulation must never masquerade as evidence.
# --------------------------------------------------------------------------


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
