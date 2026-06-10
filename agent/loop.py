from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from agent.commands import AgentCommands
from agent.llm import LLMProvider
from agent.models import ToolCall
from agent.prompt import PromptBuilder
from bootstrap.config import Settings
from bus.event_bus import (
    AfterLLMEvent,
    AfterMemoryExtractEvent,
    AfterTurnEvent,
    BeforeLLMEvent,
    BeforeMemoryExtractEvent,
    BeforeTurnEvent,
    EventBus,
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

        await self.event_bus.publish(
            BeforeTurnEvent(session_id=message.session_id, user_message=content)
        )
        recent = self.session_manager.get_messages(
            message.session_id,
            limit=self.config.memory.max_recent_messages,
        )
        memory_context = await self.memory_runtime.build_memory_context(content, recent)
        messages = self.prompt_builder.build(content, recent, memory_context)
        await self.event_bus.publish(PromptRenderEvent(session_id=message.session_id, messages=messages))

        response = await self._complete_with_tools(message.session_id, messages)
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
                metadata={"memory_extracted": True},
            )
        )

        outbound = OutboundMessage(
            channel=message.channel,
            session_id=message.session_id,
            content=response,
        )
        await self.message_bus.publish_outbound(outbound)
        return outbound

    async def _complete_with_tools(
        self,
        session_id: str,
        messages: list[dict[str, str]],
        max_tool_rounds: int = 3,
    ) -> str:
        response = ""
        for round_index in range(max_tool_rounds + 1):
            await self.event_bus.publish(BeforeLLMEvent(session_id=session_id, messages=messages))
            response = await self.llm_provider.complete(messages)
            await self.event_bus.publish(AfterLLMEvent(session_id=session_id, response=response))
            call = parse_tool_call(response)
            if call is None:
                return response
            if round_index >= max_tool_rounds:
                return "工具调用次数已达到上限。"
            try:
                result = await self.tool_registry.call_tool(call.name, call.arguments)
            except ToolError as exc:
                result = {"error": str(exc)}
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
