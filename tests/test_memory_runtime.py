from __future__ import annotations

import json

import pytest

from bootstrap.config import MemoryConfig
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


@pytest.mark.asyncio
async def test_memory_runtime_add_search_and_soft_delete(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    item = MemoryItem(
        type="preference",
        text="用户喜欢简洁回答",
        tags=["communication"],
        importance=0.9,
        confidence=0.9,
        source="test",
    )

    await runtime.add_memory(item)
    results = await runtime.search("concise communication")
    assert results[0].id == item.id
    assert "简洁回答" in await runtime.read_core_memory()

    await runtime.delete_memory(item.id, "测试清理")
    assert await runtime.search("concise") == []
    deleted = runtime.store.deleted_jsonl.read_text(encoding="utf-8")
    assert "测试清理" in deleted


@pytest.mark.asyncio
async def test_memory_runtime_writes_index(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()

    await runtime.add_memory(MemoryItem(type="goal", text="完成 Collie-agent 项目"))

    data = json.loads(runtime.store.index_json.read_text(encoding="utf-8"))
    assert data[0]["type"] == "goal"
