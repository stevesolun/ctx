"""Regressions for repository discovery: what CTX Fit claims it observed.

Every test here pins a case where the product previously stated an inference as
a fact — told a repository with a passing suite that it had no tests, awarded
points for an English word, or published a value that changed with the
filesystem. The product's own rule is that a Fit verdict must be falsifiable by
the user, so each of these asserts on the *claim*, not just the score.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import scan_repo
from ctx.cli.fit import cmd_fit, default_namespace
from ctx.fit.profile import build_fit_profile
from ctx.fit.readiness import score_readiness
from ctx.fit.verification import discover_verification


def _check(repo: Path, check_id: str):
    report = score_readiness(build_fit_profile(repo), repo)
    return next(item for item in report.checks if item.check_id == check_id)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _python_repo(root: Path) -> Path:
    _write(
        root / "pyproject.toml",
        "[project]\nname='demo'\nversion='0.1.0'\n\n[tool.pytest.ini_options]\n",
    )
    return root


# --------------------------------------------------------------------------
# Test discovery must see tests that exist (FITBUG-023, FITBUG-072).
# --------------------------------------------------------------------------


def test_tests_in_a_workspace_package_are_found(tmp_path: Path) -> None:
    """packages/*/tests/ is three levels down and is still a real test suite."""

    repo = _python_repo(tmp_path)
    _write(
        repo / "packages" / "alpha" / "tests" / "test_alpha.py", "def test_a():\n    assert True\n"
    )

    inventory = discover_verification(repo)

    assert inventory.test_files == ("packages/alpha/tests/",)
    assert inventory.has_deterministic_verification is True
    assert build_fit_profile(repo).is_fit_evaluable is True
    assert not any("no test files were found" in warning for warning in inventory.warnings)


def test_go_tests_beside_deeply_nested_code_are_found(tmp_path: Path) -> None:
    """Go keeps `_test.go` next to the package, which is routinely 3 dirs deep."""

    _write(tmp_path / "go.mod", "module example.com/x\n\ngo 1.22\n")
    _write(
        tmp_path / "internal" / "svc" / "user" / "user_test.go",
        'package user\n\nimport "testing"\n\nfunc TestHello(t *testing.T) {}\n',
    )

    inventory = discover_verification(tmp_path)

    assert inventory.test_files == ("internal/svc/user/user_test.go",)
    assert inventory.has_deterministic_verification is True


def test_reported_test_files_are_sorted_not_directory_ordered(tmp_path: Path) -> None:
    """The reported value must be a function of the repo, not of the filesystem.

    ``Path.glob`` yields ``os.scandir`` order, so the same commit reported a
    different file on APFS and on ext4 — in a field the profile documents as
    reproducible.
    """

    for name in ("zebra", "yak", "walrus", "test_zzz", "test_aaa", "test_mmm"):
        _write(tmp_path / f"test_{name}.py", "def test_x():\n    assert True\n")

    reported = discover_verification(tmp_path).test_files

    assert list(reported) == sorted(reported)
    assert reported[0] == "test_test_aaa.py"


def test_a_directory_of_fixtures_is_not_a_test_suite(tmp_path: Path) -> None:
    """`test/` holding only notes.txt is not material any runner could execute."""

    _write(tmp_path / "test" / "fixtures.txt", "notes\n")

    assert discover_verification(tmp_path).test_files == ()


# --------------------------------------------------------------------------
# Reproducibility of the emitted profile (FITBUG-024, FITBUG-074).
# --------------------------------------------------------------------------


def _k8s_repo(root: Path) -> Path:
    for name in ("k8s", "kubernetes", "helm", "charts"):
        _write(root / name / "manifest.yaml", "a\n")
    return root


def test_kubernetes_evidence_is_a_sorted_sequence_not_a_set_repr(tmp_path: Path) -> None:
    """A set's repr follows per-process hash order and must never be serialized."""

    stack = build_fit_profile(_k8s_repo(tmp_path)).stack
    kubernetes = next(item for item in stack["infrastructure"] if item["name"] == "kubernetes")

    assert kubernetes["evidence"] == [
        "directory: charts",
        "directory: helm",
        "directory: k8s",
        "directory: kubernetes",
    ]


def _profile_json_with_hash_seed(repo: Path, seed: str) -> str:
    script = (
        "import json,sys;"
        "from ctx.fit.profile import build_fit_profile;"
        "print(json.dumps(build_fit_profile(sys.argv[1]).to_dict(), sort_keys=True))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    )
    return result.stdout


def test_profile_json_is_identical_across_processes(tmp_path: Path) -> None:
    """The in-process comparison cannot see hash-order leakage.

    Python fixes the hash seed for a process lifetime, so building two profiles
    in one interpreter compares equal even when the output depends on hash
    order. Only separate processes with different seeds can detect it.
    """

    repo = _k8s_repo(tmp_path)

    outputs = {_profile_json_with_hash_seed(repo, seed) for seed in ("1", "2", "3", "4")}

    assert len(outputs) == 1


def test_equal_language_counts_break_ties_by_name(tmp_path: Path) -> None:
    """Two languages with the same file count must not be ordered by walk order."""

    for name in ("a.py", "b.py", "a.go", "b.go"):
        _write(tmp_path / name, "x\n")

    signals = scan_repo.scan_directory(str(tmp_path))
    baseline = [
        item["name"] for item in scan_repo.detect_stack(str(tmp_path), signals)["languages"]
    ]

    for order in (
        [("a.py", ".py"), ("b.py", ".py"), ("a.go", ".go"), ("b.go", ".go")],
        [("a.go", ".go"), ("b.go", ".go"), ("a.py", ".py"), ("b.py", ".py")],
    ):
        permuted = dict(signals)
        permuted["files"] = order
        languages = scan_repo.detect_stack(str(tmp_path), permuted)["languages"]
        assert [item["name"] for item in languages] == baseline == ["go", "python"]


# --------------------------------------------------------------------------
# Verification discovery must not describe files it never read
# (FITBUG-049, FITBUG-050, FITBUG-051, FITBUG-071).
# --------------------------------------------------------------------------


def test_tox_ini_without_pytest_does_not_claim_pytest_configuration(tmp_path: Path) -> None:
    """tox.ini is also where projects park [flake8]; existence proves nothing."""

    _write(tmp_path / "tox.ini", "[flake8]\nmax-line-length = 100\n")
    _write(tmp_path / "tests" / "test_a.py", "def test_a():\n    assert True\n")

    inventory = discover_verification(tmp_path)

    assert not any(command.source == "tox.ini" for command in inventory.commands)
    assert not any(
        "tox.ini declares pytest" in line
        for command in inventory.commands
        for line in command.evidence
    )


def test_tox_ini_declaring_pytest_is_still_recognised(tmp_path: Path) -> None:
    _write(tmp_path / "tox.ini", "[tox]\nenvlist = py311\n\n[pytest]\naddopts = -q\n")

    command = discover_verification(tmp_path).best("test")

    assert command is not None
    assert command.source == "tox.ini"
    assert command.confidence == "high"


def test_typecheck_target_exists_on_disk(tmp_path: Path) -> None:
    """`mypy src` in a flat-layout repo fails with "Cannot read file 'src'"."""

    _write(
        tmp_path / "pyproject.toml",
        "[project]\nname='mypkg'\nversion='0.1'\n\n[tool.mypy]\nstrict = true\n",
    )
    _write(tmp_path / "mypkg" / "__init__.py", "def f():\n    return 1\n")

    command = discover_verification(tmp_path).best("typecheck")

    assert command is not None
    assert command.command == ("python", "-m", "mypy", "mypkg")
    assert (tmp_path / command.command[-1]).is_dir()


def test_npm_default_test_script_is_not_verification(tmp_path: Path) -> None:
    """`npm init -y` writes a script whose only job is to say there are no tests."""

    _write(
        tmp_path / "package.json",
        json.dumps(
            {
                "name": "n",
                "version": "1.0.0",
                "scripts": {"test": 'echo "Error: no test specified" && exit 1'},
            }
        ),
    )
    _write(tmp_path / "test" / "fixtures.txt", "notes\n")

    inventory = discover_verification(tmp_path)

    assert inventory.declares_test_command is False
    assert build_fit_profile(tmp_path).is_fit_evaluable is False


def test_a_real_node_runner_keeps_high_confidence(tmp_path: Path) -> None:
    _write(
        tmp_path / "package.json",
        json.dumps({"name": "n", "version": "1.0.0", "scripts": {"test": "jest --ci"}}),
    )

    command = discover_verification(tmp_path).best("test")

    assert command is not None
    assert command.confidence == "high"


def test_an_empty_pyproject_is_not_reported_as_unparseable(tmp_path: Path) -> None:
    """A zero-byte pyproject.toml is valid TOML and needs no fixing."""

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    warnings = discover_verification(tmp_path).warnings

    assert not any("could not be parsed" in warning for warning in warnings)
    assert not any("pyproject.toml" in warning for warning in warnings)


def test_an_invalid_pyproject_still_says_so(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project\nname = broken\n")

    warnings = discover_verification(tmp_path).warnings

    assert any("pyproject.toml is not valid TOML" in warning for warning in warnings)


# --------------------------------------------------------------------------
# "Can this repository be evaluated at all" must match the repository
# (FITBUG-026, FITBUG-027, FITBUG-058, FITBUG-059).
#
# This verdict gates the whole product, so each case below asserts on the
# sentence `ctx fit` prints to the user, not only on a helper's return value.
# --------------------------------------------------------------------------

_CAN_BE_EVALUATED = "This repository can be evaluated"
_CANNOT_BE_EVALUATED = "This repository cannot yet be evaluated honestly"


def _fit_output(repo: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """What a bare `ctx fit` actually prints about this repository."""

    assert cmd_fit(default_namespace(str(repo))) == 0
    return capsys.readouterr().out


def _cargo_crate(root: Path) -> Path:
    """A crate laid out the way `cargo new --lib` lays one out.

    The unit test lives inline in the module it tests, behind `#[cfg(test)]`.
    `cargo test` compiles and runs it; no file here is named `*_test.rs` and
    there is no `tests/` directory, because the dominant Rust convention needs
    neither.
    """

    _write(
        root / "Cargo.toml",
        '[package]\nname = "widget"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
    )
    _write(
        root / "src" / "lib.rs",
        "pub fn add(a: i32, b: i32) -> i32 {\n"
        "    a + b\n"
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "\n"
        "    #[test]\n"
        "    fn add_sums_two_numbers() {\n"
        "        assert_eq!(add(2, 2), 4);\n"
        "    }\n"
        "}\n",
    )
    return root


def test_a_crate_with_inline_cfg_test_tests_is_evaluable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`#[cfg(test)] mod tests` is how Rust writes unit tests, and no filename shows it."""

    repo = _cargo_crate(tmp_path)

    inventory = discover_verification(repo)

    assert inventory.test_files == ("src/lib.rs",)
    assert inventory.has_deterministic_verification is True
    assert not any("no test files were found" in warning for warning in inventory.warnings)
    assert build_fit_profile(repo).is_fit_evaluable is True
    assert _CAN_BE_EVALUATED in _fit_output(repo, capsys)


def test_a_crate_with_no_tests_at_all_is_still_not_evaluable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inline detection must read code, not prose: a doc example is not a suite."""

    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "widget"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    _write(
        tmp_path / "src" / "lib.rs",
        "/// Tests for this crate live behind `#[cfg(test)]`, once somebody writes them.\n"
        "///\n"
        "/// ```ignore\n"
        "/// #[cfg(test)]\n"
        "/// mod tests {}\n"
        "/// ```\n"
        "pub fn add(a: i32, b: i32) -> i32 {\n"
        "    a + b\n"
        "}\n",
    )

    assert discover_verification(tmp_path).test_files == ()
    assert build_fit_profile(tmp_path).is_fit_evaluable is False
    assert _CANNOT_BE_EVALUATED in _fit_output(tmp_path, capsys)


def test_tests_belonging_to_a_dependency_are_not_this_repositorys_tests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An installed node_modules is somebody else's suite, however test-shaped."""

    _write(
        tmp_path / "package.json",
        json.dumps(
            {
                "name": "app",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "^29.0.0"},
            }
        ),
    )
    _write(tmp_path / "index.js", "module.exports = () => 1;\n")
    _write(
        tmp_path / "node_modules" / "left-pad" / "package.json",
        json.dumps({"name": "left-pad", "version": "1.3.0", "main": "index.js"}),
    )
    _write(
        tmp_path / "node_modules" / "left-pad" / "index.test.js",
        "test('pads', () => { expect(1).toBe(1); });\n",
    )

    inventory = discover_verification(tmp_path)

    assert inventory.declares_test_command is True
    assert inventory.test_files == ()
    assert build_fit_profile(tmp_path).is_fit_evaluable is False
    assert _CANNOT_BE_EVALUATED in _fit_output(tmp_path, capsys)


def test_vendored_rust_sources_are_not_this_repositorys_tests(tmp_path: Path) -> None:
    """Reading file contents must not reach where the directory walk refuses to."""

    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "widget"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    _write(tmp_path / "src" / "lib.rs", "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n")
    _cargo_crate(tmp_path / "vendor" / "serde")

    assert discover_verification(tmp_path).test_files == ()
    assert build_fit_profile(tmp_path).is_fit_evaluable is False


def test_a_byte_order_mark_does_not_erase_every_pyproject_command(tmp_path: Path) -> None:
    """One invisible character used to delete pytest, ruff, mypy and the build at once."""

    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n\n'
        "[tool.pytest.ini_options]\n"
        'addopts = "-q"\n\n'
        "[tool.ruff]\n"
        "line-length = 100\n\n"
        "[tool.mypy]\n"
        "strict = true\n",
        encoding="utf-8-sig",
    )
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "tests" / "test_demo.py", "def test_ok():\n    assert True\n")

    inventory = discover_verification(tmp_path)

    assert not any("pyproject.toml" in warning for warning in inventory.warnings)
    test_command = inventory.best("test")
    assert test_command is not None
    assert test_command.source == "pyproject.toml [tool.pytest]"
    assert test_command.confidence == "high"
    assert {command.kind for command in inventory.commands} == {
        "test",
        "lint",
        "typecheck",
        "build",
    }


def test_a_file_that_is_not_utf8_is_reported_as_such_not_as_bad_toml(tmp_path: Path) -> None:
    """Tolerating a BOM must not turn an undecodable file into a silent empty table."""

    (tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = '\xff\xfe not utf8'\n")

    warnings = discover_verification(tmp_path).warnings

    assert any("pyproject.toml is not valid UTF-8" in warning for warning in warnings)


def test_setup_cfg_pytest_configuration_is_recognised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[tool:pytest] in setup.cfg is pytest config, and the tests are not in tests/."""

    _write(
        tmp_path / "setup.cfg",
        "[metadata]\nname = legacy\nversion = 0.1.0\n\n"
        "[tool:pytest]\ntestpaths = t\naddopts = -q\n",
    )
    _write(tmp_path / "t" / "test_a.py", "def test_a():\n    assert True\n")

    command = discover_verification(tmp_path).best("test")

    assert command is not None
    assert command.source == "setup.cfg [tool:pytest]"
    assert command.confidence == "high"
    assert not any("no pytest config" in line for line in command.evidence)
    assert build_fit_profile(tmp_path).is_fit_evaluable is True
    assert _CAN_BE_EVALUATED in _fit_output(tmp_path, capsys)


def test_a_byte_order_mark_does_not_hide_setup_cfg_pytest_configuration(tmp_path: Path) -> None:
    """A BOM'd setup.cfg is what a Windows editor produces, and pytest reads it fine."""

    (tmp_path / "setup.cfg").write_text(
        "[tool:pytest]\naddopts = -q\n",
        encoding="utf-8-sig",
    )
    _write(tmp_path / "t" / "test_a.py", "def test_a():\n    assert True\n")

    command = discover_verification(tmp_path).best("test")

    assert command is not None
    assert command.source == "setup.cfg [tool:pytest]"


def test_setup_cfg_without_pytest_does_not_claim_pytest_configuration(tmp_path: Path) -> None:
    """Every setup.cfg has a [metadata] section; that is not a pytest declaration."""

    _write(
        tmp_path / "setup.cfg",
        "[metadata]\nname = legacy\n\n[flake8]\nmax-line-length = 100\n",
    )
    _write(tmp_path / "t" / "test_a.py", "def test_a():\n    assert True\n")

    inventory = discover_verification(tmp_path)

    assert not any(command.source.startswith("setup.cfg") for command in inventory.commands)
    assert inventory.declares_test_command is False


# --------------------------------------------------------------------------
# Readiness checks must assert only what they verified
# (FITBUG-041, 042, 043, 044, 045, 066, 067).
# --------------------------------------------------------------------------


def test_the_word_latest_does_not_earn_the_verification_points(tmp_path: Path) -> None:
    repo = _python_repo(tmp_path)
    _write(repo / "tests" / "test_a.py", "def test_a():\n    assert True\n")
    _write(repo / "AGENTS.md", "# Guide\n\nAlways use the latest dependencies.\n")

    result = _check(repo, "I2")

    assert result.state == "fail"
    assert result.earned == 0


def test_naming_the_test_command_still_earns_the_points(tmp_path: Path) -> None:
    repo = _python_repo(tmp_path)
    _write(repo / "tests" / "test_a.py", "def test_a():\n    assert True\n")
    _write(repo / "AGENTS.md", "# Guide\n\nVerify a change with `pytest -q`.\n")

    result = _check(repo, "I2")

    assert result.state == "pass"
    assert "pytest" in result.evidence[0]


def _lint_only_repo(root: Path, provider: str) -> Path:
    repo = _python_repo(root)
    _write(repo / "tests" / "test_a.py", "def test_a():\n    assert True\n")
    if provider == "github":
        _write(
            repo / ".github" / "workflows" / "ci.yml",
            "name: lint\non: [push]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo lint\n",
        )
    else:
        _write(repo / ".gitlab-ci.yml", "lint:\n  script:\n    - echo lint\n")
    return repo


def test_ci_points_do_not_depend_on_the_ci_provider(tmp_path: Path) -> None:
    """Two byte-equivalent lint-only repos must not score differently.

    C2 used to return not_applicable for anything but GitHub Actions, and
    not_applicable leaves the denominator — so deleting a GitHub workflow and
    writing an equivalent GitLab one *raised* the readiness score.
    """

    github = score_readiness(
        build_fit_profile(_lint_only_repo(tmp_path / "gh", "github")), tmp_path / "gh"
    )
    gitlab = score_readiness(
        build_fit_profile(_lint_only_repo(tmp_path / "gl", "gitlab")), tmp_path / "gl"
    )

    assert github.assessable == gitlab.assessable
    assert github.score == gitlab.score


def test_a_workflow_that_only_installs_pytest_does_not_run_tests(tmp_path: Path) -> None:
    _write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        "name: lint\non: [push]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: pip install ruff pytest\n      - run: ruff check .\n",
    )

    result = _check(tmp_path, "C2")

    assert result.state == "fail"


def test_a_workflow_that_runs_pytest_passes(tmp_path: Path) -> None:
    _write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: pip install pytest\n      - run: python -m pytest -q\n",
    )

    result = _check(tmp_path, "C2")

    assert result.state == "pass"


@pytest.mark.parametrize(
    "step",
    [
        "npm ci && npm test",
        "pip install -e . && pytest -q",
        "yarn install --frozen-lockfile; yarn test",
        "uv sync && python -m pytest -q",
    ],
)
def test_one_step_that_installs_and_then_tests_still_runs_tests(tmp_path: Path, step: str) -> None:
    """The install veto is per command; a chained step contains both answers.

    Rejecting the whole line because it mentions an installer told the most
    ordinary Node workflow there is that its CI does not run the suite, and
    pointed the user at work they had already done.
    """

    repo = tmp_path / step.split()[0]
    _write(
        repo / ".github" / "workflows" / "ci.yml",
        "name: ci\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        f"    steps:\n      - uses: actions/checkout@v4\n      - run: {step}\n",
    )

    result = _check(repo, "C2")

    assert result.state == "pass"
    assert result.earned == result.possible


def test_a_cache_key_named_after_pytest_is_not_a_test_run(tmp_path: Path) -> None:
    """`key:` is data. Quoting it as proof the suite runs is a fabricated fact."""

    _write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        "name: lint\non: [push]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/cache@v4\n        with:\n"
        "          path: .cache\n          key: pytest-cache-v1\n"
        "      - run: ruff check .\n",
    )

    result = _check(tmp_path, "C2")

    assert result.state == "fail"


def test_a_subdirectory_of_a_git_repository_is_under_version_control(tmp_path: Path) -> None:
    """Advising `git init` inside an existing repo is worse than the problem."""

    (tmp_path / ".git").mkdir()
    backend = tmp_path / "backend"
    _write(backend / "pyproject.toml", "[project]\nname='b'\nversion='1'\n")

    result = _check(backend, "S1")

    assert result.state == "pass"
    assert result.earned == result.possible
    # Naming the repository is the point: ".." told the user nothing they
    # could go and check.
    assert tmp_path.name in result.evidence[0]


def test_no_git_anywhere_above_still_fails(tmp_path: Path) -> None:
    repo = tmp_path / "loose"
    _write(repo / "main.py", "x = 1\n")

    assert _check(repo, "S1").state == "fail"


def test_envrc_in_gitignore_does_not_protect_an_unignored_env(tmp_path: Path) -> None:
    """The substring test blessed exactly the situation the check exists to catch."""

    _write(tmp_path / ".gitignore", ".envrc\n")
    _write(tmp_path / ".env", "SECRET_KEY=hunter2\n")

    result = _check(tmp_path, "S2")

    assert result.state == "fail"


@pytest.mark.parametrize("pattern", [".env", ".env*", "**/.env"])
def test_patterns_that_really_ignore_env_still_pass(tmp_path: Path, pattern: str) -> None:
    root = tmp_path / pattern.replace("*", "star").replace("/", "_")
    _write(root / ".gitignore", f"{pattern}\n")
    _write(root / ".env", "SECRET_KEY=hunter2\n")

    assert _check(root, "S2").state == "pass"


def test_readme_choice_is_sorted_not_directory_ordered(tmp_path: Path, monkeypatch) -> None:
    """Which README is published must not depend on ``os.scandir`` order.

    The adverse order is forced rather than hoped for: whether APFS happens to
    hand back the sorted one is exactly the machine-dependence under test.
    """

    _write(tmp_path / "README.txt", "a\n")
    _write(tmp_path / "README.adoc", "b\n")
    real_glob = Path.glob

    def reverse_ordered_glob(self, pattern, *args, **kwargs):
        return iter(sorted(real_glob(self, pattern, *args, **kwargs), reverse=True))

    monkeypatch.setattr(Path, "glob", reverse_ordered_glob)

    result = _check(tmp_path, "X2")

    assert result.evidence[0].startswith("README.adoc")


def test_an_unpinned_requirements_file_is_not_a_lockfile(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project]\nname='r'\nversion='1'\n")
    _write(tmp_path / "requirements.txt", "requests\nflask\n")

    result = _check(tmp_path, "E1")

    assert result.state == "partial"
    assert result.earned < result.possible


def test_a_fully_pinned_requirements_file_counts(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project]\nname='r'\nversion='1'\n")
    _write(tmp_path / "requirements.txt", "requests==2.31.0\nflask==3.0.0\n")

    assert _check(tmp_path, "E1").state == "pass"


# --------------------------------------------------------------------------
# Stack detection (FITBUG-053, 054, 068, 073) and CLI guidance (FITBUG-055).
# --------------------------------------------------------------------------


def _stack(repo: Path) -> dict:
    signals = scan_repo.scan_directory(str(repo))
    return scan_repo.detect_stack(str(repo), signals)


def test_a_javascript_workspace_is_detected_as_a_monorepo(tmp_path: Path) -> None:
    """A nested package.json used to overwrite the root one that holds the key."""

    _write(
        tmp_path / "package.json",
        json.dumps({"name": "root", "private": True, "workspaces": ["packages/*"]}),
    )
    _write(tmp_path / "packages" / "alpha" / "package.json", json.dumps({"name": "alpha"}))
    _write(tmp_path / "index.js", "x\n")

    assert _stack(tmp_path)["monorepo"] is True


def test_declared_workspace_members_are_listed_wherever_they_live(tmp_path: Path) -> None:
    """`workspaces: ["frontend/*"]` names its members; the count must match.

    Listing only members under a conventional parent (packages/, apps/, ...)
    made X1 report "monorepo with 0 workspace packages" for a monorepo whose
    root manifest names two — a count the file on disk contradicts.
    """

    _write(
        tmp_path / "package.json",
        json.dumps({"name": "root", "private": True, "workspaces": ["frontend/*"]}),
    )
    for name in ("api", "web"):
        _write(tmp_path / "frontend" / name / "package.json", json.dumps({"name": name}))
    _write(tmp_path / "index.js", "x\n")

    stack = _stack(tmp_path)

    assert stack["monorepo"] is True
    assert stack["workspace_packages"] == ["frontend/api", "frontend/web"]
    assert "2 workspace packages" in _check(tmp_path, "X1").evidence[0]


def test_a_python_workspace_is_detected_as_a_monorepo(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        "[project]\nname='mono'\nversion='0.1'\n\n[tool.uv.workspace]\nmembers=['packages/*']\n",
    )
    for name in ("alpha", "beta"):
        _write(
            tmp_path / "packages" / name / "pyproject.toml",
            f"[project]\nname='{name}'\nversion='0.1'\n",
        )

    stack = _stack(tmp_path)

    assert stack["monorepo"] is True
    assert stack["workspace_packages"] == ["packages/alpha", "packages/beta"]

    result = _check(tmp_path, "X1")
    assert result.state == "partial"


def test_a_single_package_repository_is_not_called_a_monorepo(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project]\nname='solo'\nversion='0.1'\n")
    _write(tmp_path / "solo" / "__init__.py", "x = 1\n")

    assert _stack(tmp_path)["monorepo"] is False


@pytest.mark.parametrize(
    "manifest",
    ['{"dependencies": null}', '["dependencies"]', '{"dependencies": [1, 2]}', "5"],
)
def test_a_malformed_package_manifest_does_not_crash_the_scan(
    tmp_path: Path, manifest: str
) -> None:
    """Valid JSON of the wrong shape used to escape as an unhandled traceback."""

    _write(tmp_path / "package.json", manifest)
    _write(tmp_path / "a.js", "x\n")

    stack = _stack(tmp_path)

    assert stack["monorepo"] is False
    assert build_fit_profile(tmp_path).stack["project_type"] is not None


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read any directory"
)
def test_an_unreadable_subtree_is_recorded_rather_than_ignored(tmp_path: Path) -> None:
    """Exit 0 plus an empty profile must not be how "could not look" is reported."""

    _write(tmp_path / "top.py", "y = 2\n")
    locked = tmp_path / "locked"
    _write(locked / "mod.py", "x = 1\n")
    locked.chmod(0o000)
    try:
        signals = scan_repo.scan_directory(str(tmp_path))
    finally:
        locked.chmod(0o755)

    assert signals["unreadable_dirs"] == ["locked"]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read any directory"
)
def test_an_unreadable_subtree_is_named_in_the_profile(tmp_path: Path) -> None:
    """The scan's own walk was silent too, so the profile still said nothing.

    "We were denied here" and "there is nothing here" are different answers,
    and only one of them is checkable by the user.
    """

    _write(tmp_path / "pyproject.toml", "[project]\nname='d'\nversion='1'\n")
    locked = tmp_path / "locked"
    _write(locked / "test_hidden.py", "def test_x():\n    assert True\n")
    locked.chmod(0o000)
    try:
        profile = build_fit_profile(tmp_path)
        inventory = discover_verification(tmp_path)
    finally:
        locked.chmod(0o755)

    assert any("could not read" in w and "locked" in w for w in inventory.warnings)
    assert any("could not read" in w and "locked" in w for w in profile.warnings)


def test_the_recommender_hint_names_commands_that_still_exist(monkeypatch, capsys) -> None:
    """`ctx-scan-repo` was telling users to run three retired console scripts."""

    monkeypatch.setattr(scan_repo, "_shared_recommendations", lambda profile: [])
    scan_repo._print_recommendations("/tmp/does-not-matter", {"languages": [], "frameworks": []})

    printed = capsys.readouterr().out

    assert "ctx-mcp-fetch" not in printed
    assert "ctx-mcp-add" not in printed
    assert "python -m mcp_fetch" in printed
    assert "python -m mcp_add" in printed


# --------------------------------------------------------------------------
# "Does this repository have tests that can judge an agent?" is ONE question,
# and it is verification's (ARCH-2). It used to be answered twice, at two
# fidelities, by two modules, and the two answers printed contradicting each
# other four lines apart on one screen of `ctx fit` output.
# --------------------------------------------------------------------------


def test_test_files_that_declare_no_test_case_are_not_executable_tests(tmp_path: Path) -> None:
    """A file named like a test that declares none is a runner running on nothing.

    This is the deliberate product change in ARCH-2: evaluability is now decided
    by what the test material *says*, not by what it is named. `pytest -q`
    against an empty `tests/test_demo.py` collects zero tests and exits happy,
    which cannot tell a configuration that solved a task from one that claimed
    to -- the one inference this product may never make.
    """

    repo = _python_repo(tmp_path)
    _write(repo / "tests" / "test_demo.py", "")

    inventory = discover_verification(repo)

    assert inventory.declares_test_command is True
    assert inventory.test_files == ("tests/",)  # named like tests, and found
    assert inventory.has_executable_tests is False
    assert inventory.has_deterministic_verification is False
    assert build_fit_profile(repo).is_fit_evaluable is False


def test_one_screen_never_both_blocks_on_tests_and_promises_evaluation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ARCH-2 friction itself, asserted on the printed page.

    `ctx fit` printed, four lines apart on this very repository: a Blocking
    entry saying "Tests are runnable ... fix: Add a test suite", and "This
    repository can be evaluated: it has deterministic tests". Both sentences
    answer the same question, so at most one of them may ever appear.
    """

    repo = _python_repo(tmp_path)
    _write(repo / "tests" / "test_demo.py", "")

    printed = _fit_output(repo, capsys)

    assert _CAN_BE_EVALUATED not in printed
    assert _CANNOT_BE_EVALUATED in printed
    assert "Tests are runnable" in printed  # still blocking, and now consistently


def test_a_real_test_case_keeps_the_repository_evaluable(tmp_path: Path) -> None:
    """The other half of the change: tightening must not refuse a real suite."""

    repo = _python_repo(tmp_path)
    _write(repo / "tests" / "test_demo.py", "def test_ok():\n    assert True\n")

    inventory = discover_verification(repo)

    assert inventory.has_executable_tests is True
    assert inventory.has_deterministic_verification is True
    assert inventory.test_declaration == "tests/test_demo.py"


def test_inline_rust_tests_are_read_the_same_way_twice(tmp_path: Path) -> None:
    """Discovery and the declaration scan must not disagree about Rust.

    Finding `src/lib.rs` with one spelling of `#[cfg(test)]` and then rejecting
    it with a stricter second spelling would report the same crate as having
    tests and as having none. rustfmt writes it tight; a human may not.
    """

    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "widget"\nversion = "0.1.0"\nedition = "2021"\n',
    )
    _write(
        tmp_path / "src" / "lib.rs",
        "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
        "\n"
        "#[ cfg ( test ) ]\n"
        "mod tests {\n"
        "    #[ test ]\n"
        "    fn adds() { assert_eq!(add(2, 2), 4); }\n"
        "}\n",
    )

    inventory = discover_verification(tmp_path)

    assert inventory.test_files == ("src/lib.rs",)
    assert inventory.has_executable_tests is True
    assert build_fit_profile(tmp_path).is_fit_evaluable is True


def test_a_repository_inside_a_hidden_directory_still_has_its_tests_read(
    tmp_path: Path,
) -> None:
    """A hidden directory *above* the repository is not a hidden directory in it.

    The declaration scan tested every component of each absolute path for a
    leading dot, so a checkout under `~/.local/src/app` or a worktree under
    `.worktrees/` had every one of its test files skipped -- and now that the
    same answer gates evaluability, a whole real suite would be refused.
    """

    repo = _python_repo(tmp_path / ".worktrees" / "app")
    _write(repo / "tests" / "test_demo.py", "def test_ok():\n    assert True\n")

    inventory = discover_verification(repo)

    assert inventory.has_executable_tests is True
    assert build_fit_profile(repo).is_fit_evaluable is True
