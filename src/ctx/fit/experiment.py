"""Experiment planning and the budget gate.

Before CTX Fit spends anything, it produces a plan the user can inspect: which
candidates, against which tasks, repeated how many times, and what that costs.
The plan is the artifact that makes spending a decision rather than a surprise.

Two properties matter more than convenience here.

**Nothing expensive happens by accident.** The plan is computed without calling
a model, and execution is refused unless the plan fits an explicit budget.

**An unknown cost is never treated as an affordable one.** CTX has no pricing
table of its own; without one, the dollar cost of a plan is genuinely unknown.
A budget cannot be enforced against an unknown number, so a plan with unknown
cost and a budget is *blocked*, not waved through. Under-reporting cost would
be the most damaging possible bug in a product whose objective is "cheapest".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ctx.fit.candidates import CandidateSet
from ctx.fit.profile import FitProfile

EXPERIMENT_PLAN_SCHEMA = "ctx.fit.experiment-plan-v1"

#: Reliability is a constraint, not a tie-break (ADR-014): a configuration that
#: works once has not been shown to work. Three trials is the smallest number
#: that can distinguish "always" from "usually".
DEFAULT_TRIALS_PER_TASK = 3

CostCompleteness = Literal["known", "partial", "unknown"]

PlanDecision = Literal[
    "ready",
    "blocked-no-budget",
    "blocked-unknown-cost",
    "blocked-over-budget",
    "blocked-not-evaluable",
]

DECISION_EXPLANATION: dict[PlanDecision, str] = {
    "ready": "the plan fits the budget and can be executed",
    "blocked-no-budget": ("execution needs an explicit budget; CTX Fit never spends without one"),
    "blocked-unknown-cost": (
        "the cost of this plan cannot be derived, so it cannot be checked against a "
        "budget; supply pricing or run without a budget to see the plan only"
    ),
    "blocked-over-budget": "the estimated cost exceeds the budget",
    "blocked-not-evaluable": (
        "this repository has no runnable tests, so no candidate could be verified"
    ),
}


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-token pricing for one model, supplied by the caller.

    CTX ships no price table. Prices change, and a stale hardcoded rate would
    silently produce wrong cost comparisons — the exact failure the objective
    function cannot tolerate.
    """

    model: str
    usd_per_million_input: float
    usd_per_million_output: float
    source: str = "user-supplied"

    def estimate(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.usd_per_million_input + output_tokens * self.usd_per_million_output
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """An estimated cost, with its completeness stated rather than implied."""

    completeness: CostCompleteness
    low_usd: float | None = None
    high_usd: float | None = None
    basis: str = ""

    @property
    def is_known(self) -> bool:
        return self.completeness == "known" and self.high_usd is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "completeness": self.completeness,
            "low_usd": self.low_usd,
            "high_usd": self.high_usd,
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class ExperimentPlan:
    """Exactly what CTX Fit would run, and what it would cost."""

    schema: str
    repository: str
    candidate_count: int
    task_count: int
    trials_per_task: int
    executions: int
    verification: tuple[str, ...]
    cost: CostEstimate
    budget_usd: float | None
    decision: PlanDecision
    warnings: tuple[str, ...] = ()

    @property
    def can_execute(self) -> bool:
        return self.decision == "ready"

    @property
    def explanation(self) -> str:
        return DECISION_EXPLANATION[self.decision]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "repository": self.repository,
            "candidate_count": self.candidate_count,
            "task_count": self.task_count,
            "trials_per_task": self.trials_per_task,
            "executions": self.executions,
            "verification": list(self.verification),
            "cost": self.cost.to_dict(),
            "budget_usd": self.budget_usd,
            "decision": self.decision,
            "explanation": self.explanation,
            "can_execute": self.can_execute,
            "warnings": list(self.warnings),
        }


def _estimate_cost(
    executions: int,
    price: ModelPrice | None,
    *,
    expected_input_tokens: int,
    expected_output_tokens: int,
) -> CostEstimate:
    if price is None:
        return CostEstimate(
            completeness="unknown",
            basis=(
                "no pricing supplied; CTX ships no price table because a stale rate "
                "would silently corrupt a cost comparison"
            ),
        )
    per_execution = price.estimate(expected_input_tokens, expected_output_tokens)
    total = per_execution * executions
    # A range, not a point estimate: token use varies per task and the honest
    # signal is the order of magnitude, not false precision.
    return CostEstimate(
        completeness="known",
        low_usd=round(total * 0.6, 2),
        high_usd=round(total * 1.6, 2),
        basis=(
            f"{executions} executions x ~{expected_input_tokens + expected_output_tokens} "
            f"tokens at {price.model} rates ({price.source})"
        ),
    )


def plan_experiment(
    profile: FitProfile,
    candidates: CandidateSet,
    *,
    task_count: int = 0,
    trials_per_task: int = DEFAULT_TRIALS_PER_TASK,
    budget_usd: float | None = None,
    price: ModelPrice | None = None,
    expected_input_tokens: int = 20_000,
    expected_output_tokens: int = 4_000,
) -> ExperimentPlan:
    """Produce an inspectable plan and decide whether it may be executed.

    Calls no model and spends nothing. The returned plan is only executable
    when ``decision == "ready"``.
    """

    warnings: list[str] = []
    verification = tuple(
        " ".join(command.command)
        for command in profile.verification.commands
        if command.kind in {"test", "typecheck", "lint"}
    )

    candidate_count = len(candidates.candidates)
    executions = candidate_count * task_count * trials_per_task
    cost = _estimate_cost(
        executions,
        price,
        expected_input_tokens=expected_input_tokens,
        expected_output_tokens=expected_output_tokens,
    )

    if task_count == 0:
        warnings.append(
            "no representative tasks have been derived yet, so this plan describes "
            "shape rather than a runnable experiment"
        )

    decision: PlanDecision
    if not profile.is_fit_evaluable or candidates.abstained:
        decision = "blocked-not-evaluable"
    elif budget_usd is None:
        decision = "blocked-no-budget"
    elif not cost.is_known:
        # A budget cannot be enforced against a number that does not exist.
        decision = "blocked-unknown-cost"
    elif cost.high_usd is not None and cost.high_usd > budget_usd:
        decision = "blocked-over-budget"
    else:
        decision = "ready"

    if trials_per_task < DEFAULT_TRIALS_PER_TASK:
        warnings.append(
            f"{trials_per_task} trial(s) per task is below the {DEFAULT_TRIALS_PER_TASK} "
            "needed to distinguish a configuration that always works from one that "
            "usually does"
        )

    return ExperimentPlan(
        schema=EXPERIMENT_PLAN_SCHEMA,
        repository=profile.repo_path,
        candidate_count=candidate_count,
        task_count=task_count,
        trials_per_task=trials_per_task,
        executions=executions,
        verification=verification,
        cost=cost,
        budget_usd=budget_usd,
        decision=decision,
        warnings=tuple(warnings),
    )


__all__ = [
    "DEFAULT_TRIALS_PER_TASK",
    "EXPERIMENT_PLAN_SCHEMA",
    "CostEstimate",
    "ExperimentPlan",
    "ModelPrice",
    "PlanDecision",
    "plan_experiment",
]
