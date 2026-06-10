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
    assert {"remember", "search_memory", "trigger_proactive_check", "trigger_drift_cycle"} <= tool_names
    assert "manual" in runtime.proactive_runtime.source_registry.sources
    assert "memory_consolidation" in runtime.drift_runtime.task_registry.tasks
    assert runtime.plugin_manager.context.llm_provider is runtime.llm_provider
    assert runtime.plugin_manager.context.main_llm_provider is runtime.llm_provider
    assert runtime.plugin_manager.context.fast_llm_provider is runtime.fast_llm_provider
    await runtime.llm_provider.close()
