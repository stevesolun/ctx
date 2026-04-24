"""
test_providers_base.py -- Dataclass/Protocol tests for the provider layer.

Pins the immutability + shape invariants of the messages/tool-calls/
responses flowing through the generic harness. Provider-specific
behaviour lives in test_litellm_provider.py.
"""

from __future__ import annotations

import pytest

from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
)


class TestMessage:
    def test_defaults(self) -> None:
        m = Message(role="user", content="hi")
        assert m.role == "user"
        assert m.content == "hi"
        assert m.tool_calls == ()
        assert m.tool_call_id is None
        assert m.name is None

    def test_is_frozen(self) -> None:
        m = Message(role="user", content="hi")
        with pytest.raises(Exception):  # FrozenInstanceError
            m.content = "changed"  # type: ignore[misc]

    def test_assistant_with_tool_calls(self) -> None:
        tc = ToolCall(id="c1", name="fs_read", arguments={"path": "/tmp"})
        m = Message(role="assistant", content="", tool_calls=(tc,))
        assert m.tool_calls[0].name == "fs_read"

    def test_tool_result_shape(self) -> None:
        m = Message(
            role="tool",
            content="ok",
            tool_call_id="c1",
            name="fs_read",
        )
        assert m.tool_call_id == "c1"
        assert m.name == "fs_read"


class TestToolCall:
    def test_frozen(self) -> None:
        tc = ToolCall(id="c1", name="fs_read", arguments={"path": "/tmp"})
        with pytest.raises(Exception):
            tc.name = "other"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = ToolCall(id="1", name="n", arguments={"x": 1})
        b = ToolCall(id="1", name="n", arguments={"x": 1})
        assert a == b


class TestToolDefinition:
    def test_construction(self) -> None:
        td = ToolDefinition(
            name="fs_read",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
        assert td.parameters["required"] == ["path"]


class TestUsage:
    def test_defaults(self) -> None:
        u = Usage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.cost_usd is None

    def test_cost_nullable(self) -> None:
        u = Usage(input_tokens=10, output_tokens=20)
        assert u.cost_usd is None

    def test_cost_numeric(self) -> None:
        u = Usage(input_tokens=10, output_tokens=20, cost_usd=0.00015)
        assert u.cost_usd == 0.00015


class TestCompletionResponse:
    def test_frozen(self) -> None:
        r = CompletionResponse(
            content="hi",
            tool_calls=(),
            finish_reason="stop",
            usage=Usage(),
            provider="litellm",
            model="test",
        )
        with pytest.raises(Exception):
            r.content = "changed"  # type: ignore[misc]

    def test_raw_default_empty_dict(self) -> None:
        r = CompletionResponse(
            content="", tool_calls=(), finish_reason="stop",
            usage=Usage(), provider="x", model="y",
        )
        assert r.raw == {}
