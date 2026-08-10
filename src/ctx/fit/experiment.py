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

import math
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
    "blocked-invalid-budget",
    "blocked-unknown-cost",
    "blocked-over-budget",
    "blocked-not-evaluable",
    "blocked-no-candidates",
    "blocked-no-tasks",
]

DECISION_EXPLANATION: dict[PlanDecision, str] = {
    "ready": "the plan fits the budget and can be executed",
    "blocked-no-budget": ("execution needs an explicit budget; CTX Fit never spends without one"),
    "blocked-invalid-budget": (
        "the budget is not a finite number of dollars, so nothing can be checked "
        "against it; supply a real amount such as --budget 5"
    ),
    "blocked-unknown-cost": (
        "the cost of this plan cannot be derived, so it cannot be checked against a "
        "budget; supply pricing or run without a budget to see the plan only"
    ),
    "blocked-over-budget": "the estimated cost exceeds the budget",
    "blocked-not-evaluable": (
        "this repository has no runnable tests, so no candidate could be verified"
    ),
    "blocked-no-candidates": (
        "no candidate configuration could be proposed, so there is nothing to compare"
    ),
    "blocked-no-tasks": (
        "no representative task could be derived, so an experiment would run zero "
        "executions and produce no evidence"
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

    @classmethod
    def from_litellm(cls, model: str) -> ModelPrice | None:
        """Read rates from LiteLLM's own table, or return None if unavailable.

        LiteLLM is already the source of truth for *actual* cost at runtime, so
        using its table for the pre-flight estimate keeps both halves of the
        cost story consistent and avoids CTX shipping a rate that goes stale.
        Returning None rather than a guess keeps the budget gate fail-closed.
        """

        try:
            import litellm
        except ImportError:
            return None
        table = getattr(litellm, "model_cost", None)
        if not isinstance(table, dict):
            return None
        entry = table.get(model)
        if not isinstance(entry, dict):
            return None
        per_input = entry.get("input_cost_per_token")
        per_output = entry.get("output_cost_per_token")
        if not isinstance(per_input, int | float) or not isinstance(per_output, int | float):
            return None
        return cls(
            model=model,
            usd_per_million_input=float(per_input) * 1_000_000,
            usd_per_million_output=float(per_output) * 1_000_000,
            source="litellm model_cost",
        )


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
    cause: str = ""
    """The specific cause behind ``decision``, when one was actually observed.

    A decision is a category; the cause is what happened. Where the two differ
    the user needs the cause, because that is the thing they can act on — being
    told to add tests to a repository that has them sends them to fix something
    that is not broken (FITBUG-014).
    """

    @property
    def can_execute(self) -> bool:
        return self.decision == "ready"

    @property
    def explanation(self) -> str:
        return self.cause or DECISION_EXPLANATION[self.decision]

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
    cause = ""
    if not profile.is_fit_evaluable:
        decision = "blocked-not-evaluable"
    elif candidates.abstained:
        # Abstention has several causes — an unopenable catalog, nothing in it
        # relevant to this repository, nothing a trial could actually apply —
        # and none of them is "this repository has no tests". Collapsing them
        # into blocked-not-evaluable made the output contradict itself three
        # lines apart, so the observed reason is carried through instead.
        decision = "blocked-no-candidates"
        cause = candidates.abstention_reason or ""
    elif task_count <= 0:
        # Zero tasks means zero executions. Such a plan trivially "fits" any
        # budget while proving nothing, so it must never be reported runnable.
        decision = "blocked-no-tasks"
    elif budget_usd is None:
        decision = "blocked-no-budget"
    elif not math.isfinite(budget_usd):
        # Every comparison against NaN is False, so an unchecked NaN budget
        # walks straight past the over-budget test into "ready": the one gate
        # before real spend, failing open. Infinity is refused with it — a
        # budget that no cost can exceed is not a bound on spending, and this
        # module's whole promise is that an unenforceable number is blocked
        # rather than waved through.
        decision = "blocked-invalid-budget"
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
        cause=cause,
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
