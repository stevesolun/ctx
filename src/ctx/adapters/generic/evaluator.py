"""ctx.adapters.generic.evaluator — grading + revision loop (second P/G/E agent).

Anthropic's harness-design article calls self-evaluation out as a
hard anti-pattern: a Generator asked to grade its own work almost
always declares victory. The Evaluator is a separate agent run with
explicit grading criteria and a stricter temperature; its feedback,
when the verdict isn't "pass", becomes a revision directive fed
back into the Generator for up to N rounds.

Integration shape:

    run_with_evaluation(
        provider=...,
        system_prompt=...,
        task=...,
        evaluator=Evaluator(provider, criteria=[...]),
        max_rounds=2,
        planner=Planner(provider),   # optional — if set, P/G/E triad
        router=...,
        tool_executor=...,
        ...
    )

One call to the generator, one to the evaluator, zero-to-N
revision rounds. Each round is logged through the same session
observer as the rest of the run, so the trajectory is auditable.

Default criteria when no caller-supplied list + no planner:

    1. "Did the final answer address the task as stated?"
    2. "Is the answer factually grounded (no invented paths/APIs/names)?"
    3. "Are there obvious gaps the user would immediately ask about?"

When a Planner ran, its ``success_criteria`` become the default set —
the same spec that drove the Generator is what the Evaluator checks.

Plan 001 Phase H11.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from ctx.adapters.generic.contract import (
    Contract,
    ContractBuilder,
    augmented_system_prompt_with_contract,
)
from ctx.adapters.generic.loop import LoopObserver, LoopResult, ToolPolicy, run_loop
from ctx.adapters.generic.planner import PlanArtifact, Planner, augmented_system_prompt
from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ModelProvider,
    ToolDefinition,
    Usage,
)
from ctx.adapters.generic.tools import McpRouter


_logger = logging.getLogger(__name__)


Verdict = Literal["pass", "needs_revision", "fail"]


_DEFAULT_EVALUATOR_PROMPT = """\
You are an evaluator agent. Your job is to grade an assistant's
response against a set of criteria, and produce a structured
verdict the rest of the system can act on.

You will receive:
  * TASK: the original user request
  * CRITERIA: the standards to grade against
  * CONTEXT (optional): additional background (e.g. a planner spec)
  * ANSWER: the assistant's final response

Your output MUST be valid JSON matching this exact schema:

{
  "verdict": "pass" | "needs_revision" | "fail",
  "overall_score": 0.0-1.0,
  "criteria": [
    {"name": "<criterion short name>", "passed": true|false,
     "score": 0.0-1.0, "note": "<one sentence why>"}
  ],
  "summary_feedback": "2-4 sentences of evaluator-voice review",
  "revision_directive": "<short, actionable instruction to the
                         assistant for a revision round — only
                         populate when verdict != 'pass'>"
}

Principles:
  * Grade against the criteria as written. Do not invent new
    standards. Do not soften your assessment to be nice.
  * 'pass' means EVERY criterion passed. A single fail → at least
    'needs_revision'. Structural problems (task unaddressed, wrong
    output format) → 'fail'.
  * 'revision_directive' must be actionable. "Be better" is useless.
    "Address criterion 3: the function still errors on an empty
    input — handle that explicitly" is useful.
  * Return ONLY the JSON object. No prose before or after.
"""


_DEFAULT_CRITERIA: tuple[str, ...] = (
    "The final answer addresses the task as stated.",
    "The answer is factually grounded (no invented paths, APIs, or names).",
    "Obvious user-facing gaps are absent — nothing a user would immediately re-ask.",
)


# ── Data shapes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CriterionResult:
    """Per-criterion grading detail."""

    name: str
    passed: bool
    score: float
    note: str


@dataclass(frozen=True)
class EvaluationResult:
    """Structured output of an Evaluator.evaluate() call."""

    verdict: Verdict
    overall_score: float
    criterion_results: tuple[CriterionResult, ...]
    summary_feedback: str
    revision_directive: str
    usage: Usage
    raw_json: str
    parsed_ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "overall_score": self.overall_score,
            "criteria": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "score": c.score,
                    "note": c.note,
                }
                for c in self.criterion_results
            ],
            "summary_feedback": self.summary_feedback,
            "revision_directive": self.revision_directive,
            "parsed_ok": self.parsed_ok,
        }


@dataclass(frozen=True)
class EvaluationLoopResult:
    """Return shape of ``run_with_evaluation``.

    ``rounds`` is one entry per Generator+Evaluator pair; ``final``
    is the last Generator LoopResult; ``plan`` is populated when a
    Planner was supplied; ``contract`` is populated when a
    ContractBuilder was supplied (full P/G/E/C flow).
    """

    final: LoopResult
    rounds: tuple["EvaluationRound", ...]
    plan: PlanArtifact | None
    contract: Contract | None
    total_usage: Usage


@dataclass(frozen=True)
class EvaluationRound:
    """One Generator→Evaluator pair inside ``run_with_evaluation``."""

    index: int                    # 1-based
    loop_result: LoopResult
    evaluation: EvaluationResult
    revision_task: str            # the revision prompt fed into this round
                                   # (empty for round 1)


# ── Evaluator ──────────────────────────────────────────────────────────────


class Evaluator:
    """Wraps a ``ModelProvider`` with a grading system prompt."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        criteria: tuple[str, ...] | list[str] | None = None,
        model: str | None = None,
        system_prompt: str = _DEFAULT_EVALUATOR_PROMPT,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> None:
        self._provider = provider
        self._criteria: tuple[str, ...] = (
            tuple(criteria) if criteria else _DEFAULT_CRITERIA
        )
        self._model = model
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def criteria(self) -> tuple[str, ...]:
        return self._criteria

    def with_criteria(
        self, criteria: tuple[str, ...] | list[str],
    ) -> "Evaluator":
        """Return a fresh Evaluator with different criteria (same provider + model)."""
        return Evaluator(
            self._provider,
            criteria=tuple(criteria),
            model=self._model,
            system_prompt=self._system_prompt,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )

    def evaluate(
        self,
        *,
        task: str,
        answer: str,
        context: str = "",
    ) -> EvaluationResult:
        """Grade ``answer`` against this evaluator's criteria."""
        user_content = self._format_user_turn(task, answer, context)
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
        return self._parse_response(response)

    # ── internals ────────────────────────────────────────────────────────

    def _format_user_turn(
        self, task: str, answer: str, context: str,
    ) -> str:
        parts = [
            f"TASK:\n{task.strip()}",
            "CRITERIA:\n" + "\n".join(f"- {c}" for c in self._criteria),
        ]
        if context.strip():
            parts.append(f"CONTEXT:\n{context.strip()}")
        parts.append(f"ANSWER:\n{answer.strip() or '(empty)'}")
        parts.append(
            "Respond with ONLY the JSON verdict per the system-prompt schema."
        )
        return "\n\n".join(parts)

    def _parse_response(
        self, response: CompletionResponse,
    ) -> EvaluationResult:
        raw = response.content or ""
        extracted = _extract_json(raw)
        if extracted is None:
            _logger.warning(
                "Evaluator: provider returned no parseable JSON; "
                "falling back to 'fail' verdict with raw text as feedback"
            )
            return EvaluationResult(
                verdict="fail",
                overall_score=0.0,
                criterion_results=(),
                summary_feedback=raw.strip() or "Evaluator returned empty response.",
                revision_directive="Rework the answer — evaluator could not parse it.",
                usage=response.usage,
                raw_json=raw,
                parsed_ok=False,
            )
        data, parsed_ok = extracted
        verdict = _coerce_verdict(data.get("verdict"))
        criterion_results = _parse_criteria(data.get("criteria"))
        return EvaluationResult(
            verdict=verdict,
            overall_score=_coerce_score(data.get("overall_score")),
            criterion_results=criterion_results,
            summary_feedback=_safe_str(data.get("summary_feedback")),
            revision_directive=_safe_str(data.get("revision_directive")),
            usage=response.usage,
            raw_json=raw,
            parsed_ok=parsed_ok,
        )


# ── The top-level run_with_evaluation orchestrator ─────────────────────────


def run_with_evaluation(
    *,
    provider: ModelProvider,
    system_prompt: str,
    task: str,
    evaluator: Evaluator,
    max_rounds: int = 2,
    planner: Planner | None = None,
    contract_builder: ContractBuilder | None = None,
    # All other kwargs mirror run_loop.
    router: McpRouter | None = None,
    extra_tools: list[ToolDefinition] | None = None,
    tool_executor: Callable[..., str] | None = None,
    tool_policy: ToolPolicy | None = None,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    max_iterations: int = 25,
    budget_usd: float | None = None,
    budget_tokens: int | None = None,
    observer: LoopObserver | None = None,
    compactor: Any | None = None,
) -> EvaluationLoopResult:
    """Run Generator → Evaluator → (revise) until pass or round cap.

    Supports three progressively richer flows:

      * Solo G+E: planner=None, contract_builder=None — grade the
        Generator's first output against the evaluator's default
        criteria.
      * P/G/E: planner supplied. Planner runs first, its
        success_criteria replace the evaluator defaults, its spec
        is embedded in the Generator's system prompt.
      * P/C/G/E: also contract_builder supplied. After the planner,
        a contract agent refines the plan's success_criteria into
        testable pass/fail conditions; those conditions become the
        evaluator's grading criteria AND are embedded in the
        Generator's system prompt.

    Budgets (``budget_usd``, ``budget_tokens``) apply to the
    Generator call in each round, NOT to the planner / contract /
    evaluator calls. Their costs are tracked in ``total_usage`` so
    the caller sees the full picture.

    ``max_rounds`` caps the total Generator calls. 1 = solo agent
    with a grade applied at the end; 2 = one revision; etc.
    """
    if max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1 (got {max_rounds})")

    # Planner pass — if supplied, transform the system prompt + give
    # the evaluator its spec-based criteria.
    plan: PlanArtifact | None = None
    contract: Contract | None = None
    augmented_prompt = system_prompt
    active_evaluator = evaluator
    if planner is not None:
        plan = planner.plan(task)
        augmented_prompt = augmented_system_prompt(system_prompt, plan)
        if plan.success_criteria:
            active_evaluator = evaluator.with_criteria(plan.success_criteria)

    # Contract pass — runs AFTER the planner, BEFORE the Generator.
    # Replaces the evaluator's criteria with the contract's testable
    # clauses and overwrites the Generator's system prompt with the
    # contract markdown (more specific than the planner's).
    if contract_builder is not None:
        contract = contract_builder.build(task, plan=plan)
        if contract.criteria:
            active_evaluator = evaluator.with_criteria(
                contract.as_evaluator_criteria()
            )
            augmented_prompt = augmented_system_prompt_with_contract(
                system_prompt, contract,
            )

    # Total usage starts with the planner's + contract builder's cost.
    totals = _UsageTotals()
    if plan is not None:
        totals.add(plan.usage)
    if contract is not None:
        totals.add(contract.usage)

    rounds: list[EvaluationRound] = []
    round_index = 0
    next_task = task
    # The Generator's conversation carries over across rounds so the
    # revision call sees the prior assistant turn + tool outputs.
    accumulated_messages: list[Message] = []

    while round_index < max_rounds:
        round_index += 1

        # Generator call.
        loop_result = run_loop(
            provider=provider,
            system_prompt=augmented_prompt,
            task=next_task,
            router=router,
            extra_tools=extra_tools,
            tool_executor=tool_executor,
            tool_policy=tool_policy,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            max_iterations=max_iterations,
            budget_usd=budget_usd,
            budget_tokens=budget_tokens,
            observer=observer,
            compactor=compactor,
            messages=accumulated_messages[:] or None,
            append_task_after_messages=bool(accumulated_messages),
        )
        totals.add(loop_result.usage)

        # If the Generator's stop reason isn't "completed", skip
        # evaluation — there's no reasonable answer to grade. Record
        # the round as-is and break out.
        if loop_result.stop_reason != "completed":
            eval_result = EvaluationResult(
                verdict="fail",
                overall_score=0.0,
                criterion_results=(),
                summary_feedback=(
                    f"Generator stopped with {loop_result.stop_reason}; "
                    f"{loop_result.detail}"
                ),
                revision_directive="",
                usage=Usage(),
                raw_json="",
                parsed_ok=False,
            )
            rounds.append(
                EvaluationRound(
                    index=round_index,
                    loop_result=loop_result,
                    evaluation=eval_result,
                    revision_task=next_task if round_index > 1 else "",
                )
            )
            break

        # Evaluator call.
        plan_context = plan.to_markdown() if plan is not None else ""
        evaluation = active_evaluator.evaluate(
            task=task,
            answer=loop_result.final_message,
            context=plan_context,
        )
        totals.add(evaluation.usage)
        rounds.append(
            EvaluationRound(
                index=round_index,
                loop_result=loop_result,
                evaluation=evaluation,
                revision_task=next_task if round_index > 1 else "",
            )
        )

        if evaluation.verdict == "pass":
            break
        if round_index >= max_rounds:
            break

        # Prepare the next round's prompt + carry over conversation.
        accumulated_messages = list(loop_result.messages)
        next_task = _build_revision_task(task, evaluation)

    final = rounds[-1].loop_result if rounds else _empty_loop_result(task)
    return EvaluationLoopResult(
        final=final,
        rounds=tuple(rounds),
        plan=plan,
        contract=contract,
        total_usage=totals.as_usage(),
    )


# ── Helpers ────────────────────────────────────────────────────────────────


@dataclass
class _UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if usage.cost_usd is not None:
            self.cost_usd += usage.cost_usd

    def as_usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd if self.cost_usd > 0 else None,
        )


def _build_revision_task(original_task: str, evaluation: EvaluationResult) -> str:
    """Construct the next Generator turn when a revision is needed.

    Keeps the original task visible so the Generator doesn't lose
    track of the top-level goal when the evaluator's directive is
    tactical. Only populates the directive when it's non-empty to
    avoid "Please revise" with no detail.
    """
    directive = evaluation.revision_directive.strip()
    feedback = evaluation.summary_feedback.strip()
    if not directive and not feedback:
        return (
            "An evaluator flagged your prior answer for revision, but "
            "gave no actionable feedback. Re-read the original task "
            "and produce an improved answer.\n\n"
            f"Original task: {original_task}"
        )
    parts = ["The evaluator reviewed your previous answer."]
    if feedback:
        parts.append(f"Feedback: {feedback}")
    if directive:
        parts.append(f"Directive: {directive}")
    parts.append(f"Original task: {original_task}")
    parts.append("Produce a revised answer.")
    return "\n\n".join(parts)


def _empty_loop_result(task: str) -> LoopResult:
    return LoopResult(
        stop_reason="other",  # type: ignore[arg-type]  # conservative placeholder
        final_message="",
        iterations=0,
        usage=Usage(),
        messages=(),
        detail="no rounds ran",
    )


_CODE_FENCE_RE = re.compile(
    r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.MULTILINE | re.DOTALL
)


def _extract_json(text: str) -> tuple[dict[str, Any], bool] | None:
    """Three-strategy JSON extractor (same shape as planner's)."""
    if not text or not text.strip():
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, True
    except json.JSONDecodeError:
        pass
    m = _CODE_FENCE_RE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed, False
        except json.JSONDecodeError:
            pass
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
                try:
                    parsed = json.loads(text[start : i + 1])
                    if isinstance(parsed, dict):
                        return parsed, False
                except json.JSONDecodeError:
                    return None
                break
    return None


def _coerce_verdict(raw: Any) -> Verdict:
    if isinstance(raw, str):
        norm = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if norm in ("pass", "passed", "ok", "approved"):
            return "pass"
        if norm in ("needs_revision", "revise", "revision", "partial"):
            return "needs_revision"
    return "fail"


def _coerce_score(raw: Any) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


def _parse_criteria(raw: Any) -> tuple[CriterionResult, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[CriterionResult] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            CriterionResult(
                name=_safe_str(item.get("name")),
                passed=bool(item.get("passed")),
                score=_coerce_score(item.get("score")),
                note=_safe_str(item.get("note")),
            )
        )
    return tuple(out)


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
