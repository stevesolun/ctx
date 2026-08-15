"""The experiment: resolving it, planning it, and running it.

Before CTX Fit spends anything, it produces a plan the user can inspect: which
candidates, against which tasks, repeated how many times, and what that costs.
The plan is the artifact that makes spending a decision rather than a surprise.

Three properties matter more than convenience here.

**Nothing expensive happens by accident.** The plan is computed without calling
a model, and execution is refused unless the plan fits an explicit budget.

**An unknown cost is never treated as an affordable one.** CTX ships an exact,
release-verified rate for its default model and resolves custom-model rates
only from an available provider table. Without either, the dollar cost of a
plan is genuinely unknown. A budget cannot be enforced against an unknown
number, so a plan with unknown cost and a budget is *blocked*, not waved
through. Under-reporting cost would be the most damaging possible bug in a
product whose objective is "cheapest".

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

import hashlib
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeGuard

from ctx.fit.candidates import CandidateSet
from ctx.fit.execution import DEFAULT_RELIABILITY_FLOOR
from ctx.fit.profile import FitProfile
from ctx.fit.verification import (
    VERIFICATION_ENVIRONMENT_ASSUMPTION,
    VERIFIER_TRUST_ASSUMPTION,
)

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

#: The provider for CTX Fit's unprefixed default model. Keep this beside the
#: model selection itself: the base product must be able to select the matching
#: credential without importing the optional live-harness dependency.
DEFAULT_MODEL_PROVIDER = "openai"

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


def _finite_number(value: object) -> TypeGuard[int | float]:
    """Whether ``value`` is a finite real number representable as a float."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


CostCompleteness = Literal["known", "partial", "unknown"]

PlanDecision = Literal[
    "ready",
    "blocked-no-budget",
    "blocked-invalid-budget",
    "blocked-unknown-cost",
    "blocked-over-budget",
    "blocked-invalid-plan",
    "blocked-not-evaluable",
    "blocked-no-candidates",
    "blocked-no-comparison",
    "blocked-no-tasks",
]

DECISION_EXPLANATION: dict[PlanDecision, str] = {
    "ready": "the plan fits the budget and can be authorized",
    "blocked-no-budget": ("execution needs an explicit budget; CTX Fit never spends without one"),
    "blocked-invalid-budget": (
        "the budget is not a finite, non-negative number of dollars, so nothing "
        "can be checked against it; supply a real amount such as --budget 5"
    ),
    "blocked-unknown-cost": (
        "the cost of this plan cannot be derived, so it cannot be checked against a "
        "budget; supply pricing or run without a budget to see the plan only"
    ),
    "blocked-over-budget": "the estimated cost exceeds the budget",
    "blocked-invalid-plan": "the plan must contain at least one trial per task",
    "blocked-not-evaluable": (
        "this repository has no runnable tests, so no candidate could be verified"
    ),
    "blocked-no-candidates": (
        "no candidate configuration could be proposed, so there is nothing to compare"
    ),
    "blocked-no-comparison": (
        "the field does not contain one baseline and at least one challenger, so paid "
        "execution could not support a comparison"
    ),
    "blocked-no-tasks": (
        "no representative task could be derived, so an experiment would run zero "
        "executions and produce no evidence"
    ),
}


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Per-token pricing for one model, supplied by the caller.

    CTX ships one release-verified rate for its own default model. Other rates
    come from the optional runtime or the caller; unresolved cost stays unknown
    rather than silently using a stale or guessed number.
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
        """Resolve an exact rate, or return None if one is unavailable.

        The base CTX Fit install owns its default model, so it also ships that
        model's release-verified provider rate. Custom models use LiteLLM's
        runtime table when the optional harness dependency is installed.
        Returning ``None`` for every other unresolved model keeps the budget
        gate fail-closed rather than inventing a rate.
        """

        if model == DEFAULT_MODEL:
            return cls(
                model=model,
                usd_per_million_input=0.15,
                usd_per_million_output=0.60,
                source=(
                    "OpenAI API model pricing "
                    "(developers.openai.com/api/docs/models/gpt-4o-mini; "
                    "verified 2026-08-14)"
                ),
            )

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
        if not _finite_number(per_input) or not _finite_number(per_output):
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
        low = self.low_usd
        high = self.high_usd
        return (
            self.completeness == "known"
            and _finite_number(low)
            and _finite_number(high)
            and 0 <= low <= high
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "completeness": self.completeness,
            "low_usd": self.low_usd,
            "high_usd": self.high_usd,
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class PlannedCandidate:
    """The candidate identity and configuration the preview authorizes."""

    candidate_id: str
    role: str
    capability_ids: tuple[str, ...]
    model: str | None
    instructions: tuple[str, ...]
    configuration_hash: str

    def matches(self, candidate: CandidateConfiguration) -> bool:
        return (
            self.candidate_id == candidate.candidate_id
            and self.role == candidate.role
            and self.capability_ids == candidate.capability_ids
            and self.model == candidate.model
            and self.instructions == candidate.instructions
            and self.configuration_hash == candidate.configuration_hash
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "role": self.role,
            "capability_ids": list(self.capability_ids),
            "model": self.model,
            "instructions": list(self.instructions),
            "configuration_hash": self.configuration_hash,
        }


@dataclass(frozen=True, slots=True)
class PlannedTask:
    """The representative task the preview authorizes."""

    task_id: str
    title: str
    provenance: str
    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    verify_command: tuple[str, ...]
    definition_hash: str

    def matches(self, task: FitTask) -> bool:
        return (
            self.task_id == task.task_id
            and self.title == task.title
            and self.provenance == task.provenance
            and self.source_paths == task.source_paths
            and self.test_paths == task.test_paths
            and self.verify_command == task.verify_command
            and self.definition_hash == _task_definition_hash(task)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "provenance": self.provenance,
            "source_paths": list(self.source_paths),
            "test_paths": list(self.test_paths),
            "verify_command": list(self.verify_command),
            "definition_hash": self.definition_hash,
        }


def _task_definition_hash(task: FitTask) -> str:
    """Stable identity of the task definition the user reviewed.

    ``starts_red`` is deliberately absent: it changes from unknown in the
    read-only preview to proven inside each isolated trial. Every instruction,
    editable path, protected test path and verification command is bound.
    """

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
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR
    warnings: tuple[str, ...] = ()
    cause: str = ""
    """The specific cause behind ``decision``, when one was actually observed.

    A decision is a category; the cause is what happened. Where the two differ
    the user needs the cause, because that is the thing they can act on — being
    told to add tests to a repository that has them sends them to fix something
    that is not broken (FITBUG-014).
    """
    candidates: tuple[PlannedCandidate, ...] = ()
    tasks: tuple[PlannedTask, ...] = ()
    authorized: bool = False
    authorization_digest: str = ""

    @property
    def executable_digest(self) -> str:
        """Canonical identity of every field an authorization permits."""

        payload = json.dumps(
            {
                "schema": self.schema,
                "repository": self.repository,
                "candidate_count": self.candidate_count,
                "task_count": self.task_count,
                "candidates": [candidate.to_dict() for candidate in self.candidates],
                "tasks": [task.to_dict() for task in self.tasks],
                "trials_per_task": self.trials_per_task,
                "executions": self.executions,
                "verification": list(self.verification),
                "cost": self.cost.to_dict(),
                "budget_usd": self.budget_usd,
                "decision": self.decision,
                "reliability_floor": self.reliability_floor,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def can_authorize(self) -> bool:
        budget = self.budget_usd
        candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
        configuration_hashes = tuple(candidate.configuration_hash for candidate in self.candidates)
        task_ids = tuple(task.task_id for task in self.tasks)
        task_hashes = tuple(task.definition_hash for task in self.tasks)
        return (
            self.decision == "ready"
            and _finite_number(budget)
            and budget >= 0
            and self.cost.is_known
            and self.cost.high_usd is not None
            and self.cost.high_usd <= budget
            and type(self.candidate_count) is int
            and self.candidate_count == len(self.candidates)
            and self.candidate_count >= 2
            and len(set(candidate_ids)) == len(candidate_ids)
            and all(isinstance(item, str) and item for item in candidate_ids)
            and len(set(configuration_hashes)) == len(configuration_hashes)
            and all(isinstance(item, str) and item for item in configuration_hashes)
            and sum(
                candidate.role == "baseline" and candidate.candidate_id == "baseline"
                for candidate in self.candidates
            )
            == 1
            and all(
                (candidate.role == "baseline") == (candidate.candidate_id == "baseline")
                for candidate in self.candidates
            )
            and type(self.task_count) is int
            and self.task_count == len(self.tasks)
            and self.task_count > 0
            and len(set(task_ids)) == len(task_ids)
            and all(isinstance(item, str) and item for item in task_ids)
            and len(set(task_hashes)) == len(task_hashes)
            and all(isinstance(item, str) and item for item in task_hashes)
            and type(self.trials_per_task) is int
            and self.trials_per_task > 0
            and _finite_number(self.reliability_floor)
            and 0 <= self.reliability_floor <= 1
            and type(self.executions) is int
            and self.executions == self.candidate_count * self.task_count * self.trials_per_task
            and self.executions > 0
        )

    @property
    def can_execute(self) -> bool:
        """Whether this exact plan is both bounded and explicitly authorized."""

        return (
            self.can_authorize
            and self.authorized
            and self.authorization_digest == self.executable_digest
        )

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(candidate.candidate_id for candidate in self.candidates)

    @property
    def explanation(self) -> str:
        return self.cause or DECISION_EXPLANATION[self.decision]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "repository": self.repository,
            "candidate_count": self.candidate_count,
            "task_count": self.task_count,
            "candidate_ids": list(self.candidate_ids),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "tasks": [task.to_dict() for task in self.tasks],
            "trials_per_task": self.trials_per_task,
            "executions": self.executions,
            "verification": list(self.verification),
            "cost": self.cost.to_dict(),
            "budget_usd": self.budget_usd,
            "decision": self.decision,
            "reliability_floor": self.reliability_floor,
            "explanation": self.explanation,
            "can_authorize": self.can_authorize,
            "can_execute": self.can_execute,
            "authorized": self.authorized,
            "executable_digest": self.executable_digest,
            "authorization_digest": self.authorization_digest,
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
                "no exact pricing resolved; CTX ships only its release-verified "
                "default-model rate and will not guess a custom model's cost"
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
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR,
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
    warnings.append(VERIFIER_TRUST_ASSUMPTION)
    warnings.append(VERIFICATION_ENVIRONMENT_ASSUMPTION)
    verification = tuple(
        dict.fromkeys(" ".join(task.verify_command) for task in tasks if task.verify_command)
    )
    if not verification and (declared_test := profile.verification.best("test")) is not None:
        verification = (" ".join(declared_test.command),)

    task_count = len(tasks)
    candidate_count = len(candidates.candidates)
    valid_trial_count = type(trials_per_task) is int and trials_per_task > 0
    executions = candidate_count * task_count * trials_per_task if valid_trial_count else 0
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
    elif (
        candidate_count < 2
        or len({candidate.candidate_id for candidate in candidates.candidates}) != candidate_count
        or len({candidate.configuration_hash for candidate in candidates.candidates})
        != candidate_count
        or sum(
            candidate.role == "baseline" and candidate.candidate_id == "baseline"
            for candidate in candidates.candidates
        )
        != 1
        or any(
            (candidate.role == "baseline") != (candidate.candidate_id == "baseline")
            for candidate in candidates.candidates
        )
    ):
        decision = "blocked-no-comparison"
    elif task_count <= 0:
        # Zero tasks means zero executions. Such a plan trivially "fits" any
        # budget while proving nothing, so it must never be reported runnable.
        decision = "blocked-no-tasks"
    elif (
        not valid_trial_count
        or not _finite_number(reliability_floor)
        or not 0 <= reliability_floor <= 1
    ):
        decision = "blocked-invalid-plan"
    elif budget_usd is None:
        decision = "blocked-no-budget"
    elif not _finite_number(budget_usd) or budget_usd < 0:
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

    if type(trials_per_task) is int and trials_per_task < DEFAULT_TRIALS_PER_TASK:
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
        candidates=tuple(
            PlannedCandidate(
                candidate_id=candidate.candidate_id,
                role=candidate.role,
                capability_ids=candidate.capability_ids,
                model=candidate.model,
                instructions=candidate.instructions,
                configuration_hash=candidate.configuration_hash,
            )
            for candidate in candidates.candidates
        ),
        tasks=tuple(
            PlannedTask(
                task_id=task.task_id,
                title=task.title,
                provenance=task.provenance,
                source_paths=task.source_paths,
                test_paths=task.test_paths,
                verify_command=task.verify_command,
                definition_hash=_task_definition_hash(task),
            )
            for task in tasks
        ),
        trials_per_task=trials_per_task,
        executions=executions,
        verification=verification,
        cost=cost,
        budget_usd=budget_usd,
        decision=decision,
        reliability_floor=reliability_floor,
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
    reliability_floor: float
    plan: ExperimentPlan

    @property
    def can_execute(self) -> bool:
        return self.plan.can_execute and self.plan_matches

    @property
    def can_authorize(self) -> bool:
        return self.plan.can_authorize and self.plan_matches

    @property
    def plan_matches(self) -> bool:
        """Whether the preview still describes the exact resolved campaign."""

        candidates_match = len(self.plan.candidates) == len(self.candidates.candidates) and all(
            planned.matches(candidate)
            for planned, candidate in zip(
                self.plan.candidates, self.candidates.candidates, strict=True
            )
        )
        tasks_match = len(self.plan.tasks) == len(self.tasks.tasks) and all(
            planned.matches(task)
            for planned, task in zip(self.plan.tasks, self.tasks.tasks, strict=True)
        )
        return (
            self.plan.repository == self.profile.repo_path
            and candidates_match
            and tasks_match
            and self.plan.trials_per_task == self.trials_per_task
            and self.plan.reliability_floor == self.reliability_floor
            and all(candidate.model == self.model for candidate in self.candidates.candidates)
            and self.plan.executions
            == len(self.candidates.candidates) * len(self.tasks.tasks) * self.trials_per_task
        )

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


def _current_baseline_error(experiment: ResolvedExperiment) -> str:
    from ctx.fit.candidates import current_baseline_error
    from ctx.fit.profile import build_fit_profile

    baseline = experiment.candidates.baseline
    if baseline is None:
        return "the experiment has no baseline to revalidate"
    try:
        current_profile = build_fit_profile(Path(experiment.profile.repo_path))
    except (OSError, ValueError) as exc:
        return f"the repository could not be re-profiled: {exc}"
    return current_baseline_error(current_profile, baseline)


def resolve_experiment(
    profile: FitProfile,
    *,
    budget_usd: float | None = None,
    model: str | None = None,
    trials_per_task: int = DEFAULT_TRIALS_PER_TASK,
    reliability_floor: float = DEFAULT_RELIABILITY_FLOOR,
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

    effective_model = model or DEFAULT_MODEL
    try:
        from ctx.fit.applied_configuration import load_applied_configuration

        current = load_applied_configuration(Path(profile.repo_path))
        if current is not None:
            effective_model = current.model
    except (ValueError, OSError):
        # Candidate generation turns an invalid sidecar into a precise
        # abstention. Resolution still has to remain read-only and produce the
        # same blocked plan rather than failing before that explanation exists.
        pass

    source = open_release_candidate_source()
    if source is None:
        candidates = CandidateSet(abstained=True, abstention_reason=_CATALOG_UNAVAILABLE)
    else:
        # The candidates carry the model the plan is priced against, or the
        # estimate quotes one model while the trials silently run another.
        candidates = generate_candidates(
            profile, BoundedCapabilityPlanner(source=source), model=effective_model
        )

    field_models = {candidate.model for candidate in candidates.candidates}
    if len(field_models) == 1:
        field_model = next(iter(field_models))
        if isinstance(field_model, str) and field_model:
            # Candidate generation is the final authority on the evaluated
            # field. An applied sidecar can appear or change between the first
            # read above and exact materialization inside generation, so price
            # and report the model every arm actually carries.
            effective_model = field_model
        else:
            candidates = replace(
                candidates,
                abstained=True,
                abstention_reason="the generated candidate field has no pinned model",
            )
    elif candidates.candidates:
        candidates = replace(
            candidates,
            abstained=True,
            abstention_reason=(
                "the generated candidate field does not share one pinned model, so its "
                "cost and effects cannot be compared"
            ),
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
        price=ModelPrice.from_litellm(effective_model),
        # The harness is launched with an iteration bound and every iteration
        # resends the accumulated context, so the plan is priced for the loop
        # the trial will actually run rather than for its first exchange. A
        # spend gate that under-promises is worse than one that over-promises.
        expected_input_tokens=EXCHANGE_INPUT_TOKENS * DEFAULT_MAX_ITERATIONS,
        expected_output_tokens=EXCHANGE_OUTPUT_TOKENS * DEFAULT_MAX_ITERATIONS,
        reliability_floor=reliability_floor,
    )

    return ResolvedExperiment(
        profile=profile,
        candidates=candidates,
        tasks=tasks,
        verify_command=verify_command,
        model=effective_model,
        trials_per_task=trials_per_task,
        reliability_floor=reliability_floor,
        plan=plan,
    )


def authorize_experiment(
    experiment: ResolvedExperiment, *, expected_digest: str
) -> ResolvedExperiment:
    """Confirm the bounded plan without changing any part of the campaign.

    This is the explicit API equivalent of answering the CLI confirmation or
    passing ``--yes``.  Authorization is carried on the immutable plan so the
    public runner and the lower executor can both enforce it; it is not ambient
    CLI state that disappears below the command boundary.
    """

    if not experiment.can_authorize:
        raise PermissionError(f"cannot authorize experiment: {experiment.plan.explanation}")
    digest = experiment.plan.executable_digest
    if expected_digest != digest:
        raise PermissionError(
            "cannot authorize experiment: the confirmed plan digest does not match "
            "the executable plan"
        )
    if baseline_error := _current_baseline_error(experiment):
        raise PermissionError(
            f"cannot authorize experiment: current baseline changed: {baseline_error}"
        )
    budget = experiment.plan.budget_usd
    if not _finite_number(budget):
        # ``can_authorize`` already implies this through the decision, but keep
        # the authorization constructor independently fail-closed if a plan is
        # ever built outside ``plan_experiment``.
        raise PermissionError("cannot authorize experiment without a finite budget")
    return replace(
        experiment,
        plan=replace(
            experiment.plan,
            authorized=True,
            authorization_digest=digest,
        ),
    )


def run_experiment(experiment: ResolvedExperiment, *, live: bool) -> ExperimentOutcome:
    """Run the experiment that was resolved, under the budget it was planned against.

    ``live`` chooses the runner and nothing else. The candidates, the tasks,
    the trial count and the authorization all come from ``experiment``, so a
    campaign cannot quietly differ from the plan the user approved.

    Raises :class:`~ctx.fit.providers.ProviderUnavailable` when a live run is
    asked for and no agent can be driven — before any workspace exists, so the
    caller can still say truthfully that nothing was run and nothing was spent.

    A live run is refused here unless the immutable plan carries both its
    bounded budget and explicit authorization.  The CLI performs the preview
    and confirmation, but this public spending boundary never trusts that its
    caller remembered to do so.
    """

    if live and not experiment.can_execute:
        raise PermissionError(
            "live execution requires an authorized, bounded experiment plan; "
            "confirm the preview with authorize_experiment() first"
        )
    if live and (baseline_error := _current_baseline_error(experiment)):
        raise PermissionError(
            f"live execution refused because the current baseline changed: {baseline_error}"
        )

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
            reliability_floor=experiment.reliability_floor,
            simulated=simulated,
            # The authorization is over real money. A simulated trial's cost is
            # an invention of the simulator, so charging it against the user's
            # budget would truncate the pipeline demonstration over dollars
            # nobody was ever going to be billed.
            execution_plan=None if simulated else experiment.plan,
            runner_for_budget=runner_for_budget,
        )
    finally:
        # The campaign owns the environment's lifetime: a whole dependency set
        # on disk must not outlive the run that needed it, including when the
        # run raises.
        if environment is not None:
            environment.close()

    recommendation = recommend(
        report,
        experiment.candidates.candidates,
        # This is the declared field, not only the slots that happened to
        # produce evidence. Shrinking the count after an infrastructure or
        # budget failure made a partial report look complete.
        task_count=len(tasks),
        trials_per_task=experiment.trials_per_task,
        expected_plan_digest=experiment.plan.executable_digest,
        expected_task_ids=tuple(task.task_id for task in tasks),
        expected_reliability_floor=experiment.reliability_floor,
    )
    if live and (baseline_error := _current_baseline_error(experiment)):
        recommendation = replace(
            recommendation,
            verdict="no-verdict",
            winner_id=None,
            reasoning=(
                "The repository's current baseline changed during the paid "
                "campaign, so its report is preserved but cannot support a "
                "recommendation.",
                *recommendation.reasoning,
            ),
            limitations=(
                *recommendation.limitations,
                f"Current-baseline drift after execution: {baseline_error}",
            ),
            confidence="low",
        )

    return ExperimentOutcome(
        experiment=experiment,
        report=report,
        recommendation=recommendation,
        simulated=simulated,
    )


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_MODEL_PROVIDER",
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
    "PlannedCandidate",
    "PlannedTask",
    "PlanDecision",
    "ResolvedExperiment",
    "authorize_experiment",
    "plan_experiment",
    "resolve_experiment",
    "run_experiment",
]
