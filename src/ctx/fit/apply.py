"""Turning a recommendation into reviewable repository changes, and a PR.

The product promise ends with "…then opens a PR containing the winning
configuration", so this is the terminal deliverable rather than an optional
extra. A report a user has to hand-translate into config files has not finished
the job.

Three safety properties, in order of importance:

**Nothing is written without being shown first.** Artifacts are generated into
a preview and applied only on an explicit, separate request.

**Nothing is applied from evidence that cannot support it.** A simulated run, a
`no-verdict`, or a `keep-current` verdict produces no configuration change —
the last of those because "your setup already won" means the correct action is
to change nothing.

**Nothing is merged.** This prepares a branch and a PR body. A human merges.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.recommend import Recommendation

APPLY_SCHEMA = "ctx.fit.apply-v1"

BRANCH_PREFIX = "ctx-fit"

ApplyRefusal = Literal[
    "simulated-evidence",
    "no-verdict",
    "keep-current",
    "winner-not-found",
]

REFUSAL_EXPLANATION: dict[ApplyRefusal, str] = {
    "simulated-evidence": (
        "this recommendation came from a simulated run, which proves the pipeline "
        "works but nothing about this repository"
    ),
    "no-verdict": "no candidate cleared the reliability floor, so there is nothing to apply",
    "keep-current": ("your current setup already won, so the correct action is to change nothing"),
    "winner-not-found": "the winning candidate is not among the configurations supplied",
}


@dataclass(frozen=True, slots=True)
class Artifact:
    """One file CTX Fit would create or modify, with its full proposed content."""

    path: str
    content: str
    action: Literal["create", "modify"]
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "bytes": len(self.content.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """What would change, why, and whether it may be applied at all."""

    schema: str
    artifacts: tuple[Artifact, ...]
    branch: str
    pr_title: str
    pr_body: str
    refusal: ApplyRefusal | None = None

    @property
    def can_apply(self) -> bool:
        return self.refusal is None and bool(self.artifacts)

    @property
    def explanation(self) -> str:
        if self.refusal is None:
            return "ready to apply"
        return REFUSAL_EXPLANATION[self.refusal]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "branch": self.branch,
            "pr_title": self.pr_title,
            "can_apply": self.can_apply,
            "refusal": self.refusal,
            "explanation": self.explanation,
        }


def _agents_md(winner: CandidateConfiguration, recommendation: Recommendation) -> str:
    ranked = next(
        (item for item in recommendation.ranked if item.candidate_id == winner.candidate_id),
        None,
    )
    lines = [
        "# AI coding configuration",
        "",
        "This configuration was selected by CTX Fit: it was the cheapest setup that",
        "reliably passed real tasks from this repository's own history.",
        "",
        "## Capabilities",
        "",
    ]
    if winner.capability_ids:
        lines.extend(f"- `{capability}`" for capability in winner.capability_ids)
    else:
        lines.append("- none; the repository's existing setup was sufficient")
    lines.extend(["", "## Evidence", ""])
    if ranked is not None:
        lines.append(
            f"- verified {ranked.verified}/{ranked.scored} trials"
            + (f" at ${ranked.total_cost_usd}" if ranked.total_cost_usd is not None else "")
        )
    lines.extend(f"- {line}" for line in recommendation.reasoning)
    lines.extend(["", f"Confidence: {recommendation.confidence}.", ""])
    if recommendation.limitations:
        lines.append("## Limitations")
        lines.append("")
        lines.extend(f"- {line}" for line in recommendation.limitations)
        lines.append("")
    return "\n".join(lines)


def _pr_body(winner: CandidateConfiguration, recommendation: Recommendation) -> str:
    lines = [
        "## CTX Fit recommendation",
        "",
        recommendation.headline + ".",
        "",
        "### Winning configuration",
        "",
        f"`{winner.candidate_id}` — {winner.selection_reason}",
        "",
        "### How candidates compared",
        "",
        "| Candidate | Verified | Cost | Qualified |",
        "| --- | --- | --- | --- |",
    ]
    for item in recommendation.ranked:
        cost = f"${item.total_cost_usd}" if item.total_cost_usd is not None else "unknown"
        qualified = "yes" if item.qualified else f"no — {item.exclusion_reason}"
        lines.append(
            f"| {item.candidate_id} | {item.verified}/{item.scored} | {cost} | {qualified} |"
        )
    lines.extend(["", "### Why this won", ""])
    lines.extend(f"- {line}" for line in recommendation.reasoning)
    lines.extend(["", f"**Confidence:** {recommendation.confidence}", ""])
    if recommendation.limitations:
        lines.extend(["### Limitations", ""])
        lines.extend(f"- {line}" for line in recommendation.limitations)
        lines.append("")
    lines.extend(
        [
            "### Rollback",
            "",
            "Delete this branch, or revert the merge commit. CTX Fit changes only",
            "configuration files and never application code.",
            "",
            "---",
            "",
            "Prepared by CTX Fit. Review before merging; CTX Fit never merges.",
        ]
    )
    return "\n".join(lines)


def plan_apply(
    recommendation: Recommendation,
    candidates: tuple[CandidateConfiguration, ...],
    *,
    repo_path: str | Path = ".",
    run_id: str = "run",
) -> ApplyPlan:
    """Produce a reviewable set of changes, or refuse with a reason.

    Writes nothing. The returned plan is applied only by a separate, explicit
    call to :func:`apply_plan`.
    """

    branch = f"{BRANCH_PREFIX}/{run_id}"
    title = "Optimize AI coding configuration using CTX Fit"

    def refuse(reason: ApplyRefusal) -> ApplyPlan:
        return ApplyPlan(
            schema=APPLY_SCHEMA,
            artifacts=(),
            branch=branch,
            pr_title=title,
            pr_body="",
            refusal=reason,
        )

    if recommendation.simulated:
        return refuse("simulated-evidence")
    if recommendation.verdict == "no-verdict":
        return refuse("no-verdict")
    if recommendation.verdict == "keep-current":
        return refuse("keep-current")

    winner = next(
        (item for item in candidates if item.candidate_id == recommendation.winner_id),
        None,
    )
    if winner is None:
        return refuse("winner-not-found")

    root = Path(repo_path)
    target = root / "AGENTS.md"
    artifacts = (
        Artifact(
            path="AGENTS.md",
            content=_agents_md(winner, recommendation),
            action="modify" if target.exists() else "create",
            reason="records the winning capability set and the evidence behind it",
        ),
    )

    return ApplyPlan(
        schema=APPLY_SCHEMA,
        artifacts=artifacts,
        branch=branch,
        pr_title=title,
        pr_body=_pr_body(winner, recommendation),
    )


def apply_plan(plan: ApplyPlan, repo_path: str | Path) -> tuple[str, ...]:
    """Write the planned artifacts. Returns the paths written.

    Refuses to write anything when the plan is not applicable, so a caller
    cannot accidentally materialize a refused recommendation.
    """

    if not plan.can_apply:
        raise ValueError(f"this plan cannot be applied: {plan.explanation}")

    root = Path(repo_path)
    written: list[str] = []
    for artifact in plan.artifacts:
        destination = root / artifact.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(artifact.content, encoding="utf-8")
        written.append(artifact.path)
    return tuple(written)


__all__ = [
    "APPLY_SCHEMA",
    "BRANCH_PREFIX",
    "REFUSAL_EXPLANATION",
    "ApplyPlan",
    "ApplyRefusal",
    "Artifact",
    "apply_plan",
    "plan_apply",
]
