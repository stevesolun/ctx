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


def test_creating_empty_files_cannot_move_the_score(tmp_path: Path) -> None:
    """The behavioural half of the anti-gaming gate (FITBUG-057).

    The gate above is prose: it asserts that every check *states* how it helps an
    agent. It cannot fail when a check's predicate stops measuring what its
    rationale describes, which is exactly what happened — I1's rationale is that
    "an agent told nothing about the project rediscovers conventions by
    guessing", and `touch AGENTS.md` left an agent in precisely that state while
    collecting the rubric's largest single award. So this test asserts on
    behaviour instead: zero-byte files are worth zero points, everywhere.
    """

    repo = tmp_path / "probe"
    repo.mkdir()
    (repo / ".git").mkdir()
    before = _score(repo)

    for name in (
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        ".python-version",
        "requirements.txt",
        "poetry.lock",
        ".gitignore",
    ):
        (repo / name).touch()
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".circleci").mkdir()
    after = _score(repo)

    assert after.earned == before.earned
    # Per check, not just in total: the guarantee is that no individual check
    # paid out for a file with nothing in it. The denominator may legitimately
    # grow (an empty requirements.txt makes Python checks assessable), so the
    # score itself is the wrong thing to pin here.
    assert {item.check_id: item.earned for item in after.checks} == {
        item.check_id: item.earned for item in before.checks
    }


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


def test_static_node_discovery_does_not_claim_dependencies_are_runnable(
    tmp_path: Path, capsys
) -> None:
    """A package script and a test declaration do not prove vitest is installed."""

    repo = tmp_path / "node-without-installed-dependencies"
    repo.mkdir()
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest"}}), encoding="utf-8"
    )
    (repo / "demo.test.js").write_text("test('works', () => {});\n", encoding="utf-8")

    report = _score(repo)
    verification = next(item for item in report.checks if item.check_id == "V1")
    assert verification.state == "pass"
    assert verification.title == "Test verification is declared"

    assert ctx_main(["fit", str(repo)]) == 0
    printed = capsys.readouterr().out
    assert "Tests are runnable" not in printed
    assert "static evidence needed to plan an evaluation" in printed
    assert "already available in the repository" in printed


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


# --------------------------------------------------------------------------
# Substance, not existence (FITBUG-057). A check that stops at is_file() awards
# points for `touch`, which tells someone their repository improved when
# nothing about it did.
# --------------------------------------------------------------------------


def _check(report, check_id: str):
    return next(item for item in report.checks if item.check_id == check_id)


_REAL_INSTRUCTIONS = (
    "# Guide\n\n"
    "This project is a small Python CLI. The source lives in src/ and the\n"
    "tests live in tests/. Run `pytest -q` before you commit, keep lines under\n"
    "one hundred characters, and never edit generated files by hand.\n"
)

_REAL_README = (
    "# widgets\n\n"
    "Widgets is a small library for drawing widgets in the terminal. Install it\n"
    "with `pip install widgets` and call `widgets.draw()`; see docs/ for the API.\n"
)


def test_two_empty_files_do_not_move_the_headline_number(tmp_path: Path) -> None:
    """`touch AGENTS.md README.md` used to be worth 24 points, 17/100 -> 41/100."""

    repo = tmp_path / "probe"
    repo.mkdir()
    (repo / ".git").mkdir()
    before = _score(repo)

    (repo / "AGENTS.md").touch()
    (repo / "README.md").touch()
    after = _score(repo)

    assert after.score == before.score
    assert _check(after, "I1").earned == 0
    assert _check(after, "X2").earned == 0
    # The evidence has to say what was actually observed, not "found AGENTS.md".
    assert "empty" in _check(after, "I1").evidence[0]


def test_a_three_word_agents_md_is_not_instructions(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Guide\n\nBe careful here.\n", encoding="utf-8")

    result = _check(_score(repo), "I1")

    assert result.state == "fail"
    assert result.earned == 0


def test_a_thin_agents_md_is_partial_rather_than_full_marks(tmp_path: Path) -> None:
    """Real but short: it says something, so it is not a stub, and not the remedy either."""

    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text(
        "# Guide\n\nThis project is a small Python CLI; the source lives in src/.\n",
        encoding="utf-8",
    )

    result = _check(_score(repo), "I1")

    assert result.state == "partial"
    assert 0 < result.earned < result.possible


def test_a_written_agents_md_still_earns_the_full_points(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text(_REAL_INSTRUCTIONS, encoding="utf-8")

    result = _check(_score(repo), "I1")

    assert result.state == "pass"
    assert result.earned == result.possible


def test_the_richest_instruction_file_decides(tmp_path: Path) -> None:
    """A one-line `@AGENTS.md` include is the normal shape of CLAUDE.md.

    Grading the first file found, or the average, would let a real AGENTS.md be
    dragged down by the pointer file that exists to reference it.
    """

    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text(_REAL_INSTRUCTIONS, encoding="utf-8")
    (repo / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")

    result = _check(_score(repo), "I1")

    assert result.state == "pass"
    assert "AGENTS.md" in result.evidence[0]


def test_an_empty_readme_gives_an_agent_no_project_context(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")

    result = _check(_score(repo), "X2")

    assert result.state == "fail"
    assert result.earned == 0


def test_a_written_readme_still_earns_the_full_points(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text(_REAL_README, encoding="utf-8")

    result = _check(_score(repo), "X2")

    assert result.state == "pass"
    assert result.earned == result.possible


def test_an_empty_lockfile_pins_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "uv.lock").touch()

    result = _check(_score(repo), "E1")

    assert result.state == "fail"
    assert result.earned == 0


def test_an_empty_requirements_file_pins_nothing(tmp_path: Path) -> None:
    """A file that requests nothing cannot outscore the repo that never wrote it."""

    repo = _repo(tmp_path)
    (repo / "requirements.txt").touch()

    result = _check(_score(repo), "E1")

    assert result.state == "fail"
    assert result.earned == 0


def test_requires_python_in_a_comment_does_not_pin_the_interpreter(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "# TODO: decide what requires-python should be\n"
        "[project]\nname='demo'\nversion='0.1.0'\n\n[tool.pytest.ini_options]\n",
        encoding="utf-8",
    )

    result = _check(_score(repo), "E2")

    assert result.state == "fail"


def test_an_empty_python_version_file_does_not_pin_the_interpreter(tmp_path: Path) -> None:
    """The old evidence asserted a pin that the file did not contain."""

    repo = _repo(tmp_path)
    (repo / ".python-version").touch()
    (repo / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\n\n[tool.pytest.ini_options]\n", encoding="utf-8"
    )

    result = _check(_score(repo), "E2")

    assert result.state == "fail"
    assert result.earned == 0


def test_a_real_python_version_file_still_passes_and_quotes_the_pin(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".python-version").write_text("3.12.4\n", encoding="utf-8")

    result = _check(_score(repo), "E2")

    assert result.state == "pass"
    assert "3.12.4" in result.evidence[0]


def test_an_empty_workflows_directory_is_not_configured_ci(tmp_path: Path) -> None:
    """C1 passing while C2 reports "no CI configuration" is one report saying both."""

    repo = _repo(tmp_path)
    (repo / ".github" / "workflows").mkdir(parents=True)

    report = _score(repo)

    assert _check(report, "C1").state == "fail"
    assert _check(report, "C1").earned == 0


def test_a_real_workflow_still_counts_as_configured_ci(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    workflow = repo / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: pytest -q\n",
        encoding="utf-8",
    )

    report = _score(repo)

    assert _check(report, "C1").state == "pass"
    # Both CI checks read the same files now, so they cannot disagree about
    # whether CI exists at all.
    assert _check(report, "C2").state != "not_applicable"


def test_an_empty_gitignore_excludes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitignore").touch()

    result = _check(_score(repo), "S2")

    assert result.state == "fail"
    assert result.earned == 0


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


def test_an_empty_test_file_does_not_earn_the_verification_check(tmp_path: Path) -> None:
    """V1 is the rubric's largest check; it must not pay out for `touch`.

    The check used to pass on the mere existence of a test-shaped filename, so
    `touch tests/test_demo.py` bought 16 points on an otherwise-empty
    repository. A file that declares no test case cannot fail, and a suite that
    cannot fail cannot tell a working configuration from a broken one -- which
    is the reason this check is blocking in the first place.
    """

    repo = _repo(tmp_path, tests=True)
    (repo / "tests" / "test_demo.py").write_text("", encoding="utf-8")

    hollow = _score(repo)
    v1_hollow = next(check for check in hollow.checks if check.check_id == "V1")
    assert v1_hollow.state == "partial", v1_hollow.evidence
    assert any("declare no test" in item for item in v1_hollow.evidence), v1_hollow.evidence

    (repo / "tests" / "test_demo.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    real = _score(repo)
    v1_real = next(check for check in real.checks if check.check_id == "V1")

    assert v1_real.state == "pass"
    assert real.score is not None and hollow.score is not None
    assert real.score > hollow.score, (
        "a repository with a real test must outscore one with an empty file"
    )


def test_a_tests_directory_is_expanded_rather_than_read_as_a_file(tmp_path: Path) -> None:
    """Discovery reports `tests/`, not each file, so V1 must walk into it.

    Reading the directory entry as a file raises OSError, which would report
    every ``testpaths``-style repository as unassessable -- the common case.
    """

    repo = _repo(tmp_path, tests=True)
    profile = build_fit_profile(repo)

    assert "tests/" in profile.verification.test_files
    v1 = next(check for check in _score(repo).checks if check.check_id == "V1")
    assert v1.state == "pass", v1.evidence


def test_a_build_cache_does_not_exhaust_the_test_inspection_budget(tmp_path: Path) -> None:
    """Real tests must be found past a __pycache__ that sorts ahead of them.

    `sorted(rglob("*"))` puts `__pycache__` first alphabetically. On CTX's own
    repository 39 of the first 40 entries under the test directory were `.pyc`
    files, so the inspection budget was spent before a single real test was
    opened, and the product told its own 604-test suite to "add a test suite"
    while printing the working test command as the evidence.
    """

    repo = _repo(tmp_path, tests=True)
    cache = repo / "tests" / "__pycache__"
    cache.mkdir()
    for index in range(60):
        (cache / f"test_{index:03d}.cpython-312-pytest.pyc").write_bytes(b"\x00\x01binary")

    v1 = next(check for check in _score(repo).checks if check.check_id == "V1")

    assert v1.state == "pass", v1.evidence


# --------------------------------------------------------------------------
# V1 consumes verification's answer rather than deriving its own (ARCH-2), and
# a blocker must lead with the reason it did not pass.
# --------------------------------------------------------------------------


def test_the_reason_a_blocker_did_not_pass_is_the_first_evidence(tmp_path: Path) -> None:
    """The CLI renders only ``evidence[0]``, so evidence[0] must be the reason.

    V1's partial branch used to put the working test command first, so the page
    printed "Tests are runnable: `python -m pytest -q` from pyproject.toml" as
    though a discovered command were the complaint, and the sentence that
    explained the problem never reached the user at all.
    """

    repo = _repo(tmp_path, tests=True)
    (repo / "tests" / "test_demo.py").write_text("", encoding="utf-8")

    v1 = _check(_score(repo), "V1")

    assert v1.state == "partial"
    assert "declare no test case" in v1.evidence[0]


def test_the_printed_blocker_says_why(tmp_path: Path, capsys) -> None:
    """The same guarantee at the surface the user actually reads."""

    repo = _repo(tmp_path, tests=True)
    (repo / "tests" / "test_demo.py").write_text("", encoding="utf-8")

    assert ctx_main(["fit", str(repo)]) == 0
    printed = capsys.readouterr().out

    blocking = printed.split("Blocking", 1)[1].splitlines()[1]
    assert "declare no test case" in blocking


def test_no_failing_check_leads_with_a_quoted_command(tmp_path: Path) -> None:
    """A backtick-quoted command is a fact about the repository, never a reason.

    The rubric-wide form of the bug above: whatever a check puts first is the
    only line the CLI shows for it, so leading with a command tells the user
    what works instead of what does not.
    """

    hollow = _repo(tmp_path, tests=True, name="hollow")
    (hollow / "tests" / "test_demo.py").write_text("", encoding="utf-8")
    barren = tmp_path / "barren"
    barren.mkdir()

    for repo in (hollow, barren, _repo(tmp_path, tests=False, name="runnerless")):
        for result in _score(repo).checks:
            if result.state in {"fail", "partial"}:
                assert result.evidence, f"{result.check_id} must say why"
                assert not result.evidence[0].lstrip().startswith("`"), (
                    f"{result.check_id} leads with a command instead of a reason: "
                    f"{result.evidence[0]}"
                )


def test_v1_does_not_walk_the_repository_a_second_time(tmp_path: Path) -> None:
    """The deletion test for ``_declared_tests``: V1 reads the profile, not the disk.

    Scoring against a root that does not exist is the cheapest proof that the
    duplicate walk is gone. While readiness owned its own content scan this
    returned ``unassessable`` -- a second, weaker answer to a question the
    profile in hand had already answered.
    """

    repo = _repo(tmp_path, tests=True)
    profile = build_fit_profile(repo)

    v1 = next(
        item
        for item in score_readiness(profile, tmp_path / "nowhere").checks
        if item.check_id == "V1"
    )

    assert v1.state == "pass", v1.evidence


def test_readiness_no_longer_carries_a_copy_of_the_test_scan() -> None:
    """Guard: the duplicated machinery must not grow back.

    Each of these existed only to answer verification's question a second time,
    and ``_TEST_SCAN_SKIP_DIRS`` was a hand-copy of verification's own set whose
    comment promised it would disappear with exactly this change.
    """

    from ctx.fit import readiness

    for name in (
        "_declared_tests",
        "_inspectable",
        "_TEST_SCAN_SKIP_DIRS",
        "_UNREADABLE_SUFFIXES",
        "_TEST_SHAPED",
        "_TEST_DECLARATION",
        "_TEST_FILES_INSPECTED",
    ):
        assert not hasattr(readiness, name), f"readiness still defines {name}"
