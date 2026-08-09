"""Driving a real coding agent through one task.

CTX already ships a model-agnostic harness (``ctx run``) with per-execution
budget caps, iteration and timeout bounds, machine-readable output, and honest
nullable cost. Reimplementing an agent loop here would duplicate a tested
subsystem for no gain, so this module drives that one as a subprocess and
translates its result.

The translation is deliberately narrow. This module reports what the harness
spent and whether it finished; it never reports whether the work was correct.
That judgement belongs to the repository's own tests, in
:mod:`ctx.fit.live_runner`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from ctx.fit.live_runner import AgentDriver, AgentInvocation, AgentOutcome

DEFAULT_MAX_ITERATIONS = 25

#: A per-execution ceiling so one runaway trial cannot consume a campaign
#: budget. The campaign-level budget is enforced separately, before any spend.
DEFAULT_PER_TRIAL_BUDGET_USD = 2.0


class ProviderUnavailable(RuntimeError):
    """No usable way to run a real agent was found."""


def _prompt(invocation: AgentInvocation) -> str:
    """Describe the task without disclosing how it was originally solved."""

    files = "\n".join(f"- {path}" for path in invocation.files_to_change)
    verify = " ".join(invocation.verify_command)
    return (
        f"{invocation.task_title}\n\n"
        "The tests in this repository describe behavior that is currently "
        "missing. Implement it.\n\n"
        f"Files expected to change:\n{files}\n\n"
        f"Verify your work with:\n    {verify}\n\n"
        "Do not modify the tests. They are the specification."
    )


def build_agent_driver(
    *,
    executable: str | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    per_trial_budget_usd: float = DEFAULT_PER_TRIAL_BUDGET_USD,
) -> AgentDriver:
    """Return a driver backed by ``ctx run``.

    Raises :class:`ProviderUnavailable` at build time rather than mid-campaign,
    so a missing harness is reported before any workspace is prepared.
    """

    binary = executable or shutil.which("ctx")
    if binary is None:
        raise ProviderUnavailable(
            "the `ctx` harness is not on PATH, so no real agent can be driven"
        )

    def drive(invocation: AgentInvocation) -> AgentOutcome:
        command = [
            binary,
            "run",
            "--json",
            "--max-iterations",
            str(max_iterations),
            "--budget-usd",
            str(per_trial_budget_usd),
        ]
        if invocation.model:
            command.extend(["--model", invocation.model])
        command.append(_prompt(invocation))

        try:
            completed = subprocess.run(
                command,
                cwd=str(invocation.workspace),
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return AgentOutcome(completed=False, detail=f"harness could not run: {exc}")

        payload: dict[str, object] = {}
        for line in reversed(completed.stdout.splitlines()):
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    decoded = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    payload = decoded
                    break

        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}

        def integer(name: str) -> int | None:
            value = usage.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        cost = usage.get("cost_usd")
        # Unknown stays unknown: a missing cost must never become zero, or a
        # candidate would look cheaper for having been measured less.
        cost_usd = (
            float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None
        )

        stop_reason = payload.get("stop_reason")
        return AgentOutcome(
            completed=completed.returncode == 0,
            input_tokens=integer("input_tokens"),
            output_tokens=integer("output_tokens"),
            cost_usd=cost_usd,
            detail=stop_reason if isinstance(stop_reason, str) else "",
        )

    return drive


def provider_diagnostics() -> tuple[bool, str]:
    """Whether a real run is currently possible, and why not if it is not."""

    if shutil.which("ctx") is None and not sys.executable:
        return False, "the ctx harness is not available"
    try:
        build_agent_driver()
    except ProviderUnavailable as exc:
        return False, str(exc)
    return True, "the ctx harness is available"


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_PER_TRIAL_BUDGET_USD",
    "ProviderUnavailable",
    "build_agent_driver",
    "provider_diagnostics",
]
