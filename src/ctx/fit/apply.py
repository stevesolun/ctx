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

**Nothing the user wrote is destroyed, and nothing outside the repository is
touched.** The generated document lives inside a delimited block that CTX Fit
owns; every other byte of an existing file is carried through untouched. A
destination that is a symbolic link is refused rather than followed: writing
through it would edit a file the preview never named, possibly outside the
repository entirely, where git cannot show it and the PR's rollback advice
cannot undo it (FITBUG-010, FITBUG-011).

**Nothing is merged.** This prepares a branch and a PR body. A human merges.
"""

from __future__ import annotations

import os
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
    "unsafe-destination",
]

REFUSAL_EXPLANATION: dict[ApplyRefusal, str] = {
    "simulated-evidence": (
        "this recommendation came from a simulated run, which proves the pipeline "
        "works but nothing about this repository"
    ),
    # Deliberately silent on the cause: `qualifying` empties for several
    # different reasons and asserting the reliability one told users their
    # repository had defeated every configuration when in fact every
    # configuration had passed every trial (FITBUG-046). The specific cause
    # arrives as the refusal detail, from the recommendation's own reasoning.
    "no-verdict": "the evidence does not support a change, so there is nothing to apply",
    "keep-current": ("your current setup already won, so the correct action is to change nothing"),
    "winner-not-found": "the winning candidate is not among the configurations supplied",
    "unsafe-destination": (
        "a file this change would write cannot be written safely, and CTX Fit will "
        "not edit a file it did not name"
    ),
}

#: Delimiters around the part of a configuration file CTX Fit owns. Everything
#: outside them belongs to the user and is carried through verbatim.
OWNED_BLOCK_START = "<!-- BEGIN CTX FIT -->"
OWNED_BLOCK_END = "<!-- END CTX FIT -->"


@dataclass(frozen=True, slots=True)
class Artifact:
    """One file CTX Fit would create or modify, with its full proposed content."""

    path: str
    content: str
    action: Literal["create", "modify"]
    reason: str
    #: Bytes of pre-existing, user-authored content this artifact carries
    #: through unchanged, ignoring surrounding blank space and any block CTX
    #: Fit itself wrote on an earlier run. Zero on a create.
    preserved_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "bytes": len(self.content.encode("utf-8")),
            "preserved_bytes": self.preserved_bytes,
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
    #: What, specifically, was wrong on this run. The refusal names the class of
    #: problem; a class alone left users guessing which of several causes fired.
    refusal_detail: str = ""

    @property
    def can_apply(self) -> bool:
        return self.refusal is None and bool(self.artifacts)

    @property
    def explanation(self) -> str:
        if self.refusal is None:
            return "ready to apply"
        general = REFUSAL_EXPLANATION[self.refusal]
        return f"{general} ({self.refusal_detail})" if self.refusal_detail else general

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "branch": self.branch,
            "pr_title": self.pr_title,
            "can_apply": self.can_apply,
            "refusal": self.refusal,
            "refusal_detail": self.refusal_detail,
            "explanation": self.explanation,
        }


def _owned_block(body: str) -> str:
    """Wrap generated prose in the markers that say who owns it."""

    return f"{OWNED_BLOCK_START}\n{body.strip()}\n{OWNED_BLOCK_END}\n"


def _merge_owned_block(existing: str, block: str) -> str:
    """Splice CTX Fit's block into a file the user also writes in.

    A previous run's block is replaced in place, so repeated applies do not
    stack up copies. Anything else in the file — a hand-written AGENTS.md, the
    house rules, the on-call contact — is carried through byte for byte.
    Overwriting it wholesale destroyed uncommitted work that no rollback could
    recover, while the preview said only "modify: AGENTS.md" (FITBUG-011).
    """

    if not existing.strip():
        return block

    start = existing.find(OWNED_BLOCK_START)
    end = existing.find(OWNED_BLOCK_END, start + 1) if start != -1 else -1
    if start != -1 and end != -1:
        head = existing[:start]
        tail = existing[end + len(OWNED_BLOCK_END) :].lstrip("\n")
        return f"{head}{block}{tail}"
    return f"{existing.rstrip()}\n\n{block}"


def _user_authored(existing: str) -> str:
    """The part of a file CTX Fit does not own: everything outside its block."""

    start = existing.find(OWNED_BLOCK_START)
    end = existing.find(OWNED_BLOCK_END, start + 1) if start != -1 else -1
    if start == -1 or end == -1:
        return existing
    return existing[:start] + existing[end + len(OWNED_BLOCK_END) :]


def _read_destination(target: Path) -> tuple[str, str | None]:
    """Current contents of a destination, or the reason it cannot be written.

    A symbolic link is a refusal rather than a target: ``write_text`` follows
    it, so the bytes land in the link's target — frequently a shared file
    outside the repository, where git shows nothing and the PR body's "revert
    the merge commit" is no help at all (FITBUG-010).
    """

    if target.is_symlink():
        try:
            points_at = os.readlink(target)
        except OSError:  # pragma: no cover - readlink on a link we just saw
            points_at = "an unreadable location"
        return "", (
            f"{target.name} is a symbolic link to {points_at}, and writing it would "
            "change that file instead"
        )
    if not target.exists():
        return "", None
    if not target.is_file():
        return "", f"{target.name} is not a regular file"
    try:
        return target.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        # Content that cannot be read cannot be preserved, and a file whose
        # contents are unknown must not be replaced.
        return "", f"{target.name} could not be read, so its contents cannot be preserved: {exc}"


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

    def refuse(reason: ApplyRefusal, detail: str = "") -> ApplyPlan:
        return ApplyPlan(
            schema=APPLY_SCHEMA,
            artifacts=(),
            branch=branch,
            pr_title=title,
            pr_body="",
            refusal=reason,
            refusal_detail=detail,
        )

    if recommendation.simulated:
        return refuse("simulated-evidence")
    if recommendation.verdict == "no-verdict":
        # The recommendation already worked out which of several causes emptied
        # the field; repeating a guess here is what made the refusal wrong.
        return refuse("no-verdict", recommendation.reasoning[0] if recommendation.reasoning else "")
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
    existing, unsafe = _read_destination(target)
    if unsafe is not None:
        return refuse("unsafe-destination", unsafe)

    block = _owned_block(_agents_md(winner, recommendation))
    artifacts = (
        Artifact(
            path="AGENTS.md",
            content=_merge_owned_block(existing, block),
            action="modify" if target.is_file() else "create",
            reason="records the winning capability set and the evidence behind it",
            preserved_bytes=len(_user_authored(existing).strip().encode("utf-8")),
        ),
    )

    return ApplyPlan(
        schema=APPLY_SCHEMA,
        artifacts=artifacts,
        branch=branch,
        pr_title=title,
        pr_body=_pr_body(winner, recommendation),
    )


def _write_atomically(destination: Path, content: str) -> None:
    """Replace a file in one step, so an interrupted write cannot truncate it."""

    scratch = destination.with_name(f".{destination.name}.ctx-fit.tmp")
    try:
        scratch.write_text(content, encoding="utf-8")
        os.replace(scratch, destination)
    finally:
        scratch.unlink(missing_ok=True)


def apply_plan(plan: ApplyPlan, repo_path: str | Path) -> tuple[str, ...]:
    """Write the planned artifacts. Returns the paths written.

    Refuses to write anything when the plan is not applicable, so a caller
    cannot accidentally materialize a refused recommendation.

    The destination is re-checked here even though :func:`plan_apply` already
    checked it: a plan is previewed, discussed and only then applied, and the
    write must not follow a symbolic link that appeared in the meantime. The
    path a user consented to is the one inside their repository.
    """

    if not plan.can_apply:
        raise ValueError(f"this plan cannot be applied: {plan.explanation}")

    root = Path(repo_path)
    root_resolved = root.resolve()
    written: list[str] = []
    for artifact in plan.artifacts:
        relative = Path(artifact.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"refusing to write outside the repository: {artifact.path}")

        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ValueError(
                f"refusing to write through the symbolic link {artifact.path}: it would "
                f"change {os.readlink(destination)}, which this plan never named"
            )
        if not destination.parent.resolve().is_relative_to(root_resolved):
            raise ValueError(f"refusing to write outside the repository: {artifact.path}")

        _write_atomically(destination, artifact.content)
        written.append(artifact.path)
    return tuple(written)


__all__ = [
    "APPLY_SCHEMA",
    "BRANCH_PREFIX",
    "OWNED_BLOCK_END",
    "OWNED_BLOCK_START",
    "REFUSAL_EXPLANATION",
    "ApplyPlan",
    "ApplyRefusal",
    "Artifact",
    "apply_plan",
    "plan_apply",
]
