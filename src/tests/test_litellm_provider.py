"""
test_litellm_provider.py -- Unit tests for LiteLLMProvider.

LiteLLM is mocked so these tests run without the real dependency
installed. Each test pins one slice of the provider contract:

  - Import gate: missing litellm surfaces a clear RuntimeError.
  - Message → LiteLLM dict: role/content/tool_calls/tool-result shapes.
  - Tool-definition → LiteLLM dict: OpenAI function-tool format.
  - Response normalisation: content, tool_calls parsing (JSON args),
    usage extraction, finish-reason alias table.
  - Config wiring: default_model / base_url / api_key_env surface in
    the call kwargs.

Real live-provider smoke is out of scope — it belongs in an
``integration`` marked suite that talks to OpenRouter/Ollama.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from ctx.adapters.generic.providers import (
    CompletionResponse,
    LiteLLMProvider,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
    get_provider,
)
from ctx.adapters.generic.providers.litellm_provider import (
    _message_to_litellm,
    _normalise_finish_reason,
    _normalise_response,
    _parse_tool_call,
    _tool_def_to_litellm,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def fake_litellm(monkeypatch: pytest.MonkeyPatch):
    """Stub `litellm.completion` so no real call leaves the machine."""
    fake = types.ModuleType("litellm")
    calls: list[dict[str, Any]] = []

    def completion(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        # Return whatever the test's setattr installed on `fake._response`,
        # default to a trivial stop response.
        response = getattr(fake, "_response", None)
        if response is None:
            return {
                "choices": [
                    {
                        "message": {"content": "hello", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        return response

    fake.completion = completion  # type: ignore[attr-defined]
    fake._calls = calls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


# ── Import gate ──────────────────────────────────────────────────────


class TestImportGate:
    def test_missing_litellm_raises_runtime_error_with_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the import to fail by dropping the module if present and
        # making future imports blow up.
        monkeypatch.delitem(sys.modules, "litellm", raising=False)
        # Inject a loader that raises ImportError for "litellm".
        import importlib.abc

        class _Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):  # noqa: ANN001
                if name == "litellm":
                    raise ImportError("blocked for test")
                return None

        sys.meta_path.insert(0, _Blocker())
        try:
            prov = LiteLLMProvider(default_model="ollama/llama3")
            with pytest.raises(RuntimeError, match="litellm is required"):
                prov.complete([Message(role="user", content="hi")])
        finally:
            sys.meta_path.pop(0)


# ── Constructor ──────────────────────────────────────────────────────


class TestConstructor:
    def test_empty_default_model_rejected(self) -> None:
        with pytest.raises(ValueError, match="default_model is required"):
            LiteLLMProvider(default_model="")

    def test_name_constant(self) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        assert prov.name == "litellm"

    def test_get_provider_returns_litellm_impl(self) -> None:
        prov = get_provider(default_model="ollama/llama3")
        assert isinstance(prov, LiteLLMProvider)


# ── Message → LiteLLM translation ────────────────────────────────────


class TestMessageToLiteLLM:
    def test_user_message(self) -> None:
        m = Message(role="user", content="hi")
        out = _message_to_litellm(m)
        assert out == {"role": "user", "content": "hi"}

    def test_system_message(self) -> None:
        m = Message(role="system", content="be helpful")
        out = _message_to_litellm(m)
        assert out == {"role": "system", "content": "be helpful"}

    def test_assistant_plain(self) -> None:
        m = Message(role="assistant", content="answer")
        out = _message_to_litellm(m)
        assert out["role"] == "assistant"
        assert out["content"] == "answer"
        assert "tool_calls" not in out

    def test_assistant_with_tool_calls(self) -> None:
        tc = ToolCall(id="c1", name="fs_read", arguments={"path": "/tmp"})
        m = Message(role="assistant", content="", tool_calls=(tc,))
        out = _message_to_litellm(m)
        assert out["role"] == "assistant"
        assert out["content"] is None
        assert out["tool_calls"] == [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "fs_read",
                    "arguments": json.dumps({"path": "/tmp"}),
                },
            }
        ]

    def test_tool_result(self) -> None:
        m = Message(
            role="tool",
            content="file contents",
            tool_call_id="c1",
            name="fs_read",
        )
        out = _message_to_litellm(m)
        assert out == {
            "role": "tool",
            "content": "file contents",
            "tool_call_id": "c1",
            "name": "fs_read",
        }


# ── Tool definition → LiteLLM translation ────────────────────────────


class TestToolDefToLiteLLM:
    def test_shape(self) -> None:
        td = ToolDefinition(
            name="fs_read",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
        out = _tool_def_to_litellm(td)
        assert out == {
            "type": "function",
            "function": {
                "name": "fs_read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }


# ── Tool-call parsing ────────────────────────────────────────────────


class TestParseToolCall:
    def test_happy_path(self) -> None:
        raw = {
            "id": "c1",
            "function": {"name": "fs_read", "arguments": '{"path": "/tmp"}'},
        }
        tc = _parse_tool_call(raw)
        assert tc == ToolCall(id="c1", name="fs_read", arguments={"path": "/tmp"})

    def test_malformed_json_args_preserve_parse_error(self) -> None:
        raw = {
            "id": "c1",
            "function": {"name": "fs_read", "arguments": "not json"},
        }
        tc = _parse_tool_call(raw)
        assert tc.arguments == {}
        assert "invalid JSON arguments" in tc.parse_error

    def test_dict_args_passthrough(self) -> None:
        raw = {
            "id": "c1",
            "function": {"name": "fs_read", "arguments": {"path": "/tmp"}},
        }
        tc = _parse_tool_call(raw)
        assert tc.arguments == {"path": "/tmp"}
        assert tc.parse_error == ""

    def test_missing_function_fields(self) -> None:
        tc = _parse_tool_call({"id": "c1"})
        assert tc == ToolCall(id="c1", name="", arguments={})


# ── Response normalisation ───────────────────────────────────────────


class TestNormaliseResponse:
    def test_usage_defaults_preserve_legacy_construction(self) -> None:
        assert Usage(1, 2, 0.3) == Usage(
            input_tokens=1,
            output_tokens=2,
            cost_usd=0.3,
            cached_input_tokens=None,
            tokens_reported=True,
        )

    def test_plain_content_response(self) -> None:
        raw = {
            "model": "provider-returned-model",
            "choices": [
                {
                    "message": {"content": "hello", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        resp = _normalise_response(raw, provider="litellm", model="test")
        assert resp.content == "hello"
        assert resp.tool_calls == ()
        assert resp.finish_reason == "stop"
        assert resp.usage.input_tokens == 5
        assert resp.usage.output_tokens == 3
        assert resp.usage.tokens_reported
        assert resp.usage.cached_input_tokens is None
        assert resp.provider == "litellm"
        assert resp.model == "test"
        assert resp.response_model == "provider-returned-model"
        assert resp.authentication_submitted is False
        assert resp.request_endpoint_hash is None

    def test_tool_calls_in_response(self) -> None:
        raw = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "fs_read",
                                    "arguments": '{"path": "/tmp"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }
        resp = _normalise_response(raw, provider="litellm", model="test")
        assert resp.finish_reason == "tool_calls"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "fs_read"
        assert resp.tool_calls[0].arguments == {"path": "/tmp"}

    def test_anthropic_finish_reason_aliased(self) -> None:
        raw = {
            "choices": [
                {
                    "message": {"content": "done"},
                    "finish_reason": "end_turn",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        resp = _normalise_response(raw, provider="litellm", model="test")
        assert resp.finish_reason == "stop"

    def test_usage_with_cost(self) -> None:
        raw = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "response_cost": 0.0002,
        }
        resp = _normalise_response(raw, provider="litellm", model="test")
        assert resp.usage.cost_usd == 0.0002

    def test_cost_without_token_fields_marks_tokens_unreported(self) -> None:
        raw = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {},
            "response_cost": 0.0002,
        }

        resp = _normalise_response(raw, provider="litellm", model="test")

        assert resp.usage.cost_usd == 0.0002
        assert not resp.usage.tokens_reported

    def test_missing_usage_is_distinct_from_explicit_zero(self) -> None:
        missing = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        }
        explicit_zero = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

        missing_resp = _normalise_response(missing, provider="litellm", model="test")
        zero_resp = _normalise_response(explicit_zero, provider="litellm", model="test")

        assert not missing_resp.usage.tokens_reported
        assert zero_resp.usage.tokens_reported

    def test_partial_usage_is_not_reported_as_complete(self) -> None:
        raw = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4},
        }

        resp = _normalise_response(raw, provider="litellm", model="test")

        assert not resp.usage.tokens_reported
        assert resp.usage.input_tokens == 4
        assert resp.usage.output_tokens == 0

    def test_openai_cached_tokens_are_already_in_prompt_total(self) -> None:
        raw = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 7},
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }

        resp = _normalise_response(raw, provider="litellm", model="test")

        assert resp.usage.input_tokens == 10
        assert resp.usage.cached_input_tokens == 7

    def test_anthropic_cached_tokens_do_not_double_count_prompt_total(self) -> None:
        raw = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 2,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 3,
            },
        }

        resp = _normalise_response(raw, provider="litellm", model="test")

        assert resp.usage.input_tokens == 20
        assert resp.usage.cached_input_tokens == 7

    @pytest.mark.parametrize(
        ("cache_read", "expected"),
        [
            (None, None),
            (0, 0),
        ],
    )
    def test_cache_read_null_is_distinct_from_explicit_zero(
        self,
        cache_read: int | None,
        expected: int | None,
    ) -> None:
        raw = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "cache_read_input_tokens": cache_read,
            },
        }

        resp = _normalise_response(raw, provider="litellm", model="test")

        assert resp.usage.cached_input_tokens == expected

    def test_empty_choices_degrades_cleanly(self) -> None:
        raw: dict[str, Any] = {"choices": [], "usage": {}}
        resp = _normalise_response(raw, provider="litellm", model="test")
        assert resp.content == ""
        assert resp.finish_reason == "other"

    def test_pydantic_shaped_response_is_dumped(self) -> None:
        class _FakePydantic:
            def model_dump(self) -> dict[str, Any]:
                return {
                    "choices": [
                        {
                            "message": {"content": "from pydantic"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        resp = _normalise_response(_FakePydantic(), provider="litellm", model="x")
        assert resp.content == "from pydantic"

    def test_raw_is_preserved(self) -> None:
        raw = {
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "_internal_marker": "visible-to-debugging",
        }
        resp = _normalise_response(raw, provider="litellm", model="x")
        assert resp.raw["_internal_marker"] == "visible-to-debugging"


# ── Finish-reason aliasing ───────────────────────────────────────────


class TestFinishReasonAliasing:
    @pytest.mark.parametrize(
        "provider_reason,canonical",
        [
            ("stop", "stop"),
            ("end_turn", "stop"),
            ("function_call", "tool_calls"),
            ("tool_calls", "tool_calls"),
            ("tool_use", "tool_calls"),
            ("length", "length"),
            ("max_tokens", "length"),
            ("content_filter", "content_filter"),
        ],
    )
    def test_alias_table(self, provider_reason: str, canonical: str) -> None:
        assert _normalise_finish_reason(provider_reason) == canonical

    def test_unknown_reason(self) -> None:
        assert _normalise_finish_reason("invented_reason") == "other"

    def test_non_string_input(self) -> None:
        assert _normalise_finish_reason(None) == "other"
        assert _normalise_finish_reason(42) == "other"


# ── End-to-end provider.complete() wiring ────────────────────────────


class TestCompleteWiring:
    def test_default_model_passed_through(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        prov.complete([Message(role="user", content="hi")])
        call_kwargs = fake_litellm._calls[0]  # type: ignore[attr-defined]
        assert call_kwargs["model"] == "ollama/llama3"

    def test_per_call_model_override(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        prov.complete(
            [Message(role="user", content="hi")],
            model="openrouter/anthropic/claude-opus-4.7",
        )
        assert fake_litellm._calls[0]["model"] == "openrouter/anthropic/claude-opus-4.7"

    def test_base_url_passed_as_api_base(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(
            default_model="ollama/llama3",
            base_url="http://localhost:11434",
        )
        prov.complete([Message(role="user", content="hi")])
        assert fake_litellm._calls[0]["api_base"] == "http://localhost:11434"

    def test_api_key_env_read(
        self,
        fake_litellm: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-123")
        prov = LiteLLMProvider(
            default_model="openrouter/anthropic/claude",
            api_key_env="OPENROUTER_API_KEY",
        )
        prov.complete([Message(role="user", content="hi")])
        assert fake_litellm._calls[0]["api_key"] == "sk-test-123"

    def test_response_records_non_secret_request_provenance(
        self,
        fake_litellm: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MODEL_API_KEY", "private-key-value")
        fake_litellm._response = {  # type: ignore[attr-defined]
            "model": "openai/gpt-test",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        provider = LiteLLMProvider(
            default_model="openai/gpt-test",
            base_url="https://provider.example/v1",
            api_key_env="MODEL_API_KEY",
        )

        response = provider.complete([Message(role="user", content="hi")])

        assert response.response_model == "openai/gpt-test"
        assert response.authentication_submitted is True
        assert response.request_endpoint_hash == (
            "sha256:ebcd93ca6f177cf940def36d04b24abddd12d02500b1240228f7ea94e6223dd7"
        )
        assert "private-key-value" not in repr(response)

    def test_missing_api_key_env_silent(
        self,
        fake_litellm: types.ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        prov = LiteLLMProvider(
            default_model="openrouter/x",
            api_key_env="OPENROUTER_API_KEY",
        )
        prov.complete([Message(role="user", content="hi")])
        # no api_key kwarg when env is unset — let LiteLLM surface the error
        # if it needs a key
        assert "api_key" not in fake_litellm._calls[0]

    def test_tools_forwarded(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        tools = [
            ToolDefinition(
                name="fs_read",
                description="Read a file",
                parameters={"type": "object", "properties": {}},
            )
        ]
        prov.complete([Message(role="user", content="hi")], tools=tools)
        call = fake_litellm._calls[0]
        assert call["tools"][0]["function"]["name"] == "fs_read"

    def test_no_tools_omits_key(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        prov.complete([Message(role="user", content="hi")])
        assert "tools" not in fake_litellm._calls[0]

    def test_max_tokens_forwarded(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        prov.complete(
            [Message(role="user", content="hi")],
            max_tokens=500,
        )
        assert fake_litellm._calls[0]["max_tokens"] == 500

    def test_temperature_default(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        prov.complete([Message(role="user", content="hi")])
        assert fake_litellm._calls[0]["temperature"] == 0.7

    def test_timeout_default(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        prov.complete([Message(role="user", content="hi")])
        assert fake_litellm._calls[0]["timeout"] == 120.0

    def test_returns_completion_response(self, fake_litellm: types.ModuleType) -> None:
        prov = LiteLLMProvider(default_model="ollama/llama3")
        resp = prov.complete([Message(role="user", content="hi")])
        assert isinstance(resp, CompletionResponse)
        assert resp.content == "hello"  # from fake_litellm default
        assert resp.finish_reason == "stop"
