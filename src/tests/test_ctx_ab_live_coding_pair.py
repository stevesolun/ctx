from __future__ import annotations

import argparse
import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from scripts import ctx_ab_benchmark as benchmark


ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "benchmarks" / "ctx_ab" / "scenarios.yaml"
MODEL = "openai/gpt-5.5"
API_KEY_ENV = "CTX_AB_TEST_PROVIDER_KEY"


def _contract(
    scenario: benchmark.Scenario,
    *,
    output: Path,
    cache_root: Path,
    scenarios_path: Path = SCENARIOS,
    max_tokens: int = 2_048,
    execute: bool = False,
) -> dict[str, Any]:
    return benchmark.live_coding_pair_contract(
        scenario=scenario,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        model=MODEL,
        api_key_env=API_KEY_ENV,
        timeout=900,
        max_iterations=1,
        max_tokens=max_tokens,
        provider_timeout=120,
        execute=execute,
    )


def _argv(*, output: Path, cache_root: Path) -> list[str]:
    scenario = benchmark.load_scenarios(SCENARIOS)[0]
    return [
        "--live-coding-pair",
        "--unified-engine-treatment",
        "--engine",
        benchmark.PRODUCTION_CATALOG_ENGINE,
        "--arm",
        "both",
        "--retries",
        "0",
        "--trials",
        "1",
        "--scenario",
        scenario.id,
        "--scenarios",
        str(SCENARIOS),
        "--model",
        MODEL,
        "--api-key-env",
        API_KEY_ENV,
        "--max-iterations",
        "1",
        "--max-tokens",
        "2048",
        "--provider-timeout",
        "120",
        "--timeout",
        "900",
        "--cache-root",
        str(cache_root),
        "--output",
        str(output),
    ]


def _args(*, output: Path, cache_root: Path) -> argparse.Namespace:
    return benchmark.build_parser().parse_args(_argv(output=output, cache_root=cache_root))


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
    test_body = "from feature import VALUE\n\n\ndef test_value():\n    assert VALUE == 'after'\n"
    reference_patch = (
        "diff --git a/feature.py b/feature.py\n"
        "--- a/feature.py\n"
        "+++ b/feature.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 'before'\n"
        "+VALUE = 'after'\n"
    )
    scenario = benchmark.Scenario(
        id="live-python-tests",
        repo_url="https://github.com/example/ctx-live-pair-fixture.git",
        commit=revision,
        task=(
            "Fix the failing Python pytest test using the smallest safe change and run the "
            "focused Python tests."
        ),
        query="fix failing Python pytest tests",
        language="python",
        benchmark_class="diagnostic",
        test_path="tests/test_ctx_ab_hidden.py",
        test_body=test_body,
        verify=("{python}", "-m", "pytest", "-q", "tests/test_ctx_ab_hidden.py"),
        expected_test_count=1,
        regression_verify=(),
        red_failure_contains="AssertionError",
        reference_patch=reference_patch,
        allowed_changes=("feature.py",),
        context=(),
    )
    scenarios_path = tmp_path / "scenarios.yaml"
    scenarios_path.write_text("schema: live-pair-test\n", encoding="utf-8")
    return scenario, source, scenarios_path


def _fake_provider_executor(
    captured: list[dict[str, Any]],
    *,
    fail_arm: str | None = None,
    tokens_reported: bool = True,
    treatment_provider_identity: str | None = None,
    treatment_provider_adapter: str | None = None,
    baseline_response_count: int = 1,
) -> Any:
    def execute(
        *,
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        containment_token: str,
    ) -> benchmark.CommandResult:
        del cwd, timeout, containment_token
        arm = "baseline" if not captured else "ctx-light"
        if fail_arm == arm:
            raise RuntimeError(f"synthetic {arm} provider crash")
        prompt = command[command.index("--task") + 1]
        session_id = command[command.index("--session-id") + 1]
        sessions_dir = Path(command[command.index("--sessions-dir") + 1])
        model = command[command.index("--model") + 1]
        sessions_dir.mkdir(parents=True, exist_ok=True)
        provider_identity = (
            treatment_provider_identity
            if arm == "ctx-light" and treatment_provider_identity is not None
            else model.split("/", 1)[0]
        )
        provider_adapter = (
            treatment_provider_adapter
            if arm == "ctx-light" and treatment_provider_adapter is not None
            else model.split("/", 1)[0]
        )
        response_count = baseline_response_count if arm == "baseline" else 1
        events = [
            {
                "type": "session_start",
                "session_id": session_id,
                "provider": provider_identity,
                "model": model,
                "base_url": "",
                "api_key_env": API_KEY_ENV,
                "ctx_tool_names": [],
            },
            *[
                {
                    "type": "model_response",
                    "session_id": session_id,
                    "provider": provider_adapter,
                    "model": model,
                    "response_model": model,
                    "finish_reason": "stop",
                    "authentication_submitted": bool(environment.get(API_KEY_ENV)),
                    "request_endpoint_hash": None,
                }
                for _ in range(response_count)
            ],
        ]
        (sessions_dir / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        input_tokens = 100 if arm == "baseline" else 140
        payload = {
            "session_id": session_id,
            "stop_reason": "completed",
            "final_message": json.dumps(
                {
                    "patch": (
                        "diff --git a/feature.py b/feature.py\n"
                        "--- a/feature.py\n"
                        "+++ b/feature.py\n"
                        "@@ -1 +1 @@\n"
                        "-VALUE = 'before'\n"
                        "+VALUE = 'after'\n"
                    )
                }
            ),
            "usage": {
                "tokens_reported": tokens_reported,
                "input_tokens": input_tokens,
                "cached_input_tokens": 0,
                "output_tokens": 20,
                "total_tokens": input_tokens + 20,
            },
        }
        captured.append(
            {
                "arm": arm,
                "command": list(command),
                "environment": dict(environment),
                "prompt": prompt,
            }
        )
        return benchmark.CommandResult(
            0,
            json.dumps(payload),
            f"provider diagnostic accidentally echoed {environment[API_KEY_ENV]}",
            0.2,
        )

    return execute


def _mirror_cache(
    tmp_path: Path,
    scenario: benchmark.Scenario,
    source: Path,
) -> tuple[Path, Path]:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    cache = cache_root / scenario.id
    result = benchmark.run_process(
        ["git", "clone", "--mirror", str(source), str(cache)],
        cwd=cache_root,
    )
    assert result.returncode == 0, result.stderr
    return cache_root, cache


def _passing_verification(
    scenario: benchmark.Scenario,
    workspace: Path,
    test_hash: str,
) -> benchmark.CommandResult:
    """Stand in for the codex-sandboxed evaluator boundary.

    ``verify_workspace`` runs the scenario's focused tests inside the
    Codex-managed macOS sandbox (``_run_verified``), which is unavailable in the
    unit-test environment.  Like the sibling deterministic-bridge and production
    A/B suites, the pair tests replace this external boundary with a deterministic
    double so the runner's serial-pairing, provider-provenance, delivery
    re-attestation, and secret-redaction guarantees are exercised hermetically.
    The evaluator-hash assertion keeps the double honest about the input the
    runner hands to verification.
    """

    assert test_hash == hashlib.sha256(scenario.test_body.encode("utf-8")).hexdigest()
    return benchmark.CommandResult(0, "1 passed\n", "", 0.0)


def test_live_coding_pair_contract_binds_the_only_treatment_delta(tmp_path: Path) -> None:
    scenario = benchmark.load_scenarios(SCENARIOS)[0]

    contract = _contract(
        scenario,
        output=tmp_path / "pair-output",
        cache_root=tmp_path / "cache",
    )

    assert contract["schema"] == "ctx.live-coding-pair-plan-v1"
    assert contract["arms"] == ["baseline", "ctx-light"]
    assert contract["execution_order"] == ["baseline", "ctx-light"]
    assert contract["execution"] == {
        "enabled": False,
        "mode": "approved_contract_only",
        "provider_calls_allowed": False,
        "workspace_mutation_allowed": False,
    }
    assert contract["controls"]["max_iterations"] == 1
    assert contract["controls"]["max_output_tokens_per_arm"] == 2_048
    assert contract["controls"]["model_tool_schemas"] == []
    assert contract["controls"]["arm_equality"] == [
        "repository",
        "commit",
        "base_prompt",
        "model",
        "model_tools",
        "max_iterations",
        "max_output_tokens",
        "wall_timeout",
        "provider_timeout",
        "evaluator",
        "runtime",
        "environment_except_isolated_paths",
    ]
    assert contract["treatment_delta"]["baseline"] == "base prompt only"
    assert contract["treatment_delta"]["ctx-light"] == (
        "base prompt plus two LF bytes plus one accepted current-release CTX delivery"
    )
    assert contract["treatment_delta"]["accepted_nonempty_delivery_required"] is True
    assert contract["treatment_delta"]["accepted_delivery_identity_preapproved"] is False
    assert contract["treatment_delta"]["required_delivery_evidence_fields"] == [
        "host_context_id",
        "host_descriptor_digest",
        "host_invocation_digest",
        "decision_receipt_digest",
        "release_root_digest",
        "release_sequence",
        "catalog_mode",
        "catalog_snapshot_digest",
        "plan_digest",
        "presentation_digest",
        "delivery_digest",
        "receipt_event_content_digest",
        "final_journal_revision",
        "final_journal_record_digest",
        "context_sha256",
        "context_bytes",
        "capabilities",
    ]
    assert contract["treatment_delta"]["host_invocation_digest_must_equal_approval"] is True
    assert contract["treatment_delta"]["policy_abstention_executes_pair"] is False
    assert contract["treatment_delta"]["all_other_differences_forbidden"] is True
    assert contract["runtime"]["release_catalog_open_verified"] is True
    assert contract["runtime"]["opened_release_identity"]["release_root_digest"]
    assert contract["runtime"]["opened_release_identity"]["release_sequence"] > 0
    assert set(contract["runtime"]["release_catalog_assets"]) == {
        "benefit-eligible-catalog-v1.json",
        "release-install-skill-material-v1.json",
        "release-load-skill-material-v1.json",
        "release-query-catalog-root-v1.json",
        "reviewed-benefit-profiles-v2.json",
        "reviewed-net-benefit-policy-v1.json",
    }
    assert contract["paired_evidence"]["both_arms_required"] is True
    assert contract["paired_evidence"]["partial_pair_publishable"] is False
    assert (
        contract["paired_evidence"]["accepted_delivery_re_attestation_required_before_provider"]
        is True
    )
    assert contract["scope_limitations"]["minimum_product_claim_scenarios"] == 6
    assert contract["scope_limitations"]["minimum_product_claim_repositories"] == 5
    assert contract["scope_limitations"]["minimum_product_claim_trials"] == 6
    assert contract["scope_limitations"]["provider_sampling_seed_controlled"] is False
    assert contract["scope_limitations"]["execution_order_counterbalanced"] is False
    assert contract["scope_limitations"]["causal_or_general_benefit_inference_allowed"] is False
    assert contract["claim_policy"] == {
        "production_efficiency_eligible": False,
        "product_claim_eligible": False,
        "benefit_verdict_allowed": False,
    }


def test_live_coding_pair_approval_binds_actual_runtime_arguments(tmp_path: Path) -> None:
    scenario = benchmark.load_scenarios(SCENARIOS)[0]
    output = tmp_path / "pair-output"
    cache_root = tmp_path / "cache"
    contract = _contract(scenario, output=output, cache_root=cache_root)
    approval = benchmark.live_coding_pair_contract_sha256(contract)

    with pytest.raises(ValueError, match="actual execution"):
        benchmark.approve_live_coding_pair_plan(
            scenario=scenario,
            scenarios_path=SCENARIOS,
            output=output,
            cache_root=cache_root,
            model=MODEL,
            api_key_env=API_KEY_ENV,
            timeout=900,
            max_iterations=1,
            max_tokens=2_049,
            provider_timeout=120,
            approval_digest=approval,
            contract=contract,
        )
    assert not output.exists()
    assert not cache_root.exists()


def test_live_coding_pair_contract_fails_closed_when_release_catalog_cannot_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.runtime import production_catalog

    scenario = benchmark.load_scenarios(SCENARIOS)[0]

    def reject_catalog() -> object:
        raise production_catalog.ReleaseCatalogError("catalog authentication failed")

    monkeypatch.setattr(production_catalog, "open_release_pinned_query_catalog", reject_catalog)

    with pytest.raises(production_catalog.ReleaseCatalogError, match="authentication failed"):
        _contract(
            scenario,
            output=tmp_path / "pair-output",
            cache_root=tmp_path / "cache",
        )
    assert not (tmp_path / "pair-output").exists()
    assert not (tmp_path / "cache").exists()


def test_live_coding_pair_cli_requires_approval_before_mutation_or_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "pair-output"
    cache_root = tmp_path / "cache"
    args = _args(output=output, cache_root=cache_root)
    forbidden = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # noqa: E731
        AssertionError("mutation or provider access occurred before approval")
    )
    monkeypatch.setattr(benchmark, "ensure_repo_cache", forbidden)
    monkeypatch.setattr(benchmark, "prepare_workspace", forbidden)
    monkeypatch.setattr(benchmark, "run_process", forbidden)

    with pytest.raises(SystemExit, match="approval required before any workspace or provider"):
        benchmark.main(_argv(output=output, cache_root=cache_root))

    stale = argparse.Namespace(**vars(args))
    stale.live_coding_pair_approval_sha256 = "0" * 64
    with pytest.raises(SystemExit, match="approval required before any workspace or provider"):
        benchmark._run_live_coding_pair_main(stale)
    assert not output.exists()
    assert not cache_root.exists()


def test_live_coding_pair_cli_approves_plan_without_executing_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "pair-output"
    cache_root = tmp_path / "cache"
    scenario = benchmark.load_scenarios(SCENARIOS)[0]
    contract = _contract(scenario, output=output, cache_root=cache_root)
    approval = benchmark.live_coding_pair_contract_sha256(contract)
    forbidden = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # noqa: E731
        AssertionError("contract-only live pair attempted execution")
    )
    monkeypatch.setattr(benchmark, "ensure_repo_cache", forbidden)
    monkeypatch.setattr(benchmark, "prepare_workspace", forbidden)
    monkeypatch.setattr(benchmark, "run_process", forbidden)

    argv = _argv(output=output, cache_root=cache_root)
    argv.extend(["--live-coding-pair-approval-sha256", approval])
    assert benchmark.main(argv) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "ctx.live-coding-pair-approval-v1"
    assert report["approval_digest"] == approval
    assert report["execution_enabled"] is False
    assert report["provider_calls_made"] == 0
    assert report["workspace_mutations_made"] == 0
    assert report["production_efficiency_eligible"] is False
    assert report["product_claim_eligible"] is False
    assert report["benefit_verdict_allowed"] is False
    assert report["benefit_verdict"] is None
    assert not output.exists()
    assert not cache_root.exists()


def test_live_coding_pair_executes_faked_provider_as_exact_serial_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, source, scenarios_path = _local_scenario(tmp_path)
    cache_root, cache = _mirror_cache(tmp_path, scenario, source)
    output = tmp_path / "output"
    contract = _contract(
        scenario,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        execute=True,
    )
    approval = benchmark.live_coding_pair_contract_sha256(contract)
    captured: list[dict[str, Any]] = []
    monkeypatch.setenv(API_KEY_ENV, "provider-secret-not-for-artifacts")
    monkeypatch.setattr(
        benchmark,
        "_execute_live_coding_provider_arm",
        _fake_provider_executor(captured),
    )
    monkeypatch.setattr(benchmark, "verify_workspace", _passing_verification)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("live pair attempted network outside the fake provider boundary")
        ),
    )

    report = benchmark.run_live_coding_pair(
        scenario,
        cache=cache,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        model=MODEL,
        api_key_env=API_KEY_ENV,
        timeout=900,
        max_iterations=1,
        max_tokens=2_048,
        provider_timeout=120,
        approval_digest=approval,
        contract=contract,
    )

    assert [row["arm"] for row in captured] == ["baseline", "ctx-light"]
    baseline_prompt = captured[0]["prompt"]
    treatment_prompt = captured[1]["prompt"]
    assert treatment_prompt.startswith(baseline_prompt + "\n\n")
    delivered_context = treatment_prompt.removeprefix(baseline_prompt + "\n\n")
    assert delivered_context
    assert (
        hashlib.sha256(delivered_context.encode()).hexdigest()
        == report["engine_delivery"]["context_sha256"]
    )
    for row in captured:
        assert "--no-ctx-tools" in row["command"]
        assert "--ctx-tool-surface" not in row["command"]
        assert row["environment"][API_KEY_ENV] == "provider-secret-not-for-artifacts"
    assert report["schema"] == "ctx.live-coding-pair-report-v1"
    assert report["evidence_level"] == "diagnostic_single_live_pair"
    assert report["paired_evidence_complete"] is True
    assert report["serial_execution_verified"] is True
    assert report["fairness_contract_verified"] is True
    assert [row["arm"] for row in report["arms"]] == ["baseline", "ctx-light"]
    assert all(row["verification_passed"] is True for row in report["arms"])
    assert all(row["token_attribution"] == "exact" for row in report["arms"])
    assert all(row["patch_paths"] == ["feature.py"] for row in report["arms"])
    assert report["production_efficiency_eligible"] is False
    assert report["product_claim_eligible"] is False
    assert report["benefit_verdict_allowed"] is False
    assert report["benefit_verdict"] is None
    assert "provider-secret-not-for-artifacts" not in json.dumps(report)
    for arm in ("baseline", "ctx-light"):
        stderr_path = output / scenario.id / "live-coding-pair" / arm / "ctx-run.stderr.log"
        assert "provider-secret-not-for-artifacts" not in stderr_path.read_text()
    assert (output / "live-coding-pair-report.json").is_file()


@pytest.mark.parametrize(
    ("drift", "expected_provider_arms"),
    [
        ("identity", ["baseline", "ctx-light"]),
        ("adapter", ["baseline", "ctx-light"]),
        ("multiple-responses", ["baseline"]),
    ],
)
def test_live_coding_pair_rejects_unfair_provider_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    expected_provider_arms: list[str],
) -> None:
    scenario, source, scenarios_path = _local_scenario(tmp_path)
    cache_root, cache = _mirror_cache(tmp_path, scenario, source)
    output = tmp_path / "output"
    contract = _contract(
        scenario,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        execute=True,
    )
    approval = benchmark.live_coding_pair_contract_sha256(contract)
    captured: list[dict[str, Any]] = []
    monkeypatch.setenv(API_KEY_ENV, "provider-secret-not-for-artifacts")
    monkeypatch.setattr(
        benchmark,
        "_execute_live_coding_provider_arm",
        _fake_provider_executor(
            captured,
            treatment_provider_identity="different-provider" if drift == "identity" else None,
            treatment_provider_adapter="different-adapter" if drift == "adapter" else None,
            baseline_response_count=2 if drift == "multiple-responses" else 1,
        ),
    )
    monkeypatch.setattr(benchmark, "verify_workspace", _passing_verification)

    with pytest.raises(RuntimeError, match="provider"):
        benchmark.run_live_coding_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            cache_root=cache_root,
            model=MODEL,
            api_key_env=API_KEY_ENV,
            timeout=900,
            max_iterations=1,
            max_tokens=2_048,
            provider_timeout=120,
            approval_digest=approval,
            contract=contract,
        )

    assert [row["arm"] for row in captured] == expected_provider_arms
    assert not (output / "live-coding-pair-report.json").exists()


@pytest.mark.parametrize("failure", ["provider-crash", "usage-unverifiable"])
def test_live_coding_pair_rejects_partial_or_unverifiable_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    scenario, source, scenarios_path = _local_scenario(tmp_path)
    cache_root, cache = _mirror_cache(tmp_path, scenario, source)
    output = tmp_path / "output"
    contract = _contract(
        scenario,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        execute=True,
    )
    approval = benchmark.live_coding_pair_contract_sha256(contract)
    captured: list[dict[str, Any]] = []
    monkeypatch.setenv(API_KEY_ENV, "provider-secret-not-for-artifacts")
    monkeypatch.setattr(
        benchmark,
        "_execute_live_coding_provider_arm",
        _fake_provider_executor(
            captured,
            fail_arm="ctx-light" if failure == "provider-crash" else None,
            tokens_reported=failure != "usage-unverifiable",
        ),
    )
    monkeypatch.setattr(benchmark, "verify_workspace", _passing_verification)

    with pytest.raises(RuntimeError, match="live coding pair"):
        benchmark.run_live_coding_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            cache_root=cache_root,
            model=MODEL,
            api_key_env=API_KEY_ENV,
            timeout=900,
            max_iterations=1,
            max_tokens=2_048,
            provider_timeout=120,
            approval_digest=approval,
            contract=contract,
        )

    expected_completed = ["baseline"] if failure == "provider-crash" else []
    failure_path = output / scenario.id / "live-coding-pair" / "live-coding-pair-failure.json"
    failure_report = json.loads(failure_path.read_text(encoding="utf-8"))
    assert failure_report["completed_arms"] == expected_completed
    assert failure_report["paired_evidence_complete"] is False
    assert failure_report["product_claim_eligible"] is False
    assert not (output / "live-coding-pair-report.json").exists()


def test_live_coding_pair_rejects_changed_delivery_after_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, source, scenarios_path = _local_scenario(tmp_path)
    cache_root, cache = _mirror_cache(tmp_path, scenario, source)
    output = tmp_path / "output"
    contract = _contract(
        scenario,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        execute=True,
    )
    approval = benchmark.live_coding_pair_contract_sha256(contract)
    captured: list[dict[str, Any]] = []
    monkeypatch.setenv(API_KEY_ENV, "provider-secret-not-for-artifacts")
    monkeypatch.setattr(
        benchmark,
        "_execute_live_coding_provider_arm",
        _fake_provider_executor(captured),
    )
    monkeypatch.setattr(benchmark, "verify_workspace", _passing_verification)
    real_identity = benchmark._live_coding_delivery_identity
    identity_calls = 0

    def drifting_identity(value: object) -> dict[str, Any]:
        nonlocal identity_calls
        identity_calls += 1
        identity = real_identity(value)
        if identity_calls >= 3:
            identity["context_sha256"] = "0" * 64
        return identity

    monkeypatch.setattr(benchmark, "_live_coding_delivery_identity", drifting_identity)

    with pytest.raises(RuntimeError, match="delivery identity changed"):
        benchmark.run_live_coding_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            cache_root=cache_root,
            model=MODEL,
            api_key_env=API_KEY_ENV,
            timeout=900,
            max_iterations=1,
            max_tokens=2_048,
            provider_timeout=120,
            approval_digest=approval,
            contract=contract,
        )

    assert [row["arm"] for row in captured] == ["baseline"]
    failure_path = output / scenario.id / "live-coding-pair" / "live-coding-pair-failure.json"
    assert json.loads(failure_path.read_text())["completed_arms"] == ["baseline"]


def test_live_coding_pair_rejects_policy_abstention_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.runtime import query_session
    from ctx.runtime.query_decision import QueryDecisionFailure

    scenario, source, scenarios_path = _local_scenario(tmp_path)
    cache_root, cache = _mirror_cache(tmp_path, scenario, source)
    output = tmp_path / "output"
    contract = _contract(
        scenario,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        execute=True,
    )
    approval = benchmark.live_coding_pair_contract_sha256(contract)
    captured: list[dict[str, Any]] = []
    monkeypatch.setenv(API_KEY_ENV, "provider-secret-not-for-artifacts")
    monkeypatch.setattr(
        benchmark,
        "_execute_live_coding_provider_arm",
        _fake_provider_executor(captured),
    )
    monkeypatch.setattr(
        query_session,
        "prepare_query_delivery",
        lambda **_kwargs: QueryDecisionFailure(failure_code="no-eligible-candidates"),
    )

    with pytest.raises(RuntimeError, match="abstained or failed"):
        benchmark.run_live_coding_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            cache_root=cache_root,
            model=MODEL,
            api_key_env=API_KEY_ENV,
            timeout=900,
            max_iterations=1,
            max_tokens=2_048,
            provider_timeout=120,
            approval_digest=approval,
            contract=contract,
        )
    assert captured == []


def test_live_coding_pair_rejects_catalog_change_after_delivery_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, source, scenarios_path = _local_scenario(tmp_path)
    cache_root, cache = _mirror_cache(tmp_path, scenario, source)
    output = tmp_path / "output"
    contract = _contract(
        scenario,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        execute=True,
    )
    approval = benchmark.live_coding_pair_contract_sha256(contract)
    captured: list[dict[str, Any]] = []
    monkeypatch.setenv(API_KEY_ENV, "provider-secret-not-for-artifacts")
    monkeypatch.setattr(
        benchmark,
        "_execute_live_coding_provider_arm",
        _fake_provider_executor(captured),
    )
    real_contract = benchmark.live_coding_pair_contract
    contract_calls = 0

    def changing_contract(**kwargs: Any) -> dict[str, Any]:
        nonlocal contract_calls
        contract_calls += 1
        value = real_contract(**kwargs)
        if contract_calls >= 2:
            value["runtime"]["opened_release_identity"]["release_root_digest"] = "0" * 64
        return value

    monkeypatch.setattr(benchmark, "live_coding_pair_contract", changing_contract)

    with pytest.raises(RuntimeError, match="contract or authenticated catalog changed"):
        benchmark.run_live_coding_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            cache_root=cache_root,
            model=MODEL,
            api_key_env=API_KEY_ENV,
            timeout=900,
            max_iterations=1,
            max_tokens=2_048,
            provider_timeout=120,
            approval_digest=approval,
            contract=contract,
        )
    assert captured == []


def test_live_coding_pair_execute_requires_distinct_approval_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    cache_root = tmp_path / "cache"
    scenario = benchmark.load_scenarios(SCENARIOS)[0]
    plan_contract = _contract(scenario, output=output, cache_root=cache_root)
    plan_approval = benchmark.live_coding_pair_contract_sha256(plan_contract)
    forbidden = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # noqa: E731
        AssertionError("execute mutation started under contract-only approval")
    )
    monkeypatch.setattr(benchmark, "ensure_repo_cache", forbidden)
    monkeypatch.setattr(benchmark, "prepare_workspace", forbidden)
    monkeypatch.setattr(benchmark, "_execute_live_coding_provider_arm", forbidden)
    argv = _argv(output=output, cache_root=cache_root)
    argv.extend(
        [
            "--live-coding-pair-execute",
            "--live-coding-pair-approval-sha256",
            plan_approval,
        ]
    )

    with pytest.raises(SystemExit, match="approval required before any workspace or provider"):
        benchmark.main(argv)
    assert not output.exists()
    assert not cache_root.exists()


def test_live_coding_pair_execute_rejects_preexisting_empty_output(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    args = _args(output=output, cache_root=tmp_path / "cache")
    args.live_coding_pair_execute = True

    with pytest.raises(SystemExit, match="execute requires the output directory to be absent"):
        benchmark._run_live_coding_pair_main(args)
    assert list(output.iterdir()) == []


def test_live_coding_pair_runner_rejects_output_created_after_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, source, scenarios_path = _local_scenario(tmp_path)
    cache_root, cache = _mirror_cache(tmp_path, scenario, source)
    output = tmp_path / "output"
    contract = _contract(
        scenario,
        scenarios_path=scenarios_path,
        output=output,
        cache_root=cache_root,
        execute=True,
    )
    approval = benchmark.live_coding_pair_contract_sha256(contract)
    output.mkdir()
    (output / "live-coding-pair-report.json").write_text("stale", encoding="utf-8")
    provider = lambda **_kwargs: (_ for _ in ()).throw(  # noqa: E731
        AssertionError("provider ran with a stale output root")
    )
    monkeypatch.setenv(API_KEY_ENV, "provider-secret-not-for-artifacts")
    monkeypatch.setattr(benchmark, "_execute_live_coding_provider_arm", provider)

    with pytest.raises(ValueError, match="output directory must be absent"):
        benchmark.run_live_coding_pair(
            scenario,
            cache=cache,
            scenarios_path=scenarios_path,
            output=output,
            cache_root=cache_root,
            model=MODEL,
            api_key_env=API_KEY_ENV,
            timeout=900,
            max_iterations=1,
            max_tokens=2_048,
            provider_timeout=120,
            approval_digest=approval,
            contract=contract,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("unified_engine_treatment", False, "requires --unified-engine-treatment"),
        ("engine", "production-ctx-run", "requires --engine codex-production-catalog"),
        ("arm", "baseline", "requires --arm both"),
        ("retries", 1, "requires --retries 0"),
        ("trials", 2, "requires --trials 1"),
        ("max_iterations", 2, "requires --max-iterations 1"),
        ("max_tokens", None, "requires a positive --max-tokens"),
        ("base_url", "https://example.invalid/v1", "rejects custom provider routes"),
        ("api_key_env", None, "requires --api-key-env"),
        ("output", None, "requires an explicit --output path"),
        ("dry_run", True, "contract gate, not list/dry-run"),
    ],
)
def test_live_coding_pair_cli_rejects_unfair_routes(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    args = _args(output=tmp_path / "output", cache_root=tmp_path / "cache")
    setattr(args, field, value)

    with pytest.raises(SystemExit, match=message):
        benchmark._run_live_coding_pair_main(args)
