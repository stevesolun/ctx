from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import ctx.fit.apply as apply_module
from ctx.fit.apply import (
    APPLIED_CONFIGURATION_SCHEMA,
    APPLY_SCHEMA,
    CONFIGURATION_MANIFEST_PATH,
    OWNED_BLOCK_END,
    OWNED_BLOCK_START,
    ApplyPlan,
    Artifact,
    CommandResult,
    apply_plan,
    open_pull_request,
    plan_apply,
    plan_pull_request,
    run_command,
)
from ctx.fit.candidates import CapabilityMaterial, CandidateConfiguration, InstructionMaterial
from ctx.fit.recommend import RankedCandidate, Recommendation


def _candidate(name: str = "lean") -> CandidateConfiguration:
    content = (
        "---\n"
        "name: ctx-python-testing\n"
        "description: Exact evaluated testing guidance.\n"
        "---\n\n"
        "# ctx Python Testing\n\n"
        "Run the narrowest deterministic regression first.\n"
    )
    return CandidateConfiguration(
        candidate_id=name,
        role="recommended",
        capability_ids=("skill:ctx-python-testing",),
        model="gpt-4o-mini",
        instructions=(),
        selection_reason="the single highest-ranked capability, to test whether less is enough",
        capability_materials=(
            CapabilityMaterial.from_content(
                capability_id="skill:ctx-python-testing",
                delivery_mode="task-user-context",
                source_identity=(
                    "package:ctx.assets/runtime-availability.json#skill:ctx-python-testing"
                ),
                catalog_entry_digest=hashlib.sha256(b"skill:ctx-python-testing").hexdigest(),
                content=content,
            ),
        ),
    )


def _recommendation(
    *,
    verdict: str = "recommend-change",
    winner: str | None = "lean",
    simulated: bool = False,
) -> Recommendation:
    return Recommendation(
        schema="ctx.fit.recommendation-v1",
        verdict=verdict,  # type: ignore[arg-type]
        winner_id=winner,
        ranked=(
            RankedCandidate(
                candidate_id="lean",
                reliability=1.0,
                verified=9,
                scored=9,
                total_cost_usd=0.45,
                capability_count=1,
                qualified=True,
            ),
            RankedCandidate(
                candidate_id="baseline",
                reliability=0.75,
                verified=3,
                scored=4,
                total_cost_usd=0.20,
                capability_count=0,
                qualified=False,
                exclusion_reason="verified 3/4 trials",
            ),
        ),
        reasoning=("lean verified 9/9 trials at $0.45.",),
        limitations=("only 3 tasks were evaluated.",),
        confidence="medium",
        simulated=simulated,
    )


# --------------------------------------------------------------------------
# Refusals: evidence that cannot support a change must not produce one.
# --------------------------------------------------------------------------


def test_simulated_evidence_is_refused(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(simulated=True), (_candidate(),), repo_path=tmp_path)

    assert plan.can_apply is False
    assert plan.refusal == "simulated-evidence"
    assert plan.artifacts == ()
    assert "nothing about this repository" in plan.explanation


def test_no_verdict_is_refused(tmp_path: Path) -> None:
    plan = plan_apply(
        _recommendation(verdict="no-verdict", winner=None), (_candidate(),), repo_path=tmp_path
    )

    assert plan.refusal == "no-verdict"
    assert plan.can_apply is False


def test_keep_current_changes_nothing(tmp_path: Path) -> None:
    """Winning by already being best means the right action is to do nothing."""

    plan = plan_apply(
        _recommendation(verdict="keep-current", winner="baseline"),
        (_candidate("baseline"),),
        repo_path=tmp_path,
    )

    assert plan.refusal == "keep-current"
    assert plan.artifacts == ()


def test_missing_winner_is_refused(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(winner="ghost"), (_candidate(),), repo_path=tmp_path)

    assert plan.refusal == "winner-not-found"


def test_refused_plan_cannot_be_applied_even_if_forced(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(simulated=True), (_candidate(),), repo_path=tmp_path)

    with pytest.raises(ValueError, match="cannot be applied"):
        apply_plan(plan, tmp_path)

    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# Nothing is written until explicitly applied.
# --------------------------------------------------------------------------


def test_planning_writes_nothing(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    assert plan.can_apply is True
    assert plan.artifacts
    assert list(tmp_path.iterdir()) == []  # preview only


def test_applying_writes_the_previewed_content_exactly(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    written = apply_plan(plan, tmp_path)

    assert written == (CONFIGURATION_MANIFEST_PATH,)
    assert not (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / CONFIGURATION_MANIFEST_PATH).read_text(encoding="utf-8") == plan.artifacts[
        0
    ].content


def test_existing_owned_manifest_is_reported_as_a_modification(tmp_path: Path) -> None:
    first = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)
    apply_plan(first, tmp_path)

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    assert plan.artifacts[0].action == "modify"


# --------------------------------------------------------------------------
# The PR is evidence, not marketing.
# --------------------------------------------------------------------------


def test_pr_body_contains_the_comparison_and_limitations(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    body = plan.pr_body
    assert "| Candidate | Verified | Cost | Qualified |" in body
    assert "verified 3/4 trials" in body  # why baseline was excluded
    assert "### Limitations" in body
    assert "only 3 tasks were evaluated." in body
    assert "Rollback" in body
    assert "never merges" in body


def test_pr_body_states_confidence(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    assert "**Confidence:** medium" in plan.pr_body


def test_branch_is_namespaced(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path, run_id="2026-08-09")

    assert plan.branch == "ctx-fit/2026-08-09"


def test_apply_writes_no_harness_instruction_file(
    tmp_path: Path,
) -> None:
    """Post-run evidence belongs in the PR, not in future agents' instructions."""

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    assert [artifact.path for artifact in plan.artifacts] == [CONFIGURATION_MANIFEST_PATH]
    assert not (tmp_path / "AGENTS.md").exists()
    assert "verified 9/9 trials" in plan.pr_body
    assert "**Confidence:** medium" in plan.pr_body
    assert "only 3 tasks were evaluated" in plan.pr_body


def test_apply_materializes_the_exact_evaluated_configuration(tmp_path: Path) -> None:
    """The artifact must be usable without resolving an ID from a later catalog."""

    winner = _candidate()
    material = winner.capability_materials[0]

    plan = plan_apply(_recommendation(), (winner,), repo_path=tmp_path)

    manifest = json.loads(plan.artifacts[0].content)
    applied = manifest["candidate"]["capability_materials"][0]
    assert applied == material.to_dict()
    assert manifest["configuration_hash"] == winner.configuration_hash


def test_apply_emits_a_canonical_machine_readable_configuration_manifest(
    tmp_path: Path,
) -> None:
    winner = _candidate()

    first = plan_apply(_recommendation(), (winner,), repo_path=tmp_path)
    second = plan_apply(_recommendation(), (winner,), repo_path=tmp_path)
    manifest = next(
        artifact for artifact in first.artifacts if artifact.path == CONFIGURATION_MANIFEST_PATH
    )
    payload = json.loads(manifest.content)

    assert payload == {
        "candidate": winner.to_dict(),
        "configuration_hash": winner.configuration_hash,
        "schema": APPLIED_CONFIGURATION_SCHEMA,
    }
    assert (
        manifest.content == json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    assert (
        next(
            artifact.content
            for artifact in second.artifacts
            if artifact.path == CONFIGURATION_MANIFEST_PATH
        )
        == manifest.content
    )
    assert manifest.ownership == "whole-file"
    assert len(first.artifacts) == 1


def test_apply_does_not_normalize_the_evaluated_material_bytes(tmp_path: Path) -> None:
    body = "# Exact body\n\nKeep these trailing spaces.  \n\n"
    original = _candidate().capability_materials[0]
    material = CapabilityMaterial.from_content(
        capability_id=original.capability_id,
        delivery_mode=original.delivery_mode,
        source_identity=original.source_identity,
        catalog_entry_digest=original.catalog_entry_digest,
        content=body,
    )
    winner = replace(_candidate(), capability_materials=(material,))

    plan = plan_apply(_recommendation(), (winner,), repo_path=tmp_path)

    payload = json.loads(plan.artifacts[0].content)
    assert payload["candidate"]["capability_materials"][0]["content"] == body


def test_machine_manifest_json_escapes_marker_like_metadata(tmp_path: Path) -> None:
    winner = replace(_candidate(), model=f"gpt-4o-mini {OWNED_BLOCK_END}")

    plan = plan_apply(_recommendation(), (winner,), repo_path=tmp_path)

    assert plan.can_apply is True
    assert json.loads(plan.artifacts[0].content)["candidate"]["model"] == winner.model


def test_apply_refuses_a_winner_that_cannot_be_reproduced(
    tmp_path: Path,
) -> None:
    winner = replace(_candidate(), model=None)

    plan = plan_apply(_recommendation(), (winner,), repo_path=tmp_path)

    assert plan.can_apply is False
    assert plan.refusal == "winner-not-reproducible"
    assert "mutable provider default" in plan.explanation
    assert plan.artifacts == ()


# --------------------------------------------------------------------------
# FITBUG-010: the write lands where the preview said, or it does not happen.
# --------------------------------------------------------------------------


def _link(link: Path, target: Path) -> None:
    """Create a symlink, or skip: unprivileged Windows cannot make one."""

    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"symlinks unavailable in this environment: {exc}")


def _shared_file_outside(tmp_path: Path) -> tuple[Path, Path]:
    """An empty repository, and a file that belongs to someone else."""

    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    shared = outside / "ORG.md"
    shared.write_text(
        "# org-wide file, not in this repository\ndo not delete me\n", encoding="utf-8"
    )
    return repo, shared


def test_a_symlinked_agents_md_is_untouched(tmp_path: Path) -> None:
    """Apply does not inspect or write a harness instruction destination."""

    repo, shared = _shared_file_outside(tmp_path)
    _link(repo / "AGENTS.md", shared)
    before = shared.read_text(encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo)

    assert plan.can_apply is True
    apply_plan(plan, repo)
    assert shared.read_text(encoding="utf-8") == before


def test_a_symlinked_machine_manifest_is_refused_instead_of_followed(tmp_path: Path) -> None:
    repo, shared = _shared_file_outside(tmp_path)
    manifest = repo / CONFIGURATION_MANIFEST_PATH
    manifest.parent.mkdir(parents=True)
    _link(manifest, shared)
    before = shared.read_text(encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo)

    assert plan.refusal == "unsafe-destination"
    assert plan.artifacts == ()
    assert CONFIGURATION_MANIFEST_PATH in plan.explanation
    assert shared.read_text(encoding="utf-8") == before


def test_a_symlinked_manifest_parent_is_refused_before_reading_outside(
    tmp_path: Path,
) -> None:
    repo, shared = _shared_file_outside(tmp_path)
    _link(repo / ".ctx", shared.parent)
    before = shared.read_text(encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo)

    assert plan.refusal == "unsafe-destination"
    assert "symbolic-link parent .ctx" in plan.explanation
    assert shared.read_text(encoding="utf-8") == before


def test_a_symlinked_manifest_parent_inside_the_repository_is_refused(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    user_owned = repo / "user-owned"
    user_owned.mkdir()
    _link(repo / ".ctx", user_owned)

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo)

    assert plan.refusal == "unsafe-destination"
    assert "symbolic-link parent .ctx" in plan.explanation
    assert not (user_owned / "fit-configuration.json").exists()


def test_an_agents_symlink_that_appears_after_preview_is_still_untouched(
    tmp_path: Path,
) -> None:
    """A plan is previewed, then applied: the destination can change in between."""

    repo, shared = _shared_file_outside(tmp_path)
    before = shared.read_text(encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo)
    assert plan.can_apply is True
    _link(repo / "AGENTS.md", shared)

    apply_plan(plan, repo)

    assert shared.read_text(encoding="utf-8") == before


def test_an_agents_edit_after_preview_is_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text("# Initial rules\n", encoding="utf-8")
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)
    late = "# Initial rules\n\nDo not overwrite this late edit.\n"
    target.write_text(late, encoding="utf-8")

    apply_plan(plan, tmp_path)

    assert target.read_text(encoding="utf-8") == late
    assert (tmp_path / CONFIGURATION_MANIFEST_PATH).exists()


def test_a_manifest_edit_after_preview_stops_all_artifact_writes(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)
    manifest = tmp_path / CONFIGURATION_MANIFEST_PATH
    manifest.parent.mkdir(parents=True)
    late = '{"belongs_to":"the user now"}\n'
    manifest.write_text(late, encoding="utf-8")

    with pytest.raises(ValueError, match="changed after the preview"):
        apply_plan(plan, tmp_path)

    assert not (tmp_path / "AGENTS.md").exists()
    assert manifest.read_text(encoding="utf-8") == late


def test_apply_refuses_when_evaluated_instruction_bytes_have_changed(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    evaluated = "# Evaluated rules\n"
    agents.write_text(evaluated, encoding="utf-8")
    winner = replace(
        _candidate(),
        instructions=("AGENTS.md",),
        instruction_materials=(
            InstructionMaterial.from_content(path="AGENTS.md", content=evaluated),
        ),
    )
    agents.write_text("# Changed rules\n", encoding="utf-8")

    plan = plan_apply(_recommendation(), (winner,), repo_path=tmp_path)

    assert plan.refusal == "winner-not-reproducible"
    assert "changed after the winning configuration was evaluated" in plan.explanation
    assert plan.artifacts == ()


def test_apply_rechecks_instruction_preimages_after_the_preview(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    evaluated = "# Evaluated rules\n"
    agents.write_text(evaluated, encoding="utf-8")
    winner = replace(
        _candidate(),
        instructions=("AGENTS.md",),
        instruction_materials=(
            InstructionMaterial.from_content(path="AGENTS.md", content=evaluated),
        ),
    )
    plan = plan_apply(_recommendation(), (winner,), repo_path=tmp_path)
    assert plan.can_apply is True
    assert plan.required_input_preimages[0].to_dict() == {
        "path": "AGENTS.md",
        "content_sha256": hashlib.sha256(evaluated.encode("utf-8")).hexdigest(),
        "file_type": "regular-file",
        "allow_symlinks": False,
    }
    agents.write_text("# Changed after preview\n", encoding="utf-8")

    with pytest.raises(ValueError, match="stale required input.*changed after the preview"):
        apply_plan(plan, tmp_path)

    assert not (tmp_path / CONFIGURATION_MANIFEST_PATH).exists()


def test_a_second_artifact_write_failure_rolls_back_the_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.json"
    first.write_bytes(b"original bytes\r\n")
    original = first.read_bytes()
    plan = ApplyPlan(
        schema=APPLY_SCHEMA,
        artifacts=(
            Artifact(
                path="first.json",
                content="replacement\n",
                action="modify",
                reason="transaction test",
                expected_preimage_sha256=hashlib.sha256(original).hexdigest(),
            ),
            Artifact(path="second.json", content="second\n", action="create", reason="test"),
        ),
        branch="ctx-fit/run",
        pr_title="test",
        pr_body="test",
    )
    real_commit = apply_module._commit_staged_write

    def fail_manifest(staged: Path, destination: Path) -> None:
        if destination == tmp_path / "second.json":
            raise OSError("simulated second replacement failure")
        real_commit(staged, destination)

    monkeypatch.setattr(apply_module, "_commit_staged_write", fail_manifest)

    with pytest.raises(OSError, match="second replacement failure"):
        apply_plan(plan, tmp_path)

    assert first.read_bytes() == original
    assert not (tmp_path / "second.json").exists()


def test_a_path_that_escapes_the_repository_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = ApplyPlan(
        schema=APPLY_SCHEMA,
        artifacts=(Artifact(path="../escaped.md", content="x", action="create", reason="r"),),
        branch="ctx-fit/run",
        pr_title="t",
        pr_body="b",
    )

    with pytest.raises(ValueError, match="outside the repository"):
        apply_plan(plan, repo)

    assert not (tmp_path / "escaped.md").exists()


# --------------------------------------------------------------------------
# FITBUG-011: what the user wrote survives what CTX Fit writes.
# --------------------------------------------------------------------------

_HAND_WRITTEN = "# My instructions\nnever touch migrations/\ncontact: platform@example.com\n"


def test_hand_written_content_survives_an_apply(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(_HAND_WRITTEN, encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)
    apply_plan(plan, tmp_path)
    after = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    assert after == _HAND_WRITTEN
    assert plan.artifacts[0].path == CONFIGURATION_MANIFEST_PATH


def test_every_existing_byte_survives_when_the_block_is_first_added(tmp_path: Path) -> None:
    hand_written = "# My instructions\n\nKeep the final whitespace.  \n\n"
    (tmp_path / "AGENTS.md").write_text(hand_written, encoding="utf-8")

    apply_plan(plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path), tmp_path)

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").startswith(hand_written)


def test_a_prior_owned_block_is_left_untouched(tmp_path: Path) -> None:
    head = "# House rules\n\n"
    tail = "\n\nKeep both blank lines and these spaces.  \n"
    previous = f"{head}{OWNED_BLOCK_START}\n# old generated content\n{OWNED_BLOCK_END}{tail}"
    (tmp_path / "AGENTS.md").write_text(previous, encoding="utf-8")

    apply_plan(plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path), tmp_path)

    after = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert after == previous


@pytest.mark.parametrize(
    "original",
    [
        f"# Rules\n\n{OWNED_BLOCK_START}\nThis belongs to the user.\n",
        f"{OWNED_BLOCK_START}\nouter\n{OWNED_BLOCK_START}\ninner\n{OWNED_BLOCK_END}\n",
        f"{OWNED_BLOCK_START}\none\n{OWNED_BLOCK_END}\n{OWNED_BLOCK_START}\ntwo\n{OWNED_BLOCK_END}\n",
        f"{OWNED_BLOCK_END}\nuser text\n{OWNED_BLOCK_START}\n",
    ],
    ids=["unbalanced", "nested", "multiple", "reversed"],
)
def test_ambiguous_legacy_reserved_markers_are_left_untouched(
    tmp_path: Path,
    original: str,
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_text(original, encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    assert plan.can_apply is True
    apply_plan(plan, tmp_path)
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "original",
    [
        b"# Windows rules\r\n\r\nKeep CRLF and spaces.  \r\n",
        b"# Mixed\r\nLF line\nCR line\rTrailing spaces.  \r\n",
    ],
    ids=["crlf", "mixed-newlines"],
)
def test_user_newline_bytes_survive_the_owned_block_splice(
    tmp_path: Path,
    original: bytes,
) -> None:
    target = tmp_path / "AGENTS.md"
    target.write_bytes(original)

    apply_plan(plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path), tmp_path)

    assert target.read_bytes().startswith(original)


def test_an_unowned_file_at_the_manifest_path_is_refused(tmp_path: Path) -> None:
    target = tmp_path / CONFIGURATION_MANIFEST_PATH
    target.parent.mkdir(parents=True)
    original = '{"belongs_to":"the user"}\n'
    target.write_text(original, encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    assert plan.refusal == "unsafe-destination"
    assert "not a CTX Fit applied-configuration manifest" in plan.explanation
    assert target.read_text(encoding="utf-8") == original


def test_applying_twice_never_adds_an_instruction_block(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text(_HAND_WRITTEN, encoding="utf-8")

    plans = []
    for _ in range(2):
        plans.append(plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path))
        apply_plan(plans[-1], tmp_path)
    after = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    assert after.count(OWNED_BLOCK_START) == 0
    assert after.count("never touch migrations/") == 1


# --------------------------------------------------------------------------
# FITBUG-046: a refusal states the reason that actually fired.
# --------------------------------------------------------------------------


def test_the_no_verdict_refusal_reports_the_cause_the_recommendation_found(
    tmp_path: Path,
) -> None:
    """Blaming reliability under a table of 3/3 candidates says the opposite of the evidence."""

    recommendation = Recommendation(
        schema="ctx.fit.recommendation-v1",
        verdict="no-verdict",
        winner_id=None,
        ranked=(),
        reasoning=(
            "2 candidate(s) worked reliably, but part of their spend was never measured, "
            "so none of them can be called cheapest.",
        ),
        limitations=(),
        confidence="low",
    )

    plan = plan_apply(recommendation, (_candidate(),), repo_path=tmp_path)

    assert plan.refusal == "no-verdict"
    assert "spend was never measured" in plan.explanation
    assert "reliability floor" not in plan.explanation


def test_plan_is_serializable_and_versioned(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)
    payload = plan.to_dict()

    assert json.loads(json.dumps(payload, sort_keys=True))["schema"] == APPLY_SCHEMA
    assert payload["can_apply"] is True
    artifact = payload["artifacts"][0]  # type: ignore[index]
    assert artifact["content"] == plan.artifacts[0].content  # type: ignore[index]
    assert (
        artifact["content_sha256"]
        == hashlib.sha256(  # type: ignore[index]
            plan.artifacts[0].content.encode("utf-8")
        ).hexdigest()
    )


# --------------------------------------------------------------------------
# FITBUG-036: --pr opens the pull request, and every gate on it holds.
#
# The command runner is injected throughout, so no test here needs a `gh`
# binary, a GitHub account or a network. The one end-to-end case runs git for
# real against a bare remote on local disk and fakes `gh` alone.
# --------------------------------------------------------------------------

#: Probes `plan_pull_request` is allowed to run: they read and change nothing.
_READ_ONLY = (
    "git rev-parse",
    "git status",
    "git show",
    "git remote get-url",
    "gh auth status",
)


def _mutating(calls: Sequence[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    """The calls that could change something — everything but the probes."""

    return tuple(call for call in calls if not shlex.join(call).startswith(_READ_ONLY))


class _Runner:
    """A command runner that records every call and answers from a script.

    A key is matched against the rendered command, so ``"gh auth"`` answers
    ``gh auth status``. An unscripted command succeeds silently unless
    ``real_git`` is set, in which case git — and only git — actually runs.
    """

    def __init__(
        self, replies: dict[str, CommandResult] | None = None, *, real_git: bool = False
    ) -> None:
        self.replies = dict(replies or {})
        self.real_git = real_git
        self.calls: list[tuple[str, ...]] = []
        self.stdin: dict[tuple[str, ...], str | None] = {}

    def __call__(
        self, command: Sequence[str], *, cwd: Path, stdin: str | None = None
    ) -> CommandResult:
        command = tuple(command)
        self.calls.append(command)
        self.stdin[command] = stdin
        rendered = shlex.join(command)
        for prefix, reply in self.replies.items():
            if rendered.startswith(prefix):
                return reply
        if self.real_git and command[0] == "git":
            return run_command(command, cwd=cwd, stdin=stdin)
        return CommandResult(0)


def _healthy(root: Path, overrides: dict[str, CommandResult] | None = None) -> _Runner:
    """A repository where every gate passes, unless a test spoils one.

    An override replaces a default by exact key, so use the key as written.
    """

    replies = {
        "git rev-parse --show-toplevel": CommandResult(0, f"{root}\n"),
        "git status": CommandResult(0, ""),  # nothing uncommitted
        "gh auth status": CommandResult(0, "Logged in to github.com account octocat"),
        "git rev-parse --verify": CommandResult(1),  # the branch does not exist yet
        "git remote get-url": CommandResult(0, "git@github.com:octocat/repo.git\n"),
        "git rev-parse --abbrev-ref": CommandResult(0, "main\n"),
        "gh pr create": CommandResult(0, "https://github.com/octocat/repo/pull/7\n"),
    }
    replies.update(overrides or {})
    return _Runner(replies)


_TITLE = "Optimize AI coding configuration using CTX Fit"


def test_the_announced_commands_are_exactly_the_commands_that_run(tmp_path: Path) -> None:
    """--pr used to print a branch name it never created. It now creates it."""

    runner = _healthy(tmp_path)
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path, run_id="2026-08-13")

    pull_request = plan_pull_request(plan, tmp_path, runner=runner)

    assert pull_request.can_open is True
    assert pull_request.commands == (
        ("git", "checkout", "-b", "ctx-fit/2026-08-13"),
        ("git", "add", "--", CONFIGURATION_MANIFEST_PATH),
        ("git", "commit", "-m", _TITLE),
        ("git", "push", "--set-upstream", "origin", "ctx-fit/2026-08-13"),
        ("gh", "pr", "create", "--title", _TITLE, "--body-file", "-"),
    )
    # Planning is allowed to probe, never to change: the announcement has to be
    # complete before anything happens.
    assert _mutating(runner.calls) == ()
    assert not (tmp_path / CONFIGURATION_MANIFEST_PATH).exists()

    result = open_pull_request(plan, pull_request, tmp_path, runner=runner)

    assert result.opened is True
    assert _mutating(runner.calls) == pull_request.commands
    assert result.url == "https://github.com/octocat/repo/pull/7"
    assert runner.stdin[pull_request.commands[-1]] == plan.pr_body
    assert (tmp_path / CONFIGURATION_MANIFEST_PATH).read_text(encoding="utf-8") == plan.artifacts[
        0
    ].content


def test_unrelated_uncommitted_work_stops_the_pull_request_before_anything_runs(
    tmp_path: Path,
) -> None:
    """`git checkout -b` would carry the user's own work onto a CTX Fit branch."""

    runner = _healthy(tmp_path, {"git status": CommandResult(0, " M src/app.py\0?? notes.txt\0")})
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    pull_request = plan_pull_request(plan, tmp_path, runner=runner)

    assert pull_request.refusal == "dirty-worktree"
    assert pull_request.can_open is False
    # A file that is simply the user's is named and left at that.
    assert "uncommitted: notes.txt, src/app.py" in pull_request.explanation
    assert "changes of your own as well" not in pull_request.explanation
    assert _mutating(runner.calls) == ()
    assert not (tmp_path / "AGENTS.md").exists()


def test_a_missing_gh_and_a_logged_out_gh_are_told_apart(tmp_path: Path) -> None:
    """The unauthenticated case is the common one, and the message is the whole UX."""

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    missing = plan_pull_request(
        plan,
        tmp_path,
        runner=_healthy(tmp_path, {"gh auth status": CommandResult(127, found=False)}),
    )
    logged_out = plan_pull_request(
        plan,
        tmp_path,
        runner=_healthy(
            tmp_path,
            {
                "gh auth status": CommandResult(
                    1, stderr="You are not logged into any GitHub hosts."
                )
            },
        ),
    )

    assert missing.refusal == "gh-not-installed"
    assert "cli.github.com" in missing.explanation
    assert logged_out.refusal == "gh-not-authenticated"
    assert "gh auth login" in logged_out.explanation
    assert "not logged into any GitHub hosts" in logged_out.explanation


@pytest.mark.parametrize(
    ("recommendation", "candidates", "expected"),
    [
        (_recommendation(simulated=True), (_candidate(),), "simulated-evidence"),
        (_recommendation(verdict="no-verdict", winner=None), (_candidate(),), "no-verdict"),
        (
            _recommendation(verdict="keep-current", winner="baseline"),
            (_candidate("baseline"),),
            "keep-current",
        ),
        (_recommendation(winner="ghost"), (_candidate(),), "winner-not-found"),
    ],
)
def test_evidence_too_weak_to_write_a_file_is_too_weak_to_open_a_pull_request(
    tmp_path: Path,
    recommendation: Recommendation,
    candidates: tuple[CandidateConfiguration, ...],
    expected: str,
) -> None:
    """A PR is a stronger claim than a file write, so it can never refuse less."""

    runner = _healthy(tmp_path)
    plan = plan_apply(recommendation, candidates, repo_path=tmp_path)
    assert plan.refusal == expected

    pull_request = plan_pull_request(plan, tmp_path, runner=runner)

    assert pull_request.refusal == "plan-refused"
    assert plan.explanation in pull_request.explanation
    assert runner.calls == []  # not even a probe: there is nothing to check


def test_a_refused_pull_request_cannot_be_opened_even_if_forced(tmp_path: Path) -> None:
    runner = _healthy(tmp_path, {"gh auth status": CommandResult(127, found=False)})
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)
    pull_request = plan_pull_request(plan, tmp_path, runner=runner)

    with pytest.raises(ValueError, match="cannot be opened"):
        open_pull_request(plan, pull_request, tmp_path, runner=runner)

    assert _mutating(runner.calls) == ()
    assert not (tmp_path / "AGENTS.md").exists()


def test_a_failed_push_stops_the_sequence_before_the_pull_request_is_claimed(
    tmp_path: Path,
) -> None:
    """Everything after the failure must not run, least of all `gh pr create`."""

    runner = _healthy(
        tmp_path, {"git push": CommandResult(1, stderr="fatal: repository not found")}
    )
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)
    pull_request = plan_pull_request(plan, tmp_path, runner=runner)

    result = open_pull_request(plan, pull_request, tmp_path, runner=runner)

    assert result.opened is False
    assert result.failed == ("git", "push", "--set-upstream", "origin", plan.branch)
    assert "repository not found" in result.detail
    assert result.url == ""
    assert not any(call[:3] == ("gh", "pr", "create") for call in runner.calls)


# --------------------------------------------------------------------------
# A refusal leaves the repository exactly as it found it. Proven against real
# git: HEAD, the branch list and the working tree are compared byte for byte.
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo), *args), check=True, capture_output=True, text=True
    )
    return completed.stdout


def _repository(root: Path) -> Path:
    """A one-commit repository whose remote is a bare repository on disk.

    On disk, so `git push` is a real push that cannot reach a network.
    """

    repo, remote = root / "repo", root / "remote.git"
    subprocess.run(("git", "init", "-q", "-b", "main", str(repo)), check=True, capture_output=True)
    subprocess.run(("git", "init", "-q", "--bare", str(remote)), check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", str(remote))
    return repo


def _state(repo: Path) -> tuple[str, str, str]:
    """HEAD, every branch, and the working tree: what a refusal must not touch."""

    return (
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "branch", "--list", "--all"),
        _git(repo, "status", "--porcelain"),
    )


def _dirty(repo: Path) -> None:
    (repo / "notes.txt").write_text("work in progress\n", encoding="utf-8")


_AUTHENTICATED = {"gh auth status": CommandResult(0, "Logged in to github.com account octocat")}


def _plan_for(repo: Path) -> ApplyPlan:
    return plan_apply(_recommendation(), (_candidate(),), repo_path=repo)


def _real_git(script: dict[str, CommandResult] | None = None) -> _Runner:
    """Everything but `gh` really runs, so the gates see a real repository."""

    return _Runner({**_AUTHENTICATED, **(script or {})}, real_git=True)


def _commit(repo: Path, path: str, content: str) -> None:
    (repo / path).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {path}")


# --------------------------------------------------------------------------
# The dirty-worktree gate asks about content, not about filenames. `git add --
# AGENTS.md` stages the whole file, so "this path is one CTX Fit writes" does
# not make the bytes in it CTX Fit's: the user's own edits can be sitting in
# there, and a path-based exemption committed and pushed them.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "committed",
    [None, "# House rules\n\nNever touch migrations/.\n"],
    ids=["brand-new-file", "already-committed-file"],
)
def test_the_file_ctx_fit_itself_writes_does_not_count_as_dirty(
    tmp_path: Path, committed: str | None
) -> None:
    """Running --apply first, then --pr, is the obvious workflow and must work."""

    repo = _repository(tmp_path)
    if committed is not None:
        _commit(repo, "AGENTS.md", committed)
    apply_plan(_plan_for(repo), repo)  # exactly what `--apply` does
    assert ".ctx/" in _git(repo, "status", "--porcelain")

    pull_request = plan_pull_request(_plan_for(repo), repo, runner=_real_git())

    assert pull_request.can_open is True, pull_request.explanation


@pytest.mark.parametrize("apply_first", [False, True], ids=["edited", "edited-then-applied"])
def test_the_users_own_edits_inside_our_file_stop_the_pull_request(
    tmp_path: Path, apply_first: bool
) -> None:
    """The whole file is staged, so an unrelated edit in it is unrelated work.

    Exempting the path instead of the bytes put a line the user had not even
    finished writing into a commit titled "Optimize AI coding configuration"
    and pushed it to their remote.
    """

    repo = _repository(tmp_path)
    _commit(repo, "AGENTS.md", "# House rules\n")
    wip = "# House rules\nTODO(me): drop the customer database before the demo.\n"
    (repo / "AGENTS.md").write_text(wip, encoding="utf-8")
    if apply_first:
        # The user's edit is now underneath CTX Fit's own block, which is the
        # case a plan-content comparison would wave through.
        apply_plan(_plan_for(repo), repo)
    before = _state(repo)
    runner = _real_git()

    pull_request = plan_pull_request(_plan_for(repo), repo, runner=runner)

    assert pull_request.refusal == "dirty-worktree"
    # Naming the file is not enough: a user who has just run `--apply` knows
    # CTX Fit wrote AGENTS.md, and "uncommitted: AGENTS.md" reads as a bug.
    assert "uncommitted: AGENTS.md" in pull_request.explanation
    assert _mutating(runner.calls) == ()
    assert _state(repo) == before
    assert "drop the customer database" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "drop the customer database" not in _git(repo, "show", "HEAD:AGENTS.md")


def test_a_hand_written_file_that_ctx_fit_has_never_touched_is_not_ours(tmp_path: Path) -> None:
    """No block in it means no part of it is CTX Fit's, whatever its name."""

    repo = _repository(tmp_path)
    _commit(repo, "AGENTS.md", "# House rules\n")
    (repo / "AGENTS.md").write_text(
        "# House rules\n\nAsk Dana before deploying.\n", encoding="utf-8"
    )
    runner = _real_git()

    pull_request = plan_pull_request(_plan_for(repo), repo, runner=runner)

    assert pull_request.refusal == "dirty-worktree"
    assert _mutating(runner.calls) == ()


@pytest.mark.parametrize(
    ("prepare", "script", "expected"),
    [
        (
            lambda repo: None,
            {"gh auth status": CommandResult(127, found=False)},
            "gh-not-installed",
        ),
        (
            lambda repo: None,
            {"gh auth status": CommandResult(1, stderr="not logged in")},
            "gh-not-authenticated",
        ),
        (_dirty, _AUTHENTICATED, "dirty-worktree"),
        (lambda repo: _git(repo, "branch", "ctx-fit/run"), _AUTHENTICATED, "branch-exists"),
        (lambda repo: _git(repo, "remote", "remove", "origin"), _AUTHENTICATED, "no-remote"),
    ],
    ids=["gh-missing", "gh-logged-out", "dirty", "branch-exists", "no-remote"],
)
def test_a_refusal_leaves_the_repository_exactly_as_it_found_it(
    tmp_path: Path,
    prepare: Callable[[Path], None],
    script: dict[str, CommandResult],
    expected: str,
) -> None:
    repo = _repository(tmp_path)
    prepare(repo)
    before = _state(repo)
    runner = _Runner(script, real_git=True)
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo)

    pull_request = plan_pull_request(plan, repo, runner=runner)

    assert pull_request.refusal == expected
    assert _state(repo) == before
    assert not (repo / "AGENTS.md").exists()


def test_a_plan_that_was_already_refused_never_reaches_the_repository(tmp_path: Path) -> None:
    """The earliest refusal is also the one that must probe least: nothing at all."""

    repo = _repository(tmp_path)
    before = _state(repo)
    runner = _real_git()
    plan = plan_apply(_recommendation(simulated=True), (_candidate(),), repo_path=repo)

    pull_request = plan_pull_request(plan, repo, runner=runner)

    assert pull_request.refusal == "plan-refused"
    assert runner.calls == []
    assert _state(repo) == before
    assert not (repo / "AGENTS.md").exists()


def test_a_missing_git_and_an_unreadable_repository_are_told_apart(tmp_path: Path) -> None:
    """`gh` gets the missing-versus-refusing distinction, and so does `git`.

    "not inside a git repository" is a lie when the truth is that `git` is not
    installed, and it sends the user looking in the wrong place.
    """

    repo = _repository(tmp_path)
    before = _state(repo)
    plan = _plan_for(repo)
    absent = _real_git(
        {
            "git rev-parse --show-toplevel": CommandResult(
                127, stderr="[Errno 2] No such file or directory: 'git'", found=False
            )
        }
    )
    elsewhere = _real_git(
        {
            "git rev-parse --show-toplevel": CommandResult(
                128, stderr="fatal: not a git repository (or any of the parent directories)"
            )
        }
    )

    missing = plan_pull_request(plan, repo, runner=absent)
    not_a_repo = plan_pull_request(plan, repo, runner=elsewhere)

    assert missing.refusal == "git-not-installed"
    assert "git-scm.com" in missing.explanation
    assert not_a_repo.refusal == "not-a-git-repository"
    assert _state(repo) == before
    assert not (repo / "AGENTS.md").exists()


def test_the_pull_request_is_actually_opened_against_a_real_repository(tmp_path: Path) -> None:
    """End to end: real git, a bare remote on disk, and `gh` the only fake."""

    repo = _repository(tmp_path)
    runner = _Runner(
        {
            **_AUTHENTICATED,
            "gh pr create": CommandResult(0, "https://github.com/octocat/repo/pull/12\n"),
        },
        real_git=True,
    )
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo, run_id="e2e")

    pull_request = plan_pull_request(plan, repo, runner=runner)
    assert pull_request.can_open is True, pull_request.explanation
    assert pull_request.original_branch == "main"

    result = open_pull_request(plan, pull_request, repo, runner=runner)

    assert result.opened is True, result.detail
    assert result.url == "https://github.com/octocat/repo/pull/12"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "ctx-fit/e2e"
    assert _git(repo, "log", "-1", "--format=%s").strip() == _TITLE
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
        ".ctx/fit-configuration.json",
    ]
    assert "refs/heads/ctx-fit/e2e" in _git(repo, "ls-remote", "--heads", "origin")
    assert _git(repo, "status", "--porcelain") == ""  # nothing left behind uncommitted
    assert runner.stdin[pull_request.commands[-1]] == plan.pr_body
