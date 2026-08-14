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

It is, however, the only place a candidate's *configuration* becomes something
the agent can actually experience. If a capability does not reach the command
built here, two candidates run byte-identically and every difference the report
attributes to them is noise (FITBUG-002). So a capability is either delivered
through a channel the harness genuinely honours, or the trial is refused --
never quietly dropped.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ctx.fit.candidates import CandidateConfiguration, render_candidate_user_context
from ctx.fit.live_runner import AgentDriver, AgentInvocation, AgentOutcome, _symlink_hop_paths
from ctx.fit.sandbox import SandboxUnavailable, require_sandbox_available, sandboxed_command

DEFAULT_MAX_ITERATIONS = 25

#: A per-execution ceiling so one runaway trial cannot consume a campaign
#: budget. The campaign-level budget is enforced separately, before any spend.
DEFAULT_PER_TRIAL_BUDGET_USD = 2.0

# The coding surface is a harness contract, not prompt advice.  One existing
# filesystem MCP is rooted at the trial subprocess's cwd; built-in CTX tools
# are disabled and no Git, shell, or network-capable server is attached.
_WORKSPACE_MCP_SPEC = "filesystem:."
_WORKSPACE_TOOL_PATTERN = "filesystem__*"

# Provider HTTPS calls may depend on an enterprise proxy or CA bundle.  These
# are connection settings, not a general environment inheritance channel.
_NETWORK_RUNTIME_ENV = frozenset(
    {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)


class ProviderUnavailable(RuntimeError):
    """No usable way to run a real agent was found."""


class CapabilityNotApplicable(ProviderUnavailable):
    """A candidate asks for something this driver cannot honestly deliver.

    Raised per trial rather than at build time: the campaign's other arms are
    still runnable. :mod:`ctx.fit.live_runner` turns a driver exception into an
    ``infrastructure-failure``, which is the correct reading -- the
    configuration was never tested, so it must not be scored as having failed.
    """


def _require_harness_dependency() -> None:
    """Refuse a paid driver when its optional provider runtime is absent."""

    try:
        import litellm  # noqa: F401
    except ImportError as exc:
        raise ProviderUnavailable(
            "the live harness dependency is not installed; install "
            "`claude-ctx[harness]` before running a real CTX Fit evaluation"
        ) from exc


@dataclass(frozen=True, slots=True)
class ModelCredential:
    """The one credential environment variable selected for one model.

    ``configured`` means only that a non-empty value is present. It never
    claims the provider accepted that value; authentication requires a remote
    request, which profile, doctor, and pre-spend selection deliberately avoid.
    """

    model: str
    environment_variable: str | None
    configured: bool


def resolve_model_credential(
    model: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> ModelCredential:
    """Resolve model → credential through the harness's own key resolver.

    Provider-prefixed models are already understood by ``ctx run``. CTX Fit's
    own unprefixed default is resolved from the provider declared beside that
    default, so clean base installs do not depend on optional LiteLLM metadata.
    Other bare model names use LiteLLM metadata when the harness extra is
    installed. The resulting provider is always passed to the same
    side-effect-free key resolver the harness calls; CTX keeps no second key
    table here.
    """

    from ctx.cli.run import _resolve_api_key_env
    from ctx.fit.experiment import DEFAULT_MODEL, DEFAULT_MODEL_PROVIDER

    provider: str | None = DEFAULT_MODEL_PROVIDER if model == DEFAULT_MODEL else None
    name = _resolve_api_key_env(None, model, provider)
    if name is None:
        try:
            import litellm
        except ImportError:
            pass
        else:
            table = getattr(litellm, "model_cost", None)
            entry = table.get(model) if isinstance(table, dict) else None
            candidate = entry.get("litellm_provider") if isinstance(entry, dict) else None
            if isinstance(candidate, str) and candidate:
                provider = candidate
                name = _resolve_api_key_env(None, model, provider)

    source = os.environ if environment is None else environment
    return ModelCredential(
        model=model,
        environment_variable=name,
        configured=bool(name and source.get(name)),
    )


def _prompt(invocation: AgentInvocation) -> str:
    """The task, plus the capabilities that make this candidate itself.

    Both halves matter. Without the first the agent has nothing to do; without
    the second every candidate sends the same bytes and the experiment varies
    nothing.
    """

    files = "\n".join(f"- {path}" for path in invocation.files_to_change)
    verify = " ".join(invocation.verify_command)
    sections = [
        f"{invocation.task_title}\n\n"
        "The tests in this repository describe behavior that is currently "
        "missing. Implement it.\n\n"
        f"Files expected to change:\n{files}\n\n"
        f"Verify your work with:\n    {verify}",
    ]
    try:
        candidate_context = render_candidate_user_context(
            CandidateConfiguration(
                candidate_id="fit-trial",
                role="baseline",
                capability_ids=invocation.capability_ids,
                model=invocation.model or "provider-default",
                instructions=invocation.instructions,
                selection_reason="Exact material for this controlled CTX Fit trial.",
                capability_materials=invocation.capability_materials,
                instruction_materials=invocation.instruction_materials,
            )
        )
    except ValueError as exc:
        raise CapabilityNotApplicable(
            f"the invocation's exact candidate material is not reproducible: {exc}"
        ) from exc
    sections.append(candidate_context)
    # Last, so an untrusted capability body is never the closing instruction.
    sections.append("Do not modify the tests. They are the specification.")
    return "\n\n".join(sections)


def _command(
    binary: str,
    invocation: AgentInvocation,
    *,
    max_iterations: int,
    per_trial_budget_usd: float,
) -> list[str]:
    """The exact argv one trial is driven with.

    Shared with the build-time parse check below, so the command that gets
    validated and the command that gets executed cannot drift apart.
    """

    command = [
        binary,
        "run",
        "--fit-controlled-trial",
        "--no-ctx-tools",
        "--mcp",
        _WORKSPACE_MCP_SPEC,
        "--allow-tool",
        _WORKSPACE_TOOL_PATTERN,
        "--json",
        "--max-iterations",
        str(max_iterations),
        "--budget-usd",
        str(per_trial_budget_usd),
    ]
    if invocation.model:
        command.extend(["--model", invocation.model])
    # `ctx run` takes no positionals and declares --task required. Appending the
    # prompt bare dies in argparse before any model is contacted.
    command.extend(["--task", _prompt(invocation)])
    return command


#: Stands in for a real trial when checking the argv shape. Only the flags and
#: their arity are under test, so the values are deliberately uninteresting.
#: A model is set so the optional ``--model`` branch is covered too.
_PROBE_INVOCATION = AgentInvocation(
    workspace=Path("."),
    task_title="probe",
    files_to_change=("src/example.py",),
    verify_command=("pytest", "-q"),
    capability_ids=(),
    model="openai/gpt-4o-mini",
)


def _reject_unparsable_command(command: Sequence[str]) -> None:
    """Refuse to build a driver whose argv ``ctx run`` would not accept.

    This module and the harness CLI evolve independently. When they drift the
    subprocess exits 2 in argparse, the repository's tests are still red, and
    every candidate is recorded as having failed -- a tooling bug rendered as
    evidence about the user's repository. Checking the argv against the real
    parser converts that silence into a refusal before any workspace is built.
    """

    try:
        from ctx.cli.run import _build_parser
    except ImportError:  # pragma: no cover - the CLI ships with this package
        return

    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            _build_parser().parse_args(list(command[1:]))
    except SystemExit as exc:
        complaint = stderr.getvalue().strip().splitlines()
        detail = complaint[-1] if complaint else f"exit {exc.code}"
        raise ProviderUnavailable(
            f"the command this driver builds is rejected by `ctx run`: {detail}"
        ) from exc


def _decode_payload(stdout: str) -> dict[str, object]:
    """Recover the harness's result object from its stdout.

    ``ctx run --json`` pretty-prints with ``indent=2``, so the payload spans
    many lines and its first line is a bare "{". Reading stdout a line at a
    time therefore decodes nothing and leaves cost unknown on every trial --
    and unknown cost poisons the candidate total by design, so nothing can be
    ranked. Parse the whole stream instead.
    """

    text = stdout.strip()
    if not text:
        return {}

    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        return whole if isinstance(whole, dict) else {}

    # Something else shared stdout. Walk the top-level objects, stepping over
    # each one's nested braces, and keep the last: the harness prints its
    # result after whatever preamble preceded it.
    decoder = json.JSONDecoder()
    payload: dict[str, object] = {}
    index = 0
    while (start := text.find("{", index)) != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            payload = value
        index = max(end, start + 1)
    return payload


def _trial_environment(runtime_root: Path, *, model: str) -> dict[str, str]:
    """Build the complete, least-authority environment for one harness.

    ``ctx run`` and its filesystem MCP need an executable search path.  The
    provider receives only the environment variable selected for this model,
    plus local proxy/CA settings. Nothing else crosses the process boundary. HOME and
    every conventional temporary-directory variable point inside the
    throwaway repository so harness session state and MCP caches cannot read or
    write the user's real home or an ambient temporary tree.
    """

    home = runtime_root / "home"
    temporary = runtime_root / "tmp"
    home.mkdir(parents=True, exist_ok=False)
    temporary.mkdir(parents=True, exist_ok=False)

    credential = resolve_model_credential(model)
    allowed_names = set(_NETWORK_RUNTIME_ENV)
    if credential.environment_variable is not None:
        allowed_names.add(credential.environment_variable)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in allowed_names or name == "LANG" or name.startswith("LC_")
    }
    environment.update(
        {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONUNBUFFERED": "1",
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "TMPDIR": str(temporary),
        }
    )
    return environment


def _runtime_read_access(
    *executables: str,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Trusted provider runtime trees and exact multihop executable paths."""

    roots: list[Path] = [Path(__file__).resolve().parents[2]]
    paths: list[Path] = []
    for name in executables:
        hops = _symlink_hop_paths(Path(name))
        for path in hops:
            if path not in paths:
                paths.append(path)
        root = hops[-1].parent.parent.resolve(strict=False)
        if root not in roots:
            roots.append(root)
    return tuple(roots), tuple(paths)


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
    _require_harness_dependency()
    try:
        require_sandbox_available({"PATH": os.environ.get("PATH", os.defpath)})
    except SandboxUnavailable as exc:
        raise ProviderUnavailable(f"a trial cannot be isolated: {exc}") from exc
    npx_executable = shutil.which("npx")
    if npx_executable is None:
        raise ProviderUnavailable(
            "npx is not on PATH, so the workspace filesystem MCP cannot be started"
        )

    _reject_unparsable_command(
        _command(
            binary,
            _PROBE_INVOCATION,
            max_iterations=max_iterations,
            per_trial_budget_usd=per_trial_budget_usd,
        )
    )

    def drive(invocation: AgentInvocation) -> AgentOutcome:
        command = _command(
            binary,
            invocation,
            max_iterations=max_iterations,
            per_trial_budget_usd=per_trial_budget_usd,
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix=".ctx-fit-runtime-", dir=invocation.workspace
            ) as runtime_directory:
                environment = _trial_environment(
                    Path(runtime_directory), model=invocation.model or ""
                )
                read_roots, read_paths = _runtime_read_access(
                    binary, npx_executable, sys.executable
                )
                isolated = sandboxed_command(
                    command,
                    cwd=invocation.workspace,
                    writable_root=invocation.workspace,
                    # The model provider needs HTTPS. No repository-controlled
                    # command runs in this process; the only attached tool is
                    # the workspace-rooted filesystem MCP.
                    network=True,
                    environment=environment,
                    read_roots=read_roots,
                    read_paths=read_paths,
                )
                completed = subprocess.run(
                    isolated,
                    cwd=str(invocation.workspace),
                    capture_output=True,
                    text=True,
                    timeout=900,
                    check=False,
                    env=environment,
                )
        except (OSError, SandboxUnavailable, subprocess.SubprocessError) as exc:
            return AgentOutcome(
                completed=False,
                stop_reason="infrastructure_failure",
                logs=str(exc)[-2000:],
                detail=f"harness could not run: {exc}",
            )

        payload = _decode_payload(completed.stdout)

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

        reported_stop = payload.get("stop_reason")
        stop_reason = reported_stop if isinstance(reported_stop, str) else ""
        reported_detail = payload.get("detail")
        detail = reported_detail if isinstance(reported_detail, str) else ""
        if not detail:
            detail = stop_reason
        if not detail and completed.returncode != 0:
            # A non-zero exit that said nothing is the harness rejecting us, not
            # the candidate failing. Record it rather than returning in silence.
            detail = f"harness exited {completed.returncode}: {completed.stderr.strip()[-300:]}"

        return AgentOutcome(
            completed=completed.returncode == 0,
            input_tokens=integer("input_tokens"),
            output_tokens=integer("output_tokens"),
            cost_usd=cost_usd,
            detail=detail,
            stop_reason=stop_reason,
            logs=completed.stderr[-2000:],
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
    "CapabilityNotApplicable",
    "ModelCredential",
    "ProviderUnavailable",
    "build_agent_driver",
    "provider_diagnostics",
    "resolve_model_credential",
]
