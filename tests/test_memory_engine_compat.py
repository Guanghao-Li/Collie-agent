from __future__ import annotations

import pytest

from bootstrap.config import MemoryConfig
from memory.engine import MemoryMutation, MemoryQuery
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


@pytest.mark.asyncio
async def test_memory_runtime_initialize_creates_markdown_memory_files(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()

    markdown_store = runtime.engine.markdown_store  # type: ignore[attr-defined]
    assert markdown_store.memory_md.exists()
    assert markdown_store.self_md.exists()
    assert markdown_store.history_md.exists()
    assert markdown_store.recent_context_md.exists()
    assert markdown_store.pending_md.exists()


@pytest.mark.asyncio
async def test_read_profile_falls_back_to_legacy_profile_md(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()

    runtime.store.write_text(
        runtime.store.profile_md,
        "# Profile\n\n- User prefers concise answers\n",
    )
    runtime.engine.markdown_store.self_md.unlink()  # type: ignore[attr-defined]

    profile = await runtime.read_profile()

    assert "User prefers concise answers" in profile


@pytest.mark.asyncio
async def test_engine_query_context_includes_profile_recent_context_and_memories(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.add_memory(
        MemoryItem(
            type="goal",
            text="Finish the Collie-agent memory refactor",
            tags=["collie", "memory"],
            source="test",
        )
    )
    await runtime.update_recent_context("Working on the memory engine phase-one refactor.")

    result = await runtime.engine.query(MemoryQuery(kind="context", text="Collie-agent"))

    assert "Finish the Collie-agent memory refactor" in result.content
    assert "Working on the memory engine phase-one refactor." in result.content
    assert result.items


@pytest.mark.asyncio
async def test_engine_mutate_remember_and_forget(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    item = MemoryItem(
        type="fact",
        text="User uses Python",
        tags=["dev"],
        source="test",
    )

    remember = await runtime.engine.mutate(
        MemoryMutation(kind="remember", item=item, stable=True)
    )
    assert remember.ok is True
    assert "User uses Python" in await runtime.read_core_memory()

    forget = await runtime.engine.mutate(
        MemoryMutation(kind="forget", memory_id=item.id, reason="test forget")
    )
    assert forget.ok is True
    assert await runtime.search("Python") == []
    assert "test forget" in runtime.store.deleted_jsonl.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_legacy_search_and_stats_methods_remain_compatible(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.add_memory(
        MemoryItem(
            type="preference",
            text="User prefers concise answers",
            tags=["style"],
            source="test",
        )
    )
    await runtime.append_pending_memory(
        MemoryItem(
            type="project",
            text="Refactoring memory runtime",
            tags=["memory"],
            source="test",
        )
    )

    results = await runtime.search("concise answers")
    stats = await runtime.stats()

    assert results
    assert results[0].text == "User prefers concise answers"
    assert stats == {"active": 1, "pending": 1, "deleted": 0}


@pytest.mark.asyncio
async def test_vector_disabled_uses_keyword_search_and_keeps_system_working(tmp_path) -> None:
    config = MemoryConfig()
    config.enable_vector_memory = False
    runtime = MemoryRuntime(tmp_path, config)
    await runtime.initialize()
    await runtime.add_memory(
        MemoryItem(
            type="project",
            text="Refactor the memory engine",
            tags=["memory"],
            source="test",
        )
    )

    result = await runtime.engine.query(MemoryQuery(kind="search", text="memory engine"))
    description = await runtime.engine.describe()

    assert result.items
    assert result.items[0].text == "Refactor the memory engine"
    assert result.metadata["search_backend"] == "keyword"
    assert result.metadata["vector_enabled"] is False
    assert description["vector_memory"]["enabled"] is False
    assert description["vector_memory"]["backend"] == "null"


@pytest.mark.asyncio
async def test_vector_flag_without_backend_falls_back_to_keyword_search(tmp_path) -> None:
    config = MemoryConfig()
    config.enable_vector_memory = True
    runtime = MemoryRuntime(tmp_path, config)
    await runtime.initialize()
    await runtime.add_memory(
        MemoryItem(
            type="fact",
            text="User uses Python for automation",
            tags=["dev"],
            source="test",
        )
    )

    result = await runtime.engine.query(MemoryQuery(kind="search", text="Python automation"))
    description = await runtime.engine.describe()

    assert result.items
    assert result.metadata["search_backend"] == "keyword"
    assert result.metadata["vector_enabled"] is False
    assert description["vector_memory"]["enabled"] is False
    assert description["vector_memory"]["requested"] is True
