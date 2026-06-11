from __future__ import annotations

import pytest

from bootstrap.app import build_app_runtime
from bootstrap.config import Settings


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
        "forget_memory",
        "trigger_proactive_check",
        "trigger_drift_cycle",
    } <= tool_names
    assert "manual" in runtime.proactive_runtime.source_registry.sources
    assert "memory_consolidation" in runtime.drift_runtime.task_registry.tasks
    assert runtime.plugin_manager.context.llm_provider is runtime.llm_provider
    assert runtime.plugin_manager.context.main_llm_provider is runtime.llm_provider
    assert runtime.plugin_manager.context.fast_llm_provider is runtime.fast_llm_provider
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
