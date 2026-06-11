from __future__ import annotations

import pytest

from bootstrap.app import build_app_runtime
from bootstrap.config import Settings
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
