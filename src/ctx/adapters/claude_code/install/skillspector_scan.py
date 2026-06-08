"""SkillSpector adapter for skill install/load security scans."""

from __future__ import annotations

import os
import re
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


_SAFE_ENV_KEYS = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
}
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:[A-Z0-9_]*"
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)"
    r"[A-Z0-9_]*|HF_TOKEN|GITHUB_TOKEN|OPENAI_API_KEY)"
    r"\s*[:=]\s*)([^\s]+)"
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,})\b"
)
_MAX_OUTPUT_CHARS = 20_000


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


def _scanner_env(*, use_llm: bool) -> dict[str, str] | None:
    if use_llm:
        return None
    safe: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.upper() in _SAFE_ENV_KEYS:
            safe[key] = value
    return safe


def _stringify_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _sanitize_output(output: str) -> str:
    clean = _ANSI_OSC_RE.sub("", output)
    clean = _ANSI_CSI_RE.sub("", clean)
    clean = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", clean)
    clean = _KNOWN_TOKEN_RE.sub("[REDACTED]", clean)
    if len(clean) > _MAX_OUTPUT_CHARS:
        clean = clean[:_MAX_OUTPUT_CHARS] + "\n[truncated SkillSpector output]"
    return clean


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
            env=_scanner_env(use_llm=use_llm),
            timeout=max(timeout_seconds, 1),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = _stringify_output(exc.stdout) + _stringify_output(exc.stderr)
        return SkillSpectorResult(
            status="error",
            command=scan_command,
            exit_code=None,
            output=(
                _sanitize_output(output.strip())
                or f"SkillSpector timed out after {timeout_seconds}s."
            ),
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
    output = _sanitize_output(output)
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
