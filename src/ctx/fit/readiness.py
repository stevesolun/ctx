"""AI agent readiness assessment.

Answers one question: *how suitable is this repository for autonomous or
semi-autonomous coding agents, and what should be fixed first?*

Three rules shape the design.

**The score is never the point.** A bare number invites gaming and tells nobody
what to do. Every check therefore carries a human-readable rationale answering
"how does this help an agent produce safer or more verifiable work?", and a
metric that cannot answer it does not belong in the rubric — a test enforces
this, so it cannot rot.

**Unassessable is not zero.** A dimension CTX could not evaluate is excluded
from the denominator rather than scored zero. Silently converting "unknown" to
"bad" is the readiness analogue of treating unknown cost as free, and this
product forbids both.

**Blockers are falsifiable, not opinions.** A finding blocks only if it breaks
the evidence chain (the repository cannot prove an agent's work) or the
containment chain (nothing bounds or reverses what an agent does). Everything
else changes the score, not the possibility. Blocking is a classification of an
already-failed check, never an extra penalty.

Scoring is deterministic and performs no model call, no network, and no
subprocess.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ctx.fit.profile import FitProfile

#: Bump when dimensions, weights, or check semantics change, so a stored score
#: is never silently compared against one computed by different rules.
READINESS_RUBRIC_VERSION = "ctx.fit.readiness-v1"

CheckState = Literal["pass", "partial", "fail", "not_applicable", "unassessable"]

Dimension = Literal[
    "verification",
    "instructions",
    "environment",
    "ci",
    "tool_safety",
    "context",
]

#: Maximum points per dimension. Verification dominates because it is the only
#: thing that converts "the agent said done" into "the repository proved done".
DIMENSION_POINTS: dict[Dimension, int] = {
    "verification": 30,
    "instructions": 20,
    "environment": 15,
    "ci": 15,
    "tool_safety": 10,
    "context": 10,
}

DIMENSION_TITLES: dict[Dimension, str] = {
    "verification": "Verification",
    "instructions": "Instructions",
    "environment": "Environment",
    "ci": "CI enforcement",
    "tool_safety": "Tool safety",
    "context": "Context tractability",
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one rubric check against one repository."""

    check_id: str
    dimension: Dimension
    title: str
    state: CheckState
    earned: int
    possible: int
    evidence: tuple[str, ...]
    remedy: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.check_id,
            "dimension": self.dimension,
            "title": self.title,
            "state": self.state,
            "earned": self.earned,
            "possible": self.possible,
            "evidence": list(self.evidence),
            "remedy": self.remedy,
        }


@dataclass(frozen=True, slots=True)
class Check:
    """One rubric item.

    ``agent_rationale`` is mandatory and unique. It is the anti-gaming gate: a
    metric that cannot say how it helps an agent produce safer or more
    verifiable work is not admitted to the rubric.
    """

    check_id: str
    dimension: Dimension
    title: str
    points: int
    agent_rationale: str
    remedy: str
    blocking: bool
    evaluate: Callable[[FitProfile, Path], tuple[CheckState, tuple[str, ...]]]


@dataclass(frozen=True, slots=True)
class DimensionScore:
    dimension: Dimension
    title: str
    earned: int
    assessable: int
    possible: int

    @property
    def is_assessable(self) -> bool:
        return self.assessable > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "title": self.title,
            "earned": self.earned,
            "assessable": self.assessable,
            "possible": self.possible,
        }


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """A readiness assessment: score, per-dimension detail, blockers, fixes."""

    rubric_version: str
    score: int | None
    """Normalized 0-100 over *assessable* points, or None if nothing could be assessed."""
    earned: int
    assessable: int
    dimensions: tuple[DimensionScore, ...]
    checks: tuple[CheckResult, ...]

    @property
    def blockers(self) -> tuple[CheckResult, ...]:
        """Blocking checks that did not fully pass.

        For a blocking check, ``partial`` still means the chain is broken — a
        repository that declares a test runner but has no tests cannot prove an
        agent's work any better than one with no runner at all. ``not_applicable``
        and ``unassessable`` never block, because absence of knowledge is not
        evidence of a problem.
        """

        blocking_ids = {check.check_id for check in RUBRIC if check.blocking}
        return tuple(
            result
            for result in self.checks
            if result.check_id in blocking_ids and result.state in {"fail", "partial"}
        )

    @property
    def improvements(self) -> tuple[CheckResult, ...]:
        """Failed and partial non-blocking checks, highest value first."""

        blocking_ids = {check.check_id for check in RUBRIC if check.blocking}
        candidates = [
            result
            for result in self.checks
            if result.state in {"fail", "partial"} and result.check_id not in blocking_ids
        ]
        candidates.sort(key=lambda result: (-(result.possible - result.earned), result.check_id))
        return tuple(candidates)

    @property
    def unassessable(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.checks if result.state == "unassessable")

    def to_dict(self) -> dict[str, object]:
        return {
            "rubric_version": self.rubric_version,
            "score": self.score,
            "earned": self.earned,
            "assessable": self.assessable,
            "dimensions": [item.to_dict() for item in self.dimensions],
            "checks": [item.to_dict() for item in self.checks],
            "blockers": [item.to_dict() for item in self.blockers],
            "improvements": [item.to_dict() for item in self.improvements],
        }


def _exists(root: Path, *names: str) -> str | None:
    for name in names:
        if (root / name).exists():
            return name
    return None


def _glob_first(root: Path, pattern: str) -> str | None:
    try:
        match = next(root.glob(pattern), None)
    except OSError:
        return None
    return match.name if match else None


# --------------------------------------------------------------------------
# Rubric checks. Each predicate is pure with respect to the repository: it only
# reads. None of them executes anything.
# --------------------------------------------------------------------------


def _check_tests_runnable(profile: FitProfile, _root: Path) -> tuple[CheckState, tuple[str, ...]]:
    verification = profile.verification
    if verification.has_deterministic_verification:
        command = verification.best("test")
        assert command is not None
        return "pass", (
            f"`{' '.join(command.command)}` from {command.source}",
            f"test material: {', '.join(verification.test_files)}",
        )
    if verification.declares_test_command:
        return "partial", ("a test command is declared but no test files were found",)
    return "fail", ("no test command could be discovered",)


def _check_static_analysis(profile: FitProfile, _root: Path) -> tuple[CheckState, tuple[str, ...]]:
    kinds = set(profile.verification.kinds)
    present = sorted(kinds & {"lint", "typecheck"})
    if len(present) == 2:
        return "pass", (f"{', '.join(present)} configured",)
    if present:
        return "partial", (f"only {present[0]} configured",)
    return "fail", ("neither lint nor type checking is configured",)


def _check_build(profile: FitProfile, _root: Path) -> tuple[CheckState, tuple[str, ...]]:
    command = profile.verification.best("build")
    if command is not None:
        return "pass", (f"`{' '.join(command.command)}` from {command.source}",)
    return "fail", ("no build command discovered",)


def _check_instructions_present(
    profile: FitProfile, _root: Path
) -> tuple[CheckState, tuple[str, ...]]:
    files = profile.existing_ai_config.instruction_files
    if files:
        return "pass", (f"found {', '.join(files)}",)
    return "fail", ("no agent instruction file (AGENTS.md, CLAUDE.md, ...) found",)


def _check_instructions_mention_verification(
    profile: FitProfile, root: Path
) -> tuple[CheckState, tuple[str, ...]]:
    files = profile.existing_ai_config.instruction_files
    if not files:
        return "not_applicable", ("no instruction file to inspect",)
    test_command = profile.verification.best("test")
    if test_command is None:
        return "unassessable", ("no known test command to look for",)
    needle = test_command.command[-1] if test_command.command else ""
    tokens = {token for token in (needle, "pytest", "test", "npm test", "cargo test") if token}
    for name in files:
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(token.lower() in text for token in tokens):
            return "pass", (f"{name} references how to run tests",)
    return "fail", (f"{', '.join(files)} never mention how to verify a change",)


def _check_lockfile(profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    languages = {item.get("name") for item in profile.stack.get("languages", [])}
    found = _exists(
        root,
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "requirements.txt",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.lock",
        "go.sum",
    )
    if found:
        return "pass", (f"{found} is committed",)
    if not languages:
        return "unassessable", ("no language detected, so no lockfile convention applies",)
    return "fail", ("no dependency lockfile is committed",)


def _check_declared_python_version(
    profile: FitProfile, root: Path
) -> tuple[CheckState, tuple[str, ...]]:
    languages = {item.get("name") for item in profile.stack.get("languages", [])}
    if "python" not in languages:
        return "not_applicable", ("not a Python repository",)
    found = _exists(root, ".python-version", "runtime.txt")
    if found:
        return "pass", (f"{found} pins the interpreter",)
    text = None
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    if text and "requires-python" in text:
        return "pass", ("pyproject.toml declares requires-python",)
    return "fail", ("no interpreter version is pinned",)


def _check_ci_configured(profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    if (root / ".github" / "workflows").is_dir():
        return "pass", (".github/workflows is present",)
    found = _exists(root, ".gitlab-ci.yml", ".circleci", "azure-pipelines.yml", "Jenkinsfile")
    if found:
        return "pass", (f"{found} is present",)
    return "fail", ("no CI configuration found",)


def _check_ci_runs_tests(profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return "not_applicable", ("no GitHub Actions workflows to inspect",)
    try:
        files = [item for item in sorted(workflows.iterdir()) if item.is_file()][:16]
    except OSError:
        return "unassessable", ("workflow directory could not be read",)
    for item in files:
        try:
            text = item.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if any(token in text for token in ("pytest", "npm test", "go test", "cargo test", "tox")):
            return "pass", (f"{item.name} runs the test suite",)
    return "fail", ("no workflow appears to run the test suite",)


def _check_version_control(_profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    if (root / ".git").exists():
        return "pass", ("repository is under Git, so agent changes are reversible",)
    return "fail", ("no .git directory: agent changes could not be reviewed or reverted",)


def _check_secret_hygiene(_profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    gitignore = root / ".gitignore"
    committed_env = (root / ".env").is_file()
    if not gitignore.is_file():
        return ("fail", ("no .gitignore: generated or secret files could be committed",))
    try:
        text = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unassessable", (".gitignore could not be read",)
    ignores_env = ".env" in text
    if committed_env and not ignores_env:
        return "fail", ("a .env file is present and not ignored",)
    if ignores_env:
        return "pass", (".gitignore excludes .env",)
    return "partial", (".gitignore exists but does not mention .env",)


def _check_repo_tractable(profile: FitProfile, _root: Path) -> tuple[CheckState, tuple[str, ...]]:
    if profile.stack.get("monorepo"):
        packages = profile.stack.get("workspace_packages") or []
        return "partial", (
            f"monorepo with {len(packages)} workspace packages; an agent must be told which to touch",
        )
    return "pass", ("single-package layout, so change scope is unambiguous",)


def _check_docs(profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    found = _exists(root, "README.md", "README.rst", "README") or _glob_first(root, "README*")
    if found:
        return "pass", (f"{found} gives an agent project context",)
    return "fail", ("no README: an agent has no project overview",)


RUBRIC: tuple[Check, ...] = (
    Check(
        check_id="V1",
        dimension="verification",
        title="Tests are runnable",
        points=18,
        agent_rationale=(
            "Without a repository-native test an agent's own claim is the only evidence, "
            "which this product forbids as proof of success."
        ),
        remedy="Add a test suite and make sure the declared runner can execute it.",
        blocking=True,
        evaluate=_check_tests_runnable,
    ),
    Check(
        check_id="V2",
        dimension="verification",
        title="Lint and type checking configured",
        points=8,
        agent_rationale=(
            "Static checks catch the plausible-looking but wrong edits agents most often "
            "make, before a human has to review them."
        ),
        remedy="Configure a linter and a type checker.",
        blocking=False,
        evaluate=_check_static_analysis,
    ),
    Check(
        check_id="V3",
        dimension="verification",
        title="Build command discoverable",
        points=4,
        agent_rationale=(
            "A build step proves an agent's change still assembles, catching breakage that "
            "unit tests alone can miss."
        ),
        remedy="Declare a build command in the project manifest.",
        blocking=False,
        evaluate=_check_build,
    ),
    Check(
        check_id="I1",
        dimension="instructions",
        title="Agent instructions exist",
        points=12,
        agent_rationale=(
            "An agent told nothing about the project rediscovers conventions by guessing, "
            "which wastes tokens and produces inconsistent work."
        ),
        remedy="Add an AGENTS.md describing the project, conventions, and how to verify a change.",
        blocking=False,
        evaluate=_check_instructions_present,
    ),
    Check(
        check_id="I2",
        dimension="instructions",
        title="Instructions explain how to verify a change",
        points=8,
        agent_rationale=(
            "An agent that is not told which check to run does not run it, so verifiable "
            "work depends on the instructions naming the command."
        ),
        remedy="State the exact test command in the instruction file.",
        blocking=False,
        evaluate=_check_instructions_mention_verification,
    ),
    Check(
        check_id="E1",
        dimension="environment",
        title="Dependencies are locked",
        points=9,
        agent_rationale=(
            "Without a lockfile a passing run in an isolated workspace proves nothing about "
            "the developer's machine, because the toolchain may differ."
        ),
        remedy="Commit a dependency lockfile.",
        blocking=False,
        evaluate=_check_lockfile,
    ),
    Check(
        check_id="E2",
        dimension="environment",
        title="Language runtime is pinned",
        points=6,
        agent_rationale=(
            "An unpinned runtime lets an agent's environment drift from the project's, "
            "producing failures that are environmental rather than real."
        ),
        remedy="Pin the interpreter version.",
        blocking=False,
        evaluate=_check_declared_python_version,
    ),
    Check(
        check_id="C1",
        dimension="ci",
        title="CI is configured",
        points=7,
        agent_rationale=(
            "CI enforces verification somewhere other than a laptop, so an agent's change is "
            "checked even when a human forgets."
        ),
        remedy="Add a CI workflow.",
        blocking=False,
        evaluate=_check_ci_configured,
    ),
    Check(
        check_id="C2",
        dimension="ci",
        title="CI runs the test suite",
        points=8,
        agent_rationale=(
            "CI that does not run tests enforces nothing an agent could fail, so it provides "
            "no independent evidence."
        ),
        remedy="Make the CI workflow run the test suite.",
        blocking=False,
        evaluate=_check_ci_runs_tests,
    ),
    Check(
        check_id="S1",
        dimension="tool_safety",
        title="Repository is under version control",
        points=6,
        agent_rationale=(
            "Version control is what makes an agent's changes reviewable and reversible; "
            "without it there is no bound on damage."
        ),
        remedy="Initialize a Git repository.",
        blocking=True,
        evaluate=_check_version_control,
    ),
    Check(
        check_id="S2",
        dimension="tool_safety",
        title="Secret hygiene",
        points=4,
        agent_rationale=(
            "An agent reads and writes repository files, so unignored secrets risk being "
            "echoed into logs, prompts, or commits."
        ),
        remedy="Add .env and other secret files to .gitignore.",
        blocking=False,
        evaluate=_check_secret_hygiene,
    ),
    Check(
        check_id="X1",
        dimension="context",
        title="Change scope is unambiguous",
        points=6,
        agent_rationale=(
            "In a monorepo an agent must be told which package owns a change, or it edits "
            "the wrong one and verification passes for the wrong reason."
        ),
        remedy="Document which package owns which concern.",
        blocking=False,
        evaluate=_check_repo_tractable,
    ),
    Check(
        check_id="X2",
        dimension="context",
        title="Project overview exists",
        points=4,
        agent_rationale=(
            "A README gives an agent the project's purpose and entry points, reducing the "
            "exploration it would otherwise pay for in tokens."
        ),
        remedy="Add a README describing the project.",
        blocking=False,
        evaluate=_check_docs,
    ),
)


def _earned_for(state: CheckState, points: int) -> int:
    if state == "pass":
        return points
    if state == "partial":
        return points // 2
    return 0


def score_readiness(profile: FitProfile, repo_path: str | Path | None = None) -> ReadinessReport:
    """Score a repository's AI agent readiness deterministically.

    Performs no model call, no network access, and no subprocess. Checks that
    cannot be assessed are excluded from the denominator rather than scored as
    failures.
    """

    root = Path(repo_path) if repo_path is not None else Path(profile.repo_path)

    results: list[CheckResult] = []
    for check in RUBRIC:
        try:
            state, evidence = check.evaluate(profile, root)
        except Exception:  # pragma: no cover - a check must never break the report
            state, evidence = "unassessable", ("check raised an unexpected error",)
        results.append(
            CheckResult(
                check_id=check.check_id,
                dimension=check.dimension,
                title=check.title,
                state=state,
                earned=_earned_for(state, check.points),
                possible=check.points,
                evidence=evidence,
                remedy=check.remedy,
            )
        )

    dimensions: list[DimensionScore] = []
    for dimension, possible in DIMENSION_POINTS.items():
        scoped = [item for item in results if item.dimension == dimension]
        assessable = sum(
            item.possible for item in scoped if item.state in {"pass", "partial", "fail"}
        )
        dimensions.append(
            DimensionScore(
                dimension=dimension,
                title=DIMENSION_TITLES[dimension],
                earned=sum(item.earned for item in scoped),
                assessable=assessable,
                possible=possible,
            )
        )

    earned = sum(item.earned for item in dimensions)
    assessable = sum(item.assessable for item in dimensions)
    score = round(100 * earned / assessable) if assessable else None

    return ReadinessReport(
        rubric_version=READINESS_RUBRIC_VERSION,
        score=score,
        earned=earned,
        assessable=assessable,
        dimensions=tuple(dimensions),
        checks=tuple(results),
    )


__all__ = [
    "DIMENSION_POINTS",
    "READINESS_RUBRIC_VERSION",
    "RUBRIC",
    "Check",
    "CheckResult",
    "DimensionScore",
    "ReadinessReport",
    "score_readiness",
]
