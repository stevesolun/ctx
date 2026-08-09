"""Choosing a winner, and refusing to when the evidence does not support one.

The rule is lexicographic and deterministic (ADR-014):

1. **Filter on reliability.** A candidate that does not clear the floor is
   excluded before cost is even looked at. Cheapness among configurations that
   do not work is meaningless.
2. **Minimize attributable cost** among survivors.
3. **Tie-break toward simplicity** — fewer capabilities.

An LLM may help *explain* a result. It never determines one: the winner falls
out of arithmetic over recorded evidence, so any verdict can be recomputed and
audited.

Two refusals are as important as the choice itself. A candidate whose cost is
incomplete cannot be called cheapest, so it is reported unranked rather than
winning by having less data. And when nothing beats the baseline, the honest
answer is to keep the current setup — a successful experiment, not a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import CandidateOutcome, ExecutionReport

RECOMMENDATION_SCHEMA = "ctx.fit.recommendation-v1"

Verdict = Literal[
    "recommend-change",
    "keep-current",
    "no-verdict",
]

VERDICT_HEADLINE: dict[Verdict, str] = {
    "recommend-change": "A cheaper configuration reliably works on this repository",
    "keep-current": "No tested configuration beat your current setup",
    "no-verdict": "The evidence does not support a recommendation",
}


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate_id: str
    reliability: float | None
    verified: int
    scored: int
    total_cost_usd: float | None
    capability_count: int
    qualified: bool
    exclusion_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "reliability": self.reliability,
            "verified": self.verified,
            "scored": self.scored,
            "total_cost_usd": self.total_cost_usd,
            "capability_count": self.capability_count,
            "qualified": self.qualified,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True, slots=True)
class Recommendation:
    schema: str
    verdict: Verdict
    winner_id: str | None
    ranked: tuple[RankedCandidate, ...]
    reasoning: tuple[str, ...]
    limitations: tuple[str, ...]
    confidence: Literal["low", "medium", "high"]
    simulated: bool = False

    @property
    def headline(self) -> str:
        if self.simulated:
            return f"SIMULATED — {VERDICT_HEADLINE[self.verdict]}"
        return VERDICT_HEADLINE[self.verdict]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "verdict": self.verdict,
            "headline": self.headline,
            "winner_id": self.winner_id,
            "ranked": [item.to_dict() for item in self.ranked],
            "reasoning": list(self.reasoning),
            "limitations": list(self.limitations),
            "confidence": self.confidence,
            "simulated": self.simulated,
        }


def _confidence(
    qualified: int, tasks: int, trials: int, *, simulated: bool
) -> Literal["low", "medium", "high"]:
    """Confidence reflects how much evidence exists, never how good it looks."""

    if simulated:
        return "low"
    if tasks >= 5 and trials >= 3 and qualified >= 2:
        return "high"
    if tasks >= 3 and trials >= 2:
        return "medium"
    return "low"


def recommend(
    report: ExecutionReport,
    candidates: tuple[CandidateConfiguration, ...],
    *,
    task_count: int,
    trials_per_task: int,
) -> Recommendation:
    """Apply the lexicographic rule to recorded evidence."""

    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    outcomes: dict[str, CandidateOutcome] = {
        outcome.candidate_id: outcome for outcome in report.outcomes
    }

    ranked: list[RankedCandidate] = []
    for candidate_id, outcome in outcomes.items():
        configuration = by_id.get(candidate_id)
        capability_count = len(configuration.capability_ids) if configuration else 0

        exclusion: str | None = None
        qualified = True
        if not outcome.is_reliable:
            qualified = False
            reliability = outcome.reliability
            exclusion = (
                f"verified {outcome.verified_count}/{len(outcome.scored_trials)} trials"
                if reliability is not None
                else "no scored trials"
            )
        elif not outcome.cost_is_complete:
            # Cost is the objective; an unknown cost cannot be the smallest one.
            qualified = False
            exclusion = "cost is incomplete, so this candidate cannot be ranked as cheapest"

        ranked.append(
            RankedCandidate(
                candidate_id=candidate_id,
                reliability=outcome.reliability,
                verified=outcome.verified_count,
                scored=len(outcome.scored_trials),
                total_cost_usd=outcome.total_cost_usd,
                capability_count=capability_count,
                qualified=qualified,
                exclusion_reason=exclusion,
            )
        )

    qualifying = [item for item in ranked if item.qualified and item.total_cost_usd is not None]
    # Cheapest first; simpler configuration breaks a tie.
    qualifying.sort(key=lambda item: (item.total_cost_usd or 0.0, item.capability_count))
    ranked.sort(key=lambda item: (not item.qualified, item.total_cost_usd or float("inf")))

    reasoning: list[str] = []
    limitations: list[str] = []
    if report.simulated:
        limitations.append(
            "This was a simulated run. It demonstrates that the evaluation "
            "pipeline works end to end and says nothing about which configuration "
            "is actually better for this repository."
        )
    if task_count < 3:
        limitations.append(
            f"only {task_count} task(s) were evaluated, which is too few to generalize beyond them"
        )
    excluded = [item for item in ranked if not item.qualified]
    if excluded:
        limitations.append(f"{len(excluded)} candidate(s) were excluded before cost was considered")

    baseline = next((item for item in qualifying if item.candidate_id == "baseline"), None)

    if not qualifying:
        return Recommendation(
            schema=RECOMMENDATION_SCHEMA,
            verdict="no-verdict",
            winner_id=None,
            ranked=tuple(ranked),
            reasoning=(
                "No candidate reached the reliability floor, so none has been shown "
                "to work on this repository.",
            ),
            limitations=tuple(limitations),
            confidence="low",
            simulated=report.simulated,
        )

    cheapest = qualifying[0]
    reasoning.append(
        f"{cheapest.candidate_id} verified {cheapest.verified}/{cheapest.scored} trials "
        f"at ${cheapest.total_cost_usd}, the lowest cost among candidates that cleared "
        "the reliability floor."
    )

    if baseline is not None and cheapest.candidate_id == "baseline":
        verdict: Verdict = "keep-current"
        reasoning.append(
            "Your current setup was both reliable and the cheapest option tested, "
            "so changing it would cost more for no measured gain."
        )
    elif baseline is not None and baseline.total_cost_usd is not None:
        saving = round(baseline.total_cost_usd - (cheapest.total_cost_usd or 0.0), 4)
        if saving <= 0:
            verdict = "keep-current"
            reasoning.append("No qualifying candidate was cheaper than your current setup.")
        else:
            verdict = "recommend-change"
            reasoning.append(f"That is ${saving} less than the baseline across the same tasks.")
    else:
        verdict = "recommend-change"
        limitations.append(
            "the baseline did not qualify, so the comparison is against tested "
            "candidates rather than your current setup"
        )

    return Recommendation(
        schema=RECOMMENDATION_SCHEMA,
        verdict=verdict,
        winner_id=cheapest.candidate_id if verdict != "no-verdict" else None,
        ranked=tuple(ranked),
        reasoning=tuple(reasoning),
        limitations=tuple(limitations),
        confidence=_confidence(
            len(qualifying), task_count, trials_per_task, simulated=report.simulated
        ),
        simulated=report.simulated,
    )


__all__ = [
    "RECOMMENDATION_SCHEMA",
    "VERDICT_HEADLINE",
    "RankedCandidate",
    "Recommendation",
    "Verdict",
    "recommend",
]
