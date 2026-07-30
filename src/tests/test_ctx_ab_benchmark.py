from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from ctx.adapters.generic.adaptive_runtime import secure_skill_reads_available


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

    assert [scenario.id for scenario in scenarios] == [
        "click-echo-json",
        "requests-json-or",
    ]
    assert all(len(scenario.commit) == 40 for scenario in scenarios)
    assert {scenario.benchmark_class for scenario in scenarios} == {"trivial"}
    assert all(
        {item["type"] for item in scenario.context} == {"skill", "agent", "mcp-server"}
        for scenario in scenarios
    )
    assert [scenario.expected_test_count for scenario in scenarios] == [5, 3]
    assert all(scenario.reference_patch and scenario.allowed_changes for scenario in scenarios)


def test_runtime_pack_independence_accepts_generic_context(tmp_path: Path) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    availability = tmp_path / "runtime-availability.json"
    availability.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "skill:generic-python-testing",
                        "body": "Inspect the target code, add a focused regression, and run tests.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    attestation = benchmark.validate_runtime_pack_scenario_independence(
        [scenario],
        availability_path=availability,
    )

    assert attestation["guard"] == "runtime-pack-distinctive-evidence-v1"
    assert (
        attestation["runtime_availability_sha256"]
        == hashlib.sha256(availability.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scenario_id", lambda scenario: scenario.id),
        (
            "repository",
            lambda scenario: "/".join(scenario.repo_url.removesuffix(".git").rsplit("/", 2)[-2:]),
        ),
        ("commit", lambda scenario: scenario.commit),
        ("query", lambda scenario: scenario.query),
        ("task", lambda scenario: scenario.task),
        ("test_path", lambda scenario: scenario.test_path),
        ("test_body", lambda scenario: scenario.test_body),
        ("reference_patch", lambda scenario: scenario.reference_patch),
        ("allowed_change", lambda scenario: scenario.allowed_changes[0]),
        ("context_body", lambda scenario: next(iter(scenario.context))["body"]),
    ],
)
def test_runtime_pack_independence_rejects_frozen_scenario_evidence(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    availability = tmp_path / "runtime-availability.json"
    availability.write_text(
        json.dumps({"entries": [{"body": value(scenario)}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=rf":{field}") as exc_info:
        benchmark.validate_runtime_pack_scenario_independence(
            [scenario],
            availability_path=availability,
        )
    assert scenario.id not in str(exc_info.value)


def test_runtime_pack_independence_accepts_common_repository_words(tmp_path: Path) -> None:
    scenarios = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")
    availability = tmp_path / "runtime-availability.json"
    availability.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "body": (
                            "Click requests rich testing output from Python through "
                            "the shared ctx-wiki."
                        )
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    benchmark.validate_runtime_pack_scenario_independence(
        scenarios,
        availability_path=availability,
    )


@pytest.mark.parametrize(
    "transform",
    [
        str.upper,
        lambda value: value.replace("/", "\\"),
        lambda value: value.replace("/", " /\r\n "),
        lambda value: f"https://github.com/{value}.git",
        lambda value: value.replace("pallets", "ｐａｌｌｅｔｓ"),
    ],
)
def test_runtime_pack_independence_normalizes_repository_variants(
    tmp_path: Path,
    transform: Any,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    repository = "/".join(scenario.repo_url.removesuffix(".git").rsplit("/", 2)[-2:])
    availability = tmp_path / "runtime-availability.json"
    availability.write_text(
        json.dumps({"entries": [{"body": transform(repository)}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=":repository"):
        benchmark.validate_runtime_pack_scenario_independence(
            [scenario],
            availability_path=availability,
        )


def test_runtime_pack_independence_rejects_distinctive_partial_test_body(
    tmp_path: Path,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    availability = tmp_path / "runtime-availability.json"
    availability.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "body": (
                            "def test_echo_json_is_public_sorted_unicode_output(): "
                            'output = StringIO() click.echo_json({"z": 1, "mark": "x"}, '
                            "file=output)"
                        )
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=":test_body"):
        benchmark.validate_runtime_pack_scenario_independence(
            [scenario],
            availability_path=availability,
        )


def test_scenario_independence_attestation_rejects_mutation(tmp_path: Path) -> None:
    scenario_source = tmp_path / "scenarios.yaml"
    scenario_source.write_bytes((ROOT / "benchmarks/ctx_ab/scenarios.yaml").read_bytes())
    availability = tmp_path / "runtime-availability.json"
    availability.write_text('{"entries": []}\n', encoding="utf-8")
    archive = tmp_path / "catalog.tar.gz"
    archive.write_bytes(b"catalog")
    scenarios = benchmark.load_scenarios(scenario_source)
    attestation = benchmark.validate_runtime_pack_scenario_independence(
        scenarios,
        availability_path=availability,
        scenarios_path=scenario_source,
        archive_path=archive,
    )
    snapshot = benchmark.CatalogSnapshot(
        wiki_dir=tmp_path,
        provenance={
            "runtime_availability_sha256": hashlib.sha256(availability.read_bytes()).hexdigest(),
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        },
    )

    benchmark.verify_scenario_independence_attestation(
        attestation,
        scenarios_path=scenario_source,
        snapshot=snapshot,
    )
    scenario_source.write_text(
        scenario_source.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scenarios_sha256"):
        benchmark.verify_scenario_independence_attestation(
            attestation,
            scenarios_path=scenario_source,
            snapshot=snapshot,
        )


@pytest.mark.parametrize(
    "field",
    ["runtime_availability_sha256", "archive_sha256"],
)
def test_scenario_independence_attestation_rejects_catalog_input_mismatch(
    tmp_path: Path,
    field: str,
) -> None:
    scenario_source = tmp_path / "scenarios.yaml"
    scenario_source.write_bytes((ROOT / "benchmarks/ctx_ab/scenarios.yaml").read_bytes())
    availability = tmp_path / "runtime-availability.json"
    availability.write_text('{"entries": []}\n', encoding="utf-8")
    archive = tmp_path / "catalog.tar.gz"
    archive.write_bytes(b"catalog")
    scenarios = benchmark.load_scenarios(scenario_source)
    attestation = benchmark.validate_runtime_pack_scenario_independence(
        scenarios,
        availability_path=availability,
        scenarios_path=scenario_source,
        archive_path=archive,
    )
    provenance = {
        "runtime_availability_sha256": attestation["runtime_availability_sha256"],
        "archive_sha256": attestation["catalog_archive_sha256"],
    }
    provenance[field] = "0" * 64
    snapshot = benchmark.CatalogSnapshot(
        wiki_dir=tmp_path,
        provenance=provenance,
    )

    with pytest.raises(ValueError, match=field.replace("archive", "catalog_archive")):
        benchmark.verify_scenario_independence_attestation(
            attestation,
            scenarios_path=scenario_source,
            snapshot=snapshot,
        )


def test_click_regression_verification_covers_import_contract() -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]

    assert any("tests/test_imports.py" in command for command in scenario.regression_verify)
    assert "+    import json" in scenario.reference_patch
    assert "\n+import json\n" not in scenario.reference_patch


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


def test_two_arm_schedule_alternates_order_three_times_each() -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    arms = ("baseline", "ctx-light")

    orders = [tuple(row["arms"]) for row in benchmark.trial_schedule([scenario], arms, trials=6)]

    assert orders == [
        ("baseline", "ctx-light"),
        ("ctx-light", "baseline"),
        ("baseline", "ctx-light"),
        ("ctx-light", "baseline"),
        ("baseline", "ctx-light"),
        ("ctx-light", "baseline"),
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


def test_trace_efficiency_counts_tool_output_and_failures() -> None:
    output = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "rg  src",
                        "exit_code": 0,
                        "aggregated_output": "abc",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "rg src",
                        "exit_code": 1,
                        "aggregated_output": "failure",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                }
            ),
        ]
    )

    assert benchmark.extract_trace_efficiency(output) == {
        "completed_item_count": 3,
        "tool_command_count": 2,
        "tool_failure_count": 1,
        "tool_output_bytes": 10,
        "max_tool_output_bytes": 7,
        "oversized_tool_output_count": 0,
        "repeated_tool_command_count": 1,
        "agent_message_count": 1,
        "agent_message_bytes": 4,
    }


def test_trace_efficiency_flags_oversized_tool_output() -> None:
    output = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "rg everything",
                "exit_code": 0,
                "aggregated_output": "x" * (benchmark.PRODUCTION_TOOL_OUTPUT_LIMIT_BYTES + 1),
            },
        }
    )

    metrics = benchmark.extract_trace_efficiency(output)

    assert metrics["max_tool_output_bytes"] == benchmark.PRODUCTION_TOOL_OUTPUT_LIMIT_BYTES + 1
    assert metrics["oversized_tool_output_count"] == 1


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


def _catalog_snapshot(tmp_path: Path) -> Any:
    wiki = tmp_path / "catalog" / ".claude" / "skill-wiki"
    wiki.mkdir(parents=True)
    wiki.chmod(0o550)
    return benchmark.CatalogSnapshot(
        wiki_dir=wiki,
        provenance={
            "archive_sha256": "a" * 64,
            "runtime_availability_sha256": "d" * 64,
            "graph_export_id": "export-1",
            "graph_export_manifest_sha256": "b" * 64,
            "overlay_sha256": "c" * 64,
            "overlay_records": [
                {
                    "overlay_id": "ctx-runtime-availability-v1",
                    "source": "ctx-runtime-availability",
                }
            ],
        },
    )


@pytest.mark.parametrize(
    ("matching_tags", "language", "decision", "reason"),
    [
        (["python"], "python", "abstain", "language_only_match"),
        (["Py"], "PYTHON", "abstain", "language_only_match"),
        (["python", "pytest"], "python", "load", "intent_match"),
        (["javascript"], "python", "abstain", "conflicting_language_match"),
        (
            ["python", "javascript"],
            "python",
            "abstain",
            "conflicting_language_match",
        ),
        (["python", "local"], "python", "abstain", "constraint_only_match"),
        (None, "python", "abstain", "insufficient_match_evidence"),
        ([], "python", "abstain", "insufficient_match_evidence"),
        (["python", "python"], "python", "abstain", "insufficient_match_evidence"),
        (["python", 1], "python", "abstain", "insufficient_match_evidence"),
    ],
)
def test_catalog_match_evidence_requires_intent_beyond_language(
    matching_tags: object,
    language: str,
    decision: str,
    reason: str,
) -> None:
    result = benchmark.classify_catalog_match_evidence(
        {"matching_tags": matching_tags},
        language=language,
    )

    assert result["decision"] == decision
    assert result["reason"] == reason


def test_production_catalog_recommendation_records_candidates_selection_and_body_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    snapshot = _catalog_snapshot(tmp_path)
    body = "Use focused pytest, then run the repository import contract."
    calls: list[tuple[str, dict[str, object]]] = []

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def call_tool(self, name: str, arguments: dict[str, object]) -> str:
            calls.append((name, arguments))
            if name == "ctx__recommend_bundle":
                return json.dumps(
                    {
                        "results": [
                            {
                                "id": "skill:ctx-python-testing",
                                "name": "ctx-python-testing",
                                "type": "skill",
                                "installable": True,
                                "load_status": "local-wiki",
                                "matching_tags": ["python", "pytest"],
                                "source": "ctx-runtime-availability",
                                "source_path": "converted/ctx-python-testing/SKILL.md",
                            },
                            {
                                "id": "agent:ctx-python-reviewer",
                                "name": "ctx-python-reviewer",
                                "type": "agent",
                                "installable": True,
                                "load_status": "available",
                                "tags": ["local", "no-api-key", "python", "security"],
                                "permissions": ["diff:read"],
                                "risk": "low",
                                "external": False,
                                "requires_api_keys": False,
                                "source": "ctx-runtime-availability",
                                "source_path": "entities/agents/ctx-python-reviewer.md",
                            },
                        ],
                        "context_policy": {
                            "initial_load": ["skill:ctx-python-testing"],
                            "deferred": ["agent:ctx-python-reviewer"],
                        },
                    }
                )
            assert name == "ctx__wiki_get"
            return json.dumps(
                {
                    "slug": "ctx-python-testing",
                    "entity_type": "skill",
                    "path": "entities/skills/ctx-python-testing.md",
                    "frontmatter": {
                        "source": "ctx-runtime-availability",
                        "license": "MIT",
                    },
                    "body": body,
                }
            )

    monkeypatch.setattr(benchmark, "_catalog_mcp_client", lambda **_kwargs: Client())
    catalog = benchmark.recommend_production_catalog(
        scenario,
        home=tmp_path / "home",
        lifecycle_root=tmp_path / "lifecycle",
        session_id="catalog-session",
        snapshot=snapshot,
    )
    evidence_path = tmp_path / "recommendations.json"
    benchmark.write_catalog_recommendation_evidence(
        evidence_path,
        catalog,
        used_ids=[],
        snapshot=snapshot,
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert [name for name, _arguments in calls] == [
        "ctx__recommend_bundle",
        "ctx__wiki_get",
    ]
    recommendation_args = calls[0][1]
    assert recommendation_args["local_code_task"] is True
    assert recommendation_args["no_api_keys"] is True
    assert recommendation_args["language"] == "python"
    assert "session_id" not in recommendation_args
    assert evidence["candidate_ids"] == [
        "skill:ctx-python-testing",
        "agent:ctx-python-reviewer",
    ]
    assert evidence["selected_ids"] == ["skill:ctx-python-testing"]
    assert evidence["used_ids"] == []
    assert evidence["deferred_activation"] == catalog["deferred_activation"]
    assert catalog["deferred_activation"] == {
        "decision": "deny",
        "selected_ids": [],
        "reason": "policy_disabled_default_deny",
        "deferred_candidates": [
            {
                "id": "agent:ctx-python-reviewer",
                "type": "agent",
                "loadability": True,
                "permissions": ["diff:read"],
                "risk": "low",
                "tags": ["local", "no-api-key", "python", "security"],
                "external": False,
                "requires_api_keys": False,
                "provenance": {
                    "source": "ctx-runtime-availability",
                    "source_path": "entities/agents/ctx-python-reviewer.md",
                },
            }
        ],
    }
    assert catalog["selected_item"]["body"] == body
    assert catalog["body_provenance"] == {
        "surface": "ctx MCP ctx__wiki_get",
        "wiki_path": "entities/skills/ctx-python-testing.md",
        "wiki_response_sha256": hashlib.sha256(
            json.dumps(
                {
                    "slug": "ctx-python-testing",
                    "entity_type": "skill",
                    "path": "entities/skills/ctx-python-testing.md",
                    "frontmatter": {
                        "source": "ctx-runtime-availability",
                        "license": "MIT",
                    },
                    "body": body,
                }
            ).encode("utf-8")
        ).hexdigest(),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body_bytes": len(body.encode("utf-8")),
        "frontmatter_source": "ctx-runtime-availability",
        "frontmatter_license": "MIT",
        "candidate_source": "ctx-runtime-availability",
        "candidate_source_path": "converted/ctx-python-testing/SKILL.md",
        "catalog_archive_sha256": "a" * 64,
        "catalog_graph_export_id": "export-1",
    }


def test_production_catalog_language_only_match_abstains_before_body_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    calls: list[str] = []

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def call_tool(self, name: str, _arguments: dict[str, object]) -> str:
            calls.append(name)
            if name != "ctx__recommend_bundle":
                raise AssertionError("language-only match must not fetch a wiki body")
            return json.dumps(
                {
                    "results": [
                        {
                            "id": "skill:ctx-python-testing",
                            "name": "ctx-python-testing",
                            "type": "skill",
                            "installable": True,
                            "load_status": "local-wiki",
                            "matching_tags": ["python"],
                            "source": "ctx-runtime-availability",
                            "source_path": "converted/ctx-python-testing/SKILL.md",
                        }
                    ],
                    "context_policy": {
                        "initial_load": ["skill:ctx-python-testing"],
                        "deferred": [],
                    },
                }
            )

    monkeypatch.setattr(benchmark, "_catalog_mcp_client", lambda **_kwargs: Client())
    catalog = benchmark.recommend_production_catalog(
        scenario,
        home=tmp_path / "home",
        lifecycle_root=tmp_path / "lifecycle",
        session_id="language-only",
        snapshot=_catalog_snapshot(tmp_path),
    )

    assert calls == ["ctx__recommend_bundle"]
    assert catalog["selected_ids"] == []
    assert catalog["selected_item"] is None
    assert catalog["body_provenance"] is None
    assert catalog["body_fetch_seconds"] == 0.0
    assert catalog["selection_skip_reason"] == "language_only_match"
    assert catalog["policy_abstention"] == {
        "candidate_id": "skill:ctx-python-testing",
        "decision": "abstain",
        "reason": "language_only_match",
        "language": "python",
        "matching_tags": ["python"],
    }
    prompt_hash = hashlib.sha256(b"unchanged").hexdigest()
    lifecycle_events = [
        {
            "action": "dev_event",
            "event_type": "catalog_recommendation",
            "payload": {
                "candidate_ids": catalog["candidate_ids"],
                "selected_ids": [],
                "context_policy": catalog["context_policy"],
            },
        },
        {"action": "session_end", "status": "passed"},
    ]
    assert benchmark.verify_production_policy_abstention(
        catalog,
        language="python",
        task_prompt_sha256=prompt_hash,
        delivered_prompt_sha256=prompt_hash,
        model_turn_observed=True,
        evaluator_isolation_verified=True,
        mcp_used=False,
        agent_attempted=False,
        lifecycle_events=lifecycle_events,
    )
    spoofed = json.loads(json.dumps(catalog))
    spoofed["selected_ids"] = ["skill:ctx-python-testing"]
    assert not benchmark.verify_production_policy_abstention(
        spoofed,
        language="python",
        task_prompt_sha256=prompt_hash,
        delivered_prompt_sha256=prompt_hash,
        model_turn_observed=True,
        evaluator_isolation_verified=True,
        mcp_used=False,
        agent_attempted=False,
        lifecycle_events=lifecycle_events,
    )
    wrong_policy = json.loads(json.dumps(catalog))
    wrong_policy["candidates"].insert(
        0,
        {
            "id": "skill:intent-specific",
            "name": "intent-specific",
            "type": "skill",
            "installable": True,
            "matching_tags": ["python", "pytest"],
        },
    )
    wrong_policy["candidate_ids"].insert(0, "skill:intent-specific")
    wrong_policy["context_policy"]["initial_load"].insert(0, "skill:intent-specific")
    wrong_policy["policy_initial_load_ids"].insert(0, "skill:intent-specific")
    assert not benchmark.verify_production_policy_abstention(
        wrong_policy,
        language="python",
        task_prompt_sha256=prompt_hash,
        delivered_prompt_sha256=prompt_hash,
        model_turn_observed=True,
        evaluator_isolation_verified=True,
        mcp_used=False,
        agent_attempted=False,
    )
    for field, value in (("type", "agent"), ("installable", False)):
        invalid = json.loads(json.dumps(catalog))
        invalid["candidates"][0][field] = value
        assert not benchmark.verify_production_policy_abstention(
            invalid,
            language="python",
            task_prompt_sha256=prompt_hash,
            delivered_prompt_sha256=prompt_hash,
            model_turn_observed=True,
            evaluator_isolation_verified=True,
            mcp_used=False,
            agent_attempted=False,
        )
    malformed_policy = json.loads(json.dumps(catalog))
    malformed_policy["policy_initial_load_ids"] = "skill:ctx-python-testing"
    assert not benchmark.verify_production_policy_abstention(
        malformed_policy,
        language="python",
        task_prompt_sha256=prompt_hash,
        delivered_prompt_sha256=prompt_hash,
        model_turn_observed=True,
        evaluator_isolation_verified=True,
        mcp_used=False,
        agent_attempted=False,
    )
    boolean_timing = json.loads(json.dumps(catalog))
    boolean_timing["body_fetch_seconds"] = False
    assert not benchmark.verify_production_policy_abstention(
        boolean_timing,
        language="python",
        task_prompt_sha256=prompt_hash,
        delivered_prompt_sha256=prompt_hash,
        model_turn_observed=True,
        evaluator_isolation_verified=True,
        mcp_used=False,
        agent_attempted=False,
    )
    assert not benchmark.verify_production_policy_abstention(
        catalog,
        language="python",
        task_prompt_sha256="a",
        delivered_prompt_sha256="a",
        model_turn_observed=True,
        evaluator_isolation_verified=True,
        mcp_used=False,
        agent_attempted=False,
    )
    tampered_events = json.loads(json.dumps(lifecycle_events))
    tampered_events[0]["payload"]["candidate_ids"] = []
    assert not benchmark.verify_production_policy_abstention(
        catalog,
        language="python",
        task_prompt_sha256=prompt_hash,
        delivered_prompt_sha256=prompt_hash,
        model_turn_observed=True,
        evaluator_isolation_verified=True,
        mcp_used=False,
        agent_attempted=False,
        lifecycle_events=tampered_events,
    )
    for candidate_ids in ([], ["skill:phantom"]):
        coordinated_catalog = json.loads(json.dumps(catalog))
        coordinated_catalog["candidate_ids"] = candidate_ids
        coordinated_events = json.loads(json.dumps(lifecycle_events))
        coordinated_events[0]["payload"]["candidate_ids"] = candidate_ids
        assert not benchmark.verify_production_policy_abstention(
            coordinated_catalog,
            language="python",
            task_prompt_sha256=prompt_hash,
            delivered_prompt_sha256=prompt_hash,
            model_turn_observed=True,
            evaluator_isolation_verified=True,
            mcp_used=False,
            agent_attempted=False,
            lifecycle_events=coordinated_events,
        )


def _deferred_candidate(
    entity_id: str = "mcp-server:ctx-core",
    *,
    entity_type: str = "mcp-server",
    tags: list[str] | None = None,
    permissions: list[str] | None = None,
    risk: str = "low",
) -> dict[str, object]:
    return {
        "id": entity_id,
        "type": entity_type,
        "installable": True,
        "load_status": "available",
        "tags": tags or ["lifecycle", "local", "no-api-key"],
        "permissions": permissions or ["graph:read"],
        "risk": risk,
        "external": False,
        "requires_api_keys": False,
        "source": "ctx-runtime-availability",
        "source_path": f"entities/{entity_type}s/{entity_id.partition(':')[2]}.md",
    }


def _activation_policy(
    *entity_ids: str,
    entity_type: str = "mcp-server",
    permissions: list[str] | None = None,
    max_risk: str = "medium",
) -> dict[str, object]:
    return {
        "enabled": True,
        "allowed_entity_types": [entity_type],
        "allowed_tools": list(entity_ids),
        "allowed_permissions": permissions or ["graph:read"],
        "max_risk": max_risk,
    }


def _public_task_evidence(*, risks: list[str] | None = None) -> dict[str, object]:
    return {
        "language": "python",
        "task_category": "lifecycle",
        "predeclared_risks": risks or [],
        "local_code_task": True,
        "no_api_keys": True,
        "repository_paths": ["src/package.py"],
        "tags": ["lifecycle"],
    }


def test_deferred_activation_defaults_to_deny_and_retains_normalized_evidence() -> None:
    candidate = _deferred_candidate()

    decision = benchmark.decide_deferred_activation(
        stage="pre-solve",
        candidates=[candidate],
        context_policy={"deferred": [candidate["id"]]},
        task_evidence=_public_task_evidence(),
    )

    assert decision["decision"] == "deny"
    assert decision["reason"] == "policy_disabled_default_deny"
    assert decision["selected_ids"] == []
    assert decision["deferred_candidates"][0] == {
        "id": "mcp-server:ctx-core",
        "type": "mcp-server",
        "loadability": True,
        "permissions": ["graph:read"],
        "risk": "low",
        "tags": ["lifecycle", "local", "no-api-key"],
        "external": False,
        "requires_api_keys": False,
        "provenance": {
            "source": "ctx-runtime-availability",
            "source_path": "entities/mcp-servers/ctx-core.md",
        },
    }


@pytest.mark.parametrize(
    ("candidate_updates", "policy_updates", "reason"),
    [
        ({"installable": False}, {}, "not_loadable"),
        ({"load_status": "external-install-required"}, {}, "not_loadable"),
        ({"tags": ["lifecycle"]}, {}, "not_local_no_key"),
        ({"external": True}, {}, "not_local_no_key"),
        ({"external": "false"}, {}, "not_local_no_key"),
        ({"requires_api_keys": True}, {}, "not_local_no_key"),
        ({"requires_api_keys": 0}, {}, "not_local_no_key"),
        ({"permissions": ["repo:write"]}, {}, "permission_denied"),
        ({"risk": "high"}, {"max_risk": "low"}, "risk_exceeds_policy"),
        ({}, {"allowed_entity_types": []}, "type_not_allowed"),
        ({}, {"allowed_tools": []}, "tool_not_allowed"),
    ],
)
def test_deferred_activation_enforces_host_gates(
    candidate_updates: dict[str, object],
    policy_updates: dict[str, object],
    reason: str,
) -> None:
    candidate = _deferred_candidate()
    candidate.update(candidate_updates)
    policy = _activation_policy(str(candidate["id"]))
    policy.update(policy_updates)

    decision = benchmark.decide_deferred_activation(
        stage="pre-solve",
        candidates=[candidate],
        context_policy={"deferred": [candidate["id"]]},
        task_evidence=_public_task_evidence(),
        activation_policy=policy,
    )

    assert decision["decision"] == "deny"
    assert decision["reason"] == reason
    assert decision["selected_ids"] == []


def test_deferred_activation_denies_missing_explicit_safety_flags() -> None:
    candidate = _deferred_candidate()
    candidate.pop("requires_api_keys")

    decision = benchmark.decide_deferred_activation(
        stage="pre-solve",
        candidates=[candidate],
        context_policy={"deferred": [candidate["id"]]},
        task_evidence=_public_task_evidence(),
        activation_policy=_activation_policy(str(candidate["id"])),
    )

    assert decision["decision"] == "deny"
    assert decision["reason"] == "not_local_no_key"


def test_deferred_activation_rejects_duplicate_and_malformed_candidate_ids() -> None:
    candidate = _deferred_candidate()

    with pytest.raises(ValueError, match="must be unique"):
        benchmark.decide_deferred_activation(
            stage="pre-solve",
            candidates=[candidate, dict(candidate)],
            context_policy={"deferred": [candidate["id"]]},
            task_evidence=_public_task_evidence(),
        )
    with pytest.raises(ValueError, match="invalid id or type"):
        benchmark.decide_deferred_activation(
            stage="pre-solve",
            candidates=[candidate, {"id": "", "type": "agent"}],
            context_policy={"deferred": [candidate["id"]]},
            task_evidence=_public_task_evidence(),
        )


def test_deferred_activation_retains_null_provenance_as_unloadable() -> None:
    candidate = _deferred_candidate()
    candidate["source_path"] = None

    decision = benchmark.decide_deferred_activation(
        stage="pre-solve",
        candidates=[candidate],
        context_policy={"deferred": [candidate["id"]]},
        task_evidence=_public_task_evidence(),
        activation_policy=_activation_policy(str(candidate["id"])),
    )

    assert decision["decision"] == "deny"
    assert decision["reason"] == "not_loadable"
    assert decision["deferred_candidates"][0]["loadability"] is False
    assert decision["deferred_candidates"][0]["provenance"]["source_path"] is None


def test_deferred_activation_selects_at_most_one_natural_mcp() -> None:
    first = _deferred_candidate()
    second = _deferred_candidate("mcp-server:ctx-secondary")

    decision = benchmark.decide_deferred_activation(
        stage="pre-solve",
        candidates=[first, second],
        context_policy={"deferred": [first["id"], second["id"]]},
        task_evidence=_public_task_evidence(),
        activation_policy=_activation_policy(str(first["id"]), str(second["id"])),
    )

    assert decision["decision"] == "select"
    assert decision["reason"] == "selected_pre-solve"
    assert decision["selected_ids"] == ["mcp-server:ctx-core"]


def test_deferred_activation_requires_diff_and_risk_for_natural_reviewer() -> None:
    reviewer = _deferred_candidate(
        "agent:ctx-python-reviewer",
        entity_type="agent",
        tags=["local", "no-api-key", "python", "security"],
        permissions=["diff:read"],
        risk="medium",
    )
    policy = _activation_policy(
        str(reviewer["id"]),
        entity_type="agent",
        permissions=["diff:read"],
    )
    denied = benchmark.decide_deferred_activation(
        stage="post-solve",
        candidates=[reviewer],
        context_policy={"deferred": [reviewer["id"]]},
        task_evidence=_public_task_evidence(risks=["security"]),
        activation_policy=policy,
        run_evidence={"diff_non_empty": False, "changed_paths": []},
    )
    selected = benchmark.decide_deferred_activation(
        stage="post-solve",
        candidates=[reviewer],
        context_policy={"deferred": [reviewer["id"]]},
        task_evidence=_public_task_evidence(risks=["security"]),
        activation_policy=policy,
        run_evidence={"diff_non_empty": True, "changed_paths": ["src/package.py"]},
    )

    assert denied["reason"] == "post_solve_requires_diff_and_predeclared_risk"
    assert denied["selected_ids"] == []
    assert selected["reason"] == "selected_post-solve"
    assert selected["selected_ids"] == ["agent:ctx-python-reviewer"]


def test_deferred_activation_rejects_hidden_oracle_keys() -> None:
    candidate = _deferred_candidate()

    with pytest.raises(ValueError, match="hidden-oracle"):
        benchmark.decide_deferred_activation(
            stage="pre-solve",
            candidates=[candidate],
            context_policy={"deferred": [candidate["id"]]},
            task_evidence={
                **_public_task_evidence(),
                "tags": [{"reference_patch": "secret"}],
            },
        )
    with pytest.raises(ValueError, match="hidden-oracle"):
        benchmark.decide_deferred_activation(
            stage="pre-solve",
            candidates=[candidate],
            context_policy={"deferred": [candidate["id"]]},
            task_evidence={
                **_public_task_evidence(),
                "repository_paths": ["hidden/reference.patch"],
            },
        )
    with pytest.raises(ValueError, match="hidden-oracle"):
        benchmark.decide_deferred_activation(
            stage="post-solve",
            candidates=[candidate],
            context_policy={"deferred": [candidate["id"]]},
            task_evidence=_public_task_evidence(risks=["security"]),
            run_evidence={"hidden_tests": ["secret"]},
        )


@pytest.mark.parametrize(
    ("field", "updates"),
    [
        ("task", {"unknown_task_field": True}),
        ("activation", {"unknown_policy_field": True}),
        ("context", {"unknown_context_field": []}),
    ],
)
def test_deferred_activation_rejects_unknown_policy_fields(
    field: str,
    updates: dict[str, object],
) -> None:
    candidate = _deferred_candidate()
    task_evidence = _public_task_evidence()
    activation_policy: dict[str, object] = {}
    context_policy: dict[str, object] = {"deferred": [candidate["id"]]}
    target = {
        "task": task_evidence,
        "activation": activation_policy,
        "context": context_policy,
    }[field]
    target.update(updates)

    with pytest.raises(ValueError, match="unsupported keys"):
        benchmark.decide_deferred_activation(
            stage="pre-solve",
            candidates=[candidate],
            context_policy=context_policy,
            task_evidence=task_evidence,
            activation_policy=activation_policy,
        )


def test_deferred_policy_does_not_change_production_task_prompt_hash() -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    candidate = _deferred_candidate()
    before = hashlib.sha256(benchmark.production_catalog_task_prompt(scenario).encode()).hexdigest()

    benchmark.decide_deferred_activation(
        stage="pre-solve",
        candidates=[candidate],
        context_policy={"deferred": [candidate["id"]]},
        task_evidence=_public_task_evidence(),
        activation_policy=_activation_policy(str(candidate["id"])),
    )
    after = hashlib.sha256(benchmark.production_catalog_task_prompt(scenario).encode()).hexdigest()

    assert before == after == "edec6d52378da164473ea090c12bf19b1ca4173d1c257e5278fd91575d60f90e"


def test_production_catalog_recommendation_allows_no_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    calls: list[str] = []

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def call_tool(self, name: str, _arguments: dict[str, object]) -> str:
            calls.append(name)
            return json.dumps(
                {
                    "results": [
                        {
                            "id": "agent:ctx-python-reviewer",
                            "name": "ctx-python-reviewer",
                            "type": "agent",
                            "installable": True,
                        }
                    ],
                    "context_policy": {"initial_load": []},
                }
            )

    monkeypatch.setattr(benchmark, "_catalog_mcp_client", lambda **_kwargs: Client())
    catalog = benchmark.recommend_production_catalog(
        scenario,
        home=tmp_path / "home",
        lifecycle_root=tmp_path / "lifecycle",
        session_id="no-selection",
        snapshot=_catalog_snapshot(tmp_path),
    )

    assert calls == ["ctx__recommend_bundle"]
    assert catalog["candidate_ids"] == ["agent:ctx-python-reviewer"]
    assert catalog["selected_ids"] == []
    assert catalog["selected_item"] is None
    assert catalog["body_provenance"] is None
    assert benchmark.production_catalog_context_prompt(catalog) == ""


def test_production_skill_use_reason_distinguishes_no_delivery() -> None:
    assert (
        benchmark.production_skill_use_evidence_reason(
            production_catalog=True,
            ctx_enabled=True,
            context_delivery_verified=False,
            policy_abstention_verified=True,
        )
        == "policy_abstained_before_context_delivery"
    )
    assert (
        benchmark.production_skill_use_evidence_reason(
            production_catalog=True,
            ctx_enabled=True,
            context_delivery_verified=False,
        )
        == "no_skill_delivered"
    )
    assert (
        benchmark.production_skill_use_evidence_reason(
            production_catalog=True,
            ctx_enabled=True,
            context_delivery_verified=True,
        )
        == "provider_does_not_expose_semantic_context_attribution"
    )
    assert (
        benchmark.production_skill_use_evidence_reason(
            production_catalog=True,
            ctx_enabled=False,
            context_delivery_verified=False,
        )
        is None
    )


def test_production_catalog_dry_run_never_writes_scenario_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    snapshot = _catalog_snapshot(tmp_path)
    production_body = "Production catalog body, not scenario fixture text."

    def fake_prepare(
        _scenario: object,
        _cache: Path,
        destination: Path,
        *,
        include_evaluator_test: bool,
    ) -> str:
        assert include_evaluator_test is False
        destination.mkdir(parents=True)
        return hashlib.sha256(scenario.test_body.encode("utf-8")).hexdigest()

    def fixture_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("write_ctx_fixture must not run for the production catalog engine")

    catalog = {
        "query": scenario.query,
        "candidates": [
            {
                "id": "skill:ctx-python-testing",
                "name": "ctx-python-testing",
                "type": "skill",
                "installable": True,
            },
            {
                "id": "agent:ctx-python-reviewer",
                "name": "ctx-python-reviewer",
                "type": "agent",
                "installable": True,
            },
        ],
        "candidate_ids": [
            "skill:ctx-python-testing",
            "agent:ctx-python-reviewer",
        ],
        "context_policy": {"initial_load": ["skill:ctx-python-testing"]},
        "policy_field": "initial_load",
        "policy_initial_load_ids": ["skill:ctx-python-testing"],
        "selected_item": {
            "id": "skill:ctx-python-testing",
            "type": "skill",
            "slug": "ctx-python-testing",
            "body": production_body,
        },
        "selected_ids": ["skill:ctx-python-testing"],
        "body_provenance": {
            "surface": "ctx MCP ctx__wiki_get",
            "body_sha256": hashlib.sha256(production_body.encode()).hexdigest(),
        },
        "selection_skip_reason": None,
        "recommendation_response_sha256": "d" * 64,
        "recommendation_seconds": 0.01,
        "body_fetch_seconds": 0.02,
        "surface_seconds": 0.03,
    }
    monkeypatch.setattr(benchmark, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(benchmark, "write_ctx_fixture", fixture_forbidden)
    monkeypatch.setattr(
        benchmark,
        "recommend_production_catalog",
        lambda *_args, **_kwargs: catalog,
    )

    result = benchmark.run_trial(
        scenario,
        arm="ctx-light",
        treatment_level="ctx-light",
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
        catalog_snapshot=snapshot,
    )
    prompt = Path(result["artifact_dir"], "prompt.txt").read_text(encoding="utf-8")

    assert result["engine"] == benchmark.PRODUCTION_CATALOG_ENGINE
    assert result["candidate_ids"] == catalog["candidate_ids"]
    assert result["selected_ids"] == ["skill:ctx-python-testing"]
    assert result["used_ids"] == []
    assert production_body in prompt
    fixture_body = next(item["body"] for item in scenario.context if item["type"] == "skill")
    assert fixture_body.strip() not in prompt
    assert scenario.test_path not in prompt
    assert result["lifecycle_actions"] == [
        "dev_event",
        "load_requested",
        "load_applied",
        "unload_requested",
        "unload_applied",
        "session_end",
    ]
    assert result["final_loaded"] == []


def test_production_catalog_delivery_precedes_terminal_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        context=(),
    )
    snapshot = _catalog_snapshot(tmp_path)
    production_body = "Use focused pytest and preserve the public import contract."
    selected_id = "skill:ctx-python-testing"
    catalog = {
        "query": scenario.query,
        "candidates": [
            {
                "id": selected_id,
                "name": "ctx-python-testing",
                "type": "skill",
                "installable": True,
            }
        ],
        "candidate_ids": [selected_id],
        "context_policy": {"initial_load": [selected_id]},
        "policy_field": "initial_load",
        "policy_initial_load_ids": [selected_id],
        "selected_item": {
            "id": selected_id,
            "type": "skill",
            "slug": "ctx-python-testing",
            "body": production_body,
        },
        "selected_ids": [selected_id],
        "body_provenance": {
            "surface": "ctx MCP ctx__wiki_get",
            "body_sha256": hashlib.sha256(production_body.encode()).hexdigest(),
        },
        "selection_skip_reason": None,
        "recommendation_response_sha256": "d" * 64,
        "recommendation_seconds": 0.01,
        "body_fetch_seconds": 0.02,
        "surface_seconds": 0.03,
    }

    def fake_prepare(
        _scenario: object,
        _cache: Path,
        destination: Path,
        *,
        include_evaluator_test: bool,
    ) -> str:
        assert include_evaluator_test is False
        destination.mkdir(parents=True)
        return hashlib.sha256(scenario.test_body.encode("utf-8")).hexdigest()

    def fake_process(command: list[str], **_kwargs: object) -> Any:
        if command[0] == "codex":
            stdout = "\n".join(
                [
                    json.dumps({"type": "turn.started"}),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 12,
                                "cached_input_tokens": 2,
                                "output_tokens": 3,
                            },
                        }
                    ),
                ]
            )
            return benchmark.CommandResult(0, stdout, "", 0.25)
        return benchmark.CommandResult(0, "", "", 0.01)

    def fake_prepare_home(home: Path, **_kwargs: object) -> Path:
        home.mkdir(parents=True)
        return home

    monkeypatch.setattr(benchmark, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(benchmark, "bind_catalog_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        benchmark,
        "recommend_production_catalog",
        lambda *_args, **_kwargs: catalog,
    )
    monkeypatch.setattr(benchmark, "prepare_isolated_codex_home", fake_prepare_home)
    monkeypatch.setattr(
        benchmark,
        "verify_agent_sandbox_isolation",
        lambda **_kwargs: {"verified": True},
    )
    monkeypatch.setattr(benchmark, "run_process", fake_process)
    monkeypatch.setattr(
        benchmark,
        "_verify_pinned_head",
        lambda *_args: benchmark.CommandResult(0, "", "", 0.01),
    )
    monkeypatch.setattr(
        benchmark,
        "verify_workspace",
        lambda *_args: benchmark.CommandResult(0, "passed", "", 0.01),
    )

    result = benchmark.run_trial(
        scenario,
        arm="ctx-light",
        treatment_level="ctx-light",
        attempt=1,
        trial=1,
        retry=0,
        cache=tmp_path / "cache",
        output=tmp_path / "output",
        codex="codex",
        model="gpt-test",
        timeout=10,
        dry_run=False,
        incidents=benchmark.IncidentLog(tmp_path / "incidents.csv"),
        catalog_snapshot=snapshot,
    )

    lifecycle_path = Path(result["lifecycle_events"])
    lifecycle_bytes = lifecycle_path.read_bytes()
    event_lines = lifecycle_bytes.splitlines(keepends=True)
    events = [json.loads(line) for line in event_lines]
    delivery_index = next(
        index
        for index, event in enumerate(events)
        if event.get("event_type") == "context_delivered"
    )
    session_end_index = next(
        index for index, event in enumerate(events) if event.get("action") == "session_end"
    )
    without_delivery = b"".join(
        line
        for line, event in zip(event_lines, events, strict=True)
        if event.get("event_type") != "context_delivered"
    )
    without_session_end = b"".join(event_lines[:session_end_index])

    assert delivery_index < session_end_index == len(events) - 1
    assert result["lifecycle_sha256"] == hashlib.sha256(lifecycle_bytes).hexdigest()
    assert result["lifecycle_sha256"] != hashlib.sha256(without_delivery).hexdigest()
    assert result["lifecycle_sha256"] != hashlib.sha256(without_session_end).hexdigest()
    assert result["used_ids"] == []
    assert result["skill_use_observed"] is None
    assert result["skill_use_evidence_unavailable_reason"] == (
        "provider_does_not_expose_semantic_context_attribution"
    )
    assert all(event.get("action") != "used" for event in events)


@pytest.mark.parametrize(
    ("matching_tags", "reason", "efficiency_eligible", "evidence_level"),
    [
        (
            ["python"],
            "language_only_match",
            True,
            benchmark.PRODUCTION_POLICY_ABSTENTION_LEVEL,
        ),
        (
            None,
            "insufficient_match_evidence",
            False,
            "production_catalog_ctx_noop",
        ),
    ],
)
def test_production_catalog_live_abstention_is_policy_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matching_tags: object,
    reason: str,
    efficiency_eligible: bool,
    evidence_level: str,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]
    snapshot = _catalog_snapshot(tmp_path)
    selected_id = "skill:ctx-python-testing"
    candidate = {
        "id": selected_id,
        "name": "ctx-python-testing",
        "type": "skill",
        "installable": True,
        "matching_tags": matching_tags,
    }
    policy_abstention = {
        "candidate_id": selected_id,
        **benchmark.classify_catalog_match_evidence(candidate, language="python"),
    }
    catalog = {
        "query": scenario.query,
        "candidates": [candidate],
        "candidate_ids": [selected_id],
        "context_policy": {"initial_load": [selected_id]},
        "policy_field": "initial_load",
        "policy_initial_load_ids": [selected_id],
        "deferred_activation": {
            "decision": "deny",
            "selected_ids": [],
            "reason": "no_deferred_candidates",
            "deferred_candidates": [],
        },
        "selected_item": None,
        "selected_ids": [],
        "body_provenance": None,
        "policy_abstention": policy_abstention,
        "selection_skip_reason": reason,
        "recommendation_response_sha256": "d" * 64,
        "recommendation_seconds": 0.01,
        "body_fetch_seconds": 0.0,
        "surface_seconds": 0.01,
    }

    def fake_prepare(
        _scenario: object,
        _cache: Path,
        destination: Path,
        *,
        include_evaluator_test: bool,
    ) -> str:
        assert include_evaluator_test is False
        destination.mkdir(parents=True)
        return hashlib.sha256(scenario.test_body.encode("utf-8")).hexdigest()

    def fake_process(command: list[str], **_kwargs: object) -> Any:
        if command[0] == "codex":
            stdout = "\n".join(
                [
                    json.dumps({"type": "turn.started"}),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 12,
                                "cached_input_tokens": 2,
                                "output_tokens": 3,
                            },
                        }
                    ),
                ]
            )
            return benchmark.CommandResult(0, stdout, "", 0.25)
        return benchmark.CommandResult(0, "", "", 0.01)

    def fake_prepare_home(home: Path, **_kwargs: object) -> Path:
        home.mkdir(parents=True)
        return home

    monkeypatch.setattr(benchmark, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(benchmark, "bind_catalog_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        benchmark,
        "recommend_production_catalog",
        lambda *_args, **_kwargs: catalog,
    )
    monkeypatch.setattr(benchmark, "prepare_isolated_codex_home", fake_prepare_home)
    monkeypatch.setattr(
        benchmark,
        "verify_agent_sandbox_isolation",
        lambda **_kwargs: {"verified": True},
    )
    monkeypatch.setattr(benchmark, "run_process", fake_process)
    monkeypatch.setattr(
        benchmark,
        "_verify_pinned_head",
        lambda *_args: benchmark.CommandResult(0, "", "", 0.01),
    )
    monkeypatch.setattr(
        benchmark,
        "verify_workspace",
        lambda *_args: benchmark.CommandResult(0, "passed", "", 0.01),
    )

    result = benchmark.run_trial(
        scenario,
        arm="ctx-light",
        treatment_level="ctx-light",
        attempt=1,
        trial=1,
        retry=0,
        cache=tmp_path / "cache",
        output=tmp_path / "output",
        codex="codex",
        model="gpt-test",
        timeout=10,
        dry_run=False,
        incidents=benchmark.IncidentLog(tmp_path / "incidents.csv"),
        catalog_snapshot=snapshot,
    )

    assert result["status"] == "passed"
    assert result["policy_valid"] is True
    assert result["production_efficiency_eligible"] is efficiency_eligible
    assert result["evidence_level"] == evidence_level
    assert result["policy_abstention_applied"] is True
    assert result["policy_abstention_verified"] is efficiency_eligible
    assert result["policy_abstention_reason"] == reason
    assert result["catalog_recommendation_lifecycle_verified"] is True
    assert result["context_delivery_verified"] is False
    assert result["task_prompt_sha256"] == result["delivered_prompt_sha256"]
    assert result["selected_ids"] == result["delivered_ids"] == result["used_ids"] == []
    assert result["body_fetch_seconds"] == 0.0
    assert result["lifecycle_actions"] == ["dev_event", "session_end"]
    assert result["final_loaded"] == []


def test_catalog_archive_validation_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "wiki-graph.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        member = tarfile.TarInfo("../outside.txt")
        payload = b"escape"
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))

    with pytest.raises(ValueError, match="unsafe graph archive path"):
        benchmark.validate_catalog_archive(archive)


def _write_runtime_availability(
    path: Path,
    *,
    content: str,
    version: int = 1,
) -> None:
    path.write_text(
        json.dumps(
            {
                "version": version,
                "entries": [
                    {
                        "id": "skill:ctx-python-testing",
                        "files": [
                            {
                                "path": "converted/ctx-python-testing/SKILL.md",
                                "content": content,
                            }
                        ],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_production_catalog_cache_uses_shipped_installer_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "wiki-graph.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        member = tarfile.TarInfo("index.md")
        payload = b"# catalog\n"
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))
    availability = tmp_path / "runtime-availability.json"
    runtime_content = "source: ctx-runtime-availability\n"
    _write_runtime_availability(availability, content=runtime_content)
    monkeypatch.setattr(benchmark, "PRODUCTION_RUNTIME_AVAILABILITY", availability)
    install_calls: list[Path] = []

    def install(claude_dir: Path, *, archive: Path) -> int:
        assert archive == tmp_path / "wiki-graph.tar.gz"
        install_calls.append(claude_dir)
        wiki = claude_dir / "skill-wiki"
        graph = wiki / "graphify-out"
        runtime_skill = wiki / "converted" / "ctx-python-testing" / "SKILL.md"
        graph.mkdir(parents=True)
        runtime_skill.parent.mkdir(parents=True)
        (graph / "graph-export-manifest.json").write_text(
            json.dumps({"version": 1, "export_id": "frozen-export"}),
            encoding="utf-8",
        )
        (graph / "entity-overlays.jsonl").write_text(
            json.dumps(
                {
                    "overlay_id": "ctx-runtime-availability-v1",
                    "source": "ctx-runtime-availability",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (graph / "graph-store.sqlite3").write_bytes(b"graph-store")
        runtime_skill.write_text(runtime_content, encoding="utf-8")
        return 0

    monkeypatch.setattr(benchmark, "_install_shipped_catalog", install)
    cache_root = tmp_path / "cache"
    first = benchmark.prepare_production_catalog(cache_root, archive=archive)
    second = benchmark.prepare_production_catalog(cache_root, archive=archive)

    assert len(install_calls) == 1
    assert first.wiki_dir == second.wiki_dir
    assert first.provenance["installer"] == "ctx_init.build_graph"
    assert first.provenance["install_mode"] == "runtime"
    assert first.provenance["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (
        first.provenance["runtime_availability_sha256"]
        == hashlib.sha256(availability.read_bytes()).hexdigest()
    )
    assert first.provenance["graph_export_id"] == "frozen-export"
    assert first.provenance["overlay_records"] == [
        {
            "overlay_id": "ctx-runtime-availability-v1",
            "source": "ctx-runtime-availability",
        }
    ]
    assert first.provenance["runtime_availability_files"] == [
        {
            "path": "converted/ctx-python-testing/SKILL.md",
            "sha256": hashlib.sha256(runtime_content.encode()).hexdigest(),
            "size_bytes": len(runtime_content.encode()),
        }
    ]
    assert not first.wiki_dir.stat().st_mode & 0o222
    _write_runtime_availability(availability, content=runtime_content, version=2)
    third = benchmark.prepare_production_catalog(cache_root, archive=archive)
    assert len(install_calls) == 2
    assert third.wiki_dir != first.wiki_dir
    benchmark._remove_catalog_staging(cache_root / "production-catalog")


def test_production_catalog_cache_rejects_content_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "wiki-graph.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        member = tarfile.TarInfo("index.md")
        payload = b"# catalog\n"
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))
    availability = tmp_path / "runtime-availability.json"
    runtime_content = "# testing\n"
    _write_runtime_availability(availability, content=runtime_content)
    monkeypatch.setattr(benchmark, "PRODUCTION_RUNTIME_AVAILABILITY", availability)

    def install(claude_dir: Path, *, archive: Path) -> int:
        assert archive.is_file()
        wiki = claude_dir / "skill-wiki"
        graph = wiki / "graphify-out"
        skill = wiki / "converted" / "ctx-python-testing" / "SKILL.md"
        graph.mkdir(parents=True)
        skill.parent.mkdir(parents=True)
        (graph / "graph-export-manifest.json").write_text(
            json.dumps({"export_id": "frozen-export"}),
            encoding="utf-8",
        )
        (graph / "entity-overlays.jsonl").write_text(
            json.dumps({"overlay_id": "runtime"}) + "\n",
            encoding="utf-8",
        )
        (graph / "graph-store.sqlite3").write_bytes(b"graph-store")
        skill.write_text(runtime_content, encoding="utf-8")
        return 0

    monkeypatch.setattr(benchmark, "_install_shipped_catalog", install)
    cache_root = tmp_path / "cache"
    snapshot = benchmark.prepare_production_catalog(cache_root, archive=archive)
    skill = snapshot.wiki_dir / "converted" / "ctx-python-testing" / "SKILL.md"
    skill.chmod(0o600)
    skill.write_text("# tampered\n", encoding="utf-8")
    skill.chmod(0o440)

    with pytest.raises(ValueError, match="does not match availability pack"):
        benchmark.prepare_production_catalog(cache_root, archive=archive)

    benchmark._remove_catalog_staging(snapshot.wiki_dir.parents[1])


def test_production_catalog_rejects_installer_availability_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "wiki-graph.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        member = tarfile.TarInfo("index.md")
        member.size = 0
        tf.addfile(member, io.BytesIO())
    availability = tmp_path / "runtime-availability.json"
    _write_runtime_availability(availability, content="# expected\n")
    monkeypatch.setattr(benchmark, "PRODUCTION_RUNTIME_AVAILABILITY", availability)

    def install(claude_dir: Path, *, archive: Path) -> int:
        assert archive.is_file()
        wiki = claude_dir / "skill-wiki"
        graph = wiki / "graphify-out"
        skill = wiki / "converted" / "ctx-python-testing" / "SKILL.md"
        graph.mkdir(parents=True)
        skill.parent.mkdir(parents=True)
        (graph / "graph-export-manifest.json").write_text(
            json.dumps({"export_id": "frozen-export"}),
            encoding="utf-8",
        )
        (graph / "entity-overlays.jsonl").write_text(
            json.dumps({"overlay_id": "runtime"}) + "\n",
            encoding="utf-8",
        )
        (graph / "graph-store.sqlite3").write_bytes(b"graph-store")
        skill.write_text("# installed package copy\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(benchmark, "_install_shipped_catalog", install)

    with pytest.raises(ValueError, match="does not match availability pack"):
        benchmark.prepare_production_catalog(tmp_path / "cache", archive=archive)


def test_incident_log_appends_machine_readable_rows(tmp_path: Path) -> None:
    path = tmp_path / "incidents.csv"
    incidents = benchmark.IncidentLog(path)

    incidents.add(
        scenario="click-echo-json",
        arm="ctx-light",
        attempt=1,
        stage="verification",
        failure_class="evaluator",
        message="focused test failed",
        root_cause="official evaluator returned exit 1",
        repro="rerun the frozen evaluator command",
        risk="quality could be scored incorrectly",
        fix="repair the evaluator boundary and rerun",
        evidence="exit 1",
    )

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["scenario"] == "click-echo-json"
    assert rows[0]["failure_class"] == "evaluator"
    assert rows[0]["risk"] == "quality could be scored incorrectly"
    assert rows[0]["fix"] == "repair the evaluator boundary and rerun"
    assert rows[0]["status"] == "open"
    assert rows[0]["incident_id"].startswith("inc-")
    assert rows[0]["fix_commit"] == "pending"
    assert rows[0]["rerun_of"] == "initial-run"
    assert rows[0]["protocol_id"] == "legacy-unfrozen"
    assert rows[0]["protocol_invalidation"] == "not_applicable"
    assert rows[0]["protocol_invalidation_reason"]

    duplicate_path = tmp_path / "duplicate-incidents.csv"
    duplicate = benchmark.IncidentLog(duplicate_path)
    duplicate.add(
        scenario="click-echo-json",
        arm="ctx-light",
        attempt=1,
        stage="verification",
        failure_class="evaluator",
        message="different diagnostic prose",
        root_cause="same incident identity",
        repro="same repro",
        evidence="new evidence",
    )
    with duplicate_path.open(newline="", encoding="utf-8") as fh:
        duplicate_row = next(csv.DictReader(fh))
    assert duplicate_row["incident_id"] == rows[0]["incident_id"]

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
    assert resolved[0]["fix_commit"] == "recovered-without-code-change"
    assert resolved[0]["rerun_of"] == resolved[0]["incident_id"]
    assert "recovered by attempt 2" in resolved[0]["evidence"]


def test_incident_log_fails_closed_on_incomplete_protocol_linkage(tmp_path: Path) -> None:
    path = tmp_path / "incidents.csv"
    incidents = benchmark.IncidentLog(path)

    with pytest.raises(ValueError, match="linkage fields"):
        incidents.add(
            scenario="owner__repo",
            arm="baseline",
            attempt=1,
            stage="verification",
            failure_class="model",
            message="no patch",
            root_cause="model returned no patch",
            repro="run the frozen assignment",
            evidence="empty patch",
            protocol_id="production-graph-holdout-v2",
            protocol_invalidation="not_invalidated",
            protocol_invalidation_reason="",
        )

    incidents.add(
        scenario="owner__repo",
        arm="baseline",
        attempt=1,
        stage="verification",
        failure_class="model",
        message="no patch",
        root_cause="model returned no patch",
        repro="run the frozen assignment",
        evidence="empty patch",
        protocol_id="production-graph-holdout-v2",
        protocol_invalidation="not_invalidated",
        status="observed",
    )
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    rows[0]["fix_commit"] = ""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=benchmark.INCIDENT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    assert incidents.unresolved_count() == 1


def _sealed_production_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sealed: list[dict[str, Any]] = []
    for row in rows:
        result = benchmark._VerifiedProductionResult(
            {
                **row,
                "repository_state_matches_start_at_end": True,
                "environment_manifest_matches_start_at_end": True,
            }
        )
        result.seal()
        sealed.append(result)
    return sealed


def test_production_summary_stays_pending_until_final_attestation(tmp_path: Path) -> None:
    result = benchmark._VerifiedProductionResult(
        {
            "scenario": "scenario",
            "arm": "baseline",
            "trial": 1,
            "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
            "status": "passed",
            "production_efficiency_eligible": True,
            "total_seconds": 1.0,
            "token_attribution": "exact",
            "total_tokens": 10,
        }
    )

    with pytest.raises(RuntimeError, match="before final attestation"):
        result.seal()
    benchmark.write_summary(tmp_path, [result])
    pending = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert pending[0]["production_efficiency_eligible"] is False
    assert pending[0]["evidence_level"] == "attestation_pending"
    assert json.loads((tmp_path / "aggregate.json").read_text(encoding="utf-8")) == []

    result["repository_state_matches_start_at_end"] = True
    result["environment_manifest_matches_start_at_end"] = True
    result.seal()
    benchmark.write_summary(tmp_path, [result])
    final = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert final[0]["production_efficiency_eligible"] is True
    assert json.loads((tmp_path / "aggregate.json").read_text(encoding="utf-8")) == [
        {
            "scenario": "scenario",
            "arm": "baseline",
            "trial": 1,
            "attempts": 1,
            "first_attempt_passed": True,
            "eventual_passed": True,
            "retries_used": 0,
            "cumulative_seconds": 1.0,
            "cumulative_exact_tokens": 10,
        }
    ]


def test_production_summary_preserves_failed_attestation_reason(tmp_path: Path) -> None:
    result = benchmark._VerifiedProductionResult(
        {
            "scenario": "scenario",
            "arm": "baseline",
            "trial": 1,
            "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
            "status": "passed",
            "production_efficiency_eligible": False,
            "evidence_level": "run_attestation_changed",
            "repository_state_matches_start_at_end": False,
            "environment_manifest_matches_start_at_end": True,
        }
    )

    benchmark.write_summary(tmp_path, [result])

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary[0]["production_efficiency_eligible"] is False
    assert summary[0]["evidence_level"] == "run_attestation_changed"
    assert json.loads((tmp_path / "aggregate.json").read_text(encoding="utf-8")) == []


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
                        "measured_phase_seconds": 10 * ratio,
                        "total_seconds": 10 * ratio,
                        "token_attribution": "exact",
                        "total_tokens": int(1000 * ratio),
                        "uncached_input_tokens": int(800 * ratio),
                        "team_token_completeness": "not_applicable",
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


def test_production_catalog_benefit_verdict_is_stricter_than_non_regression() -> None:
    def rows(time_ratio: float, token_ratio: float) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for trial in range(1, 7):
            for arm, seconds, tokens in (
                ("baseline", 10.0, 1000),
                ("ctx-light", 10.0 * time_ratio, int(1000 * token_ratio)),
            ):
                values.append(
                    {
                        "scenario": "scenario",
                        "arm": arm,
                        "trial": trial,
                        "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                        "status": "passed",
                        "repo_url": "https://example.test/repo.git",
                        "measured_phase_seconds": seconds,
                        "total_seconds": seconds,
                        "token_attribution": "exact",
                        "total_tokens": tokens,
                        "uncached_input_tokens": tokens,
                        "team_token_completeness": "not_applicable",
                        "production_efficiency_eligible": True,
                        "evaluator_isolation_verified": True,
                        "context_delivery_verified": arm == "ctx-light",
                    }
                )
        return _sealed_production_rows(values)

    beneficial = benchmark.build_performance_report(
        rows(0.85, 1.05),
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )
    non_regressing = benchmark.build_performance_report(
        rows(1.05, 1.05),
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )
    tradeoff_too_large = benchmark.build_performance_report(
        rows(0.80, 1.11),
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )

    assert beneficial["gate_passed"] is True
    assert beneficial["benefit_verdict"] == "beneficial"
    assert beneficial["beneficial"] is True
    assert non_regressing["gate_passed"] is True
    assert non_regressing["benefit_verdict"] == "not_beneficial"
    assert non_regressing["beneficial"] is False
    assert tradeoff_too_large["gate_passed"] is False
    assert tradeoff_too_large["benefit_verdict"] == "not_beneficial"


def test_production_report_keeps_asymmetric_failure_in_intent_to_treat() -> None:
    rows: list[dict[str, Any]] = []
    for trial in range(1, 7):
        for arm in ("baseline", "ctx-light"):
            rows.append(
                {
                    "scenario": "scenario",
                    "repo_url": "https://example.test/repo.git",
                    "arm": arm,
                    "trial": trial,
                    "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                    "status": "failed" if arm == "ctx-light" and trial == 3 else "passed",
                    "measured_phase_seconds": 10.0,
                    "harness_total_seconds": 11.0,
                    "token_attribution": "exact",
                    "total_tokens": 1000,
                    "uncached_input_tokens": 800,
                    "team_token_completeness": "not_applicable",
                    "production_efficiency_eligible": True,
                    "evaluator_isolation_verified": True,
                    "context_delivery_verified": arm == "ctx-light",
                }
            )

    report = benchmark.build_performance_report(
        _sealed_production_rows(rows),
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )

    assert report["assignment_complete"] is True
    assert report["intent_to_treat"]["baseline"]["pass_rate"] == 1.0
    assert report["intent_to_treat"]["ctx-light"]["pass_rate"] == pytest.approx(5 / 6)
    assert report["quality_preserved"] is False
    assert report["benefit_verdict"] == "not_beneficial"
    assert report["beneficial"] is False


def test_production_report_scores_verified_abstention_as_policy_overhead_only() -> None:
    prompt_hash = "a" * 64
    rows: list[dict[str, Any]] = []
    for trial in range(1, 7):
        rows.append(
            {
                "scenario": "scenario",
                "repo_url": "https://github.com/example/repo.git",
                "arm": "baseline",
                "trial": trial,
                "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                "status": "passed",
                "measured_phase_seconds": 10.0,
                "harness_total_seconds": 10.5,
                "token_attribution": "exact",
                "total_tokens": 1000,
                "uncached_input_tokens": 800,
                "team_token_completeness": "not_applicable",
                "production_efficiency_eligible": True,
                "evaluator_isolation_verified": True,
            }
        )
        rows.append(
            {
                "scenario": "scenario",
                "repo_url": "https://github.com/example/repo.git",
                "arm": "ctx-light",
                "trial": trial,
                "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                "status": "passed",
                "measured_phase_seconds": 10.1,
                "harness_total_seconds": 10.6,
                "token_attribution": "exact",
                "total_tokens": 1005,
                "uncached_input_tokens": 804,
                "team_token_completeness": "not_applicable",
                "production_efficiency_eligible": True,
                "evaluator_isolation_verified": True,
                "context_delivery_verified": False,
                "policy_abstention_applied": True,
                "policy_abstention_verified": True,
                "policy_abstention_reason": "language_only_match",
                "evidence_level": benchmark.PRODUCTION_POLICY_ABSTENTION_LEVEL,
                "policy_valid": True,
                "selected_ids": [],
                "delivered_ids": [],
                "adopted_ids": [],
                "used_ids": [],
                "task_prompt_sha256": prompt_hash,
                "delivered_prompt_sha256": prompt_hash,
                "body_fetch_seconds": 0.0,
                "lifecycle_actions": ["dev_event", "session_end"],
                "lifecycle_session_status": "passed",
                "catalog_recommendation_lifecycle_verified": True,
                "pinned_head_verified": True,
                "repository_state_matches_start_at_end": True,
                "environment_manifest_matches_start_at_end": True,
            }
        )

    forged = benchmark.build_performance_report(
        rows,
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )
    sealed_rows = _sealed_production_rows(rows)
    report = benchmark.build_performance_report(
        sealed_rows,
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories={
            "scenario": "https://github.com/example/repo.git",
        },
    )
    tampered_rows = _sealed_production_rows(rows)
    tampered_rows[1]["total_tokens"] = 999999
    tampered = benchmark.build_performance_report(
        tampered_rows,
        scenario_ids=["scenario"],
        trials=6,
        arms=("baseline", "ctx-light"),
    )

    assert forged["production_efficiency_claim_allowed"] is False
    assert forged["ctx_policy_outcomes"]["verified_abstentions"] == 0
    assert forged["ctx_policy_outcomes"]["untrusted_assignments"] == 6
    assert tampered["production_efficiency_claim_allowed"] is False
    assert tampered["ctx_policy_outcomes"]["verified_abstentions"] == 5
    assert tampered["ctx_policy_outcomes"]["untrusted_assignments"] == 1
    assert tampered["ctx_policy_outcomes"]["abstention_rate"] == pytest.approx(5 / 6)
    assert report["production_efficiency_claim_allowed"] is True
    assert report["evidence_complete"] is True
    assert report["gate_passed"] is True
    assert report["beneficial"] is None
    assert report["benefit_verdict"] == "policy_abstention_only"
    assert report["product_benefit_verdict"] == "insufficient_cross_repo_evidence"
    assert report["ctx_policy_outcomes"] == {
        "assigned": 6,
        "context_delivered": 0,
        "verified_abstentions": 6,
        "unverified_noops": 0,
        "untrusted_assignments": 0,
        "activation_rate": 0.0,
        "abstention_rate": 1.0,
    }


def test_policy_activation_rate_rejects_post_seal_mutation() -> None:
    rows = _sealed_production_rows(
        [
            {
                "scenario": "scenario",
                "arm": "baseline",
                "trial": 1,
                "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                "status": "passed",
                "measured_phase_seconds": 10.0,
                "harness_total_seconds": 10.5,
                "token_attribution": "exact",
                "total_tokens": 1000,
                "uncached_input_tokens": 800,
                "team_token_completeness": "not_applicable",
                "production_efficiency_eligible": True,
                "evaluator_isolation_verified": True,
            },
            {
                "scenario": "scenario",
                "arm": "ctx-light",
                "trial": 1,
                "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                "status": "passed",
                "measured_phase_seconds": 9.0,
                "harness_total_seconds": 9.5,
                "token_attribution": "exact",
                "total_tokens": 900,
                "uncached_input_tokens": 700,
                "team_token_completeness": "not_applicable",
                "production_efficiency_eligible": True,
                "evaluator_isolation_verified": True,
                "context_delivery_verified": True,
            },
        ]
    )
    rows[1]["total_tokens"] = 899

    report = benchmark.build_performance_report(
        rows,
        scenario_ids=["scenario"],
        trials=1,
        arms=("baseline", "ctx-light"),
    )

    assert report["ctx_policy_outcomes"] == {
        "assigned": 1,
        "context_delivered": 0,
        "verified_abstentions": 0,
        "unverified_noops": 0,
        "untrusted_assignments": 1,
        "activation_rate": 0.0,
        "abstention_rate": 0.0,
    }


def test_product_claim_requires_exact_complete_expected_schedule() -> None:
    scenario_ids = [f"scenario-{index}" for index in range(6)]
    expected_repositories = {
        scenario: f"https://github.com/example/repo-{index % 5}.git"
        for index, scenario in enumerate(scenario_ids)
    }
    rows: list[dict[str, Any]] = [
        {
            "scenario": scenario,
            "repo_url": expected_repositories[scenario],
            "arm": arm,
            "trial": trial,
            "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
            "status": "passed",
            "measured_phase_seconds": 10.0,
            "harness_total_seconds": 10.5,
            "token_attribution": "exact",
            "total_tokens": 1000,
            "uncached_input_tokens": 800,
            "team_token_completeness": "not_applicable",
            "production_efficiency_eligible": True,
            "evaluator_isolation_verified": True,
            "context_delivery_verified": arm == "ctx-light",
        }
        for index, scenario in enumerate(scenario_ids)
        for trial in range(1, 7)
        for arm in ("baseline", "ctx-light")
    ]
    control = benchmark.build_performance_report(
        _sealed_production_rows(rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories=expected_repositories,
    )
    rows.append(
        {
            **rows[-1],
            "scenario": "unexpected",
            "repo_url": "https://github.com/example/repo-extra.git",
            "trial": 6,
        }
    )

    report = benchmark.build_performance_report(
        _sealed_production_rows(rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories=expected_repositories,
    )

    assert control["product_claim_eligible"] is True
    assert report["assignment_complete"] is False
    assert report["evidence_complete"] is False
    assert report["gate_passed"] is False
    assert report["product_claim_eligible"] is False
    assert report["claim_scope"] == "scenario_set_only"
    assert report["distinct_repository_count"] == 5
    assert report["ctx_policy_outcomes"]["assigned"] == 36


def test_product_claim_accepts_complete_five_repository_schedule() -> None:
    scenario_ids = [f"scenario-{index}" for index in range(6)]
    expected_repositories = {
        scenario: f"https://github.com/example/repo-{index % 5}.git"
        for index, scenario in enumerate(scenario_ids)
    }
    rows: list[dict[str, Any]] = [
        {
            "scenario": scenario,
            "repo_url": f"https://github.com/example/repo-{index % 5}.git",
            "arm": arm,
            "trial": trial,
            "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
            "status": "passed",
            "measured_phase_seconds": 10.0,
            "harness_total_seconds": 10.5,
            "token_attribution": "exact",
            "total_tokens": 1000,
            "uncached_input_tokens": 800,
            "team_token_completeness": "not_applicable",
            "production_efficiency_eligible": True,
            "evaluator_isolation_verified": True,
            "context_delivery_verified": arm == "ctx-light",
        }
        for index, scenario in enumerate(scenario_ids)
        for trial in range(1, 7)
        for arm in ("baseline", "ctx-light")
    ]

    report = benchmark.build_performance_report(
        _sealed_production_rows(rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories=expected_repositories,
    )
    missing_repository_rows = [dict(row) for row in rows]
    missing_repository_rows[0]["repo_url"] = ""
    missing_repository = benchmark.build_performance_report(
        _sealed_production_rows(missing_repository_rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories=expected_repositories,
    )
    retry_relabelled_rows = [
        *rows,
        {
            **rows[0],
            "repo_url": "https://github.com/example/relabelled.git",
        },
    ]
    retry_relabelled = benchmark.build_performance_report(
        _sealed_production_rows(retry_relabelled_rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories=expected_repositories,
    )
    malformed_mapping = {
        **expected_repositories,
        scenario_ids[0]: "not-a-github-url",
    }
    malformed = benchmark.build_performance_report(
        _sealed_production_rows(rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories=malformed_mapping,
    )
    aliased_scenario = scenario_ids[4]
    aliased_repository = "https://github.com/EXAMPLE/REPO-0.git"
    aliased_mapping = {
        **expected_repositories,
        aliased_scenario: aliased_repository,
    }
    aliased_rows = [
        {
            **row,
            "repo_url": (
                aliased_repository if row["scenario"] == aliased_scenario else row["repo_url"]
            ),
        }
        for row in rows
    ]
    aliased = benchmark.build_performance_report(
        _sealed_production_rows(aliased_rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories=aliased_mapping,
    )

    assert report["assignment_complete"] is True
    assert report["evidence_complete"] is True
    assert report["product_claim_eligible"] is True
    assert report["claim_scope"] == "product_pilot"
    assert report["distinct_repository_count"] == 5
    assert report["product_beneficial"] is False
    assert report["product_benefit_verdict"] == "not_beneficial"
    assert report["repository_cluster_analysis"]["benefit_support_p_value"] == 1.0
    assert missing_repository["product_claim_eligible"] is False
    assert missing_repository["repository_cluster_analysis"]["complete"] is False
    assert retry_relabelled["product_claim_eligible"] is False
    assert retry_relabelled["repository_identity"]["verified_for_all_attempts"] is False
    assert malformed["product_claim_eligible"] is False
    assert malformed["repository_identity"]["predeclared_mapping_valid"] is False
    assert aliased["product_claim_eligible"] is False
    assert aliased["repository_identity"]["predeclared_mapping_valid"] is False
    assert aliased["repository_cluster_analysis"]["benefit_support_p_value"] is None


def test_product_claim_rejects_three_repository_pseudoreplication() -> None:
    scenario_ids = [f"scenario-{index}" for index in range(6)]
    rows: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenario_ids):
        for trial in range(1, 7):
            for arm, ratio in (("baseline", 1.0), ("ctx-light", 0.8)):
                rows.append(
                    {
                        "scenario": scenario,
                        "repo_url": f"https://github.com/example/repo-{index % 3}.git",
                        "arm": arm,
                        "trial": trial,
                        "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                        "status": "passed",
                        "measured_phase_seconds": 10.0 * ratio,
                        "harness_total_seconds": 10.5 * ratio,
                        "token_attribution": "exact",
                        "total_tokens": int(1000 * ratio),
                        "uncached_input_tokens": int(800 * ratio),
                        "team_token_completeness": "not_applicable",
                        "production_efficiency_eligible": True,
                        "evaluator_isolation_verified": True,
                        "context_delivery_verified": arm == "ctx-light",
                    }
                )

    report = benchmark.build_performance_report(
        _sealed_production_rows(rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories={
            scenario: f"https://github.com/example/repo-{index % 3}.git"
            for index, scenario in enumerate(scenario_ids)
        },
    )

    cluster = report["repository_cluster_analysis"]
    assert report["benefit_verdict"] == "beneficial"
    assert report["product_claim_eligible"] is False
    assert report["product_benefit_verdict"] == "insufficient_cross_repo_evidence"
    assert cluster["complete"] is False
    assert cluster["independent_repository_count"] == 3
    assert cluster["benefit_support_count"] == 3
    assert cluster["benefit_support_p_value"] == 0.125
    assert cluster["repeated_trials_count_as_independent_units"] is False


def test_product_claim_requires_repository_cluster_support() -> None:
    repositories = [
        "https://github.com/example/repo-a.git",
        "https://github.com/example/repo-b.git",
        "https://github.com/example/repo-c.git",
        "https://github.com/example/repo-d.git",
        "https://github.com/example/repo-e.git",
    ]
    scenario_repositories = [repositories[0]] * 5 + repositories[1:]
    scenario_ids = [f"scenario-{index}" for index in range(len(scenario_repositories))]
    rows: list[dict[str, Any]] = []
    for scenario, repository in zip(scenario_ids, scenario_repositories, strict=True):
        ctx_ratio = 0.8 if repository == repositories[0] else 1.0
        for trial in range(1, 7):
            for arm, ratio in (("baseline", 1.0), ("ctx-light", ctx_ratio)):
                rows.append(
                    {
                        "scenario": scenario,
                        "repo_url": repository,
                        "arm": arm,
                        "trial": trial,
                        "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                        "status": "passed",
                        "measured_phase_seconds": 10.0 * ratio,
                        "harness_total_seconds": 10.5 * ratio,
                        "token_attribution": "exact",
                        "total_tokens": int(1000 * ratio),
                        "uncached_input_tokens": int(800 * ratio),
                        "team_token_completeness": "not_applicable",
                        "production_efficiency_eligible": True,
                        "evaluator_isolation_verified": True,
                        "context_delivery_verified": arm == "ctx-light",
                    }
                )

    report = benchmark.build_performance_report(
        _sealed_production_rows(rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories=dict(zip(scenario_ids, scenario_repositories, strict=True)),
    )

    cluster = report["repository_cluster_analysis"]
    assert report["median_time_ratio"] == 0.8
    assert report["benefit_verdict"] == "beneficial"
    assert report["product_claim_eligible"] is True
    assert report["product_beneficial"] is False
    assert report["product_benefit_verdict"] == "not_beneficial"
    assert cluster["complete"] is True
    assert cluster["benefit_support_count"] == 1
    assert cluster["benefit_support_p_value"] == 0.96875


def test_product_claim_accepts_consistent_five_repository_benefit() -> None:
    scenario_ids = [f"scenario-{index}" for index in range(6)]
    rows: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenario_ids):
        for trial in range(1, 7):
            for arm, ratio in (("baseline", 1.0), ("ctx-light", 0.8)):
                rows.append(
                    {
                        "scenario": scenario,
                        "repo_url": f"https://github.com/example/repo-{index % 5}.git",
                        "arm": arm,
                        "trial": trial,
                        "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                        "status": "passed",
                        "measured_phase_seconds": 10.0 * ratio,
                        "harness_total_seconds": 10.5 * ratio,
                        "token_attribution": "exact",
                        "total_tokens": int(1000 * ratio),
                        "uncached_input_tokens": int(800 * ratio),
                        "team_token_completeness": "not_applicable",
                        "production_efficiency_eligible": True,
                        "evaluator_isolation_verified": True,
                        "context_delivery_verified": arm == "ctx-light",
                    }
                )

    report = benchmark.build_performance_report(
        _sealed_production_rows(rows),
        scenario_ids=scenario_ids,
        trials=6,
        arms=("baseline", "ctx-light"),
        expected_repositories={
            scenario: f"https://github.com/example/repo-{index % 5}.git"
            for index, scenario in enumerate(scenario_ids)
        },
    )

    cluster = report["repository_cluster_analysis"]
    assert report["product_claim_eligible"] is True
    assert report["product_beneficial"] is True
    assert report["product_benefit_verdict"] == "beneficial"
    assert cluster["benefit_support_count"] == 5
    assert cluster["benefit_support_p_value"] == 0.03125


def test_production_report_excludes_ctx_noop_and_missing_measured_phase() -> None:
    common = {
        "scenario": "scenario",
        "repo_url": "https://example.test/repo.git",
        "trial": 1,
        "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
        "status": "passed",
        "token_attribution": "exact",
        "total_tokens": 100,
        "uncached_input_tokens": 80,
        "team_token_completeness": "not_applicable",
        "production_efficiency_eligible": True,
        "evaluator_isolation_verified": True,
    }
    rows = [
        {
            **common,
            "arm": "baseline",
            "total_seconds": 10.0,
        },
        {
            **common,
            "arm": "ctx-light",
            "measured_phase_seconds": 9.0,
            "total_seconds": 9.0,
            "context_delivery_verified": False,
            "evidence_level": "production_catalog_ctx_noop",
        },
    ]

    report = benchmark.build_performance_report(
        rows,
        scenario_ids=["scenario"],
        trials=1,
        arms=("baseline", "ctx-light"),
    )

    assert report["production_efficiency_claim_allowed"] is False
    assert report["excluded_result_count"] == 2
    assert report["pairs"][0]["reason"] == "paired evidence ineligible"


def test_repository_drift_evidence_is_ineligible_for_product_claims() -> None:
    scenario_ids = [f"scenario-{index}" for index in range(6)]
    rows = [
        {
            "scenario": scenario,
            "repo_url": f"https://example.test/repo-{index % 3}.git",
            "arm": arm,
            "trial": 1,
            "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
            "status": "passed",
            "measured_phase_seconds": 1.0,
            "total_seconds": 1.0,
            "token_attribution": "exact",
            "total_tokens": 10,
            "uncached_input_tokens": 8,
            "team_token_completeness": "not_applicable",
            "production_efficiency_eligible": False,
            "evidence_level": "run_attestation_changed",
            "repository_state_matches_start_at_end": False,
            "environment_manifest_matches_start_at_end": True,
            "evaluator_isolation_verified": True,
            "context_delivery_verified": arm == "ctx-light",
        }
        for index, scenario in enumerate(scenario_ids)
        for arm in ("baseline", "ctx-light")
    ]

    report = benchmark.build_performance_report(
        rows,
        scenario_ids=scenario_ids,
        trials=3,
        arms=("baseline", "ctx-light"),
    )

    assert report["status"] == "functional_only"
    assert report["production_efficiency_claim_allowed"] is False
    assert report["excluded_result_count"] == 12
    assert report["excluded_evidence_levels"] == ["run_attestation_changed"]
    assert report["product_claim_eligible"] is False
    assert report["claim_scope"] == "scenario_set_only"


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
            "measured_phase_seconds": 1.0,
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


def test_codex_production_catalog_evidence_is_scored_only_for_live_runs() -> None:
    assert benchmark.classify_codex_production_catalog_evidence(dry_run=False) == {
        "endpoint_class": "codex_cli_oauth",
        "evidence_level": "production_catalog_context_delivery",
        "production_efficiency_eligible": True,
    }
    assert benchmark.classify_codex_production_catalog_evidence(dry_run=True) == {
        "endpoint_class": "codex_cli_oauth",
        "evidence_level": "production_catalog_wiring_only",
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


def test_codex_controlled_empty_context_fails_before_workspace_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        context=(),
    )

    def workspace_setup_forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("workspace setup must not run for invalid controlled context")

    monkeypatch.setattr(benchmark, "prepare_workspace", workspace_setup_forbidden)

    with pytest.raises(ValueError, match="require skill and agent context"):
        benchmark.run_trial(
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

    assert not (tmp_path / "output").exists()


def test_invalid_treatment_fails_before_workspace_or_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]

    def side_effect_forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid treatment must fail before external work")

    monkeypatch.setattr(benchmark, "prepare_workspace", side_effect_forbidden)
    monkeypatch.setattr(benchmark, "run_process", side_effect_forbidden)

    with pytest.raises(ValueError, match="unsupported treatment level"):
        benchmark.run_trial(
            scenario,
            arm="baseline",
            treatment_level="unknown",
            attempt=1,
            trial=1,
            retry=0,
            cache=tmp_path / "cache",
            output=tmp_path / "output",
            codex="codex",
            model="gpt-test",
            timeout=10,
            dry_run=False,
            incidents=benchmark.IncidentLog(tmp_path / "incidents.csv"),
        )

    assert not (tmp_path / "output").exists()


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


def test_production_catalog_dry_run_requires_catalog_evidence_level() -> None:
    result = {
        "scenario": "scenario-a",
        "arm": "baseline",
        "trial": 1,
        "status": "wiring_only",
        "evidence_level": "production_catalog_wiring_only",
    }

    assert benchmark.dry_run_results_complete(
        [result],
        expected_keys={("scenario-a", "baseline", 1)},
        engine=benchmark.PRODUCTION_CATALOG_ENGINE,
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
            "measured_phase_seconds": 1.0,
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

    assert treated[treated.index("exec") :] == baseline[baseline.index("exec") :]
    assert baseline[:5] == ["codex", "-a", "never", "--disable", "multi_agent"]
    assert treated[:5] == ["codex", "-a", "never", "--enable", "multi_agent"]
    assert baseline[5:7] == ["-c", 'web_search="disabled"']
    assert 'model_provider="openai"' in baseline
    assert "--ephemeral" in baseline
    assert "--ignore-user-config" in baseline
    assert "ctx-wiki" not in " ".join(baseline)
    treated_config = " ".join(treated)
    assert "ctx-wiki" in treated_config
    assert 'default_tools_approval_mode="approve"' in treated_config
    assert 'enabled_tools=["ctx__wiki_get"]' in treated_config
    assert "mcp_servers.ctx-wiki.required=true" in treated_config

    with pytest.raises(ValueError, match="frozen OpenAI provider"):
        benchmark.codex_command(
            codex="codex",
            model="model",
            workspace=tmp_path,
            prompt="task",
            with_ctx=False,
            provider="other",
        )


def test_agent_command_uses_isolated_named_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    command = benchmark.codex_command(
        codex="codex",
        model="model",
        workspace=workspace,
        prompt="task",
        with_ctx=False,
        agent_home=home,
        isolate_evaluator=True,
    )

    assert command[:5] == ["codex", "-a", "never", "--disable", "multi_agent"]
    assert command[command.index("exec") + 1] == "--strict-config"
    assert "--ignore-user-config" not in command
    assert "--sandbox" not in command
    assert command[-1] == "task"
    assert 'web_search="disabled"' in command


def test_isolated_codex_home_denies_credentials_oracles_and_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "original-codex"
    original.mkdir()
    (original / "auth.json").write_text('{"token":"secret"}\n', encoding="utf-8")
    monkeypatch.setattr(benchmark, "ORIGINAL_CODEX_HOME", str(original))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    forbidden = tmp_path / "private-scenarios.yaml"
    forbidden.write_text("oracle\n", encoding="utf-8")
    home = tmp_path / "agent-home"

    benchmark.prepare_isolated_codex_home(
        home,
        workspace=workspace,
        forbidden_reads={"scenario_source": forbidden},
    )

    config = (home / "config.toml").read_text(encoding="utf-8")
    assert 'default_permissions = "ctx_benchmark"' in config
    assert f'{json.dumps(str(workspace))} = "write"' in config
    assert f'{benchmark._toml_key(forbidden)} = "deny"' in config
    assert f'{benchmark._toml_key(home / "auth.json")} = "deny"' in config
    assert f'{benchmark._toml_key(home / "config.toml")} = "deny"' in config
    assert "[permissions.ctx_benchmark.network]\nenabled = false" in config
    assert home.stat().st_mode & 0o777 == 0o700
    assert (home / "auth.json").stat().st_mode & 0o777 == 0o600
    assert (home / "config.toml").stat().st_mode & 0o777 == 0o600


def test_production_agent_env_prefers_checkout_and_neutralizes_color_flags(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex-home"
    workspace = tmp_path / "repo"

    env = benchmark.production_agent_env(
        {"PATH": "/usr/bin", "NO_COLOR": "1", "FORCE_COLOR": "1"},
        home=home,
        workspace=workspace,
    )

    assert env["PYTHONPATH"].split(os.pathsep) == [
        str(workspace / "src"),
        str(workspace),
    ]
    assert env["NO_COLOR"] == ""
    assert env["FORCE_COLOR"] == ""
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)


def test_agent_sandbox_preflight_requires_allowed_write_and_denied_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    home = tmp_path / "home"
    forbidden = tmp_path / "private" / "reference.patch"
    workspace.mkdir()
    home.mkdir()
    forbidden.parent.mkdir()
    (workspace / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    forbidden.write_text("secret\n", encoding="utf-8")
    (home / "auth.json").write_text("secret\n", encoding="utf-8")
    (home / "config.toml").write_text("profile\n", encoding="utf-8")
    observed: list[list[str]] = []
    sensitive = {forbidden, home / "auth.json", home / "config.toml"}

    def fake_run(argv: list[str], **_kwargs: object) -> object:
        observed.append(argv)
        if "/usr/bin/touch" in argv:
            Path(argv[-1]).touch()
            return benchmark.CommandResult(0, "", "", 0.01)
        if Path(argv[-1]) in sensitive:
            return benchmark.CommandResult(1, "", "Operation not permitted", 0.01)
        return benchmark.CommandResult(0, "", "", 0.01)

    monkeypatch.setattr(benchmark, "run_process", fake_run)
    monkeypatch.setattr(
        benchmark,
        "_probe_loopback_network",
        lambda **_kwargs: {
            "parent_returncode": 0,
            "parent_connected": True,
            "sandbox_returncode": 1,
            "sandbox_connected": False,
        },
    )
    evidence = benchmark.verify_agent_sandbox_isolation(
        codex="codex",
        workspace=workspace,
        home=home,
        env={},
        forbidden_reads={"oracle": forbidden},
        project_check=("python", "-m", "pytest", "-q"),
    )

    assert evidence["verified"] is True
    assert all(argv[argv.index("-P") + 1] == "ctx_benchmark" for argv in observed)
    assert evidence["git_canary_returncode"] == 0
    assert evidence["project_canary_returncode"] == 0
    assert evidence["network"] == "restricted"
    assert evidence["network_canary_returncode"] == 1
    assert evidence["forbidden_reads"] == [
        {"label": "credentials", "denied": True, "returncode": 1},
        {"label": "sandbox_config", "denied": True, "returncode": 1},
        {"label": "oracle", "denied": True, "returncode": 1},
    ]
    assert str(forbidden) not in json.dumps(evidence)


def test_agent_sandbox_preflight_rejects_unrelated_command_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    home = tmp_path / "home"
    forbidden = tmp_path / "private-scenarios.yaml"
    workspace.mkdir()
    home.mkdir()
    (workspace / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    forbidden.write_text("oracle\n", encoding="utf-8")
    (home / "auth.json").write_text("secret\n", encoding="utf-8")
    (home / "config.toml").write_text("profile\n", encoding="utf-8")

    def fake_run(argv: list[str], **_kwargs: object) -> object:
        if "/usr/bin/touch" in argv:
            Path(argv[-1]).touch()
            return benchmark.CommandResult(0, "", "", 0.01)
        if argv[-1] == str(workspace / "source.py"):
            return benchmark.CommandResult(0, "", "", 0.01)
        return benchmark.CommandResult(1, "", "No such file or directory", 0.01)

    monkeypatch.setattr(benchmark, "run_process", fake_run)
    monkeypatch.setattr(
        benchmark,
        "_probe_loopback_network",
        lambda **_kwargs: {
            "parent_returncode": 0,
            "parent_connected": True,
            "sandbox_returncode": 1,
            "sandbox_connected": False,
        },
    )

    with pytest.raises(RuntimeError, match="isolation preflight failed"):
        benchmark.verify_agent_sandbox_isolation(
            codex="codex",
            workspace=workspace,
            home=home,
            env={},
            forbidden_reads={"scenario_source": forbidden},
            project_check=("python", "-m", "pytest", "-q"),
        )


def test_production_output_must_use_private_gate_root() -> None:
    private_output = benchmark.PRODUCTION_PRIVATE_RUN_ROOT / "unit-run"

    assert benchmark._validate_production_output_path(private_output) == private_output.resolve()
    assert benchmark._is_system_temp_path(Path("/tmp/private-scenarios.yaml"))
    assert not benchmark._is_system_temp_path(ROOT / "benchmarks/ctx_ab/scenarios.yaml")
    with pytest.raises(ValueError, match="output must be beneath"):
        benchmark._validate_production_output_path(Path("/tmp/ctx-ab-run"))


def test_live_production_scenarios_are_private_and_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    private_root.chmod(0o700)
    source = private_root / "scenarios.yaml"
    source.write_text("version: 1\nscenarios: []\n", encoding="utf-8")
    source.chmod(0o600)
    monkeypatch.setattr(benchmark, "PRODUCTION_PRIVATE_SCENARIO_ROOT", private_root)
    monkeypatch.setattr(benchmark, "_is_system_temp_path", lambda _path: False)

    assert benchmark._validate_production_scenarios_path(source, live=True) == source.resolve()

    source.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        benchmark._validate_production_scenarios_path(source, live=True)
    private_root.chmod(0o755)
    with pytest.raises(ValueError, match="root must be an owner-only directory"):
        benchmark._validate_production_scenarios_path(source, live=True)
    private_root.chmod(0o700)
    with pytest.raises(ValueError, match="must be beneath"):
        benchmark._validate_production_scenarios_path(tmp_path / "public.yaml", live=True)


def test_live_production_requires_clean_committed_harness() -> None:
    dirty = {"clean": False, "status": [" M scripts/ctx_ab_benchmark.py"]}

    with pytest.raises(ValueError, match="clean committed harness"):
        benchmark.require_clean_production_repository(dirty, live=True)

    benchmark.require_clean_production_repository(dirty, live=False)
    benchmark.require_clean_production_repository({"clean": True, "status": []}, live=True)


def test_final_repository_attestation_detects_mid_run_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = {
        "head": "a" * 40,
        "clean": True,
        "status": [],
        "tracked_diff_sha256": hashlib.sha256(b"").hexdigest(),
    }
    final = {
        "head": "a" * 40,
        "clean": False,
        "status": [" M scripts/ctx_ab_benchmark.py"],
        "tracked_diff_sha256": hashlib.sha256(b"diff").hexdigest(),
    }
    initial_manifest = {"repository_state": initial, "model": "gpt-test"}
    (tmp_path / "environment.json").write_text(
        json.dumps(initial_manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(benchmark, "collect_repository_state", lambda: final)

    state_matches, manifest_matches, observed = benchmark.write_final_repository_attestation(
        tmp_path,
        initial,
        initial_manifest,
    )

    manifest = json.loads((tmp_path / "environment.json").read_text(encoding="utf-8"))
    assert state_matches is False
    assert manifest_matches is True
    assert observed == final
    assert manifest["repository_state_end"] == final
    assert manifest["repository_state_matches_start_at_end"] is False
    assert manifest["environment_manifest_matches_start_at_end"] is True


def test_final_repository_attestation_rejects_valid_manifest_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "head": "a" * 40,
        "clean": True,
        "status": [],
        "tracked_diff_sha256": hashlib.sha256(b"").hexdigest(),
    }
    initial_manifest = {
        "repository_state": state,
        "model": "gpt-original",
        "scenarios_sha256": "b" * 64,
    }
    tampered = {
        **initial_manifest,
        "model": "gpt-tampered",
        "scenarios_sha256": "c" * 64,
    }
    (tmp_path / "environment.json").write_text(
        json.dumps(tampered, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(benchmark, "collect_repository_state", lambda: state)

    state_matches, manifest_matches, observed = benchmark.write_final_repository_attestation(
        tmp_path,
        state,
        initial_manifest,
    )

    manifest = json.loads((tmp_path / "environment.json").read_text(encoding="utf-8"))
    assert state_matches is True
    assert manifest_matches is False
    assert observed == state
    assert manifest["model"] == "gpt-original"
    assert manifest["scenarios_sha256"] == "b" * 64
    assert manifest["environment_manifest_matches_start_at_end"] is False


def test_evaluator_controls_scrub_solved_workspaces_and_reference_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0]

    def fake_prepare(
        _scenario: object,
        _cache: Path,
        workspace: Path,
        **_kwargs: object,
    ) -> str:
        workspace.mkdir(parents=True)
        (workspace / "marker.txt").write_text("workspace\n", encoding="utf-8")
        return "test-hash"

    monkeypatch.setattr(benchmark, "prepare_workspace", fake_prepare)
    monkeypatch.setattr(
        benchmark,
        "_focused_verification",
        lambda *_args, **_kwargs: benchmark.CommandResult(
            1,
            scenario.red_failure_contains,
            "",
            0.1,
        ),
    )
    monkeypatch.setattr(
        benchmark,
        "verify_workspace",
        lambda *_args, **_kwargs: benchmark.CommandResult(0, "passed\n", "", 0.2),
    )
    monkeypatch.setattr(
        benchmark,
        "run_process",
        lambda *_args, **_kwargs: benchmark.CommandResult(0, "", "", 0.01),
    )

    result = benchmark.validate_evaluator_controls(
        scenario,
        cache=tmp_path / "cache",
        output=tmp_path / "output",
    )
    controls = tmp_path / "output" / scenario.id / "controls"

    assert result["status"] == "passed"
    assert (controls / "control.json").is_file()
    assert not (controls / "reference.patch").exists()
    assert not (controls / "reference").exists()
    assert not (controls / "red").exists()


def test_isolated_codex_home_cleanup_removes_credentials_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text("secret\n", encoding="utf-8")

    benchmark.remove_isolated_codex_home(home)

    assert not home.exists()
    target = tmp_path / "target"
    target.mkdir()
    home.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="replaced by a symlink"):
        benchmark.remove_isolated_codex_home(home)
    assert target.is_dir()
    assert not home.exists()


def test_pinned_head_guard_rejects_agent_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
    )

    assert benchmark._verify_pinned_head(scenario, workspace).returncode == 0
    source.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "agent commit"], cwd=workspace, check=True)

    result = benchmark._verify_pinned_head(scenario, workspace)
    assert result.returncode == 1
    assert "agent changed pinned HEAD" in result.stderr


def test_tracked_evaluator_test_is_reconstructed_only_from_pristine_base(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    tests = workspace / "tests"
    tests.mkdir()
    evaluator_test = tests / "test_feature.py"
    evaluator_test.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="tests/test_feature.py",
        test_body="def test_new():\n    assert True\n",
        allowed_changes=("source.py",),
    )
    expected_hash = hashlib.sha256(scenario.test_body.encode()).hexdigest()

    benchmark.materialize_evaluator_test(
        scenario,
        workspace,
        expected_hash=expected_hash,
        require_pristine=True,
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")

    assert (
        benchmark._materialize_untracked_changes(scenario, workspace, expected_hash).returncode == 0
    )

    subprocess.run(
        ["git", "checkout", "--detach", "--force", pinned],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    evaluator_test.write_text("def test_agent_change():\n    assert False\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="agent changed"):
        benchmark.materialize_evaluator_test(
            scenario,
            workspace,
            expected_hash=expected_hash,
            require_pristine=True,
        )


def test_tracked_evaluator_rejects_cached_only_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    evaluator_test = workspace / "test_feature.py"
    base_body = "def test_old():\n    assert True\n"
    evaluator_test.write_text(base_body, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
    )
    expected_hash = hashlib.sha256(scenario.test_body.encode()).hexdigest()
    evaluator_test.write_text("def test_staged():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "add", "test_feature.py"], cwd=workspace, check=True)
    evaluator_test.write_text(base_body, encoding="utf-8")

    with pytest.raises(RuntimeError, match="git index"):
        benchmark.materialize_evaluator_test(
            scenario,
            workspace,
            expected_hash=expected_hash,
            require_pristine=True,
        )


@pytest.mark.parametrize(
    "add_args",
    [("add",), ("add", "-N")],
    ids=("staged", "intent-to-add"),
)
def test_untracked_evaluator_rejects_agent_index_entry(
    tmp_path: Path,
    add_args: tuple[str, ...],
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
        allowed_changes=("source.py",),
    )
    expected_hash = hashlib.sha256(scenario.test_body.encode()).hexdigest()
    benchmark.materialize_evaluator_test(
        scenario,
        workspace,
        expected_hash=expected_hash,
    )
    subprocess.run(["git", *add_args, "test_feature.py"], cwd=workspace, check=True)

    result = benchmark._materialize_untracked_changes(scenario, workspace, expected_hash)

    assert result.returncode == 1
    assert "benchmark-owned test" in result.stderr
    assert "git index" in result.stderr


def test_production_evaluator_rejects_deleted_intent_to_add_entry(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
    )
    evaluator = workspace / scenario.test_path
    evaluator.write_text("agent placeholder\n", encoding="utf-8")
    subprocess.run(["git", "add", "-N", scenario.test_path], cwd=workspace, check=True)
    evaluator.unlink()

    with pytest.raises(RuntimeError, match="git index"):
        benchmark.materialize_evaluator_test(
            scenario,
            workspace,
            expected_hash=hashlib.sha256(scenario.test_body.encode()).hexdigest(),
            require_pristine=True,
        )


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_tracked_evaluator_rejects_index_flag_hidden_mutation(
    tmp_path: Path,
    flag: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    evaluator = workspace / "test_feature.py"
    evaluator.write_text("def test_base():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
    )
    subprocess.run(["git", "update-index", flag, scenario.test_path], cwd=workspace, check=True)
    evaluator.write_text("def test_agent():\n    assert False\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="agent changed"):
        benchmark.materialize_evaluator_test(
            scenario,
            workspace,
            expected_hash=hashlib.sha256(scenario.test_body.encode()).hexdigest(),
            require_pristine=True,
        )


def test_evaluator_pristine_check_fails_closed_on_git_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
    )
    monkeypatch.setattr(
        benchmark,
        "run_process",
        lambda *_args, **_kwargs: benchmark.CommandResult(128, "", "fatal: broken git", 0.0),
    )

    with pytest.raises(RuntimeError, match="could not inspect"):
        benchmark.materialize_evaluator_test(
            scenario,
            workspace,
            expected_hash=hashlib.sha256(scenario.test_body.encode()).hexdigest(),
            require_pristine=True,
        )


def test_evaluator_test_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "tests").symlink_to(outside, target_is_directory=True)
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        test_path="tests/test_feature.py",
        test_body="def test_new():\n    assert True\n",
    )

    with pytest.raises(RuntimeError, match="unsafe component"):
        benchmark.materialize_evaluator_test(
            scenario,
            workspace,
            expected_hash=hashlib.sha256(scenario.test_body.encode()).hexdigest(),
        )


def test_focused_verification_rejects_evaluator_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    body = "def test_hidden():\n    assert True\n"
    outside.write_text(body, encoding="utf-8")
    (workspace / "test_feature.py").symlink_to(outside)
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        test_path="test_feature.py",
        test_body=body,
    )

    result = benchmark._focused_verification(
        scenario,
        workspace,
        hashlib.sha256(body.encode()).hexdigest(),
    )

    assert result.returncode == 1
    assert "symlink or unsafe file" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="dir-fd atomic replacement is POSIX-only")
def test_evaluator_materialization_detects_parent_swap_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    tests = workspace / "tests"
    tests.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_test = outside / "test_feature.py"
    outside_test.write_text("outside sentinel\n", encoding="utf-8")
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        test_path="tests/test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
    )
    original_replace = os.replace
    swapped = False

    def swap_parent_before_replace(
        source: str | bytes,
        destination: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and src_dir_fd is not None and dst_dir_fd is not None:
            tests.rename(workspace / "original-tests")
            tests.symlink_to(outside, target_is_directory=True)
            swapped = True
        original_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", swap_parent_before_replace)

    with pytest.raises(RuntimeError, match="unsafe component"):
        benchmark.materialize_evaluator_test(
            scenario,
            workspace,
            expected_hash=hashlib.sha256(scenario.test_body.encode()).hexdigest(),
        )

    assert swapped
    assert outside_test.read_text(encoding="utf-8") == "outside sentinel\n"


def _untracked_allowlist_fixture(
    tmp_path: Path,
) -> tuple[Path, Any, str]:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
        allowed_changes=("source.py",),
    )
    expected_hash = hashlib.sha256(scenario.test_body.encode()).hexdigest()
    benchmark.materialize_evaluator_test(scenario, workspace, expected_hash=expected_hash)
    return workspace, scenario, expected_hash


@pytest.mark.parametrize("ignore_source", ["info-exclude", "core-excludes-file"])
def test_verification_rejects_ignored_untracked_disallowed_change(
    tmp_path: Path,
    ignore_source: str,
) -> None:
    workspace, scenario, expected_hash = _untracked_allowlist_fixture(tmp_path)
    if ignore_source == "info-exclude":
        (workspace / ".git/info/exclude").write_text("forbidden.py\n", encoding="utf-8")
    else:
        excludes = tmp_path / "global-ignore"
        excludes.write_text("forbidden.py\n", encoding="utf-8")
        subprocess.run(
            ["git", "config", "core.excludesFile", str(excludes)],
            cwd=workspace,
            check=True,
        )
    (workspace / "forbidden.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = benchmark._materialize_untracked_changes(scenario, workspace, expected_hash)

    assert result.returncode == 1
    assert "changes outside scenario allowlist" in result.stderr
    assert "forbidden.py" in result.stderr


def test_verification_accepts_ignored_untracked_allowed_change(tmp_path: Path) -> None:
    workspace, scenario, expected_hash = _untracked_allowlist_fixture(tmp_path)
    scenario = replace(
        scenario,
        allowed_changes=(*scenario.allowed_changes, "generated.py"),
    )
    (workspace / ".git/info/exclude").write_text("generated.py\n", encoding="utf-8")
    (workspace / "generated.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = benchmark._materialize_untracked_changes(scenario, workspace, expected_hash)

    assert result.returncode == 0
    staged = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "generated.py" in staged.stdout.splitlines()


def test_verification_rejects_ignored_untracked_change_before_project_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, scenario, expected_hash = _untracked_allowlist_fixture(tmp_path)
    (workspace / ".git/info/exclude").write_text("forbidden.py\n", encoding="utf-8")
    (workspace / "forbidden.py").write_text("VALUE = 2\n", encoding="utf-8")
    project_test_called = False

    def reject_project_test(*_args: object, **_kwargs: object) -> Any:
        nonlocal project_test_called
        project_test_called = True
        raise AssertionError("project test ran before the allowlist preflight")

    monkeypatch.setattr(benchmark, "_run_verified", reject_project_test)

    result = benchmark.verify_workspace(scenario, workspace, expected_hash)

    assert result.returncode == 1
    assert "forbidden.py" in result.stderr
    assert project_test_called is False


def test_verification_ignores_mutable_core_worktree_config(tmp_path: Path) -> None:
    workspace, scenario, expected_hash = _untracked_allowlist_fixture(tmp_path)
    forbidden = workspace / "forbidden.py"
    forbidden.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "forbidden.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "tracked forbidden"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(scenario, commit=pinned)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    subprocess.run(
        ["git", "config", "core.worktree", str(decoy)],
        cwd=workspace,
        check=True,
    )
    forbidden.write_text("VALUE = 2\n", encoding="utf-8")

    result = benchmark._materialize_untracked_changes(scenario, workspace, expected_hash)

    assert result.returncode == 1
    assert "changes outside scenario allowlist" in result.stderr
    assert "forbidden.py" in result.stderr


def test_verification_rejects_cached_only_disallowed_change(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    forbidden = workspace / "forbidden.py"
    forbidden.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
        allowed_changes=("source.py",),
    )
    expected_hash = hashlib.sha256(scenario.test_body.encode()).hexdigest()
    benchmark.materialize_evaluator_test(
        scenario,
        workspace,
        expected_hash=expected_hash,
    )
    forbidden.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "forbidden.py"], cwd=workspace, check=True)
    forbidden.write_text("VALUE = 1\n", encoding="utf-8")

    result = benchmark._materialize_untracked_changes(scenario, workspace, expected_hash)

    assert result.returncode == 1
    assert "changes outside scenario allowlist" in result.stderr
    assert "forbidden.py" in result.stderr


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_verification_rejects_index_flag_hidden_disallowed_change(
    tmp_path: Path,
    flag: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    forbidden = workspace / "forbidden.py"
    forbidden.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
        allowed_changes=("source.py",),
    )
    expected_hash = hashlib.sha256(scenario.test_body.encode()).hexdigest()
    benchmark.materialize_evaluator_test(scenario, workspace, expected_hash=expected_hash)
    subprocess.run(["git", "update-index", flag, "forbidden.py"], cwd=workspace, check=True)
    forbidden.write_text("VALUE = 2\n", encoding="utf-8")

    result = benchmark._materialize_untracked_changes(scenario, workspace, expected_hash)

    assert result.returncode == 1
    assert "unsupported git index flags" in result.stderr
    assert "forbidden.py" in result.stderr


def test_verification_accepts_staged_allowed_change(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    source = workspace / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
        allowed_changes=("source.py",),
    )
    expected_hash = hashlib.sha256(scenario.test_body.encode()).hexdigest()
    benchmark.materialize_evaluator_test(scenario, workspace, expected_hash=expected_hash)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=workspace, check=True)

    result = benchmark._materialize_untracked_changes(scenario, workspace, expected_hash)

    assert result.returncode == 0


def test_verification_unions_staged_and_unstaged_allowed_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "ctx@example.test"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "ctx benchmark"], cwd=workspace, check=True)
    staged = workspace / "staged.py"
    unstaged = workspace / "unstaged.py"
    staged.write_text("VALUE = 1\n", encoding="utf-8")
    unstaged.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "parent"], cwd=workspace, check=True)
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scenario = replace(
        benchmark.load_scenarios(ROOT / "benchmarks/ctx_ab/scenarios.yaml")[0],
        commit=pinned,
        test_path="test_feature.py",
        test_body="def test_hidden():\n    assert True\n",
        allowed_changes=("staged.py", "unstaged.py"),
    )
    expected_hash = hashlib.sha256(scenario.test_body.encode()).hexdigest()
    benchmark.materialize_evaluator_test(scenario, workspace, expected_hash=expected_hash)
    staged.write_text("VALUE = 2\n", encoding="utf-8")
    unstaged.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.py"], cwd=workspace, check=True)

    result = benchmark._materialize_untracked_changes(scenario, workspace, expected_hash)

    assert result.returncode == 0
    assert "staged.py" in result.stdout
    assert "unstaged.py" in result.stdout


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
    assert (
        benchmark.build_parser()
        .parse_args(["--engine", benchmark.PRODUCTION_CATALOG_ENGINE])
        .engine
        == benchmark.PRODUCTION_CATALOG_ENGINE
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
    source.parent.mkdir(parents=True, exist_ok=True)
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
                expect_selected_cycle=with_ctx and secure_skill_reads_available(),
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
    expected_treatment_actions = (
        ["load_requested", "load_applied", "used", "unload_applied"]
        if secure_skill_reads_available()
        else []
    )
    assert treatment_lifecycle["actions"] == expected_treatment_actions
    assert treatment_lifecycle["selected_id"] == (
        "skill:click-public-api-feature" if secure_skill_reads_available() else None
    )
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
    assert (skill_body in request_texts[1]) is secure_skill_reads_available()
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


def _official_holdout_fixture(
    tmp_path: Path,
    *,
    codex_path: Path | None = None,
) -> tuple[Any, dict[str, Path]]:
    codex_path = codex_path or Path(sys.executable)
    scenario_ids = [f"owner__repo-{index}" for index in range(10)]
    test_source = "def test_feature():\n    assert True\n"
    test_sha256 = hashlib.sha256(test_source.encode()).hexdigest()
    scenario_rows = [
        {
            "allowed_changes": ["src/feature.py"],
            "benchmark_class": "historical",
            "commit": f"{index + 1:040x}",
            "ctx_context": [],
            "expected_test_count": 1,
            "id": scenario_id,
            "language": "python",
            "query": f"implement feature {index}",
            "red_failure_contains": "FAILED",
            "reference_patch": (
                "diff --git a/src/feature.py b/src/feature.py\n"
                "--- a/src/feature.py\n"
                "+++ b/src/feature.py\n"
                "@@ -0,0 +1 @@\n"
                "+VALUE = 1\n"
            ),
            "regression_verify": [["{python}", "-m", "pytest", "-q", "tests/test_feature.py"]],
            "repo_url": f"https://github.com/owner/repo-{index}.git",
            "reconstructed_test_sha256": test_sha256,
            "task": f"Implement frozen feature {index}.",
            "test_body": test_source,
            "test_path": "tests/test_feature.py",
            "verify": ["{python}", "-m", "pytest", "-q", "tests/test_feature.py"],
        }
        for index, scenario_id in enumerate(scenario_ids)
    ]
    scenario_pack = {"scenarios": scenario_rows, "version": 1}
    selection = {
        "analysis_instance_ids": scenario_ids,
        "analysis_repository_map": {
            scenario_id: f"https://github.com/owner/repo-{index}.git"
            for index, scenario_id in enumerate(scenario_ids)
        },
        "protocol_id": "production-graph-holdout-v2",
    }
    schedule_assignments = [
        {
            "arms": (
                ["baseline", "ctx-light"]
                if (index + trial - 1) % 2 == 0
                else ["ctx-light", "baseline"]
            ),
            "scenario": scenario_id,
            "trial": trial,
        }
        for trial in range(1, 4)
        for index, scenario_id in enumerate(scenario_ids)
    ]
    schedule = {
        "assignment_count": 30,
        "assignments": schedule_assignments,
        "baseline_first_count": 15,
        "ctx_light_first_count": 15,
        "protocol_id": "production-graph-holdout-v2",
        "schema_version": 1,
        "trials_per_scenario": 3,
    }
    verifier = {
        "bridge_sha256": "1" * 64,
        "docker_cli_sha256": "2" * 64,
        "docker_daemon_id": "daemon",
        "docker_package_sha256": "3" * 64,
        "docker_server_version": "29.5.2",
        "namespace": "swebench",
        "python_environment_sha256": "4" * 64,
        "python_sha256": "5" * 64,
        "revision": "6" * 40,
        "run_evaluation_sha256": "7" * 64,
        "schema_version": 1,
    }
    verifier_sha256 = hashlib.sha256(
        json.dumps(verifier, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    reconstructed = {
        "guard": "reconstructed-test-dependency-v1",
        "module_sha256": {scenario_id: test_sha256 for scenario_id in scenario_ids},
    }
    collision = {
        "collision_count": 0,
        "collision_free": True,
        "scenario_ids": sorted(scenario_ids),
    }

    def encoded(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    blobs = {
        "selection": encoded(selection),
        "scenario_pack": encoded(scenario_pack),
        "schedule": encoded(schedule),
    }
    selection_sha256 = hashlib.sha256(blobs["selection"]).hexdigest()
    scenario_pack_sha256 = hashlib.sha256(blobs["scenario_pack"]).hexdigest()
    reconstructed["selection_sha256"] = selection_sha256
    collision["scenarios_sha256"] = scenario_pack_sha256

    def control_phase(scenario_id: str, phase: str) -> dict[str, Any]:
        failed = phase == "red"
        image_id = "sha256:" + hashlib.sha256(scenario_id.encode()).hexdigest()
        return {
            "artifact_bytes": 100,
            "artifact_count": 2,
            "artifact_manifest_sha256": hashlib.sha256(
                f"{scenario_id}:module".encode()
            ).hexdigest(),
            "container_policy_count": 1,
            "exact_selector_identity": True,
            "fail_to_pass_count": 1,
            "image_id": image_id,
            "pass_to_pass_count": 1,
            "phase": phase,
            "runtime_identity_sha256": hashlib.sha256(
                f"{scenario_id}:runtime".encode()
            ).hexdigest(),
            "status_counts": {"FAILED": 1, "PASSED": 1} if failed else {"PASSED": 2},
            "verifier_evidence_sha256": hashlib.sha256(
                f"{scenario_id}:{phase}:evidence".encode()
            ).hexdigest(),
        }

    controls = {
        "all_scenarios_passed": True,
        "guard": "holdout-control-results-v1",
        "scenario_count": 10,
        "scenario_pack_sha256": scenario_pack_sha256,
        "scenario_results": {
            scenario_id: {
                "changed_test_module_green": True,
                "elapsed_seconds": 10.0,
                "green_evidence_sha256": control_phase(scenario_id, "green")[
                    "verifier_evidence_sha256"
                ],
                "module_evidence_sha256": control_phase(scenario_id, "green")[
                    "artifact_manifest_sha256"
                ],
                "official_swebench": {
                    "green": control_phase(scenario_id, "green"),
                    "image_id": "sha256:" + hashlib.sha256(scenario_id.encode()).hexdigest(),
                    "pins_sha256": verifier_sha256,
                    "red": control_phase(scenario_id, "red"),
                },
                "parent_with_test_patch_red": True,
                "reconstructed_test_sha256": test_sha256,
                "red_evidence_sha256": control_phase(scenario_id, "red")[
                    "verifier_evidence_sha256"
                ],
                "reference_patch_green": True,
                "timeout_compliant": True,
                "timeout_seconds": 900,
            }
            for scenario_id in scenario_ids
        },
        "selection_sha256": selection_sha256,
        "verifier_pins_sha256": verifier_sha256,
    }
    environment = {
        "codex": {"version": "test"},
        "evaluator": {
            "backend": benchmark.OFFICIAL_HOLDOUT_BACKEND,
            "pins_sha256": verifier_sha256,
        },
        "limits": {
            "agent_timeout_seconds": 900.0,
            "arms": ["baseline", "ctx-light"],
            "measured_concurrency": 1,
            "pair_count": 30,
            "retries": 0,
            "sandbox_contract": benchmark.OFFICIAL_SANDBOX_CONTRACT,
            "task_count": 10,
            "trials_per_scenario": 3,
        },
        "model": "gpt-5.5",
        "product_revision": "a" * 40,
        "protocol_id": "production-graph-holdout-v2",
        "provider": "openai",
        "python": {
            "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
            "version": benchmark.platform.python_version(),
        },
        "schema_version": 1,
    }
    blobs.update(
        {
            "collision": encoded(collision),
            "reconstructed": encoded(reconstructed),
            "controls": encoded(controls),
            "environment": encoded(environment),
        }
    )
    protocol = {
        "execution_inputs": {
            "collision_attestation_sha256": hashlib.sha256(blobs["collision"]).hexdigest(),
            "control_results_sha256": hashlib.sha256(blobs["controls"]).hexdigest(),
            "execution_environment_sha256": hashlib.sha256(blobs["environment"]).hexdigest(),
            "execution_schedule_sha256": hashlib.sha256(blobs["schedule"]).hexdigest(),
            "reconstructed_test_attestation_sha256": hashlib.sha256(
                blobs["reconstructed"]
            ).hexdigest(),
            "scenario_pack_sha256": scenario_pack_sha256,
            "selection_output_sha256": selection_sha256,
        },
        "official_swebench_verifier": verifier,
        "product_inputs": {
            "benchmark_script_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
            "catalog_archive_sha256": hashlib.sha256(
                benchmark.PRODUCTION_CATALOG_ARCHIVE.read_bytes()
            ).hexdigest(),
            "codex_binary_sha256": hashlib.sha256(codex_path.read_bytes()).hexdigest(),
            "provider_config_sha256": benchmark.codex_provider_config_sha256("openai"),
            "revision": "a" * 40,
            "runtime_availability_sha256": hashlib.sha256(
                benchmark.PRODUCTION_RUNTIME_AVAILABILITY.read_bytes()
            ).hexdigest(),
        },
        "protocol_id": "production-graph-holdout-v2",
        "schema_version": 2,
        "stage": "execution-frozen",
        "timeouts": {"control_verification_seconds": 900},
        "universe": {"selection_jsonl_sha256": "a" * 64},
    }
    paths: dict[str, Path] = {}
    for name, data in blobs.items():
        path = tmp_path / f"{name}.json"
        path.write_bytes(data)
        path.chmod(0o600)
        paths[name] = path
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_bytes(encoded(protocol))
    paths["protocol"] = protocol_path
    loaded = benchmark.load_execution_frozen_holdout(
        protocol_path=protocol_path,
        expected_protocol_sha256=hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
        selection_path=paths["selection"],
        scenario_pack_path=paths["scenario_pack"],
        collision_path=paths["collision"],
        reconstructed_path=paths["reconstructed"],
        control_results_path=paths["controls"],
        environment_path=paths["environment"],
        schedule_path=paths["schedule"],
    )
    return loaded, paths


def test_execution_frozen_holdout_authenticates_all_inputs_and_balanced_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout, paths = _official_holdout_fixture(tmp_path)
    monkeypatch.setattr(benchmark, "_command_version", lambda _command: "test")

    assert len(holdout.scenarios) == 10
    assert len(holdout.schedule.assignments) == 30
    assert sum(row["arms"][0] == "baseline" for row in holdout.schedule.assignments) == 15
    assert sum(row["arms"][0] == "ctx-light" for row in holdout.schedule.assignments) == 15
    benchmark.validate_holdout_execution_conditions(
        holdout,
        model="gpt-5.5",
        timeout=900.0,
        arms=("baseline", "ctx-light"),
        trials=3,
        retries=0,
        scenario_filters=[],
        codex=sys.executable,
    )
    assert (
        holdout.codex_binary_sha256 == hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    )
    assert holdout.provider_config_sha256 == benchmark.codex_provider_config_sha256("openai")
    assert set(holdout.sensitive_paths) == {
        paths[name]
        for name in (
            "protocol",
            "selection",
            "scenario_pack",
            "collision",
            "reconstructed",
            "controls",
            "environment",
            "schedule",
        )
    }


def test_execution_frozen_holdout_rejects_codex_or_provider_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout, _ = _official_holdout_fixture(tmp_path)
    monkeypatch.setattr(benchmark, "_command_version", lambda _command: "different")

    with pytest.raises(ValueError, match="runtime conditions"):
        benchmark.validate_holdout_execution_conditions(
            holdout,
            model="gpt-5.5",
            timeout=900.0,
            arms=("baseline", "ctx-light"),
            trials=3,
            retries=0,
            scenario_filters=[],
            codex=sys.executable,
        )

    provider_drift = replace(
        holdout,
        provider_config_sha256="0" * 64,
    )
    monkeypatch.setattr(benchmark, "_command_version", lambda _command: "test")
    with pytest.raises(ValueError, match="runtime conditions"):
        benchmark.validate_holdout_execution_conditions(
            provider_drift,
            model="gpt-5.5",
            timeout=900.0,
            arms=("baseline", "ctx-light"),
            trials=3,
            retries=0,
            scenario_filters=[],
            codex=sys.executable,
        )


def test_runtime_reauthentication_rejects_mid_campaign_codex_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex = tmp_path / "codex"
    codex.write_bytes(b"frozen-codex-binary")
    holdout_root = tmp_path / "holdout"
    holdout_root.mkdir()
    holdout, _ = _official_holdout_fixture(holdout_root, codex_path=codex)
    monkeypatch.setattr(benchmark, "_command_version", lambda _command: "test")
    conditions = {
        "model": "gpt-5.5",
        "timeout": 900.0,
        "arms": ("baseline", "ctx-light"),
        "trials": 3,
        "retries": 0,
        "scenario_filters": [],
        "codex": str(codex),
    }

    first = benchmark.validate_holdout_execution_conditions(holdout, **conditions)
    codex.write_bytes(b"drifted-codex-binary")

    assert first["codex_binary_sha256"] == holdout.codex_binary_sha256
    with pytest.raises(ValueError, match="runtime conditions"):
        benchmark.validate_holdout_execution_conditions(holdout, **conditions)


def test_execution_frozen_holdout_rejects_mutated_materialization_input(
    tmp_path: Path,
) -> None:
    holdout, paths = _official_holdout_fixture(tmp_path)
    paths["selection"].write_bytes(paths["selection"].read_bytes() + b"\n")

    assert benchmark.holdout_inputs_match_execution_freeze(holdout) is False
    with pytest.raises(ValueError, match="does not match the execution freeze"):
        benchmark.load_execution_frozen_holdout(
            protocol_path=holdout.protocol_path,
            expected_protocol_sha256=holdout.protocol_sha256,
            selection_path=paths["selection"],
            scenario_pack_path=paths["scenario_pack"],
            collision_path=paths["collision"],
            reconstructed_path=paths["reconstructed"],
            control_results_path=paths["controls"],
            environment_path=paths["environment"],
            schedule_path=paths["schedule"],
        )


def test_execution_frozen_holdout_rejects_incomplete_evaluator_closure(
    tmp_path: Path,
) -> None:
    holdout, paths = _official_holdout_fixture(tmp_path)
    controls = json.loads(paths["controls"].read_text())
    scenario_id = next(iter(controls["scenario_results"]))
    del controls["scenario_results"][scenario_id]["official_swebench"]["green"][
        "runtime_identity_sha256"
    ]
    paths["controls"].write_text(
        json.dumps(controls, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    protocol = json.loads(paths["protocol"].read_text())
    protocol["execution_inputs"]["control_results_sha256"] = hashlib.sha256(
        paths["controls"].read_bytes()
    ).hexdigest()
    paths["protocol"].write_text(
        json.dumps(protocol, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="red/green control evidence"):
        benchmark.load_execution_frozen_holdout(
            protocol_path=paths["protocol"],
            expected_protocol_sha256=hashlib.sha256(paths["protocol"].read_bytes()).hexdigest(),
            selection_path=paths["selection"],
            scenario_pack_path=paths["scenario_pack"],
            collision_path=paths["collision"],
            reconstructed_path=paths["reconstructed"],
            control_results_path=paths["controls"],
            environment_path=paths["environment"],
            schedule_path=paths["schedule"],
        )
    assert holdout.image_ids


def _official_result_rows(
    holdout: Any,
    *,
    status: str,
    failure_class: str | None,
    team_token_completeness: str = "not_applicable",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    runtime_identity = {
        "codex_binary_sha256": holdout.codex_binary_sha256,
        "codex_version": "test",
        "provider": "openai",
        "provider_config_sha256": holdout.provider_config_sha256,
        "python_executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "python_version": benchmark.platform.python_version(),
    }
    for scenario in holdout.scenarios:
        for trial in range(1, 4):
            for arm in ("baseline", "ctx-light"):
                row = benchmark._VerifiedProductionResult(
                    {
                        "agent_timeout_seconds": 900.0,
                        "arm": arm,
                        "cached_input_tokens": 20,
                        "context_delivery_verified": arm == "ctx-light",
                        "delivered_prompt_sha256": "b" * 64,
                        "development_seconds": 10.0,
                        "engine": benchmark.PRODUCTION_CATALOG_ENGINE,
                        "environment_manifest_matches_start_at_end": True,
                        "evaluator_contract_sha256": "c" * 64,
                        "evaluator_isolation_verified": True,
                        "evidence_level": (
                            "official_swebench_scored"
                            if status == "passed"
                            else "official_swebench_model_quality_failure"
                        ),
                        "frozen_scenario_sha256": holdout.scenario_sha256[scenario.id],
                        "frozen_schedule_sha256": holdout.schedule.sha256,
                        "frozen_codex_binary_sha256": holdout.codex_binary_sha256,
                        "frozen_provider": "openai",
                        "frozen_provider_config_sha256": holdout.provider_config_sha256,
                        "harness_total_seconds": 12.0,
                        "holdout_collision_sha256": holdout.collision_sha256,
                        "holdout_control_results_sha256": holdout.control_results_sha256,
                        "holdout_environment_sha256": holdout.environment_sha256,
                        "holdout_inputs_match_start_at_end": True,
                        "holdout_protocol_sha256": holdout.protocol_sha256,
                        "holdout_reconstructed_sha256": holdout.reconstructed_sha256,
                        "holdout_scenario_pack_sha256": holdout.scenario_pack_sha256,
                        "holdout_selection_sha256": holdout.selection_sha256,
                        "input_tokens": 100,
                        "measured_phase_seconds": 10.0,
                        "model": "gpt-5.5",
                        "output_tokens": 30,
                        "policy_valid": True,
                        "production_efficiency_eligible": True,
                        "repo_url": scenario.repo_url,
                        "repository_state_matches_start_at_end": True,
                        "runtime_identity_before_arm": runtime_identity,
                        "runtime_identity_matches_start_at_end": True,
                        "runtime_identity_verified_before_arm": True,
                        "sandbox_contract": benchmark.OFFICIAL_SANDBOX_CONTRACT,
                        "scenario": scenario.id,
                        "scenario_commit": scenario.commit,
                        "status": status,
                        "task_prompt_sha256": "a" * 64,
                        "team_token_completeness": team_token_completeness,
                        "token_attribution": "exact",
                        "total_tokens": 130,
                        "trial": trial,
                        "uncached_input_tokens": 80,
                        "verification_backend": benchmark.OFFICIAL_HOLDOUT_BACKEND,
                        "verification_failure_class": failure_class,
                        "verification_passed": status == "passed",
                    }
                )
                row.seal()
                rows.append(row)
    return rows


def test_authenticated_freeze_drives_all_thirty_pairs_and_sixty_arms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdout, paths = _official_holdout_fixture(tmp_path)
    result_by_key = {
        (row["scenario"], row["trial"], row["arm"]): dict(row)
        for row in _official_result_rows(holdout, status="passed", failure_class=None)
    }
    calls: list[tuple[str, int, str]] = []
    runtime_identity_checks: list[dict[str, str]] = []
    run_root = tmp_path / "runs"
    output = run_root / "confirmatory"
    cache_root = tmp_path / "cache"

    monkeypatch.setattr(benchmark, "PRODUCTION_PRIVATE_RUN_ROOT", run_root)
    monkeypatch.setattr(
        benchmark,
        "_validate_production_scenarios_path",
        lambda path, *, live: path.resolve(),
    )
    monkeypatch.setattr(benchmark, "_command_version", lambda _command: "test")
    validate_runtime = benchmark.validate_holdout_execution_conditions

    def record_runtime_identity(*args: Any, **kwargs: Any) -> dict[str, str]:
        identity = validate_runtime(*args, **kwargs)
        runtime_identity_checks.append(identity)
        return identity

    monkeypatch.setattr(
        benchmark,
        "validate_holdout_execution_conditions",
        record_runtime_identity,
    )
    monkeypatch.setattr(
        benchmark,
        "collect_repository_state",
        lambda: {
            "clean": True,
            "head": "a" * 40,
            "status": [],
            "tracked_diff_sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(
        benchmark,
        "validate_runtime_pack_scenario_independence",
        lambda *_args, **_kwargs: {
            "guard": "runtime-pack-distinctive-evidence-v1",
            "runtime_availability_sha256": "e" * 64,
        },
    )
    monkeypatch.setattr(
        benchmark,
        "prepare_production_catalog",
        lambda _cache_root: benchmark.CatalogSnapshot(
            wiki_dir=tmp_path / "wiki",
            provenance={
                "archive_sha256": "f" * 64,
                "graph_export_id": "frozen-export",
                "graph_export_manifest_sha256": "1" * 64,
                "overlay_records": 0,
                "overlay_sha256": "2" * 64,
                "runtime_availability_sha256": "3" * 64,
            },
            prepare_seconds=30.0,
        ),
    )
    monkeypatch.setattr(
        benchmark,
        "verify_scenario_independence_attestation",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        benchmark,
        "ensure_repo_cache",
        lambda scenario, _cache_root: tmp_path / "repos" / scenario.id,
    )

    def write_manifest(**kwargs: Any) -> dict[str, Any]:
        manifest = {
            "model": kwargs["model"],
            "schedule": kwargs["schedule"],
            "scenarios_sha256": holdout.scenario_pack_sha256,
        }
        (kwargs["output"] / "environment.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    monkeypatch.setattr(benchmark, "write_environment_manifest", write_manifest)
    monkeypatch.setattr(
        benchmark,
        "write_final_repository_attestation",
        lambda *_args, **_kwargs: (
            True,
            True,
            {
                "clean": True,
                "head": "a" * 40,
                "status": [],
                "tracked_diff_sha256": "d" * 64,
            },
        ),
    )

    def run_trial(
        scenario: Any,
        *,
        arm: str,
        treatment_level: str,
        attempt: int,
        trial: int,
        retry: int,
        cache: Path,
        output: Path,
        codex: str,
        model: str,
        timeout: float,
        dry_run: bool,
        incidents: Any,
        catalog_snapshot: Any,
        forbidden_agent_reads: Any,
        official_holdout: Any,
        official_runtime: Any,
        runtime_identity_before_arm: Any,
        catalog_setup_charge_seconds: float,
    ) -> dict[str, Any]:
        assert treatment_level == arm
        assert attempt == trial
        assert retry == 0
        assert cache == tmp_path / "repos" / scenario.id
        assert output == run_root / "confirmatory"
        assert codex == sys.executable
        assert model == "gpt-5.5"
        assert timeout == 900.0
        assert dry_run is False
        assert isinstance(incidents, benchmark.IncidentLog)
        assert catalog_snapshot.prepare_seconds == 30.0
        assert forbidden_agent_reads["scenario_source"] == paths["scenario_pack"].resolve()
        assert official_holdout.protocol_sha256 == holdout.protocol_sha256
        assert official_runtime.dataset_path == tmp_path / "dataset.jsonl"
        assert official_runtime.swebench_checkout == tmp_path / "swebench"
        assert official_runtime.swebench_python == tmp_path / "swebench-python"
        assert official_runtime.docker_cli == tmp_path / "docker"
        assert official_runtime.docker_host == "unix:///private/docker.sock"
        assert runtime_identity_before_arm["codex_binary_sha256"] == holdout.codex_binary_sha256
        assert runtime_identity_before_arm["provider_config_sha256"] == (
            holdout.provider_config_sha256
        )
        assert catalog_setup_charge_seconds == (1.0 if arm == "ctx-light" else 0.0)
        calls.append((scenario.id, trial, arm))
        result = dict(result_by_key[(scenario.id, trial, arm)])
        result["runtime_identity_before_arm"] = dict(runtime_identity_before_arm)
        return result

    monkeypatch.setattr(benchmark, "run_trial", run_trial)
    verifier_inputs = {
        "swebench-dataset": tmp_path / "dataset.jsonl",
        "swebench-checkout": tmp_path / "swebench",
        "swebench-python": tmp_path / "swebench-python",
        "docker-cli": tmp_path / "docker",
    }

    exit_code = benchmark.main(
        [
            "--engine",
            benchmark.PRODUCTION_CATALOG_ENGINE,
            "--arm",
            "both",
            "--trials",
            "3",
            "--retries",
            "0",
            "--timeout",
            "900",
            "--model",
            "gpt-5.5",
            "--codex",
            sys.executable,
            "--cache-root",
            str(cache_root),
            "--output",
            str(output),
            "--holdout-protocol",
            str(paths["protocol"]),
            "--holdout-protocol-sha256",
            holdout.protocol_sha256,
            "--holdout-selection",
            str(paths["selection"]),
            "--holdout-scenario-pack",
            str(paths["scenario_pack"]),
            "--holdout-collision",
            str(paths["collision"]),
            "--holdout-reconstructed",
            str(paths["reconstructed"]),
            "--holdout-controls",
            str(paths["controls"]),
            "--holdout-environment",
            str(paths["environment"]),
            "--holdout-schedule",
            str(paths["schedule"]),
            *[
                value
                for option, path in verifier_inputs.items()
                for value in (f"--{option}", str(path))
            ],
            "--docker-host",
            "unix:///private/docker.sock",
        ]
    )

    expected_calls = [
        (str(assignment["scenario"]), int(assignment["trial"]), str(arm))
        for assignment in holdout.schedule.assignments
        for arm in assignment["arms"]
    ]
    assert exit_code == 0
    assert calls == expected_calls
    assert len(calls) == 60
    assert len(runtime_identity_checks) == 62
    assert all(identity == runtime_identity_checks[0] for identity in runtime_identity_checks)
    report = json.loads((output / "performance.json").read_text())
    assert report["assignment_complete"] is True
    assert report["experiment_valid"] is True
    assert report["product_benefit_verdict"] == "not_beneficial"
    assert list(csv.DictReader((output / "incidents.csv").open())) == []


def test_confirmatory_dual_model_failure_is_valid_honest_negative(
    tmp_path: Path,
) -> None:
    holdout, _ = _official_holdout_fixture(tmp_path)
    rows = _official_result_rows(holdout, status="failed", failure_class="model")

    report = benchmark.build_performance_report(
        rows,
        scenario_ids=[scenario.id for scenario in holdout.scenarios],
        trials=3,
        arms=("baseline", "ctx-light"),
        expected_repositories={scenario.id: scenario.repo_url for scenario in holdout.scenarios},
        frozen_schedule=holdout.schedule,
    )

    assert report["experiment_valid"] is True
    assert report["quality_preserved"] is False
    assert report["quality_complete_pair_count"] == 0
    assert report["execution_complete_pair_count"] == 30
    assert report["product_benefit_verdict"] == "not_beneficial"
    expected = {
        (scenario.id, arm, trial)
        for scenario in holdout.scenarios
        for trial in range(1, 4)
        for arm in ("baseline", "ctx-light")
    }
    assert benchmark.confirmatory_run_succeeded(
        final_keys=expected,
        expected_keys=expected,
        performance=report,
        unresolved_incidents=0,
    )


def test_confirmatory_rejects_unknown_delegated_agent_tokens(tmp_path: Path) -> None:
    holdout, _ = _official_holdout_fixture(tmp_path)
    rows = _official_result_rows(
        holdout,
        status="passed",
        failure_class=None,
        team_token_completeness="unknown",
    )

    report = benchmark.build_performance_report(
        rows,
        scenario_ids=[scenario.id for scenario in holdout.scenarios],
        trials=3,
        arms=("baseline", "ctx-light"),
        expected_repositories={scenario.id: scenario.repo_url for scenario in holdout.scenarios},
        frozen_schedule=holdout.schedule,
    )

    assert report["experiment_valid"] is False
    assert report["execution_complete_pair_count"] == 0


def test_entity_activity_claims_only_observed_lifecycle_transitions() -> None:
    catalog = {
        "candidates": [
            {"id": "skill:s", "type": "skill"},
            {"id": "agent:a", "type": "agent"},
            {"id": "mcp-server:m", "type": "mcp-server"},
        ],
        "deferred_activation": {"deferred_candidates": [{"id": "agent:a", "type": "agent"}]},
    }
    lifecycle = {
        "events": [
            {"action": "load_applied", "entity_type": "skill"},
            {"action": "used", "entity_type": "skill"},
            {"action": "unload_applied", "entity_type": "skill"},
        ]
    }

    assert benchmark.actual_entity_type_activity(
        catalog=catalog,
        selected_items=[{"id": "skill:s", "type": "skill"}],
        lifecycle=lifecycle,
    ) == {
        "recommended_entity_types": ["agent", "mcp-server", "skill"],
        "selected_entity_types": ["skill"],
        "deferred_entity_types": ["agent"],
        "loaded_entity_types": ["skill"],
        "used_entity_types": ["skill"],
        "unloaded_entity_types": ["skill"],
    }


def test_official_verifier_is_the_scored_quality_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ctx_ab_swebench

    holdout, _ = _official_holdout_fixture(tmp_path)
    scenario = holdout.scenarios[0]
    captured: dict[str, Any] = {}

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "validation": {
                "image_id": holdout.image_ids[scenario.id],
                "phase": "scored",
                "resolved": True,
            }
        }

    monkeypatch.setattr(ctx_ab_swebench, "verify_swebench", fake_verify)
    run_dir = tmp_path / "scored"
    run_dir.mkdir()
    runtime = benchmark.OfficialVerifierRuntime(
        dataset_path=tmp_path / "dataset.jsonl",
        swebench_checkout=tmp_path / "swebench",
        swebench_python=tmp_path / "python",
        docker_cli=tmp_path / "docker",
        docker_host="unix:///private/docker.sock",
    )

    result = benchmark.verify_official_model_patch(
        holdout=holdout,
        runtime=runtime,
        scenario=scenario,
        model_patch="diff --git a/src/feature.py b/src/feature.py\n",
        run_dir=run_dir,
    )

    assert result.passed is True
    assert captured["phase"] == "scored"
    assert captured["instance_id"] == scenario.id
    assert captured["model_patch"].startswith("diff --git")
    assert captured["expected_image_id"] == holdout.image_ids[scenario.id]
    assert captured["allow_image_pull"] is False
    assert "test_body" not in captured
    assert "reference_patch" not in captured
    assert (run_dir / "official-verification-evidence.json").is_file()


def test_official_development_timing_includes_ctx_catalog_but_excludes_evaluator() -> None:
    totals = benchmark.trial_timing_totals(
        ctx_setup_seconds=2.0,
        catalog_setup_seconds=4.0,
        agent_seconds=10.0,
        teardown_seconds=3.0,
        verification_seconds=40.0,
        official_evaluator=True,
    )

    assert totals == {
        "development_seconds": 19.0,
        "measured_phase_seconds": 19.0,
        "total_seconds": 59.0,
    }
    baseline = benchmark.trial_timing_totals(
        ctx_setup_seconds=0.0,
        catalog_setup_seconds=0.0,
        agent_seconds=10.0,
        teardown_seconds=0.0,
        verification_seconds=40.0,
        official_evaluator=True,
    )
    assert baseline["development_seconds"] == 10.0


def test_official_verifier_boundary_classifies_unexpected_failures_as_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ctx_ab_swebench

    holdout, _ = _official_holdout_fixture(tmp_path)
    scenario = holdout.scenarios[0]

    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise OSError("docker daemon unavailable")

    monkeypatch.setattr(ctx_ab_swebench, "verify_swebench", fail)
    run_dir = tmp_path / "failed-scored"
    run_dir.mkdir()
    result = benchmark.verify_official_model_patch(
        holdout=holdout,
        runtime=benchmark.OfficialVerifierRuntime(
            dataset_path=tmp_path / "dataset.jsonl",
            swebench_checkout=tmp_path / "swebench",
            swebench_python=tmp_path / "python",
            docker_cli=tmp_path / "docker",
            docker_host="unix:///private/docker.sock",
        ),
        scenario=scenario,
        model_patch="diff --git a/src/feature.py b/src/feature.py\n",
        run_dir=run_dir,
    )

    assert result.passed is False
    assert result.failure_class == "evaluator"
    failure = json.loads((run_dir / "official-verification-failure.json").read_text())
    assert failure["error_type"] == "OSError"
    assert failure["failure_class"] == "evaluator"
