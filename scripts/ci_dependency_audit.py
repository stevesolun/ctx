"""Prepare and run pip-audit against every installable runtime extra."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import re
import subprocess
import sys
import tomllib

from packaging.requirements import InvalidRequirement, Requirement


VULNERABILITY_ID_RE = re.compile(
    r"(?:"
    r"CVE-[0-9]{4}-[0-9]{4,}"
    r"|GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-"
    r"[23456789cfghjmpqrvwx]{4}"
    r"|PYSEC-[0-9]{4}-[0-9]+"
    r")"
)


class AuditInputError(ValueError):
    """Raised when audit input is unsafe or malformed."""


def _validate_requirement(value: object, source: str) -> str:
    if not isinstance(value, str):
        raise AuditInputError(f"{source} must be a requirement string")
    requirement = value.strip()
    if not requirement:
        raise AuditInputError(f"{source} must not be empty")
    if any(character in requirement for character in ("\0", "\r", "\n")):
        raise AuditInputError(f"{source} must contain exactly one requirement")
    if requirement.startswith("-"):
        raise AuditInputError(f"{source} must not contain a requirement-file directive")
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement as exc:
        raise AuditInputError(f"{source} is not a valid packaging requirement") from exc
    if parsed.url is not None:
        raise AuditInputError(f"{source} must not use a direct URL reference")
    return requirement


def _requirement_list(value: object, source: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AuditInputError(f"{source} must be an array")
    return tuple(
        _validate_requirement(requirement, f"{source}[{index}]")
        for index, requirement in enumerate(value)
    )


def collect_runtime_requirements(manifest_path: Path) -> tuple[str, ...]:
    """Return validated base dependencies plus every optional extra except dev."""

    with manifest_path.open("rb") as stream:
        document = tomllib.load(stream)
    project = document.get("project")
    if not isinstance(project, dict):
        raise AuditInputError("pyproject.toml must contain a [project] table")

    requirements = list(_requirement_list(project.get("dependencies", []), "project.dependencies"))
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise AuditInputError("project.optional-dependencies must be a table")
    if any(not isinstance(name, str) for name in optional):
        raise AuditInputError("optional dependency names must be strings")

    for extra in sorted(name for name in optional if name != "dev"):
        requirements.extend(
            _requirement_list(optional[extra], f"project.optional-dependencies.{extra}")
        )
    if not requirements:
        raise AuditInputError("runtime dependency manifest is empty")
    return tuple(dict.fromkeys(requirements))


def parse_ignore_file(ignore_path: Path | None) -> tuple[str, ...]:
    """Parse an optional comments-friendly vulnerability ignore file."""

    if ignore_path is None or not ignore_path.exists():
        return ()
    if not ignore_path.is_file():
        raise AuditInputError(f"{ignore_path} must be a regular file")

    identifiers: list[str] = []
    for line_number, raw_line in enumerate(
        ignore_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        identifier = raw_line.partition("#")[0].strip()
        if not identifier:
            continue
        if VULNERABILITY_ID_RE.fullmatch(identifier) is None:
            raise AuditInputError(f"{ignore_path}:{line_number}: invalid vulnerability ID")
        identifiers.append(identifier)
    return tuple(dict.fromkeys(identifiers))


def build_pip_audit_argv(
    requirements_path: Path,
    ignore_ids: Sequence[str],
) -> tuple[str, ...]:
    """Build a subprocess argv without shell interpolation."""

    argv = [
        sys.executable,
        "-m",
        "pip_audit",
        "--strict",
        "--progress-spinner",
        "off",
        "--requirement",
        str(requirements_path),
    ]
    for identifier in ignore_ids:
        if VULNERABILITY_ID_RE.fullmatch(identifier) is None:
            raise AuditInputError("invalid vulnerability ID passed to pip-audit")
        argv.extend(("--ignore-vuln", identifier))
    return tuple(argv)


def prepare_audit(
    manifest_path: Path,
    requirements_output: Path,
    ignore_path: Path | None,
) -> tuple[str, ...]:
    """Write validated requirements and return the pip-audit argv."""

    requirements = collect_runtime_requirements(manifest_path)
    requirements_output.write_text(
        "".join(f"{requirement}\n" for requirement in requirements),
        encoding="utf-8",
    )
    ignore_ids = parse_ignore_file(ignore_path)
    return build_pip_audit_argv(requirements_output, ignore_ids)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--requirements-output", type=Path, required=True)
    parser.add_argument(
        "--ignore-file",
        type=Path,
        help="optional UTF-8 file containing one CVE, GHSA, or PYSEC ID per line",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        command = prepare_audit(
            args.manifest,
            args.requirements_output,
            args.ignore_file,
        )
    except (AuditInputError, OSError, tomllib.TOMLDecodeError) as exc:
        parser.error(str(exc))
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
