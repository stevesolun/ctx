from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path

import pytest

from ctx.engine.planner import BoundedCapabilityPlanner, CapabilityCandidate
from ctx.fit.candidates import (
    MAX_CANDIDATES,
    MAX_CANDIDATE_USER_CONTEXT_BYTES,
    MAX_INSTRUCTION_FILE_BYTES,
    ROLE_INTENT,
    CapabilityMaterial,
    CandidateConfiguration,
    CandidateSet,
    InstructionMaterial,
    generate_candidates,
    render_candidate_user_context,
)
from ctx.fit.profile import build_fit_profile


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Source:
    """A deterministic stand-in for the shipped capability catalog."""

    names: tuple[str, ...] = ("ctx-python-testing", "ctx-python-state-protocols", "ctx-typescript")

    def catalog_snapshot_digest(self) -> str:
        return _digest("catalog")

    def retrieve(self, _observation: object) -> tuple[CapabilityCandidate, ...]:
        return tuple(
            CapabilityCandidate(
                capability_id=f"skill:{name}",
                kind="skill",
                name=name,
                source_digest=_digest(name),
                normalized_score_ppm=900_000 - index * 1_000,
                matching_signals=("python",),
                reason_codes=("graph-match",),
                actionability="load",
            )
            for index, name in enumerate(self.names)
        )


@dataclass(frozen=True)
class _KindedSource:
    """A source whose entries span capability kinds, not just skills."""

    kinds: tuple[tuple[str, str], ...]

    def catalog_snapshot_digest(self) -> str:
        return _digest("kinded")

    def retrieve(self, _observation: object) -> tuple[CapabilityCandidate, ...]:
        return tuple(
            CapabilityCandidate(
                capability_id=f"{kind}:{name}",
                kind=kind,
                name=name,
                source_digest=_digest(name),
                normalized_score_ppm=900_000 - index * 1_000,
                matching_signals=("python",),
                reason_codes=("graph-match",),
                actionability="load",
            )
            for index, (kind, name) in enumerate(self.kinds)
        )


@dataclass(frozen=True)
class _EmptySource:
    def catalog_snapshot_digest(self) -> str:
        return _digest("empty")

    def retrieve(self, _observation: object) -> tuple[CapabilityCandidate, ...]:
        return ()


def _repo(tmp_path: Path, *, tests: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\n\n[tool.pytest.ini_options]\ntestpaths=['tests']\n",
        encoding="utf-8",
    )
    if tests:
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", "utf-8")
    return repo


def _generate(tmp_path: Path, source: object = None, **kwargs: object) -> CandidateSet:
    profile = build_fit_profile(_repo(tmp_path))
    planner = BoundedCapabilityPlanner(source=source or _Source())  # type: ignore[arg-type]
    return generate_candidates(profile, planner, **kwargs)  # type: ignore[arg-type]


def _write_applied(repo: Path, candidate: CandidateConfiguration) -> None:
    target = repo / ".ctx" / "fit-configuration.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": "ctx.fit.applied-configuration-v1",
                "configuration_hash": candidate.configuration_hash,
                "candidate": candidate.to_dict(),
            }
        ),
        encoding="utf-8",
    )


def test_every_candidate_explains_why_it_was_selected(tmp_path: Path) -> None:
    """A candidate with no reason to exist must never be proposed."""

    result = _generate(tmp_path)

    assert result.candidates
    for candidate in result.candidates:
        assert candidate.selection_reason.strip(), candidate.candidate_id
        assert len(candidate.selection_reason) > 40
        assert candidate.role in ROLE_INTENT


def test_a_baseline_is_always_present(tmp_path: Path) -> None:
    """No improvement can be claimed without a control."""

    assert _generate(tmp_path).baseline is not None
    # Even when the repository cannot be evaluated at all.
    profile = build_fit_profile(_repo(tmp_path, tests=False))
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]
    assert generate_candidates(profile, planner).baseline is not None


def test_candidate_set_is_bounded(tmp_path: Path) -> None:
    result = _generate(tmp_path)

    assert 0 < len(result.candidates) <= MAX_CANDIDATES


def test_candidates_are_diverse_not_duplicates(tmp_path: Path) -> None:
    """Testing the same configuration twice wastes the evaluation budget."""

    result = _generate(tmp_path)

    hashes = [candidate.configuration_hash for candidate in result.candidates]
    assert len(set(hashes)) == len(hashes)
    roles = [candidate.role for candidate in result.candidates]
    assert len(set(roles)) == len(roles)


def test_identical_configurations_collapse(tmp_path: Path) -> None:
    """With one capability, 'recommended' and 'lean' would be identical."""

    result = _generate(tmp_path, source=_Source(names=("ctx-python-testing",)))

    hashes = [candidate.configuration_hash for candidate in result.candidates]
    assert len(set(hashes)) == len(hashes)
    assert "lean" not in {candidate.role for candidate in result.candidates}


def test_generation_is_deterministic(tmp_path: Path) -> None:
    first = _generate(tmp_path).to_dict()
    second = _generate(tmp_path).to_dict()

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_applied_configuration_is_the_exact_baseline_and_not_ambient_context(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Later ambient instructions\n", encoding="utf-8")
    current_material = CapabilityMaterial.from_content(
        capability_id="skill:ctx-python-testing",
        delivery_mode="task-user-context",
        source_identity="package:catalog#skill:ctx-python-testing",
        catalog_entry_digest=_digest("current catalog entry"),
        content="# Applied current skill\n",
    )
    applied_instruction = InstructionMaterial.from_content(
        path="AGENTS.md", content="# Exact applied instructions\n"
    )
    current = CandidateConfiguration(
        candidate_id="prior-winner",
        role="lean",
        capability_ids=(current_material.capability_id,),
        model="openai/gpt-5.5",
        instructions=(applied_instruction.path,),
        selection_reason="The previously measured configuration currently active in ctx run.",
        capability_materials=(current_material,),
        instruction_materials=(applied_instruction,),
    )
    _write_applied(repo, current)
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="different-model")

    baseline = result.baseline
    assert baseline is not None
    assert baseline.model == current.model
    assert baseline.capability_ids == current.capability_ids
    assert baseline.capability_materials == current.capability_materials
    assert baseline.instruction_materials == current.instruction_materials
    assert "Later ambient instructions" not in render_candidate_user_context(baseline)
    assert all(candidate.model == current.model for candidate in result.candidates)
    assert all(
        candidate.instruction_materials == current.instruction_materials
        for candidate in result.candidates
    )
    assert current_material.capability_id not in {
        capability_id
        for candidate in result.candidates[1:]
        for capability_id in candidate.capability_ids
    }


def test_first_use_baseline_contains_exact_installed_repository_skill(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    installed = repo / ".claude" / "skills" / "current" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    original = b"---\nname: current\n---\n\nUse the repository's exact current skill.  \n"
    installed.write_bytes(original)
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    baseline = result.baseline
    assert baseline is not None
    assert baseline.capability_ids == ("skill:current",)
    assert len(baseline.capability_materials) == 1
    material = baseline.capability_materials[0]
    assert material.source_identity == "repository:.claude/skills/current/SKILL.md"
    assert material.content.encode("utf-8") == original
    assert material.content_sha256 == hashlib.sha256(original).hexdigest()
    assert original.decode("utf-8") in render_candidate_user_context(baseline)


@pytest.mark.parametrize(
    "content",
    (
        "# Missing frontmatter\n",
        "---\nname: another-skill\n---\n\n# Wrong identity\n",
        "---\nname: current\n---\n",
    ),
)
def test_first_use_abstains_when_installed_skill_is_not_a_valid_current_identity(
    tmp_path: Path,
    content: str,
) -> None:
    repo = _repo(tmp_path)
    installed = repo / ".claude" / "skills" / "current" / "SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text(content, encoding="utf-8")
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "skill frontmatter" in " ".join(result.warnings)


def test_invalid_applied_configuration_abstains_before_any_paid_comparison(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    target = repo / ".ctx" / "fit-configuration.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"schema":"wrong"}', encoding="utf-8")
    profile = build_fit_profile(repo)

    result = generate_candidates(
        profile,
        BoundedCapabilityPlanner(source=_Source()),  # type: ignore[arg-type]
        model="gpt-4o-mini",
    )

    assert result.abstained is True
    assert result.candidates == ()
    assert "active CTX Fit configuration is invalid" in (result.abstention_reason or "")


def test_configuration_hash_tracks_real_differences(tmp_path: Path) -> None:
    plain = _generate(tmp_path)
    with_model = _generate(tmp_path, model="gpt-5.5")

    baseline_plain = plain.candidates[1]
    baseline_model = with_model.candidates[1]
    assert baseline_plain.capability_ids == baseline_model.capability_ids
    assert baseline_plain.configuration_hash != baseline_model.configuration_hash


def test_generated_candidates_bind_the_exact_material_the_agent_will_receive(
    tmp_path: Path,
) -> None:
    """An ID cannot reproduce a skill after its catalog content changes."""

    candidate = next(item for item in _generate(tmp_path).candidates if item.role == "lean")

    assert candidate.capability_ids == ("skill:ctx-python-testing",)
    assert len(candidate.capability_materials) == 1
    material = candidate.capability_materials[0]
    assert material.capability_id == "skill:ctx-python-testing"
    assert material.delivery_mode == "task-user-context"
    assert material.source_identity == (
        "package:ctx.assets/runtime-availability.json#skill:ctx-python-testing"
    )
    assert material.content.startswith("---\nname: ctx-python-testing\n")
    assert material.content_sha256 == _digest(material.content)
    assert material.content_bytes == len(material.content.encode("utf-8"))
    catalog = json.loads(
        (resources.files("ctx.assets") / "runtime-availability.json").read_text("utf-8")
    )
    entry = next(item for item in catalog["entries"] if item["id"] == material.capability_id)
    canonical_entry = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert material.catalog_entry_digest == _digest(canonical_entry)
    assert material.catalog_entry_digest != _digest(material.capability_id)


def test_configuration_hash_binds_material_not_only_the_capability_id() -> None:
    """Two catalog revisions under one stable ID are different experiments."""

    def candidate(content: str) -> CandidateConfiguration:
        material = CapabilityMaterial.from_content(
            capability_id="skill:demo",
            delivery_mode="task-user-context",
            source_identity="package:ctx.assets/runtime-availability.json#skill:demo",
            catalog_entry_digest=_digest("skill:demo"),
            content=content,
        )
        return CandidateConfiguration(
            candidate_id="demo",
            role="recommended",
            capability_ids=("skill:demo",),
            model="gpt-4o-mini",
            instructions=("AGENTS.md",),
            selection_reason="A sufficiently long deterministic reason for selecting this candidate.",
            capability_materials=(material,),
            instruction_materials=(
                InstructionMaterial.from_content(path="AGENTS.md", content="# Rules\n"),
            ),
        )

    first = candidate("# Demo\n\nFirst revision.\n")
    second = candidate("# Demo\n\nSecond revision.\n")

    assert first.capability_ids == second.capability_ids
    assert first.configuration_hash != second.configuration_hash


def test_catalog_entry_digest_must_be_lowercase_sha256() -> None:
    with pytest.raises(ValueError, match="catalog entry digest"):
        CapabilityMaterial.from_content(
            capability_id="skill:demo",
            delivery_mode="task-user-context",
            source_identity="package:catalog#skill:demo",
            catalog_entry_digest="g" * 64,
            content="# Demo\n",
        )


def test_treatment_candidate_requires_material_for_every_capability() -> None:
    with pytest.raises(ValueError, match="missing exact capability material"):
        CandidateConfiguration(
            candidate_id="recommended",
            role="recommended",
            capability_ids=("skill:demo",),
            model="gpt-4o-mini",
            instructions=(),
            selection_reason="A sufficiently long deterministic reason for selecting this candidate.",
        )


def test_baseline_requires_material_for_every_current_capability() -> None:
    with pytest.raises(ValueError, match="missing exact capability material"):
        CandidateConfiguration(
            candidate_id="baseline",
            role="baseline",
            capability_ids=("skill:user-owned",),
            model="gpt-4o-mini",
            instructions=(),
            selection_reason="The user's current setup must be an exact control.",
        )


def test_generation_binds_exact_repository_instruction_material_to_every_arm(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    original = b"# Rules\r\n\r\nKeep these exact bytes.  \r\n"
    (repo / "AGENTS.md").write_bytes(original)
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is False
    assert result.candidates
    for candidate in result.candidates:
        assert candidate.instructions == ("AGENTS.md",)
        assert len(candidate.instruction_materials) == 1
        material = candidate.instruction_materials[0]
        assert material.path == "AGENTS.md"
        assert material.delivery_mode == "task-user-context"
        assert material.source_identity == "repository:AGENTS.md"
        assert material.content.encode("utf-8") == original
        assert material.content_sha256 == hashlib.sha256(original).hexdigest()


def test_configuration_hash_binds_exact_repository_instruction_content() -> None:
    first_material = InstructionMaterial.from_content(path="AGENTS.md", content="# First\n")
    second_material = InstructionMaterial.from_content(path="AGENTS.md", content="# Second\n")
    base = CandidateConfiguration(
        candidate_id="baseline",
        role="baseline",
        capability_ids=(),
        model="gpt-4o-mini",
        instructions=("AGENTS.md",),
        selection_reason="The user's current setup remains the explicit control configuration.",
        instruction_materials=(first_material,),
    )

    changed = replace(base, instruction_materials=(second_material,))

    assert base.configuration_hash != changed.configuration_hash


def test_candidate_user_context_is_the_single_deterministic_material_renderer() -> None:
    instruction = InstructionMaterial.from_content(
        path="AGENTS.md", content="# Repository rules\n\nKeep exact spacing.  \n"
    )
    capability = CapabilityMaterial.from_content(
        capability_id="skill:demo",
        delivery_mode="task-user-context",
        source_identity="package:catalog#skill:demo",
        catalog_entry_digest=_digest("catalog entry"),
        content="# Demo\n\nUse this exact body.\n",
    )
    candidate = CandidateConfiguration(
        candidate_id="recommended",
        role="recommended",
        capability_ids=("skill:demo",),
        model="gpt-4o-mini",
        instructions=("AGENTS.md",),
        selection_reason="This rationale must not leak into executable reference context.",
        evidence=("This evidence also must not leak.",),
        capability_materials=(capability,),
        instruction_materials=(instruction,),
    )

    first = render_candidate_user_context(candidate)
    second = render_candidate_user_context(candidate)

    assert first == second
    assert instruction.content in first
    assert capability.content in first
    assert instruction.content_sha256 in first
    assert capability.content_sha256 in first
    assert first.index(instruction.content) < first.index(capability.content)
    assert candidate.model is not None
    assert candidate.model not in first
    assert candidate.selection_reason not in first
    assert candidate.evidence[0] not in first


@pytest.mark.parametrize("problem", ["symlink", "non-utf8", "oversized"])
def test_generation_abstains_when_repository_instructions_cannot_be_safely_bound(
    tmp_path: Path,
    problem: str,
) -> None:
    repo = _repo(tmp_path)
    agents = repo / "AGENTS.md"
    if problem == "symlink":
        actual = repo / "ACTUAL.md"
        actual.write_text("# Rules\n", encoding="utf-8")
        try:
            agents.symlink_to(actual)
        except OSError as exc:  # pragma: no cover - platform dependent
            pytest.skip(f"symlinks unavailable: {exc}")
    elif problem == "non-utf8":
        agents.write_bytes(b"\xff\xfe")
    else:
        agents.write_bytes(b"x" * (MAX_INSTRUCTION_FILE_BYTES + 1))
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "cannot reproduce a baseline" in (result.abstention_reason or "")
    assert "AGENTS.md" in " ".join(result.warnings)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".mcp.json", ".mcp.json"),
        (".claude/agents/reviewer.md", ".claude/agents"),
    ],
)
def test_generation_abstains_when_current_non_skill_tools_cannot_be_reproduced(
    tmp_path: Path,
    path: str,
    expected: str,
) -> None:
    repo = _repo(tmp_path)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding="utf-8")
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "cannot be reproduced" in (result.abstention_reason or "")
    assert expected in " ".join(result.warnings)


def test_generation_abstains_for_a_repository_skill_with_unbound_companion_files(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    skill = repo / ".codex" / "skills" / "current"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Current\n", encoding="utf-8")
    (skill / "reference.md").write_text("# Required reference\n", encoding="utf-8")
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "single-file skill installation" in " ".join(result.warnings)


def test_generation_rejects_a_skill_fifo_without_waiting_for_a_writer(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable on this platform")
    repo = _repo(tmp_path)
    skill = repo / ".codex" / "skills" / "current"
    skill.mkdir(parents=True)
    os.mkfifo(skill / "SKILL.md")
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "not a regular file" in " ".join(result.warnings)


def test_generation_abstains_instead_of_crashing_on_a_noncanonical_current_skill_name(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    skill = repo / ".agents" / "skills" / "MixedCase"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Current\n", encoding="utf-8")
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "canonical skill directory name" in " ".join(result.warnings)


def test_generation_abstains_instead_of_crashing_on_an_overlong_current_skill_name(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    skill = repo / ".agents" / "skills" / ("a" * 170)
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Current\n", encoding="utf-8")
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "canonical skill directory name" in " ".join(result.warnings)


def test_generation_rejects_an_instruction_path_outside_the_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "OUTSIDE.md"
    outside.write_text("# Not repository instructions\n", encoding="utf-8")
    profile = build_fit_profile(repo)
    profile = replace(
        profile,
        existing_ai_config=replace(
            profile.existing_ai_config,
            instruction_files=("../OUTSIDE.md",),
        ),
    )
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "normalized repository-relative path" in " ".join(result.warnings)


def test_generation_abstains_before_context_exceeds_the_harness_limit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text(
        "x" * MAX_CANDIDATE_USER_CONTEXT_BYTES,
        encoding="utf-8",
    )
    profile = build_fit_profile(repo)
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner, model="gpt-4o-mini")

    assert result.abstained is True
    assert result.candidates == ()
    assert "exceeds the harness limit" in (result.abstention_reason or "")
    assert str(MAX_CANDIDATE_USER_CONTEXT_BYTES) in " ".join(result.warnings)


def test_a_selected_skill_without_material_is_not_proposed(tmp_path: Path) -> None:
    """A trial and an apply cannot share bytes that the package does not have."""

    result = _generate(tmp_path, source=_Source(names=("not-shipped",)))

    assert result.abstained is True
    assert [candidate.role for candidate in result.candidates] == ["baseline"]
    assert "skill:not-shipped" in " ".join(result.warnings)
    assert "material" in (result.abstention_reason or "")


def test_unevaluable_repository_abstains_rather_than_proposing_spend(tmp_path: Path) -> None:
    """Proposing an experiment that cannot be verified would waste real money."""

    profile = build_fit_profile(_repo(tmp_path, tests=False))
    planner = BoundedCapabilityPlanner(source=_Source())  # type: ignore[arg-type]

    result = generate_candidates(profile, planner)

    assert result.abstained is True
    assert result.abstention_reason is not None
    assert "no runnable tests" in result.abstention_reason
    assert [candidate.role for candidate in result.candidates] == ["baseline"]


def test_no_relevant_capability_abstains(tmp_path: Path) -> None:
    result = _generate(tmp_path, source=_EmptySource())

    assert result.abstained is True
    assert result.abstention_reason is not None
    assert [candidate.role for candidate in result.candidates] == ["baseline"]


# ── FITBUG-002: a candidate may only carry what a trial can actually apply ──


def test_capabilities_a_trial_cannot_apply_are_excluded_and_named(tmp_path: Path) -> None:
    """Carrying one would make an arm the baseline under a different name.

    An MCP server needs a process attached to the run and an agent needs a
    second model role; a trial has neither, so a candidate holding one differs
    from the control in the report only.
    """

    result = _generate(
        tmp_path,
        source=_KindedSource(
            kinds=(
                ("skill", "ctx-python-testing"),
                ("mcp-server", "ctx-core"),
                ("agent", "ctx-python-reviewer"),
            )
        ),
    )

    proposed = {
        capability_id
        for candidate in result.candidates
        for capability_id in candidate.capability_ids
    }
    assert proposed == {"skill:ctx-python-testing"}
    warnings = " ".join(result.warnings)
    assert "mcp-server:ctx-core" in warnings
    assert "agent:ctx-python-reviewer" in warnings


def test_only_inapplicable_capabilities_abstains_rather_than_inventing_arms(
    tmp_path: Path,
) -> None:
    """Silence is acceptable here; a comparison that cannot be run is not."""

    result = _generate(
        tmp_path,
        source=_KindedSource(kinds=(("mcp-server", "ctx-core"), ("agent", "ctx-python-reviewer"))),
    )

    assert result.abstained is True
    assert [candidate.role for candidate in result.candidates] == ["baseline"]
    assert result.abstention_reason is not None
    assert "cannot apply" in result.abstention_reason


def test_candidate_payload_is_json_serializable_and_versioned(tmp_path: Path) -> None:
    payload = _generate(tmp_path).to_dict()

    encoded = json.loads(json.dumps(payload, sort_keys=True))
    first = encoded["candidates"][0]
    assert first["schema"] == "ctx.fit.candidate-v1"
    assert first["configuration_hash"]
    assert first["role_intent"]
    treatment = encoded["candidates"][1]
    assert treatment["capability_materials"][0]["content_sha256"]
    assert treatment["capability_materials"][0]["content"].startswith("---\nname:")


def test_the_control_arm_runs_the_same_model_as_the_treatment_arms(tmp_path: Path) -> None:
    """A baseline on a different model is a confound, not a control.

    The baseline previously carried ``model=None`` while the recommended and
    lean arms carried the campaign model, so every reported difference mixed
    the capability effect with a model effect.
    """

    result = _generate(tmp_path, model="gpt-4o-mini")

    models = {candidate.model for candidate in result.candidates}
    assert models == {"gpt-4o-mini"}, (
        f"candidates do not share one model: "
        f"{ {c.candidate_id: c.model for c in result.candidates} }"
    )
