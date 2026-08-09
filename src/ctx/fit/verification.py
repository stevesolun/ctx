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

import json
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

    @property
    def kinds(self) -> tuple[VerificationKind, ...]:
        seen: list[VerificationKind] = []
        for command in self.commands:
            if command.kind not in seen:
                seen.append(command.kind)
        return tuple(seen)

    @property
    def has_deterministic_verification(self) -> bool:
        """True when at least one *test* command exists.

        Lint, typecheck, and build are useful regression signals but none of
        them demonstrates that a task was actually accomplished, so they do not
        by themselves make a repository Fit-evaluable.
        """

        return any(command.kind == "test" for command in self.commands)

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
            "has_deterministic_verification": self.has_deterministic_verification,
            "warnings": list(self.warnings),
        }


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _load_toml(path: Path) -> dict[str, object]:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_READ_BYTES:
            return {}
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_json(path: Path) -> dict[str, object]:
    text = _read_text(path)
    if text is None:
        return {}
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _discover_python(root: Path) -> tuple[list[VerificationCommand], list[str]]:
    found: list[VerificationCommand] = []
    warnings: list[str] = []
    pyproject_path = root / "pyproject.toml"
    pyproject = _load_toml(pyproject_path)
    tools = pyproject.get("tool")
    tools = tools if isinstance(tools, dict) else {}

    if "pytest" in tools or (root / "pytest.ini").is_file() or (root / "tox.ini").is_file():
        source = (
            "pyproject.toml [tool.pytest]"
            if "pytest" in tools
            else ("pytest.ini" if (root / "pytest.ini").is_file() else "tox.ini")
        )
        found.append(
            VerificationCommand(
                kind="test",
                command=("python", "-m", "pytest", "-q"),
                source=source,
                confidence="high",
                evidence=(f"{source} declares pytest configuration",),
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
        found.append(
            VerificationCommand(
                kind="typecheck",
                command=("python", "-m", "mypy", "src"),
                source=source,
                confidence="high" if (root / "src").is_dir() else "medium",
                evidence=(f"{source} declares mypy configuration",),
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

    if pyproject_path.is_file() and not pyproject:
        warnings.append("pyproject.toml exists but could not be parsed")
    return found, warnings


_NODE_SCRIPT_KINDS: tuple[tuple[str, VerificationKind], ...] = (
    ("test", "test"),
    ("lint", "lint"),
    ("typecheck", "typecheck"),
    ("type-check", "typecheck"),
    ("tsc", "typecheck"),
    ("build", "build"),
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
        if script_name in scripts:
            found.append(
                VerificationCommand(
                    kind=kind,
                    command=(runner, "run", script_name),
                    source=f"package.json scripts.{script_name}",
                    confidence="high",
                    evidence=(
                        f"{runner} inferred from lockfile" if runner != "npm" else "npm default",
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

    if not any(command.kind == "test" for command in commands):
        warnings.append(
            "no test command discovered; a Fit experiment cannot verify task "
            "completion in this repository"
        )

    return VerificationInventory(commands=tuple(commands), warnings=tuple(warnings))


__all__ = [
    "Confidence",
    "VerificationCommand",
    "VerificationInventory",
    "VerificationKind",
    "discover_verification",
]
