"""
test_harness_cli_run.py -- `ctx run` / `ctx resume` / `ctx sessions` CLI.

Tests mock LiteLLM and use fake or controlled MCP routers so no real provider
network happens. The goal is pinning:

  * argv parsing for all 3 subcommands
  * provider key-env auto-detection
  * MCP spec parser (preset vs explicit)
  * session-start metadata capture
  * exit codes
  * stdout / stderr separation (--quiet, --json)
  * resume round-trip

Real-provider smoke lives in an integration-marked suite we'll add
once the full H1-H7 stack is proven on a live model.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, cast

import pytest

from ctx import __version__
import ctx.adapters.generic.runtime_lifecycle as runtime_lifecycle
import ctx.adapters.generic.tools.mcp_router as mcp_router_module
import ctx.cli.run as run_cli
import ctx.telemetry as telemetry
from ctx.adapters.generic.adaptive_runtime import SelectedSkill
from ctx.adapters.generic.evaluator import EvaluationLoopResult
from ctx.adapters.generic.loop import LoopResult, ProviderFailure
from ctx.adapters.recommendation_presentation import render_present_bundle_context
from ctx.adapters.generic.state import load_session
from ctx.cli.run import (
    _apply_mcp_env_overlays,
    _compile_tool_policy,
    _model_provider_prefix,
    _parse_mcp_spec,
    _resolve_api_key_env,
    _split_mcp_invocation,
    main,
)
from ctx.adapters.generic.providers import ToolCall, ToolDefinition, Usage
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planning_v3 import (
    BenefitAuditReference,
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    ManualPlanningAuthority,
)
from ctx.engine.protocol import HostAction, ScopeRef, Transition
from ctx.engine.state import CommittedPlanV3, PlanCapabilityV3
from ctx.runtime.production_catalog import (
    RELEASE_QUERY_CATALOG_MODE,
    RELEASE_QUERY_CATALOG_ROOT_SHA256,
    RELEASE_QUERY_CATALOG_SEQUENCE,
)
from ctx.runtime.query_decision import (
    CommittedQueryDecision,
    QueryDecisionFailure,
    QueryHostDescriptor,
    _commit_query_decision,
)
from ctx.telemetry import read_events, record_event as real_record_event


_MCP_FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _provider_call_bytes(calls: list[dict[str, Any]]) -> bytes:
    """Canonical bytes for comparing the exact provider boundary."""

    return json.dumps(
        calls,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _ctx_run_argv(
    sessions_dir: Path,
    *,
    session_id: str,
    task: str,
    mode: str | None = None,
    ctx_tool_surface: str | None = None,
) -> list[str]:
    argv = [
        "run",
        "--model",
        "ollama/x",
        "--task",
        task,
        "--sessions-dir",
        str(sessions_dir),
        "--session-id",
        session_id,
    ]
    if mode is not None:
        argv.extend(("--ctx-engine-mode", mode))
    if ctx_tool_surface is None:
        argv.append("--no-ctx-tools")
    else:
        argv.extend(("--ctx-tool-surface", ctx_tool_surface))
    argv.append("--quiet")
    return argv


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_SYNTHETIC_REVIEWED_ROOT_SHA256 = _digest("ctx-run-synthetic-reviewed-release-root")
_SYNTHETIC_REVIEWED_SEQUENCE = RELEASE_QUERY_CATALOG_SEQUENCE + 1
_SYNTHETIC_REVIEWED_MODE = "reviewed"


def _ctx_run_scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="ctx-run-session",
        exposure_id="exposure-1",
        host_context_id="ctx-run",
    )


def _benefit_audit(*, candidates: int, evaluations: int) -> BenefitAuditReference:
    return BenefitAuditReference(
        result_schema_id="ctx.benefit-result-v1",
        result_digest=_digest("ctx-run-synthetic-benefit-result"),
        policy_schema_id="ctx.benefit-policy-v1",
        policy_digest=_digest("ctx-run-synthetic-benefit-policy"),
        selection_algorithm_id="ctx.benefit-selection-v1",
        calibration_digest=_digest("ctx-run-synthetic-calibration"),
        requested_limit=5,
        candidate_pool_count=candidates,
        search_evaluation_count=evaluations,
    )


def _closed_v3_plan_and_transition(
    *,
    plan_digest: str | None = None,
) -> tuple[CommittedPlanV3, Transition]:
    """Build one exact reviewed plan and its matching full-v3 transition."""

    scope = _ctx_run_scope()
    capability_id = "agent:reviewer"
    presentation = CapabilityCandidate(
        capability_id=capability_id,
        kind="agent",
        name="reviewer",
        source_digest=_digest("ctx-run-catalog-entry"),
        normalized_score_ppm=600_000,
        matching_signals=("python", "review"),
        reason_codes=("reviewed-match",),
        actionability="manual",
    )
    selection = CapabilityPlanSelectionV3(
        presentation=presentation,
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=capability_id,
            kind="agent",
            catalog_namespace_digest=_digest("ctx-run-catalog"),
        ),
        benefit=CapabilityBenefitProjection(
            tier="advisory",
            individual_net_benefit_u=600_000,
            marginal_net_benefit_u=600_000,
        ),
        authority=ManualPlanningAuthority(),
    )
    committed_digest = plan_digest or _digest("ctx-run-schema-v3-plan")
    plan = CommittedPlanV3(
        plan_id="ctx-run-schema-v3-plan",
        catalog_snapshot_id=_digest("ctx-run-synthetic-catalog-snapshot"),
        decision_digest=committed_digest,
        status="ready",
        abstention_code=None,
        benefit_audit=_benefit_audit(candidates=1, evaluations=1),
        capabilities=(PlanCapabilityV3(selection=selection),),
    )
    transition = Transition(
        event_id="ctx-run-intent-observed",
        scope=scope,
        from_revision=1,
        to_revision=2,
        actions=(
            HostAction(
                action_id="ctx-run-present-bundle",
                kind="PresentBundle",
                scope=scope,
                precondition_revision=2,
                payload={
                    "plan_digest": plan.decision_digest,
                    "capabilities": (selection.to_mapping(),),
                },
            ),
        ),
    )
    return plan, transition


def _closed_v3_recommendation() -> tuple[str, str]:
    """Return one independently pinned closed schema-v3 recommendation."""

    plan, transition = _closed_v3_plan_and_transition()
    expected = (
        "CTX recommendation bundle (committed, advisory only):\n"
        "1. kind=agent | name=reviewer | id=agent:reviewer | "
        "actionability=manual | score_ppm=600000\n"
        "Use only capabilities relevant to the current task. "
        "Do not install, load, or activate anything without user approval."
    )
    assert render_present_bundle_context(transition) == expected
    return expected, plan.decision_digest


def _abstained_plan_and_transition(*, plan_digest: str) -> tuple[CommittedPlanV3, Transition]:
    plan = CommittedPlanV3(
        plan_id="ctx-run-abstained-plan",
        catalog_snapshot_id=_digest("ctx-run-production-catalog-snapshot"),
        decision_digest=plan_digest,
        status="abstained",
        abstention_code="no-feasible-capability",
        benefit_audit=_benefit_audit(candidates=0, evaluations=0),
        capabilities=(),
    )
    return (
        plan,
        Transition(
            event_id="ctx-run-abstained-intent",
            scope=_ctx_run_scope(),
            from_revision=1,
            to_revision=2,
        ),
    )


def _stub_ctx_run_decision(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
    recommendation_context: str | None,
    recommendation_count: int,
    plan_digest: str,
) -> CommittedQueryDecision:
    """Return one sealed decision from an exact plan and transition."""

    if status == "presented":
        if recommendation_context is None or recommendation_count != 1:
            raise AssertionError("presented fixture must declare its one exact context")
        plan, transition = _closed_v3_plan_and_transition(plan_digest=plan_digest)
        release_root = _SYNTHETIC_REVIEWED_ROOT_SHA256
        release_sequence = _SYNTHETIC_REVIEWED_SEQUENCE
        catalog_mode = _SYNTHETIC_REVIEWED_MODE
        monkeypatch.setattr(run_cli, "RELEASE_QUERY_CATALOG_ROOT_SHA256", release_root)
        monkeypatch.setattr(run_cli, "RELEASE_QUERY_CATALOG_SEQUENCE", release_sequence)
        monkeypatch.setattr(run_cli, "RELEASE_QUERY_CATALOG_MODE", catalog_mode)
    elif status == "abstained":
        if recommendation_context is not None or recommendation_count != 0:
            raise AssertionError("abstained fixture cannot declare recommendation context")
        plan, transition = _abstained_plan_and_transition(plan_digest=plan_digest)
        release_root = RELEASE_QUERY_CATALOG_ROOT_SHA256
        release_sequence = RELEASE_QUERY_CATALOG_SEQUENCE
        catalog_mode = RELEASE_QUERY_CATALOG_MODE
    else:
        raise AssertionError("sealed success fixture supports presented or abstained only")
    decision = _commit_query_decision(
        host=QueryHostDescriptor.ctx_run(),
        transition=transition,
        plan=plan,
        journal_revision=2,
        journal_record_digest=_digest(f"ctx-run-journal:{status}"),
        release_root_digest=release_root,
        release_sequence=release_sequence,
        catalog_mode=catalog_mode,
        work_signature_digest=_digest(f"ctx-run-work:{status}"),
        host_invocation_digest=_digest(f"ctx-run-invocation:{status}"),
    )
    assert decision.recommendation_context == recommendation_context
    assert decision.recommendation_count == recommendation_count
    return decision


def _unsafe_ctx_run_decision(
    valid: CommittedQueryDecision,
    **overrides: object,
) -> CommittedQueryDecision:
    """Hostile mutation of a sealed local fixture, used only to prove rejection."""

    for field_name, value in overrides.items():
        object.__setattr__(valid, field_name, value)
    return valid


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"ctx {__version__}\n"


# ── Fixture: fake litellm so --provider ollama (no key) works ───────────────


@pytest.fixture()
def fake_litellm(monkeypatch: pytest.MonkeyPatch):
    """Drop a stub `litellm` module into sys.modules.

    Records every call to `completion` and returns a canned
    stop-response so the loop terminates after one turn.
    """
    fake = types.ModuleType("litellm")
    calls: list[dict[str, Any]] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return {
            "choices": [
                {
                    "message": {"content": "final answer", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }

    fake.completion = completion  # type: ignore[attr-defined]
    fake._calls = calls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


def _tool_call_completion(
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments or {}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }


def _submitted_tool_names(call: dict[str, Any]) -> set[str]:
    return {item["function"]["name"] for item in call.get("tools", [])}


def _enable_real_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    path = tmp_path / "telemetry" / "events.jsonl"
    config = {
        "enabled": True,
        "mode": "local_redacted",
        "path": str(path),
        "export": {"enabled": False},
    }

    def config_get(key: str, default: Any) -> Any:
        return config if key == "telemetry" else default

    monkeypatch.setattr(telemetry, "_config_get", config_get)
    monkeypatch.setattr(telemetry, "record_event", real_record_event)
    monkeypatch.setattr(run_cli, "record_event", real_record_event)
    monkeypatch.setattr(runtime_lifecycle, "record_event", real_record_event)
    return path


# ── _model_provider_prefix ─────────────────────────────────────────────────


class TestProviderPrefix:
    @pytest.mark.parametrize(
        "model,prefix",
        [
            ("openrouter/anthropic/claude", "openrouter"),
            ("ollama/llama3.1", "ollama"),
            ("openai/gpt-4", "openai"),
            ("bare-model-no-slash", "bare-model-no-slash"),
        ],
    )
    def test_parse(self, model: str, prefix: str) -> None:
        assert _model_provider_prefix(model) == prefix


# ── _resolve_api_key_env ──────────────────────────────────────────────────


class TestResolveApiKeyEnv:
    def test_explicit_wins(self) -> None:
        assert _resolve_api_key_env("MY_KEY", "ollama/x", None) == "MY_KEY"

    def test_explicit_empty_is_none(self) -> None:
        """Empty string is an explicit 'no key' (Ollama via CLI override)."""
        assert _resolve_api_key_env("", "openrouter/x", None) is None

    def test_inferred_from_model_prefix(self) -> None:
        assert _resolve_api_key_env(None, "openrouter/x", None) == "OPENROUTER_API_KEY"
        assert _resolve_api_key_env(None, "anthropic/claude", None) == "ANTHROPIC_API_KEY"
        assert _resolve_api_key_env(None, "huggingface/org-model", None) == "HF_TOKEN"

    def test_inferred_from_provider_flag(self) -> None:
        assert _resolve_api_key_env(None, "custom-x", "openai") == "OPENAI_API_KEY"
        assert _resolve_api_key_env(None, "custom-x", "huggingface") == "HF_TOKEN"

    def test_ollama_returns_none(self) -> None:
        assert _resolve_api_key_env(None, "ollama/llama3", None) is None

    def test_unknown_provider_returns_none(self) -> None:
        assert _resolve_api_key_env(None, "unknown/x", None) is None


# ── _parse_mcp_spec ────────────────────────────────────────────────────────


class TestParseMcpSpec:
    def test_preset_filesystem(self) -> None:
        cfg = _parse_mcp_spec("filesystem")
        assert cfg.name == "filesystem"
        assert cfg.command == "npx"
        assert "server-filesystem" in " ".join(cfg.args)

    def test_preset_github(self) -> None:
        cfg = _parse_mcp_spec("github")
        assert cfg.name == "github"
        assert "GITHUB_TOKEN" in cfg.credential_env

    def test_explicit_form(self) -> None:
        cfg = _parse_mcp_spec("fs:npx -y pkg /tmp")
        assert cfg.name == "fs"
        assert cfg.command == "npx"
        assert cfg.args == ("-y", "pkg", "/tmp")
        with_env = _apply_mcp_env_overlays([cfg], ["fs:MY_MCP_TOKEN"])[0]
        assert with_env.credential_env == ("MY_MCP_TOKEN",)

    def test_explicit_form_preserves_quoted_args(self) -> None:
        cfg = _parse_mcp_spec(r'fs:npx -y pkg "C:\My Project"')
        assert cfg.name == "fs"
        assert cfg.command == "npx"
        assert cfg.args == ("-y", "pkg", r"C:\My Project")

    def test_quoted_argument_preserves_literal_backslashes(self) -> None:
        assert _split_mcp_invocation(r'cmd "C:\My Project"') == [
            "cmd",
            r"C:\My Project",
        ]

    def test_explicit_single_command(self) -> None:
        cfg = _parse_mcp_spec("raw:myserver")
        assert cfg.name == "raw"
        assert cfg.command == "myserver"
        assert cfg.args == ()

    @pytest.mark.parametrize(
        "spec",
        [
            "fs:npx server --token secret-value",
            "fs:npx server --api-key=secret-value",
            "fs:npx server GITHUB_TOKEN=secret-value",
            "fs:npx server ghp_1234567890abcdefghijkl",
        ],
    )
    def test_explicit_form_rejects_inline_secret_args(self, spec: str) -> None:
        with pytest.raises(SystemExit, match="--mcp-env"):
            _parse_mcp_spec(spec)

    def test_explicit_form_allows_secret_indirection_args(self) -> None:
        cfg = _parse_mcp_spec(
            "fs:npx server --token-file /run/secrets/token --credential-env GITHUB_TOKEN"
        )

        assert cfg.args == (
            "server",
            "--token-file",
            "/run/secrets/token",
            "--credential-env",
            "GITHUB_TOKEN",
        )

    def test_filesystem_colon_path_uses_preset_command(self) -> None:
        cfg = _parse_mcp_spec("filesystem:/tmp/project")
        assert cfg.name == "filesystem"
        assert cfg.command == "npx"
        assert cfg.args[-1] == "/tmp/project"
        assert "server-filesystem" in " ".join(cfg.args)

    def test_unknown_bare_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_mcp_spec("not-a-preset")

    def test_empty_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_mcp_spec("")

    def test_empty_name_or_command_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_mcp_spec(":command-only")
        with pytest.raises(SystemExit):
            _parse_mcp_spec("name:")

    def test_whitespace_trimmed(self) -> None:
        cfg = _parse_mcp_spec("  filesystem  ")
        assert cfg.name == "filesystem"


# ── Subcommand: run ────────────────────────────────────────────────────────


class TestToolPolicy:
    def test_allow_and_deny_patterns(self) -> None:
        policy = _compile_tool_policy(["ctx__*"], ["ctx__wiki_get"])
        assert policy is not None

        assert policy(ToolCall(id="1", name="ctx__recommend_bundle", arguments={})) is None
        assert "matched deny pattern" in (
            policy(ToolCall(id="2", name="ctx__wiki_get", arguments={})) or ""
        )
        assert "no allow pattern matched" in (
            policy(ToolCall(id="3", name="filesystem__read_file", arguments={})) or ""
        )

    def test_empty_patterns_disable_policy(self) -> None:
        assert _compile_tool_policy([], []) is None

    def test_adaptive_mcp_grants_require_namespaced_patterns(self) -> None:
        configs = [_parse_mcp_spec("alpha:ignored"), _parse_mcp_spec("beta:ignored")]

        assert run_cli._adaptive_mcp_server_names(configs, ("alpha*",)) == ()
        assert run_cli._adaptive_mcp_server_names(configs, ("*",)) == (
            "alpha",
            "beta",
        )
        assert run_cli._adaptive_mcp_server_names(configs, (), ("*",)) == ()
        assert run_cli._adaptive_mcp_server_names(
            configs,
            ("ctx__*", "alpha__read*"),
        ) == ("alpha",)
        assert (
            run_cli._adaptive_mcp_server_names(
                configs,
                ("alpha__*",),
                ("alpha__*",),
            )
            == ()
        )


class TestRunCommand:
    def _write_model_profile(self, root: Path, data: dict[str, Any]) -> None:
        root.mkdir(parents=True)
        (root / "ctx-model-profile.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )

    def test_happy_path_writes_session(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "say hi",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert exit_code == 0
        # One session file created.
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        # Stdout is the final answer; stderr has the [ctx] status lines.
        captured = capsys.readouterr()
        assert "final answer" in captured.out

    def test_ctx_engine_mode_defaults_to_byte_identical_legacy_provider_payload(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine_calls: list[dict[str, Any]] = []

        def unexpected_engine_open(**kwargs: Any) -> object:
            engine_calls.append(kwargs)
            raise AssertionError("legacy mode must not open the CTX query engine")

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            unexpected_engine_open,
            raising=False,
        )
        task = "preserve the exact legacy provider request"
        default_exit = main(
            _ctx_run_argv(
                tmp_path / "default",
                session_id="legacy-compatible",
                task=task,
            )
        )
        default_calls = _provider_call_bytes(fake_litellm._calls)
        default_output = capsys.readouterr()
        fake_litellm._calls.clear()

        explicit_exit = main(
            _ctx_run_argv(
                tmp_path / "explicit",
                session_id="legacy-compatible",
                task=task,
                mode="legacy",
            )
        )
        explicit_calls = _provider_call_bytes(fake_litellm._calls)
        explicit_output = capsys.readouterr()

        assert default_exit == explicit_exit == 0
        assert default_calls == explicit_calls
        assert default_output == explicit_output
        assert engine_calls == []

    def test_ctx_engine_shadow_commits_before_provider_but_keeps_legacy_payload(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = "review the Python change"
        assert (
            main(
                _ctx_run_argv(
                    tmp_path / "legacy",
                    session_id="shadow-compatible",
                    task=task,
                )
            )
            == 0
        )
        legacy_calls = _provider_call_bytes(fake_litellm._calls)
        fake_litellm._calls.clear()

        context, plan_digest = _closed_v3_recommendation()
        events: list[str] = []

        def prepare_decision(**_kwargs: Any) -> CommittedQueryDecision:
            events.append("decision-journaled")
            return _stub_ctx_run_decision(
                monkeypatch,
                status="presented",
                recommendation_context=context,
                recommendation_count=1,
                plan_digest=plan_digest,
            )

        original_completion = fake_litellm.completion

        def completion(**kwargs: Any) -> dict[str, Any]:
            events.append("provider")
            return cast(dict[str, Any], original_completion(**kwargs))

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )
        fake_litellm.completion = completion

        shadow_exit = main(
            _ctx_run_argv(
                tmp_path / "shadow",
                session_id="shadow-compatible",
                task=task,
                mode="shadow",
            )
        )

        assert shadow_exit == 0
        assert events == ["decision-journaled", "provider"]
        assert _provider_call_bytes(fake_litellm._calls) == legacy_calls
        shadow_contents = "\n".join(
            message["content"] for call in fake_litellm._calls for message in call["messages"]
        )
        assert context not in shadow_contents
        metadata = load_session(
            "shadow-compatible",
            sessions_dir=tmp_path / "shadow",
        ).metadata["ctx_engine"]
        assert metadata["requested_mode"] == "shadow"
        assert metadata["resolved_mode"] == "shadow"
        assert metadata["effective_mode"] == "shadow"
        assert metadata["status"] == "presented"
        assert metadata["circuit_breaker_tripped"] is False
        assert metadata["recommendation_count"] == 1
        assert metadata["plan_digest"] == plan_digest
        assert metadata["journal_revision"] == 2
        assert metadata["journal_record_digest"]

    def test_ctx_engine_recommend_injects_one_exact_lower_authority_v3_bundle(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = "review the Python change"
        assert (
            main(
                _ctx_run_argv(
                    tmp_path / "legacy",
                    session_id="recommend-compatible",
                    task=task,
                )
            )
            == 0
        )
        legacy_system_prompt = fake_litellm._calls[0]["messages"][0]["content"]
        fake_litellm._calls.clear()

        context, plan_digest = _closed_v3_recommendation()
        events: list[str] = []

        def prepare_decision(**_kwargs: Any) -> CommittedQueryDecision:
            events.append("decision-journaled")
            return _stub_ctx_run_decision(
                monkeypatch,
                status="presented",
                recommendation_context=context,
                recommendation_count=1,
                plan_digest=plan_digest,
            )

        original_completion = fake_litellm.completion

        def completion(**kwargs: Any) -> dict[str, Any]:
            events.append("provider")
            return cast(dict[str, Any], original_completion(**kwargs))

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )
        fake_litellm.completion = completion

        recommend_exit = main(
            _ctx_run_argv(
                tmp_path / "recommend",
                session_id="recommend-compatible",
                task=task,
                mode="recommend",
            )
        )

        assert recommend_exit == 0
        assert events == ["decision-journaled", "provider"]
        assert len(fake_litellm._calls) == 1
        messages = fake_litellm._calls[0]["messages"]
        assert messages == [
            {"role": "system", "content": legacy_system_prompt},
            {
                "role": "user",
                "content": context + "\n\n--- current user request ---\n" + task,
            },
        ]
        assert "\n".join(message["content"] for message in messages).count(context) == 1
        session_text = (tmp_path / "recommend" / "recommend-compatible.jsonl").read_text(
            encoding="utf-8"
        )
        assert context.splitlines()[0] not in session_text
        replay = load_session(
            "recommend-compatible",
            sessions_dir=tmp_path / "recommend",
        )
        assert all(context not in message.content for message in replay.messages)
        metadata = replay.metadata["ctx_engine"]
        assert metadata["effective_mode"] == "recommend"
        assert metadata["status"] == "presented"
        assert metadata["recommendation_count"] == 1
        assert metadata["plan_digest"] == plan_digest

    def test_ctx_engine_recommend_abstention_is_successful_and_byte_identical_to_legacy(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = "make no recommendation when benefit is not positive"
        assert (
            main(
                _ctx_run_argv(
                    tmp_path / "legacy",
                    session_id="abstention-compatible",
                    task=task,
                )
            )
            == 0
        )
        legacy_calls = _provider_call_bytes(fake_litellm._calls)
        fake_litellm._calls.clear()
        decision_calls: list[dict[str, Any]] = []
        abstained_plan_digest = hashlib.sha256(b"committed-abstention-plan").hexdigest()

        def prepare_decision(**kwargs: Any) -> CommittedQueryDecision:
            decision_calls.append(kwargs)
            return _stub_ctx_run_decision(
                monkeypatch,
                status="abstained",
                recommendation_context=None,
                recommendation_count=0,
                plan_digest=abstained_plan_digest,
            )

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )

        recommend_exit = main(
            _ctx_run_argv(
                tmp_path / "recommend",
                session_id="abstention-compatible",
                task=task,
                mode="recommend",
            )
        )

        assert recommend_exit == 0
        assert len(decision_calls) == 1
        assert _provider_call_bytes(fake_litellm._calls) == legacy_calls
        metadata = load_session(
            "abstention-compatible",
            sessions_dir=tmp_path / "recommend",
        ).metadata["ctx_engine"]
        assert metadata["effective_mode"] == "recommend"
        assert metadata["status"] == "abstained"
        assert metadata["circuit_breaker_tripped"] is False
        assert metadata["failure_code"] is None
        assert metadata["recommendation_count"] == 0
        assert metadata["plan_digest"] == abstained_plan_digest

    def test_ctx_engine_failure_falls_back_once_and_records_session_breaker(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = "finish after one tool call"

        def install_two_turn_provider_script() -> None:
            fake_litellm._calls.clear()

            def completion(**kwargs: Any) -> dict[str, Any]:
                fake_litellm._calls.append(kwargs)
                if len(fake_litellm._calls) == 1:
                    return _tool_call_completion("ctx__wiki_get")
                return {
                    "choices": [
                        {
                            "message": {"content": "done", "tool_calls": None},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                }

            fake_litellm.completion = completion

        install_two_turn_provider_script()
        assert (
            main(
                _ctx_run_argv(
                    tmp_path / "legacy",
                    session_id="engine-failure-compatible",
                    task=task,
                    ctx_tool_surface="minimal",
                )
            )
            == 0
        )
        legacy_calls = _provider_call_bytes(fake_litellm._calls)
        assert len(fake_litellm._calls) == 2

        engine_calls: list[dict[str, Any]] = []

        def fail_engine(**kwargs: Any) -> QueryDecisionFailure:
            engine_calls.append(kwargs)
            return QueryDecisionFailure(failure_code="catalog-open-failed")

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            fail_engine,
            raising=False,
        )
        install_two_turn_provider_script()

        recommend_exit = main(
            _ctx_run_argv(
                tmp_path / "recommend",
                session_id="engine-failure-compatible",
                task=task,
                mode="recommend",
                ctx_tool_surface="minimal",
            )
        )

        assert recommend_exit == 0
        assert len(engine_calls) == 1
        assert len(fake_litellm._calls) == 2
        assert _provider_call_bytes(fake_litellm._calls) == legacy_calls
        metadata = load_session(
            "engine-failure-compatible",
            sessions_dir=tmp_path / "recommend",
        ).metadata["ctx_engine"]
        assert metadata["requested_mode"] == "recommend"
        assert metadata["resolved_mode"] == "recommend"
        assert metadata["effective_mode"] == "legacy"
        assert metadata["status"] == "failed"
        assert metadata["circuit_breaker_tripped"] is True
        assert metadata["failure_code"] == "catalog-open-failed"

    @pytest.mark.parametrize("mode", ("shadow", "recommend"))
    def test_ctx_engine_mode_uses_environment_when_cli_mode_is_omitted(
        self,
        mode: str,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CTX_ENGINE_MODE", mode)
        monkeypatch.delenv("CTX_FORCE_LEGACY", raising=False)
        context, plan_digest = _closed_v3_recommendation()
        decision_calls: list[dict[str, Any]] = []

        def prepare_decision(**kwargs: Any) -> CommittedQueryDecision:
            decision_calls.append(kwargs)
            return _stub_ctx_run_decision(
                monkeypatch,
                status="presented",
                recommendation_context=context,
                recommendation_count=1,
                plan_digest=plan_digest,
            )

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )
        session_id = f"environment-{mode}"

        exit_code = main(
            _ctx_run_argv(
                tmp_path / mode,
                session_id=session_id,
                task="resolve the engine mode from the environment",
            )
        )

        assert exit_code == 0
        assert len(decision_calls) == 1
        metadata = load_session(session_id, sessions_dir=tmp_path / mode).metadata["ctx_engine"]
        assert metadata["requested_mode"] == mode
        assert metadata["resolved_mode"] == mode
        assert metadata["effective_mode"] == mode
        provider_text = "\n".join(
            message["content"] for call in fake_litellm._calls for message in call["messages"]
        )
        assert (context in provider_text) is (mode == "recommend")

    def test_explicit_ctx_engine_mode_wins_over_environment(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CTX_ENGINE_MODE", "recommend")
        monkeypatch.delenv("CTX_FORCE_LEGACY", raising=False)
        context, plan_digest = _closed_v3_recommendation()
        decision_calls: list[dict[str, Any]] = []

        def prepare_decision(**kwargs: Any) -> CommittedQueryDecision:
            decision_calls.append(kwargs)
            return _stub_ctx_run_decision(
                monkeypatch,
                status="presented",
                recommendation_context=context,
                recommendation_count=1,
                plan_digest=plan_digest,
            )

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )

        exit_code = main(
            _ctx_run_argv(
                tmp_path,
                session_id="explicit-shadow",
                task="the command line must override the environment",
                mode="shadow",
            )
        )

        assert exit_code == 0
        assert len(decision_calls) == 1
        metadata = load_session("explicit-shadow", sessions_dir=tmp_path).metadata["ctx_engine"]
        assert metadata["requested_mode"] == "shadow"
        assert metadata["resolved_mode"] == "shadow"
        assert metadata["effective_mode"] == "shadow"
        provider_text = "\n".join(
            message["content"] for call in fake_litellm._calls for message in call["messages"]
        )
        assert context not in provider_text

    def test_truthy_ctx_force_legacy_overrides_recommend_without_opening_engine(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CTX_ENGINE_MODE", raising=False)
        monkeypatch.delenv("CTX_FORCE_LEGACY", raising=False)
        task = "force exact legacy compatibility"
        assert (
            main(
                _ctx_run_argv(
                    tmp_path / "baseline",
                    session_id="force-legacy",
                    task=task,
                )
            )
            == 0
        )
        baseline_calls = _provider_call_bytes(fake_litellm._calls)
        fake_litellm._calls.clear()
        engine_calls: list[dict[str, Any]] = []

        def unexpected_engine_open(**kwargs: Any) -> object:
            engine_calls.append(kwargs)
            raise AssertionError("CTX_FORCE_LEGACY must not open an engine session")

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            unexpected_engine_open,
            raising=False,
        )
        monkeypatch.setenv("CTX_ENGINE_MODE", "recommend")
        monkeypatch.setenv("CTX_FORCE_LEGACY", "true")

        exit_code = main(
            _ctx_run_argv(
                tmp_path / "forced",
                session_id="force-legacy",
                task=task,
            )
        )

        assert exit_code == 0
        assert engine_calls == []
        assert _provider_call_bytes(fake_litellm._calls) == baseline_calls
        metadata = load_session(
            "force-legacy",
            sessions_dir=tmp_path / "forced",
        ).metadata["ctx_engine"]
        assert metadata["requested_mode"] == "recommend"
        assert metadata["resolved_mode"] == "legacy"
        assert metadata["effective_mode"] == "legacy"

    def test_recommend_suppresses_legacy_ctx_surface_but_preserves_explicit_mcp(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        router_modes: list[bool] = []

        class FakeRouter:
            def __init__(
                self,
                configs: list[Any],
                *,
                session_id: str | None = None,
                lazy: bool = False,
            ) -> None:
                assert len(configs) == 1
                assert session_id in {"mcp-baseline", "mcp-recommend"}
                self.started = False
                router_modes.append(lazy)

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.started = False

            def list_tools(self) -> list[ToolDefinition]:
                assert self.started
                return [
                    ToolDefinition(
                        name="external__read",
                        description="read from the explicit external MCP",
                        parameters={"type": "object", "properties": {}},
                    )
                ]

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                raise AssertionError(f"unexpected tool call: {name} {arguments}")

        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        baseline_argv = _ctx_run_argv(
            tmp_path / "baseline",
            session_id="mcp-baseline",
            task="keep the explicit MCP provider schema",
        )
        baseline_argv[-1:-1] = ["--mcp", "external:ignored-command"]
        assert main(baseline_argv) == 0
        baseline_tools = json.loads(json.dumps(fake_litellm._calls[0]["tools"]))
        assert _submitted_tool_names(fake_litellm._calls[0]) == {"external__read"}
        fake_litellm._calls.clear()

        adaptive_calls: list[dict[str, Any]] = []

        def unexpected_adaptive_controller(cls: type[Any], *args: Any, **kwargs: Any) -> object:
            adaptive_calls.append({"args": args, "kwargs": kwargs})
            raise AssertionError(
                "a successful recommend decision must suppress the legacy adaptive controller"
            )

        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(unexpected_adaptive_controller),
        )
        context, plan_digest = _closed_v3_recommendation()

        def prepare_decision(**_kwargs: Any) -> CommittedQueryDecision:
            return _stub_ctx_run_decision(
                monkeypatch,
                status="presented",
                recommendation_context=context,
                recommendation_count=1,
                plan_digest=plan_digest,
            )

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )
        recommend_argv = _ctx_run_argv(
            tmp_path / "recommend",
            session_id="mcp-recommend",
            task="use the engine recommendation and explicit MCP",
            mode="recommend",
            ctx_tool_surface="adaptive",
        )
        recommend_argv[-1:-1] = ["--mcp", "external:ignored-command"]

        exit_code = main(recommend_argv)

        assert exit_code == 0
        assert adaptive_calls == []
        assert len(fake_litellm._calls) == 1
        recommend_call = fake_litellm._calls[0]
        assert recommend_call["tools"] == baseline_tools
        assert _submitted_tool_names(recommend_call) == {"external__read"}
        assert all(not name.startswith("ctx__") for name in _submitted_tool_names(recommend_call))
        assert context in "\n".join(message["content"] for message in recommend_call["messages"])
        assert router_modes

    def test_recommend_full_surface_exposes_only_safe_ctx_evidence_and_status_schemas(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context, plan_digest = _closed_v3_recommendation()

        def prepare_decision(**_kwargs: Any) -> CommittedQueryDecision:
            return _stub_ctx_run_decision(
                monkeypatch,
                status="presented",
                recommendation_context=context,
                recommendation_count=1,
                plan_digest=plan_digest,
            )

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )

        exit_code = main(
            _ctx_run_argv(
                tmp_path,
                session_id="recommend-safe-full-surface",
                task="use only the closed recommendation decision",
                mode="recommend",
                ctx_tool_surface="full",
            )
        )

        assert exit_code == 0
        names = _submitted_tool_names(fake_litellm._calls[0])
        forbidden = {
            "ctx__recommend_bundle",
            "ctx__recommend_related",
            "ctx__graph_query",
            "ctx__wiki_search",
            "ctx__wiki_get",
            "ctx__loop_provision",
            "ctx__loop_topup",
            "ctx__load_entity",
            "ctx__mark_entity_used",
            "ctx__unload_entity",
        }
        safe_evidence_or_status = {
            "ctx__observe_dev_event",
            "ctx__record_validation",
            "ctx__record_escalation",
            "ctx__session_end",
            "ctx__session_state",
        }
        assert names.isdisjoint(forbidden)
        assert names <= safe_evidence_or_status

    @pytest.mark.parametrize(
        "invalid_kind",
        (
            "duck-type",
            "hostile-duck",
            "uninitialized-real",
            "unsealed-copy",
            "release-root",
            "release-sequence",
            "catalog-mode",
        ),
    )
    def test_invalid_success_receipt_falls_back_before_suppression_or_injection(
        self,
        invalid_kind: str,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = "reject a success receipt outside the code-owned release"
        assert (
            main(
                _ctx_run_argv(
                    tmp_path / "legacy",
                    session_id="invalid-success-receipt",
                    task=task,
                    ctx_tool_surface="full",
                )
            )
            == 0
        )
        legacy_calls = _provider_call_bytes(fake_litellm._calls)
        fake_litellm._calls.clear()
        context, plan_digest = _closed_v3_recommendation()
        valid = _stub_ctx_run_decision(
            monkeypatch,
            status="presented",
            recommendation_context=context,
            recommendation_count=1,
            plan_digest=plan_digest,
        )
        public_fields = tuple(
            field
            for field in dataclasses.fields(CommittedQueryDecision)
            if not field.name.startswith("_")
        )
        if invalid_kind == "duck-type":
            invalid: object = types.SimpleNamespace(
                **{field.name: getattr(valid, field.name) for field in public_fields}
            )
        elif invalid_kind == "hostile-duck":

            class HostileDecision:
                @property
                def failure_code(self) -> str:
                    raise RuntimeError("private hostile receipt detail")

            invalid = HostileDecision()
        elif invalid_kind == "uninitialized-real":
            invalid = object.__new__(CommittedQueryDecision)
        elif invalid_kind == "unsealed-copy":
            invalid = object.__new__(CommittedQueryDecision)
            for field in public_fields:
                object.__setattr__(invalid, field.name, getattr(valid, field.name))
        elif invalid_kind == "release-root":
            invalid = _unsafe_ctx_run_decision(
                valid,
                release_root_digest=hashlib.sha256(b"unapproved-release").hexdigest(),
            )
        elif invalid_kind == "release-sequence":
            invalid = _unsafe_ctx_run_decision(
                valid,
                release_sequence=_SYNTHETIC_REVIEWED_SEQUENCE + 1,
            )
        else:
            invalid = _unsafe_ctx_run_decision(valid, catalog_mode="alternate-reviewed")
        engine_calls: list[dict[str, Any]] = []

        def prepare_decision(**kwargs: Any) -> object:
            engine_calls.append(kwargs)
            return invalid

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )

        exit_code = main(
            _ctx_run_argv(
                tmp_path / "recommend",
                session_id="invalid-success-receipt",
                task=task,
                mode="recommend",
                ctx_tool_surface="full",
            )
        )

        assert exit_code == 0
        assert len(engine_calls) == 1
        metadata = load_session(
            "invalid-success-receipt",
            sessions_dir=tmp_path / "recommend",
        ).metadata["ctx_engine"]
        assert metadata["effective_mode"] == "legacy"
        assert metadata["status"] == "failed"
        assert metadata["circuit_breaker_tripped"] is True
        assert _provider_call_bytes(fake_litellm._calls) == legacy_calls
        provider_text = "\n".join(
            message["content"] for call in fake_litellm._calls for message in call["messages"]
        )
        assert context not in provider_text

    @pytest.mark.parametrize("invalid_plan_digest", (None, "not-a-digest"))
    def test_abstention_with_invalid_plan_digest_trips_breaker_and_falls_back(
        self,
        invalid_plan_digest: str | None,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task = "reject an uncommitted abstention"
        assert (
            main(
                _ctx_run_argv(
                    tmp_path / "legacy",
                    session_id="invalid-abstention",
                    task=task,
                    ctx_tool_surface="full",
                )
            )
            == 0
        )
        legacy_calls = _provider_call_bytes(fake_litellm._calls)
        fake_litellm._calls.clear()
        valid = _stub_ctx_run_decision(
            monkeypatch,
            status="abstained",
            recommendation_context=None,
            recommendation_count=0,
            plan_digest=hashlib.sha256(b"valid-abstention-plan").hexdigest(),
        )
        invalid = _unsafe_ctx_run_decision(valid, plan_digest=invalid_plan_digest)

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            lambda **_kwargs: invalid,
            raising=False,
        )

        exit_code = main(
            _ctx_run_argv(
                tmp_path / "recommend",
                session_id="invalid-abstention",
                task=task,
                mode="recommend",
                ctx_tool_surface="full",
            )
        )

        assert exit_code == 0
        metadata = load_session(
            "invalid-abstention",
            sessions_dir=tmp_path / "recommend",
        ).metadata["ctx_engine"]
        assert metadata["effective_mode"] == "legacy"
        assert metadata["status"] == "failed"
        assert metadata["circuit_breaker_tripped"] is True
        assert _provider_call_bytes(fake_litellm._calls) == legacy_calls

    def test_run_passes_session_id_to_mcp_router(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []
        router_session_ids: list[str | None] = []

        class FakeRouter:
            started = False

            def __init__(self, configs: list[Any], *, session_id: str | None = None) -> None:
                assert len(configs) == 1
                router_session_ids.append(session_id)

            def start(self) -> None:
                calls.append("start")
                self.started = True

            def stop(self) -> None:
                calls.append("stop")
                self.started = False

            def list_tools(self) -> list[Any]:
                assert self.started
                calls.append("list_tools")
                return []

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                raise AssertionError(f"unexpected tool call: {name} {arguments}")

        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "say hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "mcp-run",
                "--mcp",
                "raw:ignored-command",
                "--no-ctx-tools",
                "--quiet",
            ]
        )

        assert exit_code == 0
        capsys.readouterr()
        assert router_session_ids == ["mcp-run"]
        assert calls == ["start", "list_tools", "stop"]

    def test_adaptive_mcp_is_one_turn_and_policy_bounded(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        events: list[tuple[str, Any]] = []
        routers: list[Any] = []

        class FakeRouter:
            def __init__(
                self,
                configs: list[Any],
                *,
                session_id: str | None = None,
                lazy: bool = False,
            ) -> None:
                assert len(configs) == 2 and session_id == "bounded-mcp"
                assert lazy is True
                self.lazy = lazy
                self.active: tuple[str, ...] = ()
                routers.append(self)

            def start(self) -> None:
                events.append(("start", self.active))

            def stop(self) -> None:
                assert self.active == ()
                events.append(("stop", self.active))

            @property
            def server_names(self) -> list[str]:
                return sorted(self.active)

            def list_tools(self) -> list[ToolDefinition]:
                assert self.active == ()
                events.append(("list_tools", self.active))
                return []

            def activate(
                self,
                server_names: tuple[str, ...],
                *,
                capability_epoch: int | None = None,
            ) -> list[ToolDefinition]:
                assert self.active == ()
                assert capability_epoch == 1
                self.active = tuple(server_names)
                events.append(("activate", self.active))
                return [
                    ToolDefinition(name="server__read", description="read", parameters={}),
                    ToolDefinition(name="server__write", description="write", parameters={}),
                ]

            def deactivate(self, server_names: tuple[str, ...]) -> None:
                assert tuple(server_names) == self.active
                events.append(("deactivate", self.active))
                self.active = ()

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                assert self.active == ("server",)
                assert name == "server__read"
                events.append(("call", name))
                return "bounded result"

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            events.append(("provider", tuple(sorted(_submitted_tool_names(kwargs)))))
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion("server__read")
            assert routers[0].active == ()
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        fake_litellm.completion = completion
        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(None)),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "say hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "bounded-mcp",
                "--mcp",
                "server:ignored-command",
                "--mcp",
                "other:ignored-command",
                "--allow-tool",
                "server__read",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert [_submitted_tool_names(call) for call in fake_litellm._calls] == [
            {"server__read"},
            set(),
        ]
        assert events == [
            ("start", ()),
            ("list_tools", ()),
            ("activate", ("server",)),
            ("provider", ("server__read",)),
            ("call", "server__read"),
            ("deactivate", ("server",)),
            ("provider", ()),
            ("stop", ()),
        ]
        metadata = run_cli.load_session("bounded-mcp", sessions_dir=tmp_path).metadata
        assert metadata["ctx_adaptive"]["enabled"] is True
        assert metadata["ctx_adaptive"]["mcp_configured_count"] == 2
        assert metadata["ctx_adaptive"]["mcp_activated_count"] == 1
        assert metadata["ctx_adaptive"]["mcp_fetched_tool_count"] == 2
        assert metadata["ctx_adaptive"]["mcp_submitted_tool_count"] == 1
        assert metadata["ctx_adaptive"]["mcp_submitted_schema_bytes"] > 0
        assert metadata["ctx_adaptive"]["mcp_estimated_schema_tokens"] > 0
        assert metadata["ctx_adaptive"]["mcp_schema_submission_attempted"] is True
        lifecycle_events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        mcp_events = [
            event for event in lifecycle_events if event.get("entity_type") == "mcp-server"
        ]
        assert [(event["action"], event["slug"]) for event in mcp_events] == [
            ("load_requested", "server"),
            ("load_applied", "server"),
            ("used", "server"),
            ("unload_applied", "server"),
        ]

    def test_adaptive_mcp_real_process_stays_lazy_with_ctx_subgroup(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs: list[subprocess.Popen[bytes]] = []
        real_popen = mcp_router_module.subprocess.Popen

        def capture_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            proc = real_popen(*args, **kwargs)
            procs.append(proc)
            return proc

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion("live__echo", {"text": "real"})
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        monkeypatch.setattr(mcp_router_module.subprocess, "Popen", capture_popen)
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(None)),
        )
        fake_litellm.completion = completion

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use one live tool",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "real-lazy-mcp",
                "--mcp",
                f"live:{sys.executable} {_MCP_FIXTURE}",
                "--mcp",
                "other:definitely-not-a-real-command",
                "--allow-tool",
                "ctx__recommend_bundle",
                "--allow-tool",
                "live__echo",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert [_submitted_tool_names(call) for call in fake_litellm._calls] == [
            {"ctx__recommend_bundle", "live__echo"},
            set(),
        ]
        assert len(procs) == 1
        procs[0].wait(timeout=2.0)
        assert procs[0].poll() is not None
        metadata = run_cli.load_session("real-lazy-mcp", sessions_dir=tmp_path).metadata
        assert metadata["ctx_adaptive"]["mcp_fetched_tool_count"] >= 2
        assert metadata["ctx_adaptive"]["mcp_submitted_tool_count"] == 1
        assert metadata["ctx_adaptive"]["mcp_schema_submission_attempted"] is True

    def test_adaptive_mcp_policy_denial_is_not_recorded_as_use(self, tmp_path: Path) -> None:
        class FakeRouter:
            def activate(
                self,
                server_names: tuple[str, ...],
                *,
                capability_epoch: int | None = None,
            ) -> list[ToolDefinition]:
                assert capability_epoch == 1
                return []

            def deactivate(self, server_names: tuple[str, ...]) -> None:
                pass

        lifecycle_dir = tmp_path / "runtime"
        lifecycle = run_cli.RuntimeLifecycleStore(root=lifecycle_dir)
        controller = run_cli._AdaptiveMcpController(
            run_cli.AdaptiveRuntimeController(None),
            router=cast(Any, FakeRouter()),
            server_names=("server",),
            configured_count=1,
            lifecycle=lifecycle,
            session_id="denied-mcp",
            allow_patterns=("server__read",),
            deny_patterns=(),
        )
        preparation = controller.prepare_turn(
            1,
            (),
            (),
            deadline_monotonic=None,
            cancel_event=None,
        )
        controller.activate_turn(1, preparation.capability_epoch)
        controller.on_tool_result(
            1,
            preparation.capability_epoch,
            ToolCall(id="denied", name="server__hidden", arguments={}),
            "",
            "policy: capability was not advertised",
        )
        controller.on_tool_result(
            1,
            preparation.capability_epoch,
            ToolCall(id="malformed", name="server__read", arguments={}),
            "",
            "invalid tool call arguments: malformed JSON",
        )
        controller.close_turn(1, preparation.capability_epoch, "tool_denied")

        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["action"] for event in events] == [
            "load_applied",
            "unload_applied",
        ]

    def test_run_uses_ctx_init_model_profile_when_model_omitted(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        claude = tmp_path / "claude"
        sessions = tmp_path / "sessions"
        self._write_model_profile(
            claude,
            {
                "mode": "custom",
                "provider": "openai",
                "model": "openai/gpt-5.5",
                "api_key_env": "OPENAI_API_KEY",
                "base_url": "https://api.example.test/v1",
                "goal": "build agents",
            },
        )
        monkeypatch.setattr(run_cli, "_claude_dir", lambda: claude)
        monkeypatch.setenv("OPENAI_API_KEY", "profile-secret")

        exit_code = main(
            [
                "run",
                "--task",
                "say hi",
                "--sessions-dir",
                str(sessions),
                "--session-id",
                "profile-run",
                "--no-ctx-tools",
                "--quiet",
            ],
        )

        assert exit_code == 0
        call = fake_litellm._calls[0]
        assert call["model"] == "openai/gpt-5.5"
        assert call["api_key"] == "profile-secret"
        assert call["api_base"] == "https://api.example.test/v1"
        first_line = (
            (sessions / "profile-run.jsonl")
            .read_text(
                encoding="utf-8",
            )
            .splitlines()[0]
        )
        event = json.loads(first_line)
        assert event["model"] == "openai/gpt-5.5"
        assert event["provider"] == "openai"
        assert event["api_key_env"] == "OPENAI_API_KEY"
        captured = capsys.readouterr()
        assert "final answer" in captured.out

    def test_cli_model_override_does_not_inherit_stale_profile_provider(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        claude = tmp_path / "claude"
        sessions = tmp_path / "sessions"
        self._write_model_profile(
            claude,
            {
                "mode": "custom",
                "provider": "openai",
                "model": "openai/gpt-5.5",
                "api_key_env": "OPENAI_API_KEY",
                "base_url": "https://api.example.test/v1",
            },
        )
        monkeypatch.setattr(run_cli, "_claude_dir", lambda: claude)
        monkeypatch.setenv("OPENAI_API_KEY", "profile-secret")

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/qwen",
                "--task",
                "say hi",
                "--sessions-dir",
                str(sessions),
                "--session-id",
                "override-run",
                "--no-ctx-tools",
                "--quiet",
            ],
        )

        assert exit_code == 0
        call = fake_litellm._calls[0]
        assert call["model"] == "ollama/qwen"
        assert "api_key" not in call
        assert "api_base" not in call
        first_line = (
            (sessions / "override-run.jsonl")
            .read_text(
                encoding="utf-8",
            )
            .splitlines()[0]
        )
        event = json.loads(first_line)
        assert event["provider"] == "ollama"
        assert event["api_key_env"] == ""
        assert event["base_url"] == ""

    def test_session_id_flag_pins_id(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "pinned-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert (tmp_path / "pinned-session.jsonl").is_file()

    def test_session_id_reuse_is_rejected_without_overwrite(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        path = tmp_path / "pinned-session.jsonl"
        path.write_text("sentinel\n", encoding="utf-8")
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "pinned-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert path.read_text(encoding="utf-8") == "sentinel\n"
        assert "already exists" in captured.err
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.run"
        ]
        assert len(events) == 1
        failed = events[0]
        assert failed.trace_id is not None
        assert failed.span_id is not None
        assert failed.outcome == "error"
        assert failed.error_kind == "FileExistsError"
        assert failed.payload["ctx.run.phase"] == "failed"
        assert failed.payload["ctx.run.failure_stage"] == "session_create"
        assert "hi" not in telemetry_path.read_text(encoding="utf-8")

    def test_session_id_reuse_can_overwrite_with_flag(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "pinned-session.jsonl"
        path.write_text("sentinel\n", encoding="utf-8")
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "pinned-session",
                "--overwrite-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert exit_code == 0
        assert "sentinel" not in path.read_text(encoding="utf-8")

    def test_metadata_recorded(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "openrouter/anthropic/claude",
                "--task",
                "task-content",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "meta-test",
                "--no-ctx-tools",
                "--budget-usd",
                "1.5",
                "--api-key-env",
                "CUSTOM_OPENROUTER_KEY",
                "--base-url",
                "https://openrouter.example/api",
                "--quiet",
            ]
        )
        path = tmp_path / "meta-test.jsonl"
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        event = json.loads(first_line)
        assert event["type"] == "session_start"
        assert event["task"] == "task-content"
        assert event["model"] == "openrouter/anthropic/claude"
        assert event["provider_prefix"] == "openrouter"
        assert event["provider"] == "openrouter"
        assert event["api_key_env"] == "CUSTOM_OPENROUTER_KEY"
        assert event["base_url"] == "https://openrouter.example/api"
        assert event["budget_usd"] == 1.5

    def test_tool_policy_metadata_recorded(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "policy-meta",
                "--allow-tool",
                "ctx__*",
                "--deny-tool",
                "ctx__wiki_get",
                "--quiet",
            ]
        )
        first_line = (tmp_path / "policy-meta.jsonl").read_text(encoding="utf-8").splitlines()[0]
        event = json.loads(first_line)
        assert event["tool_policy"] == {
            "allow": ["ctx__*"],
            "deny": ["ctx__wiki_get"],
        }

    def test_deny_tool_blocks_model_tool_call(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            return _tool_call_completion("ctx__wiki_get")

        fake_litellm.completion = completion
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "call denied tool",
                "--sessions-dir",
                str(tmp_path),
                "--ctx-tool-surface",
                "minimal",
                "--deny-tool",
                "ctx__wiki_get",
                "--json",
                "--quiet",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert payload["stop_reason"] == "tool_denied"
        assert "matched deny pattern" in payload["detail"]
        assert _submitted_tool_names(fake_litellm._calls[0]) == {"ctx__recommend_bundle"}

    @pytest.mark.parametrize(
        "stop_reason,final_message,detail",
        [
            ("length", "partial", "provider truncated response"),
            ("empty_response", "", "empty content with no tool calls"),
            ("provider_other", "partial", "unexpected finish_reason='other'"),
            ("content_filter", "", "provider reported content_filter finish"),
        ],
    )
    def test_abnormal_stop_reasons_exit_nonzero_in_json_mode(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stop_reason: str,
        final_message: str,
        detail: str,
    ) -> None:
        def fake_run_loop(**_kwargs: Any) -> types.SimpleNamespace:
            return types.SimpleNamespace(
                stop_reason=stop_reason,
                final_message=final_message,
                iterations=1,
                usage=Usage(input_tokens=5, output_tokens=1),
                messages=(),
                detail=detail,
            )

        monkeypatch.setattr(run_cli, "run_loop", fake_run_loop)

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "abnormal stop",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--json",
                "--quiet",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert payload["stop_reason"] == stop_reason
        assert payload["final_message"] == final_message
        assert payload["detail"] == detail
        assert payload["usage"] == {
            "tokens_reported": True,
            "input_tokens": 5,
            "output_tokens": 1,
            "total_tokens": 6,
            "cached_input_tokens": None,
            "uncached_input_tokens": None,
            "cost_usd": None,
        }

    def test_provider_timeout_reaches_real_run_loop(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            time.sleep(0.2)
            return {
                "choices": [
                    {
                        "message": {"content": "late", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        fake_litellm.completion = completion
        started = time.perf_counter()
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "timeout",
                "--sessions-dir",
                str(tmp_path),
                "--provider-timeout",
                "0.01",
                "--no-ctx-tools",
                "--json",
                "--quiet",
            ]
        )
        elapsed = time.perf_counter() - started

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert elapsed < 1.0
        assert payload["stop_reason"] == "provider_timeout"
        assert payload["detail"] == "provider call timed out after 0.010s"
        assert payload["usage"] == {
            "tokens_reported": False,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "uncached_input_tokens": None,
            "cost_usd": None,
        }

    def test_provider_error_is_valid_json_without_traceback(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)

        def completion(**kwargs: Any) -> None:
            fake_litellm._calls.append(kwargs)
            print("provider diagnostic")
            raise RuntimeError("provider unavailable authorization=sk-abcdefghijklmnopqrstuvwxyz")

        fake_litellm.completion = completion
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "provider error",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "provider-error-json",
                "--no-ctx-tools",
                "--json",
                "--quiet",
            ]
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert exit_code == 2
        assert captured.err == "provider diagnostic\n"
        assert payload["stop_reason"] == "provider_error"
        assert payload["detail"] == (
            "provider raised RuntimeError: provider unavailable authorization=[redacted]"
        )
        session_raw = (tmp_path / "provider-error-json.jsonl").read_text(encoding="utf-8")
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in session_raw
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in captured.out
        cli_events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.run"
        ]
        assert [event.payload["ctx.run.phase"] for event in cli_events] == [
            "started",
            "finished",
        ]
        finished = cli_events[-1]
        assert finished.payload["ctx.exception.message_hash"].startswith("sha256:")
        assert finished.payload["ctx.exception.stack_hash"].startswith("sha256:")
        assert finished.payload["ctx.exception.escaped"] is False

    def test_resume_provider_error_is_valid_json_without_traceback(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert (
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "initial",
                    "--sessions-dir",
                    str(tmp_path),
                    "--session-id",
                    "resume-provider-error-json",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )
            == 0
        )
        capsys.readouterr()

        def completion(**kwargs: Any) -> None:
            fake_litellm._calls.append(kwargs)
            print("resume provider diagnostic")
            raise RuntimeError("resume provider unavailable")

        fake_litellm.completion = completion
        exit_code = main(
            [
                "resume",
                "resume-provider-error-json",
                "--task",
                "retry",
                "--sessions-dir",
                str(tmp_path),
                "--json",
                "--quiet",
            ]
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert exit_code == 2
        assert captured.err == "resume provider diagnostic\n"
        assert payload["stop_reason"] == "provider_error"
        assert payload["detail"] == "provider raised RuntimeError: resume provider unavailable"

    def test_unrelated_exception_after_provider_stop_is_not_swallowed(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del fake_litellm
        provider_error = RuntimeError("provider unavailable")

        def fail_after_provider_stop(*_args: Any, **kwargs: Any) -> None:
            observer = kwargs["observer"]
            result = LoopResult(
                stop_reason="provider_error",
                final_message="",
                iterations=1,
                usage=Usage(tokens_reported=False),
                messages=(),
                detail="provider raised RuntimeError: provider unavailable",
            )
            observer.on_stop(result)
            observer.on_provider_failure(ProviderFailure(provider_error, result))
            raise RuntimeError("cleanup failed")

        monkeypatch.setattr(run_cli, "run_loop", fail_after_provider_stop)

        with pytest.raises(RuntimeError, match="cleanup failed"):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "provider error",
                    "--sessions-dir",
                    str(tmp_path),
                    "--session-id",
                    "provider-error-cleanup",
                    "--no-ctx-tools",
                    "--json",
                    "--quiet",
                ]
            )

    def test_json_output(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--json",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["stop_reason"] == "completed"
        assert payload["final_message"] == "final answer"
        assert "usage" in payload
        assert "session_id" in payload
        assert payload["usage_attribution"] == {
            "scope": "session",
            "attribution": "unavailable",
            "attribution_reason": run_cli._SESSION_USAGE_ATTRIBUTION_REASON,
            "tokens_reported": True,
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
            "cached_input_tokens": None,
            "uncached_input_tokens": None,
            "cost_usd": None,
        }

    def test_usage_telemetry_exposes_cached_and_uncached_tokens(self) -> None:
        result = types.SimpleNamespace(
            stop_reason="completed",
            iterations=1,
            usage=Usage(
                input_tokens=10,
                output_tokens=3,
                cost_usd=0.01,
                cached_input_tokens=6,
            ),
        )

        payload = run_cli._loop_result_payload(result)

        assert payload["ctx.usage.tokens_reported"] is True
        assert payload["ctx.usage.total_tokens"] == 13
        assert payload["ctx.usage.cached_input_tokens"] == 6
        assert payload["ctx.usage.uncached_input_tokens"] == 4
        assert payload["ctx.usage.cost_present"] is True

        invalid_cache = run_cli._usage_token_fields(
            Usage(input_tokens=5, output_tokens=1, cached_input_tokens=8)
        )
        assert invalid_cache["cached_input_tokens"] == 8
        assert invalid_cache["uncached_input_tokens"] is None

    def test_model_required(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(run_cli, "_claude_dir", lambda: tmp_path / "claude")

        assert main(["run", "--task", "hi"]) == 2
        assert "--model is required" in capsys.readouterr().err

    def test_missing_harness_extra_returns_friendly_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def missing_extra(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(
                "litellm is required for the generic harness provider. "
                "Install with: pip install 'claude-ctx[harness]'"
            )

        monkeypatch.setattr(run_cli, "run_loop", missing_extra)

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 2
        assert "claude-ctx[harness]" in captured.err
        assert "Traceback" not in captured.err

    def test_task_required(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit):
            main(["run", "--model", "ollama/x"])

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            ("--max-iterations", "0"),
            ("--max-tokens", "0"),
            ("--budget-tokens", "0"),
            ("--budget-usd", "0"),
            ("--evaluator-rounds", "0"),
        ],
    )
    def test_positive_numeric_flags_reject_zero(
        self,
        flag: str,
        value: str,
    ) -> None:
        with pytest.raises(SystemExit):
            main(["run", "--model", "ollama/x", "--task", "hi", flag, value])

    def test_invalid_session_id_returns_error(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "../bad",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "invalid session_id" in captured.err

    def test_no_ctx_tools_skips_extra_tools(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        # Check the call passed tools=None (or no tools).
        first_call = fake_litellm._calls[0]
        assert "tools" not in first_call  # loop passes None → omitted
        assert "ctx__" not in first_call["messages"][0]["content"]

    def test_non_ctx_allow_pattern_removes_ctx_prompt_and_schemas(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use only an attached server",
                "--sessions-dir",
                str(tmp_path),
                "--allow-tool",
                "server__*",
                "--quiet",
            ]
        )

        assert exit_code == 0
        first_call = fake_litellm._calls[0]
        assert "tools" not in first_call
        assert "ctx__" not in first_call["messages"][0]["content"]

    def test_ctx_tools_default_to_adaptive_host_surface(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(None)),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use ctx only if needed",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert len(fake_litellm._calls) == 1
        call = fake_litellm._calls[0]
        assert _submitted_tool_names(call) == set()
        assert "ctx__" not in call["messages"][0]["content"]
        metadata = json.loads(next((tmp_path.glob("*.jsonl"))).read_text().splitlines()[0])
        assert metadata["ctx_tool_surface"] == "adaptive"
        assert metadata["ctx_tool_names"] == []
        assert metadata["ctx_adaptive"]["enabled"] is True

    def test_adaptive_stays_zero_schema_when_secure_reads_unavailable(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "ctx.adapters.generic.adaptive_runtime.secure_skill_reads_available",
            lambda: False,
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use ctx",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert _submitted_tool_names(fake_litellm._calls[0]) == set()
        metadata = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[0])
        assert metadata["ctx_tool_surface"] == "adaptive"
        assert metadata["ctx_adaptive"]["enabled"] is True
        assert metadata["ctx_adaptive"]["skill_selected"] is False

    def test_adaptive_skill_is_request_only_and_not_persisted(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = "EPHEMERAL-CLI-SKILL-BODY"
        selected = SelectedSkill(
            name="focused-skill",
            content=body,
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            content_bytes=len(body.encode()),
            score=42.0,
            matched_terms=("focused",),
        )
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(selected, selection_duration_ms=1.5)),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use focused guidance",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "adaptive-ephemeral",
                "--quiet",
            ]
        )

        assert exit_code == 0
        call = fake_litellm._calls[0]
        assert _submitted_tool_names(call) == set()
        assert body in "\n".join(message["content"] for message in call["messages"])
        session_text = (tmp_path / "adaptive-ephemeral.jsonl").read_text(encoding="utf-8")
        assert body not in session_text
        metadata = run_cli.load_session(
            "adaptive-ephemeral",
            sessions_dir=tmp_path,
        ).metadata
        assert metadata["ctx_adaptive"]["selected_context_bytes"] > len(body.encode())
        assert (
            metadata["ctx_adaptive"]["submitted_context_bytes"]
            == metadata["ctx_adaptive"]["selected_context_bytes"]
        )
        assert metadata["ctx_adaptive"]["estimated_selected_context_tokens"] > 0
        assert metadata["ctx_adaptive"]["skill_hash"]

    def test_evaluator_reports_complete_usage(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        final = LoopResult(
            stop_reason="completed",
            final_message="done",
            iterations=1,
            usage=Usage(input_tokens=5, output_tokens=10),
            messages=(),
        )
        monkeypatch.setattr(
            run_cli,
            "run_with_evaluation",
            lambda **_kwargs: EvaluationLoopResult(
                final=final,
                rounds=(),
                plan=None,
                contract=None,
                total_usage=Usage(input_tokens=12, output_tokens=21),
            ),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "evaluate usage",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "evaluator-usage",
                "--no-ctx-tools",
                "--evaluator",
                "--json",
                "--quiet",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["usage"] == {
            "tokens_reported": True,
            "input_tokens": 12,
            "output_tokens": 21,
            "total_tokens": 33,
            "cached_input_tokens": None,
            "uncached_input_tokens": None,
            "cost_usd": None,
        }
        state = run_cli.load_session("evaluator-usage", sessions_dir=tmp_path)
        assert state.usage == Usage(input_tokens=12, output_tokens=21)
        events = [json.loads(line) for line in state.path.read_text().splitlines()]
        stop_events = [event for event in events if event["type"] == "stop"]
        assert len(stop_events) == 1
        assert stop_events[0]["usage"]["input_tokens"] == 12
        assert stop_events[0]["usage"]["output_tokens"] == 21

    def test_evaluator_failure_persists_one_stop_with_completed_round_usage(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        partial = LoopResult(
            stop_reason="completed",
            final_message="partial answer",
            iterations=2,
            usage=Usage(input_tokens=5, output_tokens=10),
            messages=(),
        )

        def fail_after_generator(**kwargs: Any) -> None:
            kwargs["observer"].on_stop(partial)
            raise RuntimeError("private evaluator failure")

        monkeypatch.setattr(run_cli, "run_with_evaluation", fail_after_generator)

        with pytest.raises(RuntimeError, match="private evaluator failure"):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "evaluate failure",
                    "--sessions-dir",
                    str(tmp_path),
                    "--session-id",
                    "evaluator-failure",
                    "--no-ctx-tools",
                    "--evaluator",
                    "--quiet",
                ]
            )

        state = run_cli.load_session("evaluator-failure", sessions_dir=tmp_path)
        assert state.stopped is True
        assert state.stop_reason == "provider_error"
        assert state.usage == Usage(input_tokens=5, output_tokens=10)
        assert state.metadata["ctx_usage"] == {
            "complete": False,
            "scope": "completed_generator_rounds",
        }
        events = [json.loads(line) for line in state.path.read_text().splitlines()]
        stop_events = [event for event in events if event["type"] == "stop"]
        assert len(stop_events) == 1
        assert stop_events[0]["detail"] == ("evaluator orchestration failed: RuntimeError")
        assert "private evaluator failure" not in state.path.read_text(encoding="utf-8")

    def test_cleanup_metadata_error_does_not_mask_evaluator_failure(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_evaluator(**_kwargs: Any) -> None:
            raise RuntimeError("primary evaluator failure")

        def fail_session_config(
            _store: run_cli.SessionStore,
            _config: dict[str, Any],
        ) -> None:
            raise KeyError("cleanup exploded")

        monkeypatch.setattr(run_cli, "run_with_evaluation", fail_evaluator)
        monkeypatch.setattr(
            run_cli.SessionStore,
            "write_session_config",
            fail_session_config,
        )

        with pytest.raises(RuntimeError, match="primary evaluator failure"):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "evaluate cleanup",
                    "--sessions-dir",
                    str(tmp_path),
                    "--session-id",
                    "evaluator-cleanup-failure",
                    "--no-ctx-tools",
                    "--evaluator",
                    "--quiet",
                ]
            )

    def test_explicit_minimal_surface_keeps_bootstrap_tools_on_every_iteration(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion("ctx__wiki_get")
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        fake_litellm.completion = completion
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use ctx only if needed",
                "--sessions-dir",
                str(tmp_path),
                "--ctx-tool-surface",
                "minimal",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert len(fake_litellm._calls) == 2
        assert all(
            _submitted_tool_names(call) == {"ctx__recommend_bundle", "ctx__wiki_get"}
            for call in fake_litellm._calls
        )
        prompt = fake_litellm._calls[0]["messages"][0]["content"]
        assert "ctx__recommend_bundle" in prompt
        assert "ctx__wiki_get" in prompt
        assert "ctx runtime session id:" not in prompt
        assert "ctx__mark_entity_used" not in prompt

    def test_allow_tool_submits_and_executes_only_selected_ctx_schema(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion(
                    "ctx__mark_entity_used",
                    {
                        "entity_type": "skill",
                        "slug": "focused-skill",
                        "evidence": "used in focused test",
                    },
                )
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        fake_litellm.completion = completion
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use the selected skill",
                "--sessions-dir",
                str(tmp_path / "sessions"),
                "--session-id",
                "selected-schema",
                "--allow-tool",
                "ctx__mark_entity_used",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert all(
            _submitted_tool_names(call) == {"ctx__mark_entity_used"} for call in fake_litellm._calls
        )
        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert "used" in [event["action"] for event in events]
        prompt = fake_litellm._calls[0]["messages"][0]["content"]
        assert "ctx__mark_entity_used.token_usage" in prompt
        assert "ctx__recommend_bundle" not in prompt
        assert "ctx__wiki_get" not in prompt
        assert "ctx__load_entity" not in prompt
        assert "ctx__unload_entity" not in prompt

    def test_full_ctx_tool_surface_preserves_previous_schema_inventory(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use full ctx tooling",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "full-schema",
                "--ctx-tool-surface",
                "full",
                "--quiet",
            ]
        )

        expected = {
            definition.name
            for definition in run_cli.CtxCoreToolbox(
                bound_session_id="full-schema"
            ).tool_definitions()
        }
        submitted = _submitted_tool_names(fake_litellm._calls[0])
        assert submitted == expected
        assert len(submitted) == 13
        metadata = json.loads(
            (tmp_path / "full-schema.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert metadata["ctx_tool_surface"] == "full"
        assert set(metadata["ctx_tool_names"]) == expected

    def test_system_prompt_override(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--system-prompt",
                "be terse",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        first_call = fake_litellm._calls[0]
        msgs = first_call["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "be terse"

    def test_system_prompt_from_stdin(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-prompt"))
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--system-prompt",
                "-",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        first_call = fake_litellm._calls[0]
        assert first_call["messages"][0]["content"] == "stdin-prompt"

    def test_runtime_lifecycle_events_recorded(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_events: list[dict[str, Any]] = []

        def capture_record_event(event_name: str, **kwargs: Any) -> None:
            telemetry_events.append({"event_name": event_name, **kwargs})

        monkeypatch.setattr(run_cli, "record_event", capture_record_event)
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path / "sessions"),
                "--session-id",
                "lifecycle-run",
                "--ctx-tool-surface",
                "full",
                "--quiet",
            ]
        )
        assert exit_code == 0
        capsys.readouterr()

        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "session_end",
        ]
        assert events[0]["session_id"] == "lifecycle-run"
        assert "task" not in events[0]["payload"]
        assert events[0]["payload"]["task_hash"].startswith("sha256:")
        assert "hi" not in json.dumps(events[0])
        cli_events = [event for event in telemetry_events if event["event_name"] == "ctx.cli.run"]
        assert [event["payload"]["ctx.run.phase"] for event in cli_events] == [
            "started",
            "finished",
        ]
        assert cli_events[0]["payload"]["ctx.task.length"] == len("hi")
        finished_payload = cli_events[-1]["payload"]
        assert finished_payload["ctx.stop_reason"] == "completed"
        assert finished_payload["ctx.usage.scope"] == "session"
        assert finished_payload["ctx.usage.attribution"] == "unavailable"
        assert (
            finished_payload["ctx.usage.attribution_reason"]
            == run_cli._SESSION_USAGE_ATTRIBUTION_REASON
        )
        assert "hi" not in json.dumps([event["payload"] for event in cli_events])
        system_prompt = fake_litellm._calls[0]["messages"][0]["content"]
        assert "ctx__mark_entity_used.token_usage" in system_prompt
        assert "do not allocate session totals across tools" in system_prompt
        tool = next(
            item
            for item in fake_litellm._calls[0]["tools"]
            if item["function"]["name"] == "ctx__load_entity"
        )
        assert "session_id" not in tool["function"]["parameters"]["properties"]
        assert "session_id" not in tool["function"]["parameters"]["required"]

    def test_run_telemetry_correlates_cli_and_runtime_lifecycle(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path / "sessions"),
                "--session-id",
                "trace-run",
                "--quiet",
            ]
        )

        assert exit_code == 0
        capsys.readouterr()
        events = list(read_events(telemetry_path, trusted_root=tmp_path))
        cli_events = [event for event in events if event.event_name == "ctx.cli.run"]
        lifecycle_events = [
            event for event in events if event.event_name == "ctx.runtime_lifecycle.record"
        ]

        assert [event.payload["ctx.run.phase"] for event in cli_events] == [
            "started",
            "finished",
        ]
        assert [event.payload["ctx.lifecycle.action"] for event in lifecycle_events] == [
            "dev_event",
            "session_end",
        ]
        assert cli_events[0].trace_id is not None
        assert cli_events[0].span_id is not None
        assert cli_events[1].trace_id == cli_events[0].trace_id
        assert cli_events[1].span_id == cli_events[0].span_id
        assert all(event.trace_id == cli_events[0].trace_id for event in lifecycle_events)
        assert all(event.parent_span_id == cli_events[0].span_id for event in lifecycle_events)
        assert {event.span_id for event in lifecycle_events}.isdisjoint({cli_events[0].span_id})
        assert "hi" not in telemetry_path.read_text(encoding="utf-8")

    def test_run_exception_telemetry_hashes_provider_error(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)

        def fail_run_loop(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("private provider failure for /Users/example/private-repo")

        monkeypatch.setattr(run_cli, "run_loop", fail_run_loop)

        with pytest.raises(RuntimeError):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "private run task",
                    "--sessions-dir",
                    str(tmp_path / "sessions"),
                    "--session-id",
                    "trace-run-error",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )

        capsys.readouterr()
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.run"
        ]
        assert [event.payload["ctx.run.phase"] for event in events] == [
            "started",
            "failed",
        ]
        failed = events[-1]
        assert failed.outcome == "error"
        assert failed.error_kind == "RuntimeError"
        assert failed.payload["ctx.exception.message_hash"].startswith("sha256:")
        assert failed.payload["ctx.exception.stack_hash"].startswith("sha256:")
        assert failed.payload["ctx.exception.escaped"] is True
        raw = telemetry_path.read_text(encoding="utf-8")
        assert "private provider failure" not in raw
        assert "/Users/example/private-repo" not in raw
        assert "private run task" not in raw

    def test_run_planner_failure_telemetry_has_trace_and_is_redacted(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)

        class FailingPlanner:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def plan(self, _task: str) -> None:
                raise RuntimeError("private planner failure for /Users/example/private-repo")

        monkeypatch.setattr(run_cli, "Planner", FailingPlanner)

        with pytest.raises(RuntimeError):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "private planner task",
                    "--sessions-dir",
                    str(tmp_path / "sessions"),
                    "--session-id",
                    "trace-planner-error",
                    "--planner",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )

        capsys.readouterr()
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.run"
        ]
        assert [event.payload["ctx.run.phase"] for event in events] == ["failed"]
        failed = events[0]
        assert failed.trace_id is not None
        assert failed.span_id is not None
        assert failed.outcome == "error"
        assert failed.error_kind == "RuntimeError"
        assert failed.payload["ctx.run.failure_stage"] == "planner"
        assert failed.payload["ctx.exception.message_hash"].startswith("sha256:")
        raw = telemetry_path.read_text(encoding="utf-8")
        assert "private planner failure" not in raw
        assert "/Users/example/private-repo" not in raw
        assert "private planner task" not in raw


# ── Subcommand: sessions ──────────────────────────────────────────────────


class TestSessionsCommand:
    def test_detail_missing_session_returns_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(["sessions", "not-there", "--sessions-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "session log not found" in captured.err

    def test_list_empty(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(["sessions", "--sessions-dir", str(tmp_path)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no sessions" in captured.out.lower()

    def test_list_after_run(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "listed",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()  # drop run output
        main(["sessions", "--sessions-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert "listed" in captured.out

    def test_list_json(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for sid in ("alpha", "beta"):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "hi",
                    "--sessions-dir",
                    str(tmp_path),
                    "--session-id",
                    sid,
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )
        capsys.readouterr()
        main(["sessions", "--sessions-dir", str(tmp_path), "--json"])
        captured = capsys.readouterr()
        ids = json.loads(captured.out)
        assert ids == ["alpha", "beta"]

    def test_detail_view(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "detail-task",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "detail",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        exit_code = main(["sessions", "detail", "--sessions-dir", str(tmp_path)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "detail" in captured.out
        assert "detail-task" in captured.out

    def test_detail_json(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "jdetail",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "jdetail",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        main(
            [
                "sessions",
                "jdetail",
                "--sessions-dir",
                str(tmp_path),
                "--json",
            ]
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["session_id"] == "jdetail"
        assert payload["metadata"]["task"] == "jdetail"


# ── Subcommand: resume ────────────────────────────────────────────────────


class TestResumeCommand:
    @staticmethod
    def _write_session_with_mcp(tmp_path: Path, session_id: str) -> None:
        (tmp_path / f"{session_id}.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": session_id,
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": False,
                    "mcp": [
                        {
                            "name": "danger",
                            "command": "definitely-not-a-real-mcp-command",
                            "args": ["--from-session"],
                            "credential_env": ["DANGER_TOKEN"],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_resume_after_initial_run(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Initial run.
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "resumable",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        # Resume.
        exit_code = main(
            [
                "resume",
                "resumable",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )
        assert exit_code == 0
        # Session file now has BOTH runs — count 'stop' events.
        text = (tmp_path / "resumable.jsonl").read_text(encoding="utf-8")
        stop_count = sum(
            1 for line in text.splitlines() if line and json.loads(line)["type"] == "stop"
        )
        assert stop_count == 2

    def test_resume_does_not_reopen_or_reproject_recorded_ctx_engine_mode(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        context, plan_digest = _closed_v3_recommendation()
        decision_calls: list[dict[str, Any]] = []

        def prepare_decision(**kwargs: Any) -> CommittedQueryDecision:
            decision_calls.append(kwargs)
            return _stub_ctx_run_decision(
                monkeypatch,
                status="presented",
                recommendation_context=context,
                recommendation_count=1,
                plan_digest=plan_digest,
            )

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )
        assert (
            main(
                _ctx_run_argv(
                    tmp_path,
                    session_id="recommend-resume",
                    task="initial task",
                    mode="recommend",
                )
            )
            == 0
        )
        capsys.readouterr()
        assert len(decision_calls) == 1
        assert context in "\n".join(
            message["content"] for message in fake_litellm._calls[0]["messages"]
        )
        fake_litellm._calls.clear()

        resume_exit = main(
            [
                "resume",
                "recommend-resume",
                "--task",
                "follow-up task",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert resume_exit == 0
        assert len(decision_calls) == 1
        assert len(fake_litellm._calls) == 1
        resumed_payload = "\n".join(
            message["content"] for message in fake_litellm._calls[0]["messages"]
        )
        assert context not in resumed_payload
        assert "follow-up task" in resumed_payload
        session_text = (tmp_path / "recommend-resume.jsonl").read_text(encoding="utf-8")
        assert context.splitlines()[0] not in session_text
        replay = load_session("recommend-resume", sessions_dir=tmp_path)
        assert all(context not in message.content for message in replay.messages)
        metadata = replay.metadata
        assert metadata["ctx_engine"]["requested_mode"] == "recommend"
        assert metadata["ctx_engine"]["status"] == "presented"

    @pytest.mark.parametrize("status", ("presented", "abstained"))
    def test_resume_of_successful_recommend_uses_nonlazy_mcp_without_legacy_adaptive(
        self,
        status: str,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        router_modes: list[bool] = []

        class FakeRouter:
            def __init__(
                self,
                configs: list[Any],
                *,
                session_id: str | None = None,
                lazy: bool = False,
            ) -> None:
                assert len(configs) == 1
                assert session_id == f"recommend-resume-{status}"
                self.started = False
                self.lazy = lazy
                router_modes.append(lazy)

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.started = False

            def list_tools(self) -> list[ToolDefinition]:
                assert self.started
                return [
                    ToolDefinition(
                        name="external__read",
                        description="read from the restored explicit MCP",
                        parameters={"type": "object", "properties": {}},
                    )
                ]

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                raise AssertionError(f"unexpected tool call: {name} {arguments}")

        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        adaptive_calls: list[dict[str, Any]] = []

        def unexpected_adaptive_controller(cls: type[Any], *args: Any, **kwargs: Any) -> object:
            adaptive_calls.append({"args": args, "kwargs": kwargs})
            raise AssertionError(
                "a recorded successful recommend session must not resume legacy adaptive"
            )

        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(unexpected_adaptive_controller),
        )
        presented_context, presented_digest = _closed_v3_recommendation()
        context = presented_context if status == "presented" else None
        plan_digest = (
            presented_digest
            if status == "presented"
            else hashlib.sha256(b"resume-abstention-plan").hexdigest()
        )
        decision_calls: list[dict[str, Any]] = []

        def prepare_decision(**kwargs: Any) -> CommittedQueryDecision:
            decision_calls.append(kwargs)
            return _stub_ctx_run_decision(
                monkeypatch,
                status=status,
                recommendation_context=context,
                recommendation_count=1 if status == "presented" else 0,
                plan_digest=plan_digest,
            )

        monkeypatch.setattr(
            run_cli,
            "prepare_ctx_run_query_decision",
            prepare_decision,
            raising=False,
        )
        session_id = f"recommend-resume-{status}"
        run_argv = _ctx_run_argv(
            tmp_path,
            session_id=session_id,
            task="initial recommendation task",
            mode="recommend",
            ctx_tool_surface="adaptive",
        )
        run_argv[-1:-1] = ["--mcp", "external:ignored-command"]
        assert main(run_argv) == 0
        assert len(decision_calls) == 1
        assert adaptive_calls == []
        assert router_modes == [False]
        fake_litellm._calls.clear()

        resume_exit = main(
            [
                "resume",
                session_id,
                "--task",
                "follow-up task",
                "--sessions-dir",
                str(tmp_path),
                "--restore-session-mcp",
                "--quiet",
            ]
        )

        assert resume_exit == 0
        assert len(decision_calls) == 1
        assert adaptive_calls == []
        assert router_modes == [False, False]
        assert len(fake_litellm._calls) == 1
        resumed_call = fake_litellm._calls[0]
        assert _submitted_tool_names(resumed_call) == {"external__read"}
        resumed_text = "\n".join(message["content"] for message in resumed_call["messages"])
        assert presented_context not in resumed_text
        metadata = load_session(session_id, sessions_dir=tmp_path).metadata["ctx_engine"]
        assert metadata["status"] == status
        assert metadata["plan_digest"] == plan_digest

    @pytest.mark.parametrize(
        "ctx_engine",
        (
            {"effective_mode": "recommend"},
            {
                "requested_mode": "legacy",
                "resolved_mode": "legacy",
                "effective_mode": "legacy",
                "status": "legacy",
                "circuit_breaker_tripped": False,
                "failure_code": None,
                "recommendation_count": 999,
                "plan_digest": "unexpected",
                "journal_revision": 2,
                "journal_record_digest": "unexpected",
                "release_root_digest": "unexpected",
                "release_sequence": 1,
                "catalog_mode": "unexpected",
            },
        ),
    )
    def test_resume_with_malformed_ctx_engine_metadata_suppresses_legacy_discovery(
        self,
        ctx_engine: dict[str, object],
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session_id = "malformed-engine-resume"
        (tmp_path / f"{session_id}.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": session_id,
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": True,
                    "ctx_tool_surface": "adaptive",
                    "system_prompt": run_cli._DEFAULT_SYSTEM_PROMPT,
                    "ctx_engine": ctx_engine,
                    "tool_policy": {
                        "allow": [],
                        "deny": ["ctx__recommend_bundle", "ctx__wiki_get"],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        adaptive_calls: list[dict[str, Any]] = []

        def unexpected_adaptive_controller(cls: type[Any], *args: Any, **kwargs: Any) -> object:
            adaptive_calls.append({"args": args, "kwargs": kwargs})
            raise AssertionError("malformed engine metadata must fail closed")

        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(unexpected_adaptive_controller),
        )

        exit_code = main(
            [
                "resume",
                session_id,
                "--task",
                "follow-up task",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert adaptive_calls == []
        assert len(fake_litellm._calls) == 1
        assert _submitted_tool_names(fake_litellm._calls[0]) == set()

    def test_legacy_session_without_surface_resumes_full_tool_inventory(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "legacy-surface.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "legacy-surface",
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": True,
                    "system_prompt": run_cli._LEGACY_DEFAULT_SYSTEM_PROMPT,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        exit_code = main(
            [
                "resume",
                "legacy-surface",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert len(_submitted_tool_names(fake_litellm._calls[-1])) == 13

    def test_exact_legacy_prompt_does_not_advertise_filtered_ctx_tools(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        legacy_prompt = (
            run_cli._LEGACY_DEFAULT_SYSTEM_PROMPT.rstrip()
            + "\n\nctx runtime session id: legacy-filtered\n"
            + "Use this exact session_id when calling ctx lifecycle tools. "
            + "Record ctx__load_entity and ctx__mark_entity_used when relevant.\n"
        )
        (tmp_path / "legacy-filtered.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "legacy-filtered",
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": True,
                    "system_prompt": legacy_prompt,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        exit_code = main(
            [
                "resume",
                "legacy-filtered",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--allow-tool",
                "server__*",
                "--quiet",
            ]
        )

        assert exit_code == 0
        call = fake_litellm._calls[-1]
        assert "tools" not in call
        assert "ctx__" not in call["messages"][0]["content"]

    def test_resume_surface_override_rewrites_prompt_and_persists(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "surface-resume",
                "--ctx-tool-surface",
                "full",
                "--quiet",
            ]
        )
        capsys.readouterr()
        fake_litellm._calls.clear()

        main(
            [
                "resume",
                "surface-resume",
                "--task",
                "switch to minimal",
                "--sessions-dir",
                str(tmp_path),
                "--ctx-tool-surface",
                "minimal",
                "--quiet",
            ]
        )
        capsys.readouterr()
        explicit_call = fake_litellm._calls[-1]
        assert _submitted_tool_names(explicit_call) == {
            "ctx__recommend_bundle",
            "ctx__wiki_get",
        }
        prompt = explicit_call["messages"][0]["content"]
        assert "ctx__recommend_bundle" in prompt
        assert "ctx__wiki_get" in prompt
        assert "ctx__load_entity" not in prompt
        assert "ctx__mark_entity_used" not in prompt
        assert "ctx__unload_entity" not in prompt

        fake_litellm._calls.clear()
        main(
            [
                "resume",
                "surface-resume",
                "--task",
                "inherit minimal",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )
        capsys.readouterr()
        assert _submitted_tool_names(fake_litellm._calls[-1]) == {
            "ctx__recommend_bundle",
            "ctx__wiki_get",
        }
        events = [
            json.loads(line)
            for line in (tmp_path / "surface-resume.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        config_events = [event for event in events if event["type"] == "session_config"]
        assert [event["ctx_tool_surface"] for event in config_events] == ["minimal", "minimal"]

    def test_resume_records_runtime_lifecycle_events(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_events: list[dict[str, Any]] = []

        def capture_record_event(event_name: str, **kwargs: Any) -> None:
            telemetry_events.append({"event_name": event_name, **kwargs})

        monkeypatch.setattr(run_cli, "record_event", capture_record_event)
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        sessions_dir = tmp_path / "sessions"
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(sessions_dir),
                "--session-id",
                "lifecycle-resume",
                "--ctx-tool-surface",
                "full",
                "--quiet",
            ]
        )
        capsys.readouterr()

        exit_code = main(
            [
                "resume",
                "lifecycle-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(sessions_dir),
                "--quiet",
            ]
        )
        assert exit_code == 0

        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "session_end",
            "dev_event",
            "session_end",
        ]
        assert events[2]["event_type"] == "resume_task"
        assert "task" not in events[2]["payload"]
        assert events[2]["payload"]["task_hash"].startswith("sha256:")
        assert "follow-up" not in json.dumps(events[2])
        resume_events = [
            event for event in telemetry_events if event["event_name"] == "ctx.cli.resume"
        ]
        assert [event["payload"]["ctx.run.phase"] for event in resume_events] == [
            "started",
            "finished",
        ]
        assert resume_events[0]["payload"]["ctx.messages.prior_count"] > 0
        assert resume_events[0]["payload"]["ctx.task.length"] == len("follow-up")
        assert resume_events[-1]["payload"]["ctx.stop_reason"] == "completed"
        assert "follow-up" not in json.dumps([event["payload"] for event in resume_events])
        resume_call = fake_litellm._calls[-1]
        tool = next(
            item
            for item in resume_call["tools"]
            if item["function"]["name"] == "ctx__session_state"
        )
        assert "session_id" not in tool["function"]["parameters"]["properties"]
        assert "session_id" not in tool["function"]["parameters"]["required"]

    def test_resume_telemetry_correlates_cli_and_runtime_lifecycle(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        sessions_dir = tmp_path / "sessions"
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(sessions_dir),
                "--session-id",
                "trace-resume",
                "--quiet",
            ]
        )
        capsys.readouterr()

        exit_code = main(
            [
                "resume",
                "trace-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(sessions_dir),
                "--quiet",
            ]
        )

        assert exit_code == 0
        capsys.readouterr()
        events = list(read_events(telemetry_path, trusted_root=tmp_path))
        run_cli_events = [event for event in events if event.event_name == "ctx.cli.run"]
        resume_cli_events = [event for event in events if event.event_name == "ctx.cli.resume"]
        lifecycle_events = [
            event for event in events if event.event_name == "ctx.runtime_lifecycle.record"
        ]
        resume_trace_id = resume_cli_events[0].trace_id
        resume_span_id = resume_cli_events[0].span_id
        resume_lifecycle_events = [
            event for event in lifecycle_events if event.trace_id == resume_trace_id
        ]

        assert [event.payload["ctx.run.phase"] for event in resume_cli_events] == [
            "started",
            "finished",
        ]
        assert [event.payload["ctx.lifecycle.action"] for event in resume_lifecycle_events] == [
            "dev_event",
            "session_end",
        ]
        assert resume_trace_id is not None
        assert resume_span_id is not None
        assert run_cli_events[0].trace_id is not None
        assert resume_cli_events[1].trace_id == resume_trace_id
        assert resume_cli_events[1].span_id == resume_span_id
        assert run_cli_events[0].trace_id != resume_trace_id
        assert all(
            event.payload["ctx.session.previous_trace_id"] == run_cli_events[0].trace_id
            for event in resume_cli_events
        )
        assert all(event.parent_span_id == resume_span_id for event in resume_lifecycle_events)
        session_events = [
            json.loads(line)
            for line in (sessions_dir / "trace-resume.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        session_start = next(event for event in session_events if event["type"] == "session_start")
        assert session_start["initial_trace_id"] == run_cli_events[0].trace_id
        assert "follow-up" not in telemetry_path.read_text(encoding="utf-8")

    def test_resume_exception_telemetry_hashes_provider_error(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        sessions_dir = tmp_path / "sessions"
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(sessions_dir),
                "--session-id",
                "trace-resume-error",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()

        def fail_run_loop(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("private resume provider failure for /Users/example/private-repo")

        monkeypatch.setattr(run_cli, "run_loop", fail_run_loop)

        with pytest.raises(RuntimeError):
            main(
                [
                    "resume",
                    "trace-resume-error",
                    "--task",
                    "private resume task",
                    "--sessions-dir",
                    str(sessions_dir),
                    "--quiet",
                ]
            )

        capsys.readouterr()
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.resume"
        ]
        assert [event.payload["ctx.run.phase"] for event in events] == [
            "started",
            "failed",
        ]
        failed = events[-1]
        assert failed.outcome == "error"
        assert failed.error_kind == "RuntimeError"
        assert failed.payload["ctx.exception.message_hash"].startswith("sha256:")
        assert failed.payload["ctx.exception.stack_hash"].startswith("sha256:")
        assert failed.payload["ctx.exception.escaped"] is True
        raw = telemetry_path.read_text(encoding="utf-8")
        assert "private resume provider failure" not in raw
        assert "/Users/example/private-repo" not in raw
        assert "private resume task" not in raw

    def test_runtime_lifecycle_respects_telemetry_disabled(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctx.adapters.generic.runtime_lifecycle as runtime_lifecycle

        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        monkeypatch.setattr(runtime_lifecycle, "telemetry_enabled", lambda: False)
        monkeypatch.setattr(
            runtime_lifecycle,
            "record_event",
            lambda *args, **kwargs: pytest.fail("disabled telemetry should not emit"),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "private task",
                "--sessions-dir",
                str(tmp_path / "sessions"),
                "--session-id",
                "lifecycle-disabled",
                "--quiet",
            ]
        )

        assert exit_code == 0
        capsys.readouterr()
        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "session_end",
        ]
        assert all(event["session_id"] == "lifecycle-disabled" for event in events)

    def test_resume_reuses_recorded_provider_settings(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LOCAL_KEY", "secret-value")
        main(
            [
                "run",
                "--model",
                "openai/local-model",
                "--task",
                "first",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "provider-resume",
                "--api-key-env",
                "LOCAL_KEY",
                "--base-url",
                "http://127.0.0.1:8000/v1",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        fake_litellm._calls.clear()

        exit_code = main(
            [
                "resume",
                "provider-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        resume_call = fake_litellm._calls[-1]
        assert resume_call["api_base"] == "http://127.0.0.1:8000/v1"
        assert resume_call["api_key"] == "secret-value"

    def test_resume_preserves_empty_recorded_system_prompt(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--system-prompt",
                "",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "empty-system",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        fake_litellm._calls.clear()

        exit_code = main(
            [
                "resume",
                "empty-system",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        resume_messages = fake_litellm._calls[-1]["messages"]
        assert [message["role"] for message in resume_messages] == [
            "user",
            "assistant",
            "user",
        ]
        message_events = [
            json.loads(line)
            for line in (tmp_path / "empty-system.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line)["type"] == "message"
        ]
        assert [event["content"] for event in message_events].count("final answer") == 2
        assert [event["content"] for event in message_events].count("follow-up") == 1

    def test_resume_inherits_recorded_tool_policy(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "policy-resume",
                "--deny-tool",
                "ctx__wiki_get",
                "--quiet",
            ]
        )
        capsys.readouterr()

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            return _tool_call_completion("ctx__wiki_get")

        fake_litellm.completion = completion
        exit_code = main(
            [
                "resume",
                "policy-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--json",
                "--quiet",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert payload["stop_reason"] == "tool_denied"

    def test_resume_counts_prior_usage_against_budget(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "budget-resume.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "budget-resume",
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": False,
                    "budget_tokens": 10,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "stop",
                    "ts": "t",
                    "session_id": "budget-resume",
                    "stop_reason": "completed",
                    "usage": {
                        "input_tokens": 6,
                        "output_tokens": 3,
                        "cost_usd": None,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        exit_code = main(
            [
                "resume",
                "budget-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--json",
                "--quiet",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["stop_reason"] == "token_budget"
        assert payload["usage"]["input_tokens"] == 11
        assert payload["usage"]["output_tokens"] == 6

    def test_resume_skips_recorded_mcp_by_default(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._write_session_with_mcp(tmp_path, "tampered")
        exit_code = main(
            [
                "resume",
                "tampered",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "recorded MCP server(s) skipped" in captured.err

    def test_resume_restores_recorded_mcp_only_with_flag(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        restored: list[Any] = []
        calls: list[str] = []
        router_session_ids: list[str | None] = []

        class FakeRouter:
            started = False

            def __init__(self, configs: list[Any], *, session_id: str | None = None) -> None:
                restored.extend(configs)
                router_session_ids.append(session_id)

            def start(self) -> None:
                calls.append("start")
                self.started = True

            def stop(self) -> None:
                calls.append("stop")
                self.started = False

            def list_tools(self) -> list[Any]:
                assert self.started
                calls.append("list_tools")
                return []

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                raise AssertionError(f"unexpected tool call: {name} {arguments}")

        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        self._write_session_with_mcp(tmp_path, "restore-mcp")
        exit_code = main(
            [
                "resume",
                "restore-mcp",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--restore-session-mcp",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert len(restored) == 1
        assert restored[0].command == "definitely-not-a-real-mcp-command"
        assert restored[0].credential_env == ("DANGER_TOKEN",)
        assert router_session_ids == ["restore-mcp"]
        assert "restoring MCP server danger" in captured.err
        assert calls == ["start", "list_tools", "stop"]

    def test_adaptive_resume_activates_only_selected_recorded_mcp(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        class FakeRouter:
            def __init__(
                self,
                configs: list[Any],
                *,
                session_id: str | None = None,
                lazy: bool = False,
            ) -> None:
                assert len(configs) == 2
                assert session_id == "adaptive-resume-mcp"
                assert lazy is True
                self.lazy = lazy
                self.active: tuple[str, ...] = ()

            @property
            def server_names(self) -> list[str]:
                return sorted(self.active)

            def start(self) -> None:
                calls.append("start")

            def stop(self) -> None:
                calls.append("stop")

            def list_tools(self) -> list[Any]:
                assert self.active == ()
                calls.append("list_tools")
                return []

            def activate(
                self,
                server_names: tuple[str, ...],
                *,
                capability_epoch: int | None = None,
            ) -> list[ToolDefinition]:
                assert server_names == ("danger",)
                assert capability_epoch == 1
                self.active = server_names
                calls.append("activate:danger")
                return [
                    ToolDefinition(name="danger__read", description="read", parameters={}),
                    ToolDefinition(name="danger__write", description="write", parameters={}),
                ]

            def deactivate(self, server_names: tuple[str, ...]) -> None:
                assert server_names == self.active
                calls.append("deactivate:danger")
                self.active = ()

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                assert self.active == ("danger",)
                assert name == "danger__read"
                calls.append("call:danger__read")
                return "restored result"

        (tmp_path / "adaptive-resume-mcp.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "adaptive-resume-mcp",
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": True,
                    "ctx_tool_surface": "adaptive",
                    "mcp": [
                        {
                            "name": "danger",
                            "command": "definitely-not-a-real-mcp-command",
                            "args": [],
                            "credential_env": [],
                        },
                        {
                            "name": "other",
                            "command": "also-not-a-real-mcp-command",
                            "args": [],
                            "credential_env": [],
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(None)),
        )

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion("danger__read")
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        fake_litellm.completion = completion

        exit_code = main(
            [
                "resume",
                "adaptive-resume-mcp",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--restore-session-mcp",
                "--allow-tool",
                "ctx__recommend_bundle",
                "--allow-tool",
                "danger__read",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert [_submitted_tool_names(call) for call in fake_litellm._calls] == [
            {"ctx__recommend_bundle", "danger__read"},
            set(),
        ]
        assert calls == [
            "start",
            "list_tools",
            "activate:danger",
            "call:danger__read",
            "deactivate:danger",
            "stop",
        ]
        metadata = run_cli.load_session("adaptive-resume-mcp", sessions_dir=tmp_path).metadata
        assert metadata["ctx_adaptive"]["mcp_configured_count"] == 2
        assert metadata["ctx_adaptive"]["mcp_activated_count"] == 1

    def test_resume_without_model_in_session_requires_flag(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        # Hand-write a session log with no 'model' in metadata.
        path = tmp_path / "no-model.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "no-model",
                    "task": "old",
                    "initial_trace_id": "original-trace-private",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "ts": "t",
                    "session_id": "no-model",
                    "role": "user",
                    "content": "hi",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        exit_code = main(["resume", "no-model", "--task", "go", "--sessions-dir", str(tmp_path)])
        assert exit_code == 1
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.resume"
        ]
        assert len(events) == 1
        failed = events[0]
        assert failed.trace_id is not None
        assert failed.span_id is not None
        assert failed.outcome == "error"
        assert failed.error_kind == "ValueError"
        assert failed.payload["ctx.run.phase"] == "failed"
        assert failed.payload["ctx.run.failure_stage"] == "validation"
        assert "ctx.session.previous_trace_id" not in failed.payload
        raw = telemetry_path.read_text(encoding="utf-8")
        assert "original-trace-private" not in raw
        assert "go" not in raw

    def test_resume_missing_session(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        exit_code = main(["resume", "not-there", "--task", "go", "--sessions-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "session log not found" in captured.err
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.resume"
        ]
        assert len(events) == 1
        failed = events[0]
        assert failed.trace_id is not None
        assert failed.span_id is not None
        assert failed.outcome == "error"
        assert failed.error_kind == "SessionLoadError"
        assert failed.payload["ctx.run.phase"] == "failed"
        assert failed.payload["ctx.run.failure_stage"] == "session_load"
        assert "go" not in telemetry_path.read_text(encoding="utf-8")
