from __future__ import annotations

from typing import Any

import pytest

from bootstrap.config import MemoryConfig
from memory.default_engine import DefaultMemoryEngine
from memory.engine import MemoryMutation, MemoryQuery
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime
from memory.vector_store import SQLiteVectorMemoryStore


class SimpleEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def describe(self) -> dict[str, Any]:
        return {"enabled": True, "backend": "simple-test"}

    def _embed(self, text: str) -> list[float]:
        if "alpha" in text.lower():
            return [1.0, 0.0]
        if "beta" in text.lower():
            return [0.0, 1.0]
        return [0.5, 0.5]


@pytest.mark.asyncio
async def test_default_engine_uses_disabled_vector_store_by_default(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()

    description = await runtime.engine.describe()

    assert runtime.engine.vector_store.is_enabled() is False  # type: ignore[attr-defined]
    assert description["vector_memory"]["enabled"] is False
    assert description["vector_memory"]["reason"] == "disabled by config"
    assert description["memory2"]["path"].endswith(".collie\\memory\\memory2.db") or description[
        "memory2"
    ]["path"].endswith(".collie/memory/memory2.db")
    assert description["embedder"]["enabled"] is False


@pytest.mark.asyncio
async def test_default_engine_enables_sqlite_vector_store_with_fake_embedder(tmp_path) -> None:
    config = MemoryConfig(enable_vector_memory=True, vector_score_threshold=0.0)
    engine = DefaultMemoryEngine(
        tmp_path / "memory",
        config=config,
        embedder=SimpleEmbedder(),
    )
    await engine.initialize()
    item = MemoryItem(
        id="alpha",
        type="preference",
        text="alpha memory",
        source="test",
        status="active",
    )
    engine.store.write_index([item])

    await engine.mutate(MemoryMutation(kind="sync"))
    result = await engine.query(MemoryQuery(kind="search", text="alpha query", limit=5))
    description = await engine.describe()

    assert isinstance(engine.vector_store, SQLiteVectorMemoryStore)
    assert engine.vector_store.is_enabled() is True
    assert result.metadata["search_backend"] == "vector"
    assert result.items[0].id == "alpha"
    assert description["vector_memory"]["enabled"] is True
    assert description["vector_memory"]["embedder"]["backend"] == "simple-test"


@pytest.mark.asyncio
async def test_vector_enabled_with_missing_embedding_config_is_disabled(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig(enable_vector_memory=True))
    await runtime.initialize()

    description = await runtime.engine.describe()

    assert runtime.engine.vector_store.is_enabled() is False  # type: ignore[attr-defined]
    assert description["vector_memory"]["requested"] is True
    assert "embedding config missing" in description["vector_memory"]["reason"]


@pytest.mark.asyncio
async def test_optimizer_promotion_writes_memory2_item(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    store = runtime.engine.markdown_store  # type: ignore[attr-defined]
    store.append_pending_candidate(
        "preference",
        "User likes alpha examples.",
        source_ref="turn:alpha",
    )

    result = await runtime.optimize_pending()
    row = await runtime.engine.memory2_store.get_item(result.affected_ids[0])  # type: ignore[attr-defined]

    assert result.added == 1
    assert row is not None
    assert row["summary"] == "User likes alpha examples."
    assert row["status"] == "active"


@pytest.mark.asyncio
async def test_forget_marks_memory2_item_deleted(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig())
    await runtime.initialize()
    runtime.engine.markdown_store.append_pending_candidate(  # type: ignore[attr-defined]
        "preference",
        "User likes beta examples.",
        source_ref="turn:beta",
    )
    optimized = await runtime.optimize_pending()
    active_id = optimized.affected_ids[0]

    result = await runtime.engine.mutate(
        MemoryMutation(kind="forget", ids=(active_id,), reason="test cleanup")
    )
    row = await runtime.engine.memory2_store.get_item(active_id)  # type: ignore[attr-defined]

    assert result.ok is True
    assert row is not None
    assert row["status"] == "deleted"
