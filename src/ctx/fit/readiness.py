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

import fnmatch
import os
import re
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
    """First match by name, not by directory order.

    ``Path.glob`` yields entries in ``os.scandir`` order, which is a property of
    the filesystem rather than of the repository, so an unsorted pick published
    a different value for the same commit on APFS and on ext4.
    """

    try:
        names = sorted(match.name for match in root.glob(pattern))
    except OSError:
        return None
    return names[0] if names else None


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


#: Ways an instruction file can name a test runner. Anchored on word
#: boundaries because a bare ``test`` substring matched "latest", "fastest" and
#: "contest", handing out the points for an ordinary English word and then
#: asserting, as evidence, something the file does not say.
_TEST_RUNNER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpytest\b",
        r"\bpython\s+-m\s+unittest\b",
        r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b",
        r"\bcargo\s+test\b",
        r"\bgo\s+test\b",
        r"\bmake\s+test\b",
        r"\btox\b",
    )
)


def _discovered_command_pattern(command: tuple[str, ...]) -> re.Pattern[str] | None:
    """Match the exact discovered command, whitespace-insensitively."""

    if not command:
        return None
    body = r"\s+".join(re.escape(word) for word in command)
    return re.compile(rf"(?<!\S){body}(?!\S)", re.IGNORECASE)


def _check_instructions_mention_verification(
    profile: FitProfile, root: Path
) -> tuple[CheckState, tuple[str, ...]]:
    files = profile.existing_ai_config.instruction_files
    if not files:
        return "not_applicable", ("no instruction file to inspect",)
    test_command = profile.verification.best("test")
    if test_command is None:
        return "unassessable", ("no known test command to look for",)
    discovered = _discovered_command_pattern(test_command.command)
    patterns = ([discovered] if discovered is not None else []) + list(_TEST_RUNNER_PATTERNS)
    for name in files:
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in patterns:
            match = pattern.search(text)
            if match is not None:
                # Quote what actually matched, so the evidence is falsifiable
                # by anyone reading the file.
                return "pass", (f"{name} names a test command: {match.group(0).strip()!r}",)
    return "fail", (f"{', '.join(files)} never mention how to verify a change",)


#: Files that pin resolved versions. ``requirements.txt`` is deliberately not
#: here: it is a *request* list, and only counts once it actually pins.
_LOCKFILES: tuple[str, ...] = (
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
)


def _requirements_pin_state(path: Path) -> tuple[CheckState, tuple[str, ...]]:
    """Grade a requirements.txt by whether it pins anything.

    The check's rationale is that a passing run has to be reproducible on
    another machine. ``requests``/``flask`` on two lines provides none of that,
    so it cannot earn the same marks as a real lockfile.
    """

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unassessable", ("requirements.txt could not be read",)
    entries = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not entries:
        return "partial", ("requirements.txt is committed but lists nothing",)
    if any("--hash=" in entry for entry in entries):
        return "pass", ("requirements.txt carries hashes, so resolution is reproducible",)
    # Lines starting with '-' are pip options (-r, -e, --index-url), not pins.
    requirements = [entry for entry in entries if not entry.startswith("-")]
    unpinned = [entry for entry in requirements if "==" not in entry]
    if not requirements:
        return "partial", ("requirements.txt only references other files",)
    if unpinned:
        return "partial", (
            f"requirements.txt is committed but {len(unpinned)} of "
            f"{len(requirements)} entries are unpinned",
        )
    return "pass", ("requirements.txt pins every entry with ==",)


def _check_lockfile(profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    languages = {item.get("name") for item in profile.stack.get("languages", [])}
    found = _exists(root, *_LOCKFILES)
    if found:
        return "pass", (f"{found} is committed",)
    requirements = root / "requirements.txt"
    if requirements.is_file():
        return _requirements_pin_state(requirements)
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


#: Tokens that indicate a CI step actually runs the suite.
_CI_TEST_TOKENS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bpytest\b",
        r"\bpython\s+-m\s+pytest\b",
        r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b",
        r"\bgo\s+test\b",
        r"\bcargo\s+test\b",
        r"\bmake\s+test\b",
        r"\btox\b",
    )
)

#: Commands that only *provision* a runner. A job that installs pytest and then
#: lints enforces nothing an agent could fail, which is precisely the case C2
#: exists to catch, so these must never be read as "CI runs the tests".
#:
#: Judged per shell command, never per line: ``npm ci && npm test`` is one step
#: that both provisions and runs, and vetoing the whole line for it told the
#: most ordinary Node workflow there is that its CI does not run tests.
_CI_INSTALL_MARKERS: tuple[str, ...] = (
    "pip install",
    "pip3 install",
    "pip download",
    "uv pip install",
    "uv sync",
    "uv add",
    "poetry install",
    "poetry add",
    "pipx install",
    "conda install",
    "npm ci",
    "npm install",
    "npm i ",
    "yarn install",
    "pnpm install",
    "apt-get",
    "apt install",
    "brew install",
    "cargo install",
    "go install",
    "requirements.txt",
    "restore-keys",
    "cache-dependency-path",
)

#: Shell separators that end one command and begin another inside a single
#: ``run:`` line.
_CI_COMMAND_SPLIT: re.Pattern[str] = re.compile(r"&&|\|\||;|\|")

#: YAML keys whose value is data, not a command. A cache entry named
#: ``pytest-cache-v1`` is not a test run, and claiming it is puts a quoted
#: non-command in the evidence as proof the suite executes.
_CI_DATA_KEYS: frozenset[str] = frozenset(
    {
        "cache",
        "cache-dependency-path",
        "container",
        "id",
        "if",
        "image",
        "key",
        "labels",
        "name",
        "needs",
        "path",
        "paths",
        "restore-keys",
        "runs-on",
        "uses",
        "working-directory",
    }
)

#: ``- key: value`` or ``key: value``, the only shapes where the key governs
#: what the rest of the line means.
_CI_YAML_KEY: re.Pattern[str] = re.compile(r"^(?:-\s*)?([A-Za-z_][\w.-]*)\s*:(?:\s|$)")


def _ci_config_files(root: Path) -> list[Path]:
    """Every CI definition C1 recognises, so C2 can inspect the same set."""

    found: list[Path] = []
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        try:
            found.extend(item for item in sorted(workflows.iterdir()) if item.is_file())
        except OSError:
            pass
    for name in (".gitlab-ci.yml", ".gitlab-ci.yaml", "azure-pipelines.yml", "Jenkinsfile"):
        candidate = root / name
        if candidate.is_file():
            found.append(candidate)
    circleci = root / ".circleci"
    if circleci.is_dir():
        try:
            found.extend(item for item in sorted(circleci.iterdir()) if item.is_file())
        except OSError:
            pass
    return found[:16]


def _ci_line_runs_tests(line: str) -> re.Match[str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    key = _CI_YAML_KEY.match(stripped)
    if key is not None and key.group(1).lower() in _CI_DATA_KEYS:
        return None
    # Per command, not per line: the install veto answers "is *this* command
    # only provisioning?", and a chained step contains both answers.
    for segment in _CI_COMMAND_SPLIT.split(stripped):
        command = segment.strip()
        if not command or command.startswith("#"):
            continue
        # Padded so a marker written with a trailing space ("npm i ") still
        # matches a command that ends there.
        padded = f" {command.lower()} "
        if any(marker in padded for marker in _CI_INSTALL_MARKERS):
            continue
        for pattern in _CI_TEST_TOKENS:
            match = pattern.search(command)
            if match is not None:
                return match
    return None


def _check_ci_runs_tests(profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    files = _ci_config_files(root)
    if not files:
        return "not_applicable", ("no CI configuration to inspect",)
    readable = 0
    for item in files:
        try:
            text = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        readable += 1
        for line in text.splitlines():
            match = _ci_line_runs_tests(line)
            if match is not None:
                return "pass", (f"{item.name} runs the test suite: {line.strip()!r}",)
    if not readable:
        # CI exists but could not be read: unknown is not the same as absent,
        # and scoring it zero would be the readiness analogue of treating
        # unknown cost as free.
        return "unassessable", ("CI configuration exists but could not be read",)
    return "fail", ("no CI job appears to run the test suite",)


def _check_version_control(_profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    """Pass when the analysed directory is *inside* a repository.

    Running ``ctx`` in ``repo/backend`` is ordinary, and containment is intact
    there: every change is tracked by the repository above. Testing only the
    argument directory reported "no .git directory" for a tracked subdirectory
    and then advised initializing a nested repository, which is worse than the
    situation it described.
    """

    try:
        start = root.resolve()
    except OSError:  # pragma: no cover - defensive
        start = root
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            if candidate == start:
                return "pass", ("repository is under Git, so agent changes are reversible",)
            # The directory name, not the absolute path: an absolute path makes
            # the report differ between machines for the same repository, and
            # the bare relative form ("..") named nothing the user could check.
            depth = os.path.relpath(candidate, start)
            name = candidate.name or str(candidate)
            return "pass", (
                f"under Git via the {name}/ repository at {depth}, so agent changes are reversible",
            )
    return "fail", ("no .git directory here or in any parent: agent changes could not be reverted",)


def _gitignore_ignores(text: str, path: str) -> bool:
    """Whether the .gitignore body would actually ignore ``path``.

    A raw ``".env" in text`` test passed on ``.envrc`` (direnv), ``.env.example``
    and even the negation ``!.env`` — so a repository holding real secrets in an
    unignored ``.env`` got an affirmative all-clear on the one check that exists
    to warn it. Git's rule is that the *last* matching pattern decides, which is
    what this reproduces.
    """

    ignored = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        pattern = line.strip().strip("/")
        if pattern.startswith("**/"):
            pattern = pattern[3:]
        if not pattern:
            continue
        if fnmatch.fnmatchcase(path, pattern):
            ignored = not negated
    return ignored


def _check_secret_hygiene(_profile: FitProfile, root: Path) -> tuple[CheckState, tuple[str, ...]]:
    gitignore = root / ".gitignore"
    committed_env = (root / ".env").is_file()
    if not gitignore.is_file():
        return ("fail", ("no .gitignore: generated or secret files could be committed",))
    try:
        text = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unassessable", (".gitignore could not be read",)
    ignores_env = _gitignore_ignores(text, ".env")
    if committed_env and not ignores_env:
        return "fail", ("a .env file is present and not ignored",)
    if ignores_env:
        return "pass", (".gitignore excludes .env",)
    return "partial", (".gitignore exists but does not ignore .env",)


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
