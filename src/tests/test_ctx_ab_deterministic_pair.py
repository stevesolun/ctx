from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ctx.runtime.query_decision import QueryHostDescriptor
from scripts import ctx_ab_benchmark as benchmark

# The deterministic bridge is a loopback in front of the LiteLLM runtime, so it
# cannot be exercised without it. litellm ships in the `harness` extra, not in
# `[dev]`, and CI's unit lane installs only `[dev]`. One test guarded itself at
# line 251; the other eleven did not, and failed in CI on an absence that is not
# a defect. Guard the module once. Run with `pip install -e ".[dev,harness]"`.
pytest.importorskip("litellm")


ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "benchmarks" / "ctx_ab" / "scenarios.yaml"


def _contract(
    scenario: benchmark.Scenario,
    *,
    scenarios_path: Path = SCENARIOS,
) -> dict[str, Any]:
    return benchmark.deterministic_bridge_pair_contract(
        scenario=scenario,
        scenarios_path=scenarios_path,
        model="openai/ctx-ab-deterministic",
        timeout=30,
        max_tokens=512,
        provider_timeout=5,
        token_budget=100_000,
    )


def _local_scenario(tmp_path: Path) -> tuple[benchmark.Scenario, Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    assert benchmark.run_process(["git", "init", "-q"], cwd=source).returncode == 0
    assert (
        benchmark.run_process(
            ["git", "config", "user.email", "ctx-ab@example.invalid"],
            cwd=source,
        ).returncode
        == 0
    )
    assert (
        benchmark.run_process(
            ["git", "config", "user.name", "CTX A/B"],
            cwd=source,
        ).returncode
        == 0
    )
    (source / "feature.py").write_text("VALUE = 'before'\n", encoding="utf-8")
    assert benchmark.run_process(["git", "add", "feature.py"], cwd=source).returncode == 0
    assert benchmark.run_process(["git", "commit", "-qm", "fixture"], cwd=source).returncode == 0
    revision = benchmark.run_process(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()
    test_body = "def test_fixture():\n    assert True\n"
    scenario = benchmark.Scenario(
        id="bridge-python-tests",
        repo_url=str(source),
        commit=revision,
        task="Fix the Python tests with the smallest safe implementation.",
        query="Fix Python tests",
        language="python",
        benchmark_class="diagnostic",
        test_path="tests/test_ctx_ab_hidden.py",
        test_body=test_body,
        verify=("{python}", "-c", "raise SystemExit(1)"),
        expected_test_count=1,
        regression_verify=(),
        red_failure_contains="",
        reference_patch="",
        allowed_changes=("feature.py",),
        context=(),
    )
    scenarios_path = tmp_path / "scenarios.yaml"
    scenarios_path.write_text("schema: deterministic-test\n", encoding="utf-8")
    return scenario, source, scenarios_path


def test_deterministic_bridge_contract_binds_equal_controls_and_disallows_claims() -> None:
    scenario = benchmark.load_scenarios(SCENARIOS)[0]

    first = _contract(scenario)
    second = _contract(scenario)

    assert first == second
    assert benchmark.deterministic_bridge_pair_contract_sha256(first) == (
        benchmark.deterministic_bridge_pair_contract_sha256(second)
    )
    assert first["arms"] == ["baseline", "ctx-light"]
    assert first["execution_order"] == ["baseline", "ctx-light"]
    assert first["controls"] == {
        "max_iterations": 1,
        "timeout_seconds": 30.0,
        "provider_timeout_seconds": 5.0,
        "core_tool_schemas": [],
        "core_tool_schema_sha256": hashlib.sha256(b"[]").hexdigest(),
        "context_delta": "exact suffix: two LF bytes plus accepted prepared context",
        "ctx_run_engine_mode": "legacy",
    }
    assert first["claim_policy"] == {
        "production_efficiency_eligible": False,
        "product_claim_eligible": False,
        "benefit_verdict_allowed": False,
    }
    assert first["runtime"]["litellm_version"]
    assert len(first["runtime"]["provider_adapter_module_sha256"]) == 64
    assert len(first["runtime"]["deterministic_bridge_module_sha256"]) == 64
    assert len(first["runtime"]["query_session_module_sha256"]) == 64
    assert len(first["runtime"]["prepared_query_delivery_module_sha256"]) == 64
    assert set(first["runtime"]["release_catalog_assets"]) == {
        "benefit-eligible-catalog-v1.json",
        "release-install-skill-material-v1.json",
        "release-load-skill-material-v1.json",
        "release-query-catalog-root-v1.json",
        "reviewed-benefit-profiles-v2.json",
        "reviewed-net-benefit-policy-v1.json",
    }

    changed = json.loads(json.dumps(first))
    changed["provider"]["max_output_tokens"] = 513
    assert benchmark.deterministic_bridge_pair_contract_sha256(changed) != (
        benchmark.deterministic_bridge_pair_contract_sha256(first)
    )


def test_deterministic_bridge_rejects_unapproved_and_unfair_routes_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # noqa: E731
        AssertionError("execution started before approval")
    )
    monkeypatch.setattr(benchmark, "ensure_repo_cache", forbidden)
    monkeypatch.setattr(benchmark, "run_deterministic_bridge_pair", forbidden)
    common = [
        "--deterministic-bridge",
        "--unified-engine-treatment",
        "--engine",
        benchmark.PRODUCTION_CATALOG_ENGINE,
        "--scenario",
        "click-echo-json",
        "--model",
        "openai/ctx-ab-deterministic",
        "--arm",
        "both",
        "--retries",
        "0",
        "--max-iterations",
        "1",
        "--max-tokens",
        "512",
        "--output",
        str(tmp_path / "must-not-exist"),
    ]
    args = benchmark.build_parser().parse_args(common)
    with pytest.raises(SystemExit, match="approval required") as approval_error:
        benchmark._run_deterministic_bridge_main(args)
    expected = str(approval_error.value).rsplit(" ", 1)[-1]
    assert len(expected) == 64
    assert not (tmp_path / "must-not-exist").exists()

    stale = benchmark.build_parser().parse_args(
        [*common, "--deterministic-bridge-approval-sha256", "0" * 64]
    )
    with pytest.raises(SystemExit, match="approval required"):
        benchmark._run_deterministic_bridge_main(stale)
    assert not (tmp_path / "must-not-exist").exists()

    for extra, message in (
        (["--arm", "baseline"], "--arm both"),
        (["--retries", "1"], "--retries 0"),
        (["--base-url", "http://127.0.0.1:1/v1"], "external provider routes"),
        (["--dry-run"], "executing diagnostic"),
    ):
        unfair = benchmark.build_parser().parse_args([*common, *extra])
        with pytest.raises(SystemExit, match=message):
            benchmark._run_deterministic_bridge_main(unfair)


def test_deterministic_bridge_direct_script_resolves_sibling_bridge_before_approval() -> None:
    result = benchmark.run_process(
        [
            str(Path(sys.executable)),
            str(ROOT / "scripts" / "ctx_ab_benchmark.py"),
            "--deterministic-bridge",
            "--unified-engine-treatment",
            "--engine",
            benchmark.PRODUCTION_CATALOG_ENGINE,
            "--scenario",
            "click-echo-json",
            "--arm",
            "both",
            "--trials",
            "1",
            "--retries",
            "0",
            "--max-iterations",
            "1",
            "--model",
            "openai/ctx-ab-deterministic",
            "--max-tokens",
            "512",
            "--provider-timeout",
            "5",
            "--timeout",
            "60",
        ],
        cwd=ROOT,
        timeout=30,
    )

    assert result.returncode == 1
    assert "approval required before any workspace or provider execution" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "Traceback" not in result.stderr


def test_deterministic_bridge_top_level_import_resolves_exact_sibling() -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "scripts"), str(ROOT / "src")))
    result = benchmark.run_process(
        [
            str(Path(sys.executable)),
            "-c",
            (
                "import pathlib; import ctx_ab_benchmark as benchmark; "
                "module = benchmark._load_deterministic_bridge_module(); "
                "print(pathlib.Path(module.__file__).resolve())"
            ),
        ],
        cwd=ROOT,
        env=environment,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (
        Path(result.stdout.strip())
        == (ROOT / "scripts" / "ctx_ab_deterministic_bridge.py").resolve()
    )


def test_deterministic_bridge_pair_uses_current_delivery_and_exact_provider_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("litellm")
    scenario, cache, scenarios_path = _local_scenario(tmp_path)
    contract = _contract(scenario, scenarios_path=scenarios_path)
    approval = benchmark.deterministic_bridge_pair_contract_sha256(contract)
    verification_roots: list[Path] = []
    release_factory_calls = 0
    acceptance_calls = 0

    from ctx.runtime import prepared_query_delivery, query_session

    real_factory = query_session.prepare_query_delivery
    real_accept = prepared_query_delivery.accept_prepared_query_delivery

    def counted_factory(**kwargs: Any) -> Any:
        nonlocal release_factory_calls
        release_factory_calls += 1
        host = kwargs["host"]
        assert isinstance(host, QueryHostDescriptor)
        assert host.host_context_id == "ctx-run"
        assert host.execution_intent == "activate"
        return real_factory(**kwargs)

    def counted_accept(value: Any, *, host: Any) -> Any:
        nonlocal acceptance_calls
        acceptance_calls += 1
        return real_accept(value, host=host)

    monkeypatch.setattr(query_session, "prepare_query_delivery", counted_factory)
    monkeypatch.setattr(
        prepared_query_delivery,
        "accept_prepared_query_delivery",
        counted_accept,
    )
    monkeypatch.setattr(
        benchmark,
        "prepare_engine_treatment",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("obsolete benchmark treatment was called")
        ),
    )

    def fake_verify(
        _scenario: benchmark.Scenario,
        workspace: Path,
        test_hash: str,
    ) -> benchmark.CommandResult:
        assert test_hash == hashlib.sha256(scenario.test_body.encode()).hexdigest()
        verification_roots.append(workspace)
        return benchmark.CommandResult(1, "same evaluator failure\n", "", 0.01)

    monkeypatch.setattr(benchmark, "verify_workspace", fake_verify)
    output = tmp_path / "output"
    output.mkdir()

    report = benchmark.run_deterministic_bridge_pair(
        scenario,
        cache=cache,
        scenarios_path=scenarios_path,
        output=output,
        model="openai/ctx-ab-deterministic",
        timeout=30,
        max_tokens=512,
        provider_timeout=5,
        token_budget=100_000,
        approval_digest=approval,
        contract=contract,
    )

    assert report["evidence_level"] == "deterministic_bridge_demo"
    assert report["fairness_contract_verified"] is True
    assert report["path_roots_distinct_verified"] is True
    assert report["final_workspace_identity_verified"] is True
    assert report["model_tool_surface_empty_verified"] is True
    assert report["os_process_isolation_verified"] is False
    assert report["sibling_filesystem_unreadability_verified"] is False
    assert report["serial_execution_verified"] is True
    assert report["request_delta"]["only_semantic_delta_verified"] is True
    assert report["request_delta"]["parsed_canonical_semantic_delta_verified"] is True
    assert report["request_delta"]["raw_http_body_delta_verified"] is True
    assert report["engine_delivery"]["final_journal_revision"] == 3
    assert report["engine_delivery"]["host_invocation_digest"] == approval
    assert len(report["engine_delivery"]["release_root_digest"]) == 64
    assert report["engine_delivery"]["release_sequence"] >= 1
    assert report["engine_delivery"]["catalog_mode"] == "reviewed"
    assert len(report["engine_delivery"]["catalog_snapshot_digest"]) == 64
    assert len(report["engine_delivery"]["plan_digest"]) == 64
    assert len(report["engine_delivery"]["presentation_digest"]) == 64
    assert report["engine_delivery"]["capabilities"][0]["capability_id"] == (
        "skill:ctx-python-testing"
    )
    assert report["production_efficiency_eligible"] is False
    assert report["product_claim_eligible"] is False
    assert report["benefit_verdict_allowed"] is False
    assert report["benefit_verdict"] is None
    assert report["ctx_setup_seconds"] >= 0
    assert report["timing_evidence_level"] == "diagnostic_wall_clock_non_production"
    assert [arm["arm"] for arm in report["arms"]] == ["baseline", "ctx-light"]
    assert report["arms"][0]["ctx_setup_seconds"] == 0.0
    assert report["arms"][1]["ctx_setup_seconds"] == report["ctx_setup_seconds"]
    assert all(arm["agent_seconds"] >= 0 for arm in report["arms"])
    assert all(arm["evaluator_seconds"] >= 0 for arm in report["arms"])
    assert report["normalized_environment_contract_verified"] is True
    assert (
        report["arms"][0]["normalized_environment_sha256"]
        == report["arms"][1]["normalized_environment_sha256"]
    )
    assert (
        report["arms"][0]["normalized_environment_sha256"]
        == report["normalized_environment_sha256"]
    )
    assert report["arms"][0]["task_only_sha256"] == report["arms"][1]["task_only_sha256"]
    assert report["arms"][0]["workspace_identity"] == report["arms"][1]["workspace_identity"]
    assert (
        report["arms"][0]["command_contract_sha256"] == report["arms"][1]["command_contract_sha256"]
    )
    assert report["arms"][0]["evaluator_returncode"] == report["arms"][1]["evaluator_returncode"]
    assert len(set(verification_roots)) == 2
    assert release_factory_calls == 1
    assert acceptance_calls == 1
    assert (output / "deterministic-bridge-report.json").is_file()


def test_deterministic_bridge_direct_api_rejects_stale_approval_without_mutation(
    tmp_path: Path,
) -> None:
    scenario, cache, scenarios_path = _local_scenario(tmp_path)
    contract = _contract(scenario, scenarios_path=scenarios_path)
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ValueError, match="approval digest"):
        benchmark.run_deterministic_bridge_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            model="openai/ctx-ab-deterministic",
            timeout=30,
            max_tokens=512,
            provider_timeout=5,
            token_budget=100_000,
            approval_digest="0" * 64,
            contract=contract,
        )
    assert list(output.iterdir()) == []

    approval = benchmark.deterministic_bridge_pair_contract_sha256(contract)
    stale_contract = json.loads(json.dumps(contract))
    stale_contract["provider"]["max_output_tokens"] = 513
    with pytest.raises(ValueError, match="actual execution"):
        benchmark.run_deterministic_bridge_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            model="openai/ctx-ab-deterministic",
            timeout=30,
            max_tokens=512,
            provider_timeout=5,
            token_budget=100_000,
            approval_digest=approval,
            contract=stale_contract,
        )
    assert list(output.iterdir()) == []

    runtime_tamper = json.loads(json.dumps(contract))
    runtime_tamper["runtime"]["query_session_module_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="actual execution"):
        benchmark.run_deterministic_bridge_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            model="openai/ctx-ab-deterministic",
            timeout=30,
            max_tokens=512,
            provider_timeout=5,
            token_budget=100_000,
            approval_digest=approval,
            contract=runtime_tamper,
        )
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "openai/changed-wire-model"),
        ("timeout", 31),
        ("max_tokens", 513),
        ("provider_timeout", 6),
        ("token_budget", 100_001),
    ],
)
def test_deterministic_bridge_actual_runtime_drift_rejects_approved_contract(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    scenario, cache, scenarios_path = _local_scenario(tmp_path)
    contract = _contract(scenario, scenarios_path=scenarios_path)
    approval = benchmark.deterministic_bridge_pair_contract_sha256(contract)
    output = tmp_path / "output"
    arguments: dict[str, object] = {
        "cache": cache,
        "scenarios_path": scenarios_path,
        "output": output,
        "model": "openai/ctx-ab-deterministic",
        "timeout": 30,
        "max_tokens": 512,
        "provider_timeout": 5,
        "token_budget": 100_000,
        "approval_digest": approval,
        "contract": contract,
    }
    arguments[field] = value
    output.mkdir()

    with pytest.raises(ValueError, match="actual execution"):
        benchmark.run_deterministic_bridge_pair(scenario, **arguments)  # type: ignore[arg-type]
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "foreign-scenario"),
        ("repo_url", "https://example.invalid/foreign.git"),
        ("commit", "0" * 40),
        ("language", "javascript"),
        ("task", "A different task"),
        ("test_body", "def test_changed():\n    assert False\n"),
    ],
)
def test_deterministic_bridge_actual_scenario_drift_rejects_approved_contract(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    scenario, cache, scenarios_path = _local_scenario(tmp_path)
    contract = _contract(scenario, scenarios_path=scenarios_path)
    approval = benchmark.deterministic_bridge_pair_contract_sha256(contract)
    if field == "id":
        changed = replace(scenario, id=value)
    elif field == "repo_url":
        changed = replace(scenario, repo_url=value)
    elif field == "commit":
        changed = replace(scenario, commit=value)
    elif field == "language":
        changed = replace(scenario, language=value)
    elif field == "task":
        changed = replace(scenario, task=value)
    elif field == "test_body":
        changed = replace(scenario, test_body=value)
    else:
        raise AssertionError(f"unexpected scenario field: {field}")
    output = tmp_path / "output"
    output.mkdir()

    with pytest.raises(ValueError, match="actual execution"):
        benchmark.run_deterministic_bridge_pair(
            changed,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            model="openai/ctx-ab-deterministic",
            timeout=30,
            max_tokens=512,
            provider_timeout=5,
            token_budget=100_000,
            approval_digest=approval,
            contract=contract,
        )
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "0" * 40),
        ("evaluator_test_sha256", "0" * 64),
    ],
)
def test_deterministic_bridge_rejects_workspace_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    scenario, cache, scenarios_path = _local_scenario(tmp_path)
    contract = _contract(scenario, scenarios_path=scenarios_path)
    approval = benchmark.deterministic_bridge_pair_contract_sha256(contract)
    real_identity = benchmark._deterministic_workspace_identity

    def drifted_identity(
        current: benchmark.Scenario,
        *,
        workspace: Path,
    ) -> dict[str, str]:
        identity = real_identity(current, workspace=workspace)
        identity[field] = value
        return identity

    monkeypatch.setattr(benchmark, "_deterministic_workspace_identity", drifted_identity)
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(RuntimeError, match="approved commit or evaluator"):
        benchmark.run_deterministic_bridge_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            model="openai/ctx-ab-deterministic",
            timeout=30,
            max_tokens=512,
            provider_timeout=5,
            token_budget=100_000,
            approval_digest=approval,
            contract=contract,
        )


def test_deterministic_request_delta_rejects_any_non_context_change() -> None:
    baseline = {
        "model": "openai/ctx-ab-deterministic",
        "max_tokens": 512,
        "messages": [
            {"role": "system", "content": "same"},
            {"role": "user", "content": "task"},
        ],
    }
    treatment = json.loads(json.dumps(baseline))
    treatment["messages"][-1]["content"] = "task\n\ncontext"
    assert (
        benchmark._assert_deterministic_request_delta(
            baseline_payload=baseline,
            treatment_payload=treatment,
            task="task",
            context="context",
        )["only_semantic_delta_verified"]
        is True
    )

    treatment["max_tokens"] = 513
    with pytest.raises(ValueError, match="outside the CTX suffix"):
        benchmark._assert_deterministic_request_delta(
            baseline_payload=baseline,
            treatment_payload=treatment,
            task="task",
            context="context",
        )


def test_deterministic_raw_request_delta_replaces_one_exact_json_literal() -> None:
    baseline_payload = {
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "task"}],
        "model": "wire-model",
    }
    treatment_payload = json.loads(json.dumps(baseline_payload))
    treatment_payload["messages"][0]["content"] = "task\n\ncontext"
    baseline_body = json.dumps(
        baseline_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    treatment_body = json.dumps(
        treatment_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    result = benchmark._assert_deterministic_raw_request_delta(
        baseline_body=baseline_body,
        treatment_body=treatment_body,
        baseline_prompt="task",
        treatment_prompt="task\n\ncontext",
    )

    assert result["raw_http_body_delta_verified"] is True
    assert len(result["normalized_raw_http_body_sha256"]) == 64

    duplicate = json.dumps(
        {**baseline_payload, "duplicate": "task"},
        separators=(",", ":"),
    ).encode()
    with pytest.raises(ValueError, match="one exact delivered-prompt literal"):
        benchmark._normalize_delivered_prompt_http_body(
            duplicate,
            delivered_prompt="task",
        )
    with pytest.raises(ValueError, match="one exact delivered-prompt literal"):
        benchmark._normalize_delivered_prompt_http_body(
            baseline_body,
            delivered_prompt="missing",
        )


@pytest.mark.parametrize("mutation", ["whitespace", "field"])
def test_deterministic_raw_request_delta_rejects_non_prompt_bytes(mutation: str) -> None:
    baseline_payload = {
        "messages": [{"role": "user", "content": "task"}],
        "model": "wire-model",
    }
    treatment_payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "task\n\ncontext"}],
        "model": "wire-model",
    }
    baseline_body = json.dumps(baseline_payload, separators=(",", ":")).encode()
    if mutation == "whitespace":
        treatment_body = json.dumps(treatment_payload).encode()
    else:
        treatment_payload["temperature"] = 0.7
        treatment_body = json.dumps(treatment_payload, separators=(",", ":")).encode()

    with pytest.raises(ValueError, match="differ outside the delivered prompt"):
        benchmark._assert_deterministic_raw_request_delta(
            baseline_body=baseline_body,
            treatment_body=treatment_body,
            baseline_prompt="task",
            treatment_prompt="task\n\ncontext",
        )
