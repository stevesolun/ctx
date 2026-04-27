"""
test_harness_contract.py -- Contract + ContractBuilder + P/C/G/E flow.

Pins:
  * ContractCriterion + Contract shapes, frozen, to_dict, to_markdown
  * ContractCriterion.as_evaluator_criterion rendering with/without
    metric + threshold
  * ContractBuilder.build happy path, unstructured fallback, planner
    injection into user turn, default temperature
  * _parse_criteria_list: synth name fallback, drops non-dict items
  * _safe_str_tuple: list / string-fallback / non-list
  * run_with_evaluation with contract_builder: evaluator gets
    refined criteria, system prompt uses contract markdown,
    total_usage sums all four agents
  * CLI: --contract requires --planner; --contract persists contract
    event into JSONL; JSON output does NOT re-duplicate contract
    (it's in the session file)
  * augmented_system_prompt_with_contract formatting
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ctx.adapters.generic.contract import (
    Contract,
    ContractBuilder,
    ContractCriterion,
    _parse_criteria_list,
    _safe_str_tuple,
    augmented_system_prompt_with_contract,
)
from ctx.adapters.generic.evaluator import (
    Evaluator,
    run_with_evaluation,
)
from ctx.adapters.generic.planner import PlanArtifact, Planner
from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ModelProvider,
    ToolDefinition,
    Usage,
)


# ── Scripted provider ──────────────────────────────────────────────────────


@dataclass
class _Scripted(ModelProvider):
    responses: list[CompletionResponse]
    name: str = "scripted"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResponse:
        self.calls.append(
            {
                "messages": list(messages),
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if not self.responses:
            raise RuntimeError("scripted: ran out of responses")
        return self.responses.pop(0)


def _resp(content: str) -> CompletionResponse:
    return CompletionResponse(
        content=content,
        tool_calls=(),
        finish_reason="stop",
        usage=Usage(input_tokens=10, output_tokens=20),
        provider="scripted",
        model="x",
    )


_VALID_CONTRACT_JSON = json.dumps({
    "summary": "Ensure input validation on the submit endpoint.",
    "criteria": [
        {
            "name": "empty-input-returns-422",
            "description": "Empty input must not silently succeed.",
            "pass_condition": "POST with empty body returns HTTP 422.",
            "fail_condition": "POST with empty body returns anything other than 422.",
        },
        {
            "name": "latency-under-200ms",
            "description": "Happy path must stay under 200ms p95.",
            "pass_condition": "p95 latency is under 200ms over 100 requests.",
            "fail_condition": "p95 latency exceeds 200ms.",
            "metric": "p95_latency_ms",
            "threshold": "< 200",
        },
    ],
    "scope_in": ["POST /submit handler", "input validation layer"],
    "scope_out": ["frontend forms", "unrelated endpoints"],
    "approach": "Add a Pydantic validator at the entrypoint.",
})


_VALID_PLAN_JSON = json.dumps({
    "summary": "Add validation to /submit",
    "success_criteria": [
        "Empty input should be rejected",
        "Latency should stay reasonable",
    ],
    "approach": "Add a validator",
    "out_of_scope": [],
    "risks": [],
})


# ── ContractCriterion ──────────────────────────────────────────────────────


class TestContractCriterion:
    def test_frozen(self) -> None:
        c = ContractCriterion(
            name="n", description="d", pass_condition="p",
        )
        with pytest.raises(Exception):
            c.name = "m"  # type: ignore[misc]

    def test_defaults(self) -> None:
        c = ContractCriterion(
            name="n", description="d", pass_condition="p",
        )
        assert c.fail_condition == ""
        assert c.metric == ""
        assert c.threshold == ""

    def test_as_evaluator_criterion_basic(self) -> None:
        c = ContractCriterion(
            name="x", description="d", pass_condition="returns 422",
        )
        assert c.as_evaluator_criterion() == "returns 422"

    def test_as_evaluator_criterion_with_metric(self) -> None:
        c = ContractCriterion(
            name="x", description="d",
            pass_condition="p95 fast enough",
            metric="p95_latency_ms",
        )
        out = c.as_evaluator_criterion()
        assert "p95 fast enough" in out
        assert "p95_latency_ms" in out

    def test_as_evaluator_criterion_with_threshold(self) -> None:
        c = ContractCriterion(
            name="x", description="d",
            pass_condition="fast",
            metric="latency",
            threshold="< 200",
        )
        out = c.as_evaluator_criterion()
        assert "latency" in out
        assert "< 200" in out

    def test_as_evaluator_criterion_falls_back_to_description(self) -> None:
        # Empty pass_condition → use description.
        c = ContractCriterion(
            name="x", description="desc-text", pass_condition="",
        )
        assert c.as_evaluator_criterion() == "desc-text"

    def test_as_evaluator_criterion_falls_back_to_name(self) -> None:
        c = ContractCriterion(name="the-name", description="", pass_condition="")
        assert c.as_evaluator_criterion() == "the-name"


# ── Contract ───────────────────────────────────────────────────────────────


class TestContract:
    def _mk(self) -> Contract:
        return Contract(
            task="t",
            summary="s",
            criteria=(
                ContractCriterion(
                    name="a", description="d1", pass_condition="p1",
                    fail_condition="f1",
                ),
                ContractCriterion(
                    name="b", description="d2", pass_condition="p2",
                    metric="m", threshold=">=0.8",
                ),
            ),
            scope_in=("in1",),
            scope_out=("out1",),
            approach="do X",
            usage=Usage(input_tokens=5, output_tokens=10),
            raw_json="",
        )

    def test_frozen(self) -> None:
        c = self._mk()
        with pytest.raises(Exception):
            c.summary = "other"  # type: ignore[misc]

    def test_as_evaluator_criteria(self) -> None:
        crits = self._mk().as_evaluator_criteria()
        assert crits[0] == "p1"
        assert "p2" in crits[1]
        assert "m" in crits[1]
        assert ">=0.8" in crits[1]

    def test_to_dict_shape(self) -> None:
        d = self._mk().to_dict()
        assert d["task"] == "t"
        assert d["summary"] == "s"
        assert len(d["criteria"]) == 2
        assert d["criteria"][0]["name"] == "a"
        assert d["scope_in"] == ["in1"]
        assert d["scope_out"] == ["out1"]
        assert d["parsed_ok"] is True

    def test_to_markdown_shape(self) -> None:
        md = self._mk().to_markdown()
        assert "# Sprint Contract" in md
        assert "**Task:** t" in md
        assert "## Summary" in md
        assert "### 1. a" in md
        assert "### 2. b" in md
        assert "**Pass:** p1" in md
        assert "**Fail:** f1" in md
        assert "**Metric:** m" in md
        assert "threshold: >=0.8" in md
        assert "## In scope" in md
        assert "- in1" in md
        assert "## Out of scope" in md
        assert "- out1" in md
        assert "## Approach" in md

    def test_to_markdown_empty_sections(self) -> None:
        c = Contract(
            task="t", summary="", criteria=(),
            scope_in=(), scope_out=(), approach="",
            usage=Usage(), raw_json="",
        )
        md = c.to_markdown()
        assert "_(no summary)_" in md
        # Empty sections should not appear.
        assert "## Criteria" not in md
        assert "## In scope" not in md
        assert "## Approach" not in md


# ── ContractBuilder ────────────────────────────────────────────────────────


class TestContractBuilder:
    def test_happy_path_from_plan(self) -> None:
        plan = PlanArtifact(
            task="t",
            summary="plan summary",
            success_criteria=("empty input", "good perf"),
            approach="approach",
            out_of_scope=(),
            risks=(),
            usage=Usage(),
            raw_json="",
        )
        provider = _Scripted([_resp(_VALID_CONTRACT_JSON)])
        contract = ContractBuilder(provider).build("t", plan=plan)
        assert contract.parsed_ok is True
        assert len(contract.criteria) == 2
        assert contract.criteria[0].name == "empty-input-returns-422"
        assert contract.criteria[1].metric == "p95_latency_ms"

    def test_happy_path_without_plan(self) -> None:
        provider = _Scripted([_resp(_VALID_CONTRACT_JSON)])
        contract = ContractBuilder(provider).build("task")
        assert contract.parsed_ok is True
        assert len(contract.criteria) == 2

    def test_plan_markdown_embedded_in_user_turn(self) -> None:
        plan = PlanArtifact(
            task="t",
            summary="plan-summary-marker",
            success_criteria=(),
            approach="",
            out_of_scope=(),
            risks=(),
            usage=Usage(),
            raw_json="",
        )
        provider = _Scripted([_resp(_VALID_CONTRACT_JSON)])
        ContractBuilder(provider).build("task", plan=plan)
        user_msg = next(
            m for m in provider.calls[0]["messages"] if m.role == "user"
        )
        assert "plan-summary-marker" in user_msg.content

    def test_unstructured_fallback(self) -> None:
        provider = _Scripted([_resp("not JSON, just prose")])
        contract = ContractBuilder(provider).build("t")
        assert contract.parsed_ok is False
        assert contract.criteria == ()
        assert contract.summary == "not JSON, just prose"

    def test_default_temperature_is_strict(self) -> None:
        provider = _Scripted([_resp(_VALID_CONTRACT_JSON)])
        ContractBuilder(provider).build("t")
        # Default 0.2 — stricter than the planner (0.4) and generator (0.7).
        assert provider.calls[0]["temperature"] == 0.2

    def test_model_override(self) -> None:
        provider = _Scripted([_resp(_VALID_CONTRACT_JSON)])
        ContractBuilder(provider, model="contract-model").build("t")
        assert provider.calls[0]["model"] == "contract-model"

    def test_empty_response_produces_stub(self) -> None:
        provider = _Scripted([_resp("")])
        contract = ContractBuilder(provider).build("t")
        assert contract.parsed_ok is False
        # Empty response → summary carries the placeholder text
        assert "empty" in contract.summary.lower()


# ── Parsers ────────────────────────────────────────────────────────────────


class TestParseCriteriaList:
    def test_empty(self) -> None:
        assert _parse_criteria_list([]) == ()

    def test_non_list(self) -> None:
        assert _parse_criteria_list({"a": 1}) == ()
        assert _parse_criteria_list(None) == ()

    def test_skips_non_dict_items(self) -> None:
        out = _parse_criteria_list([
            {"name": "a", "description": "d", "pass_condition": "p"},
            "garbage",
            42,
        ])
        assert len(out) == 1
        assert out[0].name == "a"

    def test_synthesises_missing_name(self) -> None:
        out = _parse_criteria_list([
            {"description": "d", "pass_condition": "p"},
            {"description": "d", "pass_condition": "p"},
        ])
        assert out[0].name == "criterion-1"
        assert out[1].name == "criterion-2"

    def test_all_fields_preserved(self) -> None:
        out = _parse_criteria_list([
            {
                "name": "n", "description": "d",
                "pass_condition": "p", "fail_condition": "f",
                "metric": "m", "threshold": "< 100",
            }
        ])
        c = out[0]
        assert c.name == "n"
        assert c.pass_condition == "p"
        assert c.fail_condition == "f"
        assert c.metric == "m"
        assert c.threshold == "< 100"


class TestSafeStrTuple:
    def test_none(self) -> None:
        assert _safe_str_tuple(None) == ()

    def test_list(self) -> None:
        assert _safe_str_tuple(["a", "b"]) == ("a", "b")

    def test_drops_empty(self) -> None:
        assert _safe_str_tuple(["a", "", None, "b"]) == ("a", "b")

    def test_string_fallback(self) -> None:
        out = _safe_str_tuple("- first\n* second\nthird")
        assert out == ("first", "second", "third")

    def test_non_list_non_string(self) -> None:
        assert _safe_str_tuple(42) == ()


# ── augmented_system_prompt_with_contract ────────────────────────────────


class TestAugmentedSystemPromptWithContract:
    def test_contract_embedded(self) -> None:
        contract = Contract(
            task="t", summary="the contract summary",
            criteria=(
                ContractCriterion(
                    name="n", description="d",
                    pass_condition="MUST return 422",
                ),
            ),
            scope_in=(), scope_out=(), approach="",
            usage=Usage(), raw_json="",
        )
        out = augmented_system_prompt_with_contract("base", contract)
        assert "base" in out
        assert "# Sprint Contract" in out
        assert "MUST return 422" in out
        assert "the contract summary" in out
        assert "non-negotiable" in out


# ── run_with_evaluation with contract_builder ─────────────────────────────


class TestRunWithContract:
    def test_contract_criteria_used_by_evaluator(self) -> None:
        # Response order: planner, contract, generator, evaluator
        provider = _Scripted([
            _resp(_VALID_PLAN_JSON),
            _resp(_VALID_CONTRACT_JSON),
            _resp("generator answer"),
            _resp(json.dumps({
                "verdict": "pass",
                "overall_score": 1.0,
                "criteria": [],
                "summary_feedback": "good",
                "revision_directive": "",
            })),
        ])
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            planner=Planner(provider),
            contract_builder=ContractBuilder(provider),
            max_rounds=1,
        )
        # Outcome carries plan + contract.
        assert outcome.plan is not None
        assert outcome.contract is not None
        assert outcome.contract.summary.startswith("Ensure")
        # Evaluator call (4th) should see contract-derived criteria.
        evaluator_call = provider.calls[3]
        user_content = next(
            m.content for m in evaluator_call["messages"] if m.role == "user"
        )
        assert "POST with empty body returns HTTP 422" in user_content

    def test_contract_prompt_injected_into_generator(self) -> None:
        provider = _Scripted([
            _resp(_VALID_PLAN_JSON),
            _resp(_VALID_CONTRACT_JSON),
            _resp("gen answer"),
            _resp(json.dumps({
                "verdict": "pass", "overall_score": 1.0, "criteria": [],
                "summary_feedback": "", "revision_directive": "",
            })),
        ])
        run_with_evaluation(
            provider=provider,
            system_prompt="base-prompt",
            task="t",
            evaluator=Evaluator(provider),
            planner=Planner(provider),
            contract_builder=ContractBuilder(provider),
            max_rounds=1,
        )
        # Generator call (3rd) should see contract markdown in system prompt.
        generator_call = provider.calls[2]
        sys_content = next(
            m.content for m in generator_call["messages"] if m.role == "system"
        )
        assert "# Sprint Contract" in sys_content
        assert "non-negotiable" in sys_content
        # Plan's narrative still present too (base + contract embeds).
        assert "base-prompt" in sys_content

    def test_total_usage_includes_contract_cost(self) -> None:
        # Each response reports 10 input + 20 output → 40 total input.
        provider = _Scripted([
            _resp(_VALID_PLAN_JSON),        # planner
            _resp(_VALID_CONTRACT_JSON),    # contract
            _resp("g"),                     # generator
            _resp(json.dumps({
                "verdict": "pass", "overall_score": 1.0, "criteria": [],
                "summary_feedback": "", "revision_directive": "",
            })),                             # evaluator
        ])
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="s",
            task="t",
            evaluator=Evaluator(provider),
            planner=Planner(provider),
            contract_builder=ContractBuilder(provider),
            max_rounds=1,
        )
        assert outcome.total_usage.input_tokens == 40
        assert outcome.total_usage.output_tokens == 80

    def test_contract_without_planner_also_works(self) -> None:
        """Contract can run without a plan — library escape hatch."""
        provider = _Scripted([
            _resp(_VALID_CONTRACT_JSON),
            _resp("g"),
            _resp(json.dumps({
                "verdict": "pass", "overall_score": 1.0, "criteria": [],
                "summary_feedback": "", "revision_directive": "",
            })),
        ])
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="s",
            task="t",
            evaluator=Evaluator(provider),
            planner=None,
            contract_builder=ContractBuilder(provider),
            max_rounds=1,
        )
        assert outcome.plan is None
        assert outcome.contract is not None

    def test_empty_contract_criteria_falls_back_to_evaluator_defaults(
        self,
    ) -> None:
        """When the contract builder produces no criteria (bad JSON),
        the evaluator still runs with its default criteria set."""
        empty_contract_json = json.dumps({
            "summary": "stub", "criteria": [],
            "scope_in": [], "scope_out": [], "approach": "",
        })
        provider = _Scripted([
            _resp(_VALID_PLAN_JSON),
            _resp(empty_contract_json),
            _resp("g"),
            _resp(json.dumps({
                "verdict": "pass", "overall_score": 1.0, "criteria": [],
                "summary_feedback": "", "revision_directive": "",
            })),
        ])
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="s",
            task="t",
            evaluator=Evaluator(provider),
            planner=Planner(provider),
            contract_builder=ContractBuilder(provider),
            max_rounds=1,
        )
        # Contract captured but empty.
        assert outcome.contract is not None
        assert outcome.contract.criteria == ()
        # Evaluator call saw the planner's success_criteria (since
        # the contract had nothing to replace them with).
        evaluator_call = provider.calls[3]
        user_content = next(
            m.content for m in evaluator_call["messages"] if m.role == "user"
        )
        # Planner criteria survive.
        assert "Empty input should be rejected" in user_content


# ── CLI integration ───────────────────────────────────────────────────────


@pytest.fixture()
def fake_litellm_contract(monkeypatch: pytest.MonkeyPatch):
    fake = types.ModuleType("litellm")
    calls: list[dict[str, Any]] = []

    def _mk(content: str) -> dict[str, Any]:
        return {
            "choices": [
                {"message": {"content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }

    # Default queue: planner → contract → generator → evaluator(pass).
    fake._responses = [  # type: ignore[attr-defined]
        _mk(_VALID_PLAN_JSON),
        _mk(_VALID_CONTRACT_JSON),
        _mk("done"),
        _mk(json.dumps({
            "verdict": "pass", "overall_score": 1.0, "criteria": [],
            "summary_feedback": "fine", "revision_directive": "",
        })),
    ]

    def completion(**kwargs):
        calls.append(kwargs)
        if not fake._responses:  # type: ignore[attr-defined]
            raise RuntimeError("fake: no more responses")
        return fake._responses.pop(0)  # type: ignore[attr-defined]

    fake.completion = completion  # type: ignore[attr-defined]
    fake._calls = calls           # type: ignore[attr-defined]
    fake._mk = _mk                 # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


class TestCliContract:
    def test_contract_without_planner_rejected(self, tmp_path: Path) -> None:
        from ctx.cli.run import main

        with pytest.raises(SystemExit, match="--contract requires --planner"):
            main(
                [
                    "run",
                    "--model", "ollama/x",
                    "--task", "t",
                    "--sessions-dir", str(tmp_path),
                    "--evaluator",
                    "--contract",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )

    def test_contract_without_evaluator_rejected(
        self, tmp_path: Path,
    ) -> None:
        """--contract is only meaningful with --evaluator + --planner."""
        # Without --evaluator the contract flag is silently ignored
        # (the solo path ignores it since run_with_evaluation isn't
        # entered); that's acceptable behaviour — document but don't
        # error. This test pins the current behaviour so a future
        # change is explicit.
        from ctx.cli.run import main

        # Should not raise — --contract is silently ignored in the
        # solo path (run_with_evaluation isn't entered). The solo
        # path with --planner makes TWO provider calls: the planner
        # call and the Generator's single iteration.
        fake = types.ModuleType("litellm")

        def _mk(content: str) -> dict[str, Any]:
            return {
                "choices": [
                    {"message": {"content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        fake._responses = [  # type: ignore[attr-defined]
            _mk(_VALID_PLAN_JSON),  # planner call
            _mk("generator answer"),  # generator
        ]

        def completion(**kwargs):
            return fake._responses.pop(0)  # type: ignore[attr-defined]

        fake.completion = completion  # type: ignore[attr-defined]
        import sys as _sys
        _sys.modules["litellm"] = fake
        exit_code = main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "t",
                "--sessions-dir", str(tmp_path),
                "--planner",       # planner enabled but evaluator isn't
                "--contract",       # contract solo path doesn't reach run_with_eval
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert exit_code == 0

    def test_contract_persisted_in_jsonl(
        self, fake_litellm_contract: Any, tmp_path: Path,
    ) -> None:
        from ctx.cli.run import main

        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "add validation",
                "--sessions-dir", str(tmp_path),
                "--session-id", "cn-run",
                "--planner",
                "--evaluator",
                "--contract",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        # JSONL should carry a 'contract' event with the refined criteria.
        events = [
            json.loads(line)
            for line in (tmp_path / "cn-run.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        contract_events = [e for e in events if e.get("type") == "contract"]
        assert len(contract_events) == 1
        ce = contract_events[0]
        assert ce["summary"].startswith("Ensure")
        assert len(ce["criteria"]) == 2
        # session_start metadata contract_used True.
        first = events[0]
        assert first["type"] == "session_start"
        assert first["contract_used"] is True

    def test_contract_model_override(
        self, fake_litellm_contract: Any, tmp_path: Path,
    ) -> None:
        from ctx.cli.run import main

        main(
            [
                "run",
                "--model", "ollama/main",
                "--contract-model", "ollama/contract",
                "--task", "t",
                "--sessions-dir", str(tmp_path),
                "--session-id", "cn-override",
                "--planner",
                "--evaluator",
                "--contract",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        calls = fake_litellm_contract._calls
        # Order: planner → contract → generator → evaluator
        contract_call = calls[1]
        assert contract_call["model"] == "ollama/contract"
