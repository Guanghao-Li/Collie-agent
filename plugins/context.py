from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.llm import LLMProvider
from bootstrap.config import Settings
from bus.event_bus import EventBus
from bus.message_bus import MessageBus
from drift.runtime import DriftRuntime
from memory.runtime import MemoryRuntime
from proactive.runtime import ProactiveRuntime
from tools.registry import ToolRegistry


@dataclass(slots=True)
class PluginContext:
    config: Settings
    workspace: Path
    event_bus: EventBus
    tool_registry: ToolRegistry
    memory_runtime: MemoryRuntime
    proactive_runtime: ProactiveRuntime
    drift_runtime: DriftRuntime
    message_bus: MessageBus
    llm_provider: LLMProvider
    main_llm_provider: LLMProvider
    fast_llm_provider: LLMProvider
