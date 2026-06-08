"""SkillSpector adapter for skill install/load security scans."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class SkillSpectorResult:
    """Result from a best-effort SkillSpector scan."""

    status: str  # passed | findings | missing | error | skipped
    command: list[str]
    exit_code: int | None
    output: str

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def _resolve_command(
    command: Sequence[str] | None = None,
    binary: str | None = None,
) -> list[str] | None:
    if command:
        return [str(part) for part in command]
    configured = binary or os.environ.get("CTX_SKILLSPECTOR_BIN") or "skillspector"
    if os.sep in configured or (os.altsep and os.altsep in configured):
        return [configured] if Path(configured).exists() else None
    found = shutil.which(configured)
    return [found] if found else None


def _stringify_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_skillspector_scan(
    target: Path,
    *,
    command: Sequence[str] | None = None,
    binary: str | None = None,
    use_llm: bool = False,
    timeout_seconds: int = 120,
) -> SkillSpectorResult:
    """Run SkillSpector against ``target`` and return captured output.

    SkillSpector is intentionally an external tool here. ctx supports Python
    3.11 while SkillSpector currently requires Python 3.12+, so depending on
    the package directly would make ordinary ctx installs heavier and less
    portable. The adapter runs static-only scans by default and preserves the
    tool's stdout/stderr so the user sees SkillSpector's own report.
    """
    resolved = _resolve_command(command=command, binary=binary)
    if resolved is None:
        return SkillSpectorResult(
            status="missing",
            command=[binary or os.environ.get("CTX_SKILLSPECTOR_BIN") or "skillspector"],
            exit_code=None,
            output=(
                "SkillSpector is not installed or not on PATH. Install it, or set "
                "CTX_SKILLSPECTOR_BIN to the scanner executable."
            ),
        )

    scan_command = [
        *resolved,
        "scan",
        str(target),
        "--format",
        "terminal",
    ]
    if not use_llm:
        scan_command.append("--no-llm")

    try:
        completed = subprocess.run(
            scan_command,
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds, 1),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = _stringify_output(exc.stdout) + _stringify_output(exc.stderr)
        return SkillSpectorResult(
            status="error",
            command=scan_command,
            exit_code=None,
            output=(output.strip() or f"SkillSpector timed out after {timeout_seconds}s."),
        )
    except OSError as exc:
        return SkillSpectorResult(
            status="error",
            command=scan_command,
            exit_code=None,
            output=f"SkillSpector failed to start: {exc}",
        )

    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part and part.strip()
    )
    if completed.returncode == 0:
        status = "passed"
    elif completed.returncode == 1:
        status = "findings"
    else:
        status = "error"
    return SkillSpectorResult(
        status=status,
        command=scan_command,
        exit_code=completed.returncode,
        output=output,
    )
