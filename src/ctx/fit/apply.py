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
touched.** The exact winner is a CTX-owned, content-addressed sidecar; harness
instruction files such as ``AGENTS.md`` are never changed. A destination or
ancestor that is a symbolic link is refused rather than followed: writing
through it would edit a path the preview never named, possibly outside the
repository entirely, where git cannot show it and the PR's rollback advice
cannot undo it (FITBUG-010, FITBUG-011). The same principle gates the pull
request: uncommitted work CTX Fit did not write is never carried into a CTX Fit
branch or commit.

**Nothing is merged.** This opens a pull request. A human merges.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.recommend import Recommendation

APPLY_SCHEMA = "ctx.fit.apply-v1"
APPLIED_CONFIGURATION_SCHEMA = "ctx.fit.applied-configuration-v1"
CONFIGURATION_MANIFEST_PATH = ".ctx/fit-configuration.json"

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
    "winner-not-reproducible",
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
    "winner-not-reproducible": (
        "the winning candidate does not contain the exact configuration CTX Fit evaluated"
    ),
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
    ownership: Literal["owned-block", "whole-file"] = "whole-file"
    #: Bytes of pre-existing, user-authored content this artifact carries
    #: through unchanged, ignoring surrounding blank space and any block CTX
    #: Fit itself wrote on an earlier run. Zero on a create.
    preserved_bytes: int = 0
    #: Digest of the exact pre-preview bytes, or None when the path was absent.
    #: Applying a stale preview is refused instead of overwriting intervening
    #: user edits.
    expected_preimage_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        encoded = self.content.encode("utf-8")
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "bytes": len(encoded),
            "content_sha256": hashlib.sha256(encoded).hexdigest(),
            "content": self.content,
            "preserved_bytes": self.preserved_bytes,
            "ownership": self.ownership,
            "expected_preimage_sha256": self.expected_preimage_sha256,
        }


@dataclass(frozen=True, slots=True)
class RequiredInputPreimage:
    """One immutable repository input the preview and apply must share."""

    path: str
    content_sha256: str
    file_type: Literal["regular-file"] = "regular-file"
    allow_symlinks: Literal[False] = False

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "content_sha256": self.content_sha256,
            "file_type": self.file_type,
            "allow_symlinks": self.allow_symlinks,
        }


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """What would change, why, and whether it may be applied at all."""

    schema: str
    artifacts: tuple[Artifact, ...]
    branch: str
    pr_title: str
    pr_body: str
    required_input_preimages: tuple[RequiredInputPreimage, ...] = ()
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
            "required_input_preimages": [item.to_dict() for item in self.required_input_preimages],
            "can_apply": self.can_apply,
            "refusal": self.refusal,
            "refusal_detail": self.refusal_detail,
            "explanation": self.explanation,
        }


def _owned_block(body: str) -> str:
    """Wrap generated prose in the markers that say who owns it."""

    separator = "" if body.endswith("\n") else "\n"
    return f"{OWNED_BLOCK_START}\n{body}{separator}{OWNED_BLOCK_END}\n"


def _merge_owned_block(existing: str, block: str) -> str:
    """Splice CTX Fit's block into a file the user also writes in.

    A previous run's block is replaced in place, so repeated applies do not
    stack up copies. Anything else in the file — a hand-written AGENTS.md, the
    house rules, the on-call contact — is carried through byte for byte.
    Overwriting it wholesale destroyed uncommitted work that no rollback could
    recover, while the preview said only "modify: AGENTS.md" (FITBUG-011).
    """

    if not existing:
        return block

    start = existing.find(OWNED_BLOCK_START)
    end = existing.find(OWNED_BLOCK_END, start + 1) if start != -1 else -1
    if start != -1 and end != -1:
        head = existing[:start]
        tail_start = end + len(OWNED_BLOCK_END)
        # `_owned_block` owns one newline after its closing marker. Remove
        # exactly that byte when replacing the block; every later newline or
        # space belongs to the user and must survive byte for byte.
        if existing[tail_start : tail_start + 1] == "\n":
            tail_start += 1
        tail = existing[tail_start:]
        return f"{head}{block}{tail}"
    separator = "" if existing.endswith("\n\n") else "\n" if existing.endswith("\n") else "\n\n"
    return f"{existing}{separator}{block}"


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
        with target.open("r", encoding="utf-8", newline="") as handle:
            content = handle.read()
        return content, None
    except (OSError, UnicodeDecodeError) as exc:
        # Content that cannot be read cannot be preserved, and a file whose
        # contents are unknown must not be replaced.
        return "", f"{target.name} could not be read, so its contents cannot be preserved: {exc}"


def _unsafe_owned_markers(existing: str) -> str | None:
    """Reject any reserved-marker shape except zero blocks or one exact block."""

    starts = existing.count(OWNED_BLOCK_START)
    ends = existing.count(OWNED_BLOCK_END)
    if starts == ends == 0:
        return None
    start = existing.find(OWNED_BLOCK_START)
    end = existing.find(OWNED_BLOCK_END)
    if starts == ends == 1 and start < end:
        return None
    return (
        "AGENTS.md has unbalanced, nested, or multiple reserved CTX Fit block markers; "
        "CTX Fit cannot tell generated bytes from user-authored bytes"
    )


def _manifest_content(winner: CandidateConfiguration) -> str:
    payload = {
        "schema": APPLIED_CONFIGURATION_SCHEMA,
        "configuration_hash": winner.configuration_hash,
        "candidate": winner.to_dict(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _is_owned_manifest(existing: str) -> bool:
    try:
        payload = json.loads(existing)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("schema") == APPLIED_CONFIGURATION_SCHEMA
        and isinstance(payload.get("configuration_hash"), str)
        and isinstance(payload.get("candidate"), dict)
    )


def _preimage_digest(existing: str, *, exists: bool) -> str | None:
    if not exists:
        return None
    return hashlib.sha256(existing.encode("utf-8")).hexdigest()


def _symlinked_parent(root: Path, relative: Path) -> Path | None:
    """First symlink between *root* and a destination's parent, if any."""

    current = root
    for part in relative.parent.parts:
        current = current / part
        if current.is_symlink():
            return current
        if current.exists() and not current.is_dir():
            break
    return None


def _instruction_preimage_error(root: Path, winner: CandidateConfiguration) -> str:
    """Why the repository no longer matches evaluated instruction bytes."""

    for material in winner.instruction_materials:
        relative = Path(material.path)
        if symlink := _symlinked_parent(root, relative):
            return f"{material.path} now traverses the symbolic link {symlink.name}"
        target = root / relative
        if not target.is_file() or target.is_symlink():
            return f"{material.path} is no longer the evaluated regular file"
        content, unsafe = _read_destination(target)
        if unsafe is not None:
            return f"{material.path} cannot be verified: {unsafe}"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != material.content_sha256:
            return f"{material.path} changed after the winning configuration was evaluated"
    return ""


def _required_input_error(root: Path, required: RequiredInputPreimage) -> str:
    """Why an approved required input no longer has its previewed bytes."""

    relative = Path(required.path)
    if relative.is_absolute() or ".." in relative.parts:
        return f"{required.path} is not a safe repository-relative input path"
    if symlink := _symlinked_parent(root, relative):
        return f"{required.path} now traverses the symbolic link {symlink.name}"
    target = root / relative
    if target.is_symlink() or not target.is_file():
        return f"{required.path} is no longer the previewed regular file"
    content, unsafe = _read_destination(target)
    if unsafe is not None:
        return f"{required.path} cannot be verified: {unsafe}"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if digest != required.content_sha256:
        return f"{required.path} changed after the preview was created"
    return ""


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
        f"- Model: `{winner.model}`",
        "- Repository instruction material: "
        + (
            ", ".join(
                f"`{item.path}` (`{item.content_sha256}`)" for item in winner.instruction_materials
            )
            or "none"
        ),
        f"- Configuration hash: `{winner.configuration_hash}`",
        "- Capability material: "
        + (
            ", ".join(
                f"`{item.capability_id}` (`{item.content_sha256}`)"
                for item in winner.capability_materials
            )
            or "none"
        ),
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
    if error := winner.reproducibility_error:
        return refuse("winner-not-reproducible", error)
    root = Path(repo_path)
    if error := _instruction_preimage_error(root, winner):
        return refuse("winner-not-reproducible", error)

    manifest_target = root / CONFIGURATION_MANIFEST_PATH
    if symlink := _symlinked_parent(root, Path(CONFIGURATION_MANIFEST_PATH)):
        return refuse(
            "unsafe-destination",
            f"{CONFIGURATION_MANIFEST_PATH} has the symbolic-link parent {symlink.name}",
        )
    manifest_existing, unsafe = _read_destination(manifest_target)
    if unsafe is not None:
        return refuse("unsafe-destination", f"{CONFIGURATION_MANIFEST_PATH}: {unsafe}")
    if manifest_target.exists() and not _is_owned_manifest(manifest_existing):
        return refuse(
            "unsafe-destination",
            f"{CONFIGURATION_MANIFEST_PATH} exists but is not a CTX Fit "
            "applied-configuration manifest",
        )

    artifacts = (
        Artifact(
            path=CONFIGURATION_MANIFEST_PATH,
            content=_manifest_content(winner),
            action="modify" if manifest_target.is_file() else "create",
            reason="machine-readable content-addressed winning configuration for a harness",
            ownership="whole-file",
            expected_preimage_sha256=_preimage_digest(
                manifest_existing, exists=manifest_target.exists()
            ),
        ),
    )

    return ApplyPlan(
        schema=APPLY_SCHEMA,
        artifacts=artifacts,
        branch=branch,
        pr_title=title,
        pr_body=_pr_body(winner, recommendation),
        required_input_preimages=tuple(
            RequiredInputPreimage(path=item.path, content_sha256=item.content_sha256)
            for item in winner.instruction_materials
        ),
    )


@dataclass(frozen=True, slots=True)
class _StagedWrite:
    artifact: Artifact
    destination: Path
    scratch: Path
    backup: Path | None


def _temporary_sibling(destination: Path, suffix: str) -> tuple[int, Path]:
    file_descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.ctx-fit-",
        suffix=suffix,
        dir=destination.parent,
    )
    return file_descriptor, Path(name)


def _stage_write(artifact: Artifact, destination: Path) -> _StagedWrite:
    """Prepare replacement and rollback bytes without changing the destination."""

    file_descriptor, scratch = _temporary_sibling(destination, ".tmp")
    backup: Path | None = None
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(artifact.content)
        if destination.exists():
            os.chmod(scratch, stat.S_IMODE(destination.stat().st_mode))
            backup_descriptor, backup = _temporary_sibling(destination, ".bak")
            os.close(backup_descriptor)
            shutil.copy2(destination, backup)
        else:
            # The applied manifest can contain organization-owned instructions
            # and capability material. Keep a newly created file private; Git
            # can still review its bytes without granting other local users
            # access through the working tree.
            os.chmod(scratch, 0o600)
        return _StagedWrite(artifact, destination, scratch, backup)
    except BaseException:
        scratch.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)
        raise


def _commit_staged_write(staged: Path, destination: Path) -> None:
    """Single replacement seam, injectable in the transaction failure test."""

    os.replace(staged, destination)


def _rollback_committed_write(staged: _StagedWrite) -> None:
    if staged.backup is None:
        staged.destination.unlink(missing_ok=True)
    else:
        os.replace(staged.backup, staged.destination)


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
    for required in plan.required_input_preimages:
        if error := _required_input_error(root, required):
            raise ValueError(f"refusing stale required input: {error}")
    destinations: list[tuple[Artifact, Path]] = []
    for artifact in plan.artifacts:
        relative = Path(artifact.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"refusing to write outside the repository: {artifact.path}")

        destination = root / relative
        if destination.is_symlink():
            raise ValueError(
                f"refusing to write through the symbolic link {artifact.path}: it would "
                f"change {os.readlink(destination)}, which this plan never named"
            )
        if symlink := _symlinked_parent(root, relative):
            raise ValueError(
                f"refusing to write {artifact.path}: its parent {symlink.name} is a symbolic link"
            )
        if not destination.parent.resolve().is_relative_to(root_resolved):
            raise ValueError(f"refusing to write outside the repository: {artifact.path}")

        current, unsafe = _read_destination(destination)
        if unsafe is not None:
            raise ValueError(f"refusing unsafe destination {artifact.path}: {unsafe}")
        current_digest = _preimage_digest(current, exists=destination.exists())
        if current_digest != artifact.expected_preimage_sha256:
            raise ValueError(
                f"refusing to write {artifact.path}: it changed after the preview was created"
            )
        destinations.append((artifact, destination))

    created_parents: list[Path] = []
    staged_writes: list[_StagedWrite] = []
    committed: list[_StagedWrite] = []
    try:
        for artifact, destination in destinations:
            if not destination.parent.exists():
                destination.parent.mkdir(parents=True)
                created_parents.append(destination.parent)
            if symlink := _symlinked_parent(root, Path(artifact.path)):
                raise ValueError(
                    f"refusing to write {artifact.path}: its parent {symlink.name} is a "
                    "symbolic link"
                )
            staged_writes.append(_stage_write(artifact, destination))

        for staged in staged_writes:
            _commit_staged_write(staged.scratch, staged.destination)
            committed.append(staged)
    except BaseException:
        for staged in reversed(committed):
            _rollback_committed_write(staged)
        raise
    finally:
        for staged in staged_writes:
            staged.scratch.unlink(missing_ok=True)
            if staged.backup is not None:
                staged.backup.unlink(missing_ok=True)
        for parent in reversed(created_parents):
            try:
                parent.rmdir()
            except OSError:
                pass

    return tuple(artifact.path for artifact, _destination in destinations)


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

    ours = {(plan_root / artifact.path).resolve(): artifact for artifact in plan.artifacts}
    unrelated: list[str] = []
    for path in _changed_paths(status_output):
        absolute = (repo_root / path).resolve()
        artifact = ours.get(absolute)
        if artifact is not None:
            worktree = _worktree_content(absolute)
            if artifact.ownership == "whole-file" and worktree == artifact.content:
                continue
            if artifact.ownership == "owned-block" and _is_ctx_fit_output(
                worktree, _committed_content(path, repo_root=repo_root, run=run)
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
    "APPLIED_CONFIGURATION_SCHEMA",
    "APPLY_SCHEMA",
    "BRANCH_PREFIX",
    "COMMAND_TIMEOUT_SECONDS",
    "CONFIGURATION_MANIFEST_PATH",
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
    "RequiredInputPreimage",
    "apply_plan",
    "open_pull_request",
    "plan_apply",
    "plan_pull_request",
    "run_command",
]
