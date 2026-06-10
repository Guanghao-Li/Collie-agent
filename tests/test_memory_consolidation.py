from __future__ import annotations

import json

import pytest

from bootstrap.config import MemoryConfig
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime


@pytest.mark.asyncio
async def test_consolidation_renders_markdown_outputs_and_keeps_legacy_index(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.update_recent_context("Working on the memory markdown refactor.")
    await runtime.append_pending_memory(
        MemoryItem(type="project", text="Refactor Collie-agent memory outputs", tags=["memory"])
    )
    await runtime.append_pending_memory(
        MemoryItem(type="preference", text="User prefers concise answers", tags=["style"])
    )
    await runtime.append_pending_memory(
        MemoryItem(type="fact", text="User uses Python", tags=["dev"])
    )

    result = await runtime.consolidate()

    assert result.added == 3
    assert (await runtime.stats())["active"] == 3

    memory_md = runtime.engine.markdown_store.memory_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    self_md = runtime.engine.markdown_store.self_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    history_md = runtime.engine.markdown_store.history_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    recent_context_md = runtime.engine.markdown_store.recent_context_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    pending_md = runtime.engine.markdown_store.pending_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    profile_md = runtime.store.profile_md.read_text(encoding="utf-8")
    index_data = json.loads(runtime.store.index_json.read_text(encoding="utf-8"))

    assert "Refactor Collie-agent memory outputs" in memory_md
    assert "User prefers concise answers" in memory_md
    assert "User uses Python" in memory_md

    assert "Refactor Collie-agent memory outputs" in self_md
    assert "User prefers concise answers" in self_md
    assert "User uses Python" not in self_md
    assert profile_md == self_md

    assert "# Recent Context" in recent_context_md
    assert "## Compression" in recent_context_md
    assert "## Ongoing Threads" in recent_context_md
    assert "## Recent Turns" in recent_context_md
    assert "Working on the memory markdown refactor." in recent_context_md

    assert "# Pending" in pending_md
    assert "- None" in pending_md

    assert "# History" in history_md
    assert "Memory Consolidation" in history_md
    assert "Refactor Collie-agent memory outputs" in history_md
    assert "User prefers concise answers" in history_md

    assert len(index_data) == 3
    assert {item["text"] for item in index_data} == {
        "Refactor Collie-agent memory outputs",
        "User prefers concise answers",
        "User uses Python",
    }


@pytest.mark.asyncio
async def test_history_is_appended_instead_of_rewritten_on_consolidate(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()

    await runtime.append_pending_memory(MemoryItem(type="project", text="First history item"))
    await runtime.consolidate()
    first_history = runtime.engine.markdown_store.history_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]

    await runtime.append_pending_memory(MemoryItem(type="project", text="Second history item"))
    await runtime.consolidate()
    second_history = runtime.engine.markdown_store.history_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]

    assert first_history in second_history
    assert "First history item" in second_history
    assert "Second history item" in second_history
    assert second_history.count("### Memory Consolidation") == 2


@pytest.mark.asyncio
async def test_duplicate_memory_is_merged(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.append_pending_memory(MemoryItem(type="fact", text="User uses Python"))
    await runtime.consolidate()
    await runtime.append_pending_memory(MemoryItem(type="fact", text="User uses Python"))

    result = await runtime.consolidate()

    assert result.merged == 1
    assert (await runtime.stats())["active"] == 1


@pytest.mark.asyncio
async def test_conflict_is_logged(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.add_memory(
        MemoryItem(type="preference", text="User likes detailed answers", tags=["style"])
    )
    await runtime.append_pending_memory(
        MemoryItem(type="preference", text="User does not like detailed answers", tags=["style"])
    )

    result = await runtime.consolidate()

    assert result.conflicts == 1
    assert "conflict" in runtime.store.consolidation_log_md.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_build_memory_context_does_not_include_history_text(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    await runtime.add_memory(MemoryItem(type="project", text="Memory refactor project"))
    await runtime.update_recent_context("Compression summary for the active workstream.")
    await runtime.append_reflection("history-only-marker")

    context = await runtime.build_memory_context("memory refactor", [])

    assert "Compression summary for the active workstream." in context
    assert "Memory refactor project" in context
    assert "history-only-marker" not in context
