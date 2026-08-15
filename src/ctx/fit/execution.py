"""Trial execution with adaptive reliability stopping.

One execution is one candidate attempting one task once. Reliability requires
repeating that, because a configuration that works once has not been shown to
work (ADR-014).

Repetition multiplies cost against the very thing the product optimizes, so
trials stop **adaptively**: as soon as a candidate's remaining trials cannot
lift it to the reliability floor, the rest are abandoned. Nothing is learned by
continuing to pay for a candidate that has already lost.

Adaptive stopping saves money; it does not *bound* it. The bound is the user's
authorization, and it binds the campaign rather than any single trial: spend is
accumulated from what each trial reports, and everything still queued is
abandoned the moment that total reaches what was approved. Each trial is
additionally capped at the authorization's *remainder*, so no single execution
can overshoot the total either. A per-trial constant cannot do this job — N
trials at a fixed cap authorize N times that cap, which is not a budget.

Simulation is a first-class mode and is deliberately quarantined. A simulated
run proves the pipeline works; it proves nothing about a repository. Every
simulated result carries an indelible flag, and the recommendation layer refuses
to present simulated evidence as real. The repository already learned this the
hard way: its deterministic provider bridge is permanently marked
"claim-ineligible" for exactly this reason.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypeGuard

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.tasks import FitTask

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ctx.fit.experiment import ExperimentPlan

EXECUTION_SCHEMA = "ctx.fit.execution-v1"

#: A trial's outcome. These are never conflated: "the agent finished" and "the
#: repository proved it" are different facts, an infrastructure failure is not
#: the candidate's fault, and neither is a budget that ran out before its turn.
TrialOutcome = Literal[
    "verified",
    "failed",
    "inconclusive",
    "infrastructure-failure",
    "skipped-adaptive",
    "skipped-budget",
]

#: Outcomes for trials that were never attempted. They cost nothing, so they
#: must not be read as missing cost data.
_UNRUN_OUTCOMES = frozenset({"skipped-adaptive", "skipped-budget"})

#: Fraction of trials that must verify for a candidate to qualify. A candidate
#: below this is excluded before cost is considered at all.
DEFAULT_RELIABILITY_FLOOR = 1.0


def _finite_number(value: object) -> TypeGuard[int | float]:
    """Whether ``value`` is a finite real number representable as a float."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class TrialResult:
    """One candidate, one task, one attempt."""

    candidate_id: str
    task_id: str
    trial_index: int
    outcome: TrialOutcome
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    elapsed_seconds: float | None = None
    simulated: bool = False
    detail: str = ""
    stop_reason: str = ""
    logs: str = ""

    @property
    def counts_toward_reliability(self) -> bool:
        """Only trials the repository actually judged say anything about a candidate.

        Infrastructure failures are ours, not the candidate's. So is an
        inconclusive trial: a verification that timed out or never returned
        taught us nothing, and scoring it as a non-verified trial drove a
        candidate's reliability to zero on its first timeout, abandoned every
        remaining trial by adaptive stopping, and threw away the evidence for a
        configuration that went on to verify everything else (FITBUG-013).
        """

        return self.outcome in {"verified", "failed"}

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": EXECUTION_SCHEMA,
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "trial_index": self.trial_index,
            "outcome": self.outcome,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "elapsed_seconds": self.elapsed_seconds,
            "simulated": self.simulated,
            "detail": self.detail,
            "stop_reason": self.stop_reason,
            "logs": self.logs,
        }


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """Everything observed about one candidate across all its trials."""

    candidate_id: str
    trials: tuple[TrialResult, ...]
    reliability_floor: float
    simulated: bool = False

    @property
    def scored_trials(self) -> tuple[TrialResult, ...]:
        return tuple(trial for trial in self.trials if trial.counts_toward_reliability)

    @property
    def verified_count(self) -> int:
        return sum(1 for trial in self.scored_trials if trial.outcome == "verified")

    @property
    def reliability(self) -> float | None:
        scored = self.scored_trials
        if not scored:
            return None
        return self.verified_count / len(scored)

    @property
    def is_reliable(self) -> bool:
        reliability = self.reliability
        return reliability is not None and reliability >= self.reliability_floor

    @property
    def total_cost_usd(self) -> float | None:
        """Total cost, or None if spend the candidate is answerable for went unmeasured.

        One unknown poisons the total. A candidate must never look cheaper
        because part of its spend went unmeasured (ADR-004). Trials that never
        ran are absent spend, not unmeasured spend. A pre-provider
        infrastructure failure is reported explicitly as $0 by the live runner;
        an infrastructure failure after provider contact remains unknown unless
        the provider returned a measured cost.
        """

        amounts: list[float] = []
        for trial in self.trials:
            if trial.outcome in _UNRUN_OUTCOMES:
                continue
            if trial.cost_usd is None:
                return None
            if not _finite_number(trial.cost_usd) or trial.cost_usd < 0:
                return None
            amounts.append(float(trial.cost_usd))
        return math.fsum(amounts) if amounts else None

    @property
    def budget_truncated(self) -> bool:
        """Did the authorization run out before this candidate finished?

        A candidate that only got through two of its nine trials has a real
        cost for two trials, which is not comparable with a rival's cost for
        nine. Naming that here keeps the comparison honest downstream.
        """

        return any(trial.outcome == "skipped-budget" for trial in self.trials)

    @property
    def cost_is_complete(self) -> bool:
        return self.total_cost_usd is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "trials": [trial.to_dict() for trial in self.trials],
            "verified": self.verified_count,
            "scored": len(self.scored_trials),
            "reliability": self.reliability,
            "reliability_floor": self.reliability_floor,
            "is_reliable": self.is_reliable,
            "total_cost_usd": self.total_cost_usd,
            "cost_is_complete": self.cost_is_complete,
            "budget_truncated": self.budget_truncated,
            "simulated": self.simulated,
        }


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    outcomes: tuple[CandidateOutcome, ...] = ()
    simulated: bool = False
    trials_run: int = 0
    trials_skipped: int = 0
    #: The authorization this campaign ran under, and what was actually charged
    #: against it. ``None`` for both means spend was not being tracked at all.
    budget_usd: float | None = None
    spent_usd: float | None = None
    trials_skipped_budget: int = 0
    #: Why the campaign stopped spending, empty when it ran to completion.
    budget_stop: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)
    #: The exact field and task slots this report claims to cover.  Keeping
    #: these on the evidence artifact prevents a caller from making a partial
    #: report look complete by supplying smaller counts to ``recommend``.
    declared_candidate_ids: tuple[str, ...] = ()
    declared_task_ids: tuple[str, ...] = ()
    trials_per_task: int = 0
    plan_digest: str = ""
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR

    def to_dict(self) -> dict[str, object]:
        return {
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "simulated": self.simulated,
            "trials_run": self.trials_run,
            "trials_skipped": self.trials_skipped,
            "budget_usd": self.budget_usd,
            "spent_usd": self.spent_usd,
            "trials_skipped_budget": self.trials_skipped_budget,
            "budget_stop": self.budget_stop,
            "warnings": list(self.warnings),
            "declared_candidate_ids": list(self.declared_candidate_ids),
            "declared_task_ids": list(self.declared_task_ids),
            "trials_per_task": self.trials_per_task,
            "plan_digest": self.plan_digest,
            "reliability_floor": self.reliability_floor,
        }


#: A runner performs one trial. Injected so the same execution logic drives a
#: real provider, a simulator, or a test double without branching.
TrialRunner = Callable[[CandidateConfiguration, FitTask, int], TrialResult]

#: Builds a runner whose per-execution spend ceiling is the dollars the
#: authorization still has left. Injected rather than a bare runner so the cap
#: handed to a trial shrinks as the campaign spends, instead of being a constant
#: that every trial gets in full.
BudgetedRunnerFactory = Callable[[float], TrialRunner]


def _can_still_qualify(verified: int, completed: int, remaining: int, floor: float) -> bool:
    """Could this candidate still reach the floor if every remaining trial passed?"""

    best_possible_total = completed + remaining
    if best_possible_total == 0:
        return True
    return (verified + remaining) / best_possible_total >= floor


def _unrun_trial(
    candidate: CandidateConfiguration,
    task: FitTask,
    index: int,
    *,
    outcome: TrialOutcome,
    detail: str,
    simulated: bool,
) -> TrialResult:
    """Record a trial that was never attempted, and so never cost anything."""

    return TrialResult(
        candidate_id=candidate.candidate_id,
        task_id=task.task_id,
        trial_index=index,
        outcome=outcome,
        simulated=simulated,
        detail=detail,
    )


_BUDGET_SPENT = "the authorized budget was spent"
_BUDGET_UNTRACKABLE = "a trial reported no cost, so spend can no longer be tracked"
_BUDGET_INVALID = "a trial reported an invalid cost"
_BUDGET_BREACH = "a trial exceeded its remaining authorized cap"


@dataclass(frozen=True, slots=True)
class _DeterministicSimulator:
    """Data-only simulator accepted by the no-spend execution path.

    A public callable is not evidence that no provider is hidden behind it.
    The previous marker attribute could be attached to any function, so
    ``simulated=True`` was an authorization bypass.  This private value owns no
    callable supplied by its caller: it can only select deterministic result
    data and therefore cannot perform I/O when invoked.
    """

    rates: tuple[tuple[str, float], ...]
    candidate_outcomes: tuple[tuple[str, TrialOutcome], ...]
    task_outcomes: tuple[tuple[str, TrialOutcome], ...]
    outcomes: tuple[tuple[tuple[str, str, int], TrialOutcome], ...]
    costs: tuple[tuple[str, float | None], ...]
    default_cost: float | None
    default_rate: float
    known_zero_infrastructure: bool

    def __call__(self, candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        explicit = dict(self.outcomes).get((candidate.candidate_id, task.task_id, index))
        if explicit is None:
            explicit = dict(self.candidate_outcomes).get(candidate.candidate_id)
        if explicit is None:
            explicit = dict(self.task_outcomes).get(task.task_id)
        if explicit is None:
            seed = f"{candidate.candidate_id}:{task.task_id}:{index}"
            draw = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
            rate = dict(self.rates).get(candidate.candidate_id, self.default_rate)
            explicit = "verified" if draw < rate else "failed"
        cost = dict(self.costs).get(candidate.candidate_id, self.default_cost)
        if explicit == "infrastructure-failure":
            cost = 0.0 if self.known_zero_infrastructure else None
        return TrialResult(
            candidate_id=candidate.candidate_id,
            task_id=task.task_id,
            trial_index=index,
            outcome=explicit,
            input_tokens=20_000,
            output_tokens=4_000,
            cost_usd=cost,
            elapsed_seconds=30.0,
            simulated=True,
            detail="simulated trial",
        )


def execute_trials(
    candidates: tuple[CandidateConfiguration, ...],
    tasks: tuple[FitTask, ...],
    runner: TrialRunner,
    *,
    trials_per_task: int = 3,
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR,
    simulated: bool = False,
    budget_usd: float | None = None,
    runner_for_budget: BudgetedRunnerFactory | None = None,
    execution_plan: ExperimentPlan | None = None,
) -> ExecutionReport:
    """Run every candidate against every task, stopping candidates that cannot win.

    Adaptive stopping is a cost control, not a shortcut: a candidate is only
    abandoned once arithmetic shows it can no longer reach the reliability
    floor, so no candidate that could have qualified is ever cut short.

    A non-simulated call requires the exact authorized experiment plan. Its
    budget bounds the campaign as a whole. Trials are charged against it as
    they complete and everything still queued is abandoned once the total
    reaches it, so the campaign cannot bill the user for an experiment they did
    not approve. A trial that reports no cost stops the campaign too: unmeasured
    spend cannot be counted against the authorization, and continuing would be
    spending blind.

    ``runner_for_budget`` supplies the trial runner for a given remaining
    balance. It is mandatory for non-simulated execution: checking the total
    after a trial returns cannot undo an overspend inside that trial.
    """

    if type(trials_per_task) is not int or trials_per_task <= 0:
        raise ValueError("trials_per_task must be a positive integer")
    if not _finite_number(reliability_floor) or not 0 <= reliability_floor <= 1:
        raise ValueError("reliability_floor must be a finite number between 0 and 1")

    if simulated and type(runner) is not _DeterministicSimulator:
        raise PermissionError(
            "simulated=True accepts only the built-in CTX Fit simulator, not an arbitrary runner"
        )

    if not simulated:
        if execution_plan is None or not execution_plan.can_execute:
            raise PermissionError(
                "non-simulated execution requires an authorized, bounded experiment plan"
            )
        plan_budget = execution_plan.budget_usd
        if not _finite_number(plan_budget) or plan_budget < 0:
            raise PermissionError(
                "an authorized execution plan must carry a finite, non-negative budget"
            )
        if (
            not execution_plan.cost.is_known
            or execution_plan.cost.high_usd is None
            or execution_plan.cost.high_usd > plan_budget
        ):
            raise PermissionError("the authorized execution plan is not bounded by its budget")
        if budget_usd is not None and budget_usd != plan_budget:
            raise ValueError("budget_usd differs from the authorized execution plan")
        budget_usd = plan_budget
        if len(candidates) != len(execution_plan.candidates) or not all(
            planned.matches(candidate)
            for planned, candidate in zip(execution_plan.candidates, candidates, strict=True)
        ):
            raise PermissionError("candidate field differs from the authorized execution plan")
        if len(tasks) != len(execution_plan.tasks) or not all(
            planned.matches(task) for planned, task in zip(execution_plan.tasks, tasks, strict=True)
        ):
            raise PermissionError("task field differs from the authorized execution plan")
        if trials_per_task != execution_plan.trials_per_task:
            raise PermissionError("trial count differs from the authorized execution plan")
        if reliability_floor != execution_plan.reliability_floor:
            raise PermissionError("reliability floor differs from the authorized execution plan")
        expected_executions = len(candidates) * len(tasks) * trials_per_task
        if (
            execution_plan.candidate_count != len(candidates)
            or execution_plan.task_count != len(tasks)
            or execution_plan.executions != expected_executions
        ):
            raise PermissionError("campaign size differs from the authorized execution plan")
        if runner_for_budget is None:
            raise PermissionError(
                "non-simulated execution requires a runner capped by the remaining budget"
            )

    if runner_for_budget is not None and budget_usd is None:
        raise ValueError(
            "runner_for_budget derives each trial's cap from the remaining "
            "authorization; without budget_usd there is no authorization to derive it from"
        )

    warnings: list[str] = []
    invalid = [task.task_id for task in tasks if not task.is_valid]
    if invalid:
        warnings.append(
            f"skipped {len(invalid)} task(s) not proven to start red; an unproven "
            "task cannot distinguish a working configuration from a broken one"
        )
    usable = tuple(task for task in tasks if task.is_valid)

    outcomes: list[CandidateOutcome] = []
    run_count = 0
    skip_count = 0
    budget_skip_count = 0
    spend_amounts: list[float] = []
    spent = 0.0
    # Why the campaign stopped spending. Non-empty means no further trial may
    # run, for any candidate: the authorization covers the campaign, not a
    # candidate, so it cannot be renewed by moving on to the next one. A
    # non-positive authorization authorizes nothing, and starts out stopped.
    budget_stop = _BUDGET_SPENT if budget_usd is not None and budget_usd <= 0 else ""

    for candidate in candidates:
        trials: list[TrialResult] = []
        verified = 0
        completed = 0
        abandoned = False
        total = len(usable) * trials_per_task

        for task in usable:
            for index in range(trials_per_task):
                if budget_stop:
                    trials.append(
                        _unrun_trial(
                            candidate,
                            task,
                            index,
                            outcome="skipped-budget",
                            detail=f"not run: {budget_stop}",
                            simulated=simulated,
                        )
                    )
                    skip_count += 1
                    budget_skip_count += 1
                    continue

                remaining = (
                    total
                    - completed
                    - len([trial for trial in trials if trial.outcome == "skipped-adaptive"])
                )
                if abandoned or not _can_still_qualify(
                    verified, completed, max(remaining - 1, 0), reliability_floor
                ):
                    abandoned = True
                    trials.append(
                        _unrun_trial(
                            candidate,
                            task,
                            index,
                            outcome="skipped-adaptive",
                            detail=(
                                "cannot reach the reliability floor; further spend "
                                "would buy nothing"
                            ),
                            simulated=simulated,
                        )
                    )
                    skip_count += 1
                    continue

                trial_runner = runner
                if budget_usd is not None and runner_for_budget is not None:
                    # The cap is what is left, not a constant: a trial must never
                    # be authorized to spend money the campaign no longer has.
                    trial_runner = runner_for_budget(budget_usd - spent)

                result = trial_runner(candidate, task, index)
                trials.append(result)
                run_count += 1
                if result.counts_toward_reliability:
                    completed += 1
                    if result.outcome == "verified":
                        verified += 1

                if budget_usd is None:
                    continue
                if result.cost_usd is None:
                    budget_stop = _BUDGET_UNTRACKABLE
                    continue
                remaining_budget = budget_usd - spent
                invalid_cost = not _finite_number(result.cost_usd) or result.cost_usd < 0
                over_cap = (
                    not invalid_cost
                    and isinstance(result.cost_usd, int | float)
                    and result.cost_usd > remaining_budget
                )
                if invalid_cost or over_cap:
                    observed_cost = (
                        float(result.cost_usd)
                        if over_cap and isinstance(result.cost_usd, int | float)
                        else None
                    )
                    if observed_cost is not None:
                        spend_amounts.append(observed_cost)
                        spent = math.fsum(spend_amounts)
                        budget_stop = _BUDGET_BREACH
                    else:
                        budget_stop = _BUDGET_INVALID
                    trials[-1] = TrialResult(
                        candidate_id=result.candidate_id,
                        task_id=result.task_id,
                        trial_index=result.trial_index,
                        outcome="inconclusive",
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        cost_usd=observed_cost,
                        elapsed_seconds=result.elapsed_seconds,
                        simulated=result.simulated,
                        detail=(
                            f"{budget_stop}: reported {result.cost_usd!r} with "
                            f"only {remaining_budget} authorized"
                        ),
                        stop_reason=result.stop_reason
                        or ("cost_budget_breach" if over_cap else "invalid_cost"),
                        logs=result.logs,
                    )
                    continue
                spend_amounts.append(float(result.cost_usd))
                spent = math.fsum(spend_amounts)
                if spent >= budget_usd:
                    budget_stop = _BUDGET_SPENT

        outcomes.append(
            CandidateOutcome(
                candidate_id=candidate.candidate_id,
                trials=tuple(trials),
                reliability_floor=reliability_floor,
                simulated=simulated,
            )
        )

    if budget_stop:
        warnings.append(
            f"stopped after ${round(spent, 4)} of the ${budget_usd} authorized: "
            f"{budget_stop}; {budget_skip_count} trial(s) did not run, so the "
            "candidates were not all given the same chance to prove themselves"
        )

    if simulated:
        warnings.append(
            "SIMULATED RUN — these results prove the pipeline works and prove "
            "nothing about this repository"
        )

    return ExecutionReport(
        outcomes=tuple(outcomes),
        simulated=simulated,
        trials_run=run_count,
        trials_skipped=skip_count,
        budget_usd=budget_usd,
        spent_usd=spent if budget_usd is not None else None,
        trials_skipped_budget=budget_skip_count,
        budget_stop=budget_stop,
        warnings=tuple(warnings),
        declared_candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
        declared_task_ids=tuple(task.task_id for task in tasks),
        trials_per_task=trials_per_task,
        plan_digest=execution_plan.executable_digest
        if execution_plan is not None
        else "simulation",
        reliability_floor=reliability_floor,
    )


def make_simulated_runner(
    *,
    verified_rate_by_candidate: dict[str, float] | None = None,
    cost_per_trial: float | None = 0.05,
    outcome_by_trial: Mapping[tuple[str, str, int], TrialOutcome] | None = None,
    outcome_by_candidate: Mapping[str, TrialOutcome] | None = None,
    outcome_by_task: Mapping[str, TrialOutcome] | None = None,
    cost_by_candidate: Mapping[str, float | None] | None = None,
    default_verified_rate: float = 0.5,
    known_zero_infrastructure: bool = False,
) -> TrialRunner:
    """Build a deterministic simulator for exercising the pipeline without spend.

    Outcomes are derived from a hash of the identifiers, so a given candidate,
    task, and trial index always produce the same result. This makes simulated
    runs reproducible and testable while remaining transparently fake.
    """

    rates = tuple(sorted((verified_rate_by_candidate or {}).items()))
    candidate_outcomes = tuple(sorted((outcome_by_candidate or {}).items()))
    task_outcomes = tuple(sorted((outcome_by_task or {}).items()))
    outcomes = tuple(sorted((outcome_by_trial or {}).items()))
    costs = tuple(sorted((cost_by_candidate or {}).items()))
    return _DeterministicSimulator(
        rates,
        candidate_outcomes,
        task_outcomes,
        outcomes,
        costs,
        cost_per_trial,
        default_verified_rate,
        known_zero_infrastructure,
    )


__all__ = [
    "DEFAULT_RELIABILITY_FLOOR",
    "EXECUTION_SCHEMA",
    "BudgetedRunnerFactory",
    "CandidateOutcome",
    "ExecutionReport",
    "TrialOutcome",
    "TrialResult",
    "TrialRunner",
    "execute_trials",
    "make_simulated_runner",
]
