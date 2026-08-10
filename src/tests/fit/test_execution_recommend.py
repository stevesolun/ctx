from __future__ import annotations

import json

import pytest

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import (
    CandidateOutcome,
    ExecutionReport,
    TrialResult,
    TrialRunner,
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
# The authorization bounds the campaign, not one trial.
# --------------------------------------------------------------------------


def _outcomes_of(report: ExecutionReport, candidate_id: str) -> list[str]:
    outcome = next(item for item in report.outcomes if item.candidate_id == candidate_id)
    return [trial.outcome for trial in outcome.trials]


def test_campaign_stops_once_cumulative_spend_reaches_the_authorization() -> None:
    """Six trials at $0.10 must not run under a $0.25 authorization."""

    candidates = (_candidate("spender"),)
    report = execute_trials(
        candidates,
        (_task("t1"), _task("t2")),
        _fixed_runner({"spender": "verified"}, cost=0.10),
        trials_per_task=3,
        budget_usd=0.25,
    )

    assert report.trials_run == 3
    assert report.trials_skipped_budget == 3
    assert report.spent_usd == 0.3
    assert report.budget_stop == "the authorized budget was spent"
    assert any("did not run" in warning for warning in report.warnings)


def test_without_a_budget_nothing_stops_the_campaign() -> None:
    """The unbounded path is what FITBUG-001 was: keep it visible in the suite."""

    report = execute_trials(
        (_candidate("spender"),),
        (_task("t1"), _task("t2")),
        _fixed_runner({"spender": "verified"}, cost=0.10),
        trials_per_task=3,
    )

    assert report.trials_run == 6
    assert report.trials_skipped_budget == 0
    assert report.budget_stop == ""
    assert report.spent_usd is None


def test_a_trial_stopped_by_the_budget_is_not_blamed_on_the_candidate() -> None:
    """Running out of money says nothing about whether a configuration works."""

    candidates = (_candidate("spender"),)
    report = execute_trials(
        candidates,
        (_task("t1"), _task("t2")),
        _fixed_runner({"spender": "verified"}, cost=0.10),
        trials_per_task=3,
        budget_usd=0.25,
    )
    outcome = report.outcomes[0]

    assert _outcomes_of(report, "spender").count("skipped-budget") == 3
    assert "failed" not in _outcomes_of(report, "spender")
    # Unrun trials are neither scored against reliability nor counted as
    # unmeasured spend: they are absent spend.
    assert len(outcome.scored_trials) == 3
    assert outcome.reliability == 1.0
    assert outcome.total_cost_usd == 0.3


def test_the_authorization_covers_the_campaign_not_each_candidate() -> None:
    """A spent budget cannot be renewed by moving on to the next candidate."""

    candidates = (_candidate("first"), _candidate("second"), _candidate("third"))
    report = execute_trials(
        candidates,
        (_task("t1"),),
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

    report = execute_trials(
        (_candidate("halver"),),
        (_task("t1"), _task("t2")),
        _fixed_runner({}, cost=0.10),
        trials_per_task=3,
        budget_usd=1.0,
        runner_for_budget=runner_for_budget,
    )

    assert caps == [1.0, 0.5, 0.25, 0.125, 0.0625, 0.03125]
    assert report.trials_run == 6
    # Six trials, and the authorization was never exceeded.
    assert report.spent_usd is not None and report.spent_usd < 1.0


def test_a_trial_with_unknown_cost_halts_the_campaign() -> None:
    """Spend that cannot be measured cannot be counted; continuing is blind."""

    report = execute_trials(
        (_candidate("mystery"),),
        (_task("t1"), _task("t2")),
        _fixed_runner({}, cost=None),
        trials_per_task=3,
        budget_usd=100.0,
    )

    assert report.trials_run == 1
    assert report.trials_skipped_budget == 5
    assert "no cost" in report.budget_stop


def test_an_authorization_of_nothing_runs_nothing() -> None:
    """$0 approved means $0 spendable, not "the first trial is free"."""

    report = execute_trials(
        (_candidate("spender"),),
        (_task("t1"),),
        _fixed_runner({}, cost=0.10),
        trials_per_task=2,
        budget_usd=0.0,
    )

    assert report.trials_run == 0
    assert report.trials_skipped_budget == 2
    assert report.spent_usd == 0.0


def test_a_per_trial_cap_without_an_authorization_is_refused() -> None:
    with pytest.raises(ValueError, match="budget_usd"):
        execute_trials(
            (_candidate("a"),),
            (_task("t1"),),
            _fixed_runner({}),
            runner_for_budget=lambda remaining: _fixed_runner({}),
        )


def test_the_report_states_what_was_authorized_and_what_was_spent() -> None:
    report = execute_trials(
        (_candidate("spender"),),
        (_task("t1"), _task("t2")),
        _fixed_runner({"spender": "verified"}, cost=0.10),
        trials_per_task=3,
        budget_usd=0.25,
    )
    payload = json.loads(json.dumps(report.to_dict(), sort_keys=True))

    assert payload["budget_usd"] == 0.25
    assert payload["spent_usd"] == 0.3
    assert payload["trials_skipped_budget"] == 3
    assert payload["budget_stop"]


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
# A failure of ours is not the candidate's, in cost as well as in reliability.
# --------------------------------------------------------------------------


def _outcome(candidate_id: str, rows: list[tuple[str, float | None]]) -> CandidateOutcome:
    return CandidateOutcome(
        candidate_id=candidate_id,
        trials=tuple(
            TrialResult(candidate_id, f"t{index}", 0, outcome, cost_usd=cost)  # type: ignore[arg-type]
            for index, (outcome, cost) in enumerate(rows)
        ),
        reliability_floor=1.0,
    )


def _by_task_runner(unusable_task: str, cost_by_candidate: dict[str, float]):
    """Every candidate verifies, except on one task nothing can prepare."""

    def run(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        if task.task_id == unusable_task:
            return TrialResult(
                candidate_id=candidate.candidate_id,
                task_id=task.task_id,
                trial_index=index,
                outcome="infrastructure-failure",
                detail="could not revert source paths: no parent commit",
            )
        return TrialResult(
            candidate_id=candidate.candidate_id,
            task_id=task.task_id,
            trial_index=index,
            outcome="verified",
            cost_usd=cost_by_candidate[candidate.candidate_id],
        )

    return run


def test_one_unusable_task_does_not_make_every_cost_unknown() -> None:
    """FITBUG-012: a root commit no one can revert must not disqualify the field."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = execute_trials(
        candidates,
        (_task("t1"), _task("unusable")),
        _by_task_runner("unusable", {"baseline": 0.50, "lean": 0.10}),
        trials_per_task=3,
    )

    costs = {outcome.candidate_id: outcome.total_cost_usd for outcome in report.outcomes}
    assert costs == {"baseline": 1.5, "lean": 0.3}

    result = recommend(report, candidates, task_count=2, trials_per_task=3)
    assert result.verdict == "recommend-change"
    assert result.winner_id == "lean"
    assert any("infrastructure reasons" in item for item in result.limitations)


def test_an_infrastructure_failure_does_not_halt_a_budgeted_campaign() -> None:
    """It spent nothing, so there is no untracked spend to stop for."""

    report = execute_trials(
        (_candidate("baseline"),),
        (_task("t1"), _task("unusable")),
        _by_task_runner("unusable", {"baseline": 0.10}),
        trials_per_task=3,
        budget_usd=10.0,
    )

    assert report.budget_stop == ""
    assert report.trials_run == 6
    assert report.trials_skipped_budget == 0
    assert report.spent_usd == 0.3


def test_an_inconclusive_trial_is_not_scored_against_the_candidate() -> None:
    """FITBUG-013: a timeout taught us nothing, so it cannot count as a failure."""

    candidates = (_candidate("baseline"),)
    seen = {"count": 0}

    def run(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        seen["count"] += 1
        outcome = "inconclusive" if seen["count"] == 1 else "verified"
        return TrialResult(
            candidate_id=candidate.candidate_id,
            task_id=task.task_id,
            trial_index=index,
            outcome=outcome,  # type: ignore[arg-type]
            cost_usd=0.10,
            detail="verification did not complete: timed out" if outcome == "inconclusive" else "",
        )

    report = execute_trials(
        candidates, (_task("t1"), _task("t2"), _task("t3")), run, trials_per_task=3
    )
    outcome = report.outcomes[0]

    # The eight trials after the timeout still ran, and all of them verified.
    assert report.trials_run == 9
    assert report.trials_skipped == 0
    assert outcome.reliability == 1.0
    assert outcome.is_reliable is True

    result = recommend(report, candidates, task_count=3, trials_per_task=3)
    assert result.verdict != "no-verdict"
    # Evidence was still lost, and the report says so rather than quietly
    # reweighting.
    assert any("inconclusively" in item for item in result.limitations)


# --------------------------------------------------------------------------
# Candidates are only compared when they were given the same chance.
# --------------------------------------------------------------------------


def test_a_budget_truncated_candidate_cannot_win_on_fewer_trials() -> None:
    """One trial at $0.50 is not cheaper than nine at $0.50; it is less work."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = execute_trials(
        candidates,
        (_task("t1"), _task("t2"), _task("t3")),
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
    assert result.winner_id == "baseline"
    assert result.verdict == "keep-current"
    assert any("did not finish inside the authorized budget" in i for i in result.limitations)
    assert not any("across the same tasks" in line for line in result.reasoning)


# --------------------------------------------------------------------------
# The report says which check actually failed, and prints in ranking order.
# --------------------------------------------------------------------------


def test_no_verdict_names_the_exclusion_that_actually_fired() -> None:
    """FITBUG-046: every candidate verified every trial; reliability is not the reason."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = ExecutionReport(
        outcomes=(
            _outcome("baseline", [("verified", 0.10), ("verified", None)]),
            _outcome("lean", [("verified", 0.02), ("verified", None)]),
        )
    )

    result = recommend(report, candidates, task_count=3, trials_per_task=2)

    assert result.verdict == "no-verdict"
    assert all(item.reliability == 1.0 for item in result.ranked)
    joined = " ".join(result.reasoning)
    assert "never measured" in joined
    assert "reliability floor" not in joined


def test_an_unreliable_field_is_still_reported_as_unreliable() -> None:
    """The reliability sentence must survive: it is right when it is right."""

    candidates = (_candidate("a"),)
    report = ExecutionReport(outcomes=(_outcome("a", [("failed", 0.10), ("verified", 0.10)]),))

    result = recommend(report, candidates, task_count=3, trials_per_task=2)

    assert result.verdict == "no-verdict"
    assert any("reliability floor" in line for line in result.reasoning)


def test_the_table_is_ordered_by_the_rule_that_chose_the_winner() -> None:
    """FITBUG-069: the winner cannot be printed below candidates it beat."""

    free_field = (_candidate("baseline"), _candidate("local-free", capabilities=1))
    free_report = ExecutionReport(
        outcomes=(
            _outcome("baseline", [("verified", 0.50), ("verified", 0.50)]),
            _outcome("local-free", [("verified", 0.0), ("verified", 0.0)]),
        )
    )
    free = recommend(free_report, free_field, task_count=3, trials_per_task=2)

    assert free.winner_id == "local-free"
    assert [item.candidate_id for item in free.ranked] == ["local-free", "baseline"]

    tied_field = (_candidate("recommended", capabilities=5), _candidate("lean", capabilities=1))
    tied_report = ExecutionReport(
        outcomes=(
            _outcome("recommended", [("verified", 0.09), ("verified", 0.09)]),
            _outcome("lean", [("verified", 0.09), ("verified", 0.09)]),
        )
    )
    tied = recommend(tied_report, tied_field, task_count=3, trials_per_task=2)

    assert tied.winner_id == "lean"
    assert [item.candidate_id for item in tied.ranked] == ["lean", "recommended"]


def test_keep_current_names_the_setup_that_is_being_kept() -> None:
    """FITBUG-070: a winner that is not the baseline contradicts the headline."""

    candidates = (_candidate("baseline", capabilities=3), _candidate("lean", capabilities=1))
    report = ExecutionReport(
        outcomes=(
            _outcome("baseline", [("verified", 0.10), ("verified", 0.10)]),
            _outcome("lean", [("verified", 0.10), ("verified", 0.10)]),
        )
    )

    result = recommend(report, candidates, task_count=3, trials_per_task=2)

    assert result.verdict == "keep-current"
    assert result.winner_id == "baseline"


# --------------------------------------------------------------------------
# Simulation must never masquerade as evidence.
# --------------------------------------------------------------------------


def test_a_single_simulated_trial_quarantines_the_whole_recommendation() -> None:
    """FITBUG-065: the per-trial flag is the durable one, so it must be read."""

    candidates = (_candidate("baseline"), _candidate("lean", capabilities=1))
    report = ExecutionReport(
        outcomes=(
            CandidateOutcome(
                candidate_id="baseline",
                trials=(TrialResult("baseline", "t1", 0, "verified", cost_usd=0.50),),
                reliability_floor=1.0,
            ),
            CandidateOutcome(
                candidate_id="lean",
                trials=(TrialResult("lean", "t1", 0, "verified", cost_usd=0.01, simulated=True),),
                reliability_floor=1.0,
            ),
        ),
        simulated=False,  # the caller's flag disagrees with the evidence
    )

    result = recommend(report, candidates, task_count=3, trials_per_task=1)

    assert result.simulated is True
    assert result.headline.startswith("SIMULATED")
    assert result.confidence == "low"


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
