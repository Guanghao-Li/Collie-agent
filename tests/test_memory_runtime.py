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
    assert await runtime.search("concise communication") == []
    assert runtime.store.read_index() == []

    optimized = await runtime.optimize_pending()
    results = await runtime.search("concise communication")
    active_id = optimized.affected_ids[0]
    assert results[0].id == active_id
    assert "简洁回答" in await runtime.read_core_memory()

    await runtime.delete_memory(active_id, "测试清理")
    assert await runtime.search("concise") == []
    deleted = runtime.store.deleted_jsonl.read_text(encoding="utf-8")
    assert "测试清理" in deleted


@pytest.mark.asyncio
async def test_memory_runtime_writes_index(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()

    await runtime.add_memory(MemoryItem(type="preference", text="喜欢详细说明"))
    assert json.loads(runtime.store.index_json.read_text(encoding="utf-8")) == []

    await runtime.optimize_pending()

    data = json.loads(runtime.store.index_json.read_text(encoding="utf-8"))
    assert data[0]["type"] == "preference"
