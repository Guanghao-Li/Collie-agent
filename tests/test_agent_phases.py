from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from agent.frame import TurnFrame
from agent.models import CommandResult
from agent.phase_modules import CommandModule
from agent.phases import PhaseName, PhaseRunner
from bootstrap.app import build_app_runtime
from bootstrap.config import Settings
from bus.event_bus import (
    AfterLLMEvent,
    AfterMemoryExtractEvent,
    AfterTurnEvent,
    BaseEvent,
    BeforeLLMEvent,
    BeforeMemoryExtractEvent,
    BeforeTurnEvent,
    IntentClassifiedEvent,
    PromptRenderEvent,
)
from bus.models import InboundMessage


def _frame(content: str = "hello") -> TurnFrame:
    inbound = InboundMessage(
        channel="discord",
        session_id="s1",
        user_id="u1",
        content=content,
    )
    return TurnFrame(
        inbound=inbound,
        content=content,
        session_id=inbound.session_id,
        channel=inbound.channel,
        user_id=inbound.user_id,
    )


@dataclass(slots=True)
class _RecorderModule:
    name: str
    priority: int
    phase: PhaseName = PhaseName.BEFORE_TURN

    def run(self, frame: TurnFrame) -> None:
        frame.slots.setdefault("order", []).append(self.name)


@dataclass(slots=True)
class _SlotModule:
    phase: PhaseName
    priority: int
    slots: dict[str, object]

    def run(self, frame: TurnFrame) -> None:
        frame.slots.update(self.slots)


@dataclass(slots=True)
class _AbortModule:
    name: str
    priority: int
    phase: PhaseName = PhaseName.BEFORE_TURN

    def run(self, frame: TurnFrame) -> None:
        frame.slots.setdefault("order", []).append(self.name)
        frame.abort = True
        frame.abort_reply = "aborted"


class _FailingProvider:
    name = "failing"

    async def complete(self, messages, **kwargs):
        raise AssertionError("LLM should not be called")

    async def close(self) -> None:
        return None


class _AlwaysToolProvider:
    name = "always_tool"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        return f"<tool_call>{json.dumps({'name': 'calculator', 'arguments': {'expression': '1 + 1'}})}</tool_call>"

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_phase_runner_runs_modules_by_priority() -> None:
    runner = PhaseRunner()
    runner.register(_RecorderModule("third", 30))
    runner.register(_RecorderModule("first", 10))
    runner.register(_RecorderModule("second", 20))
    frame = _frame()

    await runner.run(PhaseName.BEFORE_TURN, frame)

    assert frame.slots["order"] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_phase_runner_stops_current_phase_after_abort() -> None:
    runner = PhaseRunner()
    runner.register(_RecorderModule("first", 10))
    runner.register(_AbortModule("abort", 20))
    runner.register(_RecorderModule("never", 30))
    frame = _frame()

    await runner.run(PhaseName.BEFORE_TURN, frame)

    assert frame.abort is True
    assert frame.abort_reply == "aborted"
    assert frame.slots["order"] == ["first", "abort"]


def test_turn_frame_from_inbound_sets_message_fields() -> None:
    inbound = InboundMessage(
        channel="discord",
        session_id="session-1",
        user_id="user-1",
        content="  hello  ",
        metadata={"source": "test"},
    )

    frame = TurnFrame.from_inbound(inbound)

    assert frame.inbound is inbound
    assert frame.content == "hello"
    assert frame.session_id == "session-1"
    assert frame.channel == "discord"
    assert frame.user_id == "user-1"
    assert frame.metadata == {"source": "test"}
    assert frame.metadata is not inbound.metadata


class _FakeCommands:
    async def handle(self, session_id: str, content: str) -> CommandResult:
        return CommandResult(handled=True, response=f"handled:{session_id}:{content}")


class _FakeLoop:
    commands = _FakeCommands()


@pytest.mark.asyncio
async def test_command_module_sets_abort_frame() -> None:
    frame = _frame("!help")

    await CommandModule(_FakeLoop()).run(frame)  # type: ignore[arg-type]

    assert frame.abort is True
    assert frame.abort_reason == "command"
    assert frame.abort_reply == "handled:s1:!help"
    assert frame.response == "handled:s1:!help"


@pytest.mark.asyncio
async def test_command_abort_returns_directly_without_turn_events(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()
    events: list[type[object]] = []
    runtime.event_bus.subscribe(BaseEvent, lambda event: events.append(type(event)))

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="!help")
    )

    assert "!help" in outbound.content
    assert runtime.session_manager.get_messages("c1") == []
    assert events == []
    assert not (tmp_path / "traces" / "agent_traces.jsonl").exists()
    assert await runtime.message_bus.receive_outbound() == outbound
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_regular_message_runs_complete_default_phase_flow(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()
    events: list[type[object]] = []
    runtime.event_bus.subscribe(BaseEvent, lambda event: events.append(type(event)))

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="你好")
    )

    assert outbound.content == "回声：你好"
    assert [message.role for message in runtime.session_manager.get_messages("c1")] == [
        "user",
        "assistant",
    ]
    assert [message.content for message in runtime.session_manager.get_messages("c1")] == [
        "你好",
        "回声：你好",
    ]
    assert events == [
        BeforeTurnEvent,
        IntentClassifiedEvent,
        PromptRenderEvent,
        BeforeLLMEvent,
        AfterLLMEvent,
        BeforeMemoryExtractEvent,
        AfterMemoryExtractEvent,
        AfterTurnEvent,
    ]
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_prompt_section_slots_are_injected_in_sorted_order(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()
    rendered: list[list[dict[str, str]]] = []
    runtime.event_bus.subscribe(PromptRenderEvent, lambda event: rendered.append(event.messages))
    runtime.phase_runner.register(
        _SlotModule(
            phase=PhaseName.PROMPT_RENDER,
            priority=20,
            slots={
                "prompt:section_top:z": "top-z",
                "prompt:section_top:a": 123,
                "prompt:section_bottom:z": "bottom-z",
                "prompt:section_bottom:a": False,
            },
        )
    )

    await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="你好")
    )

    messages = rendered[0]
    assert messages[0]["role"] == "system"
    assert "可用工具" in messages[0]["content"]
    assert messages[1] == {"role": "system", "content": "123\n\ntop-z"}
    assert messages[2]["role"] == "system"
    assert "意图识别结果" in messages[2]["content"]
    assert messages[-2] == {"role": "system", "content": "False\n\nbottom-z"}
    assert messages[-1] == {"role": "user", "content": "你好"}
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_abort_reply_slot_returns_without_calling_llm(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()
    runtime.agent_loop.llm_provider = _FailingProvider()
    runtime.phase_runner.register(
        _SlotModule(
            phase=PhaseName.BEFORE_REASONING,
            priority=40,
            slots={"session:abort_reply": "slot abort"},
        )
    )

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="你好")
    )

    assert outbound.content == "slot abort"
    assert runtime.session_manager.get_messages("c1") == []
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_reasoning_max_tool_rounds_slot_overrides_default(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()
    provider = _AlwaysToolProvider()
    runtime.agent_loop.llm_provider = provider
    runtime.phase_runner.register(
        _SlotModule(
            phase=PhaseName.REASONER,
            priority=1,
            slots={"reasoning:max_tool_rounds": 1},
        )
    )

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="use tool")
    )

    assert outbound.content == "工具调用次数已达到上限。"
    assert provider.calls == 2
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_skip_persist_and_memory_extract_slots(tmp_path) -> None:
    config = Settings()
    config.plugins.enabled = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.session_manager.initialize()
    await runtime.memory_runtime.initialize()
    events: list[object] = []
    runtime.event_bus.subscribe(BaseEvent, lambda event: events.append(event))
    runtime.phase_runner.register(
        _SlotModule(
            phase=PhaseName.AFTER_REASONING,
            priority=20,
            slots={"session:skip_persist": True, "memory:skip_extract": True},
        )
    )

    outbound = await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="你好")
    )

    assert outbound.content == "回声：你好"
    assert runtime.session_manager.get_messages("c1") == []
    assert not any(isinstance(event, BeforeMemoryExtractEvent) for event in events)
    assert not any(isinstance(event, AfterMemoryExtractEvent) for event in events)
    after_turn = [event for event in events if isinstance(event, AfterTurnEvent)][0]
    assert after_turn.metadata["memory_skip_extract"] is True
    await runtime.llm_provider.close()
