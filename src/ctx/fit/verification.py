"""Discovery of a repository's own verification mechanisms.

CTX Fit may never treat "the agent said done" as success.  Every Fit trial has
to be judged by evidence the repository itself produces, so the first thing the
product must learn about a repository is *how that repository proves its own
code works*.

Nothing here executes a discovered command.  Discovery is pure inspection and
returns candidates with the evidence that justified them; validating or running
a command is a separate, explicitly requested step.  A repository with no
usable verification is a first-class, honestly reported outcome rather than an
error, because a Fit experiment on such a repository cannot produce trustworthy
results and the product must say so.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

VerificationKind = Literal["test", "lint", "typecheck", "build"]

#: Confidence is deliberately coarse. It ranks candidates for presentation and
#: never implies calibrated probability.
Confidence = Literal["high", "medium", "low"]

_MAX_READ_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """One discovered way for a repository to check itself."""

    kind: VerificationKind
    command: tuple[str, ...]
    source: str
    """Where the command came from, e.g. ``pyproject.toml [tool.pytest]``."""
    confidence: Confidence
    evidence: tuple[str, ...] = ()
    validated: bool = False
    """True only after a cheap non-mutating probe confirmed the command runs."""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "command": list(self.command),
            "source": self.source,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "validated": self.validated,
        }


@dataclass(frozen=True, slots=True)
class VerificationInventory:
    """Everything CTX Fit knows about how a repository verifies itself."""

    commands: tuple[VerificationCommand, ...] = ()
    warnings: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    """Observed test locations. A declared runner is not evidence of tests."""

    @property
    def kinds(self) -> tuple[VerificationKind, ...]:
        seen: list[VerificationKind] = []
        for command in self.commands:
            if command.kind not in seen:
                seen.append(command.kind)
        return tuple(seen)

    @property
    def declares_test_command(self) -> bool:
        """Whether the repository *declares* a way to run tests."""

        return any(command.kind == "test" for command in self.commands)

    @property
    def has_deterministic_verification(self) -> bool:
        """True only when a test command is declared **and** tests exist.

        Declaring a runner is not the same as having tests. A repository whose
        manifest configures pytest but contains no test files would run the
        command successfully against nothing, so treating it as evaluable would
        state an inference as a fact — the exact failure this product must not
        commit. Lint, typecheck, and build are useful regression signals but
        none demonstrates that a task was accomplished.
        """

        return self.declares_test_command and bool(self.test_files)

    def best(self, kind: VerificationKind) -> VerificationCommand | None:
        order = {"high": 0, "medium": 1, "low": 2}
        ranked = sorted(
            (command for command in self.commands if command.kind == kind),
            key=lambda command: order[command.confidence],
        )
        return ranked[0] if ranked else None

    def to_dict(self) -> dict[str, object]:
        return {
            "commands": [command.to_dict() for command in self.commands],
            "kinds": list(self.kinds),
            "declares_test_command": self.declares_test_command,
            "test_files": list(self.test_files),
            "has_deterministic_verification": self.has_deterministic_verification,
            "warnings": list(self.warnings),
        }


def _read_text(path: Path) -> str | None:
    # utf-8-sig, not utf-8: a leading byte-order mark is a routine artefact of
    # Windows editors, and a config file is not unreadable for carrying one.
    # scan_repo.py already reads the same files this way, so the two halves of
    # one `ctx fit` run must not disagree about whether a file is parseable.
    try:
        if not path.is_file() or path.stat().st_size > _MAX_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _load_toml(path: Path) -> tuple[dict[str, object], str | None]:
    """Parse a TOML file, reporting *why* it yielded nothing.

    Returns the mapping plus a human-readable problem, or ``None`` when there
    is none. Collapsing "empty", "too large to read" and "invalid syntax" into
    a bare ``{}`` made the product tell users with a zero-byte pyproject.toml —
    which is perfectly valid TOML — to go fix a parse error that did not exist.

    The bytes are decoded here rather than handed to ``tomllib.load``, which
    decodes as strict UTF-8 and therefore rejects a leading BOM as invalid
    syntax. That single character used to delete *every* pyproject-derived
    command at once — pytest, ruff, mypy and the build backend — and told the
    user a file every other tool accepts was malformed.
    """

    try:
        if not path.is_file():
            return {}, None
        if path.stat().st_size > _MAX_READ_BYTES:
            return {}, f"{path.name} is larger than {_MAX_READ_BYTES // 1024} KB and was not read"
        raw = path.read_bytes()
    except OSError:
        return {}, f"{path.name} exists but could not be read"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {}, f"{path.name} is not valid UTF-8, so it could not be parsed as TOML"
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return {}, f"{path.name} is not valid TOML: {exc}"
    if not isinstance(loaded, dict):  # pragma: no cover - tomllib always yields a table
        return {}, f"{path.name} is not a TOML table"
    return loaded, None


def _load_json(path: Path) -> dict[str, object]:
    text = _read_text(path)
    if text is None:
        return {}
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


_TOX_PYTEST_SECTION = re.compile(r"^\s*\[(?:pytest|tool:pytest)\]", re.MULTILINE)
_PYTEST_WORD = re.compile(r"\bpytest\b")


def _tox_pytest_evidence(root: Path) -> tuple[Confidence, str] | None:
    """What a tox.ini actually says about pytest, having read it.

    ``tox.ini`` is also where projects park ``[flake8]``, and plenty of tox
    users drive unittest or a custom runner. Emitting a high-confidence pytest
    command from the file's mere existence stated an inference as a fact, and
    the evidence sentence described content nothing had opened.
    """

    text = _read_text(root / "tox.ini")
    if text is None:
        return None
    if _TOX_PYTEST_SECTION.search(text):
        return "high", "tox.ini declares a [pytest] section"
    if _PYTEST_WORD.search(text):
        return "medium", "tox.ini names pytest but declares no [pytest] section"
    return None


_SETUP_CFG_PYTEST_SECTION = re.compile(r"^\s*\[tool:pytest\]", re.MULTILINE)


def _setup_cfg_pytest_evidence(root: Path) -> str | None:
    """Whether setup.cfg carries pytest's own configuration section.

    setup.cfg is the oldest and still most common place to configure pytest,
    and ``[tool:pytest]`` is unambiguous — pytest rejects a bare ``[pytest]``
    there, so unlike tox.ini the section cannot belong to some other tool.
    Skipping the file made the product report "no pytest config" about a file
    that *is* pytest config: it downgraded the test command to a low-confidence
    guess, and in a repository whose tests do not live in a root ``tests/``
    directory it produced no test command at all, which routes a repository
    with a passing suite to "cannot be evaluated honestly".
    """

    text = _read_text(root / "setup.cfg")
    if text is None:
        return None
    if _SETUP_CFG_PYTEST_SECTION.search(text):
        return "setup.cfg declares a [tool:pytest] section"
    return None


def _mypy_target(root: Path, pyproject: dict[str, object]) -> str | None:
    """Pick a target that exists, rather than assuming a src/ layout.

    ``python -m mypy src`` in a flat-layout repository fails with "Cannot read
    file 'src'", so presenting it under "How this repository verifies itself"
    was showing the user a command known not to work here.
    """

    if (root / "src").is_dir():
        return "src"
    project = pyproject.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if isinstance(name, str):
        for candidate in (name, name.replace("-", "_")):
            if candidate and (root / candidate).is_dir():
                return candidate
    return None


def _discover_python(root: Path) -> tuple[list[VerificationCommand], list[str]]:
    found: list[VerificationCommand] = []
    warnings: list[str] = []
    pyproject_path = root / "pyproject.toml"
    pyproject, pyproject_problem = _load_toml(pyproject_path)
    tools = pyproject.get("tool")
    tools = tools if isinstance(tools, dict) else {}

    setup_cfg_evidence = _setup_cfg_pytest_evidence(root)
    tox_evidence = _tox_pytest_evidence(root)
    if (
        "pytest" in tools
        or (root / "pytest.ini").is_file()
        or setup_cfg_evidence is not None
        or tox_evidence is not None
    ):
        if "pytest" in tools:
            source = "pyproject.toml [tool.pytest]"
            confidence: Confidence = "high"
            detail = f"{source} declares pytest configuration"
        elif (root / "pytest.ini").is_file():
            source = "pytest.ini"
            confidence = "high"
            detail = "pytest.ini declares pytest configuration"
        elif setup_cfg_evidence is not None:
            # Ranked above tox.ini because [tool:pytest] can only mean pytest,
            # whereas tox.ini's weaker rung fires on the word alone.
            source = "setup.cfg [tool:pytest]"
            confidence = "high"
            detail = setup_cfg_evidence
        else:
            assert tox_evidence is not None
            source = "tox.ini"
            confidence, detail = tox_evidence
        found.append(
            VerificationCommand(
                kind="test",
                command=("python", "-m", "pytest", "-q"),
                source=source,
                confidence=confidence,
                evidence=(detail,),
            )
        )
    elif (root / "tests").is_dir() or list(root.glob("test_*.py"))[:1]:
        found.append(
            VerificationCommand(
                kind="test",
                command=("python", "-m", "pytest", "-q"),
                source="tests/ directory",
                confidence="low",
                evidence=("a tests directory or test_*.py files exist, but no pytest config",),
            )
        )

    if "mypy" in tools or (root / "mypy.ini").is_file():
        source = "pyproject.toml [tool.mypy]" if "mypy" in tools else "mypy.ini"
        target = _mypy_target(root, pyproject)
        if target is None:
            # No inspectable target: say nothing rather than emit a command
            # that cannot run here.
            warnings.append(
                f"{source} declares mypy but no source directory could be "
                "identified, so no typecheck command is offered"
            )
        else:
            found.append(
                VerificationCommand(
                    kind="typecheck",
                    command=("python", "-m", "mypy", target),
                    source=source,
                    confidence="high",
                    evidence=(f"{source} declares mypy configuration", f"target {target}/ exists"),
                )
            )

    if "ruff" in tools or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        source = "pyproject.toml [tool.ruff]" if "ruff" in tools else "ruff.toml"
        found.append(
            VerificationCommand(
                kind="lint",
                command=("python", "-m", "ruff", "check", "."),
                source=source,
                confidence="high",
                evidence=(f"{source} declares ruff configuration",),
            )
        )

    build_system = pyproject.get("build-system")
    if isinstance(build_system, dict) and build_system.get("build-backend"):
        found.append(
            VerificationCommand(
                kind="build",
                command=("python", "-m", "build"),
                source="pyproject.toml [build-system]",
                confidence="medium",
                evidence=(f"build backend {build_system.get('build-backend')!r} declared",),
            )
        )

    if pyproject_problem is not None:
        warnings.append(pyproject_problem)
    return found, warnings


_NODE_SCRIPT_KINDS: tuple[tuple[str, VerificationKind], ...] = (
    ("test", "test"),
    ("lint", "lint"),
    ("typecheck", "typecheck"),
    ("type-check", "typecheck"),
    ("tsc", "typecheck"),
    ("build", "build"),
)

#: ``npm init -y`` writes a ``test`` script whose whole job is to announce that
#: there are no tests. Accepting it as high-confidence verification let the
#: product declare such a repository evaluable and invite the user into a paid
#: trial on the strength of it.
_NODE_NO_TEST_SCRIPT = re.compile(r"no tests?\s+(?:specified|configured|yet)", re.IGNORECASE)

#: Test runners we recognise. A body that names one is evidence; a body that
#: does not is a script we cannot vouch for, so it is not "high" confidence.
_NODE_TEST_RUNNERS = re.compile(
    r"\b(?:jest|vitest|mocha|ava|tap|tape|karma|jasmine|playwright|cypress|nyc|c8|uvu|"
    r"react-scripts\s+test|ng\s+test|bun\s+test|node\s+--test)\b",
    re.IGNORECASE,
)


def _discover_node(root: Path) -> tuple[list[VerificationCommand], list[str]]:
    found: list[VerificationCommand] = []
    warnings: list[str] = []
    package_json_path = root / "package.json"
    if not package_json_path.is_file():
        return found, warnings
    package = _load_json(package_json_path)
    if not package:
        warnings.append("package.json exists but could not be parsed")
        return found, warnings
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return found, warnings

    runner = "npm"
    for lockfile, candidate in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
    ):
        if (root / lockfile).is_file():
            runner = candidate
            break

    for script_name, kind in _NODE_SCRIPT_KINDS:
        if script_name not in scripts:
            continue
        body = scripts[script_name]
        body = body if isinstance(body, str) else ""
        confidence: Confidence = "high"
        if kind == "test":
            if _NODE_NO_TEST_SCRIPT.search(body):
                warnings.append(
                    f"package.json scripts.{script_name} only reports that no tests "
                    "exist, so it is not a verification command"
                )
                continue
            if not _NODE_TEST_RUNNERS.search(body):
                # The script may well run tests, but nothing here demonstrates
                # it, and confidence must reflect what was actually observed.
                confidence = "medium"
        found.append(
            VerificationCommand(
                kind=kind,
                command=(runner, "run", script_name),
                source=f"package.json scripts.{script_name}",
                confidence=confidence,
                evidence=(
                    f"{runner} inferred from lockfile" if runner != "npm" else "npm default",
                    f"scripts.{script_name} = {body!r}",
                ),
            )
        )
    return found, warnings


def _discover_other_ecosystems(root: Path) -> list[VerificationCommand]:
    found: list[VerificationCommand] = []
    if (root / "Cargo.toml").is_file():
        found.append(
            VerificationCommand(
                kind="test",
                command=("cargo", "test"),
                source="Cargo.toml",
                confidence="high",
                evidence=("Cargo manifest present",),
            )
        )
        found.append(
            VerificationCommand(
                kind="build",
                command=("cargo", "build"),
                source="Cargo.toml",
                confidence="high",
                evidence=("Cargo manifest present",),
            )
        )
    if (root / "go.mod").is_file():
        found.append(
            VerificationCommand(
                kind="test",
                command=("go", "test", "./..."),
                source="go.mod",
                confidence="high",
                evidence=("Go module present",),
            )
        )
    return found


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+):(?!=)", re.MULTILINE)
_MAKE_TARGET_KINDS: tuple[tuple[str, VerificationKind], ...] = (
    ("test", "test"),
    ("lint", "lint"),
    ("typecheck", "typecheck"),
    ("build", "build"),
)


def _discover_make(root: Path) -> list[VerificationCommand]:
    text = _read_text(root / "Makefile")
    if text is None:
        return []
    targets = set(_MAKE_TARGET.findall(text))
    found: list[VerificationCommand] = []
    for target, kind in _MAKE_TARGET_KINDS:
        if target in targets:
            found.append(
                VerificationCommand(
                    kind=kind,
                    command=("make", target),
                    source=f"Makefile target {target}",
                    confidence="medium",
                    evidence=(f"Makefile declares a {target} target",),
                )
            )
    return found


#: Directories and glob patterns that constitute observed test material.
_TEST_DIRS: tuple[str, ...] = ("tests", "test", "spec", "__tests__")
_TEST_GLOBS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.ts",
    "*.test.js",
    "*.spec.ts",
    "*.spec.js",
    "*_test.rs",
)
#: How many directory levels below the root are searched. Go packages keep
#: ``*_test.go`` beside the code in ``internal/a/b/``, and workspace monorepos
#: keep ``packages/*/tests/``; stopping short of those told repositories with
#: passing suites that they had no tests, which routed them to "cannot be
#: evaluated honestly" — the exact inversion this product must never commit.
_TEST_SCAN_DEPTH = 4
#: Directories that never hold a repository's *own* tests but often hold
#: thousands of vendored ones. Walking them is both slow and misleading.
_TEST_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "vendor",
        "venv",
        "env",
        "target",
        "dist",
        "build",
        "__pycache__",
        "site-packages",
        "third_party",
        "testdata",
    }
)
#: Ceiling on directories visited, so a pathological tree cannot make discovery
#: slow. Discovery must stay cheap enough to run unconditionally.
_TEST_SCAN_MAX_DIRS = 2000
#: ``test_files`` is evidence a human reads, not an index of the suite.
_TEST_REPORT_LIMIT = 5
#: Rust's dominant convention puts unit tests *inside* the module they test,
#: behind a ``#[cfg(test)]`` gate in an ordinary ``src/*.rs`` file. No filename
#: pattern can see them, so a crate whose ``cargo test`` runs and passes was
#: reported as having no tests at all and routed to "cannot be evaluated".
#: Anchored at line start so a ``#[cfg(test)]`` quoted inside a ``///`` doc
#: comment or a string literal is not counted as a test.
_RUST_INLINE_TEST = re.compile(r"^\s*#!?\[\s*(?:cfg\s*\(\s*test\s*\)|test)\s*\]", re.MULTILINE)
#: Ceiling on Rust sources opened. Detecting inline tests needs file contents,
#: so it is bounded like the directory walk: discovery runs unconditionally and
#: must stay cheap. Sources are inspected in sorted order so a crate over the
#: ceiling still profiles identically on every machine.
_RUST_SOURCE_SCAN_LIMIT = 400
#: Suffixes a test runner could actually execute. A directory named ``test``
#: holding only ``fixtures.txt`` is not test material, and counting it let a
#: repository with no tests at all be declared evaluable. The list is
#: deliberately broad across ecosystems rather than tied to ``_TEST_GLOBS``,
#: whose naming conventions do not cover Ruby, Java, or Elixir suites.
_TEST_CODE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".kt",
        ".scala",
        ".cs",
        ".php",
        ".swift",
        ".ex",
        ".exs",
        ".c",
        ".cc",
        ".cpp",
        ".m",
        ".mm",
        ".sh",
        ".bats",
        ".feature",
    }
)


def _is_test_filename(name: str) -> bool:
    # fnmatchcase, not fnmatch: case folding is a property of the host
    # filesystem, and the profile must not depend on which machine ran it.
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _TEST_GLOBS)


def _rust_inline_test_files(root: Path, sources: list[str]) -> list[str]:
    """Rust sources that declare tests inline, named so a human can check.

    Only the paths the pruned walk already collected are considered, so the
    vendored trees skipped there stay skipped here — a content scan that
    reached into ``target/`` or ``vendor/`` would recreate the mirror-image
    bug of counting somebody else's tests as this repository's own.
    """

    found: list[str] = []
    for relative in sorted(sources)[:_RUST_SOURCE_SCAN_LIMIT]:
        text = _read_text(root / relative)
        if text is None:
            continue
        if _RUST_INLINE_TEST.search(text):
            found.append(relative)
            if len(found) >= _TEST_REPORT_LIMIT:
                break
    return found


def _find_test_files(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return observed test locations and any subtree that could not be read.

    Existence of a runner in a manifest proves only intent. This looks for
    material the runner could actually execute.

    Directories named ``tests``/``test``/``spec``/``__tests__`` are reported in
    preference to individual files: they describe the layout more usefully and
    keep the evidence short. Results are sorted so the reported value is a
    function of the repository alone — ``os.scandir`` order differs between
    filesystems, which would otherwise make the same commit profile differently
    on a developer machine and on CI.

    Unreadable directories are returned rather than dropped: ``os.walk``
    swallows scandir errors by default, which would let "the tests are in a
    subtree we were denied" be reported as "there are no tests".
    """

    test_dir_candidates: list[str] = []
    dirs_holding_code: list[str] = []
    test_files: list[str] = []
    rust_sources: list[str] = []
    cargo_manifest_seen = False
    unreadable: list[str] = []
    visited = 0

    def _on_walk_error(exc: OSError) -> None:
        target = getattr(exc, "filename", None) or str(exc)
        try:
            target = os.path.relpath(target, root)
        except (OSError, ValueError):  # pragma: no cover - defensive
            pass
        unreadable.append(str(target))

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
        visited += 1
        if visited > _TEST_SCAN_MAX_DIRS:
            break
        try:
            relative = Path(dirpath).relative_to(root)
        except ValueError:  # pragma: no cover - defensive
            continue
        parts = relative.parts
        depth = len(parts)
        prefix = f"{relative.as_posix()}/" if depth else ""

        if depth and parts[-1] in _TEST_DIRS:
            test_dir_candidates.append(prefix)
        if any(Path(name).suffix.lower() in _TEST_CODE_SUFFIXES for name in filenames):
            dirs_holding_code.append(prefix)
        test_files.extend(prefix + name for name in filenames if _is_test_filename(name))
        cargo_manifest_seen = cargo_manifest_seen or "Cargo.toml" in filenames
        rust_sources.extend(prefix + name for name in filenames if name.endswith(".rs"))

        if depth >= _TEST_SCAN_DEPTH:
            dirnames.clear()
            continue
        # Hidden directories are skipped, matching the previous glob-based
        # search, which never matched a leading dot.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _TEST_SCAN_SKIP_DIRS and not name.startswith(".")
        )

    # A test directory only counts when it (or something under it) holds a file
    # a runner could execute. Requiring executable material rather than any
    # entry at all is what stops a `test/` folder of fixtures from being
    # reported as a test suite.
    qualifying_dirs = [
        candidate
        for candidate in test_dir_candidates
        if any(holder.startswith(candidate) for holder in dirs_holding_code)
    ]
    skipped = tuple(sorted(set(unreadable))[:_TEST_REPORT_LIMIT])
    if qualifying_dirs:
        return tuple(sorted(qualifying_dirs)[:_TEST_REPORT_LIMIT]), skipped
    # Reading file contents is the expensive rung, so it is reached only when
    # names and directories found nothing — which is exactly the shape of an
    # idiomatic crate: no tests/ directory, no *_test.rs, tests inline in src/.
    if cargo_manifest_seen and not test_files:
        test_files.extend(_rust_inline_test_files(root, rust_sources))
    return tuple(sorted(set(test_files))[:_TEST_REPORT_LIMIT]), skipped


def discover_verification(repo_path: str | Path) -> VerificationInventory:
    """Inspect a repository and report how it can verify itself.

    Purely read-only.  Returns an empty inventory with a warning rather than
    raising when a repository offers nothing usable, because "this repository
    cannot be evaluated honestly" is a real answer the product must be able to
    give.
    """

    root = Path(repo_path)
    if not root.is_dir():
        return VerificationInventory(warnings=("repository path is not a directory",))

    commands: list[VerificationCommand] = []
    warnings: list[str] = []

    python_commands, python_warnings = _discover_python(root)
    commands.extend(python_commands)
    warnings.extend(python_warnings)

    node_commands, node_warnings = _discover_node(root)
    commands.extend(node_commands)
    warnings.extend(node_warnings)

    commands.extend(_discover_other_ecosystems(root))
    commands.extend(_discover_make(root))

    test_files, unreadable = _find_test_files(root)
    if unreadable:
        # Named, not counted: the user has to be able to check the claim, and
        # "we were denied here" is a different answer from "nothing is there".
        warnings.append(
            "could not read "
            + ", ".join(unreadable)
            + "; any tests below are invisible to this scan"
        )
    if not any(command.kind == "test" for command in commands):
        warnings.append(
            "no test command discovered; a Fit experiment cannot verify task "
            "completion in this repository"
        )
    elif not test_files:
        warnings.append(
            "a test command is declared but no test files were found; the "
            "command would run against nothing, so this repository is not "
            "evaluable until tests exist"
        )

    return VerificationInventory(
        commands=tuple(commands),
        warnings=tuple(warnings),
        test_files=test_files,
    )


__all__ = [
    "Confidence",
    "VerificationCommand",
    "VerificationInventory",
    "VerificationKind",
    "discover_verification",
]
