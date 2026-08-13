from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import pytest

from ctx.adapters.generic.engine_turn import EngineTurnController
from ctx.adapters.generic.loop import (
    DEFAULT_MAX_EPHEMERAL_CONTEXT_BYTES,
    TurnActivation,
    TurnAuthorization,
    TurnPreparation,
    run_loop,
)
from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
)


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=f"{name} tool", parameters={"type": "object"})


@dataclass
class _RecordingDelegate:
    preparation: TurnPreparation
    activation: TurnActivation | Usage | None
    authorization: TurnAuthorization | None
    result_usage: Usage | None
    close_usage: Usage | None
    provider_error: Exception | None = None
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def prepare_turn(
        self,
        iteration: int,
        messages: tuple[Message, ...],
        base_tools: tuple[ToolDefinition, ...],
        *,
        deadline_monotonic: float | None,
        cancel_event: threading.Event | None,
    ) -> TurnPreparation:
        self.calls.append(
            (
                "prepare",
                (iteration, messages, base_tools, deadline_monotonic, cancel_event),
            )
        )
        return self.preparation

    def activate_turn(self, iteration: int, capability_epoch: int) -> TurnActivation | Usage | None:
        self.calls.append(("activate", (iteration, capability_epoch)))
        return self.activation

    def authorize_tool_call(
        self,
        iteration: int,
        capability_epoch: int,
        call: ToolCall,
    ) -> TurnAuthorization | None:
        self.calls.append(("authorize", (iteration, capability_epoch, call)))
        return self.authorization

    def on_provider_request(self, iteration: int, capability_epoch: int) -> None:
        self.calls.append(("provider", (iteration, capability_epoch)))
        if self.provider_error is not None:
            raise self.provider_error

    def on_tool_result(
        self,
        iteration: int,
        capability_epoch: int,
        call: ToolCall,
        result: str,
        error: str | None,
    ) -> Usage | None:
        self.calls.append(("result", (iteration, capability_epoch, call, result, error)))
        return self.result_usage

    def close_turn(
        self,
        iteration: int,
        capability_epoch: int,
        outcome: str,
    ) -> Usage | None:
        self.calls.append(("close", (iteration, capability_epoch, outcome)))
        return self.close_usage


def test_recommendation_is_lower_authority_until_first_provider_request() -> None:
    controller = EngineTurnController(
        recommendation_context="CTX recommendation bundle",
        delegate=None,
    )

    first = controller.prepare_turn(
        1,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert first.ephemeral_context == ()
    assert first.ephemeral_user_context == ("CTX recommendation bundle",)

    controller.on_provider_request(1, first.capability_epoch)
    second = controller.prepare_turn(
        1,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert second.ephemeral_user_context == ()


def test_delegate_contract_is_preserved_while_recommendation_is_appended() -> None:
    base_tool = _tool("base")
    prepared_tool = _tool("prepared")
    activated_tool = _tool("activated")
    messages = (Message(role="user", content="task"),)
    cancel_event = threading.Event()
    preparation_usage = Usage(input_tokens=1, output_tokens=2, cost_usd=0.25)
    activation = TurnActivation(
        tools=(activated_tool,),
        usage=Usage(input_tokens=3, output_tokens=4),
    )
    authorization = TurnAuthorization(
        denial="delegate denial",
        usage=Usage(input_tokens=5, output_tokens=6),
    )
    result_usage = Usage(input_tokens=7, output_tokens=8)
    close_usage = Usage(input_tokens=9, output_tokens=10)
    delegate = _RecordingDelegate(
        preparation=TurnPreparation(
            ephemeral_context=("delegate system context",),
            ephemeral_user_context=("delegate user context",),
            tools=(prepared_tool,),
            capability_epoch=73,
            usage=preparation_usage,
        ),
        activation=activation,
        authorization=authorization,
        result_usage=result_usage,
        close_usage=close_usage,
    )
    controller = EngineTurnController(
        recommendation_context="committed recommendation",
        delegate=delegate,
    )

    prepared = controller.prepare_turn(
        4,
        messages,
        (base_tool,),
        deadline_monotonic=123.5,
        cancel_event=cancel_event,
    )

    assert prepared == TurnPreparation(
        ephemeral_context=("delegate system context",),
        ephemeral_user_context=("delegate user context", "committed recommendation"),
        tools=(prepared_tool,),
        capability_epoch=73,
        usage=preparation_usage,
    )
    assert controller.activate_turn(4, 73) is activation
    call = ToolCall(id="call-1", name="prepared", arguments={})
    assert controller.authorize_tool_call(4, 73, call) is authorization
    assert controller.on_tool_result(4, 73, call, "result", None) is result_usage
    controller.on_provider_request(4, 73)
    assert controller.close_turn(4, 73, "stop") is close_usage
    assert delegate.calls == [
        ("prepare", (4, messages, (base_tool,), 123.5, cancel_event)),
        ("activate", (4, 73)),
        ("authorize", (4, 73, call)),
        ("result", (4, 73, call, "result", None)),
        ("provider", (4, 73)),
        ("close", (4, 73, "stop")),
    ]

    after_submission = controller.prepare_turn(
        1,
        messages,
        (base_tool,),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert after_submission is delegate.preparation


@pytest.mark.parametrize(
    "value,error",
    [
        (1, TypeError),
        ("", ValueError),
        (" \n\t", ValueError),
        ("x" * (DEFAULT_MAX_EPHEMERAL_CONTEXT_BYTES + 1), ValueError),
        ("\ud800", ValueError),
    ],
)
def test_recommendation_context_is_nonblank_bounded_utf8(
    value: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        EngineTurnController(recommendation_context=value, delegate=None)  # type: ignore[arg-type]


def test_utf8_byte_bound_accepts_multibyte_context_at_the_exact_limit() -> None:
    context = "é" * (DEFAULT_MAX_EPHEMERAL_CONTEXT_BYTES // 2)

    prepared = EngineTurnController(
        recommendation_context=context,
        delegate=None,
    ).prepare_turn(
        1,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )

    assert prepared.ephemeral_user_context == (context,)


def test_failed_provider_request_hook_does_not_consume_and_close_releases_lease() -> None:
    delegate = _RecordingDelegate(
        preparation=TurnPreparation(capability_epoch=19),
        activation=None,
        authorization=None,
        result_usage=None,
        close_usage=None,
        provider_error=RuntimeError("delegate rejected request"),
    )
    controller = EngineTurnController(
        recommendation_context="committed recommendation",
        delegate=delegate,
    )
    first = controller.prepare_turn(
        2,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )

    with pytest.raises(RuntimeError, match="delegate rejected"):
        controller.on_provider_request(2, first.capability_epoch)
    while_open = controller.prepare_turn(
        3,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert while_open.ephemeral_user_context == ()

    controller.close_turn(2, first.capability_epoch, "controller_error")
    delegate.provider_error = None
    retry = controller.prepare_turn(
        1,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert retry.ephemeral_user_context == ("committed recommendation",)
    controller.on_provider_request(1, retry.capability_epoch)
    assert (
        controller.prepare_turn(
            1,
            (),
            (),
            deadline_monotonic=None,
            cancel_event=None,
        ).ephemeral_user_context
        == ()
    )


def test_close_releases_recommendation_even_when_delegate_close_raises() -> None:
    class _RaisingCloseDelegate(_RecordingDelegate):
        def close_turn(
            self,
            iteration: int,
            capability_epoch: int,
            outcome: str,
        ) -> Usage | None:
            super().close_turn(iteration, capability_epoch, outcome)
            raise RuntimeError("close failed")

    delegate = _RaisingCloseDelegate(
        preparation=TurnPreparation(capability_epoch=31),
        activation=None,
        authorization=None,
        result_usage=None,
        close_usage=None,
    )
    controller = EngineTurnController(
        recommendation_context="committed recommendation",
        delegate=delegate,
    )
    first = controller.prepare_turn(
        1,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )

    with pytest.raises(RuntimeError, match="close failed"):
        controller.close_turn(1, first.capability_epoch, "cancelled")

    retry = controller.prepare_turn(
        2,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert retry.ephemeral_user_context == ("committed recommendation",)


def test_concurrent_preparations_grant_one_recommendation_lease() -> None:
    controller = EngineTurnController(
        recommendation_context="committed recommendation",
        delegate=None,
    )

    def prepare(iteration: int) -> TurnPreparation:
        return controller.prepare_turn(
            iteration,
            (),
            (),
            deadline_monotonic=None,
            cancel_event=None,
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        preparations = tuple(executor.map(prepare, range(1, 65)))

    winners = tuple(item for item in preparations if item.ephemeral_user_context)
    assert len(winners) == 1
    winner = winners[0]
    winner_iteration = preparations.index(winner) + 1
    controller.on_provider_request(winner_iteration, winner.capability_epoch)
    assert prepare(100).ephemeral_user_context == ()


def test_overlapping_identical_epochs_cannot_release_the_lease_early() -> None:
    controller = EngineTurnController(
        recommendation_context="committed recommendation",
        delegate=None,
    )

    first = controller.prepare_turn(
        1,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    overlapping = controller.prepare_turn(
        1,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert first.ephemeral_user_context == ("committed recommendation",)
    assert overlapping.ephemeral_user_context == ()

    controller.close_turn(1, overlapping.capability_epoch, "cancelled")
    still_reserved = controller.prepare_turn(
        1,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert still_reserved.ephemeral_user_context == ()

    controller.close_turn(1, first.capability_epoch, "cancelled")
    controller.close_turn(1, still_reserved.capability_epoch, "cancelled")
    retry = controller.prepare_turn(
        2,
        (),
        (),
        deadline_monotonic=None,
        cancel_event=None,
    )
    assert retry.ephemeral_user_context == ("committed recommendation",)


@dataclass
class _RecordingProvider:
    name: str = "recording"
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
        del tools, model, temperature, max_tokens
        self.calls.append(list(messages))
        return CompletionResponse(
            content="done",
            tool_calls=(),
            finish_reason="stop",
            usage=Usage(input_tokens=1, output_tokens=1),
            provider=self.name,
            model="test-model",
        )


def test_same_controller_submits_lower_authority_context_once_across_loop_rounds() -> None:
    provider = _RecordingProvider()
    controller = EngineTurnController(
        recommendation_context="committed recommendation",
        delegate=None,
    )

    for task in ("first evaluator round", "second evaluator round"):
        result = run_loop(
            provider=provider,
            system_prompt="trusted system",
            task=task,
            turn_controller=controller,
            max_iterations=1,
        )
        assert result.stop_reason == "completed"

    assert len(provider.calls) == 2
    first_system = tuple(
        message.content for message in provider.calls[0] if message.role == "system"
    )
    first_user = tuple(message.content for message in provider.calls[0] if message.role == "user")
    second_user = tuple(message.content for message in provider.calls[1] if message.role == "user")
    assert all("committed recommendation" not in content for content in first_system)
    assert any(content.startswith("committed recommendation") for content in first_user)
    assert all("committed recommendation" not in content for content in second_user)
