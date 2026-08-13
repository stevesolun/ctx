"""Turning a recommendation into reviewable repository changes, and a PR.

The product promise ends with "…then opens a PR containing the winning
configuration", so this is the terminal deliverable rather than an optional
extra. A report a user has to hand-translate into config files has not finished
the job.

The two deliverables are deliberately different in strength (FITBUG-036):

**`--apply` runs no git command at all.** :func:`apply_plan` writes the winning
configuration into the working tree and stops. The user reviews it with
``git diff`` and discards it with ``git checkout``, the two commands they
already trust. No branch is created, nothing is committed.

**`--pr` actually opens the pull request.** :func:`plan_pull_request` gathers
the branch, commit, push and ``gh pr create`` invocations and every reason not
to run them; :func:`open_pull_request` runs exactly that announced sequence.
Printing a branch name that was never created was the defect — a reader
believed their change was isolated on a branch they could delete.

Four safety properties, in order of importance:

**Nothing is written without being shown first.** Artifacts are generated into
a preview and applied only on an explicit, separate request. For a pull request
that extends to the commands themselves: every git and gh invocation is
returned for printing before any of them runs.

**Nothing is applied from evidence that cannot support it.** A simulated run, a
`no-verdict`, or a `keep-current` verdict produces no configuration change —
the last of those because "your setup already won" means the correct action is
to change nothing. A pull request is a stronger claim than a file write, so it
refuses on all of those too, and on more besides.

**Nothing the user wrote is destroyed, and nothing outside the repository is
touched.** The generated document lives inside a delimited block that CTX Fit
owns; every other byte of an existing file is carried through untouched. A
destination that is a symbolic link is refused rather than followed: writing
through it would edit a file the preview never named, possibly outside the
repository entirely, where git cannot show it and the PR's rollback advice
cannot undo it (FITBUG-010, FITBUG-011). The same principle gates the pull
request: uncommitted work CTX Fit did not write is never carried into a CTX Fit
branch or commit. That is decided by content, not by filename — `git add --
AGENTS.md` stages a whole file, so a user's unrelated edit *inside* a file CTX
Fit writes has to stop the pull request just as their unrelated file does.

**Nothing is merged.** This opens a pull request. A human merges.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.recommend import Recommendation

APPLY_SCHEMA = "ctx.fit.apply-v1"

BRANCH_PREFIX = "ctx-fit"

#: The remote a pull request is pushed to unless the caller names another.
DEFAULT_REMOTE = "origin"

#: How long any one git or gh command may take. A push to a remote that never
#: answers has to fail the run rather than hang the terminal indefinitely.
COMMAND_TIMEOUT_SECONDS = 300

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


# --------------------------------------------------------------------------
# Opening the pull request.
#
# Every git and gh invocation goes through one injectable runner, so a test can
# assert the exact argv sequence without a repository, a `gh` binary or a
# network — and so the production path has exactly one place that shells out.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one git or gh invocation did."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    #: False when the executable itself was not found. A `gh` that is missing
    #: and a `gh` that is installed but logged out need different messages, and
    #: this is what separates them.
    found: bool = True

    @property
    def ok(self) -> bool:
        return self.found and self.returncode == 0

    @property
    def message(self) -> str:
        """The most useful line the command produced, for a refusal."""

        text = (self.stderr or self.stdout).strip()
        return text.splitlines()[0].strip() if text else ""


class CommandRunner(Protocol):
    """Runs one command in the repository and reports what happened."""

    def __call__(
        self, command: Sequence[str], *, cwd: Path, stdin: str | None = None
    ) -> CommandResult: ...


def run_command(command: Sequence[str], *, cwd: Path, stdin: str | None = None) -> CommandResult:
    """The production runner: an actual subprocess, never a shell."""

    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        # An absent binary is a different problem from a binary that ran and
        # refused, and the user needs to be told which one they have.
        return CommandResult(returncode=127, stderr=str(exc), found=False)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        # Decoding is in the list because one of these commands reads a file out
        # of the repository (`git show`), and a repository may hold bytes that
        # are not text. Unreadable output is a failed command, not a traceback.
        return CommandResult(returncode=1, stderr=f"{command[0]} could not be run: {exc}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


PullRequestRefusal = Literal[
    "plan-refused",
    "git-not-installed",
    "not-a-git-repository",
    "dirty-worktree",
    "gh-not-installed",
    "gh-not-authenticated",
    "branch-exists",
    "no-remote",
]

PR_REFUSAL_EXPLANATION: dict[PullRequestRefusal, str] = {
    "plan-refused": "there is no change for a pull request to carry",
    "git-not-installed": (
        "git is what creates the branch and the commit, and it is not on PATH; "
        "install it from https://git-scm.com"
    ),
    "not-a-git-repository": (
        "this directory is not inside a git repository, so there is no branch to push"
    ),
    "dirty-worktree": (
        "the working tree has changes CTX Fit did not write, and they would be carried "
        "onto the new branch; commit or stash them first"
    ),
    "gh-not-installed": (
        "the GitHub CLI (`gh`) is what opens the pull request and it is not on PATH; "
        "install it from https://cli.github.com"
    ),
    "gh-not-authenticated": (
        "the GitHub CLI (`gh`) is installed but not logged in; run `gh auth login`"
    ),
    "branch-exists": (
        "that branch already exists, and CTX Fit will not commit onto a branch it did "
        "not create; delete it or re-run to get a new name"
    ),
    "no-remote": "there is no remote to push the branch to",
}


@dataclass(frozen=True, slots=True)
class PullRequestPlan:
    """The exact sequence that would open the pull request, or why it will not.

    Holding the commands as data rather than running them is what makes the
    announcement honest: the caller prints this, asks, and only then hands it
    back to :func:`open_pull_request`, which runs these and nothing else.
    """

    branch: str
    #: Paths written into the working tree before the first command runs.
    writes: tuple[str, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    #: The pull-request body, piped to `gh` on standard input. A temporary file
    #: inside the repository would dirty the tree this command is strict about.
    body: str = ""
    #: The branch the user was standing on, so a mid-sequence failure can say
    #: how to get back to it.
    original_branch: str = ""
    refusal: PullRequestRefusal | None = None
    refusal_detail: str = ""

    @property
    def can_open(self) -> bool:
        return self.refusal is None and bool(self.commands)

    @property
    def explanation(self) -> str:
        if self.refusal is None:
            return "ready to open"
        general = PR_REFUSAL_EXPLANATION[self.refusal]
        return f"{general} ({self.refusal_detail})" if self.refusal_detail else general

    @property
    def rendered_commands(self) -> tuple[str, ...]:
        """The commands as a user would type them, for the announcement."""

        return tuple(shlex.join(command) for command in self.commands)


@dataclass(frozen=True, slots=True)
class PullRequestResult:
    """What actually ran, and how far it got."""

    written: tuple[str, ...] = ()
    ran: tuple[tuple[str, ...], ...] = ()
    url: str = ""
    #: The command that failed, if one did. Everything after it never ran.
    failed: tuple[str, ...] | None = None
    detail: str = ""

    @property
    def opened(self) -> bool:
        return self.failed is None and bool(self.ran)


def _changed_paths(status_output: str) -> tuple[str, ...]:
    """Every path in ``git status --porcelain -z``, relative to the repo root.

    The NUL-separated form is used because the default one C-quotes any path
    with a space or a non-ASCII byte in it, and a path that fails to parse must
    not silently become a path that looks unchanged.
    """

    fields = [field for field in status_output.split("\0") if field]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:  # "XY " plus at least one character of path
            continue
        status, path = record[:2], record[3:]
        paths.append(path)
        if ("R" in status or "C" in status) and index < len(fields):
            # A rename or copy is reported as the new path followed by a second
            # record holding the old one. Both are uncommitted changes.
            paths.append(fields[index])
            index += 1
    return tuple(paths)


def _committed_content(path: str, *, repo_root: Path, run: CommandRunner) -> str:
    """The committed bytes of a repository path, or "" when HEAD has none.

    A path absent from HEAD — untracked, or a repository with no commits yet —
    is the empty file for this purpose: everything in the working tree is then
    new, and all of it has to be accounted for.
    """

    shown = run(("git", "show", f"HEAD:{path}"), cwd=repo_root)
    return shown.stdout if shown.ok else ""


def _worktree_content(target: Path) -> str:
    """What is in a file now, or "" if it cannot be read.

    A file that cannot be read cannot be shown to be CTX Fit's own work, and
    the safe answer to "is any of this the user's?" is yes — which is what the
    empty string produces, since no block can be found in it.
    """

    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _is_ctx_fit_output(worktree: str, committed: str) -> bool:
    """Is this working-tree file exactly what CTX Fit makes of the committed one?

    Answered by reproducing it: take the block CTX Fit's markers delimit in the
    working-tree file, splice it into the committed file with the same merge
    :func:`apply_plan` uses, and require the result byte for byte. That holds
    only when every byte outside CTX Fit's own block is still what was
    committed — which is the actual question the dirty-worktree gate asks.

    A file with no block, or one the user has edited around the block, fails
    here, and it must: `git add -- AGENTS.md` stages the whole file, so any
    other change in it would be committed and pushed inside a pull request
    whose title and body describe a configuration change and nothing else.
    """

    start = worktree.find(OWNED_BLOCK_START)
    end = worktree.find(OWNED_BLOCK_END, start + 1) if start != -1 else -1
    if start == -1 or end == -1:
        return False
    # The trailing newline is part of what `_owned_block` produced; the block
    # has to go back in exactly as it came out for the comparison to be exact.
    block = worktree[start : end + len(OWNED_BLOCK_END)] + "\n"
    return _merge_owned_block(committed, block) == worktree


def _unrelated_changes(
    status_output: str,
    repo_root: Path,
    plan_root: Path,
    plan: ApplyPlan,
    *,
    run: CommandRunner,
) -> tuple[str, ...]:
    """Uncommitted paths holding work CTX Fit did not write.

    A path this plan writes is exempt only when the bytes in it are CTX Fit's
    own output over the committed file. Exempting the path itself was the
    defect: a user with unrelated edits in AGENTS.md had them staged by
    `git add -- AGENTS.md`, committed under CTX Fit's title and pushed.

    Compared as absolute paths: `git status` reports relative to the repository
    root while an artifact path is relative to the directory the user named,
    and those coincide only when `ctx fit` was run from the top of the tree.
    """

    ours = {(plan_root / artifact.path).resolve() for artifact in plan.artifacts}
    unrelated: list[str] = []
    for path in _changed_paths(status_output):
        absolute = (repo_root / path).resolve()
        if absolute in ours and _is_ctx_fit_output(
            _worktree_content(absolute), _committed_content(path, repo_root=repo_root, run=run)
        ):
            continue
        unrelated.append(path)
    return tuple(unrelated)


def _dirty_detail(
    unrelated: Sequence[str], repo_root: Path, plan_root: Path, plan: ApplyPlan
) -> str:
    """Name the paths, and say when the work is inside a file CTX Fit writes.

    "uncommitted: AGENTS.md" on its own reads as a bug to anyone who has just
    run `--apply`: they know CTX Fit wrote that file. What stops the pull
    request is the rest of what is in it, so the message has to say so.
    """

    ours = {(plan_root / artifact.path).resolve() for artifact in plan.artifacts}
    labels = [
        f"{path} (which holds changes of your own as well)"
        if (repo_root / path).resolve() in ours
        else path
        for path in sorted(unrelated)
    ]
    return "uncommitted: " + ", ".join(labels)


def plan_pull_request(
    plan: ApplyPlan,
    repo_path: str | Path = ".",
    *,
    runner: CommandRunner | None = None,
    remote: str = DEFAULT_REMOTE,
) -> PullRequestPlan:
    """Check every gate, and return the commands that would open the PR.

    Nothing here changes anything. The probes it does run — ``git rev-parse``,
    ``git status``, ``git show``, ``git remote get-url``, ``gh auth status`` — are read-only,
    which is what lets every refusal below leave the repository byte for byte as
    it was found. The commands that do change something are returned rather than
    run, so the caller can print them and ask first.
    """

    run = runner if runner is not None else run_command
    root = Path(repo_path)

    def refuse(reason: PullRequestRefusal, detail: str = "") -> PullRequestPlan:
        return PullRequestPlan(branch=plan.branch, refusal=reason, refusal_detail=detail)

    if not plan.can_apply:
        # A pull request is a stronger claim than a file write, never a weaker
        # one: evidence too thin to write AGENTS.md cannot open a PR either.
        return refuse("plan-refused", plan.explanation)

    toplevel = run(("git", "rev-parse", "--show-toplevel"), cwd=root)
    if not toplevel.found:
        # Same distinction the `gh` gate makes: a binary that is absent and a
        # binary that ran and said no are different problems with different fixes.
        return refuse("git-not-installed")
    if not toplevel.ok:
        return refuse("not-a-git-repository", toplevel.message)
    repo_root = Path(toplevel.stdout.strip())

    status = run(("git", "status", "--porcelain", "-z", "--untracked-files=all"), cwd=root)
    if not status.ok:
        return refuse("not-a-git-repository", status.message)
    unrelated = _unrelated_changes(status.stdout, repo_root, root, plan, run=run)
    if unrelated:
        # `git checkout -b` carries uncommitted work onto the new branch, and
        # `git add -- AGENTS.md` stages a whole file, so this covers both the
        # user's other files and the user's other edits inside ours.
        return refuse("dirty-worktree", _dirty_detail(unrelated, repo_root, root, plan))

    auth = run(("gh", "auth", "status"), cwd=root)
    if not auth.found:
        return refuse("gh-not-installed")
    if not auth.ok:
        return refuse("gh-not-authenticated", auth.message)

    if run(("git", "rev-parse", "--verify", "--quiet", f"refs/heads/{plan.branch}"), cwd=root).ok:
        return refuse("branch-exists", plan.branch)
    if not run(("git", "remote", "get-url", remote), cwd=root).ok:
        # Checked here rather than discovered at the push, which would leave a
        # branch and a commit behind for a run that could never have finished.
        return refuse("no-remote", f"`git remote add {remote} <url>` first")

    head = run(("git", "rev-parse", "--abbrev-ref", "HEAD"), cwd=root)
    paths = tuple(artifact.path for artifact in plan.artifacts)
    return PullRequestPlan(
        branch=plan.branch,
        writes=paths,
        commands=(
            ("git", "checkout", "-b", plan.branch),
            ("git", "add", "--", *paths),
            ("git", "commit", "-m", plan.pr_title),
            ("git", "push", "--set-upstream", remote, plan.branch),
            ("gh", "pr", "create", "--title", plan.pr_title, "--body-file", "-"),
        ),
        body=plan.pr_body,
        original_branch=head.stdout.strip() if head.ok else "",
    )


def _pull_request_url(output: str) -> str:
    """The PR URL `gh` prints, ignoring whatever else it said."""

    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if candidate.startswith("https://"):
            return candidate
    return ""


def open_pull_request(
    plan: ApplyPlan,
    pull_request: PullRequestPlan,
    repo_path: str | Path,
    *,
    runner: CommandRunner | None = None,
) -> PullRequestResult:
    """Write the artifacts, then run the announced commands, in order.

    The files are written before the branch is created so that a failed write
    leaves the user exactly where ``--apply`` would have: on their own branch,
    with a modified file `git checkout` undoes. `git checkout -b` carries the
    change onto the new branch, so the resulting commit is the same either way.

    Stops at the first failure and reports it rather than unwinding: an unwind
    would be git commands the user was never shown, which is the whole defect
    this gate exists to prevent.
    """

    if not pull_request.can_open:
        raise ValueError(f"this pull request cannot be opened: {pull_request.explanation}")

    run = runner if runner is not None else run_command
    root = Path(repo_path)
    written = apply_plan(plan, root)

    ran: list[tuple[str, ...]] = []
    url = ""
    for command in pull_request.commands:
        # The body goes on stdin: it is markdown with newlines and backticks in
        # it, and no temporary file has to be created inside the repository.
        stdin = pull_request.body if tuple(command[:3]) == ("gh", "pr", "create") else None
        result = run(command, cwd=root, stdin=stdin)
        if not result.ok:
            return PullRequestResult(
                written=written,
                ran=tuple(ran),
                failed=tuple(command),
                detail=result.message or f"exit status {result.returncode}",
            )
        ran.append(tuple(command))
        url = _pull_request_url(result.stdout) or url
    return PullRequestResult(written=written, ran=tuple(ran), url=url)


__all__ = [
    "APPLY_SCHEMA",
    "BRANCH_PREFIX",
    "COMMAND_TIMEOUT_SECONDS",
    "DEFAULT_REMOTE",
    "OWNED_BLOCK_END",
    "OWNED_BLOCK_START",
    "PR_REFUSAL_EXPLANATION",
    "REFUSAL_EXPLANATION",
    "ApplyPlan",
    "ApplyRefusal",
    "Artifact",
    "CommandResult",
    "CommandRunner",
    "PullRequestPlan",
    "PullRequestRefusal",
    "PullRequestResult",
    "apply_plan",
    "open_pull_request",
    "plan_apply",
    "plan_pull_request",
    "run_command",
]
