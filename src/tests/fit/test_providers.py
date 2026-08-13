"""The seam between CTX Fit and the paid harness.

Both properties tested here failed silently in production rather than loudly:
a rejected command line and an undecodable result both turn into "this
candidate did not work on your repository". Nothing downstream can tell the
difference, so the checks have to live at the seam itself.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ctx.fit import providers
from ctx.fit.live_runner import AgentInvocation
from ctx.fit.providers import (
    CapabilityNotApplicable,
    ProviderUnavailable,
    build_agent_driver,
)


def _invocation(
    workspace: Path,
    *,
    model: str | None = None,
    capability_ids: tuple[str, ...] = ("skill:ctx-python-testing",),
) -> AgentInvocation:
    return AgentInvocation(
        workspace=workspace,
        task_title="Reject empty capability ids",
        files_to_change=("src/ctx/fit/candidates.py",),
        verify_command=("pytest", "-q", "src/tests/fit"),
        capability_ids=capability_ids,
        model=model,
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


def test_the_agent_receives_the_shipped_body_of_each_capability(tmp_path: Path) -> None:
    """A configured capability has to arrive as content, not as a name.

    Asserted against the catalog's own bytes rather than a phrase from the
    prompt template, because prose about a capability is not the capability.
    """

    body = providers._shipped_skill_bodies().get("skill:ctx-python-testing")
    assert body, "the shipped catalog no longer carries this skill; this test is stale"

    task = _task_sent(tmp_path, _invocation(tmp_path, capability_ids=("skill:ctx-python-testing",)))

    assert body in task


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


def test_a_capability_with_no_shipped_material_is_refused(tmp_path: Path) -> None:
    """An id with nothing behind it must not silently become the baseline."""

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
