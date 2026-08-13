from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctx.fit.apply import (
    APPLY_SCHEMA,
    OWNED_BLOCK_START,
    ApplyPlan,
    Artifact,
    apply_plan,
    plan_apply,
)
from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.recommend import RankedCandidate, Recommendation


def _candidate(name: str = "lean") -> CandidateConfiguration:
    return CandidateConfiguration(
        candidate_id=name,
        role="recommended",
        capability_ids=("skill:ctx-python-testing",),
        model=None,
        instructions=(),
        selection_reason="the single highest-ranked capability, to test whether less is enough",
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

    assert written == ("AGENTS.md",)
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == plan.artifacts[0].content


def test_existing_file_is_reported_as_a_modification(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# existing\n", encoding="utf-8")

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


def test_generated_agents_md_records_evidence_not_just_the_answer(tmp_path: Path) -> None:
    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path)

    content = plan.artifacts[0].content
    assert "skill:ctx-python-testing" in content
    assert "verified 9/9 trials" in content
    assert "Confidence: medium" in content
    assert "## Limitations" in content


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


def test_a_symlinked_agents_md_is_refused_instead_of_followed(tmp_path: Path) -> None:
    """Writing through the link edits a file the preview never named."""

    repo, shared = _shared_file_outside(tmp_path)
    _link(repo / "AGENTS.md", shared)
    before = shared.read_text(encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo)

    assert plan.refusal == "unsafe-destination"
    assert plan.can_apply is False
    assert plan.artifacts == ()
    assert "symbolic link" in plan.explanation
    assert shared.read_text(encoding="utf-8") == before


def test_a_symlink_that_appears_after_the_preview_is_still_not_followed(
    tmp_path: Path,
) -> None:
    """A plan is previewed, then applied: the destination can change in between."""

    repo, shared = _shared_file_outside(tmp_path)
    before = shared.read_text(encoding="utf-8")

    plan = plan_apply(_recommendation(), (_candidate(),), repo_path=repo)
    assert plan.can_apply is True
    _link(repo / "AGENTS.md", shared)

    with pytest.raises(ValueError, match="symbolic link"):
        apply_plan(plan, repo)

    assert shared.read_text(encoding="utf-8") == before


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

    assert "never touch migrations/" in after
    assert "contact: platform@example.com" in after
    assert "skill:ctx-python-testing" in after  # and the new evidence is there too
    assert plan.artifacts[0].preserved_bytes == len(_HAND_WRITTEN.strip().encode("utf-8"))


def test_applying_twice_replaces_the_previous_block_instead_of_stacking(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text(_HAND_WRITTEN, encoding="utf-8")

    plans = []
    for _ in range(2):
        plans.append(plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path))
        apply_plan(plans[-1], tmp_path)
    after = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    # The second run's own block is not counted as content it preserved.
    assert plans[1].artifacts[0].preserved_bytes == plans[0].artifacts[0].preserved_bytes

    assert after.count(OWNED_BLOCK_START) == 1
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
    payload = plan_apply(_recommendation(), (_candidate(),), repo_path=tmp_path).to_dict()

    assert json.loads(json.dumps(payload, sort_keys=True))["schema"] == APPLY_SCHEMA
    assert payload["can_apply"] is True
