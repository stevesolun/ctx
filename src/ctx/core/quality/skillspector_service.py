"""Reusable SkillSpector service for ctx skill intake/install gates.

SkillSpector stays optional and external because ctx supports Python 3.11 while
SkillSpector currently requires Python 3.12+. This module is the ctx-wide
adapter used by CLI, dashboard, and host-specific integrations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
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

    @property
    def passed(self) -> bool:
        return self.status == "passed"

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


def skill_scan_target(source_path: Path) -> Path:
    """Return the path SkillSpector should scan for a candidate skill."""
    if source_path.is_file() and source_path.name.lower() == "skill.md":
        return source_path.parent
    return source_path


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
    """Run SkillSpector against ``target`` and return captured output."""
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
        part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip()
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


def run_skillspector_scan_text(
    slug: str,
    markdown_text: str,
    *,
    command: Sequence[str] | None = None,
    binary: str | None = None,
    use_llm: bool = False,
    timeout_seconds: int = 120,
) -> SkillSpectorResult:
    """Run SkillSpector against an in-memory skill body."""
    safe_slug = re.sub(r"[^a-zA-Z0-9_.+-]+", "-", slug).strip("-") or "skill"
    with tempfile.TemporaryDirectory(prefix="ctx-skillspector-") as temp_root:
        skill_dir = Path(temp_root) / safe_slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(markdown_text, encoding="utf-8")
        return run_skillspector_scan(
            skill_dir,
            command=command,
            binary=binary,
            use_llm=use_llm,
            timeout_seconds=timeout_seconds,
        )


def render_scan_report(result: SkillSpectorResult) -> str:
    """Return a concise user-facing report for a scan result."""
    lines = [
        f"SkillSpector: {result.status}",
        "Command: " + " ".join(result.command),
    ]
    if result.output:
        lines.extend(["", result.output])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run ctx's SkillSpector service gate on a skill path."
    )
    parser.add_argument("target", help="Skill directory or SKILL.md path to scan")
    parser.add_argument(
        "--optional", action="store_true", help="Return 0 even when the scan does not pass"
    )
    parser.add_argument("--use-llm", action="store_true", help="Allow SkillSpector LLM analysis")
    parser.add_argument(
        "--skillspector-bin", default=None, help="SkillSpector executable path/name"
    )
    parser.add_argument("--timeout", type=int, default=120, help="SkillSpector timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)

    target = skill_scan_target(Path(args.target).expanduser())
    result = run_skillspector_scan(
        target,
        binary=args.skillspector_bin,
        use_llm=args.use_llm,
        timeout_seconds=args.timeout,
    )
    if args.json:
        print(json.dumps(result.to_json(), indent=2, sort_keys=True))
    else:
        print(render_scan_report(result))
    return 0 if result.passed or args.optional else 1


if __name__ == "__main__":
    raise SystemExit(main())
