from __future__ import annotations

import pytest

from bootstrap.app import build_app_runtime
from bootstrap.config import Settings
from bus.event_bus import PromptRenderEvent
from bus.models import InboundMessage
from memory.models import MemoryItem
from tools.registry import ToolError


@pytest.mark.asyncio
async def test_plugin_manager_loads_builtin_plugins(tmp_path) -> None:
    config = Settings()
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    await runtime.session_manager.initialize()

    await runtime.plugin_manager.load_plugins()

    tool_names = {tool.name for tool in runtime.tool_registry.list_tools()}
    assert {
        "remember",
        "search_memory",
        "summarize_memory",
        "optimize_memory",
        "recall_memory",
        "memorize",
        "forget_memory",
        "list_memory",
        "get_memory",
        "update_memory",
        "delete_memory",
        "find_similar_memory",
        "list_memory_events",
        "trigger_proactive_check",
        "trigger_drift_cycle",
    } <= tool_names
    assert "manual" in runtime.proactive_runtime.source_registry.sources
    assert "memory_consolidation" in runtime.drift_runtime.task_registry.tasks
    assert runtime.plugin_manager.context.llm_provider is runtime.llm_provider
    assert runtime.plugin_manager.context.main_llm_provider is runtime.llm_provider
    assert runtime.plugin_manager.context.fast_llm_provider is runtime.fast_llm_provider
    assert runtime.plugin_manager.context.phase_runner is runtime.phase_runner
    assert runtime.plugin_manager.context.tool_executor is runtime.tool_executor
    assert runtime.agent_loop.phase_runner is runtime.phase_runner
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_plugin_can_register_prompt_render_phase_module(tmp_path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_dir = plugin_root / "example_prompt_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        '''
class ExamplePromptPlugin:
    name = "example_prompt_plugin"

    async def setup(self, context):
        context.phase_runner.register(ExamplePromptModule())


class ExamplePromptModule:
    name = "example.prompt_inject"
    phase = "prompt_render"
    priority = 20

    async def run(self, frame):
        if "搜索" in frame.content or "查一下" in frame.content:
            frame.slots["prompt:section_bottom:example_search_hint"] = (
                "当用户请求查询资料时，优先考虑使用可用搜索工具。"
            )


plugin = ExamplePromptPlugin()
'''.strip(),
        encoding="utf-8",
    )
    config = Settings()
    config.plugins.paths = [str(plugin_root)]
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    await runtime.session_manager.initialize()
    rendered: list[list[dict[str, str]]] = []
    runtime.event_bus.subscribe(PromptRenderEvent, lambda event: rendered.append(event.messages))

    await runtime.plugin_manager.load_plugins()
    await runtime.agent_loop.process_message(
        InboundMessage(channel="discord", session_id="c1", user_id="u1", content="查一下今天的资料")
    )

    prompt_modules = runtime.phase_runner.modules_for("prompt_render")
    assert any(getattr(module, "name", "") == "example.prompt_inject" for module in prompt_modules)
    assert any(
        message["role"] == "system" and "优先考虑使用可用搜索工具" in message["content"]
        for message in rendered[0]
    )
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_plugin_phase_registration_failure_is_recorded(tmp_path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_dir = plugin_root / "bad_phase_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        '''
class BadPhasePlugin:
    name = "bad_phase_plugin"

    async def setup(self, context):
        context.phase_runner.register(BadPhaseModule())


class BadPhaseModule:
    name = "bad.phase"
    phase = "not_a_phase"
    priority = 1

    async def run(self, frame):
        return None


plugin = BadPhasePlugin()
'''.strip(),
        encoding="utf-8",
    )
    config = Settings()
    config.plugins.paths = [str(plugin_root)]
    runtime = build_app_runtime(config, tmp_path)

    await runtime.plugin_manager.load_plugins()

    assert len(runtime.plugin_manager.errors) == 1
    assert "bad_phase_plugin" in runtime.plugin_manager.errors[0]
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_plugin_can_register_tool_pre_hook(tmp_path) -> None:
    plugin_root = tmp_path / "plugins"
    plugin_dir = plugin_root / "tool_hook_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.py").write_text(
        '''
from tools.hooks import ToolHookResult


class ToolHookPlugin:
    name = "tool_hook_plugin"

    async def setup(self, context):
        context.tool_executor.register_pre_hook(CalculatorRewriteHook())


class CalculatorRewriteHook:
    name = "test.calculator_rewrite"
    priority = 10
    tool_name = "calculator"

    async def before_tool_call(self, tool_name, arguments, frame):
        return ToolHookResult(
            decision="modify",
            arguments={"expression": "4 + 5"},
        )


plugin = ToolHookPlugin()
'''.strip(),
        encoding="utf-8",
    )
    config = Settings()
    config.plugins.paths = [str(plugin_root)]
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    await runtime.session_manager.initialize()

    await runtime.plugin_manager.load_plugins()
    outbound = await runtime.agent_loop.process_message(
        InboundMessage(
            channel="discord",
            session_id="c1",
            user_id="u1",
            content="TOOL:calculator 1 + 1",
        )
    )

    assert any(
        getattr(hook, "name", "") == "test.calculator_rewrite"
        for hook in runtime.tool_executor.list_pre_hooks()
    )
    assert outbound.content == "工具结果：9"
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_memory_plugin_tools_use_optimizer_lifecycle(tmp_path) -> None:
    config = Settings()
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    await runtime.session_manager.initialize()
    await runtime.plugin_manager.load_plugins()

    pending_id = await runtime.tool_registry.call_tool(
        "remember",
        {"text": "User prefers examples in code explanations", "stable": True},
    )
    assert pending_id
    assert runtime.memory_runtime.store.read_index() == []

    dry_run = await runtime.tool_registry.call_tool("optimize_memory", {"dry_run": True})
    assert dry_run["ok"] is True
    assert dry_run["added"] == 1
    assert runtime.memory_runtime.store.read_index() == []

    optimized = await runtime.tool_registry.call_tool("optimize_memory", {})
    assert optimized["ok"] is True
    assert optimized["added"] == 1

    recalled = await runtime.tool_registry.call_tool(
        "recall_memory",
        {"query": "examples code explanations", "intent": "interest", "limit": 3},
    )
    assert recalled["items"]
    active_id = recalled["items"][0]["id"]

    forgotten = await runtime.tool_registry.call_tool(
        "forget_memory",
        {"ids": [active_id], "reason": "test cleanup"},
    )
    assert forgotten["ok"] is True
    assert forgotten["affected_ids"] == [active_id]
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_memory_plugin_admin_tools(tmp_path) -> None:
    config = Settings()
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    await runtime.session_manager.initialize()
    await runtime.plugin_manager.load_plugins()

    memorized = await runtime.tool_registry.call_tool(
        "memorize",
        {
            "summary": "User prefers admin tool examples",
            "memory_kind": "preference",
            "source_ref": "turn:memorize",
        },
    )
    assert memorized["status"] == "pending"
    assert runtime.memory_runtime.store.read_index() == []

    optimized = await runtime.tool_registry.call_tool("optimize_memory", {})
    assert optimized["added"] == 1
    listed = await runtime.tool_registry.call_tool(
        "list_memory",
        {"query": "admin tool", "limit": 5},
    )
    active_id = listed["items"][0]["id"]

    detail = await runtime.tool_registry.call_tool("get_memory", {"id": active_id})
    updated = await runtime.tool_registry.call_tool(
        "update_memory",
        {
            "id": active_id,
            "fields": {
                "summary": "User prefers updated admin examples",
                "tags": ["admin"],
            },
        },
    )
    similar = await runtime.tool_registry.call_tool(
        "find_similar_memory",
        {"text": "updated admin examples", "limit": 3},
    )
    event = MemoryItem(
        id="event-tool",
        type="event",
        text="Admin tool event happened",
        source_ref="turn:event",
    )
    runtime.memory_runtime.store.write_index([
        *runtime.memory_runtime.store.read_index(),
        event,
    ])
    await runtime.memory_runtime.engine.memory2_store.upsert_item(event)  # type: ignore[attr-defined]
    events = await runtime.tool_registry.call_tool("list_memory_events", {"limit": 5})
    deleted = await runtime.tool_registry.call_tool(
        "delete_memory",
        {"id": active_id, "reason": "test cleanup"},
    )

    assert detail["id"] == active_id
    assert updated["summary"] == "User prefers updated admin examples"
    assert similar["items"]
    assert events["events"]
    assert deleted["affected_ids"] == [active_id]

    with pytest.raises(ToolError):
        await runtime.tool_registry.call_tool("find_similar_memory", {})

    await runtime.llm_provider.close()
