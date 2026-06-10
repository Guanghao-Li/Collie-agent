from __future__ import annotations

import pytest

from bootstrap.config import MemoryConfig
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


@pytest.mark.asyncio
async def test_pending_memory_enters_index(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.append_pending_memory(
        MemoryItem(type="project", text="构建 Collie-agent", tags=["project"])
    )

    result = await runtime.consolidate()

    assert result.added == 1
    assert (await runtime.stats())["active"] == 1
    assert "构建 Collie-agent" in await runtime.read_core_memory()


@pytest.mark.asyncio
async def test_duplicate_memory_is_merged(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.append_pending_memory(MemoryItem(type="fact", text="用户使用 Python"))
    await runtime.consolidate()
    await runtime.append_pending_memory(MemoryItem(type="fact", text="用户使用 Python"))

    result = await runtime.consolidate()

    assert result.merged == 1
    assert (await runtime.stats())["active"] == 1


@pytest.mark.asyncio
async def test_conflict_is_logged(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.add_memory(
        MemoryItem(type="preference", text="用户喜欢详细回答", tags=["style"])
    )
    await runtime.append_pending_memory(
        MemoryItem(type="preference", text="用户不喜欢详细回答", tags=["style"])
    )

    result = await runtime.consolidate()

    assert result.conflicts == 1
    assert "可能存在冲突" in runtime.store.consolidation_log_md.read_text(encoding="utf-8")
