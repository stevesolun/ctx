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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.tasks import FitTask

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

#: Outcomes for trials CTX itself could not carry out. They are excluded from
#: reliability because the candidate never got its turn, and for the same
#: reason an unmeasured cost on one of them is not the candidate's unmeasured
#: spend: every path that produces one gives up before an agent is paid.
_OUR_FAILURE_OUTCOMES = frozenset({"infrastructure-failure"})

#: Fraction of trials that must verify for a candidate to qualify. A candidate
#: below this is excluded before cost is considered at all.
DEFAULT_RELIABILITY_FLOOR = 1.0


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
        because part of its spend went unmeasured (ADR-004). Two kinds of trial
        are exempt, because neither is unmeasured candidate spend. Trials that
        never ran are absent spend, not unmeasured spend. And an infrastructure
        failure is ours: every path that returns one abandons the trial before
        an agent is paid, so charging the candidate an "unknown" for it made a
        single unusable task -- a root commit that cannot be reverted, a task
        that would not start red -- disqualify every candidate in the campaign
        for incomplete cost (FITBUG-012). Whatever such a trial *does* report is
        still added, so a failure that did spend money is never hidden.
        """

        total = 0.0
        measured = False
        for trial in self.trials:
            if trial.outcome in _UNRUN_OUTCOMES:
                continue
            if trial.cost_usd is None:
                if trial.outcome in _OUR_FAILURE_OUTCOMES:
                    continue
                return None
            total += trial.cost_usd
            measured = True
        return round(total, 4) if measured else None

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
) -> ExecutionReport:
    """Run every candidate against every task, stopping candidates that cannot win.

    Adaptive stopping is a cost control, not a shortcut: a candidate is only
    abandoned once arithmetic shows it can no longer reach the reliability
    floor, so no candidate that could have qualified is ever cut short.

    ``budget_usd`` is the user's authorization and bounds the campaign as a
    whole. Trials are charged against it as they complete and everything still
    queued is abandoned once the total reaches it, so the campaign cannot bill
    the user for an experiment they did not approve. A trial that reports no
    cost stops the campaign too: unmeasured spend cannot be counted against the
    authorization, and continuing would be spending blind.

    ``runner_for_budget`` supplies the trial runner for a given remaining
    balance. Without it the campaign can still stop on the total, but nothing
    stops one runaway trial from overshooting it on its own.
    """

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
                    trial_runner = runner_for_budget(round(budget_usd - spent, 6))

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
                    if result.outcome in _OUR_FAILURE_OUTCOMES:
                        # Our failure, raised before an agent could be paid.
                        # There is no spend to lose track of, so halting the
                        # whole campaign over it would abandon trials the user
                        # authorized and paid nothing for.
                        continue
                    budget_stop = _BUDGET_UNTRACKABLE
                    continue
                spent = round(spent + result.cost_usd, 6)
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
        spent_usd=round(spent, 4) if budget_usd is not None else None,
        trials_skipped_budget=budget_skip_count,
        budget_stop=budget_stop,
        warnings=tuple(warnings),
    )


def make_simulated_runner(
    *,
    verified_rate_by_candidate: dict[str, float] | None = None,
    cost_per_trial: float | None = 0.05,
) -> TrialRunner:
    """Build a deterministic simulator for exercising the pipeline without spend.

    Outcomes are derived from a hash of the identifiers, so a given candidate,
    task, and trial index always produce the same result. This makes simulated
    runs reproducible and testable while remaining transparently fake.
    """

    rates = verified_rate_by_candidate or {}

    def run(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        seed = f"{candidate.candidate_id}:{task.task_id}:{index}"
        draw = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
        rate = rates.get(candidate.candidate_id, 0.5)
        outcome: TrialOutcome = "verified" if draw < rate else "failed"
        return TrialResult(
            candidate_id=candidate.candidate_id,
            task_id=task.task_id,
            trial_index=index,
            outcome=outcome,
            input_tokens=20_000,
            output_tokens=4_000,
            cost_usd=cost_per_trial,
            elapsed_seconds=30.0,
            simulated=True,
            detail="simulated trial",
        )

    return run


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
