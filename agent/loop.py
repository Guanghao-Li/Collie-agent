from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable

from agent.commands import AgentCommands
from agent.frame import TurnFrame
from agent.intent import IntentDecision, IntentRouter
from agent.llm import LLMProvider
from agent.models import ToolCall
from agent.phase_modules import (
    AfterTurnEventModule,
    BeforeTurnEventModule,
    CommandModule,
    IntentModule,
    MemoryContextModule,
    MemoryExtractModule,
    OutboundModule,
    PromptRenderModule,
    ReasonerModule,
    RecentMessagesModule,
    ResponseCleanupModule,
    SessionPersistModule,
    TraceFinalizeModule,
    UserActivityModule,
    _write_trace_if_needed,
)
from agent.phases import PhaseName, PhaseRunner
from agent.prompt import PromptBuilder
from agent.slots import apply_abort_reply_slot
from agent.trace import AgentTrace, TraceRecorder
from bootstrap.config import Settings
from bus.event_bus import (
    AfterLLMEvent,
    BeforeLLMEvent,
    EventBus,
    IntentClassifiedEvent,
    ToolCallEvent,
)
from bus.message_bus import MessageBus
from bus.models import InboundMessage, OutboundMessage
from memory.runtime import MemoryRuntime
from plugins.manager import PluginManager
from session.manager import SessionManager
from tools.executor import ToolExecutor
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
        tool_executor: ToolExecutor | None,
        plugin_manager: PluginManager | None,
        llm_provider: LLMProvider,
        commands: AgentCommands,
        intent_router: IntentRouter | None = None,
        trace_recorder: TraceRecorder | None = None,
        on_user_activity: Callable[[], None] | None = None,
        phase_runner: PhaseRunner | None = None,
    ) -> None:
        self.config = config
        self.message_bus = message_bus
        self.event_bus = event_bus
        self.session_manager = session_manager
        self.memory_runtime = memory_runtime
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor or ToolExecutor(tool_registry)
        self.plugin_manager = plugin_manager
        self.llm_provider = llm_provider
        self.commands = commands
        self.intent_router = intent_router
        self.trace_recorder = trace_recorder
        self.on_user_activity = on_user_activity
        self.prompt_builder = PromptBuilder(config, tool_registry)
        self.phase_runner = phase_runner or PhaseRunner()
        self._register_default_phase_modules(self.phase_runner)
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

    def _register_default_phase_modules(self, runner: PhaseRunner) -> None:
        for module in (
            UserActivityModule(self),
            CommandModule(self),
            BeforeTurnEventModule(self),
            IntentModule(self),
            RecentMessagesModule(self),
            MemoryContextModule(self),
            PromptRenderModule(self),
            ReasonerModule(self),
            ResponseCleanupModule(self),
            SessionPersistModule(self),
            MemoryExtractModule(self),
            AfterTurnEventModule(self),
            OutboundModule(self),
            TraceFinalizeModule(self),
        ):
            runner.register(module)

    async def process_message(self, message: InboundMessage) -> OutboundMessage:
        frame = TurnFrame.from_inbound(message)
        try:
            await self._run_phase(PhaseName.BEFORE_TURN, frame)
            if not frame.abort:
                await self._run_phase(PhaseName.BEFORE_REASONING, frame)
            if not frame.abort:
                await self._run_phase(PhaseName.PROMPT_RENDER, frame)
            if not frame.abort:
                await self._run_phase(PhaseName.REASONER, frame)
            if not frame.abort:
                await self._run_phase(PhaseName.AFTER_REASONING, frame)
            await self._run_phase(PhaseName.AFTER_TURN, frame)
            if frame.outbound is None:
                content = frame.abort_reply if frame.abort else frame.response
                frame.outbound = OutboundMessage(
                    channel=frame.channel,
                    session_id=frame.session_id,
                    content=content or "",
                )
                await self.message_bus.publish_outbound(frame.outbound)
            return frame.outbound
        except Exception as exc:
            if frame.trace is not None:
                frame.trace.finish_reason = "error"
                frame.trace.error = (
                    self.trace_recorder.preview(str(exc)) if self.trace_recorder else str(exc)
                )
            raise
        finally:
            _write_trace_if_needed(self.trace_recorder, frame)

    async def _run_phase(self, phase: PhaseName, frame: TurnFrame) -> None:
        await self.phase_runner.run(phase, frame)
        apply_abort_reply_slot(frame)

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
        frame: TurnFrame | None = None,
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
                result = await self.tool_executor.call_tool(
                    call.name,
                    call.arguments,
                    frame=frame,
                )
            except ToolError as exc:
                tool_error = str(exc)
                result = {"error": str(exc)}
            executed_arguments = (
                dict(frame.slots.get("tool:last_arguments", call.arguments))
                if frame is not None
                else dict(self.tool_executor.last_arguments or call.arguments)
            )
            if (
                isinstance(result, dict)
                and (result.get("denied") or result.get("requires_confirmation"))
                and result.get("error")
            ):
                tool_error = str(result["error"])
            tool_latency_ms = int((time.perf_counter() - tool_started) * 1000)
            if self.trace_recorder is not None:
                self.trace_recorder.record_tool_step(
                    trace,
                    round_index=round_index,
                    tool_name=call.name,
                    arguments=executed_arguments,
                    result=result,
                    latency_ms=tool_latency_ms,
                    error=tool_error,
                )
            await self.event_bus.publish(
                ToolCallEvent(
                    session_id=session_id,
                    tool_name=call.name,
                    arguments=executed_arguments,
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
