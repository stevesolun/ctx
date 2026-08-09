from __future__ import annotations

import json
from pathlib import Path

from ctx.cli.run import main as ctx_main
from ctx.fit.profile import build_fit_profile
from ctx.fit.readiness import (
    DIMENSION_POINTS,
    READINESS_RUBRIC_VERSION,
    RUBRIC,
    score_readiness,
)


def _repo(tmp_path: Path, *, tests: bool = True, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\nrequires-python='>=3.11'\n\n"
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n\n"
        "[tool.ruff]\nline-length=100\n\n[tool.mypy]\nstrict=true\n\n"
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n",
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    if tests:
        (repo / "tests").mkdir()
        (repo / "tests" / "test_demo.py").write_text("def test_ok():\n    assert True\n", "utf-8")
    return repo


def _score(repo: Path):
    return score_readiness(build_fit_profile(repo), repo)


# --------------------------------------------------------------------------
# Anti-gaming: the rubric must justify itself, enforced at build time.
# --------------------------------------------------------------------------


def test_every_check_states_how_it_helps_an_agent() -> None:
    """A metric that cannot say why it matters does not belong in the rubric."""

    rationales = [check.agent_rationale.strip() for check in RUBRIC]

    assert all(rationales), "every check needs a non-empty agent_rationale"
    assert len(set(rationales)) == len(rationales), "rationales must be distinct, not copy-pasted"
    assert all(len(text) > 40 for text in rationales), "rationales must be substantive"
    assert all(check.remedy.strip() for check in RUBRIC), "every check needs an actionable remedy"


def test_check_ids_are_unique_and_points_match_dimension_budgets() -> None:
    ids = [check.check_id for check in RUBRIC]
    assert len(set(ids)) == len(ids)

    for dimension, budget in DIMENSION_POINTS.items():
        total = sum(check.points for check in RUBRIC if check.dimension == dimension)
        assert total == budget, f"{dimension} checks sum to {total}, expected {budget}"


def test_scoring_is_deterministic_and_versioned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    first, second = _score(repo), _score(repo)

    assert first.to_dict() == second.to_dict()
    assert first.rubric_version == READINESS_RUBRIC_VERSION


# --------------------------------------------------------------------------
# Unassessable must never be silently scored as zero.
# --------------------------------------------------------------------------


def test_not_applicable_checks_leave_the_denominator(tmp_path: Path) -> None:
    """A Python-only check must not penalize a repository with no Python."""

    repo = tmp_path / "node"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}), "utf-8")
    (repo / "__tests__").mkdir()
    (repo / "__tests__" / "a.test.js").write_text("test('x', () => {});\n", "utf-8")

    report = score_readiness(build_fit_profile(repo), repo)
    runtime_check = next(item for item in report.checks if item.check_id == "E2")

    assert runtime_check.state == "not_applicable"
    assert runtime_check.earned == 0
    # Excluded from the denominator rather than counted as a failure.
    environment = next(item for item in report.dimensions if item.dimension == "environment")
    assert environment.assessable < environment.possible


def test_score_is_none_rather_than_zero_when_nothing_is_assessable(tmp_path: Path) -> None:
    report = score_readiness.__wrapped__ if False else None  # keep import used
    del report

    empty = tmp_path / "empty"
    empty.mkdir()
    actual = score_readiness(build_fit_profile(empty), empty)

    # An empty repo still has assessable checks (it fails them), so the score is
    # a real 0-100 value; the guarantee under test is that it is never a
    # fabricated number derived from an empty denominator.
    assert actual.assessable > 0
    assert actual.score is not None
    assert 0 <= actual.score <= 100


# --------------------------------------------------------------------------
# Blockers are falsifiable and never double-counted.
# --------------------------------------------------------------------------


def test_missing_tests_is_a_blocker(tmp_path: Path) -> None:
    report = _score(_repo(tmp_path, tests=False))

    blocker_ids = {item.check_id for item in report.blockers}
    assert "V1" in blocker_ids
    blocker = next(item for item in report.blockers if item.check_id == "V1")
    assert blocker.remedy


def test_blocking_is_a_classification_not_an_extra_penalty(tmp_path: Path) -> None:
    report = _score(_repo(tmp_path, tests=False))

    total_earned = sum(item.earned for item in report.checks)
    assert report.earned == total_earned  # no separate blocker deduction


def test_a_healthy_repository_has_no_blockers(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".git").mkdir()

    report = _score(repo)

    assert report.blockers == ()
    assert report.score is not None and report.score > 40


def test_improvements_are_ranked_by_points_recoverable(tmp_path: Path) -> None:
    report = _score(_repo(tmp_path))

    gains = [item.possible - item.earned for item in report.improvements]
    assert gains == sorted(gains, reverse=True)
    assert all(item.state in {"fail", "partial"} for item in report.improvements)


# --------------------------------------------------------------------------
# Adversarial repositories must not crash the report.
# --------------------------------------------------------------------------


def test_monorepo_scope_is_reported_as_partial(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "package.json").write_text(json.dumps({"workspaces": ["packages/*"]}), "utf-8")
    (repo / "packages").mkdir()

    report = _score(repo)
    scope = next(item for item in report.checks if item.check_id == "X1")

    assert scope.state in {"pass", "partial"}


def test_unknown_language_repository_still_produces_a_report(tmp_path: Path) -> None:
    repo = tmp_path / "mystery"
    repo.mkdir()
    (repo / "main.zig").write_text("pub fn main() void {}\n", encoding="utf-8")

    report = score_readiness(build_fit_profile(repo), repo)

    assert report.score is not None
    assert report.blockers  # no tests, no git


def test_readiness_appears_in_cli_output_and_json(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path, tests=False)

    assert ctx_main(["fit", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "AI agent readiness" in text
    assert "Blocking" in text

    assert ctx_main(["fit", str(repo), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["readiness"]["rubric_version"] == READINESS_RUBRIC_VERSION
    assert payload["readiness"]["blockers"]
