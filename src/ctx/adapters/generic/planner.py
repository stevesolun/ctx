"""ctx.adapters.generic.planner — task-decomposition front-end.

One of Anthropic's three-agent components (Planner / Generator /
Evaluator). Runs BEFORE ``run_loop`` to convert a vague or
under-specified task into a structured spec the Generator can
execute against. File-based handoff (the spec is written to disk as
a markdown artifact, then injected into the Generator's system
prompt) matches the Anthropic harness-design article §5 pattern.

Use via ``--planner`` on ``ctx run`` (H7 CLI wires the flag), or
directly from Python:

    from ctx.adapters.generic.planner import Planner
    from ctx.adapters.generic.providers import get_provider

    provider = get_provider(default_model="openrouter/anthropic/claude-opus-4.7")
    plan = Planner(provider).plan("fix the failing tests")
    print(plan.to_markdown())

The spec shape is intentionally slim for coding agents (summary,
success criteria, approach, out-of-scope, risks). Anthropic's
article shows richer specs (data models, visual design) but those
are product-build specific — you can swap in a custom Planner with
a different artifact shape by using ``Planner`` as a reference
implementation rather than a hard dependency.

Evidence-driven delivery per Plan 001 §5 user decision: this phase
ships, but the planner is OPT-IN (``--planner``). Solo agent remains
the default until we measure runs where multi-step decomposition
actually helps (scope drift, spec-to-implementation gap).

Plan 001 Phase H10.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ModelProvider,
    Usage,
)


_logger = logging.getLogger(__name__)


_DEFAULT_PLANNER_PROMPT = """\
You are a planner agent. Your job is to take a vague or under-
specified coding task and produce a structured execution spec for
an implementation agent. Your output MUST be valid JSON matching
this exact schema:

{
  "summary": "1-2 sentence task restatement",
  "success_criteria": [
    "testable criterion 1",
    "testable criterion 2"
  ],
  "approach": "high-level strategy — steps not granular details",
  "out_of_scope": [
    "explicit non-goals to prevent scope creep"
  ],
  "risks": [
    "concrete risks or constraints the implementer should watch for"
  ]
}

Principles:
  * Success criteria must be testable. 'Make it fast' is bad;
    'Request latency under 200ms on the happy path' is good.
  * Approach is high-level — the implementer decides granular
    technical details. Do not pin specific libraries or file names
    unless the task explicitly requires them.
  * Out-of-scope prevents scope creep. List anything the task might
    invite but shouldn't include.
  * Risks are actionable — what could go wrong, what assumptions
    might be false, what edge cases to cover.
  * Be concise. 3-6 items per list is usually right.

Return ONLY the JSON object. No prose before or after, no markdown
code fences, no explanation.
"""


@dataclass(frozen=True)
class PlanArtifact:
    """Structured output of a Planner call.

    ``raw_json`` preserves the exact provider output for debugging
    + session replay. ``parsed_ok`` is False when the model returned
    unstructured text; callers can still use ``summary`` (which
    falls back to the raw text in that case) and decide whether to
    skip planning for this run.
    """

    task: str
    summary: str
    success_criteria: tuple[str, ...]
    approach: str
    out_of_scope: tuple[str, ...]
    risks: tuple[str, ...]
    usage: Usage
    raw_json: str
    parsed_ok: bool = True

    # ── Serialisation ──────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "summary": self.summary,
            "success_criteria": list(self.success_criteria),
            "approach": self.approach,
            "out_of_scope": list(self.out_of_scope),
            "risks": list(self.risks),
            "parsed_ok": self.parsed_ok,
        }

    def to_markdown(self) -> str:
        """Render as a markdown artifact suitable for injection as context."""
        lines = [
            "# Task Plan",
            "",
            f"**Task:** {self.task}",
            "",
            "## Summary",
            self.summary or "_(no summary produced)_",
            "",
        ]
        if self.success_criteria:
            lines.append("## Success criteria")
            lines.extend(f"- {c}" for c in self.success_criteria)
            lines.append("")
        if self.approach:
            lines.append("## Approach")
            lines.append(self.approach)
            lines.append("")
        if self.out_of_scope:
            lines.append("## Out of scope")
            lines.extend(f"- {c}" for c in self.out_of_scope)
            lines.append("")
        if self.risks:
            lines.append("## Risks")
            lines.extend(f"- {c}" for c in self.risks)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def write(self, path: Path) -> Path:
        """Write the plan to disk as a markdown file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(), encoding="utf-8")
        return path


# ── Planner ───────────────────────────────────────────────────────────────


class Planner:
    """Wraps a ``ModelProvider`` with a planner system prompt.

    Stateless across calls — a fresh ``plan()`` call makes a fresh
    provider invocation. Keeps track of cumulative usage via the
    returned ``PlanArtifact.usage``; the caller sums into the overall
    session budget.
    """

    def __init__(
        self,
        provider: ModelProvider,
        *,
        model: str | None = None,
        system_prompt: str = _DEFAULT_PLANNER_PROMPT,
        temperature: float = 0.4,
        max_tokens: int = 1200,
    ) -> None:
        self._provider = provider
        self._model = model
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens

    def plan(
        self,
        task: str,
        *,
        context: str = "",
    ) -> PlanArtifact:
        """Call the provider to produce a PlanArtifact for ``task``.

        ``context`` is optional additional background the planner
        should consider — file paths, project notes, recent errors.
        Threaded into the user-turn message so the provider sees it
        but the system prompt stays reusable.
        """
        user_content = self._format_user_turn(task, context)
        messages = [
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=user_content),
        ]
        response = self._provider.complete(
            messages,
            tools=None,
            model=self._model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        return self._parse_response(task, response)

    # ── internals ───────────────────────────────────────────────────────

    def _format_user_turn(self, task: str, context: str) -> str:
        body = f"Task: {task}"
        if context.strip():
            body += f"\n\nAdditional context:\n{context.strip()}"
        body += (
            "\n\nRespond with ONLY the JSON spec per the schema in "
            "the system prompt."
        )
        return body

    def _parse_response(
        self,
        task: str,
        response: CompletionResponse,
    ) -> PlanArtifact:
        raw = response.content or ""
        extracted = _extract_json(raw)
        if extracted is None:
            _logger.warning(
                "Planner: provider returned no parseable JSON; falling "
                "back to raw text as summary"
            )
            return PlanArtifact(
                task=task,
                summary=raw.strip(),
                success_criteria=(),
                approach="",
                out_of_scope=(),
                risks=(),
                usage=response.usage,
                raw_json=raw,
                parsed_ok=False,
            )
        data, parsed_ok = extracted
        return PlanArtifact(
            task=task,
            summary=_safe_str(data.get("summary")),
            success_criteria=_safe_str_tuple(data.get("success_criteria")),
            approach=_safe_str(data.get("approach")),
            out_of_scope=_safe_str_tuple(data.get("out_of_scope")),
            risks=_safe_str_tuple(data.get("risks")),
            usage=response.usage,
            raw_json=raw,
            parsed_ok=parsed_ok,
        )


# ── Helpers ────────────────────────────────────────────────────────────────


_CODE_FENCE_RE = re.compile(
    r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.MULTILINE | re.DOTALL
)


def _extract_json(text: str) -> tuple[dict[str, Any], bool] | None:
    """Pull a JSON object out of the provider's response.

    Models don't always follow "output only JSON" — common failure
    modes:
      * Wrapped in a ```json code fence
      * Prefixed with "Here's your plan:" or similar
      * Followed by a comment

    We try three strategies in order:
      1. Parse as-is.
      2. Strip a ```json...``` code fence and parse the body.
      3. Find the first {...} balanced-brace block and parse that.

    Returns (parsed_dict, parsed_ok) on success. None when none of
    the strategies yield a dict. ``parsed_ok=True`` means strategy 1
    worked cleanly; False means we had to fall back to stripping.
    """
    if not text or not text.strip():
        return None

    # Strategy 1: straight JSON parse.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, True
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip a code fence.
    m = _CODE_FENCE_RE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed, False
        except json.JSONDecodeError:
            pass

    # Strategy 3: first balanced-brace block.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, dict):
                        return parsed, False
                except json.JSONDecodeError:
                    return None
                break
    return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # Some models emit a single string where a list was expected;
        # split on newlines or bullets and clean.
        parts = [p.strip().lstrip("-*•").strip() for p in value.splitlines()]
        return tuple(p for p in parts if p)
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        s = _safe_str(item)
        if s:
            out.append(s)
    return tuple(out)


# ── Convenience: integrate with run_loop ───────────────────────────────────


def augmented_system_prompt(
    base_system_prompt: str,
    plan: PlanArtifact,
) -> str:
    """Return a system prompt that embeds the plan for the Generator.

    Typical use from the CLI:

        plan = Planner(provider).plan(task)
        result = run_loop(
            provider=provider,
            system_prompt=augmented_system_prompt(default_sys, plan),
            task=task,
            ...
        )
    """
    base = base_system_prompt.strip()
    plan_md = plan.to_markdown().strip()
    separator = "\n\n---\n\n"
    return (
        base
        + separator
        + "A planner agent has produced the following execution spec. "
        + "Use it to guide your work; do NOT restate the task — act on it.\n\n"
        + plan_md
        + "\n"
    )
