"""ctx.adapters.generic.compaction — context compaction for long runs.

When a run_loop session accumulates enough turns that the next
provider call risks hitting the model's context window, this module
condenses the middle of the conversation into a single summary
assistant message. Head (system prompt + initial task) and tail (last
N messages, including any live tool calls) are preserved verbatim.

Strategy (v1 — summarize-on-overflow, per Plan 001 §6):

    [system, user(task), m1, m2, m3, ..., mN-10, ..., mN-1, assistant(last)]
     └── head ──┘        └── middle (compacted) ──┘ └── tail ──┘

                                    ↓ compact()

    [system, user(task), assistant(SUMMARY), ..., mN-10, ..., mN-1, assistant(last)]

The summary is produced by calling the same provider with a short
summarisation system prompt over the middle slice. One extra provider
call per compaction; cost lands in the session's usage budget like
any other call.

Why char-based trigger instead of token-counting?
  Token counting is provider-specific (tiktoken for OpenAI,
  tokenizers for HF, SentencePiece for Mistral). A character proxy
  is ~4x the token count for English and holds roughly across models.
  For v1 it is a reasonable trigger that works without a tokenizer
  dependency. Callers who need precise token accounting can write a
  custom ``ContextCompactor`` implementation against the Protocol.

Limitations (deferred):
  * No branching / multi-path compaction (LangGraph territory).
  * No structured summary of tool-call history — the summariser sees
    the full message content including tool arguments/results and
    must condense. Works well enough in practice for coding tasks.
  * No token-budget guarantee on the summary itself — v1 prompts the
    model to be brief but trusts it to stay under a few hundred words.

Plan 001 Phase H5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from ctx.adapters.generic.providers import (
    Message,
    ModelProvider,
    Usage,
)


_logger = logging.getLogger(__name__)


_DEFAULT_SUMMARY_PROMPT = (
    "You are a context compactor. The user will send you the middle "
    "portion of a conversation between a user and an AI assistant "
    "(the head and tail have been trimmed). Produce a compact "
    "factual summary of what was discussed, what tools were used "
    "with what arguments, what decisions were made, and what state "
    "the conversation ended in. Be specific — preserve file paths, "
    "slugs, values, and tool names verbatim. Do not add commentary, "
    "advice, or meta-description. Use at most 400 words."
)

_DEFAULT_COMPACTION_NOTICE_PREFIX = "[Compacted"


class ContextCompactor(Protocol):
    """Decides when the loop's conversation needs compacting, then does it.

    ``should_compact`` is a pure check; ``compact`` performs the
    mutation and may make a provider call. Implementations must
    return a NEW list; the loop swaps it in place of the live one.
    """

    def should_compact(self, messages: list[Message]) -> bool: ...

    def compact(
        self,
        messages: list[Message],
        provider: ModelProvider,
    ) -> list[Message]: ...


@dataclass(frozen=True)
class CompactionResult:
    """Return shape for ``compact_now`` — the explicit (non-Protocol)
    helper callers can use if they want the details (e.g. to log how
    many messages were collapsed).

    ``usage`` carries the summary provider-call's tokens + cost so the
    loop can fold it into running budget totals (codex review fix #6 —
    summary calls are real provider calls and must count). Defaults to
    a zeroed ``Usage`` for compactors that don't expose this.
    """

    new_messages: list[Message]
    compacted_count: int
    summary: str
    usage: Usage = field(default_factory=Usage)


class TokenBudgetCompactor:
    """Default ``ContextCompactor``: char-threshold trigger + summarise middle.

    Configuration:
      max_chars        - total char budget for the conversation. When
                         the live char count exceeds this, compaction
                         fires. Rough proxy for tokens (~4 chars/token).
      max_messages     - secondary cap; fires when message count
                         exceeds this, regardless of char count. Stops
                         very-many-short-messages runs from slipping
                         past the char trigger.
      keep_head        - how many messages from the start to preserve
                         (default 2: system + initial user task).
      keep_tail        - how many messages from the end to preserve.
                         Must be even enough to keep the last
                         assistant+tool pair intact.
      min_middle       - don't compact if the middle has fewer than
                         this many messages. Prevents churning on
                         trivially-short conversations.
      summary_model    - optional model override for the summary call.
                         None = use the loop's default.
      summary_prompt   - system prompt for the summarisation call.

    Trade-offs:
      * keep_tail too low → loses recent tool-call context the model
        was about to act on. Default 10 covers ~5 turns.
      * keep_tail too high → compaction saves less, triggers more
        often, costs more in provider calls.
      * max_chars too low → false-positive compactions shrink the
        conversation below what the model needed to reason.
      * max_chars too high → waits until the actual context window
        error, which provider-side surfaces as a hard failure.
        Pick a value well under the model's advertised window.
    """

    def __init__(
        self,
        *,
        max_chars: int = 60_000,        # ~15k tokens, safe for 32k+ ctx
        max_messages: int = 80,
        keep_head: int = 2,
        keep_tail: int = 10,
        min_middle: int = 4,
        summary_model: str | None = None,
        summary_prompt: str = _DEFAULT_SUMMARY_PROMPT,
        summary_max_tokens: int = 600,
    ) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars must be > 0")
        if max_messages <= 0:
            raise ValueError("max_messages must be > 0")
        if keep_head < 0 or keep_tail < 0:
            raise ValueError("keep_head and keep_tail must be >= 0")
        if keep_head + keep_tail >= max_messages:
            raise ValueError(
                "keep_head + keep_tail must be less than max_messages "
                "(otherwise compaction would never have a middle slice)"
            )
        self._max_chars = max_chars
        self._max_messages = max_messages
        self._keep_head = keep_head
        self._keep_tail = keep_tail
        self._min_middle = min_middle
        self._summary_model = summary_model
        self._summary_prompt = summary_prompt
        self._summary_max_tokens = summary_max_tokens

    # ── ContextCompactor Protocol ───────────────────────────────────────

    def should_compact(self, messages: list[Message]) -> bool:
        if len(messages) > self._max_messages:
            return True
        chars = _char_count(messages)
        return chars > self._max_chars

    def compact(
        self,
        messages: list[Message],
        provider: ModelProvider,
    ) -> list[Message]:
        """Condense the middle slice. Returns a NEW list (never mutates).

        Drops summary-call usage on the floor. Use ``compact_with_usage``
        when you need to attribute the summary cost to running budget
        totals.
        """
        return self.compact_with_usage(messages, provider).new_messages

    def compact_with_usage(
        self,
        messages: list[Message],
        provider: ModelProvider,
    ) -> "CompactionResult":
        """Condense the middle slice + return the summary-call usage.

        Codex review fix #6: the summary provider call costs real
        tokens and real money. ``compact()`` previously hid that cost.
        ``compact_with_usage()`` surfaces it via ``CompactionResult.usage``
        so the loop can add it to its running totals.
        """
        if len(messages) <= self._keep_head + self._keep_tail + self._min_middle:
            _logger.debug(
                "compact: skipping, only %d messages (need > %d for middle)",
                len(messages),
                self._keep_head + self._keep_tail + self._min_middle,
            )
            return CompactionResult(
                new_messages=list(messages),
                compacted_count=0,
                summary="",
                usage=Usage(),
            )

        head = messages[: self._keep_head]
        tail = messages[-self._keep_tail :]
        middle = messages[self._keep_head : -self._keep_tail]

        summary_text, summary_usage = self._summarise_middle_with_usage(middle, provider)
        notice_count = len(middle)
        notice = Message(
            role="assistant",
            content=(
                f"{_DEFAULT_COMPACTION_NOTICE_PREFIX} {notice_count} prior messages.] "
                f"{summary_text}"
            ),
        )
        return CompactionResult(
            new_messages=[*head, notice, *tail],
            compacted_count=notice_count,
            summary=summary_text,
            usage=summary_usage,
        )

    # ── internals ───────────────────────────────────────────────────────

    def _summarise_middle(
        self,
        middle: list[Message],
        provider: ModelProvider,
    ) -> str:
        """Call the provider with a summary prompt over the middle slice.

        Drops the usage; callers that need it call
        ``_summarise_middle_with_usage`` directly.
        """
        text, _usage = self._summarise_middle_with_usage(middle, provider)
        return text

    def _summarise_middle_with_usage(
        self,
        middle: list[Message],
        provider: ModelProvider,
    ) -> tuple[str, Usage]:
        """Like ``_summarise_middle`` but also returns the summary call's usage."""
        stringified = _render_messages_for_summary(middle)
        summary_messages = [
            Message(role="system", content=self._summary_prompt),
            Message(
                role="user",
                content=(
                    "Here is the middle of a longer conversation. Summarise:\n\n"
                    f"{stringified}"
                ),
            ),
        ]
        try:
            response = provider.complete(
                summary_messages,
                tools=None,
                model=self._summary_model,
                temperature=0.3,
                max_tokens=self._summary_max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "compact: summary call failed (%s); leaving middle as "
                "'[compaction failed]' stub",
                exc,
            )
            return "[compaction summary failed; original turns omitted]", Usage()
        text = response.content.strip() or "[summary was empty]"
        return text, response.usage


# ── Helpers ──────────────────────────────────────────────────────────────


def _char_count(messages: list[Message]) -> int:
    """Rough char proxy for token count — sums content + tool-call JSON."""
    import json as _json

    total = 0
    for msg in messages:
        total += len(msg.content or "")
        if msg.tool_calls:
            for tc in msg.tool_calls:
                total += len(tc.name) + len(_json.dumps(tc.arguments, default=str))
        if msg.tool_call_id:
            total += len(msg.tool_call_id)
        if msg.name:
            total += len(msg.name)
    return total


def _render_messages_for_summary(messages: list[Message]) -> str:
    """Linearise a message list into a compact string the summariser can read."""
    import json as _json

    lines: list[str] = []
    for msg in messages:
        role = msg.role.upper()
        if msg.role == "assistant" and msg.tool_calls:
            lines.append(
                f"[{role}] (tool_calls: "
                + ", ".join(
                    f"{tc.name}({_json.dumps(tc.arguments, default=str)})"
                    for tc in msg.tool_calls
                )
                + ")"
            )
            if msg.content:
                lines.append(f"[{role}] {msg.content}")
        elif msg.role == "tool":
            lines.append(f"[TOOL RESULT {msg.name or ''}] {msg.content}")
        else:
            lines.append(f"[{role}] {msg.content}")
    return "\n".join(lines)


def compact_now(
    messages: list[Message],
    provider: ModelProvider,
    compactor: ContextCompactor | None = None,
) -> CompactionResult:
    """One-shot helper: force a compaction pass regardless of trigger.

    Useful for tests and for CLI tools that want to condense an
    existing session file. Returns a ``CompactionResult`` with the
    new messages + diagnostics.

    If no ``compactor`` is supplied, uses ``TokenBudgetCompactor``'s
    defaults.
    """
    comp = compactor if compactor is not None else TokenBudgetCompactor()
    new_messages = comp.compact(messages, provider)
    summary = ""
    if len(new_messages) < len(messages):
        # Extract the summary text out of the notice message.
        for m in new_messages:
            if (
                m.role == "assistant"
                and m.content.startswith(_DEFAULT_COMPACTION_NOTICE_PREFIX)
            ):
                # Drop the "[Compacted N prior messages.] " prefix.
                parts = m.content.split("] ", 1)
                summary = parts[1] if len(parts) == 2 else m.content
                break
    compacted = len(messages) - len(new_messages) + (1 if summary else 0)
    return CompactionResult(
        new_messages=list(new_messages),
        compacted_count=max(0, compacted - 1),  # -1 for the notice we added
        summary=summary,
    )
