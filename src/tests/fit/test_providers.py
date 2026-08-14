"""The seam between CTX Fit and the paid harness.

Both properties tested here failed silently in production rather than loudly:
a rejected command line and an undecodable result both turn into "this
candidate did not work on your repository". Nothing downstream can tell the
difference, so the checks have to live at the seam itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ctx.fit import providers
from ctx.fit.candidates import CapabilityMaterial, InstructionMaterial
from ctx.fit.live_runner import AgentInvocation
from ctx.fit.providers import (
    CapabilityNotApplicable,
    ProviderUnavailable,
    build_agent_driver,
    resolve_model_credential,
)

_REAL_REQUIRE_HARNESS_DEPENDENCY = providers._require_harness_dependency
_REAL_REQUIRE_OPERATIONAL_SANDBOX = providers._require_operational_sandbox
_REAL_SANDBOXED_COMMAND = providers.sandboxed_command


@pytest.fixture(autouse=True)
def _deterministic_production_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply process-level stand-ins for the production sandbox and MCP host."""

    npx = tmp_path / "npx"
    npx.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8")
    npx.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(tmp_path), os.environ.get("PATH", ""))))
    monkeypatch.setattr(providers, "require_sandbox_available", lambda environment: None)
    monkeypatch.setattr(providers, "_require_harness_dependency", lambda: None)
    monkeypatch.setattr(providers, "_require_operational_sandbox", lambda environment: None)
    monkeypatch.setattr(
        providers,
        "sandboxed_command",
        lambda command, **kwargs: tuple(command),
    )


def _invocation(
    workspace: Path,
    *,
    model: str | None = None,
    capability_ids: tuple[str, ...] = ("skill:ctx-python-testing",),
    instruction_body: str | None = None,
) -> AgentInvocation:
    materials = tuple(
        CapabilityMaterial.from_content(
            capability_id=capability_id,
            delivery_mode="task-user-context",
            source_identity=f"test-catalog#{capability_id}",
            catalog_entry_digest=hashlib.sha256(capability_id.encode()).hexdigest(),
            content=(
                providers._shipped_skill_bodies()[capability_id]
                if hasattr(providers, "_shipped_skill_bodies")
                else f"Exact test material for {capability_id}"
            ),
        )
        for capability_id in capability_ids
        if capability_id.startswith("skill:") and capability_id != "skill:nothing-ships-this"
    )
    instruction_materials = (
        (InstructionMaterial.from_content(path="AGENTS.md", content=instruction_body),)
        if instruction_body is not None
        else ()
    )
    return AgentInvocation(
        workspace=workspace,
        task_title="Reject empty capability ids",
        files_to_change=("src/ctx/fit/candidates.py",),
        verify_command=("pytest", "-q", "src/tests/fit"),
        capability_ids=capability_ids,
        model=model,
        capability_materials=materials,
        instructions=tuple(item.path for item in instruction_materials),
        instruction_materials=instruction_materials,
    )


def _real_ctx_run_json_stdout(
    *,
    input_tokens: int = 18_432,
    output_tokens: int = 2_105,
    cost_usd: float | None = 0.0417,
    stop_reason: str = "done",
) -> str:
    """The exact stdout ``ctx run --json`` writes, produced by the real emitter.

    Generated rather than transcribed: a hand-written fixture would encode this
    module's belief about the harness's output, which is precisely the belief
    that was wrong.
    """

    from ctx.adapters.generic.loop import LoopResult
    from ctx.adapters.generic.providers.base import Usage
    from ctx.cli.run import _emit_result

    result = LoopResult(
        stop_reason=stop_reason,  # type: ignore[arg-type]
        final_message="Implemented the missing validation.",
        iterations=4,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cached_input_tokens=1_024,
            tokens_reported=True,
        ),
        messages=(),
        detail="",
    )

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _emit_result(result, "session-abc", as_json=True, quiet=True)
    return buffer.getvalue()


def _fake_harness(tmp_path: Path, stdout: str, *, exit_code: int = 0) -> tuple[Path, Path]:
    """A stand-in ``ctx`` that records its argv and replays real harness bytes."""

    argv_path = tmp_path / "argv.json"
    stdout_path = tmp_path / "stdout.txt"
    stdout_path.write_text(stdout, encoding="utf-8")

    script = tmp_path / "fake-ctx"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(argv_path)!r}, 'w'))\n"
        f"sys.stdout.write(open({str(stdout_path)!r}).read())\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, argv_path


def _fake_harness_with_environment(tmp_path: Path, stdout: str) -> tuple[Path, Path, Path]:
    """A stand-in that records both argv and the exact inherited environment."""

    script, argv_path = _fake_harness(tmp_path, stdout)
    env_path = tmp_path / "env.json"
    original = script.read_text(encoding="utf-8")
    script.write_text(
        original.replace(
            "import json, sys\n",
            f"import json, os, sys\njson.dump(dict(os.environ), open({str(env_path)!r}, 'w'))\n",
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, argv_path, env_path


@pytest.mark.parametrize(
    ("model", "environment", "expected_name", "configured"),
    (
        ("gpt-4o-mini", {"ANTHROPIC_API_KEY": "wrong"}, "OPENAI_API_KEY", False),
        ("gpt-4o-mini", {"OPENAI_API_KEY": "right"}, "OPENAI_API_KEY", True),
        (
            "anthropic/claude-sonnet-4-20250514",
            {"ANTHROPIC_API_KEY": "right"},
            "ANTHROPIC_API_KEY",
            True,
        ),
        ("gpt-4o-mini", {"CTX_FIT_API_KEY": "unused"}, "OPENAI_API_KEY", False),
    ),
)
def test_model_credential_resolution_uses_the_ctx_run_provider_contract(
    model: str,
    environment: dict[str, str],
    expected_name: str,
    configured: bool,
) -> None:
    resolved = resolve_model_credential(model, environment=environment)

    assert resolved.environment_variable == expected_name
    assert resolved.configured is configured


def test_default_model_credential_resolution_does_not_require_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean CTX install still knows which key its own default model uses."""

    monkeypatch.setitem(sys.modules, "litellm", None)

    resolved = resolve_model_credential("gpt-4o-mini", environment={"OPENAI_API_KEY": "configured"})

    assert resolved.environment_variable == "OPENAI_API_KEY"
    assert resolved.configured is True


# ── FITBUG-006: the command must be one `ctx run` accepts ──────────────────


def test_the_prompt_is_passed_as_task_not_as_a_positional(tmp_path: Path) -> None:
    """`ctx run` has no positionals, so a bare prompt dies in argparse."""

    stdout = _real_ctx_run_json_stdout()
    binary, argv_path = _fake_harness(tmp_path, stdout)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    build_agent_driver(executable=str(binary))(_invocation(workspace))

    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    assert "--task" in argv
    assert argv[argv.index("--task") + 1].startswith("Reject empty capability ids")


def test_the_driven_command_parses_against_the_real_ctx_run_parser(tmp_path: Path) -> None:
    """The argv we build and the argv the CLI accepts must be the same argv."""

    from ctx.cli.run import _build_parser

    binary, argv_path = _fake_harness(tmp_path, _real_ctx_run_json_stdout())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    build_agent_driver(executable=str(binary))(_invocation(workspace, model="openai/gpt-5.5"))

    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    args = _build_parser().parse_args(argv)

    assert args.command == "run"
    assert args.json is True
    assert args.model == "openai/gpt-5.5"
    assert args.task.startswith("Reject empty capability ids")


def test_a_trial_gets_only_a_workspace_rooted_filesystem_tool_surface(tmp_path: Path) -> None:
    """A coding trial without edit tools measures nothing about coding ability.

    The surface is deliberately one existing MCP preset, rooted by the
    subprocess cwd.  Built-in CTX tools, Git, shell, and any second MCP server
    are absent, so the model cannot turn a repository trial into ambient host
    access.
    """

    from ctx.cli.run import _build_parser, _parse_mcp_spec

    binary, argv_path = _fake_harness(tmp_path, _real_ctx_run_json_stdout())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    build_agent_driver(executable=str(binary))(_invocation(workspace))

    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    args = _build_parser().parse_args(argv)
    configs = tuple(_parse_mcp_spec(spec) for spec in args.mcp)

    assert args.no_ctx_tools is True
    assert args.mcp == ["filesystem:."]
    assert args.allow_tool == ["filesystem__*"]
    assert len(configs) == 1
    assert configs[0].name == "filesystem"
    assert configs[0].args[-1] == "."
    assert not any(name.startswith(("ctx__", "git__", "shell__")) for name in args.allow_tool)


@pytest.mark.parametrize(
    ("missing", "message"),
    (("sandbox", "isolated"), ("npx", "filesystem MCP")),
)
def test_building_a_driver_refuses_when_required_isolation_tooling_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    message: str,
) -> None:
    """A paid campaign must not discover an absent boundary after it starts."""

    binary, _ = _fake_harness(tmp_path, "")
    if missing == "sandbox":

        def unavailable(environment: object) -> None:
            raise providers.SandboxUnavailable("sandbox unavailable")

        monkeypatch.setattr(
            providers, "_require_operational_sandbox", _REAL_REQUIRE_OPERATIONAL_SANDBOX
        )
        monkeypatch.setattr(providers, "require_sandbox_available", unavailable)
    else:
        real_which = providers.shutil.which
        monkeypatch.setattr(
            providers.shutil,
            "which",
            lambda name: None if name == missing else real_which(name),
        )

    with pytest.raises(ProviderUnavailable, match=message):
        build_agent_driver(executable=str(binary))


def test_building_a_live_driver_refuses_before_setup_when_litellm_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured key must not make a base-only CTX install look runnable."""

    binary, _ = _fake_harness(tmp_path, "")
    sandbox_checked = False

    def sandbox_probe(_environment: object) -> None:
        nonlocal sandbox_checked
        sandbox_checked = True

    monkeypatch.setitem(sys.modules, "litellm", None)
    monkeypatch.setattr(providers, "_require_harness_dependency", _REAL_REQUIRE_HARNESS_DEPENDENCY)
    monkeypatch.setattr(providers, "require_sandbox_available", sandbox_probe)

    with pytest.raises(ProviderUnavailable, match=r"claude-ctx\[harness\]"):
        build_agent_driver(executable=str(binary))

    assert sandbox_checked is False


def test_installed_bubblewrap_that_cannot_start_a_network_namespace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executable presence is not proof Ubuntu permits Bubblewrap's namespace."""

    binary, _ = _fake_harness(tmp_path, "")
    observed: dict[str, object] = {}
    run_options: dict[str, object] = {}

    def sandbox_probe(command: object, **kwargs: object) -> tuple[str, ...]:
        observed.update(kwargs)
        return ("/usr/bin/bwrap", "--probe")

    def denied(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        run_options.update(kwargs)
        return subprocess.CompletedProcess(
            args=("/usr/bin/bwrap", "--probe"),
            returncode=1,
            stdout="",
            stderr="bwrap: setting up uid map: Permission denied",
        )

    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        providers, "_require_operational_sandbox", _REAL_REQUIRE_OPERATIONAL_SANDBOX
    )
    monkeypatch.setattr(providers, "require_sandbox_available", lambda _environment: None)
    monkeypatch.setattr(providers, "sandboxed_command", sandbox_probe)
    monkeypatch.setattr(providers.subprocess, "run", denied)

    with pytest.raises(ProviderUnavailable, match="network-disabled namespace") as caught:
        build_agent_driver(executable=str(binary))

    assert "Permission denied" in str(caught.value)
    assert "bwrap-userns-restrict" in str(caught.value)
    assert "keep the global" in str(caught.value)
    assert "apparmor_restrict_unprivileged_userns=0" not in str(caught.value)
    assert observed["network"] is False
    assert run_options["timeout"] == 15


def test_operational_probe_uses_the_repository_linux_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness probe must exercise the empty-root, no-network shape."""

    observed: dict[str, object] = {}

    def success(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    real_which = providers.shutil.which
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        providers.shutil,
        "which",
        lambda name, **kwargs: "/usr/bin/bwrap" if name == "bwrap" else real_which(name, **kwargs),
    )
    monkeypatch.setattr(providers, "sandboxed_command", _REAL_SANDBOXED_COMMAND)
    monkeypatch.setattr(providers.subprocess, "run", success)

    _REAL_REQUIRE_OPERATIONAL_SANDBOX({"PATH": os.environ.get("PATH", os.defpath)})

    command = observed["command"]
    assert isinstance(command, tuple)
    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-user" in command
    assert "--unshare-net" in command
    assert tuple(command[command.index("--tmpfs") : command.index("--tmpfs") + 2]) == (
        "--tmpfs",
        "/",
    )
    assert command[-2:] == ("--", "/bin/true")
    assert observed["timeout"] == 15


def test_operational_probe_timeout_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def timeout(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=("bwrap", "--probe"), timeout=15)

    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    monkeypatch.setattr(providers, "sandboxed_command", lambda *_args, **_kwargs: ("bwrap",))
    monkeypatch.setattr(providers.subprocess, "run", timeout)

    with pytest.raises(providers.SandboxUnavailable, match="not operational"):
        _REAL_REQUIRE_OPERATIONAL_SANDBOX({"PATH": os.environ.get("PATH", os.defpath)})

    assert observed["timeout"] == 15


def test_provider_diagnostics_reports_an_inoperable_installed_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, _ = _fake_harness(tmp_path, "")
    real_which = providers.shutil.which

    def which(name: str) -> str | None:
        if name == "ctx":
            return str(binary)
        return real_which(name)

    def unavailable(_environment: object) -> None:
        raise providers.SandboxUnavailable(
            "Bubblewrap is installed but the network-disabled namespace is not operational"
        )

    monkeypatch.setattr(providers.shutil, "which", which)
    monkeypatch.setattr(providers, "_require_operational_sandbox", unavailable)

    ok, detail = providers.provider_diagnostics()

    assert ok is False
    assert "network-disabled namespace is not operational" in detail


def test_provider_uses_the_shared_workspace_boundary_with_only_required_read_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, _ = _fake_harness(tmp_path, _real_ctx_run_json_stdout())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observed: dict[str, object] = {}

    def capture(command: object, **kwargs: object) -> tuple[str, ...]:
        observed.update(kwargs)
        return tuple(command)  # type: ignore[arg-type]

    monkeypatch.setattr(providers, "sandboxed_command", capture)

    build_agent_driver(executable=str(binary))(_invocation(workspace))

    assert observed["cwd"] == workspace
    assert observed["writable_root"] == workspace
    assert observed["network"] is True
    read_roots = observed["read_roots"]
    assert isinstance(read_roots, tuple)
    assert Path.home() not in read_roots
    read_paths = observed["read_paths"]
    assert isinstance(read_paths, tuple)
    assert binary in read_paths


def test_provider_runtime_access_tracks_multihop_shims_without_broadening_them(
    tmp_path: Path,
) -> None:
    cellar = tmp_path / "cellar" / "fake" / "1"
    (cellar / "bin").mkdir(parents=True)
    executable = cellar / "bin" / "fake-ctx"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    opt = tmp_path / "opt" / "fake"
    opt.parent.mkdir()
    opt.symlink_to(cellar, target_is_directory=True)
    shim = tmp_path / "shim" / "bin" / "fake-ctx"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(opt / "bin" / "fake-ctx")

    roots, paths = providers._runtime_read_access(str(shim))

    assert shim in paths
    assert opt / "bin" / "fake-ctx" in paths
    assert executable in paths
    assert shim.parent.parent not in roots
    assert cellar in roots


def test_a_trial_subprocess_inherits_only_required_runtime_and_provider_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient host credentials must not become agent or MCP credentials."""

    allowed_credentials = {
        "OPENAI_API_KEY": "openai-test",
        "ANTHROPIC_API_KEY": "anthropic-test",
        "CTX_FIT_API_KEY": "ctx-fit-test",
    }
    for name, value in allowed_credentials.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross")
    monkeypatch.setenv("GH_TOKEN", "must-not-cross")
    monkeypatch.setenv("UNRELATED_API_KEY", "must-not-cross")
    monkeypatch.setenv("PYTHONPATH", "/ambient/source")
    monkeypatch.setenv("VIRTUAL_ENV", "/ambient/venv")

    binary, _, env_path = _fake_harness_with_environment(tmp_path, _real_ctx_run_json_stdout())
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    build_agent_driver(executable=str(binary))(_invocation(workspace, model="gpt-4o-mini"))

    inherited = json.loads(env_path.read_text(encoding="utf-8"))
    assert inherited.get("OPENAI_API_KEY") == allowed_credentials["OPENAI_API_KEY"]
    assert "ANTHROPIC_API_KEY" not in inherited
    assert "CTX_FIT_API_KEY" not in inherited
    assert inherited["PATH"] == os.environ["PATH"]
    assert Path(inherited["HOME"]).is_relative_to(workspace)
    assert Path(inherited["TMPDIR"]).is_relative_to(workspace)
    assert (
        not {
            "AWS_SECRET_ACCESS_KEY",
            "GH_TOKEN",
            "UNRELATED_API_KEY",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        }
        & inherited.keys()
    )


def test_building_a_driver_refuses_when_the_command_would_be_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Argv/CLI drift must break at build time, not become evidence at scale.

    The stand-in here is the exact shape of the original defect: the prompt
    appended as a positional argument.
    """

    def positional_prompt(binary: str, invocation: AgentInvocation, **_: object) -> list[str]:
        return [binary, "run", "--json", providers._prompt(invocation)]

    monkeypatch.setattr(providers, "_command", positional_prompt)
    binary, _ = _fake_harness(tmp_path, "")

    with pytest.raises(ProviderUnavailable) as caught:
        build_agent_driver(executable=str(binary))

    assert "rejected by `ctx run`" in str(caught.value)


def test_a_healthy_command_line_survives_the_build_time_check(tmp_path: Path) -> None:
    """The guard must not fire on the command the module actually builds."""

    binary, _ = _fake_harness(tmp_path, "")

    assert callable(build_agent_driver(executable=str(binary)))


# ── FITBUG-007: the harness's pretty-printed JSON must be read ─────────────


def test_cost_and_tokens_are_read_from_real_harness_output(tmp_path: Path) -> None:
    """`ctx run --json` pretty-prints, so a line-oriented scan sees only "{"."""

    stdout = _real_ctx_run_json_stdout(input_tokens=18_432, output_tokens=2_105, cost_usd=0.0417)
    # Guard the guard: if the harness ever emits single-line JSON, this test
    # stops proving anything and should be revisited rather than pass hollowly.
    assert stdout.count("\n") > 1, "the harness no longer pretty-prints; this fixture is stale"

    binary, _ = _fake_harness(tmp_path, stdout)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcome = build_agent_driver(executable=str(binary))(_invocation(workspace))

    assert outcome.completed is True
    assert outcome.cost_usd == pytest.approx(0.0417)
    assert outcome.input_tokens == 18_432
    assert outcome.output_tokens == 2_105
    assert outcome.detail == "done"
    assert outcome.stop_reason == "done"
    assert outcome.logs == ""


def test_an_unmeasured_cost_stays_unknown(tmp_path: Path) -> None:
    """Reading the payload must not turn a null cost into a flattering zero."""

    stdout = _real_ctx_run_json_stdout(cost_usd=None)
    binary, _ = _fake_harness(tmp_path, stdout)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcome = build_agent_driver(executable=str(binary))(_invocation(workspace))

    assert outcome.cost_usd is None
    assert outcome.input_tokens == 18_432


def test_the_result_is_found_after_unrelated_stdout_chatter(tmp_path: Path) -> None:
    """Anything else on stdout must not cost us the measurement."""

    stdout = 'starting up\n{"session_id": "earlier"}\n' + _real_ctx_run_json_stdout()
    binary, _ = _fake_harness(tmp_path, stdout)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcome = build_agent_driver(executable=str(binary))(_invocation(workspace))

    assert outcome.cost_usd == pytest.approx(0.0417)
    assert outcome.output_tokens == 2_105


def test_unreadable_output_reports_unknown_rather_than_zero(tmp_path: Path) -> None:
    binary, _ = _fake_harness(tmp_path, "no json here at all\n", exit_code=1)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    outcome = build_agent_driver(executable=str(binary))(_invocation(workspace))

    assert outcome.completed is False
    assert outcome.cost_usd is None
    assert outcome.input_tokens is None
    # A silent failure is what let the original defect masquerade as a verdict.
    assert "harness exited 1" in outcome.detail


# ── FITBUG-002: two candidates must not be the same run ────────────────────


def _task_sent(tmp_path: Path, invocation: AgentInvocation) -> str:
    """The ``--task`` string the driver really hands the harness."""

    binary, argv_path = _fake_harness(tmp_path, _real_ctx_run_json_stdout())
    build_agent_driver(executable=str(binary))(invocation)
    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    return argv[argv.index("--task") + 1]


def _python_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "demo").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\n\n"
        "[tool.pytest.ini_options]\ntestpaths=['tests']\n",
        encoding="utf-8",
    )
    (repo / "src" / "demo" / "calc.py").write_text("def add(a, b):\n    return a + b\n", "utf-8")
    (repo / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n", "utf-8")
    return repo


def test_the_agent_receives_the_candidate_bound_body_not_a_reresolved_catalog(
    tmp_path: Path,
) -> None:
    """The configuration hash must name the bytes the trial actually runs."""

    material = CapabilityMaterial.from_content(
        capability_id="skill:ctx-python-testing",
        delivery_mode="task-user-context",
        source_identity="test-catalog#custom-revision",
        catalog_entry_digest=hashlib.sha256(b"custom entry").hexdigest(),
        content="A candidate-bound revision that is not in the package catalog.",
    )
    invocation = _invocation(tmp_path, capability_ids=())
    invocation = AgentInvocation(
        workspace=invocation.workspace,
        task_title=invocation.task_title,
        files_to_change=invocation.files_to_change,
        verify_command=invocation.verify_command,
        capability_ids=(material.capability_id,),
        model=invocation.model,
        capability_materials=(material,),
    )

    task = _task_sent(tmp_path, invocation)

    assert material.content in task


def test_the_agent_receives_exact_bound_repository_instructions_and_controlled_mode(
    tmp_path: Path,
) -> None:
    instruction = "# Exact evaluated instructions\n\nDo the narrow thing.  \n"
    invocation = _invocation(tmp_path, instruction_body=instruction)
    binary, argv_path = _fake_harness(tmp_path, _real_ctx_run_json_stdout())

    build_agent_driver(executable=str(binary))(invocation)

    argv = json.loads(argv_path.read_text(encoding="utf-8"))
    task = argv[argv.index("--task") + 1]
    assert "--fit-controlled-trial" in argv
    assert instruction in task
    assert invocation.instruction_materials[0].content_sha256 in task


def test_candidates_from_the_real_generator_run_different_commands(tmp_path: Path) -> None:
    """The experiment's entire claim rests on this.

    Driven from the shipped catalog through the real candidate generator rather
    than from hand-built configurations: the defect was precisely that those two
    ends of the pipeline never met, so a fixture in the middle would hide it.
    """

    from ctx.engine.planner import BoundedCapabilityPlanner
    from ctx.fit.candidates import generate_candidates
    from ctx.fit.profile import build_fit_profile
    from ctx.fit.release_catalog import open_release_candidate_source

    repo = _python_repo(tmp_path)
    source = open_release_candidate_source()
    assert source is not None, "the shipped capability catalog could not be opened"

    generated = generate_candidates(
        build_fit_profile(repo), BoundedCapabilityPlanner(source=source)
    )
    assert len(generated.candidates) > 1, "nothing is being compared; the fixture is wrong"

    commands = {
        candidate.candidate_id: tuple(
            providers._command(
                "ctx",
                AgentInvocation(
                    workspace=repo,
                    task_title="Implement the missing behavior",
                    files_to_change=("src/demo/calc.py",),
                    verify_command=("pytest", "-q"),
                    capability_ids=candidate.capability_ids,
                    model=candidate.model,
                    capability_materials=candidate.capability_materials,
                    instructions=candidate.instructions,
                    instruction_materials=candidate.instruction_materials,
                ),
                max_iterations=25,
                per_trial_budget_usd=2.0,
            )
        )
        for candidate in generated.candidates
    }

    assert len(set(commands.values())) == len(commands), (
        "candidates the report presents as compared execute an identical command, "
        f"so every difference between them is noise: {sorted(commands)}"
    )


def test_a_capability_the_driver_cannot_apply_is_refused_not_ignored(tmp_path: Path) -> None:
    """Silently dropping one is how a vacuous comparison gets reported as real.

    The refusal is an exception so that :mod:`ctx.fit.live_runner` records an
    ``infrastructure-failure``: the configuration was never tested, and must not
    be scored as having failed.
    """

    binary, _ = _fake_harness(tmp_path, _real_ctx_run_json_stdout())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    drive = build_agent_driver(executable=str(binary))

    with pytest.raises(CapabilityNotApplicable) as caught:
        drive(_invocation(workspace, capability_ids=("mcp-server:ctx-core",)))

    assert "mcp-server:ctx-core" in str(caught.value)
    # Callers that already handle an unusable provider must keep working.
    assert isinstance(caught.value, ProviderUnavailable)


def test_a_capability_with_no_bound_material_is_refused(tmp_path: Path) -> None:
    """An id with no content identity must not silently become the baseline."""

    binary, _ = _fake_harness(tmp_path, _real_ctx_run_json_stdout())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    drive = build_agent_driver(executable=str(binary))

    with pytest.raises(CapabilityNotApplicable):
        drive(_invocation(workspace, capability_ids=("skill:nothing-ships-this",)))


def test_the_specification_rule_survives_the_capability_bodies(tmp_path: Path) -> None:
    """Untrusted reference material must never be the closing instruction."""

    task = _task_sent(tmp_path, _invocation(tmp_path, capability_ids=("skill:ctx-python-testing",)))

    assert task.rstrip().endswith("Do not modify the tests. They are the specification.")


def test_a_baseline_still_sends_only_the_task(tmp_path: Path) -> None:
    """The control has no capabilities, so it must carry no capability text."""

    task = _task_sent(tmp_path, _invocation(tmp_path, capability_ids=()))

    assert "capability bodies below" not in task
    assert task.startswith("Reject empty capability ids")


# ── The harness contract these tests lean on ───────────────────────────────


def test_ctx_run_still_requires_task_and_takes_no_positionals() -> None:
    """If this ever changes, the tests above are asserting against a ghost."""

    result = subprocess.run(
        [sys.executable, "-m", "ctx", "run", "--json", "a bare prompt"],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 2
    assert "--task" in result.stderr
