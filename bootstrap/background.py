from __future__ import annotations

from agent.llm import LLMProvider
from bootstrap.config import Settings
from bus.event_bus import EventBus
from bus.message_bus import MessageBus
from drift.registry import DriftTaskRegistry
from drift.runtime import DriftRuntime
from memory.runtime import MemoryRuntime
from proactive.judge import ProactiveJudge
from proactive.runtime import ProactiveRuntime
from proactive.sources import ProactiveSourceRegistry
from session.manager import SessionManager


def create_proactive_runtime(
    config: Settings,
    memory_runtime: MemoryRuntime,
    message_bus: MessageBus,
    event_bus: EventBus,
    llm_provider: LLMProvider,
    fast_llm_provider: LLMProvider | None = None,
) -> ProactiveRuntime:
    source_registry = ProactiveSourceRegistry()
    judge = ProactiveJudge(llm_provider, memory_runtime, fast_llm_provider)
    return ProactiveRuntime(
        config=config,
        source_registry=source_registry,
        judge=judge,
        memory_runtime=memory_runtime,
        message_bus=message_bus,
        event_bus=event_bus,
    )


def create_drift_runtime(
    config: Settings,
    memory_runtime: MemoryRuntime,
    session_manager: SessionManager,
    proactive_runtime: ProactiveRuntime,
    llm_provider: LLMProvider,
    event_bus: EventBus,
    fast_llm_provider: LLMProvider | None = None,
) -> DriftRuntime:
    return DriftRuntime(
        config=config,
        task_registry=DriftTaskRegistry(),
        memory_runtime=memory_runtime,
        session_manager=session_manager,
        proactive_runtime=proactive_runtime,
        llm_provider=llm_provider,
        event_bus=event_bus,
        fast_llm_provider=fast_llm_provider,
    )
