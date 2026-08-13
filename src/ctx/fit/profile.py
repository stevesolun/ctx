"""The normalized repository profile CTX Fit reasons about.

This deliberately does **not** re-implement repository analysis.  CTX already
detects languages, frameworks, testing tools, and AI tooling with per-signal
confidence and evidence; that work is reused verbatim.  What this module adds is
the part CTX never needed before: how the repository verifies itself, what AI
coding configuration it already has, and which optimization dimensions are
therefore worth evaluating.

The profile is versioned because downstream Fit results are meant to be
reproducible and machine-readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctx.fit.verification import VerificationInventory, discover_verification

#: Bump when the emitted structure changes in a way consumers must notice.
FIT_PROFILE_SCHEMA = "ctx.fit.profile-v1"

_DEFAULT_SCAN_DEPTH = 4

#: Files that describe how an AI coding agent should behave in this repository.
_INSTRUCTION_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONVENTIONS.md",
    ".cursorrules",
    ".windsurfrules",
    "GEMINI.md",
)

#: Configuration describing tools an agent may reach for.
_TOOL_CONFIG_PATHS: tuple[str, ...] = (
    ".mcp.json",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".codex/config.toml",
    ".cursor/mcp.json",
)

#: Directories holding capability material an agent can load.
_CAPABILITY_DIRS: tuple[tuple[str, str], ...] = (
    (".claude/skills", "skills"),
    (".claude/agents", "agents"),
    (".codex/skills", "skills"),
    (".agents/skills", "skills"),
)


@dataclass(frozen=True, slots=True)
class ExistingAiConfig:
    """The AI coding setup a repository already carries.

    This is the honest starting point for any comparison: the baseline is what
    the repository does today, not an idealized empty configuration.
    """

    instruction_files: tuple[str, ...] = ()
    tool_config_files: tuple[str, ...] = ()
    capability_dirs: tuple[str, ...] = ()
    capability_counts: tuple[tuple[str, int], ...] = ()

    @property
    def is_configured(self) -> bool:
        return bool(self.instruction_files or self.tool_config_files or self.capability_dirs)

    def to_dict(self) -> dict[str, object]:
        return {
            "instruction_files": list(self.instruction_files),
            "tool_config_files": list(self.tool_config_files),
            "capability_dirs": list(self.capability_dirs),
            "capability_counts": dict(self.capability_counts),
            "is_configured": self.is_configured,
        }


@dataclass(frozen=True, slots=True)
class OptimizationDimension:
    """One axis CTX Fit could vary, and whether it can do so honestly today."""

    name: str
    evaluable: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "evaluable": self.evaluable, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class FitProfile:
    """Everything Fit knows about a repository before any model is invoked."""

    schema: str
    repo_path: str
    stack: dict[str, Any]
    verification: VerificationInventory
    existing_ai_config: ExistingAiConfig
    dimensions: tuple[OptimizationDimension, ...]
    warnings: tuple[str, ...] = ()

    @property
    def is_fit_evaluable(self) -> bool:
        """Whether a Fit experiment on this repository could mean anything.

        Without deterministic verification there is no way to distinguish a
        configuration that solved a task from one that merely claimed to, so
        the product must decline rather than produce a confident guess.
        """

        return self.verification.has_deterministic_verification

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "repo_path": self.repo_path,
            "stack": self.stack,
            "verification": self.verification.to_dict(),
            "existing_ai_config": self.existing_ai_config.to_dict(),
            "dimensions": [dimension.to_dict() for dimension in self.dimensions],
            "is_fit_evaluable": self.is_fit_evaluable,
            "warnings": list(self.warnings),
        }


def _count_entries(path: Path) -> int:
    try:
        return sum(1 for child in path.iterdir() if not child.name.startswith("."))
    except OSError:
        return 0


def _detect_existing_ai_config(root: Path) -> ExistingAiConfig:
    instruction_files = tuple(name for name in _INSTRUCTION_FILES if (root / name).is_file())
    tool_config_files = tuple(name for name in _TOOL_CONFIG_PATHS if (root / name).is_file())

    capability_dirs: list[str] = []
    counts: dict[str, int] = {}
    for relative, label in _CAPABILITY_DIRS:
        directory = root / relative
        if directory.is_dir():
            capability_dirs.append(relative)
            counts[label] = counts.get(label, 0) + _count_entries(directory)

    return ExistingAiConfig(
        instruction_files=instruction_files,
        tool_config_files=tool_config_files,
        capability_dirs=tuple(capability_dirs),
        capability_counts=tuple(sorted(counts.items())),
    )


def _dimensions(verification: VerificationInventory) -> tuple[OptimizationDimension, ...]:
    """Report which axes are honestly evaluable given today's execution rig.

    These reflect real constraints of the current CTX experiment path, not
    aspirations. Claiming an axis the rig cannot vary would let the product
    imply a comparison it never made.
    """

    verifiable = verification.has_deterministic_verification
    blocked = "no deterministic test command was discovered"
    return (
        OptimizationDimension(
            name="ctx-capability-set",
            evaluable=verifiable,
            reason=(
                "skills, agents, and MCP servers can be varied inside one "
                "counterbalanced baseline-versus-candidate pair"
                if verifiable
                else blocked
            ),
        ),
        OptimizationDimension(
            name="repository-instructions",
            evaluable=verifiable,
            reason=(
                "instruction text is delivered as prepared context on the same path"
                if verifiable
                else blocked
            ),
        ),
        OptimizationDimension(
            name="model",
            evaluable=verifiable,
            reason=(
                "the model is a run-level setting, so models compare across pinned "
                "runs rather than inside one pair, which is a weaker control"
                if verifiable
                else blocked
            ),
        ),
        OptimizationDimension(
            name="coding-harness",
            evaluable=False,
            reason=(
                "the current execution rig supports a single harness, so harness "
                "comparison is out of scope and must not be implied"
            ),
        ),
    )


def build_fit_profile(
    repo_path: str | Path,
    *,
    max_depth: int = _DEFAULT_SCAN_DEPTH,
) -> FitProfile:
    """Produce a Fit profile for one repository without invoking any model.

    Reuses CTX's existing repository intelligence for the stack portion and
    adds the Fit-specific inventories on top.
    """

    root = Path(repo_path)
    warnings: list[str] = []
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")

    # Imported lazily: scan_repo is a flat legacy module, and importing it at
    # module scope would make the whole ctx.fit package depend on the legacy
    # layout at import time.
    try:
        import scan_repo
    except ImportError:  # pragma: no cover - defensive
        stack: dict[str, Any] = {}
        warnings.append("repository stack analysis unavailable: scan_repo could not be imported")
    else:
        signals = scan_repo.scan_directory(str(root), max_depth=max_depth)
        stack = scan_repo.detect_stack(str(root), signals)
        # The legacy scanner stamps wall-clock time and an absolute path into
        # its result. A Fit profile must be reproducible: identical inputs have
        # to serialize identically, or provenance comparisons between runs are
        # meaningless. Both facts are already carried by the profile itself.
        for volatile in ("scanned_at", "repo_path"):
            stack.pop(volatile, None)

    verification = discover_verification(root)
    warnings.extend(verification.warnings)

    return FitProfile(
        schema=FIT_PROFILE_SCHEMA,
        repo_path=str(root.resolve()),
        stack=stack,
        verification=verification,
        existing_ai_config=_detect_existing_ai_config(root),
        dimensions=_dimensions(verification),
        warnings=tuple(warnings),
    )


__all__ = [
    "FIT_PROFILE_SCHEMA",
    "ExistingAiConfig",
    "FitProfile",
    "OptimizationDimension",
    "build_fit_profile",
]
