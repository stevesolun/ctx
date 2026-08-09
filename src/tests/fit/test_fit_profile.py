from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctx.cli.run import main as ctx_main
from ctx.fit import build_fit_profile, discover_verification
from ctx.fit.profile import FIT_PROFILE_SCHEMA


def _python_repo(tmp_path: Path, *, with_pytest: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    tool = "[tool.pytest.ini_options]\ntestpaths = ['tests']\n" if with_pytest else ""
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n\n"
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n\n"
        "[tool.ruff]\nline-length = 100\n\n"
        "[tool.mypy]\nstrict = true\n\n" + tool,
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    return repo


def _with_tests(repo: Path) -> Path:
    tests = repo / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_demo.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return repo


def test_declared_runner_without_test_files_is_not_evaluable(tmp_path: Path) -> None:
    """A configured runner is intent, not evidence.

    Claiming evaluability from a manifest stanza alone would state an inference
    as a fact, which is the one thing this product must never do.
    """

    repo = _python_repo(tmp_path)  # pyproject configures pytest; no tests exist
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")

    inventory = discover_verification(repo)

    assert inventory.declares_test_command is True
    assert inventory.test_files == ()
    assert inventory.has_deterministic_verification is False
    assert any("no test files were found" in warning for warning in inventory.warnings)
    assert build_fit_profile(repo).is_fit_evaluable is False


def test_declared_runner_with_test_files_is_evaluable(tmp_path: Path) -> None:
    inventory = discover_verification(_with_tests(_python_repo(tmp_path)))

    assert inventory.has_deterministic_verification is True
    assert inventory.test_files == ("tests/",)


def test_profile_json_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """Reproducibility: identical inputs must serialize identically."""

    repo = _with_tests(_python_repo(tmp_path))

    first = json.dumps(build_fit_profile(repo).to_dict(), sort_keys=True)
    second = json.dumps(build_fit_profile(repo).to_dict(), sort_keys=True)

    assert first == second
    assert "scanned_at" not in first


def test_discovers_python_verification_commands(tmp_path: Path) -> None:
    inventory = discover_verification(_with_tests(_python_repo(tmp_path)))

    assert inventory.has_deterministic_verification
    assert set(inventory.kinds) == {"test", "typecheck", "lint", "build"}
    test_command = inventory.best("test")
    assert test_command is not None
    assert test_command.command == ("python", "-m", "pytest", "-q")
    assert test_command.confidence == "high"
    assert test_command.evidence


def test_repository_without_tests_is_not_fit_evaluable(tmp_path: Path) -> None:
    repo = _python_repo(tmp_path, with_pytest=False)

    profile = build_fit_profile(repo)

    assert profile.is_fit_evaluable is False
    assert any("no test command" in warning for warning in profile.warnings)
    # Every optimization dimension must be honestly reported as unevaluable.
    assert all(not dimension.evaluable for dimension in profile.dimensions)


def test_harness_dimension_is_never_claimed_evaluable(tmp_path: Path) -> None:
    profile = build_fit_profile(_python_repo(tmp_path))

    harness = next(item for item in profile.dimensions if item.name == "coding-harness")
    assert harness.evaluable is False
    assert "single harness" in harness.reason


def test_detects_existing_ai_configuration(tmp_path: Path) -> None:
    repo = _python_repo(tmp_path)
    (repo / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (repo / ".mcp.json").write_text("{}", encoding="utf-8")
    skills = repo / ".claude" / "skills" / "demo-skill"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: demo-skill\n---\n", encoding="utf-8")

    config = build_fit_profile(repo).existing_ai_config

    assert config.is_configured
    assert "AGENTS.md" in config.instruction_files
    assert ".mcp.json" in config.tool_config_files
    assert dict(config.capability_counts)["skills"] == 1


def test_node_repository_uses_the_lockfile_runner(tmp_path: Path) -> None:
    repo = tmp_path / "node"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "build": "tsc"}}), encoding="utf-8"
    )
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: 6.0\n", encoding="utf-8")

    inventory = discover_verification(repo)

    test_command = inventory.best("test")
    assert test_command is not None
    assert test_command.command == ("pnpm", "run", "test")


def test_malformed_manifests_warn_instead_of_raising(tmp_path: Path) -> None:
    repo = tmp_path / "broken"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("this is not toml {{{", encoding="utf-8")
    (repo / "package.json").write_text("{not json", encoding="utf-8")

    inventory = discover_verification(repo)

    assert not inventory.has_deterministic_verification
    assert any("pyproject.toml" in warning for warning in inventory.warnings)
    assert any("package.json" in warning for warning in inventory.warnings)


def test_empty_repository_is_reported_not_crashed(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()

    profile = build_fit_profile(repo)

    assert profile.schema == FIT_PROFILE_SCHEMA
    assert profile.is_fit_evaluable is False
    assert profile.verification.commands == ()


def test_missing_repository_path_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        build_fit_profile(tmp_path / "does-not-exist")


def test_profile_is_json_serializable_and_versioned(tmp_path: Path) -> None:
    payload = build_fit_profile(_python_repo(tmp_path)).to_dict()

    encoded = json.dumps(payload, sort_keys=True)

    assert json.loads(encoded)["schema"] == FIT_PROFILE_SCHEMA
    assert "is_fit_evaluable" in payload


def test_ctx_fit_subcommand_runs_without_spending(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _with_tests(_python_repo(tmp_path))

    exit_code = ctx_main(["fit", str(repo), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == FIT_PROFILE_SCHEMA
    assert payload["is_fit_evaluable"] is True


def test_ctx_fit_dry_run_states_cost_is_not_calculable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _python_repo(tmp_path)

    exit_code = ctx_main(["fit", str(repo), "--dry-run"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "No model was invoked and nothing was spent." in output
    # The product must never invent a cost it cannot derive: with no pricing
    # configured, the estimate is reported as unknown and no figure appears.
    assert "Estimated cost:    unknown" in output
    assert "$" not in output
