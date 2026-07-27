from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ctx_ab_benchmark.py"
SPEC = importlib.util.spec_from_file_location("ctx_ab_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            check=False,
            capture_output=True,
            text=True,
        )
        return str(pid) in listed.stdout
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            state = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except (IndexError, OSError):
            state = ""
        if state == "Z":
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_ctx_env_preserves_windows_process_plumbing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("COMSPEC", "PATHEXT", "SYSTEMROOT", "WINDIR"):
        monkeypatch.setenv(name, f"sentinel-{name.lower()}")

    home = tmp_path / "home"
    env = benchmark._ctx_env(home, tmp_path / "lifecycle")

    assert {name: env[name] for name in ("COMSPEC", "PATHEXT", "SYSTEMROOT", "WINDIR")} == {
        name: f"sentinel-{name.lower()}" for name in ("COMSPEC", "PATHEXT", "SYSTEMROOT", "WINDIR")
    }
    assert env["USERPROFILE"] == str(home)
    assert {env[name] for name in ("TEMP", "TMP", "TMPDIR")} == {str(home / "tmp")}


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
    assert (tmp_path / ".codex/skills/click-public-api-feature/SKILL.md").is_file()
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
                        "production_efficiency_eligible": True,
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


def test_unverified_custom_endpoint_evidence_is_excluded_from_efficiency_aggregates(
    tmp_path: Path,
) -> None:
    classification = benchmark.classify_production_evidence(
        base_url="http://127.0.0.1:8000/v1",
        dry_run=False,
    )
    rows = [
        {
            "scenario": "scenario",
            "arm": arm,
            "trial": 1,
            "status": "passed",
            "total_seconds": 1.0,
            "token_attribution": "exact",
            "total_tokens": 10,
            **classification,
        }
        for arm in ("baseline", "ctx-light")
    ]

    report = benchmark.build_performance_report(
        rows,
        scenario_ids=["scenario"],
        trials=1,
        arms=("baseline", "ctx-light"),
    )
    benchmark.write_summary(tmp_path, rows)

    assert classification == {
        "endpoint_class": "custom_endpoint",
        "evidence_level": "functional_only",
        "production_efficiency_eligible": False,
    }
    assert report["status"] == "functional_only"
    assert report["production_efficiency_claim_allowed"] is False
    assert report["gate_passed"] is None
    assert report["excluded_result_count"] == 2
    assert report["median_time_ratio"] is None
    assert json.loads((tmp_path / "aggregate.json").read_text(encoding="utf-8")) == []
    assert len(json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))) == 2


def test_verified_custom_endpoint_evidence_is_efficiency_eligible() -> None:
    assert benchmark.classify_production_evidence(
        base_url="https://provider.example/v1",
        dry_run=False,
        provider_provenance={
            "provider_identity_verified": True,
            "provider_endpoint_verified": True,
            "provider_authentication_verified": True,
            "provider_response_success": True,
        },
    ) == {
        "endpoint_class": "custom_endpoint",
        "evidence_level": "live_provider",
        "production_efficiency_eligible": True,
    }


def test_codex_controlled_evidence_never_claims_shipped_provider_proof() -> None:
    assert benchmark.classify_codex_controlled_evidence(dry_run=False) == {
        "endpoint_class": "codex_controlled",
        "evidence_level": "controlled_context_delivery",
        "production_efficiency_eligible": False,
    }
    assert benchmark.classify_codex_controlled_evidence(dry_run=True) == {
        "endpoint_class": "codex_controlled",
        "evidence_level": "controlled_wiring_only",
        "production_efficiency_eligible": False,
    }


def test_codex_controlled_dry_run_result_is_explicitly_non_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]

    def fake_prepare(_scenario: object, _cache: Path, destination: Path) -> str:
        destination.mkdir(parents=True)
        return "test-hash"

    monkeypatch.setattr(benchmark, "prepare_workspace", fake_prepare)
    result = benchmark.run_trial(
        scenario,
        arm="baseline",
        treatment_level="baseline",
        attempt=1,
        trial=1,
        retry=0,
        cache=tmp_path / "cache",
        output=tmp_path / "output",
        codex="codex",
        model="gpt-test",
        timeout=10,
        dry_run=True,
        incidents=benchmark.IncidentLog(tmp_path / "incidents.csv"),
    )

    assert result["engine"] == "codex-controlled"
    assert result["endpoint_class"] == "codex_controlled"
    assert result["evidence_level"] == "controlled_wiring_only"
    assert result["production_efficiency_eligible"] is False


def test_codex_controlled_dry_run_is_accepted_as_complete() -> None:
    result = {
        "scenario": "scenario-a",
        "arm": "baseline",
        "trial": 1,
        "status": "wiring_only",
        "evidence_level": "controlled_wiring_only",
    }

    assert benchmark.dry_run_results_complete(
        [result],
        expected_keys={("scenario-a", "baseline", 1)},
        engine="codex-controlled",
    )
    assert not benchmark.dry_run_results_complete(
        [{**result, "evidence_level": "wiring_only"}],
        expected_keys={("scenario-a", "baseline", 1)},
        engine="codex-controlled",
    )


def test_no_base_url_and_success_do_not_infer_live_provider() -> None:
    assert benchmark.classify_production_evidence(
        base_url=None,
        dry_run=False,
        provider_provenance={
            "provider_identity": "openai",
            "provider_identity_verified": False,
            "provider_endpoint_verified": False,
            "provider_authentication_verified": False,
            "provider_response_success": True,
        },
    ) == {
        "endpoint_class": "provider_default_unverified",
        "evidence_level": "functional_unverified",
        "production_efficiency_eligible": False,
    }
    assert benchmark.classify_production_evidence(base_url=None, dry_run=False) == {
        "endpoint_class": "provider_default_unverified",
        "evidence_level": "functional_unverified",
        "production_efficiency_eligible": False,
    }


def test_aggregation_excludes_missing_and_error_eligibility(tmp_path: Path) -> None:
    rows = [
        {
            "scenario": "eligible",
            "arm": "baseline",
            "trial": 1,
            "status": "passed",
            "total_seconds": 1.0,
            "token_attribution": "exact",
            "total_tokens": 10,
            "production_efficiency_eligible": True,
        },
        {
            "scenario": "missing",
            "arm": "baseline",
            "trial": 1,
            "status": "failed",
            "total_seconds": 2.0,
            "token_attribution": "unavailable",
        },
        {
            "scenario": "error",
            "arm": "baseline",
            "trial": 1,
            "status": "harness_error",
            "evidence_level": "harness_error",
            "production_efficiency_eligible": False,
            "provider_response_success": False,
        },
    ]

    benchmark.write_summary(tmp_path, rows)
    aggregate = json.loads((tmp_path / "aggregate.json").read_text(encoding="utf-8"))
    report = benchmark.build_performance_report(
        rows,
        scenario_ids=["eligible"],
        trials=1,
        arms=("baseline",),
    )

    assert [row["scenario"] for row in aggregate] == ["eligible"]
    assert report["production_efficiency_claim_allowed"] is False
    assert report["excluded_result_count"] == 2


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


def test_production_ctx_command_uses_shipped_cli_with_equal_controls(tmp_path: Path) -> None:
    common = {
        "model": "openai/model",
        "prompt": "task",
        "session_id": "session",
        "sessions_dir": tmp_path,
        "api_key_env": "MODEL_API_KEY",
        "base_url": "http://localhost:11434",
        "max_iterations": 7,
        "max_tokens": 2048,
        "provider_timeout": 45.0,
    }
    baseline = benchmark.production_ctx_command(with_ctx=False, **common)
    treated = benchmark.production_ctx_command(with_ctx=True, **common)

    surface_index = treated.index("--ctx-tool-surface")
    assert baseline[:-1] == treated[:surface_index]
    assert baseline[-1:] == ["--no-ctx-tools"]
    assert treated[surface_index : surface_index + 2] == ["--ctx-tool-surface", "adaptive"]
    assert [
        treated[index + 1] for index, value in enumerate(treated) if value == "--allow-tool"
    ] == list(benchmark.PRODUCTION_CTX_TOOL_NAMES)
    mcp_index = treated.index("--mcp")
    assert treated[mcp_index + 1].startswith("ctx-benchmark-control:")
    assert "ctx.mcp_server.server" in treated[mcp_index + 1]
    assert baseline[:5] == [sys.executable, "-m", "ctx.cli.run", "run", "--model"]
    assert benchmark.build_parser().parse_args([]).engine == "codex-controlled"
    assert (
        benchmark.build_parser().parse_args(["--engine", "production-ctx-run"]).engine
        == "production-ctx-run"
    )


def test_production_usage_requires_exact_provider_counts() -> None:
    exact = benchmark.extract_production_usage(
        {
            "usage": {
                "tokens_reported": True,
                "input_tokens": 120,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "total_tokens": 150,
            }
        }
    )

    assert exact["attribution"] == "exact"
    assert exact["uncached_input_tokens"] == 100


def test_provider_request_tool_surface_rejects_missing_and_extra_schemas() -> None:
    expected = benchmark.production_ctx_tool_schemas()
    exact = benchmark.validate_provider_request_tool_surface(
        {"tools": expected},
        expected_tools=expected,
    )

    assert exact["provider_request_tool_names"] == sorted(benchmark.PRODUCTION_CTX_TOOL_NAMES)
    assert len(exact["provider_request_tool_schema_sha256"]) == 64
    assert (
        benchmark.validate_provider_request_tool_surface(
            {},
            expected_tools=[],
        )["provider_request_tool_names"]
        == []
    )

    with pytest.raises(ValueError, match="missing="):
        benchmark.validate_provider_request_tool_surface(
            {"tools": expected[:-1]},
            expected_tools=expected,
        )
    extra = [
        *expected,
        {
            "type": "function",
            "function": {
                "name": "ctx__unexpected",
                "description": "unexpected",
                "parameters": {"type": "object"},
            },
        },
    ]
    with pytest.raises(ValueError, match="extra="):
        benchmark.validate_provider_request_tool_surface(
            {"tools": extra},
            expected_tools=expected,
        )
    changed = json.loads(json.dumps(expected))
    changed[0]["function"]["description"] = "changed"
    with pytest.raises(ValueError, match="schema mismatch"):
        benchmark.validate_provider_request_tool_surface(
            {"tools": changed},
            expected_tools=expected,
        )
    with pytest.raises(ValueError, match="did not report exact"):
        benchmark.extract_production_usage(
            {
                "usage": {
                    "tokens_reported": False,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                }
            }
        )
    with pytest.raises(ValueError, match="inconsistent"):
        benchmark.extract_production_usage(
            {
                "usage": {
                    "tokens_reported": True,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 99,
                }
            }
        )


def test_production_payload_requires_requested_successful_session() -> None:
    payload = {
        "session_id": "requested-session",
        "stop_reason": "completed",
        "usage": {
            "tokens_reported": True,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    }

    validated = benchmark.validate_production_payload(
        payload,
        session_id="requested-session",
    )

    assert benchmark.extract_production_usage(validated)["total_tokens"] == 15
    with pytest.raises(ValueError, match="does not match"):
        benchmark.validate_production_payload(payload, session_id="different-session")
    for stop_reason in ("max_iterations", "token_budget", "provider_error", None):
        with pytest.raises(ValueError, match="not successful"):
            benchmark.validate_production_payload(
                {**payload, "stop_reason": stop_reason},
                session_id="requested-session",
            )


def test_provider_response_provenance_requires_a_successful_authenticated_response(
    tmp_path: Path,
) -> None:
    session_id = "provider-evidence"
    session_path = tmp_path / f"{session_id}.jsonl"
    events = [
        {
            "type": "session_start",
            "session_id": session_id,
            "provider": "openai",
            "model": "openai/gpt-test",
            "api_key_env": "MODEL_API_KEY",
            "base_url": "",
        },
        {
            "type": "model_response",
            "session_id": session_id,
            "provider": "litellm",
            "model": "openai/gpt-test",
            "response_model": "openai/gpt-test",
            "authentication_submitted": True,
            "request_endpoint_hash": None,
            "finish_reason": "stop",
        },
    ]
    session_path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    evidence = benchmark.extract_provider_response_provenance(
        sessions_dir=tmp_path,
        session_id=session_id,
        model="openai/gpt-test",
        base_url=None,
        api_key_env="MODEL_API_KEY",
        env={"MODEL_API_KEY": "secret-value"},
    )

    assert evidence["provider_identity"] == "openai"
    assert evidence["provider_adapter"] == "litellm"
    assert evidence["provider_response_success"] is True
    assert evidence["provider_auth_mode"] == "api_key_env"
    assert (
        evidence["provider_authentication_evidence"]
        == "credential_submitted_with_successful_response"
    )
    assert evidence["provider_identity_verified"] is True
    assert evidence["provider_endpoint_verified"] is True
    assert evidence["provider_authentication_verified"] is True
    assert evidence["provider_request_authentication_submitted"] is True
    assert "secret-value" not in json.dumps(evidence)
    assert len(evidence["provider_session_sha256"]) == 64


def test_provider_key_presence_without_success_is_not_authentication_evidence(
    tmp_path: Path,
) -> None:
    session_id = "provider-failure"
    events = [
        {
            "type": "session_start",
            "session_id": session_id,
            "provider": "openai",
            "model": "openai/gpt-test",
            "api_key_env": "MODEL_API_KEY",
            "base_url": "",
        },
        {
            "type": "model_response",
            "session_id": session_id,
            "provider": "litellm",
            "model": "openai/gpt-test",
            "response_model": "openai/gpt-test",
            "authentication_submitted": True,
            "request_endpoint_hash": None,
            "finish_reason": "error",
        },
    ]
    (tmp_path / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    evidence = benchmark.extract_provider_response_provenance(
        sessions_dir=tmp_path,
        session_id=session_id,
        model="openai/gpt-test",
        base_url=None,
        api_key_env="MODEL_API_KEY",
        env={"MODEL_API_KEY": "secret-value"},
    )

    assert evidence["provider_response_success"] is False
    assert evidence["provider_identity_verified"] is False
    assert evidence["provider_endpoint_verified"] is False
    assert evidence["provider_authentication_verified"] is False
    assert (
        evidence["provider_authentication_evidence"]
        == "credential_submitted_without_successful_response"
    )
    assert "secret-value" not in json.dumps(evidence)


def test_successful_local_no_key_provider_remains_functional_only(tmp_path: Path) -> None:
    session_id = "local-no-key"
    model = "openai/local-test"
    base_url = "http://127.0.0.1:11434/v1"
    events = [
        {
            "type": "session_start",
            "session_id": session_id,
            "provider": "openai",
            "model": model,
            "api_key_env": "",
            "base_url": base_url,
        },
        {
            "type": "model_response",
            "session_id": session_id,
            "provider": "litellm",
            "model": model,
            "response_model": model,
            "authentication_submitted": False,
            "request_endpoint_hash": (
                "sha256:" + hashlib.sha256(base_url.encode("utf-8")).hexdigest()
            ),
            "finish_reason": "stop",
        },
    ]
    (tmp_path / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    evidence = benchmark.extract_provider_response_provenance(
        sessions_dir=tmp_path,
        session_id=session_id,
        model=model,
        base_url=base_url,
        api_key_env=None,
        env={},
    )

    assert evidence["provider_identity_verified"] is True
    assert evidence["provider_endpoint_verified"] is True
    assert evidence["provider_authentication_verified"] is False
    assert benchmark.classify_production_evidence(
        base_url=base_url,
        dry_run=False,
        provider_provenance=evidence,
    ) == {
        "endpoint_class": "custom_endpoint",
        "evidence_level": "functional_only",
        "production_efficiency_eligible": False,
    }


def test_provider_reported_model_mismatch_does_not_verify_provider(
    tmp_path: Path,
) -> None:
    session_id = "unattested-provider"
    events = [
        {
            "type": "session_start",
            "session_id": session_id,
            "provider": "openai",
            "model": "openai/gpt-test",
            "api_key_env": "MODEL_API_KEY",
            "base_url": "",
        },
        {
            "type": "model_response",
            "session_id": session_id,
            "provider": "litellm",
            "model": "openai/gpt-test",
            "response_model": "provider-returned-different",
            "authentication_submitted": True,
            "request_endpoint_hash": None,
            "finish_reason": "stop",
        },
    ]
    (tmp_path / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    evidence = benchmark.extract_provider_response_provenance(
        sessions_dir=tmp_path,
        session_id=session_id,
        model="openai/gpt-test",
        base_url=None,
        api_key_env="MODEL_API_KEY",
        env={"MODEL_API_KEY": "secret-value"},
    )

    assert evidence["provider_response_success"] is True
    assert evidence["provider_identity_verified"] is False
    assert evidence["provider_authentication_verified"] is True
    assert evidence["provider_request_authentication_submitted"] is True
    assert (
        benchmark.classify_production_evidence(
            base_url=None,
            dry_run=False,
            provider_provenance=evidence,
        )["production_efficiency_eligible"]
        is False
    )


def test_environment_key_without_submitted_credential_does_not_verify_authentication(
    tmp_path: Path,
) -> None:
    session_id = "credential-not-submitted"
    events = [
        {
            "type": "session_start",
            "session_id": session_id,
            "provider": "openai",
            "model": "openai/gpt-test",
            "api_key_env": "MODEL_API_KEY",
            "base_url": "",
        },
        {
            "type": "model_response",
            "session_id": session_id,
            "provider": "litellm",
            "model": "openai/gpt-test",
            "response_model": "openai/gpt-test",
            "authentication_submitted": False,
            "request_endpoint_hash": None,
            "finish_reason": "stop",
        },
    ]
    (tmp_path / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    evidence = benchmark.extract_provider_response_provenance(
        sessions_dir=tmp_path,
        session_id=session_id,
        model="openai/gpt-test",
        base_url=None,
        api_key_env="MODEL_API_KEY",
        env={"MODEL_API_KEY": "secret-value"},
    )

    assert evidence["provider_identity_verified"] is True
    assert evidence["provider_authentication_verified"] is False
    assert evidence["provider_request_authentication_submitted"] is False
    assert (
        evidence["provider_authentication_evidence"]
        == "configured_api_key_env_present_but_not_submitted"
    )


def test_provider_response_provenance_rejects_foreign_session(tmp_path: Path) -> None:
    session_id = "requested"
    (tmp_path / f"{session_id}.jsonl").write_text(
        json.dumps(
            {
                "type": "session_start",
                "session_id": "foreign",
                "provider": "openai",
                "model": "openai/gpt-test",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="foreign session"):
        benchmark.extract_provider_response_provenance(
            sessions_dir=tmp_path,
            session_id=session_id,
            model="openai/gpt-test",
            base_url=None,
            api_key_env=None,
            env={},
        )


def test_production_lifecycle_rejects_request_only_evidence(tmp_path: Path) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    lifecycle = benchmark.make_lifecycle_store(tmp_path)
    session_id = "request-only"
    skill = next(item for item in scenario.context if item["type"] == "skill")
    common = {
        "session_id": session_id,
        "entity_type": "skill",
        "slug": skill["slug"],
    }
    lifecycle.load_entity(**common, selected=True, selection_source="host")
    lifecycle.unload_entity(**common)
    lifecycle.end_session(session_id=session_id, status="completed")

    with pytest.raises(ValueError, match="invalid transition"):
        benchmark.validate_production_lifecycle(
            scenario,
            lifecycle_root=tmp_path,
            session_id=session_id,
        )


def _write_lifecycle_actions(
    root: Path,
    scenario: Any,
    *,
    session_id: str,
    actions: list[str],
    include_session_start: bool = False,
    session_end_status: str = "completed",
) -> list[dict[str, object]]:
    skill = next(item for item in scenario.context if item["type"] == "skill")
    events: list[dict[str, object]] = []
    if include_session_start:
        events.append(
            {
                "session_id": session_id,
                "action": "session_start",
                "created_at_epoch": 0.0,
            }
        )
    events.extend(
        [
            {
                "session_id": session_id,
                "action": action,
                "entity_type": "skill",
                "slug": skill["slug"],
                "created_at_epoch": float(index),
            }
            for index, action in enumerate(actions, start=1)
        ]
    )
    events.append(
        {
            "session_id": session_id,
            "action": "session_end",
            "status": session_end_status,
            "created_at_epoch": float(len(events) + 1),
        }
    )
    _write_lifecycle_events(root, events)
    return events


def _write_lifecycle_events(root: Path, events: list[dict[str, object]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


@pytest.mark.parametrize("session_end_status", ["completed", "successful"])
def test_production_lifecycle_accepts_optional_unload_request(
    tmp_path: Path,
    session_end_status: str,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    actions = [
        "load_requested",
        "load_applied",
        "used",
        "unload_requested",
        "unload_applied",
    ]
    _write_lifecycle_actions(
        tmp_path,
        scenario,
        session_id="valid-with-request",
        actions=actions,
        session_end_status=session_end_status,
    )

    evidence = benchmark.validate_production_lifecycle(
        scenario,
        lifecycle_root=tmp_path,
        session_id="valid-with-request",
    )

    assert evidence["actions"] == actions
    assert evidence["final_loaded"] == []
    assert evidence["session_status"] == session_end_status
    assert evidence["session_actions"][-1] == "session_end"
    assert len(evidence["lifecycle_sha256"]) == 64


def test_production_lifecycle_rejects_nonempty_final_loaded_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    session_id = "nonempty-final-state"
    _write_lifecycle_actions(
        tmp_path,
        scenario,
        session_id=session_id,
        actions=["load_requested", "load_applied", "used", "unload_applied"],
    )

    class NonemptyStore:
        def session_state(self, *, session_id: str) -> dict[str, object]:
            return {"session_id": session_id, "loaded": [{"slug": "still-loaded"}]}

    monkeypatch.setattr(benchmark, "make_lifecycle_store", lambda _root: NonemptyStore())

    with pytest.raises(ValueError, match="ended with loaded context"):
        benchmark.validate_production_lifecycle(
            scenario,
            lifecycle_root=tmp_path,
            session_id=session_id,
        )


def test_production_lifecycle_rejects_other_entity_transitions(tmp_path: Path) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    events = _write_lifecycle_actions(
        tmp_path,
        scenario,
        session_id="other-entity",
        actions=["load_requested", "load_applied", "used", "unload_applied"],
    )
    events.insert(
        -1,
        {
            "session_id": "other-entity",
            "action": "load_requested",
            "entity_type": "agent",
            "slug": "unexpected-agent",
            "created_at_epoch": 5.0,
        },
    )
    _write_lifecycle_events(tmp_path, events)

    with pytest.raises(ValueError, match="unexpected entity transition"):
        benchmark.validate_production_lifecycle(
            scenario,
            lifecycle_root=tmp_path,
            session_id="other-entity",
        )


def test_production_lifecycle_rejects_foreign_session_before_digest(
    tmp_path: Path,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    events = _write_lifecycle_actions(
        tmp_path,
        scenario,
        session_id="requested-session",
        actions=["load_requested", "load_applied", "used", "unload_applied"],
    )
    events.append(
        {
            "session_id": "foreign-session",
            "action": "load_requested",
            "entity_type": "agent",
            "slug": "foreign-agent",
            "created_at_epoch": 99.0,
        }
    )
    _write_lifecycle_events(tmp_path, events)

    with pytest.raises(ValueError, match="foreign session"):
        benchmark.validate_production_lifecycle(
            scenario,
            lifecycle_root=tmp_path,
            session_id="requested-session",
        )


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("late-session-start", "session_start"),
        ("missing-session-end", "exactly one session_end"),
        ("duplicate-session-end", "exactly one session_end"),
        ("event-after-session-end", "events after session_end"),
        ("failed-session-end", "status is not successful"),
    ],
)
def test_production_lifecycle_rejects_invalid_session_envelope(
    tmp_path: Path,
    case: str,
    match: str,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    events = _write_lifecycle_actions(
        tmp_path,
        scenario,
        session_id="invalid-envelope",
        actions=["load_requested", "load_applied", "used", "unload_applied"],
        include_session_start=True,
    )
    if case == "late-session-start":
        events.insert(2, events.pop(0))
    elif case == "missing-session-end":
        events.pop()
    elif case == "duplicate-session-end":
        events.append(dict(events[-1]))
    elif case == "event-after-session-end":
        events.append(
            {
                "session_id": "invalid-envelope",
                "action": "dev_event",
                "created_at_epoch": 99.0,
            }
        )
    else:
        events[-1]["status"] = "failed"
    _write_lifecycle_events(tmp_path, events)

    with pytest.raises(ValueError, match=match):
        benchmark.validate_production_lifecycle(
            scenario,
            lifecycle_root=tmp_path,
            session_id="invalid-envelope",
        )


def test_baseline_lifecycle_accepts_no_ledger_and_rejects_selected_cycle(
    tmp_path: Path,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]

    empty = benchmark.validate_production_lifecycle(
        scenario,
        lifecycle_root=tmp_path,
        session_id="baseline",
        expect_selected_cycle=False,
    )

    assert empty["lifecycle_emitted"] is False
    assert empty["actions"] == []
    assert empty["final_loaded"] == []
    _write_lifecycle_actions(
        tmp_path,
        scenario,
        session_id="baseline",
        actions=["load_requested", "load_applied", "used", "unload_applied"],
    )
    with pytest.raises(ValueError, match="baseline ctx run"):
        benchmark.validate_production_lifecycle(
            scenario,
            lifecycle_root=tmp_path,
            session_id="baseline",
            expect_selected_cycle=False,
        )


@pytest.mark.parametrize(
    "actions",
    [
        ["load_requested", "load_applied", "unload_applied", "used", "unload_applied"],
        ["load_requested", "load_applied", "used", "unload_applied", "used"],
        [
            "load_requested",
            "load_applied",
            "used",
            "unload_applied",
            "load_requested",
            "load_applied",
            "used",
            "unload_applied",
        ],
        ["load_requested", "load_requested", "load_applied", "used", "unload_applied"],
        ["load_requested", "used", "unload_applied"],
        ["load_requested", "load_applied", "used", "unload_requested", "used", "unload_applied"],
        ["load_requested", "load_applied", "validation", "used", "unload_applied"],
    ],
    ids=[
        "use-after-early-unload",
        "use-after-complete",
        "duplicate-cycle",
        "duplicate-load-request",
        "use-before-load-applied",
        "use-after-unload-request",
        "unexpected-selected-entity-event",
    ],
)
def test_production_lifecycle_rejects_contradictory_transitions(
    tmp_path: Path,
    actions: list[str],
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    _write_lifecycle_actions(
        tmp_path,
        scenario,
        session_id="invalid-transitions",
        actions=actions,
    )

    with pytest.raises(ValueError, match="invalid transition"):
        benchmark.validate_production_lifecycle(
            scenario,
            lifecycle_root=tmp_path,
            session_id="invalid-transitions",
        )


def test_production_patch_is_limited_to_scenario_allowlist(tmp_path: Path) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[1]
    workspace = tmp_path / "repo"
    workspace.mkdir()
    benchmark.run_process(["git", "init", "-q"], cwd=workspace)
    source = workspace / scenario.allowed_changes[0]
    source.parent.mkdir(parents=True)
    source.write_text("old\n", encoding="utf-8")
    patch = (
        f"diff --git a/{scenario.allowed_changes[0]} b/{scenario.allowed_changes[0]}\n"
        f"--- a/{scenario.allowed_changes[0]}\n"
        f"+++ b/{scenario.allowed_changes[0]}\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    assert benchmark.apply_production_patch(scenario, workspace, patch) == [
        scenario.allowed_changes[0]
    ]
    assert source.read_text(encoding="utf-8") == "new\n"
    with pytest.raises(ValueError, match="disallowed paths"):
        benchmark.apply_production_patch(
            scenario,
            workspace,
            "diff --git a/OTHER b/OTHER\n--- /dev/null\n+++ b/OTHER\n@@ -0,0 +1 @@\n+bad\n",
        )


def test_production_trial_mocked_boundaries_validate_usage_and_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    observed_command: list[str] = []

    def fake_prepare(_scenario: object, _cache: Path, destination: Path) -> str:
        destination.mkdir(parents=True)
        for relative in scenario.allowed_changes:
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# source\n", encoding="utf-8")
        return "test-hash"

    def fake_process(argv: list[str], **kwargs: object) -> object:
        observed_command[:] = argv
        env = kwargs["env"]
        assert isinstance(env, dict)
        lifecycle = benchmark.make_lifecycle_store(Path(env["CTX_RUNTIME_LIFECYCLE_DIR"]))
        session_id = argv[argv.index("--session-id") + 1]
        skill = next(item for item in scenario.context if item["type"] == "skill")
        common = {
            "session_id": session_id,
            "entity_type": "skill",
            "slug": skill["slug"],
        }
        lifecycle.load_entity(**common, selected=True, selection_source="host")
        lifecycle.mark_entity_loaded(**common)
        lifecycle.mark_entity_used(**common, evidence="submitted")
        lifecycle.mark_entity_unloaded(**common)
        lifecycle.end_session(session_id=session_id, status="completed")
        sessions_dir = Path(argv[argv.index("--sessions-dir") + 1])
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{session_id}.jsonl").write_text(
            "".join(
                json.dumps(event) + "\n"
                for event in (
                    {
                        "type": "session_start",
                        "session_id": session_id,
                        "provider": "openai",
                        "model": "openai/model",
                        "api_key_env": "OPENAI_API_KEY",
                        "base_url": "",
                        "ctx_tools_enabled": True,
                        "ctx_tool_surface": "adaptive",
                        "ctx_tool_names": list(benchmark.PRODUCTION_CTX_TOOL_NAMES),
                    },
                    {
                        "type": "model_response",
                        "session_id": session_id,
                        "provider": "litellm",
                        "model": "openai/model",
                        "finish_reason": "stop",
                    },
                )
            ),
            encoding="utf-8",
        )
        payload = {
            "session_id": session_id,
            "stop_reason": "completed",
            "final_message": json.dumps({"patch": "diff"}),
            "usage": {
                "tokens_reported": True,
                "input_tokens": 90,
                "cached_input_tokens": 10,
                "output_tokens": 10,
                "total_tokens": 100,
            },
        }
        return benchmark.CommandResult(0, json.dumps(payload), "", 0.25)

    monkeypatch.setattr(benchmark, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(benchmark, "run_process", fake_process)
    monkeypatch.setattr(
        benchmark,
        "apply_production_patch",
        lambda *_args: list(scenario.allowed_changes),
    )
    monkeypatch.setattr(
        benchmark,
        "verify_workspace",
        lambda *_args: benchmark.CommandResult(0, "passed", "", 0.1),
    )
    result = benchmark.run_production_trial(
        scenario,
        arm="ctx-light",
        attempt=1,
        trial=1,
        retry=0,
        cache=tmp_path / "cache",
        output=tmp_path / "output",
        model="openai/model",
        timeout=10,
        dry_run=False,
        incidents=benchmark.IncidentLog(tmp_path / "incidents.csv"),
        api_key_env=None,
        base_url=None,
        max_iterations=3,
        max_tokens=1024,
        provider_timeout=5,
    )

    assert result["status"] == "passed"
    assert result["token_attribution"] == "exact"
    assert result["lifecycle_actions"] == [
        "load_requested",
        "load_applied",
        "used",
        "unload_applied",
    ]
    assert result["final_loaded"] == []
    assert result["ctx_run_session_id"] == "ctx-ab-click-echo-json-ctx-light-1"
    assert result["ctx_run_stop_reason"] == "completed"
    assert result["endpoint_class"] == "provider_default_unverified"
    assert result["evidence_level"] == "functional_unverified"
    assert result["production_efficiency_eligible"] is False
    assert result["provider_identity"] == "openai"
    assert result["provider_response_success"] is True
    assert result["provider_authentication_verified"] is False
    assert result["cryptographic_independence"] is False
    assert "same-process artifacts" in result["evidence_trust_boundary"]
    assert (
        result["ctx_run_payload_sha256"]
        == hashlib.sha256(Path(result["artifact_dir"], "ctx-run.json").read_bytes()).hexdigest()
    )
    assert result["ctx_run_payload_digest_scope"] == "exact_ctx_run_stdout_bytes"
    assert (
        result["lifecycle_sha256"]
        == hashlib.sha256(Path(result["lifecycle_events"]).read_bytes()).hexdigest()
    )
    assert result["lifecycle_digest_scope"] == "entire_isolated_events_jsonl_bytes"
    assert result["expected_ctx_tool_names"] == list(benchmark.PRODUCTION_CTX_TOOL_NAMES)
    assert result["configured_ctx_tool_names"] == sorted(benchmark.PRODUCTION_CTX_TOOL_NAMES)
    assert result["configured_ctx_tool_surface_verified"] is True
    assert result["provider_tool_surface_evidence"] == ("ctx_run_session_start_pre_request_config")
    assert observed_command[observed_command.index("--ctx-tool-surface") + 1] == "adaptive"


def test_production_dry_run_is_labeled_wiring_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]

    def fake_prepare(_scenario: object, _cache: Path, destination: Path) -> str:
        destination.mkdir(parents=True)
        for relative in scenario.allowed_changes:
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# source\n", encoding="utf-8")
        return "test-hash"

    monkeypatch.setattr(benchmark, "prepare_workspace", fake_prepare)
    result = benchmark.run_production_trial(
        scenario,
        arm="ctx-light",
        attempt=1,
        trial=1,
        retry=0,
        cache=tmp_path / "cache",
        output=tmp_path / "output",
        model="openai/model",
        timeout=10,
        dry_run=True,
        incidents=benchmark.IncidentLog(tmp_path / "incidents.csv"),
        api_key_env=None,
        base_url=None,
        max_iterations=1,
        max_tokens=128,
        provider_timeout=5,
    )

    assert result["status"] == "wiring_only"
    assert result["evidence_level"] == "wiring_only"
    assert result["production_efficiency_eligible"] is False
    assert not Path(result["artifact_dir"], "ctx-run.json").exists()


def test_production_ctx_run_subprocess_compares_both_arms_with_local_provider(
    tmp_path: Path,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    changed_relative = scenario.allowed_changes[-1]
    patch = (
        f"diff --git a/{changed_relative} b/{changed_relative}\n"
        f"--- a/{changed_relative}\n"
        f"+++ b/{changed_relative}\n"
        "@@ -1 +1 @@\n"
        '-VALUE = "before"\n'
        '+VALUE = "after"\n'
    )

    shim_dir = tmp_path / "provider-shim"
    shim_dir.mkdir()
    (shim_dir / "litellm.py").write_text(
        """
import json
import urllib.request


def completion(**params):
    payload = {
        key: params[key]
        for key in ("model", "messages", "temperature", "max_tokens", "tools")
        if key in params
    }
    headers = {"Content-Type": "application/json"}
    if params.get("api_key"):
        headers["Authorization"] = f"Bearer {params['api_key']}"
    request = urllib.request.Request(
        str(params["api_base"]).rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=float(params.get("timeout", 5))) as response:
        return json.loads(response.read().decode("utf-8"))
""".lstrip(),
        encoding="utf-8",
    )

    observed_requests: list[dict[str, object]] = []
    expected_treatment_tools = benchmark.production_ctx_tool_schemas()
    provider_payload = {
        "id": "chatcmpl-local",
        "object": "chat.completion",
        "created": 0,
        "model": "openai/local-mock",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"patch": patch}),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 20},
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            if self.headers.get("Authorization") != "Bearer local-test-key":
                self.send_response(401)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            request_payload = json.loads(self.rfile.read(length))
            expected_tools = [] if not observed_requests else expected_treatment_tools
            tool_surface = benchmark.validate_provider_request_tool_surface(
                request_payload,
                expected_tools=expected_tools,
            )
            observed_requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": request_payload,
                    "tool_surface": tool_surface,
                }
            )
            body = json.dumps(provider_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    commands: dict[str, list[str]] = {}
    arm_evidence: dict[str, dict[str, object]] = {}
    try:
        port = int(server.server_address[1])
        session_id = "ctx-ab-local-provider"
        sessions_dir = tmp_path / "sessions"
        base_url = f"http://127.0.0.1:{port}/v1"
        for arm, with_ctx in (("baseline", False), ("ctx-light", True)):
            arm_root = tmp_path / arm
            workspace = arm_root / "repo"
            workspace.mkdir(parents=True)
            assert benchmark.run_process(["git", "init", "-q"], cwd=workspace).returncode == 0
            for relative in scenario.allowed_changes:
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# source\n", encoding="utf-8")
            changed_path = workspace / changed_relative
            changed_path.write_text('VALUE = "before"\n', encoding="utf-8")

            home = arm_root / "home"
            lifecycle_root = arm_root / "lifecycle"
            benchmark.write_ctx_fixture(scenario, home)
            prompt = benchmark.production_task_prompt(scenario, workspace)
            command = benchmark.production_ctx_command(
                model="openai/local-mock",
                prompt=prompt,
                session_id=session_id,
                sessions_dir=sessions_dir,
                with_ctx=with_ctx,
                api_key_env="LOCAL_MOCK_API_KEY",
                base_url=base_url,
                max_iterations=1,
                max_tokens=512,
                provider_timeout=5,
            )
            commands[arm] = command
            env = benchmark._ctx_env(home, lifecycle_root)
            env["CODEX_HOME"] = str(arm_root / "codex-home")
            env["LOCAL_MOCK_API_KEY"] = "local-test-key"
            env["PYTHONPATH"] = os.pathsep.join((str(shim_dir), str(ROOT / "src")))

            result = benchmark.run_process(
                command,
                cwd=workspace,
                env=env,
                timeout=10,
                contain_descendants=True,
            )
            assert result.returncode == 0, result.stderr
            payload = benchmark.validate_production_payload(
                json.loads(result.stdout),
                session_id=session_id,
            )
            usage = benchmark.extract_production_usage(payload)
            returned_patch = benchmark.extract_production_patch(payload)
            changed_paths = benchmark.apply_production_patch(
                scenario,
                workspace,
                returned_patch,
            )
            lifecycle = benchmark.validate_production_lifecycle(
                scenario,
                lifecycle_root=lifecycle_root,
                session_id=session_id,
                expect_selected_cycle=with_ctx,
            )
            provider_provenance = benchmark.extract_provider_response_provenance(
                sessions_dir=sessions_dir,
                session_id=session_id,
                model="openai/local-mock",
                base_url=base_url,
                api_key_env="LOCAL_MOCK_API_KEY",
                env=env,
                expected_ctx_tool_names=(benchmark.PRODUCTION_CTX_TOOL_NAMES if with_ctx else ()),
            )
            arm_evidence[arm] = {
                "usage": usage,
                "changed_paths": changed_paths,
                "changed_body": changed_path.read_text(encoding="utf-8"),
                "lifecycle": lifecycle,
                "provider_provenance": provider_provenance,
                "payload_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            }
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)

    treatment_surface_index = commands["ctx-light"].index("--ctx-tool-surface")
    assert commands["baseline"][:-1] == commands["ctx-light"][:treatment_surface_index]
    assert commands["baseline"][-1:] == ["--no-ctx-tools"]
    assert commands["ctx-light"][treatment_surface_index : treatment_surface_index + 2] == [
        "--ctx-tool-surface",
        "adaptive",
    ]
    for evidence in arm_evidence.values():
        usage = evidence["usage"]
        assert isinstance(usage, dict)
        assert usage["total_tokens"] == 150
        assert usage["cached_input_tokens"] == 20
        assert evidence["changed_paths"] == [changed_relative]
        assert evidence["changed_body"] == 'VALUE = "after"\n'
        assert len(str(evidence["payload_sha256"])) == 64
    baseline_lifecycle = arm_evidence["baseline"]["lifecycle"]
    treatment_lifecycle = arm_evidence["ctx-light"]["lifecycle"]
    assert isinstance(baseline_lifecycle, dict)
    assert isinstance(treatment_lifecycle, dict)
    assert baseline_lifecycle["selected_id"] is None
    assert baseline_lifecycle["actions"] == []
    assert baseline_lifecycle["final_loaded"] == []
    assert treatment_lifecycle["actions"] == [
        "load_requested",
        "load_applied",
        "used",
        "unload_applied",
    ]
    assert treatment_lifecycle["session_status"] == "completed"
    assert treatment_lifecycle["final_loaded"] == []
    assert len(observed_requests) == 2
    assert all(request["path"] == "/v1/chat/completions" for request in observed_requests)
    assert all(request["authorization"] == "Bearer local-test-key" for request in observed_requests)
    baseline_payload = observed_requests[0]["payload"]
    treatment_payload = observed_requests[1]["payload"]
    assert isinstance(baseline_payload, dict)
    assert isinstance(treatment_payload, dict)
    assert baseline_payload.get("tools") in (None, [])
    assert treatment_payload.get("tools") == expected_treatment_tools
    assert observed_requests[0]["tool_surface"] == {
        "provider_request_tool_names": [],
        "provider_request_tool_schema_sha256": hashlib.sha256(b"[]").hexdigest(),
        "provider_request_tool_surface_observed": True,
    }
    treatment_surface = observed_requests[1]["tool_surface"]
    assert isinstance(treatment_surface, dict)
    assert treatment_surface["provider_request_tool_names"] == sorted(
        benchmark.PRODUCTION_CTX_TOOL_NAMES
    )
    assert len(str(treatment_surface["provider_request_tool_schema_sha256"])) == 64
    request_texts: list[str] = []
    for request in observed_requests:
        request_payload = request["payload"]
        assert isinstance(request_payload, dict)
        messages = request_payload.get("messages")
        assert isinstance(messages, list)
        request_texts.append(
            "\n".join(
                str(message.get("content") or "")
                for message in messages
                if isinstance(message, dict)
            )
        )
    skill = next(item for item in scenario.context if item["type"] == "skill")
    skill_body = str(skill["body"]).strip()
    assert skill_body not in request_texts[0]
    assert skill_body in request_texts[1]
    for evidence in arm_evidence.values():
        provider_provenance = evidence["provider_provenance"]
        assert isinstance(provider_provenance, dict)
        assert provider_provenance["provider_identity"] == "openai"
        assert provider_provenance["provider_adapter"] == "litellm"
        assert provider_provenance["provider_response_success"] is True
        assert provider_provenance["provider_identity_verified"] is True
        assert provider_provenance["provider_endpoint_verified"] is True
        assert provider_provenance["provider_authentication_verified"] is True
        assert provider_provenance["configured_ctx_tool_surface_verified"] is True
        assert benchmark.classify_production_evidence(
            base_url=base_url,
            dry_run=False,
            provider_provenance=provider_provenance,
        ) == {
            "endpoint_class": "custom_endpoint",
            "evidence_level": "live_provider",
            "production_efficiency_eligible": True,
        }


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
        deadline = time.monotonic() + 2
        while _pid_is_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_is_running(child_pid)


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
