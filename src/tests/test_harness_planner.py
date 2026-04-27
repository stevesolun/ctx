"""
test_harness_planner.py -- Planner + PlanArtifact unit tests.

Covers:
  * PlanArtifact serialisation (to_dict, to_markdown, write)
  * _extract_json strategies (pure JSON, code-fenced, brace-pulled)
  * Planner.plan against a scripted provider (happy path, wrapped
    JSON, unstructured response fallback, legacy list-as-string)
  * augmented_system_prompt injection
  * CLI wiring (ctx run --planner): spec captured in session metadata
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ctx.adapters.generic.planner import (
    PlanArtifact,
    Planner,
    _CODE_FENCE_RE,
    _extract_json,
    _safe_str,
    _safe_str_tuple,
    augmented_system_prompt,
)
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
            raise RuntimeError("scripted: no more responses")
        return self.responses.pop(0)


def _resp(content: str) -> CompletionResponse:
    return CompletionResponse(
        content=content,
        tool_calls=(),
        finish_reason="stop",
        usage=Usage(input_tokens=10, output_tokens=30),
        provider="scripted",
        model="x",
    )


_VALID_JSON = json.dumps({
    "summary": "Fix the three failing pytest cases.",
    "success_criteria": [
        "All tests pass.",
        "No new tests added.",
    ],
    "approach": "Read the traceback for each, fix the minimal change.",
    "out_of_scope": ["Refactor unrelated code."],
    "risks": ["The tests might be pinning wrong behaviour."],
})


# ── PlanArtifact ───────────────────────────────────────────────────────────


class TestPlanArtifact:
    def test_to_dict_shape(self) -> None:
        p = PlanArtifact(
            task="fix bugs",
            summary="summary text",
            success_criteria=("a", "b"),
            approach="do X",
            out_of_scope=("don't Y",),
            risks=("Z might break",),
            usage=Usage(input_tokens=5, output_tokens=10),
            raw_json=_VALID_JSON,
        )
        d = p.to_dict()
        assert d["task"] == "fix bugs"
        assert d["success_criteria"] == ["a", "b"]
        assert d["parsed_ok"] is True

    def test_to_markdown_shape(self) -> None:
        p = PlanArtifact(
            task="fix bugs",
            summary="Short summary.",
            success_criteria=("A", "B"),
            approach="Step 1, step 2.",
            out_of_scope=("nope 1",),
            risks=("risk 1",),
            usage=Usage(),
            raw_json="",
        )
        md = p.to_markdown()
        assert "# Task Plan" in md
        assert "**Task:** fix bugs" in md
        assert "## Summary" in md
        assert "## Success criteria" in md
        assert "- A" in md
        assert "- B" in md
        assert "## Approach" in md
        assert "## Out of scope" in md
        assert "- nope 1" in md
        assert "## Risks" in md
        assert "- risk 1" in md

    def test_to_markdown_skips_empty_sections(self) -> None:
        p = PlanArtifact(
            task="t",
            summary="s",
            success_criteria=(),
            approach="",
            out_of_scope=(),
            risks=(),
            usage=Usage(),
            raw_json="",
        )
        md = p.to_markdown()
        assert "## Success criteria" not in md
        assert "## Approach" not in md
        assert "## Risks" not in md

    def test_summary_fallback_message(self) -> None:
        p = PlanArtifact(
            task="t", summary="", success_criteria=(), approach="",
            out_of_scope=(), risks=(), usage=Usage(), raw_json="",
        )
        md = p.to_markdown()
        assert "_(no summary produced)_" in md

    def test_write_to_disk(self, tmp_path: Path) -> None:
        p = PlanArtifact(
            task="t", summary="s", success_criteria=("c",), approach="",
            out_of_scope=(), risks=(), usage=Usage(), raw_json="",
        )
        path = tmp_path / "nested" / "dir" / "plan.md"
        result = p.write(path)
        assert result == path
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "# Task Plan" in text


# ── _extract_json ─────────────────────────────────────────────────────────


class TestExtractJson:
    def test_empty(self) -> None:
        assert _extract_json("") is None
        assert _extract_json("   \n\n  ") is None

    def test_no_brace(self) -> None:
        assert _extract_json("plain prose") is None

    def test_clean_json(self) -> None:
        extracted = _extract_json(_VALID_JSON)
        assert extracted is not None
        data, ok = extracted
        assert ok is True
        assert data["summary"].startswith("Fix")

    def test_code_fenced_json(self) -> None:
        wrapped = f"```json\n{_VALID_JSON}\n```"
        extracted = _extract_json(wrapped)
        assert extracted is not None
        data, ok = extracted
        assert ok is False  # parsed after fallback, not clean
        assert data["summary"].startswith("Fix")

    def test_code_fenced_no_lang(self) -> None:
        wrapped = f"```\n{_VALID_JSON}\n```"
        extracted = _extract_json(wrapped)
        assert extracted is not None
        data, ok = extracted
        assert ok is False
        assert "summary" in data

    def test_prose_prefix_and_fence(self) -> None:
        wrapped = (
            "Here's the plan you asked for:\n\n"
            f"```json\n{_VALID_JSON}\n```\n\n"
            "Let me know if you want anything else."
        )
        extracted = _extract_json(wrapped)
        assert extracted is not None
        data, ok = extracted
        assert ok is False
        assert data["summary"].startswith("Fix")

    def test_prose_prefix_no_fence(self) -> None:
        wrapped = f"Here you go: {_VALID_JSON}"
        extracted = _extract_json(wrapped)
        assert extracted is not None
        data, ok = extracted
        assert ok is False
        assert data["summary"].startswith("Fix")

    def test_not_a_dict(self) -> None:
        # Top-level list is not a valid plan.
        assert _extract_json('["a", "b"]') is None

    def test_malformed_unrecoverable(self) -> None:
        assert _extract_json("{ unclosed object") is None


# ── _safe_str / _safe_str_tuple ──────────────────────────────────────────


class TestSafeCoercers:
    def test_safe_str_none(self) -> None:
        assert _safe_str(None) == ""

    def test_safe_str_number(self) -> None:
        assert _safe_str(42) == "42"

    def test_safe_str_strip(self) -> None:
        assert _safe_str("   hi   ") == "hi"

    def test_safe_str_tuple_none(self) -> None:
        assert _safe_str_tuple(None) == ()

    def test_safe_str_tuple_list(self) -> None:
        assert _safe_str_tuple(["a", "b", "c"]) == ("a", "b", "c")

    def test_safe_str_tuple_drops_empties(self) -> None:
        assert _safe_str_tuple(["a", "", "b", None]) == ("a", "b")

    def test_safe_str_tuple_string_fallback_newlines(self) -> None:
        """Model returns a single string where a list should be — split."""
        raw = "- first thing\n- second thing\n* third thing"
        assert _safe_str_tuple(raw) == ("first thing", "second thing", "third thing")

    def test_safe_str_tuple_non_list_non_string(self) -> None:
        assert _safe_str_tuple({"not": "a list"}) == ()


# ── Planner ───────────────────────────────────────────────────────────────


class TestPlanner:
    def test_happy_path(self) -> None:
        provider = _Scripted([_resp(_VALID_JSON)])
        plan = Planner(provider).plan("fix bugs")
        assert plan.parsed_ok is True
        assert plan.summary.startswith("Fix")
        assert len(plan.success_criteria) == 2
        assert plan.approach.startswith("Read")
        assert plan.task == "fix bugs"

    def test_unstructured_response_fallback(self) -> None:
        provider = _Scripted([_resp("This is not JSON, just prose.")])
        plan = Planner(provider).plan("fix bugs")
        assert plan.parsed_ok is False
        assert plan.summary == "This is not JSON, just prose."
        assert plan.success_criteria == ()

    def test_code_fenced_response_parsed(self) -> None:
        provider = _Scripted([_resp(f"```json\n{_VALID_JSON}\n```")])
        plan = Planner(provider).plan("fix bugs")
        assert plan.parsed_ok is False  # strategy 2 → ok=False
        assert plan.summary.startswith("Fix")

    def test_model_and_temperature_passed(self) -> None:
        provider = _Scripted([_resp(_VALID_JSON)])
        Planner(
            provider, model="openrouter/x", temperature=0.2, max_tokens=500,
        ).plan("t")
        call = provider.calls[0]
        assert call["model"] == "openrouter/x"
        assert call["temperature"] == 0.2
        assert call["max_tokens"] == 500

    def test_usage_captured(self) -> None:
        provider = _Scripted([_resp(_VALID_JSON)])
        plan = Planner(provider).plan("t")
        assert plan.usage.input_tokens == 10
        assert plan.usage.output_tokens == 30

    def test_context_appended_to_user_turn(self) -> None:
        provider = _Scripted([_resp(_VALID_JSON)])
        Planner(provider).plan("fix bugs", context="src/ has 3 failing tests")
        user_msg = next(
            m for m in provider.calls[0]["messages"] if m.role == "user"
        )
        assert "src/ has 3 failing tests" in user_msg.content
        assert "Task: fix bugs" in user_msg.content

    def test_system_prompt_present(self) -> None:
        provider = _Scripted([_resp(_VALID_JSON)])
        Planner(provider).plan("t")
        sys_msg = next(
            m for m in provider.calls[0]["messages"] if m.role == "system"
        )
        assert "planner agent" in sys_msg.content.lower()
        assert "JSON" in sys_msg.content

    def test_empty_response(self) -> None:
        provider = _Scripted([_resp("")])
        plan = Planner(provider).plan("t")
        assert plan.parsed_ok is False
        assert plan.summary == ""


# ── augmented_system_prompt ───────────────────────────────────────────────


class TestAugmentedSystemPrompt:
    def test_embeds_plan_into_prompt(self) -> None:
        plan = PlanArtifact(
            task="t",
            summary="do the thing",
            success_criteria=("pass tests",),
            approach="straight line",
            out_of_scope=(),
            risks=(),
            usage=Usage(),
            raw_json="",
        )
        augmented = augmented_system_prompt(
            "You are a coding assistant.",
            plan,
        )
        assert "You are a coding assistant." in augmented
        assert "# Task Plan" in augmented
        assert "do the thing" in augmented
        assert "pass tests" in augmented
        assert "execution spec" in augmented.lower()

    def test_separator_present(self) -> None:
        plan = PlanArtifact(
            task="t", summary="s", success_criteria=(), approach="",
            out_of_scope=(), risks=(), usage=Usage(), raw_json="",
        )
        augmented = augmented_system_prompt("base", plan)
        assert "---" in augmented


# ── CLI integration (ctx run --planner) ─────────────────────────────────


@pytest.fixture()
def fake_litellm_two_calls(monkeypatch: pytest.MonkeyPatch):
    """Stub LiteLLM: planner call returns JSON, generator returns 'done'."""
    fake = types.ModuleType("litellm")
    calls: list[dict[str, Any]] = []
    responses = [
        # Planner call: valid JSON spec.
        {
            "choices": [
                {
                    "message": {"content": _VALID_JSON},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 100},
        },
        # Generator call: plain stop.
        {
            "choices": [
                {
                    "message": {"content": "final answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 60, "completion_tokens": 10},
        },
    ]

    def completion(**kwargs):
        calls.append(kwargs)
        if not responses:
            raise RuntimeError("fake_litellm: ran out of canned responses")
        return responses.pop(0)

    fake.completion = completion  # type: ignore[attr-defined]
    fake._calls = calls           # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


class TestCliIntegration:
    def test_planner_flag_persists_plan_in_metadata(
        self,
        fake_litellm_two_calls: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ctx.cli.run import main

        exit_code = main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "fix the failing tests",
                "--sessions-dir", str(tmp_path),
                "--session-id", "plan-run",
                "--planner",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert exit_code == 0
        # session_start event should carry the plan.
        first_line = (tmp_path / "plan-run.jsonl").read_text(encoding="utf-8").splitlines()[0]
        event = json.loads(first_line)
        assert event["type"] == "session_start"
        assert event["planner_used"] is True
        assert event["plan"] is not None
        assert event["plan"]["summary"].startswith("Fix")
        assert event["plan_usage"]["input_tokens"] == 50

    def test_planner_omitted_by_default(
        self,
        fake_litellm_two_calls: Any,
        tmp_path: Path,
    ) -> None:
        from ctx.cli.run import main

        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "t",
                "--sessions-dir", str(tmp_path),
                "--session-id", "no-plan",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        first_line = (
            (tmp_path / "no-plan.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        event = json.loads(first_line)
        assert event["planner_used"] is False
        assert event["plan"] is None

    def test_planner_model_override(
        self,
        fake_litellm_two_calls: Any,
        tmp_path: Path,
    ) -> None:
        from ctx.cli.run import main

        main(
            [
                "run",
                "--model", "ollama/x",
                "--planner-model", "openrouter/y",
                "--task", "t",
                "--sessions-dir", str(tmp_path),
                "--session-id", "planner-model-override",
                "--planner",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        # First call should be planner with the override model.
        planner_call = fake_litellm_two_calls._calls[0]
        assert planner_call["model"] == "openrouter/y"
        # Second call (generator) keeps the primary model.
        generator_call = fake_litellm_two_calls._calls[1]
        assert generator_call["model"] == "ollama/x"


# ── Regex sanity ───────────────────────────────────────────────────────────


class TestCodeFenceRegex:
    def test_matches_json_fence(self) -> None:
        assert _CODE_FENCE_RE.search("```json\n{}\n```") is not None

    def test_matches_plain_fence(self) -> None:
        assert _CODE_FENCE_RE.search("```\n{}\n```") is not None

    def test_no_match_without_newlines(self) -> None:
        # The fence spec requires a newline after the opener for body.
        assert _CODE_FENCE_RE.search("```json {} ```") is None
