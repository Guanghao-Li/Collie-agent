from __future__ import annotations

from typing import Any

import pytest

from memory.embedder import DisabledEmbedder
from memory.memory2_store import SQLiteMemory2Store
from memory.vector_store import (
    DisabledVectorMemoryStore,
    SQLiteVectorMemoryStore,
    VectorMemoryRecord,
)


class SimpleEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self._embed(text) for text in texts]

    async def describe(self) -> dict[str, Any]:
        return {"enabled": True, "backend": "simple-test"}

    def _embed(self, text: str) -> list[float]:
        lowered = text.lower()
        if "alpha" in lowered:
            return [1.0, 0.0]
        if "beta" in lowered:
            return [0.0, 1.0]
        return [0.5, 0.5]


@pytest.mark.asyncio
async def test_sqlite_vector_store_upsert_search_delete_and_describe(tmp_path) -> None:
    memory2 = SQLiteMemory2Store(tmp_path / "memory2.db")
    embedder = SimpleEmbedder()
    store = SQLiteVectorMemoryStore(memory2_store=memory2, embedder=embedder)
    await store.initialize()

    await store.upsert(VectorMemoryRecord(item_id="alpha", memory_type="preference", summary="alpha memory"))
    await store.upsert(VectorMemoryRecord(item_id="beta", memory_type="preference", summary="beta memory"))

    matches = await store.search("alpha query", top_k=5, score_threshold=0.0)
    description = await store.describe()

    assert embedder.calls[0] == ["alpha memory"]
    assert [match.record.item_id for match in matches] == ["alpha", "beta"]
    assert matches[0].score > matches[1].score
    assert description["enabled"] is True
    assert description["memory2"]["fallback_mode"] == "numpy-cosine"

    await store.delete("alpha")
    matches_after_delete = await store.search("alpha query", top_k=5, score_threshold=0.0)

    assert [match.record.item_id for match in matches_after_delete] == ["beta"]


@pytest.mark.asyncio
async def test_sqlite_vector_store_uses_provided_embedding_without_embedder_call(tmp_path) -> None:
    memory2 = SQLiteMemory2Store(tmp_path / "memory2.db")
    embedder = SimpleEmbedder()
    store = SQLiteVectorMemoryStore(memory2_store=memory2, embedder=embedder)
    await store.initialize()

    await store.upsert(
        VectorMemoryRecord(
            item_id="manual",
            memory_type="preference",
            summary="manual embedding",
            embedding=[1.0, 0.0],
        )
    )

    assert embedder.calls == []
    assert (await memory2.get_item("manual"))["embedding"] == [1.0, 0.0]  # type: ignore[index]


@pytest.mark.asyncio
async def test_disabled_vector_store_reports_reason() -> None:
    store = DisabledVectorMemoryStore(requested=True, reason="embedding config missing")

    description = await store.describe()

    assert store.is_enabled() is False
    assert await store.search("alpha", top_k=5, score_threshold=0.0) == []
    assert description["requested"] is True
    assert description["reason"] == "embedding config missing"


@pytest.mark.asyncio
async def test_sqlite_vector_store_with_disabled_embedder_is_not_enabled(tmp_path) -> None:
    store = SQLiteVectorMemoryStore(
        memory2_store=SQLiteMemory2Store(tmp_path / "memory2.db"),
        embedder=DisabledEmbedder(reason="no key", requested=True),
    )
    await store.initialize()

    description = await store.describe()

    assert store.is_enabled() is False
    assert description["enabled"] is False
    assert description["embedder"]["reason"] == "no key"
