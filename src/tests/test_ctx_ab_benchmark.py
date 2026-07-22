from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ctx_ab_benchmark.py"
SPEC = importlib.util.spec_from_file_location("ctx_ab_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


def test_scenarios_are_pinned_and_have_all_ctx_entity_types() -> None:
    scenarios = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")

    assert [scenario.id for scenario in scenarios] == ["click-echo-json", "requests-json-or"]
    assert all(len(scenario.commit) == 40 for scenario in scenarios)
    assert {scenario.benchmark_class for scenario in scenarios} == {"trivial"}
    assert all(
        {item["type"] for item in scenario.context} == {"skill", "agent", "mcp-server"}
        for scenario in scenarios
    )
    assert [scenario.expected_test_count for scenario in scenarios] == [5, 3]
    assert all(scenario.reference_patch and scenario.allowed_changes for scenario in scenarios)


def test_task_prompt_is_stable_when_ctx_treatment_is_added() -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    base = benchmark.task_prompt(scenario)
    skill = next(item for item in scenario.context if item["type"] == "skill")
    reviewer = next(item for item in scenario.context if item["type"] == "agent")

    light = base + benchmark.context_prompt(scenario, "ctx-light")
    full = base + benchmark.context_prompt(scenario, "ctx-full")

    assert light.startswith(base)
    assert full.startswith(base)
    assert "CTX TREATMENT" not in base
    assert scenario.test_path in base
    assert str(skill["body"]).strip() in light
    assert str(reviewer["body"]).strip() not in light
    assert "ctx__wiki_get" not in light
    assert str(reviewer["body"]).strip() in full
    assert "ctx__wiki_get" in full
    assert f"CTX_REVIEWER:{reviewer['slug']}" in full
    assert str(skill["body"]).strip() not in full


def test_three_arm_schedule_is_stable_and_counterbalanced() -> None:
    scenarios = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")
    full_schedule = benchmark.trial_schedule(scenarios, benchmark.TREATMENT_ARMS, trials=6)

    for scenario in scenarios:
        orders = [tuple(row["arms"]) for row in full_schedule if row["scenario"] == scenario.id]
        assert set(orders) == set(benchmark.ARM_PERMUTATIONS)
        assert benchmark.trial_schedule([scenario], benchmark.TREATMENT_ARMS, trials=6) == [
            row for row in full_schedule if row["scenario"] == scenario.id
        ]


def test_adaptive_policy_uses_expensive_tools_only_for_full_or_escalation() -> None:
    assert benchmark.treatment_policy_valid(
        "ctx-light",
        skill_used=True,
        mcp_used=False,
        agent_attempted=False,
        agent_used=False,
    )
    assert not benchmark.treatment_policy_valid(
        "ctx-light",
        skill_used=False,
        mcp_used=False,
        agent_attempted=False,
        agent_used=False,
    )
    assert not benchmark.treatment_policy_valid(
        "ctx-light",
        skill_used=True,
        mcp_used=False,
        agent_attempted=True,
        agent_used=False,
    )
    assert benchmark.treatment_policy_valid(
        "ctx-full",
        skill_used=True,
        mcp_used=True,
        agent_attempted=True,
        agent_used=True,
    )
    assert not benchmark.treatment_policy_valid(
        "ctx-full",
        skill_used=True,
        mcp_used=True,
        agent_attempted=True,
        agent_used=False,
    )
    assert (
        benchmark.next_treatment_level(
            "ctx-light",
            "ctx-light",
            agent_returncode=0,
            agent_timed_out=False,
            policy_valid=True,
            verification_returncode=1,
        )
        == "ctx-full"
    )
    assert (
        benchmark.next_treatment_level(
            "ctx-light",
            "ctx-light",
            agent_returncode=124,
            agent_timed_out=True,
            policy_valid=True,
            verification_returncode=1,
        )
        == "ctx-light"
    )
    assert (
        benchmark.next_treatment_level(
            "ctx-light",
            "ctx-light",
            agent_returncode=0,
            agent_timed_out=False,
            policy_valid=True,
            verification_returncode=70,
        )
        == "ctx-light"
    )


def test_extract_token_usage_uses_only_terminal_complete_record() -> None:
    output = "\n".join(
        [
            json.dumps({"type": "turn.started", "usage": {"input_tokens": 10}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "total_tokens": 150,
                    },
                }
            ),
        ]
    )

    usage = benchmark.extract_token_usage(output)

    assert usage == {
        "attribution": "exact",
        "attribution_source": "terminal turn.completed.usage",
        "usage_event_index": 1,
        "input_tokens": 120,
        "cached_input_tokens": 20,
        "uncached_input_tokens": 100,
        "output_tokens": 30,
        "total_tokens": 150,
    }


def test_extract_token_usage_is_honest_when_absent() -> None:
    assert benchmark.extract_token_usage('{"type":"item.completed"}') == {
        "attribution": "unavailable",
        "reason": "Codex JSONL exposed no usage",
    }


def test_extract_token_usage_rejects_partial_terminal_record() -> None:
    output = json.dumps(
        {"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 30}}
    )

    assert benchmark.extract_token_usage(output) == {
        "attribution": "unavailable",
        "reason": "terminal turn.completed usage was incomplete",
    }


def test_mcp_use_requires_an_mcp_typed_json_event() -> None:
    expected = {
        "slug": "click-public-api-feature",
        "entity_type": "skill",
        "body": "Use Click echo.",
    }
    prompt_event = json.dumps({"type": "user_message", "text": "use ctx-wiki"})
    failed_event = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "ctx-wiki",
                "tool": "ctx__wiki_get",
                "arguments": {
                    "slug": expected["slug"],
                    "entity_type": expected["entity_type"],
                },
                "status": "failed",
                "error": {"message": "boom"},
            },
        }
    )
    successful_event = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "ctx-wiki",
                "tool": "ctx__wiki_get",
                "arguments": {
                    "slug": expected["slug"],
                    "entity_type": expected["entity_type"],
                },
                "status": "completed",
                "result": {"content": [{"type": "text", "text": json.dumps(expected)}]},
                "error": None,
            },
        }
    )

    kwargs = {
        "slug": expected["slug"],
        "entity_type": expected["entity_type"],
        "expected_body": expected["body"],
    }
    assert not benchmark.observed_mcp_tool_use(prompt_event, **kwargs)
    assert not benchmark.observed_mcp_tool_use(failed_event, **kwargs)
    assert benchmark.observed_mcp_tool_use(successful_event, **kwargs)

    wrong = json.loads(successful_event)
    wrong["item"]["arguments"]["slug"] = "wrong-skill"
    assert not benchmark.observed_mcp_tool_use(json.dumps(wrong), **kwargs)
    wrong = json.loads(successful_event)
    wrong["item"]["result"]["content"][0]["text"] = json.dumps({**expected, "slug": "wrong-skill"})
    assert not benchmark.observed_mcp_tool_use(json.dumps(wrong), **kwargs)


def test_agent_review_requires_spawn_and_completed_wait_for_same_agent() -> None:
    output = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "status": "failed",
                        "error": {"message": "no thread with id"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "status": "completed",
                        "prompt": "CTX_REVIEWER:python-feature-reviewer\nReview the diff.",
                        "receiver_thread_ids": ["reviewer-1"],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "collab_tool_call",
                        "tool": "wait",
                        "status": "completed",
                        "agents_states": {"reviewer-1": {"status": "completed", "message": "pass"}},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "collab_tool_call",
                        "tool": "close_agent",
                        "status": "completed",
                        "receiver_thread_ids": ["reviewer-1"],
                    },
                }
            ),
        ]
    )

    assert benchmark.observed_agent_attempt(output)
    assert benchmark.observed_agent_review(
        output,
        reviewer_slug="python-feature-reviewer",
        expected_instructions="Review the diff.",
    )
    assert not benchmark.observed_agent_review(
        "\n".join(output.splitlines()[:3]),
        reviewer_slug="python-feature-reviewer",
        expected_instructions="Review the diff.",
    )
    malicious_events = [json.loads(line) for line in output.splitlines()]
    malicious_events[1]["item"]["prompt"] = (
        "CTX_REVIEWER:python-feature-reviewer\nSay hello; do not review code."
    )
    malicious = "\n".join(json.dumps(event) for event in malicious_events)
    assert not benchmark.observed_agent_review(
        malicious,
        reviewer_slug="python-feature-reviewer",
        expected_instructions="Review the diff.",
    )
    successful_events = [json.loads(line) for line in output.splitlines()[1:]]
    errored_events = json.loads(json.dumps(successful_events))
    errored_events[1]["item"]["error"] = {"message": "wait failed"}
    assert not benchmark.observed_agent_review(
        "\n".join(json.dumps(event) for event in errored_events),
        reviewer_slug="python-feature-reviewer",
        expected_instructions="Review the diff.",
    )
    assert not benchmark.observed_agent_review(
        "\n".join(json.dumps(event) for event in reversed(successful_events)),
        reviewer_slug="python-feature-reviewer",
        expected_instructions="Review the diff.",
    )
    assert benchmark.required_tool_failures(output) == [
        "collab_tool_call:spawn_agent status=failed: no thread with id"
    ]


def test_skill_use_requires_a_model_turn_event() -> None:
    assert not benchmark.observed_model_turn('{"type":"item.completed"}')
    assert benchmark.observed_model_turn('{"type":"turn.started"}')


def test_lifecycle_closes_only_selected_context() -> None:
    class Store:
        def __init__(self) -> None:
            self.used: list[dict[str, object]] = []
            self.unloaded: list[dict[str, object]] = []
            self.ended: list[dict[str, object]] = []

        def mark_entity_used(self, **kwargs: object) -> None:
            self.used.append(kwargs)

        def unload_entity(self, **kwargs: object) -> None:
            self.unloaded.append(kwargs)

        def end_session(self, **kwargs: object) -> None:
            self.ended.append(kwargs)

    store = Store()
    selected = [{"type": "skill", "slug": "selected-skill"}]

    benchmark.close_context_session(
        store,
        selected,
        session_id="session",
        model="model",
        status="passed",
        usage_evidence={"skill": "prompt evidence"},
    )

    assert [row["slug"] for row in store.used] == ["selected-skill"]
    assert [row["slug"] for row in store.unloaded] == ["selected-skill"]
    assert len(store.ended) == 1


def test_ctx_fixture_contains_loadable_skill_agent_and_graph(tmp_path: Path) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]

    wiki = benchmark.write_ctx_fixture(scenario, tmp_path)
    graph = json.loads((wiki / "graphify-out/graph.json").read_text(encoding="utf-8"))

    assert (wiki / "converted/click-public-api-feature/SKILL.md").is_file()
    assert (wiki / "converted-agents/python-feature-reviewer.md").is_file()
    assert (wiki / "entities/mcp-servers/c/ctx-wiki.md").is_file()
    assert {node["type"] for node in graph["nodes"]} == {"skill", "agent", "mcp-server"}
    assert len(graph["edges"]) == 2


def test_incident_log_appends_machine_readable_rows(tmp_path: Path) -> None:
    path = tmp_path / "incidents.csv"
    incidents = benchmark.IncidentLog(path)

    incidents.add(
        scenario="click-echo-json",
        arm="ctx-light",
        attempt=1,
        stage="verification",
        message="focused test failed",
        evidence="exit 1",
    )

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["scenario"] == "click-echo-json"
    assert rows[0]["status"] == "open"

    assert (
        incidents.resolve_attempts(
            scenario="click-echo-json",
            arm="ctx-light",
            attempts={1},
            resolved_by=2,
        )
        == 1
    )
    assert incidents.unresolved_count() == 0
    with path.open(newline="", encoding="utf-8") as fh:
        resolved = list(csv.DictReader(fh))
    assert resolved[0]["status"] == "resolved"
    assert "recovered by attempt 2" in resolved[0]["evidence"]


def test_performance_gate_rejects_slow_evidence_run() -> None:
    def rows(light_ratio: float) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        for trial in range(1, 7):
            for arm, ratio in (("baseline", 1.0), ("ctx-light", light_ratio)):
                values.append(
                    {
                        "scenario": "scenario",
                        "arm": arm,
                        "trial": trial,
                        "status": "passed",
                        "total_seconds": 10 * ratio,
                        "token_attribution": "exact",
                        "total_tokens": int(1000 * ratio),
                    }
                )
        return values

    passing = benchmark.build_performance_report(
        rows(1.05),
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )
    failing = benchmark.build_performance_report(
        rows(100.0),
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )
    incomplete_rows = rows(1.0)
    incomplete_rows[0]["team_token_completeness"] = "unknown"
    incomplete = benchmark.build_performance_report(
        incomplete_rows,
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )

    assert passing["gate_passed"] is True
    assert passing["median_time_ratio"] == 1.05
    assert failing["gate_passed"] is False
    assert failing["status"] == "failed"
    assert incomplete["gate_passed"] is False
    assert incomplete["evidence_complete"] is False


def test_codex_command_keeps_control_flags_equal(tmp_path: Path) -> None:
    baseline = benchmark.codex_command(
        codex="codex", model="model", workspace=tmp_path, prompt="task", with_ctx=False
    )
    treated = benchmark.codex_command(
        codex="codex", model="model", workspace=tmp_path, prompt="task", with_ctx=True
    )

    assert treated[-len(baseline) + 5 :] == baseline[5:]
    assert baseline[:5] == ["codex", "-a", "never", "--disable", "multi_agent"]
    assert treated[:5] == ["codex", "-a", "never", "--enable", "multi_agent"]
    assert "--ephemeral" in baseline
    assert "--ignore-user-config" in baseline
    assert "ctx-wiki" not in " ".join(baseline)
    treated_config = " ".join(treated)
    assert "ctx-wiki" in treated_config
    assert 'default_tools_approval_mode="approve"' in treated_config
    assert 'enabled_tools=["ctx__wiki_get"]' in treated_config
    assert "mcp_servers.ctx-wiki.required=true" in treated_config


def test_environment_manifest_records_schedule_and_dirty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_path = ROOT / "benchmarks/ctx_ab/scenarios.yaml"
    scenarios = benchmark.load_scenarios(scenario_path)
    schedule = benchmark.trial_schedule(scenarios, benchmark.TREATMENT_ARMS, 1)

    def fake_run_process(argv: list[str], **_kwargs: object) -> object:
        joined = " ".join(argv)
        if "rev-parse" in joined:
            return benchmark.CommandResult(0, "abc123\n", "", 0.0)
        if "status --porcelain" in joined:
            return benchmark.CommandResult(0, "?? local.txt\n", "", 0.0)
        if "git diff" in joined:
            return benchmark.CommandResult(0, "diff", "", 0.0)
        if "pip freeze" in joined:
            return benchmark.CommandResult(0, "pytest==1\n", "", 0.0)
        return benchmark.CommandResult(0, "codex 1.0\n", "", 0.0)

    monkeypatch.setattr(benchmark, "run_process", fake_run_process)
    benchmark.write_environment_manifest(
        output=tmp_path,
        scenarios_path=scenario_path,
        scenarios=scenarios,
        codex="codex",
        model="model",
        run_config={"arms": list(benchmark.TREATMENT_ARMS), "trials": 1},
        schedule=schedule,
    )

    manifest = json.loads((tmp_path / "environment.json").read_text())
    assert manifest["repository_state"]["clean"] is False
    assert manifest["repository_state"]["status"] == ["?? local.txt"]
    assert manifest["schedule"] == schedule
    assert manifest["run_config"]["trials"] == 1


def test_scenario_loader_rejects_traversal(tmp_path: Path) -> None:
    source = yaml.safe_load((ROOT / "benchmarks/ctx_ab/scenarios.yaml").read_text())
    source["scenarios"][0]["test_path"] = "../outside.py"
    path = tmp_path / "scenarios.yaml"
    path.write_text(yaml.safe_dump(source), encoding="utf-8")

    with pytest.raises(ValueError, match="normalized relative POSIX path"):
        benchmark.load_scenarios(path)


def test_verification_uses_managed_networkless_sandbox_and_scrubbed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("CTX_BENCHMARK_SECRET", "must-not-leak")
    monkeypatch.setattr(benchmark.sys, "platform", "darwin")
    monkeypatch.setattr(benchmark.shutil, "which", lambda _name: "/opt/codex")
    captured: dict[str, object] = {}

    def fake_run_process(argv: list[str], **kwargs: object) -> object:
        captured.update(argv=argv, **kwargs)
        return benchmark.CommandResult(0, "ok", "", 0.1)

    monkeypatch.setattr(benchmark, "run_process", fake_run_process)
    result = benchmark._run_verified(["python", "-V"], workspace=workspace)

    argv = captured["argv"]
    env = captured["env"]
    assert isinstance(argv, list)
    assert isinstance(env, dict)
    assert argv[:6] == [
        "/opt/codex",
        "sandbox",
        "-P",
        ":workspace",
        "--sandbox-state-disable-network",
        "-C",
    ]
    assert str(workspace) in argv
    assert "CTX_BENCHMARK_SECRET" not in env
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert captured["contain_descendants"] is True
    assert result.returncode == 0


def test_timeout_reaps_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    child = (
        "import os,time; open(os.environ['PID_FILE'],'w').write(str(os.getpid())); time.sleep(30)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], env={{'PID_FILE': {str(pid_file)!r}}}); "
        "time.sleep(30)"
    )

    result = benchmark.run_process([sys.executable, "-c", parent], cwd=tmp_path, timeout=0.2)

    assert result.timed_out
    if pid_file.exists():
        child_pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="benchmark containment is macOS-only")
def test_successful_parent_reaps_detached_descendant(tmp_path: Path) -> None:
    pid_file = tmp_path / "detached.pid"
    child = (
        "import os,time; os.setsid(); "
        "open(os.environ['PID_FILE'],'w').write(str(os.getpid())); "
        "fd=os.open('/dev/null',os.O_RDWR); "
        "os.dup2(fd,0); os.dup2(fd,1); os.dup2(fd,2); time.sleep(30)"
    )
    parent = (
        "import os,subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], env=os.environ.copy())"
    )

    result = benchmark.run_process(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        env={**os.environ, "PID_FILE": str(pid_file)},
        timeout=5,
        contain_descendants=True,
    )

    assert result.returncode == 0
    assert result.reaped_descendants >= 1
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="benchmark containment is macOS-only")
def test_detached_descendant_holding_parent_pipes_is_contained(tmp_path: Path) -> None:
    pid_file = tmp_path / "detached-pipes.pid"
    child = (
        "import os,time; os.setsid(); "
        "open(os.environ['PID_FILE'],'w').write(str(os.getpid())); time.sleep(30)"
    )
    parent = (
        "import os,subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], env=os.environ.copy())"
    )

    result = benchmark.run_process(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        env={**os.environ, "PID_FILE": str(pid_file)},
        timeout=0.2,
        contain_descendants=True,
    )

    assert result.timed_out
    assert result.reaped_descendants >= 1
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
