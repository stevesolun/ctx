"""ctx.adapters.generic.contract — sprint contracts (third P/G/E agent).

Third and final agent in Anthropic's P/G/E triad. Runs AFTER the
Planner, BEFORE the Generator. Its job is to close the ambiguity
gap between the Planner's plain-English success criteria and the
Evaluator's pass/fail decision — by refining each criterion into
a testable contract clause with an explicit pass condition, fail
condition, and (where applicable) a measurable threshold.

Why bother?
  Plan 001 §5 describes the evidence-driven rollout: solo agent
  first, add Planner when decomposition fails, add Evaluator when
  self-praise bias appears, add CONTRACTS when Generator and
  Evaluator disagree on what "done" means in practice. Vague
  Planner criteria like "performance is acceptable" or "handle
  edge cases" give both agents too much latitude. A contract
  clause like "p95 latency < 200ms on the happy path" or
  "function returns 422 when input is empty" removes the slop.

One provider call: takes a ``PlanArtifact`` + the raw task,
produces a ``Contract`` whose ``criteria`` are typed
``ContractCriterion`` objects with explicit conditions. The
Evaluator then grades against the contract's ``pass_condition``
strings rather than the plan's vague criteria.

Shape:

    task (str) + PlanArtifact ─► ContractBuilder.build() ─► Contract
                                           │
                                           │ (one provider call,
                                           │  low temperature)
                                           ▼
                                     JSON with per-criterion
                                     pass_condition + fail_condition
                                     (+ optional metric/threshold)

Integration: ``run_with_evaluation(..., contract=contract)`` swaps
the evaluator's criteria for the contract's ``pass_condition`` list.
Everything else in the run_with_evaluation flow stays the same —
contract is additive, opt-in, and keeps its cost visible in the
returned ``total_usage``.

Plan 001 Phase H12 (final harness phase).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ctx.adapters.generic.planner import PlanArtifact, _extract_json, _safe_str
from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ModelProvider,
    Usage,
)


_logger = logging.getLogger(__name__)


_DEFAULT_CONTRACT_PROMPT = """\
You are a contract agent. Your job is to take a task + a planner's
spec, and refine each success criterion into a testable contract
clause with explicit pass/fail conditions. This contract will be
the source of truth for an independent evaluator agent — so each
clause must be unambiguous enough that two reasonable evaluators
would reach the same verdict.

Your output MUST be valid JSON matching this exact schema:

{
  "summary": "1-sentence restatement of the contract's goal",
  "criteria": [
    {
      "name": "<short kebab-case id, e.g. 'empty-input-handled'>",
      "description": "<one sentence — what the criterion means>",
      "pass_condition": "<concrete, observable condition that marks pass>",
      "fail_condition": "<concrete, observable condition that marks fail>",
      "metric": "<optional — name of the measurable if one applies>",
      "threshold": "<optional — 'op value', e.g. '<200', '>=0.8'>"
    }
  ],
  "scope_in": ["<explicit in-scope item>"],
  "scope_out": ["<explicit out-of-scope item>"],
  "approach": "<one-paragraph high-level strategy — not granular>"
}

Principles:
  * Each pass_condition must be observable — a third party reading
    the output can say yes/no without interpretation. Avoid
    adjectives. "Function returns 422 on empty input" is good;
    "Function handles empty input well" is bad.
  * fail_condition names ONE concrete way the criterion can go
    wrong. Use it to disambiguate when the assistant gets half-way.
  * metric + threshold are OPTIONAL — fill them only when there IS
    a measurable (latency, coverage, error rate, etc.). Leave
    empty otherwise.
  * Preserve the planner's criteria verbatim when they were already
    testable. Refinement is addition + specificity, not rewriting.
  * Return ONLY the JSON object. No prose, no markdown fences, no
    "Here is your contract" preamble.
"""


# ── Data types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContractCriterion:
    """One clause of a Contract — testable pass/fail per the agent flow."""

    name: str
    description: str
    pass_condition: str
    fail_condition: str = ""
    metric: str = ""
    threshold: str = ""

    def as_evaluator_criterion(self) -> str:
        """Render this clause as a single string the Evaluator consumes.

        Evaluator's ``criteria`` list is a tuple of strings, one per
        clause. We stitch the pass_condition + (optional) threshold
        so the evaluator's grading prompt carries the contract's
        concrete language.
        """
        core = self.pass_condition.strip() or self.description.strip() or self.name
        if self.metric and self.threshold:
            return f"{core} (metric: {self.metric} {self.threshold})"
        if self.metric:
            return f"{core} (metric: {self.metric})"
        return core


@dataclass(frozen=True)
class Contract:
    """Sprint contract — Planner's spec refined into testable clauses."""

    task: str
    summary: str
    criteria: tuple[ContractCriterion, ...]
    scope_in: tuple[str, ...]
    scope_out: tuple[str, ...]
    approach: str
    usage: Usage
    raw_json: str
    parsed_ok: bool = True

    def as_evaluator_criteria(self) -> tuple[str, ...]:
        """Flatten the clauses into the Evaluator's ``criteria`` shape."""
        return tuple(c.as_evaluator_criterion() for c in self.criteria)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "summary": self.summary,
            "criteria": [
                {
                    "name": c.name,
                    "description": c.description,
                    "pass_condition": c.pass_condition,
                    "fail_condition": c.fail_condition,
                    "metric": c.metric,
                    "threshold": c.threshold,
                }
                for c in self.criteria
            ],
            "scope_in": list(self.scope_in),
            "scope_out": list(self.scope_out),
            "approach": self.approach,
            "parsed_ok": self.parsed_ok,
        }

    def to_markdown(self) -> str:
        """Human-readable contract artifact — what you'd commit to disk."""
        lines = [
            "# Sprint Contract",
            "",
            f"**Task:** {self.task}",
            "",
            "## Summary",
            self.summary or "_(no summary)_",
            "",
        ]
        if self.criteria:
            lines.append("## Criteria")
            for i, c in enumerate(self.criteria, 1):
                lines.append(f"### {i}. {c.name}")
                if c.description:
                    lines.append(c.description)
                lines.append("")
                if c.pass_condition:
                    lines.append(f"- **Pass:** {c.pass_condition}")
                if c.fail_condition:
                    lines.append(f"- **Fail:** {c.fail_condition}")
                if c.metric:
                    t = f" (threshold: {c.threshold})" if c.threshold else ""
                    lines.append(f"- **Metric:** {c.metric}{t}")
                lines.append("")
        if self.scope_in:
            lines.append("## In scope")
            lines.extend(f"- {s}" for s in self.scope_in)
            lines.append("")
        if self.scope_out:
            lines.append("## Out of scope")
            lines.extend(f"- {s}" for s in self.scope_out)
            lines.append("")
        if self.approach:
            lines.append("## Approach")
            lines.append(self.approach)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


# ── Builder ───────────────────────────────────────────────────────────────


class ContractBuilder:
    """Wraps a ``ModelProvider`` with the contract-refinement prompt."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        model: str | None = None,
        system_prompt: str = _DEFAULT_CONTRACT_PROMPT,
        temperature: float = 0.2,
        max_tokens: int = 1500,
    ) -> None:
        self._provider = provider
        self._model = model
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens

    def build(
        self,
        task: str,
        plan: PlanArtifact | None = None,
    ) -> Contract:
        """Produce a Contract from a task (+ optional Planner spec).

        Without a plan: the builder still produces a contract by
        treating the task as the whole spec, but the resulting
        criteria will be less richly grounded. In practice the
        H12 CLI flag requires --planner, so this case is a
        library escape hatch.
        """
        user_content = self._format_user_turn(task, plan)
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

    # ── internals ────────────────────────────────────────────────────────

    def _format_user_turn(
        self, task: str, plan: PlanArtifact | None,
    ) -> str:
        body = f"TASK:\n{task.strip()}"
        if plan is not None:
            plan_md = plan.to_markdown().strip()
            body += f"\n\nPLANNER SPEC:\n{plan_md}"
        body += (
            "\n\nProduce the contract JSON per the schema in the "
            "system prompt. Respond with ONLY the JSON object."
        )
        return body

    def _parse_response(
        self, task: str, response: CompletionResponse,
    ) -> Contract:
        raw = response.content or ""
        extracted = _extract_json(raw)
        if extracted is None:
            _logger.warning(
                "ContractBuilder: no parseable JSON; returning stub "
                "contract with raw text as summary"
            )
            return Contract(
                task=task,
                summary=raw.strip() or "(contract builder returned empty)",
                criteria=(),
                scope_in=(),
                scope_out=(),
                approach="",
                usage=response.usage,
                raw_json=raw,
                parsed_ok=False,
            )
        data, parsed_ok = extracted
        criteria = _parse_criteria_list(data.get("criteria"))
        return Contract(
            task=task,
            summary=_safe_str(data.get("summary")),
            criteria=criteria,
            scope_in=_safe_str_tuple(data.get("scope_in")),
            scope_out=_safe_str_tuple(data.get("scope_out")),
            approach=_safe_str(data.get("approach")),
            usage=response.usage,
            raw_json=raw,
            parsed_ok=parsed_ok,
        )


# ── Helpers ────────────────────────────────────────────────────────────────


def _parse_criteria_list(raw: Any) -> tuple[ContractCriterion, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[ContractCriterion] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name = _safe_str(item.get("name"))
        if not name:
            # Fall back to a synthesised kebab-case id so every
            # criterion has a stable identifier.
            name = f"criterion-{idx + 1}"
        out.append(
            ContractCriterion(
                name=name,
                description=_safe_str(item.get("description")),
                pass_condition=_safe_str(item.get("pass_condition")),
                fail_condition=_safe_str(item.get("fail_condition")),
                metric=_safe_str(item.get("metric")),
                threshold=_safe_str(item.get("threshold")),
            )
        )
    return tuple(out)


def _safe_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
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


# ── Integration helper — wrap augmented_system_prompt with the contract ──


def augmented_system_prompt_with_contract(
    base_system_prompt: str,
    contract: Contract,
) -> str:
    """Embed a Contract into the Generator's system prompt.

    Mirror of ctx.adapters.generic.planner.augmented_system_prompt
    but richer — the Generator sees the testable clauses up front,
    so even without the evaluator's feedback loop it aims for the
    same bar the evaluator will grade against.
    """
    base = base_system_prompt.strip()
    contract_md = contract.to_markdown().strip()
    separator = "\n\n---\n\n"
    return (
        base
        + separator
        + "A contract agent has refined the task into the following "
        + "testable clauses. Treat each criterion's pass_condition "
        + "as a non-negotiable checkbox — your answer will be graded "
        + "against them verbatim.\n\n"
        + contract_md
        + "\n"
    )
