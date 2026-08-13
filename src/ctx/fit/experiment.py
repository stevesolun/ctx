"""The experiment: resolving it, planning it, and running it.

Before CTX Fit spends anything, it produces a plan the user can inspect: which
candidates, against which tasks, repeated how many times, and what that costs.
The plan is the artifact that makes spending a decision rather than a surprise.

Three properties matter more than convenience here.

**Nothing expensive happens by accident.** The plan is computed without calling
a model, and execution is refused unless the plan fits an explicit budget.

**An unknown cost is never treated as an affordable one.** CTX has no pricing
table of its own; without one, the dollar cost of a plan is genuinely unknown.
A budget cannot be enforced against an unknown number, so a plan with unknown
cost and a budget is *blocked*, not waved through. Under-reporting cost would
be the most damaging possible bug in a product whose objective is "cheapest".

**The plan and the campaign are the same experiment.** They used to be two
independent derivations, one in the pre-flight path and one in the spending
path, agreeing only by convention: each opened the catalog, each computed the
verify command, each derived tasks, each chose a trial count and a model. Since
``--budget`` is the single gate between a user and real spend, any drift
between them silently approved one experiment and ran another.
:func:`resolve_experiment` derives it once; :func:`run_experiment` executes what
was resolved. The plan is a view of that object rather than a parallel account
of it, so there is nothing left to drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ctx.fit.candidates import CandidateSet
from ctx.fit.profile import FitProfile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ctx.fit.candidates import CandidateConfiguration
    from ctx.fit.execution import ExecutionReport
    from ctx.fit.live_runner import CampaignEnvironment
    from ctx.fit.recommend import Recommendation
    from ctx.fit.tasks import FitTask, TaskSet

EXPERIMENT_PLAN_SCHEMA = "ctx.fit.experiment-plan-v1"

#: Reliability is a constraint, not a tie-break (ADR-014): a configuration that
#: works once has not been shown to work. Three trials is the smallest number
#: that can distinguish "always" from "usually".
DEFAULT_TRIALS_PER_TASK = 3

#: The model every arm of an experiment runs. One global choice, applied to the
#: control and the treatments alike: a baseline on a different model turns every
#: reported difference into a mixture of capability effect and model effect.
DEFAULT_MODEL = "gpt-4o-mini"

#: How many representative tasks one experiment uses. Each task multiplies the
#: campaign by ``candidates x trials_per_task`` executions, so this number is a
#: cost decision as much as an evidence one.
DEFAULT_TASK_LIMIT = 3

#: What to verify with when the repository declares no test command of its own.
FALLBACK_VERIFY_COMMAND = ("python", "-m", "pytest", "-q")

#: What a single exchange with the agent is assumed to cost. One execution is a
#: whole ``ctx run`` loop rather than one exchange, so the caller scales these:
#: pricing an execution as a single exchange let the budget gate approve a plan
#: costing an order of magnitude more than the number the user was shown.
EXCHANGE_INPUT_TOKENS = 20_000
EXCHANGE_OUTPUT_TOKENS = 4_000

_CATALOG_UNAVAILABLE = "the capability catalog could not be opened"

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
    tasks: tuple[FitTask, ...],
    *,
    trials_per_task: int = DEFAULT_TRIALS_PER_TASK,
    budget_usd: float | None = None,
    price: ModelPrice | None = None,
    expected_input_tokens: int = 20_000,
    expected_output_tokens: int = 4_000,
) -> ExperimentPlan:
    """Produce an inspectable plan and decide whether it may be executed.

    Calls no model and spends nothing. The returned plan is only executable
    when ``decision == "ready"``.

    ``tasks`` is the tasks themselves, not a count of them. A count is a number
    severed from the thing it counts: it let the pre-flight gate be priced for
    one set of tasks while the campaign ran another, which is the one drift a
    spend gate cannot tolerate.
    """

    warnings: list[str] = []
    verification = tuple(
        " ".join(command.command)
        for command in profile.verification.commands
        if command.kind in {"test", "typecheck", "lint"}
    )

    task_count = len(tasks)
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


@dataclass(frozen=True, slots=True)
class ResolvedExperiment:
    """One experiment, derived once, read by both the plan and the campaign.

    Every field here was previously derived twice — once to show the user a
    price and once to spend against it. The two derivations could disagree
    about the candidates, the verify command, the trial count or the model, and
    nothing would have said so.
    """

    profile: FitProfile
    candidates: CandidateSet
    tasks: TaskSet
    verify_command: tuple[str, ...]
    model: str
    trials_per_task: int
    plan: ExperimentPlan

    @property
    def can_execute(self) -> bool:
        return self.plan.can_execute

    @property
    def budget_usd(self) -> float | None:
        """The authorization the plan was checked against.

        Read from the plan rather than kept alongside it, so the number the
        gate compared against and the number the campaign spends under cannot
        be two different numbers.
        """

        return self.plan.budget_usd


@dataclass(frozen=True, slots=True)
class ExperimentOutcome:
    """What one campaign produced, and the regime it produced it under."""

    experiment: ResolvedExperiment
    report: ExecutionReport
    recommendation: Recommendation
    simulated: bool

    @property
    def candidates(self) -> tuple[CandidateConfiguration, ...]:
        return self.experiment.candidates.candidates


def resolve_experiment(
    profile: FitProfile,
    *,
    budget_usd: float | None = None,
    model: str = DEFAULT_MODEL,
    trials_per_task: int = DEFAULT_TRIALS_PER_TASK,
    task_limit: int = DEFAULT_TASK_LIMIT,
) -> ResolvedExperiment:
    """Derive the whole experiment, and plan it. Executes nothing (ADR-013).

    Reads the shipped capability catalog and this repository's Git history. No
    model is called and no money can be spent here, so a user who only wants to
    see the plan pays nothing for it. Task derivation is the slow half, and it
    happens exactly once — a ``--test`` run used to pay for it twice.
    """

    from ctx.engine.planner import BoundedCapabilityPlanner
    from ctx.fit.candidates import generate_candidates
    from ctx.fit.providers import DEFAULT_MAX_ITERATIONS
    from ctx.fit.release_catalog import open_release_candidate_source
    from ctx.fit.tasks import derive_tasks

    source = open_release_candidate_source()
    if source is None:
        candidates = CandidateSet(abstained=True, abstention_reason=_CATALOG_UNAVAILABLE)
    else:
        # The candidates carry the model the plan is priced against, or the
        # estimate quotes one model while the trials silently run another.
        candidates = generate_candidates(
            profile, BoundedCapabilityPlanner(source=source), model=model
        )

    test_command = profile.verification.best("test")
    verify_command = test_command.command if test_command else FALLBACK_VERIFY_COMMAND
    tasks = derive_tasks(profile.repo_path, verify_command=verify_command, limit=task_limit)

    plan = plan_experiment(
        profile,
        candidates,
        tasks.tasks,
        trials_per_task=trials_per_task,
        budget_usd=budget_usd,
        price=ModelPrice.from_litellm(model),
        # The harness is launched with an iteration bound and every iteration
        # resends the accumulated context, so the plan is priced for the loop
        # the trial will actually run rather than for its first exchange. A
        # spend gate that under-promises is worse than one that over-promises.
        expected_input_tokens=EXCHANGE_INPUT_TOKENS * DEFAULT_MAX_ITERATIONS,
        expected_output_tokens=EXCHANGE_OUTPUT_TOKENS * DEFAULT_MAX_ITERATIONS,
    )

    return ResolvedExperiment(
        profile=profile,
        candidates=candidates,
        tasks=tasks,
        verify_command=verify_command,
        model=model,
        trials_per_task=trials_per_task,
        plan=plan,
    )


def run_experiment(experiment: ResolvedExperiment, *, live: bool) -> ExperimentOutcome:
    """Run the experiment that was resolved, under the budget it was planned against.

    ``live`` chooses the runner and nothing else. The candidates, the tasks,
    the trial count and the authorization all come from ``experiment``, so a
    campaign cannot quietly differ from the plan the user approved.

    Raises :class:`~ctx.fit.providers.ProviderUnavailable` when a live run is
    asked for and no agent can be driven — before any workspace exists, so the
    caller can still say truthfully that nothing was run and nothing was spent.

    The gate lives in the caller. ``cmd_fit`` refuses unless
    ``experiment.can_execute``, so no production path reaches a live campaign
    without an authorization. That makes the refusal a caller obligation on a
    public name that spends, which is a trap for anyone calling this directly;
    moving it in here is tracked as a follow-up rather than done now, because
    the obvious guard makes the "lost its budget mid-campaign" branch below
    unreachable and would retire the test that pins it.
    """

    from dataclasses import replace

    from ctx.fit.execution import (
        BudgetedRunnerFactory,
        TrialRunner,
        execute_trials,
        make_simulated_runner,
    )
    from ctx.fit.recommend import recommend

    simulated = not live
    repo_path = experiment.profile.repo_path
    budget_usd = experiment.budget_usd
    environment: CampaignEnvironment | None = None

    # ``derive_tasks`` proposes tasks; it does not validate them, and
    # ``execute_trials`` admits only tasks proven to start red. Marking them
    # here is honest under both regimes, for different reasons: a live trial
    # re-proves redness itself, per trial, inside its own isolated workspace,
    # and a simulated run executes nothing at all, so it claims nothing.
    tasks = tuple(replace(task, starts_red=True) for task in experiment.tasks.tasks)

    runner_for_budget: BudgetedRunnerFactory | None = None
    if simulated:
        runner = make_simulated_runner()
    else:
        from ctx.fit.live_runner import CampaignEnvironment, make_live_runner
        from ctx.fit.providers import build_agent_driver

        # One environment for the whole campaign, so the first trial pays for
        # the dependency set and the rest are re-pointed at their own workspace
        # (FITBUG-016). It is created here rather than inside the runner because
        # ``capped_runner`` below builds a *new* runner for every trial; a
        # runner-owned environment would therefore be a trial-owned one, and
        # every trial would pay a full install. Constructing it spends nothing
        # and touches no disk (ADR-013) — the first ``aim_at``, inside a trial
        # the budget gate has already cleared, is what builds it.
        environment = CampaignEnvironment()

        # This driver is built for its exception, not for the runner it returns.
        # ``build_agent_driver`` raises ProviderUnavailable for an unusable
        # harness, and raising it here — before a single workspace exists — is
        # what lets the caller say nothing was run and nothing was spent.
        runner = make_live_runner(repo_path, build_agent_driver(), environment=environment)

        def capped_runner(remaining_usd: float) -> TrialRunner:
            """Hand this trial only the dollars the authorization has left.

            The provider's own per-trial ceiling is a module constant that bears
            no relation to what the user approved, so a campaign of N trials
            would authorize N times that constant.
            """

            return make_live_runner(
                repo_path,
                build_agent_driver(per_trial_budget_usd=remaining_usd),
                environment=environment,
            )

        if budget_usd is not None:
            # Each cap is the remaining authorization, so without one there is
            # nothing to derive a cap from; ``execute_trials`` refuses the pair.
            runner_for_budget = capped_runner

    try:
        report = execute_trials(
            experiment.candidates.candidates,
            tasks,
            runner,
            trials_per_task=experiment.trials_per_task,
            simulated=simulated,
            # The authorization is over real money. A simulated trial's cost is
            # an invention of the simulator, so charging it against the user's
            # budget would truncate the pipeline demonstration over dollars
            # nobody was ever going to be billed.
            budget_usd=None if simulated else budget_usd,
            runner_for_budget=runner_for_budget,
        )
    finally:
        # The campaign owns the environment's lifetime: a whole dependency set
        # on disk must not outlive the run that needed it, including when the
        # run raises.
        if environment is not None:
            environment.close()

    # Tasks that never produced a scored trial -- an infrastructure failure, or
    # one abandoned by adaptive stopping -- are not evidence. Counting them
    # overstates the evidence base in the very section that exists to say how
    # thin it is.
    evaluated_tasks = {
        trial.task_id
        for outcome in report.outcomes
        for trial in outcome.trials
        if trial.counts_toward_reliability
    }
    recommendation = recommend(
        report,
        experiment.candidates.candidates,
        task_count=len(evaluated_tasks),
        trials_per_task=experiment.trials_per_task,
    )

    return ExperimentOutcome(
        experiment=experiment,
        report=report,
        recommendation=recommendation,
        simulated=simulated,
    )


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_TASK_LIMIT",
    "DEFAULT_TRIALS_PER_TASK",
    "EXCHANGE_INPUT_TOKENS",
    "EXCHANGE_OUTPUT_TOKENS",
    "EXPERIMENT_PLAN_SCHEMA",
    "FALLBACK_VERIFY_COMMAND",
    "CostEstimate",
    "ExperimentOutcome",
    "ExperimentPlan",
    "ModelPrice",
    "PlanDecision",
    "ResolvedExperiment",
    "plan_experiment",
    "resolve_experiment",
    "run_experiment",
]
