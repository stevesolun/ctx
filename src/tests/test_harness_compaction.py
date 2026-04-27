"""
test_harness_compaction.py -- TokenBudgetCompactor + run_loop integration.

Covers:
  * should_compact triggers: max_chars, max_messages, no trigger.
  * compact() layout: head preserved, tail preserved, middle replaced
    with a single notice message containing the summary text.
  * Summary failure tolerance (provider raises) → loop keeps running
    with uncompacted conversation rather than crash.
  * run_loop integration: compactor called between iterations, new
    messages observed by the next provider call.
  * Config validation: bad constructor args rejected.

The summariser provider is mocked with a deterministic canned
summary so tests don't depend on LLM behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ctx.adapters.generic.compaction import (
    CompactionResult,
    TokenBudgetCompactor,
    _char_count,
    _render_messages_for_summary,
    compact_now,
)
from ctx.adapters.generic.loop import run_loop
from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ModelProvider,
    ToolCall,
    ToolDefinition,
    Usage,
)


# ── Scripted provider that tracks which messages it saw ────────────────────


@dataclass
class _Scripted(ModelProvider):
    responses: list[CompletionResponse]
    name: str = "scripted"
    calls: list[list[Message]] = field(default_factory=list)

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResponse:
        self.calls.append(list(messages))
        if not self.responses:
            raise RuntimeError("scripted: ran out of responses")
        return self.responses.pop(0)


def _resp(content: str = "ok", tool_calls: tuple[ToolCall, ...] = ()) -> CompletionResponse:
    return CompletionResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=Usage(input_tokens=5, output_tokens=3),
        provider="scripted",
        model="x",
    )


# ── Construction / validation ──────────────────────────────────────────────


class TestConstructor:
    def test_defaults_accepted(self) -> None:
        c = TokenBudgetCompactor()
        assert c.should_compact([Message(role="user", content="hi")]) is False

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            ({"max_chars": 0}, "max_chars"),
            ({"max_chars": -1}, "max_chars"),
            ({"max_messages": 0}, "max_messages"),
            ({"keep_head": -1}, "keep_head"),
            ({"keep_tail": -1}, "keep_tail"),
            (
                {"max_messages": 5, "keep_head": 3, "keep_tail": 3},
                "keep_head \\+ keep_tail",
            ),
        ],
    )
    def test_rejects_bad_config(self, kwargs: dict[str, Any], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            TokenBudgetCompactor(**kwargs)


# ── should_compact triggers ────────────────────────────────────────────────


class TestShouldCompact:
    def test_trivial_conversation_not_compacted(self) -> None:
        c = TokenBudgetCompactor(max_chars=10_000, max_messages=40)
        msgs = [Message(role="user", content="hi")]
        assert c.should_compact(msgs) is False

    def test_char_threshold_fires(self) -> None:
        c = TokenBudgetCompactor(max_chars=100)
        msgs = [
            Message(role="user", content="x" * 50),
            Message(role="assistant", content="y" * 60),
        ]
        # 50 + 60 = 110 > 100
        assert c.should_compact(msgs) is True

    def test_message_count_threshold_fires(self) -> None:
        # Pick keep values that fit below max_messages so the
        # constructor guard doesn't trip.
        c = TokenBudgetCompactor(
            max_chars=10**9,
            max_messages=5,
            keep_head=1,
            keep_tail=2,
            min_middle=1,
        )
        msgs = [Message(role="user", content="tiny") for _ in range(6)]
        assert c.should_compact(msgs) is True

    def test_tool_call_arguments_count_toward_chars(self) -> None:
        c = TokenBudgetCompactor(max_chars=100)
        msgs = [
            Message(role="user", content=""),
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="srv__fetch",
                        arguments={"url": "x" * 200},
                    ),
                ),
            ),
        ]
        assert c.should_compact(msgs) is True


# ── compact() layout ───────────────────────────────────────────────────────


class TestCompactLayout:
    def _provider_with_summary(self, summary: str = "SUMMARY") -> _Scripted:
        return _Scripted(responses=[_resp(content=summary)])

    def test_head_tail_preserved(self) -> None:
        c = TokenBudgetCompactor(
            max_chars=10**9, max_messages=10, keep_head=2, keep_tail=3, min_middle=1,
        )
        msgs = [
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
            Message(role="assistant", content="m1"),
            Message(role="user", content="m2"),
            Message(role="assistant", content="m3"),
            Message(role="user", content="m4"),
            Message(role="assistant", content="t1"),
            Message(role="user", content="t2"),
            Message(role="assistant", content="t3"),
        ]
        p = self._provider_with_summary("MIDDLE_SUMMARY")
        out = c.compact(msgs, p)
        # head = first 2, tail = last 3, notice in between = 6 total
        assert len(out) == 6
        assert out[0].role == "system"
        assert out[0].content == "sys"
        assert out[1].role == "user"
        assert out[1].content == "task"
        assert out[2].role == "assistant"
        assert out[2].content.startswith("[Compacted")
        assert "MIDDLE_SUMMARY" in out[2].content
        # Tail preserved verbatim.
        assert [m.content for m in out[3:]] == ["t1", "t2", "t3"]

    def test_skip_when_middle_too_short(self) -> None:
        c = TokenBudgetCompactor(
            max_chars=10**9, max_messages=20, keep_head=2, keep_tail=3, min_middle=10,
        )
        msgs = [Message(role="user", content=f"m{i}") for i in range(7)]
        p = self._provider_with_summary()
        # Middle would be 7 - 2 - 3 = 2 < 10 — skip.
        out = c.compact(msgs, p)
        assert out == msgs
        # Provider was not called for a summary.
        assert p.calls == []

    def test_empty_summary_replaced_with_placeholder(self) -> None:
        c = TokenBudgetCompactor(
            max_chars=10**9, max_messages=10, keep_head=1, keep_tail=2, min_middle=1,
        )
        msgs = [Message(role="user", content=f"m{i}") for i in range(8)]
        # Summary call returns empty string.
        p = _Scripted(responses=[_resp(content="")])
        out = c.compact(msgs, p)
        notice = out[1]
        assert "[summary was empty]" in notice.content


class TestCompactOnProviderError:
    def test_summary_call_failure_returns_stub(self) -> None:
        class _Boom(ModelProvider):
            name = "boom"

            def complete(self, messages, tools=None, **kw):
                raise RuntimeError("provider kaput")

        c = TokenBudgetCompactor(
            max_chars=10**9, max_messages=10, keep_head=1, keep_tail=2, min_middle=1,
        )
        msgs = [Message(role="user", content=f"m{i}") for i in range(8)]
        out = c.compact(msgs, _Boom())
        # Still compacted, but the notice content has the failure stub.
        notice = out[1]
        assert "compaction summary failed" in notice.content.lower()


# ── Helpers (_char_count, _render_messages_for_summary) ───────────────────


class TestHelpers:
    def test_char_count_text_only(self) -> None:
        msgs = [Message(role="user", content="hello")]
        assert _char_count(msgs) == 5

    def test_char_count_tool_calls(self) -> None:
        msgs = [
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(id="c1", name="fs__r", arguments={"p": "abc"}),
                ),
            )
        ]
        # name("fs__r") + json({"p": "abc"}) = 5 + 12 = 17
        total = _char_count(msgs)
        assert total >= 17

    def test_char_count_tool_result(self) -> None:
        msgs = [
            Message(
                role="tool",
                content="result-text",
                tool_call_id="c1",
                name="fs__r",
            )
        ]
        # content + tool_call_id + name
        assert _char_count(msgs) == len("result-text") + len("c1") + len("fs__r")

    def test_render_plain_messages(self) -> None:
        msgs = [
            Message(role="user", content="q"),
            Message(role="assistant", content="a"),
        ]
        out = _render_messages_for_summary(msgs)
        assert "[USER] q" in out
        assert "[ASSISTANT] a" in out

    def test_render_assistant_with_tool_calls(self) -> None:
        msgs = [
            Message(
                role="assistant",
                content="using tool",
                tool_calls=(
                    ToolCall(id="c1", name="fs__r", arguments={"p": "/tmp"}),
                ),
            )
        ]
        out = _render_messages_for_summary(msgs)
        assert "tool_calls:" in out
        assert "fs__r" in out
        assert "/tmp" in out
        assert "[ASSISTANT] using tool" in out

    def test_render_tool_result(self) -> None:
        msgs = [
            Message(
                role="tool", content="ok", tool_call_id="c1", name="fs__r",
            )
        ]
        out = _render_messages_for_summary(msgs)
        assert "[TOOL RESULT fs__r]" in out
        assert "ok" in out


# ── compact_now helper ────────────────────────────────────────────────────


class TestCompactNow:
    def test_runs_with_default_compactor(self) -> None:
        msgs = [
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
            *[Message(role="user", content=f"m{i}") for i in range(20)],
            *[Message(role="assistant", content=f"t{i}") for i in range(5)],
        ]
        p = _Scripted(responses=[_resp(content="CONDENSED")])
        result = compact_now(msgs, p)
        assert isinstance(result, CompactionResult)
        assert len(result.new_messages) < len(msgs)
        assert "CONDENSED" in result.summary

    def test_noop_when_no_compaction_happens(self) -> None:
        """compact_now returns original message list when middle is too short."""
        c = TokenBudgetCompactor(
            max_chars=10**9, max_messages=20, keep_head=2, keep_tail=3, min_middle=100,
        )
        msgs = [Message(role="user", content=f"m{i}") for i in range(7)]
        p = _Scripted(responses=[_resp(content="unused")])
        result = compact_now(msgs, p, compactor=c)
        assert result.new_messages == msgs
        assert result.summary == ""


# ── run_loop integration ─────────────────────────────────────────────────


class TestLoopIntegration:
    def test_compactor_fires_between_iterations(self) -> None:
        """After compaction, the NEXT provider call sees the condensed list."""
        # Set up a loop with 3 iterations. Each iteration has a
        # user+assistant pair (the model calls a tool, we answer).
        tc = ToolCall(id="c1", name="srv__noop", arguments={})
        responses = [
            _resp(content="", tool_calls=(tc,)),   # iter 1 — tool call
            _resp(content="", tool_calls=(tc,)),   # iter 2 — tool call
            _resp(content="final answer"),          # iter 3 — stop
        ]
        provider = _Scripted(responses=responses)

        # Compactor fires AFTER iteration 1 (message count > 3).
        c = TokenBudgetCompactor(
            max_chars=10**9,
            max_messages=3,
            keep_head=1,
            keep_tail=1,
            min_middle=1,
        )

        # Intercept the summary call — it uses provider.complete too.
        # We prepend a canned summary response so the scripted provider
        # hands back "summary-text" on the compactor's call.
        summary_response = _resp(content="summary-text")
        # Insertion order: iter1 → summary → iter2 → summary → iter3
        # But compaction fires AFTER iter1's tool call, before iter2
        # provider call. So sequence is: iter1_resp, summary_resp,
        # iter2_resp, summary_resp, iter3_resp.
        provider = _Scripted(
            responses=[
                responses[0],   # iter 1
                summary_response,
                responses[1],   # iter 2
                summary_response,
                responses[2],   # iter 3
            ]
        )

        def exec_(call: ToolCall) -> str:
            return "tool-ok"

        result = run_loop(
            provider=provider,
            system_prompt="sys",
            task="task",
            tool_executor=exec_,
            compactor=c,
            max_iterations=10,
        )
        assert result.stop_reason == "completed"
        assert result.final_message == "final answer"
        # Confirm the iter-2 provider call saw a compacted list.
        # Calls 0 (iter1), 1 (summary), 2 (iter2), 3 (summary),
        # 4 (iter3). Check that call 2 has <= call 0's message count.
        iter1_msgs = provider.calls[0]
        iter2_msgs = provider.calls[2]
        # After iter1: conversation had ~5 msgs (system, user, assistant, tool, ...)
        # After compaction: kept head(1) + notice + tail(1) = 3.
        assert len(iter2_msgs) <= len(iter1_msgs) + 1
        # Notice message must be visible to iter2.
        assert any(
            m.content.startswith("[Compacted") for m in iter2_msgs
        )

    def test_compactor_error_does_not_crash_loop(self) -> None:
        class _AlwaysCompact:
            def should_compact(self, messages: list[Message]) -> bool:
                return True

            def compact(self, messages, provider):
                raise RuntimeError("bad compactor")

        provider = _Scripted(responses=[_resp(content="done")])
        result = run_loop(
            provider=provider,
            system_prompt="",
            task="hi",
            compactor=_AlwaysCompact(),
        )
        # The compactor raised but the loop kept running.
        assert result.stop_reason == "completed"

    def test_no_compactor_parameter_is_backward_compatible(self) -> None:
        provider = _Scripted(responses=[_resp(content="done")])
        result = run_loop(
            provider=provider, system_prompt="", task="hi",
        )
        assert result.stop_reason == "completed"

    def test_compactor_never_triggers_is_noop(self) -> None:
        c = TokenBudgetCompactor(max_chars=10**9, max_messages=10**6)
        provider = _Scripted(responses=[_resp(content="done")])
        run_loop(
            provider=provider,
            system_prompt="sys",
            task="task",
            compactor=c,
        )
        # No summary call — just the one iter.
        assert len(provider.calls) == 1
