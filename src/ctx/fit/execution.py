"""Trial execution with adaptive reliability stopping.

One execution is one candidate attempting one task once. Reliability requires
repeating that, because a configuration that works once has not been shown to
work (ADR-014).

Repetition multiplies cost against the very thing the product optimizes, so
trials stop **adaptively**: as soon as a candidate's remaining trials cannot
lift it to the reliability floor, the rest are abandoned. Nothing is learned by
continuing to pay for a candidate that has already lost.

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
#: repository proved it" are different facts, and an infrastructure failure is
#: not the candidate's fault.
TrialOutcome = Literal[
    "verified",
    "failed",
    "inconclusive",
    "infrastructure-failure",
    "skipped-adaptive",
]

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
        """Infrastructure failures are ours, not the candidate's."""

        return self.outcome in {"verified", "failed", "inconclusive"}

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
        """Total cost, or None if any trial's cost is unknown.

        One unknown poisons the total. A candidate must never look cheaper
        because part of its spend went unmeasured (ADR-004).
        """

        costs = [trial.cost_usd for trial in self.trials if trial.outcome != "skipped-adaptive"]
        if not costs or any(cost is None for cost in costs):
            return None
        return round(sum(cost for cost in costs if cost is not None), 4)

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
            "simulated": self.simulated,
        }


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    outcomes: tuple[CandidateOutcome, ...] = ()
    simulated: bool = False
    trials_run: int = 0
    trials_skipped: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "simulated": self.simulated,
            "trials_run": self.trials_run,
            "trials_skipped": self.trials_skipped,
            "warnings": list(self.warnings),
        }


#: A runner performs one trial. Injected so the same execution logic drives a
#: real provider, a simulator, or a test double without branching.
TrialRunner = Callable[[CandidateConfiguration, FitTask, int], TrialResult]


def _can_still_qualify(verified: int, completed: int, remaining: int, floor: float) -> bool:
    """Could this candidate still reach the floor if every remaining trial passed?"""

    best_possible_total = completed + remaining
    if best_possible_total == 0:
        return True
    return (verified + remaining) / best_possible_total >= floor


def execute_trials(
    candidates: tuple[CandidateConfiguration, ...],
    tasks: tuple[FitTask, ...],
    runner: TrialRunner,
    *,
    trials_per_task: int = 3,
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR,
    simulated: bool = False,
) -> ExecutionReport:
    """Run every candidate against every task, stopping candidates that cannot win.

    Adaptive stopping is a cost control, not a shortcut: a candidate is only
    abandoned once arithmetic shows it can no longer reach the reliability
    floor, so no candidate that could have qualified is ever cut short.
    """

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

    for candidate in candidates:
        trials: list[TrialResult] = []
        verified = 0
        completed = 0
        abandoned = False
        total = len(usable) * trials_per_task

        for task in usable:
            for index in range(trials_per_task):
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
                        TrialResult(
                            candidate_id=candidate.candidate_id,
                            task_id=task.task_id,
                            trial_index=index,
                            outcome="skipped-adaptive",
                            simulated=simulated,
                            detail="cannot reach the reliability floor; further spend would buy nothing",
                        )
                    )
                    skip_count += 1
                    continue

                result = runner(candidate, task, index)
                trials.append(result)
                run_count += 1
                if result.counts_toward_reliability:
                    completed += 1
                    if result.outcome == "verified":
                        verified += 1

        outcomes.append(
            CandidateOutcome(
                candidate_id=candidate.candidate_id,
                trials=tuple(trials),
                reliability_floor=reliability_floor,
                simulated=simulated,
            )
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
    "CandidateOutcome",
    "ExecutionReport",
    "TrialOutcome",
    "TrialResult",
    "TrialRunner",
    "execute_trials",
    "make_simulated_runner",
]
