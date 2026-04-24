"""
test_harness_loop.py -- run_loop() integration tests with a scripted provider.

The loop is the central integration point; tests cover:

  * Termination paths (all 7 stop_reasons).
  * Tool dispatch (router path, tool_executor path, missing dispatcher).
  * Budget + iteration caps.
  * Cancellation mid-run.
  * Observer hook firing order.
  * State seeding (resume path).

Uses ``_ScriptedProvider`` — a deterministic ``ModelProvider`` that
emits a predefined sequence of responses. No LiteLLM, no real network.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from ctx.adapters.generic.loop import (
    LoopResult,
    LoopObserver,
    run_loop,
)
from ctx.adapters.generic.providers import (
    CompletionResponse,
    FinishReason,
    Message,
    ModelProvider,
    ToolCall,
    ToolDefinition,
    Usage,
)
from ctx.adapters.generic.tools import (
    McpRouter,
    McpServerError,
    TOOL_SEPARATOR,
)


# ── Scripted provider ────────────────────────────────────────────────────────


@dataclass
class _Scripted(ModelProvider):
    """Replays a scripted sequence of CompletionResponses, in order."""

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
                "tools": list(tools) if tools else None,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if not self.responses:
            raise RuntimeError("scripted provider: ran out of canned responses")
        return self.responses.pop(0)


def _stop_response(
    content: str = "done",
    usage: Usage | None = None,
) -> CompletionResponse:
    return CompletionResponse(
        content=content,
        tool_calls=(),
        finish_reason="stop",
        usage=usage or Usage(input_tokens=5, output_tokens=3),
        provider="scripted",
        model="x",
    )


def _tool_response(
    *tool_calls: ToolCall,
    content: str = "",
    usage: Usage | None = None,
) -> CompletionResponse:
    return CompletionResponse(
        content=content,
        tool_calls=tuple(tool_calls),
        finish_reason="tool_calls",
        usage=usage or Usage(input_tokens=10, output_tokens=5),
        provider="scripted",
        model="x",
    )


def _filter_response() -> CompletionResponse:
    return CompletionResponse(
        content="I can't answer that.",
        tool_calls=(),
        finish_reason="content_filter",
        usage=Usage(input_tokens=5, output_tokens=5),
        provider="scripted",
        model="x",
    )


# ── Termination: completed ──────────────────────────────────────────────────


class TestCompletion:
    def test_single_turn_completion(self) -> None:
        provider = _Scripted([_stop_response("hello there")])
        result = run_loop(
            provider=provider,
            system_prompt="Be terse.",
            task="hi",
        )
        assert result.stop_reason == "completed"
        assert result.final_message == "hello there"
        assert result.iterations == 1

    def test_conversation_layout(self) -> None:
        """First turn: system prompt + user task; last turn: assistant."""
        provider = _Scripted([_stop_response("ok")])
        result = run_loop(
            provider=provider,
            system_prompt="prompt",
            task="task",
        )
        roles = [m.role for m in result.messages]
        assert roles[0] == "system"
        assert roles[1] == "user"
        assert roles[-1] == "assistant"

    def test_no_system_prompt_skips_system_message(self) -> None:
        provider = _Scripted([_stop_response("hi")])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="task",
        )
        assert result.messages[0].role == "user"


# ── Termination: max_iterations ─────────────────────────────────────────────


class TestMaxIterations:
    def test_hits_iteration_cap(self) -> None:
        """Model that never stops calling a tool hits the iteration cap."""
        tc = ToolCall(id="c1", name="srv__noop", arguments={})
        provider = _Scripted([_tool_response(tc) for _ in range(10)])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="loop forever",
            tool_executor=lambda _call: "noop-result",
            max_iterations=5,
        )
        assert result.stop_reason == "max_iterations"
        assert result.iterations == 5

    def test_invalid_max_iterations_rejected(self) -> None:
        provider = _Scripted([_stop_response("hi")])
        with pytest.raises(ValueError, match="max_iterations"):
            run_loop(
                provider=provider,
                system_prompt="",
                task="task",
                max_iterations=0,
            )


# ── Termination: cost / token budget ────────────────────────────────────────


class TestBudgets:
    def test_cost_budget_trips(self) -> None:
        tc = ToolCall(id="c1", name="srv__noop", arguments={})
        # Each turn: $0.10 cost. Budget $0.25 → trips after second turn.
        expensive = CompletionResponse(
            content="",
            tool_calls=(tc,),
            finish_reason="tool_calls",
            usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.10),
            provider="scripted",
            model="x",
        )
        provider = _Scripted([expensive, expensive, expensive, _stop_response()])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="spend",
            tool_executor=lambda _c: "ok",
            budget_usd=0.25,
            max_iterations=20,
        )
        assert result.stop_reason == "cost_budget"
        # Cost accumulated up to and including the trip-point call.
        assert result.usage.cost_usd is not None
        assert result.usage.cost_usd > 0.25

    def test_token_budget_trips(self) -> None:
        tc = ToolCall(id="c1", name="srv__noop", arguments={})
        heavy = _tool_response(tc, usage=Usage(input_tokens=100, output_tokens=50))
        provider = _Scripted([heavy, heavy, heavy, _stop_response()])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="burn",
            tool_executor=lambda _c: "ok",
            budget_tokens=250,
            max_iterations=20,
        )
        assert result.stop_reason == "token_budget"
        assert result.usage.input_tokens + result.usage.output_tokens > 250


# ── Termination: cancellation ───────────────────────────────────────────────


class TestCancel:
    def test_cancelled_before_first_iteration(self) -> None:
        provider = _Scripted([_stop_response("unreached")])
        cancel = threading.Event()
        cancel.set()  # set BEFORE calling
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            cancel_event=cancel,
        )
        assert result.stop_reason == "cancelled"
        assert result.iterations == 1  # counted the iteration we checked

    def test_cancel_mid_run(self) -> None:
        tc = ToolCall(id="c1", name="srv__noop", arguments={})
        cancel = threading.Event()
        calls_seen = [0]

        def executor(call: ToolCall) -> str:
            calls_seen[0] += 1
            if calls_seen[0] == 2:
                cancel.set()
            return "ok"

        provider = _Scripted([_tool_response(tc), _tool_response(tc), _tool_response(tc)])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="loop",
            tool_executor=executor,
            cancel_event=cancel,
            max_iterations=10,
        )
        assert result.stop_reason == "cancelled"


# ── Termination: content_filter ─────────────────────────────────────────────


class TestContentFilter:
    def test_filter_stops_run(self) -> None:
        provider = _Scripted([_filter_response()])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="sensitive query",
        )
        assert result.stop_reason == "content_filter"
        assert result.final_message == "I can't answer that."


# ── Tool dispatch ───────────────────────────────────────────────────────────


class TestToolDispatch:
    def test_executor_path(self) -> None:
        tc = ToolCall(id="c1", name="custom__echo", arguments={"x": 42})
        invoked = []

        def exec_(call: ToolCall) -> str:
            invoked.append(call)
            return f"echo={call.arguments['x']}"

        provider = _Scripted([_tool_response(tc), _stop_response("finished")])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="call tool",
            tool_executor=exec_,
        )
        assert result.stop_reason == "completed"
        assert invoked == [tc]
        # Check the tool result message landed on the conversation.
        tool_msgs = [m for m in result.messages if m.role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == "echo=42"
        assert tool_msgs[0].tool_call_id == "c1"

    def test_missing_dispatcher_surfaces_error(self) -> None:
        """No router + no executor → tool call lands an error, loop stops."""
        tc = ToolCall(id="c1", name="anything__here", arguments={})
        provider = _Scripted([_tool_response(tc), _stop_response("unreached")])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="task",
        )
        assert result.stop_reason == "tool_error"
        assert "no dispatcher" in result.detail

    def test_executor_exception_ends_loop(self) -> None:
        tc = ToolCall(id="c1", name="x__fail", arguments={})

        def boom(call: ToolCall) -> str:
            raise RuntimeError("kaboom")

        provider = _Scripted([_tool_response(tc), _stop_response("unreached")])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            tool_executor=boom,
        )
        assert result.stop_reason == "tool_error"
        assert "kaboom" in result.detail

    def test_router_dispatch(self, tmp_path) -> None:
        import sys
        from pathlib import Path as _Path
        from ctx.adapters.generic.tools import McpServerConfig

        fixture = _Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
        router = McpRouter(
            [
                McpServerConfig(
                    name="fake",
                    command=sys.executable,
                    args=(str(fixture),),
                    startup_timeout=5.0,
                )
            ]
        )
        router.start()
        try:
            tc = ToolCall(
                id="c1", name="fake__echo", arguments={"text": "via-router"}
            )
            provider = _Scripted(
                [_tool_response(tc), _stop_response("done")]
            )
            result = run_loop(
                provider=provider,
                system_prompt="",
                task="task",
                router=router,
            )
            assert result.stop_reason == "completed"
            tool_msgs = [m for m in result.messages if m.role == "tool"]
            assert tool_msgs[0].content == "via-router"
        finally:
            router.stop()

    def test_multiple_tool_calls_in_one_turn(self) -> None:
        tc1 = ToolCall(id="c1", name="x__a", arguments={})
        tc2 = ToolCall(id="c2", name="x__b", arguments={})

        def exec_(call: ToolCall) -> str:
            return f"result-{call.id}"

        provider = _Scripted(
            [_tool_response(tc1, tc2), _stop_response("done")]
        )
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            tool_executor=exec_,
        )
        tool_msgs = [m for m in result.messages if m.role == "tool"]
        assert [m.content for m in tool_msgs] == ["result-c1", "result-c2"]
        assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2"]


# ── Tool catalogue + extra_tools ────────────────────────────────────────────


class TestToolCatalogue:
    def test_tools_passed_to_provider(self) -> None:
        extra = ToolDefinition(
            name="custom_tool",
            description="A custom tool.",
            parameters={"type": "object", "properties": {}},
        )
        provider = _Scripted([_stop_response("hi")])
        run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            extra_tools=[extra],
        )
        passed = provider.calls[0]["tools"]
        assert len(passed) == 1
        assert passed[0].name == "custom_tool"

    def test_no_tools_passed_as_none(self) -> None:
        provider = _Scripted([_stop_response("hi")])
        run_loop(
            provider=provider,
            system_prompt="",
            task="task",
        )
        assert provider.calls[0]["tools"] is None


# ── Usage accumulation ──────────────────────────────────────────────────────


class TestUsage:
    def test_usage_sums_across_turns(self) -> None:
        tc = ToolCall(id="c1", name="x__a", arguments={})
        a = _tool_response(tc, usage=Usage(input_tokens=10, output_tokens=5))
        b = _stop_response("done", usage=Usage(input_tokens=20, output_tokens=7))
        provider = _Scripted([a, b])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            tool_executor=lambda _c: "ok",
        )
        assert result.usage.input_tokens == 30
        assert result.usage.output_tokens == 12

    def test_cost_none_when_provider_never_reports(self) -> None:
        """Ollama + others don't report cost — result reflects that as None."""
        provider = _Scripted(
            [_stop_response("hi", usage=Usage(input_tokens=1, output_tokens=1))]
        )
        result = run_loop(
            provider=provider, system_prompt="", task="task",
        )
        assert result.usage.cost_usd is None

    def test_cost_accumulated_when_reported(self) -> None:
        r = CompletionResponse(
            content="",
            tool_calls=(ToolCall(id="c1", name="x__a", arguments={}),),
            finish_reason="tool_calls",
            usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.01),
            provider="scripted",
            model="x",
        )
        final = _stop_response("done", usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.02))
        provider = _Scripted([r, final])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            tool_executor=lambda _c: "ok",
        )
        assert result.usage.cost_usd == pytest.approx(0.03)


# ── Observer hooks ──────────────────────────────────────────────────────────


class _RecordingObserver(LoopObserver):
    def __init__(self) -> None:
        self.iter_starts: list[int] = []
        self.responses: list[tuple[int, str]] = []
        self.tool_calls: list[tuple[int, str, str, str | None]] = []
        self.stops: list[LoopResult] = []

    def on_iteration_start(self, iteration, messages):
        self.iter_starts.append(iteration)

    def on_model_response(self, iteration, response):
        self.responses.append((iteration, response.content))

    def on_tool_call(self, iteration, call, result, error):
        self.tool_calls.append((iteration, call.name, result, error))

    def on_stop(self, result):
        self.stops.append(result)


class TestObserver:
    def test_fires_on_single_turn(self) -> None:
        provider = _Scripted([_stop_response("done")])
        obs = _RecordingObserver()
        run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            observer=obs,
        )
        assert obs.iter_starts == [1]
        assert obs.responses == [(1, "done")]
        assert obs.tool_calls == []  # no tool calls
        assert len(obs.stops) == 1
        assert obs.stops[0].stop_reason == "completed"

    def test_fires_on_tool_turn(self) -> None:
        tc = ToolCall(id="c1", name="x__a", arguments={})
        provider = _Scripted([_tool_response(tc), _stop_response("done")])
        obs = _RecordingObserver()
        run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            tool_executor=lambda _c: "tool-result",
            observer=obs,
        )
        assert obs.iter_starts == [1, 2]
        assert obs.tool_calls == [(1, "x__a", "tool-result", None)]
        assert obs.responses[1] == (2, "done")

    def test_fires_on_error_turn(self) -> None:
        tc = ToolCall(id="c1", name="x__a", arguments={})
        provider = _Scripted([_tool_response(tc), _stop_response("unreached")])

        def boom(_c):
            raise RuntimeError("kaboom")

        obs = _RecordingObserver()
        run_loop(
            provider=provider,
            system_prompt="",
            task="task",
            tool_executor=boom,
            observer=obs,
        )
        assert len(obs.tool_calls) == 1
        assert obs.tool_calls[0][3] is not None  # error field set
        assert "kaboom" in obs.tool_calls[0][3]


# ── State seeding (resume path) ─────────────────────────────────────────────


class TestResumePath:
    def test_prior_messages_appended_after_task(self) -> None:
        provider = _Scripted([_stop_response("done")])
        prior = [
            Message(role="assistant", content="from a prior session"),
            Message(role="user", content="then the user said"),
        ]
        result = run_loop(
            provider=provider,
            system_prompt="sys",
            task="current-task",
            messages=prior,
        )
        roles = [m.role for m in result.messages]
        # Order: system, user(task), assistant(prior), user(prior), assistant(current)
        assert roles == ["system", "user", "assistant", "user", "assistant"]
