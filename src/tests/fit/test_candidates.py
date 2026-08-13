from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ctx.engine.planner import BoundedCapabilityPlanner, CapabilityCandidate
from ctx.fit.candidates import (
    MAX_CANDIDATES,
    ROLE_INTENT,
    CandidateSet,
    generate_candidates,
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


def test_configuration_hash_tracks_real_differences(tmp_path: Path) -> None:
    plain = _generate(tmp_path)
    with_model = _generate(tmp_path, model="gpt-5.5")

    baseline_plain = plain.candidates[1]
    baseline_model = with_model.candidates[1]
    assert baseline_plain.capability_ids == baseline_model.capability_ids
    assert baseline_plain.configuration_hash != baseline_model.configuration_hash


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
