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

Three refusals are as important as the choice itself. A candidate whose cost is
incomplete cannot be called cheapest, so it is reported unranked rather than
winning by having less data. Neither can a candidate whose campaign the budget
cut short: its total is a real price for a fraction of the work, and comparing
it against a rival that finished would hand the win to whoever was interrupted
soonest. And when nothing beats the baseline, the honest answer is to keep the
current setup — a successful experiment, not a failure.

When no candidate can be ranked, the reason given is the reason that actually
fired. Reporting "nothing cleared the reliability floor" under a table of
candidates that all verified every trial told users the opposite of what their
own evidence said (FITBUG-046).
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


#: Why a candidate could not be ranked, in the order the checks are applied.
#: Kept apart from the per-candidate sentence so the summary can name the cause
#: that actually emptied the field instead of assuming one.
_UNRELIABLE = "unreliable"
_TRUNCATED = "budget-truncated"
_UNMEASURED = "unmeasured-cost"

_NO_VERDICT_REASON: dict[str, str] = {
    _UNRELIABLE: (
        "{count} candidate(s) did not reach the reliability floor, so they have not "
        "been shown to work on this repository."
    ),
    _TRUNCATED: (
        "{count} candidate(s) worked reliably, but the authorized budget ran out "
        "before they finished, so what they cost cannot be compared with anything. "
        "Re-run with a larger --budget."
    ),
    _UNMEASURED: (
        "{count} candidate(s) worked reliably, but part of their spend was never "
        "measured, so none of them can be called cheapest."
    ),
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


def _no_verdict_reasoning(excluded_by: dict[str, list[str]]) -> tuple[str, ...]:
    """Say which exclusion actually emptied the field, not which one is likeliest."""

    lines = [
        _NO_VERDICT_REASON[kind].format(count=len(ids)) for kind, ids in excluded_by.items() if ids
    ]
    if not lines:
        # No candidates at all: nothing was excluded because nothing was tried.
        lines.append("No candidate was evaluated, so there is nothing to recommend.")
    return tuple(lines)


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
    # A run is simulated if any evidence in it is. Reading only the flag the
    # caller passed made the per-trial flags -- the ones execution.py calls
    # indelible -- decorative, so a mixed or mislabelled campaign could be
    # applied as if it were real (FITBUG-065).
    simulated = report.simulated or any(
        trial.simulated for outcome in report.outcomes for trial in outcome.trials
    )

    ranked: list[RankedCandidate] = []
    excluded_by: dict[str, list[str]] = {_UNRELIABLE: [], _TRUNCATED: [], _UNMEASURED: []}
    inconclusive_trials = 0
    infrastructure_trials = 0
    for candidate_id, outcome in outcomes.items():
        configuration = by_id.get(candidate_id)
        capability_count = len(configuration.capability_ids) if configuration else 0
        inconclusive_trials += sum(1 for trial in outcome.trials if trial.outcome == "inconclusive")
        infrastructure_trials += sum(
            1 for trial in outcome.trials if trial.outcome == "infrastructure-failure"
        )

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
            excluded_by[_UNRELIABLE].append(candidate_id)
        elif outcome.budget_truncated:
            # A price for two trials is not a cheaper price than a rival's price
            # for nine; it is a price for less work. Ranking them together would
            # award the win to whichever candidate the budget interrupted first.
            qualified = False
            exclusion = (
                "the budget ran out before this candidate finished, so its cost "
                "cannot be compared with candidates that completed"
            )
            excluded_by[_TRUNCATED].append(candidate_id)
        elif not outcome.cost_is_complete:
            # Cost is the objective; an unknown cost cannot be the smallest one.
            qualified = False
            exclusion = "cost is incomplete, so this candidate cannot be ranked as cheapest"
            excluded_by[_UNMEASURED].append(candidate_id)

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

    def cheapest_first(item: RankedCandidate) -> tuple[float, int]:
        """The selection rule itself, so the tables print in the order it ranks.

        ``or float("inf")`` would send a legitimate $0.00 candidate -- a local
        model -- to the bottom of a cheapest-first table it had just won, and
        omitting the capability tie-break printed the loser of a tie above the
        winner with nothing on screen to explain it (FITBUG-069).
        """

        cost = item.total_cost_usd
        return (float("inf") if cost is None else cost, item.capability_count)

    qualifying = [item for item in ranked if item.qualified and item.total_cost_usd is not None]
    # Cheapest first; simpler configuration breaks a tie.
    qualifying.sort(key=cheapest_first)
    ranked.sort(key=lambda item: (not item.qualified, *cheapest_first(item)))

    reasoning: list[str] = []
    limitations: list[str] = []
    if simulated:
        limitations.append(
            "This was a simulated run. It demonstrates that the evaluation "
            "pipeline works end to end and says nothing about which configuration "
            "is actually better for this repository."
        )
    if task_count < 3:
        limitations.append(
            f"only {task_count} task(s) were evaluated, which is too few to generalize beyond them"
        )
    if excluded_by[_UNRELIABLE]:
        limitations.append(
            f"{len(excluded_by[_UNRELIABLE])} candidate(s) were excluded before cost was considered"
        )
    if excluded_by[_TRUNCATED]:
        limitations.append(
            f"{len(excluded_by[_TRUNCATED])} candidate(s) did not finish inside the "
            "authorized budget and were left unranked; only candidates that completed "
            "every trial are compared here"
        )
    if inconclusive_trials:
        limitations.append(
            f"{inconclusive_trials} trial(s) ended inconclusively and are excluded from "
            "reliability, so the candidates were judged on fewer trials than were paid for"
        )
    if infrastructure_trials:
        limitations.append(
            f"{infrastructure_trials} trial(s) failed for infrastructure reasons and were "
            "excluded from both reliability and cost"
        )

    baseline = next((item for item in qualifying if item.candidate_id == "baseline"), None)

    if not qualifying:
        return Recommendation(
            schema=RECOMMENDATION_SCHEMA,
            verdict="no-verdict",
            winner_id=None,
            ranked=tuple(ranked),
            reasoning=_no_verdict_reasoning(excluded_by),
            limitations=tuple(limitations),
            confidence="low",
            simulated=simulated,
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

    # On keep-current the winner is the setup being kept. Naming the challenger
    # that merely tied contradicted the headline in the same object, and any
    # consumer reading winner_id without also branching on verdict acted on the
    # wrong candidate (FITBUG-070).
    keeping = verdict == "keep-current" and baseline is not None
    winner_id = baseline.candidate_id if keeping and baseline else cheapest.candidate_id

    return Recommendation(
        schema=RECOMMENDATION_SCHEMA,
        verdict=verdict,
        winner_id=winner_id,
        ranked=tuple(ranked),
        reasoning=tuple(reasoning),
        limitations=tuple(limitations),
        confidence=_confidence(len(qualifying), task_count, trials_per_task, simulated=simulated),
        simulated=simulated,
    )


__all__ = [
    "RECOMMENDATION_SCHEMA",
    "VERDICT_HEADLINE",
    "RankedCandidate",
    "Recommendation",
    "Verdict",
    "recommend",
]
