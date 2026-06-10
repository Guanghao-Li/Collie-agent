from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import asyncio
import logging

from agent.commands import AgentCommands
from agent.llm import LLMProvider
from agent.loop import AgentLoop
from bootstrap.background import create_drift_runtime, create_proactive_runtime
from bootstrap.config import Settings
from bootstrap.discord import create_discord_channel
from bootstrap.memory import create_memory_runtime
from bootstrap.plugins import create_plugin_manager
from bootstrap.providers import build_fast_provider, build_main_provider
from bootstrap.tools import create_tool_registry
from bus.event_bus import EventBus, ShutdownEvent, StartupEvent
from bus.message_bus import MessageBus
from channels.discord_channel import DiscordChannel
from drift.runtime import DriftRuntime
from memory.runtime import MemoryRuntime
from plugins.context import PluginContext
from plugins.manager import PluginManager
from proactive.runtime import ProactiveRuntime
from session.manager import SessionManager
from tools.registry import ToolRegistry


@dataclass(slots=True)
class AppRuntime:
    config: Settings
    workspace: Path
    project_root: Path
    llm_provider: LLMProvider
    main_llm_provider: LLMProvider
    fast_llm_provider: LLMProvider
    message_bus: MessageBus
    event_bus: EventBus
    session_manager: SessionManager
    memory_runtime: MemoryRuntime
    tool_registry: ToolRegistry
    plugin_manager: PluginManager
    discord_channel: DiscordChannel
    agent_loop: AgentLoop
    proactive_runtime: ProactiveRuntime
    drift_runtime: DriftRuntime
    runtime_state: dict[str, object] = field(default_factory=lambda: {"running": False})

    async def start(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        await self.session_manager.initialize()
        await self.memory_runtime.initialize()
        await self.plugin_manager.load_plugins()
        await self.event_bus.publish(StartupEvent())
        await self.discord_channel.start()
        await self.agent_loop.start()
        await self.proactive_runtime.start()
        await self.drift_runtime.start()
        self.runtime_state["running"] = True

    async def run(self) -> None:
        await self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if not self.runtime_state.get("running"):
            return
        self.runtime_state["running"] = False
        await self.event_bus.publish(ShutdownEvent())
        await self.discord_channel.stop()
        await self.agent_loop.stop()
        await self.proactive_runtime.stop()
        await self.drift_runtime.stop()
        self.session_manager.save_all()
        await self.llm_provider.close()
        if self.fast_llm_provider is not self.llm_provider:
            await self.fast_llm_provider.close()


def build_app_runtime(config: Settings, workspace: str | Path) -> AppRuntime:
    workspace_path = Path(workspace)
    project_root = Path(__file__).resolve().parents[1]
    main_llm_provider = build_main_provider(config)
    fast_llm_provider = build_fast_provider(config, main_llm_provider)
    llm_provider = main_llm_provider
    message_bus = MessageBus()
    event_bus = EventBus()
    session_manager = SessionManager(workspace_path, config.memory.max_recent_messages)
    memory_runtime = create_memory_runtime(workspace_path, config, llm_provider, fast_llm_provider)
    tool_registry = create_tool_registry(config)
    proactive_runtime = create_proactive_runtime(
        config,
        memory_runtime,
        message_bus,
        event_bus,
        llm_provider,
        fast_llm_provider,
    )
    drift_runtime = create_drift_runtime(
        config,
        memory_runtime,
        session_manager,
        proactive_runtime,
        llm_provider,
        event_bus,
        fast_llm_provider,
    )
    plugin_context = PluginContext(
        config=config,
        workspace=workspace_path,
        event_bus=event_bus,
        tool_registry=tool_registry,
        memory_runtime=memory_runtime,
        proactive_runtime=proactive_runtime,
        drift_runtime=drift_runtime,
        message_bus=message_bus,
        llm_provider=llm_provider,
        main_llm_provider=main_llm_provider,
        fast_llm_provider=fast_llm_provider,
    )
    plugin_manager = create_plugin_manager(config, plugin_context, project_root)
    runtime_state: dict[str, object] = {"running": False}
    commands = AgentCommands(
        session_manager=session_manager,
        memory_runtime=memory_runtime,
        tool_registry=tool_registry,
        llm_provider=llm_provider,
        main_llm_provider=main_llm_provider,
        fast_llm_provider=fast_llm_provider,
        proactive_runtime=proactive_runtime,
        drift_runtime=drift_runtime,
        runtime_state=runtime_state,
    )
    agent_loop = AgentLoop(
        config=config,
        message_bus=message_bus,
        event_bus=event_bus,
        session_manager=session_manager,
        memory_runtime=memory_runtime,
        tool_registry=tool_registry,
        plugin_manager=plugin_manager,
        llm_provider=llm_provider,
        commands=commands,
        on_user_activity=drift_runtime.touch_user_activity,
    )
    discord_channel = create_discord_channel(config, message_bus)
    return AppRuntime(
        config=config,
        workspace=workspace_path,
        project_root=project_root,
        llm_provider=llm_provider,
        main_llm_provider=main_llm_provider,
        fast_llm_provider=fast_llm_provider,
        message_bus=message_bus,
        event_bus=event_bus,
        session_manager=session_manager,
        memory_runtime=memory_runtime,
        tool_registry=tool_registry,
        plugin_manager=plugin_manager,
        discord_channel=discord_channel,
        agent_loop=agent_loop,
        proactive_runtime=proactive_runtime,
        drift_runtime=drift_runtime,
        runtime_state=runtime_state,
    )
