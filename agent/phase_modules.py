from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from agent.frame import TurnFrame
from agent.phases import PhaseName
from agent.slots import slot_enabled, slot_int, slot_values
from bus.event_bus import (
    AfterMemoryExtractEvent,
    AfterTurnEvent,
    BeforeMemoryExtractEvent,
    BeforeTurnEvent,
    PromptRenderEvent,
)
from bus.models import OutboundMessage

if TYPE_CHECKING:
    from agent.loop import AgentLoop


@dataclass(slots=True)
class UserActivityModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.BEFORE_TURN
    priority: ClassVar[int] = 10

    def run(self, frame: TurnFrame) -> None:
        if self.loop.on_user_activity:
            self.loop.on_user_activity()


@dataclass(slots=True)
class CommandModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.BEFORE_TURN
    priority: ClassVar[int] = 20

    async def run(self, frame: TurnFrame) -> None:
        result = await self.loop.commands.handle(frame.session_id, frame.content)
        if result.handled:
            frame.abort = True
            frame.abort_reply = result.response
            frame.abort_reason = "command"
            frame.response = result.response
            return
        if result.replacement_content:
            frame.content = result.replacement_content


@dataclass(slots=True)
class BeforeTurnEventModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.BEFORE_TURN
    priority: ClassVar[int] = 30

    async def run(self, frame: TurnFrame) -> None:
        if frame.abort:
            return
        if self.loop.trace_recorder is not None and frame.trace is None:
            frame.trace = self.loop.trace_recorder.start_trace(
                frame.session_id,
                frame.content,
            )
        await self.loop.event_bus.publish(
            BeforeTurnEvent(session_id=frame.session_id, user_message=frame.content)
        )


@dataclass(slots=True)
class IntentModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.BEFORE_REASONING
    priority: ClassVar[int] = 10

    async def run(self, frame: TurnFrame) -> None:
        if frame.abort:
            return
        frame.intent = await self.loop._classify_intent(frame.session_id, frame.content)
        if frame.trace is not None:
            frame.trace.intent = frame.intent.to_dict()


@dataclass(slots=True)
class RecentMessagesModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.BEFORE_REASONING
    priority: ClassVar[int] = 20

    def run(self, frame: TurnFrame) -> None:
        if frame.abort:
            return
        frame.recent = self.loop.session_manager.get_messages(
            frame.session_id,
            limit=self.loop.config.memory.max_recent_messages,
        )


@dataclass(slots=True)
class MemoryContextModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.BEFORE_REASONING
    priority: ClassVar[int] = 30

    async def run(self, frame: TurnFrame) -> None:
        if frame.abort:
            return
        frame.memory_context = await self.loop.memory_runtime.build_memory_context(
            frame.content,
            frame.recent,
        )
        if frame.trace is not None:
            frame.trace.memory_context_chars = len(frame.memory_context)


@dataclass(slots=True)
class PromptRenderModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.PROMPT_RENDER
    priority: ClassVar[int] = 100

    async def run(self, frame: TurnFrame) -> None:
        if frame.abort:
            return
        if frame.intent is None:
            raise RuntimeError("IntentModule must set frame.intent before prompt render.")
        frame.messages = self.loop.prompt_builder.build(
            frame.content,
            frame.recent,
            frame.memory_context,
        )
        frame.messages.insert(1, {"role": "system", "content": frame.intent.to_system_hint()})
        section_top = slot_values(frame, "prompt:section_top:")
        if section_top:
            frame.messages.insert(1, {"role": "system", "content": "\n\n".join(section_top)})
        section_bottom = slot_values(frame, "prompt:section_bottom:")
        if section_bottom:
            user_index = next(
                (
                    index
                    for index, message in enumerate(frame.messages)
                    if message.get("role") == "user"
                ),
                len(frame.messages),
            )
            frame.messages.insert(
                user_index,
                {"role": "system", "content": "\n\n".join(section_bottom)},
            )
        if frame.trace is not None:
            frame.trace.prompt_message_count = len(frame.messages)
        await self.loop.event_bus.publish(
            PromptRenderEvent(session_id=frame.session_id, messages=frame.messages)
        )


@dataclass(slots=True)
class ReasonerModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.REASONER
    priority: ClassVar[int] = 10

    async def run(self, frame: TurnFrame) -> None:
        if frame.abort:
            return
        max_tool_rounds = slot_int(frame, "reasoning:max_tool_rounds", 3)
        frame.response = await self.loop._complete_with_tools(
            frame.session_id,
            frame.messages,
            max_tool_rounds=max_tool_rounds,
            trace=frame.trace,
            frame=frame,
        )


@dataclass(slots=True)
class ResponseCleanupModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.AFTER_REASONING
    priority: ClassVar[int] = 10

    def run(self, frame: TurnFrame) -> None:
        if frame.abort:
            return
        frame.response = frame.response.strip()


@dataclass(slots=True)
class SessionPersistModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.AFTER_TURN
    priority: ClassVar[int] = 10

    def run(self, frame: TurnFrame) -> None:
        if frame.abort or slot_enabled(frame, "session:skip_persist"):
            return
        self.loop.session_manager.append_message(frame.session_id, "user", frame.content)
        self.loop.session_manager.append_message(frame.session_id, "assistant", frame.response)


@dataclass(slots=True)
class MemoryExtractModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.AFTER_TURN
    priority: ClassVar[int] = 20

    async def run(self, frame: TurnFrame) -> None:
        if frame.abort or slot_enabled(frame, "memory:skip_extract"):
            return
        await self.loop.event_bus.publish(
            BeforeMemoryExtractEvent(
                session_id=frame.session_id,
                user_message=frame.content,
                assistant_message=frame.response,
            )
        )
        extracted = await self.loop.memory_runtime.extract_from_turn(
            frame.session_id,
            frame.content,
            frame.response,
        )
        if frame.trace is not None:
            frame.trace.memory_extracted_count = len(extracted)
        await self.loop.event_bus.publish(
            AfterMemoryExtractEvent(
                session_id=frame.session_id,
                extracted_count=len(extracted),
            )
        )


@dataclass(slots=True)
class AfterTurnEventModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.AFTER_TURN
    priority: ClassVar[int] = 30

    async def run(self, frame: TurnFrame) -> None:
        if frame.abort:
            return
        memory_skip_extract = slot_enabled(frame, "memory:skip_extract")
        await self.loop.event_bus.publish(
            AfterTurnEvent(
                session_id=frame.session_id,
                user_message=frame.content,
                assistant_message=frame.response,
                metadata={
                    "memory_extracted": True,
                    "memory_skip_extract": memory_skip_extract,
                    "intent": frame.intent.intent if frame.intent else None,
                },
            )
        )


@dataclass(slots=True)
class OutboundModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.AFTER_TURN
    priority: ClassVar[int] = 1000

    async def run(self, frame: TurnFrame) -> None:
        content = frame.abort_reply if frame.abort else frame.response
        frame.outbound = OutboundMessage(
            channel=frame.channel,
            session_id=frame.session_id,
            content=content or "",
        )
        await self.loop.message_bus.publish_outbound(frame.outbound)


@dataclass(slots=True)
class TraceFinalizeModule:
    loop: AgentLoop

    phase: ClassVar[PhaseName] = PhaseName.AFTER_TURN
    priority: ClassVar[int] = 1010

    def run(self, frame: TurnFrame) -> None:
        _write_trace_if_needed(self.loop.trace_recorder, frame)


def _write_trace_if_needed(trace_recorder: Any, frame: TurnFrame) -> None:
    if frame.trace is None or trace_recorder is None or frame.metadata.get("trace_written"):
        return
    if not frame.trace.finish_reason:
        frame.trace.finish_reason = "final_answer"
    trace_recorder.write(frame.trace)
    frame.metadata["trace_written"] = True
