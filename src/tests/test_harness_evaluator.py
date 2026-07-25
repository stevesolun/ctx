"""
test_harness_evaluator.py -- Evaluator + run_with_evaluation tests.

Pins:
  * CriterionResult + EvaluationResult + EvaluationRound shapes
  * _coerce_verdict + _coerce_score + _parse_criteria + _safe_str
  * Evaluator.evaluate happy path, unstructured fallback, criteria
    injection, context threading
  * Evaluator.with_criteria returns a new instance with shared
    config except the criteria
  * _build_revision_task with directive/feedback permutations
  * run_with_evaluation:
      - Single-round pass completes without revision
      - Revision round triggered on needs_revision verdict
      - max_rounds cap respected
      - Planner integration (spec criteria replace defaults)
      - Generator non-completed → evaluator skipped
      - Total usage sums across all agent calls
  * CLI integration via fake litellm: --evaluator round counts land
    in session metadata + final JSON output
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ctx.adapters.generic.evaluator import (
    CriterionResult,
    EvaluationLoopResult,
    EvaluationResult,
    EvaluationRound,
    Evaluator,
    _build_revision_task,
    _coerce_score,
    _coerce_verdict,
    _parse_criteria,
    _UsageTotals,
    run_with_evaluation,
)
from ctx.adapters.generic.loop import LoopResult, TurnPreparation
from ctx.adapters.generic.planner import Planner
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
                "tools": list(tools) if tools else None,
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


_PASS_JSON = json.dumps(
    {
        "verdict": "pass",
        "overall_score": 0.9,
        "criteria": [
            {"name": "addresses task", "passed": True, "score": 1.0, "note": "yes"},
        ],
        "summary_feedback": "Complete and correct.",
        "revision_directive": "",
    }
)

_NEEDS_REVISION_JSON = json.dumps(
    {
        "verdict": "needs_revision",
        "overall_score": 0.5,
        "criteria": [
            {"name": "addresses task", "passed": False, "score": 0.4, "note": "missed edge case"},
        ],
        "summary_feedback": "Partial answer; missed the empty-input case.",
        "revision_directive": "Handle the empty-input case explicitly.",
    }
)

_FAIL_JSON = json.dumps(
    {
        "verdict": "fail",
        "overall_score": 0.1,
        "criteria": [],
        "summary_feedback": "Off-topic.",
        "revision_directive": "Re-read the task.",
    }
)

_PLAN_JSON = json.dumps(
    {
        "summary": "Add input validation",
        "success_criteria": ["Reject empty input", "Return error code 422"],
        "approach": "Add a guard at function entry",
        "out_of_scope": [],
        "risks": [],
    }
)


# ── Data-shape pinning ─────────────────────────────────────────────────────


class TestDataShapes:
    def test_criterion_result_frozen(self) -> None:
        c = CriterionResult(name="x", passed=True, score=1.0, note="n")
        with pytest.raises(Exception):
            c.score = 0.5  # type: ignore[misc]

    def test_evaluation_result_to_dict(self) -> None:
        r = EvaluationResult(
            verdict="pass",
            overall_score=0.95,
            criterion_results=(CriterionResult(name="a", passed=True, score=1.0, note="ok"),),
            summary_feedback="fine",
            revision_directive="",
            usage=Usage(input_tokens=5, output_tokens=10),
            raw_json="",
        )
        d = r.to_dict()
        assert d["verdict"] == "pass"
        assert d["overall_score"] == 0.95
        assert d["criteria"][0]["name"] == "a"

    def test_evaluation_round_frozen(self) -> None:
        from ctx.adapters.generic.loop import LoopResult

        r = EvaluationRound(
            index=1,
            loop_result=LoopResult(
                stop_reason="completed",
                final_message="done",
                iterations=1,
                usage=Usage(),
                messages=(),
                detail="",
            ),
            evaluation=EvaluationResult(
                verdict="pass",
                overall_score=1.0,
                criterion_results=(),
                summary_feedback="",
                revision_directive="",
                usage=Usage(),
                raw_json="",
            ),
            revision_task="",
        )
        with pytest.raises(Exception):
            r.index = 5  # type: ignore[misc]


# ── Coercers ───────────────────────────────────────────────────────────────


class TestCoerceVerdict:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("pass", "pass"),
            ("passed", "pass"),
            ("OK", "pass"),
            ("approved", "pass"),
            ("needs_revision", "needs_revision"),
            ("needs-revision", "needs_revision"),
            ("revise", "needs_revision"),
            ("partial", "needs_revision"),
            ("fail", "fail"),
            ("failed", "fail"),
            ("garbage", "fail"),
            ("", "fail"),
            (None, "fail"),
            (42, "fail"),
        ],
    )
    def test_mappings(self, raw: Any, expected: str) -> None:
        assert _coerce_verdict(raw) == expected


class TestCoerceScore:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0.5, 0.5),
            ("0.7", 0.7),
            (1.5, 1.0),  # clamp high
            (-0.3, 0.0),  # clamp low
            (None, 0.0),
            ("garbage", 0.0),
        ],
    )
    def test_values(self, raw: Any, expected: float) -> None:
        assert _coerce_score(raw) == pytest.approx(expected)


class TestParseCriteria:
    def test_none(self) -> None:
        assert _parse_criteria(None) == ()

    def test_not_list(self) -> None:
        assert _parse_criteria({"a": 1}) == ()

    def test_skips_non_dict_items(self) -> None:
        result = _parse_criteria([{"name": "x", "passed": True}, "garbage"])
        assert len(result) == 1

    def test_happy_path(self) -> None:
        result = _parse_criteria(
            [
                {"name": "a", "passed": True, "score": 1.0, "note": "ok"},
                {"name": "b", "passed": False, "score": 0.2, "note": "nope"},
            ]
        )
        assert len(result) == 2
        assert result[0].name == "a"
        assert result[1].passed is False


# ── Evaluator ─────────────────────────────────────────────────────────────


class TestEvaluator:
    def test_default_criteria(self) -> None:
        ev = Evaluator(_Scripted([_resp(_PASS_JSON)]))
        assert len(ev.criteria) == 3

    def test_custom_criteria(self) -> None:
        ev = Evaluator(
            _Scripted([_resp(_PASS_JSON)]),
            criteria=["crit1", "crit2"],
        )
        assert ev.criteria == ("crit1", "crit2")

    def test_with_criteria_returns_new_instance(self) -> None:
        ev = Evaluator(_Scripted([]))
        new_ev = ev.with_criteria(["a", "b"])
        assert new_ev is not ev
        assert new_ev.criteria == ("a", "b")

    def test_happy_path(self) -> None:
        provider = _Scripted([_resp(_PASS_JSON)])
        ev = Evaluator(provider)
        result = ev.evaluate(task="do X", answer="did X")
        assert result.verdict == "pass"
        assert result.overall_score == 0.9
        assert len(result.criterion_results) == 1
        assert result.summary_feedback == "Complete and correct."

    def test_unstructured_response_maps_to_fail(self) -> None:
        provider = _Scripted([_resp("This is not JSON.")])
        result = Evaluator(provider).evaluate(task="t", answer="a")
        assert result.verdict == "fail"
        assert result.parsed_ok is False
        assert result.summary_feedback == "This is not JSON."
        assert "Rework" in result.revision_directive

    def test_criteria_injected_into_prompt(self) -> None:
        provider = _Scripted([_resp(_PASS_JSON)])
        ev = Evaluator(provider, criteria=["be concise", "be factual"])
        ev.evaluate(task="t", answer="a")
        user_msg = next(m for m in provider.calls[0]["messages"] if m.role == "user")
        assert "be concise" in user_msg.content
        assert "be factual" in user_msg.content

    def test_context_threaded(self) -> None:
        provider = _Scripted([_resp(_PASS_JSON)])
        Evaluator(provider).evaluate(
            task="t",
            answer="a",
            context="project background",
        )
        user_msg = next(m for m in provider.calls[0]["messages"] if m.role == "user")
        assert "project background" in user_msg.content

    def test_empty_answer_handled(self) -> None:
        provider = _Scripted([_resp(_FAIL_JSON)])
        result = Evaluator(provider).evaluate(task="t", answer="")
        user_msg = next(m for m in provider.calls[0]["messages"] if m.role == "user")
        assert "(empty)" in user_msg.content
        assert result.verdict == "fail"

    def test_model_override(self) -> None:
        provider = _Scripted([_resp(_PASS_JSON)])
        Evaluator(provider, model="eval-model").evaluate(task="t", answer="a")
        assert provider.calls[0]["model"] == "eval-model"

    def test_temperature_default_is_strict(self) -> None:
        provider = _Scripted([_resp(_PASS_JSON)])
        Evaluator(provider).evaluate(task="t", answer="a")
        # Evaluator default is 0.3 — stricter than Generator's 0.7.
        assert provider.calls[0]["temperature"] == 0.3


# ── _build_revision_task ──────────────────────────────────────────────────


class TestBuildRevisionTask:
    def _eval(self, feedback: str, directive: str) -> EvaluationResult:
        return EvaluationResult(
            verdict="needs_revision",
            overall_score=0.5,
            criterion_results=(),
            summary_feedback=feedback,
            revision_directive=directive,
            usage=Usage(),
            raw_json="",
        )

    def test_both_present(self) -> None:
        out = _build_revision_task(
            "original",
            self._eval("missed edge case", "handle empty input"),
        )
        assert "missed edge case" in out
        assert "handle empty input" in out
        assert "original" in out
        assert "Produce a revised answer." in out

    def test_feedback_only(self) -> None:
        out = _build_revision_task(
            "original",
            self._eval("not detailed enough", ""),
        )
        assert "not detailed enough" in out
        assert "Directive" not in out

    def test_directive_only(self) -> None:
        out = _build_revision_task(
            "original",
            self._eval("", "add a test for edge case"),
        )
        assert "add a test for edge case" in out
        assert "Feedback" not in out

    def test_neither_gives_generic_prompt(self) -> None:
        out = _build_revision_task("original", self._eval("", ""))
        assert "no actionable feedback" in out.lower()
        assert "Original task: original" in out


# ── run_with_evaluation ──────────────────────────────────────────────────


class TestRunWithEvaluation:
    def test_single_round_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ctx.adapters.generic import evaluator as evaluator_module
        from ctx.adapters.generic.loop import LoopResult

        captured: dict[str, Any] = {}

        def fake_run_loop(**kwargs: Any) -> LoopResult:
            captured.update(kwargs)
            return LoopResult(
                stop_reason="completed",
                final_message="final answer",
                iterations=1,
                usage=Usage(input_tokens=10, output_tokens=20),
                messages=(Message(role="assistant", content="final answer"),),
                detail="",
            )

        monkeypatch.setattr(evaluator_module, "run_loop", fake_run_loop)
        provider = _Scripted([_resp(_PASS_JSON)])
        evaluator = Evaluator(provider)
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=evaluator,
            max_rounds=2,
            provider_timeout=3.25,
            turn_prepare_timeout=0.25,
            max_ephemeral_context_bytes=123,
            max_turn_tools=4,
            max_turn_schema_bytes=456,
        )
        assert isinstance(outcome, EvaluationLoopResult)
        assert len(outcome.rounds) == 1
        assert outcome.rounds[0].evaluation.verdict == "pass"
        assert outcome.final.stop_reason == "completed"
        assert outcome.final.final_message == "final answer"
        assert captured["provider_timeout"] == 3.25
        assert captured["turn_prepare_timeout"] == 0.25
        assert captured["max_ephemeral_context_bytes"] == 123
        assert captured["max_turn_tools"] == 4
        assert captured["max_turn_schema_bytes"] == 456

    def test_needs_revision_triggers_second_round(self) -> None:
        # Round 1: gen -> evaluator (needs_revision). Round 2: gen -> eval (pass).
        class _Controller:
            prepared_turns = 0

            def prepare_turn(
                self,
                iteration,
                messages,
                base_tools,
                *,
                deadline_monotonic,
                cancel_event,
            ):
                self.prepared_turns += 1
                return TurnPreparation(capability_epoch=self.prepared_turns)

            def authorize_tool_call(self, iteration, capability_epoch, call):
                return None

            def on_tool_result(
                self,
                iteration,
                capability_epoch,
                call,
                result,
                error,
            ):
                return None

            def close_turn(self, iteration, capability_epoch, outcome):
                return None

        provider = _Scripted(
            [
                _resp("first attempt"),
                _resp(_NEEDS_REVISION_JSON),
                _resp("revised attempt"),
                _resp(_PASS_JSON),
            ]
        )
        controller = _Controller()
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            max_rounds=2,
            turn_controller=controller,
        )
        assert len(outcome.rounds) == 2
        assert outcome.rounds[0].evaluation.verdict == "needs_revision"
        assert outcome.rounds[1].evaluation.verdict == "pass"
        assert outcome.final.final_message == "revised attempt"
        assert controller.prepared_turns == 2

    def test_max_rounds_cap(self) -> None:
        # Every evaluator call says needs_revision.
        # With max_rounds=2, we expect 2 gen + 2 eval = 4 calls total.
        provider = _Scripted(
            [
                _resp("try 1"),
                _resp(_NEEDS_REVISION_JSON),
                _resp("try 2"),
                _resp(_NEEDS_REVISION_JSON),
            ]
        )
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            max_rounds=2,
        )
        assert len(outcome.rounds) == 2
        # Final verdict still needs_revision — cap reached.
        assert outcome.rounds[-1].evaluation.verdict == "needs_revision"

    def test_max_rounds_one_is_grade_only(self) -> None:
        # With max_rounds=1, a single gen+eval happens and the grade
        # is returned even if it's needs_revision — no extra gen call.
        provider = _Scripted(
            [
                _resp("first"),
                _resp(_NEEDS_REVISION_JSON),
            ]
        )
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            max_rounds=1,
        )
        assert len(outcome.rounds) == 1
        assert outcome.rounds[0].evaluation.verdict == "needs_revision"

    def test_max_rounds_zero_rejected(self) -> None:
        provider = _Scripted([])
        with pytest.raises(ValueError, match="max_rounds"):
            run_with_evaluation(
                provider=provider,
                system_prompt="sys",
                task="t",
                evaluator=Evaluator(provider),
                max_rounds=0,
            )

    def test_more_than_one_revision_is_rejected(self) -> None:
        provider = _Scripted([])
        with pytest.raises(ValueError, match="max_rounds must be <= 2"):
            run_with_evaluation(
                provider=provider,
                system_prompt="sys",
                task="t",
                evaluator=Evaluator(provider),
                max_rounds=3,
            )

    def test_with_planner_replaces_evaluator_criteria(self) -> None:
        # Planner call → 1, Generator → 1, Evaluator → 1
        provider = _Scripted(
            [
                _resp(_PLAN_JSON),
                _resp("answer"),
                _resp(_PASS_JSON),
            ]
        )
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            planner=Planner(provider),
            max_rounds=1,
        )
        assert outcome.plan is not None
        assert outcome.plan.summary == "Add input validation"
        # Evaluator saw the plan's success_criteria. Check the
        # 3rd call (evaluator) used the plan criteria.
        evaluator_call = provider.calls[2]
        user_content = next(m.content for m in evaluator_call["messages"] if m.role == "user")
        assert "Reject empty input" in user_content
        assert "Return error code 422" in user_content

    def test_generator_non_completion_skips_evaluator(self) -> None:
        """A Generator that hits max_iterations is graded as 'fail'
        without making an evaluator call — there's nothing coherent
        to grade."""
        # Generator never stops: returns a tool call with no executor
        # → tool_error stop on first iter.
        from ctx.adapters.generic.providers import ToolCall

        loop_response = CompletionResponse(
            content="",
            tool_calls=(ToolCall(id="c1", name="x__y", arguments={}),),
            finish_reason="tool_calls",
            usage=Usage(),
            provider="scripted",
            model="x",
        )
        # Only one response in the queue — no evaluator response.
        provider = _Scripted([loop_response])
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            max_rounds=2,
        )
        assert len(outcome.rounds) == 1
        assert outcome.rounds[0].loop_result.stop_reason == "tool_error"
        assert outcome.rounds[0].evaluation.verdict == "fail"
        # Only 1 provider call made — no evaluator call.
        assert len(provider.calls) == 1

    def test_total_usage_sums_across_agents(self) -> None:
        provider = _Scripted(
            [
                _resp(_PLAN_JSON),  # planner
                _resp("answer"),  # generator
                _resp(_NEEDS_REVISION_JSON),  # evaluator round 1
                _resp("revised"),  # generator round 2
                _resp(_PASS_JSON),  # evaluator round 2
            ]
        )
        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            planner=Planner(provider),
            max_rounds=2,
        )
        # 5 calls, each reporting 10 input + 20 output = 50 + 100
        total = outcome.total_usage
        assert total.input_tokens == 50
        assert total.output_tokens == 100

    def test_shared_budget_stops_after_planner_without_generator_call(self) -> None:
        provider = _Scripted([_resp(_PLAN_JSON)])

        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            planner=Planner(provider),
            budget_tokens=30,
        )

        assert len(provider.calls) == 1
        assert outcome.final.stop_reason == "token_budget"
        assert outcome.total_usage.input_tokens + outcome.total_usage.output_tokens == 30

    def test_shared_budget_prevents_revision_after_evaluator(self) -> None:
        provider = _Scripted([_resp("first"), _resp(_NEEDS_REVISION_JSON)])

        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            max_rounds=2,
            budget_tokens=60,
        )

        assert len(provider.calls) == 2
        assert len(outcome.rounds) == 1
        assert outcome.final.stop_reason == "token_budget"

    def test_agent_usage_observer_receives_each_explicit_agent_call(self) -> None:
        provider = _Scripted([_resp(_PLAN_JSON), _resp("answer"), _resp(_PASS_JSON)])
        observed: list[tuple[str, Usage]] = []

        run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            planner=Planner(provider),
            max_rounds=1,
            agent_usage_observer=lambda role, usage: observed.append((role, usage)),
        )

        assert [role for role, _usage in observed] == ["planner", "evaluator"]
        assert all(usage.input_tokens + usage.output_tokens == 30 for _, usage in observed)

    def test_revision_task_carries_conversation_forward(self) -> None:
        provider = _Scripted(
            [
                _resp("first attempt"),
                _resp(_NEEDS_REVISION_JSON),
                _resp("revised attempt"),
                _resp(_PASS_JSON),
            ]
        )
        run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="original task",
            evaluator=Evaluator(provider),
            max_rounds=2,
        )
        # Round 2 gets a bounded revision task, not the complete prior transcript.
        round_2_gen_call = provider.calls[2]
        messages = round_2_gen_call["messages"]
        roles = [m.role for m in messages]
        assert roles == ["system", "user"]
        assert "evaluator" in messages[-1].content.lower()
        assert "first attempt" in messages[-1].content

    def test_revision_bounds_prior_answer_instead_of_replaying_transcript(self) -> None:
        provider = _Scripted(
            [
                _resp("x" * 70_000),
                _resp(_NEEDS_REVISION_JSON),
                _resp("revised"),
                _resp(_PASS_JSON),
            ]
        )

        run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="original",
            evaluator=Evaluator(provider),
            max_rounds=2,
        )

        revision_content = provider.calls[2]["messages"][-1].content
        assert len(revision_content.encode("utf-8")) < 20_000
        assert "[prior answer truncated by ctx]" in revision_content

    def test_generator_iteration_limit_is_shared_across_rounds(self) -> None:
        from ctx.adapters.generic.providers import ToolCall

        provider = _Scripted(
            [
                _resp("first"),
                _resp(_NEEDS_REVISION_JSON),
                CompletionResponse(
                    content="",
                    tool_calls=(ToolCall(id="c1", name="x", arguments={}),),
                    finish_reason="tool_calls",
                    usage=Usage(input_tokens=10, output_tokens=20),
                    provider="scripted",
                    model="x",
                ),
            ]
        )

        outcome = run_with_evaluation(
            provider=provider,
            system_prompt="sys",
            task="t",
            evaluator=Evaluator(provider),
            max_rounds=2,
            max_iterations=2,
            extra_tools=[ToolDefinition(name="x", description="x", parameters={})],
            tool_executor=lambda _call: "ok",
        )

        assert len(provider.calls) == 3
        assert outcome.final.stop_reason == "max_iterations"
        assert outcome.final.iterations == 2


# ── _UsageTotals ──────────────────────────────────────────────────────────


class TestUsageTotals:
    def test_cost_none_stays_none(self) -> None:
        t = _UsageTotals()
        t.add(Usage(input_tokens=10, output_tokens=20))
        assert t.as_usage().cost_usd is None

    def test_explicit_zero_cost_and_cache_stay_reported(self) -> None:
        t = _UsageTotals()
        t.add(Usage(cost_usd=0.0, cached_input_tokens=0))

        usage = t.as_usage()

        assert usage.cost_usd == 0.0
        assert usage.cached_input_tokens == 0
        assert usage.tokens_reported

    def test_partial_usage_preserves_unavailable_fields(self) -> None:
        t = _UsageTotals.from_usage(
            Usage(
                input_tokens=4,
                output_tokens=2,
                cost_usd=0.01,
                cached_input_tokens=3,
            )
        )
        t.add(
            Usage(
                input_tokens=5,
                output_tokens=1,
                tokens_reported=False,
            )
        )

        usage = t.as_usage()

        assert usage.input_tokens == 9
        assert usage.output_tokens == 3
        assert usage.cost_usd is None
        assert usage.cached_input_tokens is None
        assert not usage.tokens_reported

    def test_cost_sums(self) -> None:
        t = _UsageTotals()
        t.add(Usage(input_tokens=5, output_tokens=10, cost_usd=0.01))
        t.add(Usage(input_tokens=3, output_tokens=5, cost_usd=0.02))
        u = t.as_usage()
        assert u.input_tokens == 8
        assert u.output_tokens == 15
        assert u.cost_usd == pytest.approx(0.03)

    def test_failure_observer_uses_latest_cumulative_usage(self) -> None:
        from ctx.cli.run import _DeferredStopObserver

        observer = _DeferredStopObserver(object())  # type: ignore[arg-type]
        observer.on_stop(
            LoopResult(
                stop_reason="completed",
                final_message="first",
                iterations=1,
                usage=Usage(input_tokens=10, output_tokens=20, cost_usd=0.1),
                messages=(),
            )
        )
        observer.on_stop(
            LoopResult(
                stop_reason="completed",
                final_message="second",
                iterations=1,
                usage=Usage(input_tokens=30, output_tokens=60, cost_usd=0.3),
                messages=(),
            )
        )

        result = observer.failure_result(RuntimeError("evaluation failed"))

        assert result.iterations == 2
        assert result.usage == Usage(input_tokens=30, output_tokens=60, cost_usd=0.3)

    def test_cost_only_agent_usage_does_not_invent_exact_tokens(self) -> None:
        from ctx.cli.run import _agent_token_usage

        payload = _agent_token_usage(
            Usage(cost_usd=0.1, tokens_reported=False),
            model="openai/reviewer",
            provider="openai",
        )

        assert payload["attribution"] == "unavailable"
        assert payload["total_tokens"] is None
        assert payload["cached_input_tokens"] is None
        assert payload["uncached_input_tokens"] is None
        assert payload["cost_usd"] == 0.1

    def test_explicit_zero_agent_usage_remains_exact(self) -> None:
        from ctx.cli.run import _agent_token_usage

        payload = _agent_token_usage(
            Usage(cost_usd=0.0, cached_input_tokens=0),
            model="local/free",
            provider="local",
        )

        assert payload["attribution"] == "exact"
        assert payload["total_tokens"] == 0
        assert payload["cached_input_tokens"] == 0
        assert payload["uncached_input_tokens"] == 0
        assert payload["cost_usd"] == 0.0


# ── CLI integration ──────────────────────────────────────────────────────


_VALID_PLAN = _PLAN_JSON


@pytest.fixture()
def fake_litellm_evaluator(monkeypatch: pytest.MonkeyPatch):
    fake = types.ModuleType("litellm")
    calls: list[dict[str, Any]] = []
    # Pre-loaded queue of responses. Test-specific calls can override
    # before they call main() by setting ``fake._responses`` to a list.

    def _mk(content: str) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }

    fake._responses = [_mk("answer"), _mk(_PASS_JSON)]  # type: ignore[attr-defined]

    def completion(**kwargs):
        calls.append(kwargs)
        if not fake._responses:  # type: ignore[attr-defined]
            raise RuntimeError("fake_litellm: no more responses")
        return fake._responses.pop(0)  # type: ignore[attr-defined]

    fake.completion = completion  # type: ignore[attr-defined]
    fake._calls = calls  # type: ignore[attr-defined]
    fake._mk = _mk  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


class TestCliEvaluator:
    def test_evaluator_flag_runs_eval_and_persists(
        self,
        fake_litellm_evaluator: Any,
        tmp_path: Path,
    ) -> None:
        from ctx.cli.run import main

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "fix the failing tests",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "ev-run",
                "--evaluator",
                "--evaluator-rounds",
                "2",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert exit_code == 0
        # session_start has evaluator_used=True.
        first = json.loads((tmp_path / "ev-run.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert first["evaluator_used"] is True
        assert first["evaluator_max_rounds"] == 2

    def test_evaluator_json_output_includes_rounds(
        self,
        fake_litellm_evaluator: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ctx.cli.run import main

        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "t",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "ev-json",
                "--evaluator",
                "--no-ctx-tools",
                "--quiet",
                "--json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert "evaluator_rounds" in payload
        assert len(payload["evaluator_rounds"]) >= 1
        assert payload["evaluator_rounds"][0]["verdict"] == "pass"

    def test_evaluator_agent_lifecycle_is_exact_and_unloaded(
        self,
        fake_litellm_evaluator: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ctx.cli.run import main

        lifecycle_root = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_root))

        assert (
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "t",
                    "--sessions-dir",
                    str(tmp_path / "sessions"),
                    "--session-id",
                    "ev-lifecycle",
                    "--evaluator",
                    "--evaluator-model",
                    "openai/reviewer",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )
            == 0
        )
        assert len(fake_litellm_evaluator._calls) == 2
        assert [call["timeout"] for call in fake_litellm_evaluator._calls] == [120.0, 45.0]
        assert [call["model"] for call in fake_litellm_evaluator._calls] == [
            "ollama/x",
            "openai/reviewer",
        ]

        events = [
            json.loads(line)
            for line in (lifecycle_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        agent_events = [event for event in events if event.get("entity_type") == "agent"]
        assert [event["action"] for event in agent_events] == [
            "load_requested",
            "load_applied",
            "used",
            "unload_requested",
            "unload_applied",
        ]
        assert {event["slug"] for event in agent_events} == {"evaluator"}
        used = next(event for event in agent_events if event["action"] == "used")
        assert used["token_usage"]["attribution"] == "exact"
        assert used["token_usage"]["total_tokens"] == 15
        assert used["token_usage"]["provider"] == "openai"

    def test_missing_agent_usage_is_not_reported_as_exact(
        self,
        fake_litellm_evaluator: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ctx.cli.run import main

        lifecycle_root = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_root))
        evaluator_response = fake_litellm_evaluator._mk(_PASS_JSON)
        evaluator_response.pop("usage")
        fake_litellm_evaluator._responses = [
            fake_litellm_evaluator._mk("answer"),
            evaluator_response,
        ]

        assert (
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "t",
                    "--sessions-dir",
                    str(tmp_path / "sessions"),
                    "--session-id",
                    "ev-usage-unavailable",
                    "--evaluator",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )
            == 0
        )

        events = [
            json.loads(line)
            for line in (lifecycle_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        used = next(event for event in events if event.get("action") == "used")
        assert used["token_usage"]["attribution"] == "unavailable"
        assert used["token_usage"]["total_tokens"] is None

    def test_default_run_emits_no_agent_lifecycle(
        self,
        fake_litellm_evaluator: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ctx.cli.run import main

        lifecycle_root = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_root))
        fake_litellm_evaluator._responses = [fake_litellm_evaluator._mk("answer")]

        assert (
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "t",
                    "--sessions-dir",
                    str(tmp_path / "sessions"),
                    "--session-id",
                    "no-agent-lifecycle",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )
            == 0
        )
        assert len(fake_litellm_evaluator._calls) == 1
        assert not (lifecycle_root / "events.jsonl").exists()

    def test_evaluator_without_flag_omits_rounds(
        self,
        fake_litellm_evaluator: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_litellm_evaluator._responses = [
            fake_litellm_evaluator._mk("done"),
        ]
        from ctx.cli.run import main

        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "t",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "no-ev",
                "--no-ctx-tools",
                "--quiet",
                "--json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        # When --evaluator is NOT set, the evaluator_rounds key is absent.
        assert "evaluator_rounds" not in payload
