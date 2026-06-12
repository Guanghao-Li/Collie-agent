from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from agent.commands import AgentCommands
from agent.intent import IntentDecision, IntentRouter
from agent.llm import LLMProvider
from agent.models import ToolCall
from agent.prompt import PromptBuilder
from agent.trace import AgentTrace, TraceRecorder
from bootstrap.config import Settings
from bus.event_bus import (
    AfterLLMEvent,
    AfterMemoryExtractEvent,
    AfterTurnEvent,
    BeforeLLMEvent,
    BeforeMemoryExtractEvent,
    BeforeTurnEvent,
    EventBus,
    IntentClassifiedEvent,
    PromptRenderEvent,
    ToolCallEvent,
)
from bus.message_bus import MessageBus
from bus.models import InboundMessage, OutboundMessage
from memory.runtime import MemoryRuntime
from plugins.manager import PluginManager
from session.manager import SessionManager
from tools.registry import ToolError, ToolRegistry


class AgentLoop:
    def __init__(
        self,
        config: Settings,
        message_bus: MessageBus,
        event_bus: EventBus,
        session_manager: SessionManager,
        memory_runtime: MemoryRuntime,
        tool_registry: ToolRegistry,
        plugin_manager: PluginManager | None,
        llm_provider: LLMProvider,
        commands: AgentCommands,
        intent_router: IntentRouter | None = None,
        trace_recorder: TraceRecorder | None = None,
        on_user_activity: callable | None = None,
    ) -> None:
        self.config = config
        self.message_bus = message_bus
        self.event_bus = event_bus
        self.session_manager = session_manager
        self.memory_runtime = memory_runtime
        self.tool_registry = tool_registry
        self.plugin_manager = plugin_manager
        self.llm_provider = llm_provider
        self.commands = commands
        self.intent_router = intent_router
        self.trace_recorder = trace_recorder
        self.on_user_activity = on_user_activity
        self.prompt_builder = PromptBuilder(config, tool_registry)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._logger = logging.getLogger(__name__)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self.run_loop(), name="agent-loop")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_loop(self) -> None:
        while self._running:
            message = await self.message_bus.receive_inbound()
            try:
                await self.process_message(message)
            except Exception:
                self._logger.exception("Agent 单轮处理失败")
                await self.message_bus.publish_outbound(
                    OutboundMessage(
                        channel=message.channel,
                        session_id=message.session_id,
                        content="处理这一轮消息时发生了内部错误。",
                    )
                )

    async def process_message(self, message: InboundMessage) -> OutboundMessage:
        if self.on_user_activity:
            self.on_user_activity()
        content = message.content.strip()
        command_result = await self.commands.handle(message.session_id, content)
        if command_result.handled:
            outbound = OutboundMessage(
                channel=message.channel,
                session_id=message.session_id,
                content=command_result.response,
            )
            await self.message_bus.publish_outbound(outbound)
            return outbound
        if command_result.replacement_content:
            content = command_result.replacement_content

        trace = self.trace_recorder.start_trace(message.session_id, content) if self.trace_recorder else None
        try:
            await self.event_bus.publish(
                BeforeTurnEvent(session_id=message.session_id, user_message=content)
            )
            intent = await self._classify_intent(message.session_id, content)
            if trace is not None:
                trace.intent = intent.to_dict()

            recent = self.session_manager.get_messages(
                message.session_id,
                limit=self.config.memory.max_recent_messages,
            )
            memory_context = await self.memory_runtime.build_memory_context(content, recent)
            if trace is not None:
                trace.memory_context_chars = len(memory_context)
            messages = self.prompt_builder.build(content, recent, memory_context)
            messages.insert(1, {"role": "system", "content": intent.to_system_hint()})
            if trace is not None:
                trace.prompt_message_count = len(messages)
            await self.event_bus.publish(
                PromptRenderEvent(session_id=message.session_id, messages=messages)
            )

            response = await self._complete_with_tools(message.session_id, messages, trace=trace)
            self.session_manager.append_message(message.session_id, "user", content)
            self.session_manager.append_message(message.session_id, "assistant", response)

            await self.event_bus.publish(
                BeforeMemoryExtractEvent(
                    session_id=message.session_id,
                    user_message=content,
                    assistant_message=response,
                )
            )
            extracted = await self.memory_runtime.extract_from_turn(
                message.session_id,
                content,
                response,
            )
            if trace is not None:
                trace.memory_extracted_count = len(extracted)
            await self.event_bus.publish(
                AfterMemoryExtractEvent(
                    session_id=message.session_id,
                    extracted_count=len(extracted),
                )
            )
            await self.event_bus.publish(
                AfterTurnEvent(
                    session_id=message.session_id,
                    user_message=content,
                    assistant_message=response,
                    metadata={"memory_extracted": True, "intent": intent.intent},
                )
            )

            outbound = OutboundMessage(
                channel=message.channel,
                session_id=message.session_id,
                content=response,
            )
            await self.message_bus.publish_outbound(outbound)
            return outbound
        except Exception as exc:
            if trace is not None:
                trace.finish_reason = "error"
                trace.error = self.trace_recorder.preview(str(exc)) if self.trace_recorder else str(exc)
            raise
        finally:
            if trace is not None and self.trace_recorder is not None:
                if not trace.finish_reason:
                    trace.finish_reason = "final_answer"
                self.trace_recorder.write(trace)

    async def _classify_intent(self, session_id: str, content: str) -> IntentDecision:
        if self.intent_router is None:
            return IntentDecision(
                intent="general_chat",
                confidence=0.0,
                route="chat",
                reason="Intent router is not configured.",
            )
        decision = await self.intent_router.classify(content)
        await self.event_bus.publish(
            IntentClassifiedEvent(
                session_id=session_id,
                intent=decision.intent,
                confidence=decision.confidence,
                route=decision.route,
                entities=decision.entities,
            )
        )
        return decision

    async def _complete_with_tools(
        self,
        session_id: str,
        messages: list[dict[str, str]],
        max_tool_rounds: int = 3,
        trace: AgentTrace | None = None,
    ) -> str:
        response = ""
        for round_index in range(max_tool_rounds + 1):
            await self.event_bus.publish(BeforeLLMEvent(session_id=session_id, messages=messages))
            started = time.perf_counter()
            response = await self.llm_provider.complete(messages, purpose="agent_loop")
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self.event_bus.publish(AfterLLMEvent(session_id=session_id, response=response))
            call = parse_tool_call(response)
            if self.trace_recorder is not None:
                self.trace_recorder.record_llm_step(
                    trace,
                    round_index=round_index,
                    purpose="agent_loop",
                    latency_ms=latency_ms,
                    response=response,
                    has_tool_call=call is not None,
                    tool_name=call.name if call else None,
                )
            if call is None:
                if trace is not None:
                    trace.finish_reason = "final_answer"
                return response
            if round_index >= max_tool_rounds:
                if trace is not None:
                    trace.finish_reason = "max_tool_rounds"
                return "工具调用次数已达到上限。"
            tool_started = time.perf_counter()
            tool_error: str | None = None
            try:
                result = await self.tool_registry.call_tool(call.name, call.arguments)
            except ToolError as exc:
                tool_error = str(exc)
                result = {"error": str(exc)}
            tool_latency_ms = int((time.perf_counter() - tool_started) * 1000)
            if self.trace_recorder is not None:
                self.trace_recorder.record_tool_step(
                    trace,
                    round_index=round_index,
                    tool_name=call.name,
                    arguments=call.arguments,
                    result=result,
                    latency_ms=tool_latency_ms,
                    error=tool_error,
                )
            await self.event_bus.publish(
                ToolCallEvent(
                    session_id=session_id,
                    tool_name=call.name,
                    arguments=call.arguments,
                    result=result,
                )
            )
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "tool", "content": json.dumps(result, ensure_ascii=False)})
        return response


def parse_tool_call(text: str) -> ToolCall | None:
    match = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        payload: dict[str, Any] = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    name = payload.get("name")
    if not isinstance(name, str):
        return None
    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
    return ToolCall(name=name, arguments=arguments)
