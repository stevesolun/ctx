"""Bounded candidate configuration generation.

The configuration space is combinatorial — agents times models times skills
times instructions. Brute force is both unaffordable and contrary to the point
of CTX. This module is where CTX's existing intelligence earns its place: the
shipped capability catalog and the already-accepted
:class:`~ctx.engine.planner.BoundedCapabilityPlanner` reduce that space to a
handful of capabilities, and this module composes those into a small, diverse,
explained set of configurations worth actually testing.

Three rules:

**Every candidate explains itself.** A configuration with no answer to "why did
CTX believe this was worth testing?" is not admitted. The reason is carried as
data, not generated prose.

**Diversity beats ranking.** Testing three near-identical configurations wastes
a budget that could have distinguished real alternatives, so the set is
composed from distinct roles rather than taking the top N by score.

**The baseline is always present.** No improvement may be claimed without the
repository's current setup as a control.

No model is called here, and nothing is executed. Generation is deterministic:
the same profile and catalog always produce the same candidate set.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

from ctx.engine.planner import (
    BoundedCapabilityPlanner,
    CapabilityCandidate,
    CapabilityPlan,
    WorkObservation,
)
from ctx.fit.profile import FitProfile

CANDIDATE_SCHEMA = "ctx.fit.candidate-v1"

#: The role a candidate plays in the comparison. Roles exist so that a limited
#: evaluation budget buys information rather than repetition.
CandidateRole = Literal[
    "baseline",
    "recommended",
    "lean",
    "exploratory",
]

ROLE_INTENT: dict[CandidateRole, str] = {
    "baseline": "the repository's current setup, used as the control",
    "recommended": "the capabilities CTX ranks most relevant to this repository",
    "lean": "the single highest-ranked capability, to test whether less is enough",
    "exploratory": "a relevant capability the top-ranked set left out",
}

#: A Fit experiment compares a small set. More arms multiply cost without
#: adding much information at the sample sizes Fit can afford.
MAX_CANDIDATES = 4


@dataclass(frozen=True, slots=True)
class CandidateConfiguration:
    """One configuration worth testing, with the reason it was selected.

    Differences between candidates live in explicit fields rather than inside
    free-form prompt text, so two candidates can always be diffed exactly.
    """

    candidate_id: str
    role: CandidateRole
    capability_ids: tuple[str, ...]
    model: str | None
    instructions: tuple[str, ...]
    selection_reason: str
    evidence: tuple[str, ...] = ()

    @property
    def configuration_hash(self) -> str:
        """Stable identity of what this configuration actually *is*.

        Two candidates with the same hash are the same experiment and must
        never both be run.
        """

        payload = json.dumps(
            {
                "capability_ids": sorted(self.capability_ids),
                "instructions": sorted(self.instructions),
                "model": self.model,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": CANDIDATE_SCHEMA,
            "candidate_id": self.candidate_id,
            "role": self.role,
            "role_intent": ROLE_INTENT[self.role],
            "capability_ids": list(self.capability_ids),
            "model": self.model,
            "instructions": list(self.instructions),
            "selection_reason": self.selection_reason,
            "evidence": list(self.evidence),
            "configuration_hash": self.configuration_hash,
        }


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """The bounded, explained set of configurations Fit would evaluate."""

    candidates: tuple[CandidateConfiguration, ...] = ()
    abstained: bool = False
    abstention_reason: str | None = None
    considered: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def baseline(self) -> CandidateConfiguration | None:
        return next((item for item in self.candidates if item.role == "baseline"), None)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates": [item.to_dict() for item in self.candidates],
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
            "considered": self.considered,
            "warnings": list(self.warnings),
        }


def _observation_from_profile(profile: FitProfile, *, limit: int) -> WorkObservation:
    """Turn a repository profile into privacy-safe planner signals.

    Only normalized tokens leave the profile — never file contents, paths, or
    prose.
    """

    stack = profile.stack or {}
    languages = {
        str(item["name"])
        for item in stack.get("languages", [])
        if isinstance(item, dict) and item.get("name")
    }
    signals: set[str] = set(languages)
    for group in ("frameworks", "testing", "infrastructure", "build_system"):
        signals.update(
            str(item["name"])
            for item in stack.get(group, [])
            if isinstance(item, dict) and item.get("name")
        )
    # The repository's verification style is a strong relevance signal: a repo
    # that type-checks wants different help from one that only runs tests.
    signals.update(profile.verification.kinds)

    # The planner requires canonical sorted, deduplicated tokens so that the
    # same repository always yields the same plan.
    return WorkObservation(
        signals=tuple(sorted(signals)),
        languages=tuple(sorted(languages)),
        baseline_capability_ids=(),
        requested_limit=limit,
    )


def _baseline(profile: FitProfile) -> CandidateConfiguration:
    config = profile.existing_ai_config
    evidence: list[str] = []
    if config.instruction_files:
        evidence.append(f"instructions: {', '.join(config.instruction_files)}")
    for label, count in config.capability_counts:
        evidence.append(f"{count} {label} already installed")
    if not evidence:
        evidence.append("no AI coding configuration detected in this repository")

    return CandidateConfiguration(
        candidate_id="baseline",
        role="baseline",
        capability_ids=(),
        model=None,
        instructions=config.instruction_files,
        selection_reason=(
            "The repository's current setup. Every comparison needs a control, "
            "and no improvement can be claimed without one."
        ),
        evidence=tuple(evidence),
    )


def _describe(candidate: CapabilityCandidate) -> str:
    matched = ", ".join(candidate.matching_signals[:3])
    if matched:
        return f"{candidate.capability_id} (matches {matched})"
    return candidate.capability_id


def generate_candidates(
    profile: FitProfile,
    planner: BoundedCapabilityPlanner,
    *,
    model: str | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> CandidateSet:
    """Produce a bounded, diverse, explained candidate set for one repository.

    Returns an abstaining set rather than inventing configurations when the
    repository cannot be evaluated or nothing relevant was found. Abstention is
    a valid outcome: proposing an experiment that cannot produce trustworthy
    evidence would waste the user's money.
    """

    warnings: list[str] = []
    baseline = _baseline(profile)

    if not profile.is_fit_evaluable:
        return CandidateSet(
            candidates=(baseline,),
            abstained=True,
            abstention_reason=(
                "this repository has no runnable tests, so no candidate could be "
                "verified against it"
            ),
        )

    observation = _observation_from_profile(profile, limit=5)
    plan: CapabilityPlan = planner.plan(observation)

    if plan.status != "ready" or not plan.selections:
        reason = {
            "abstained": "CTX found no capability relevant enough to this repository to be worth testing",
            "degraded": "the capability catalog was unavailable, so no candidate could be proposed",
        }.get(plan.status, "no relevant capability was found")
        if plan.abstention_code:
            warnings.append(f"planner: {plan.abstention_code}")
        return CandidateSet(
            candidates=(baseline,),
            abstained=True,
            abstention_reason=reason,
            warnings=tuple(warnings),
        )

    ranked = list(plan.selections)
    considered = len(ranked)
    top_ids = tuple(item.capability_id for item in ranked)

    candidates: list[CandidateConfiguration] = [baseline]

    candidates.append(
        CandidateConfiguration(
            candidate_id="recommended",
            role="recommended",
            capability_ids=top_ids,
            model=model,
            instructions=profile.existing_ai_config.instruction_files,
            selection_reason=(
                f"CTX ranked these {len(top_ids)} capabilities most relevant to this "
                "repository's languages, frameworks and verification style."
            ),
            evidence=tuple(_describe_selection(item) for item in ranked),
        )
    )

    if len(top_ids) > 1:
        candidates.append(
            CandidateConfiguration(
                candidate_id="lean",
                role="lean",
                capability_ids=top_ids[:1],
                model=model,
                instructions=profile.existing_ai_config.instruction_files,
                selection_reason=(
                    "Only the single highest-ranked capability, to test whether the "
                    "larger set earns its added context cost."
                ),
                evidence=(_describe_selection(ranked[0]),),
            )
        )

    # Deduplicate by what the configuration actually is, not by its name.
    unique: list[CandidateConfiguration] = []
    seen: set[str] = set()
    for candidate in candidates:
        digest = candidate.configuration_hash
        if digest in seen:
            warnings.append(f"dropped {candidate.candidate_id}: identical to an earlier candidate")
            continue
        seen.add(digest)
        unique.append(candidate)

    if len(unique) > max_candidates:
        warnings.append(
            f"kept {max_candidates} of {len(unique)} candidates to stay within the evaluation budget"
        )
        unique = unique[:max_candidates]

    return CandidateSet(
        candidates=tuple(unique),
        abstained=False,
        considered=considered,
        warnings=tuple(warnings),
    )


def _describe_selection(selection: object) -> str:
    capability_id = getattr(selection, "capability_id", "")
    reasons = getattr(selection, "reason_codes", ()) or ()
    if reasons:
        return f"{capability_id} ({', '.join(str(code) for code in reasons[:2])})"
    return str(capability_id)


__all__ = [
    "CANDIDATE_SCHEMA",
    "MAX_CANDIDATES",
    "ROLE_INTENT",
    "CandidateConfiguration",
    "CandidateRole",
    "CandidateSet",
    "generate_candidates",
]
