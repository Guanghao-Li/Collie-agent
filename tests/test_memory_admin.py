from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from bootstrap.config import MemoryConfig
from memory.admin import MemoryAdminService
from memory.default_engine import DefaultMemoryEngine
from memory.engine import MemoryMutation
from memory.markdown_store import MarkdownMemoryStore
from memory.memory2_store import SQLiteMemory2Store
from memory.models import MemoryItem
from memory.search import MemorySearch
from memory.store import MemoryStore
from memory.vector_store import VectorMemoryMatch, VectorMemoryRecord


class DeterministicFakeEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def describe(self) -> dict[str, Any]:
        return {"enabled": True, "backend": "deterministic-fake"}

    def _embed(self, text: str) -> list[float]:
        lowered = text.lower()
        if "beta" in lowered:
            return [0.0, 1.0]
        if "alpha" in lowered:
            return [1.0, 0.0]
        return [0.5, 0.5]


class SpyVectorStore:
    def __init__(self) -> None:
        self.upserts: list[VectorMemoryRecord] = []
        self.deleted: list[str] = []

    async def initialize(self) -> None:
        return None

    def is_enabled(self) -> bool:
        return False

    async def upsert(self, record: VectorMemoryRecord) -> None:
        self.upserts.append(record)

    async def delete(self, item_id: str) -> None:
        self.deleted.append(item_id)

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        score_threshold: float,
    ) -> list[VectorMemoryMatch]:
        return []

    async def describe(self) -> dict[str, Any]:
        return {"enabled": False, "backend": "spy"}


def _item(
    memory_id: str,
    text: str,
    *,
    kind: str = "preference",
    status: str = "active",
    happened_at: datetime | None = None,
) -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        type=kind,  # type: ignore[arg-type]
        text=text,
        tags=[kind],
        importance=0.6,
        confidence=0.7,
        source="test",
        source_ref=f"turn:{memory_id}",
        happened_at=happened_at,
        status=status,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_admin_list_and_detail_use_memory2_without_embedding_leak(tmp_path) -> None:
    engine = DefaultMemoryEngine(tmp_path / "memory", config=MemoryConfig())
    await engine.initialize()
    active = _item("active", "Active preference", kind="preference")
    deleted = _item("deleted", "Deleted fact", kind="fact", status="deleted")
    superseded = _item("superseded", "Old preference", kind="preference", status="superseded")
    engine.store.write_index([active, deleted, superseded])
    await engine.memory2_store.upsert_item(active, embedding=[1.0, 0.0])
    await engine.memory2_store.upsert_item(deleted)
    await engine.memory2_store.upsert_item(superseded)

    listed = await engine.admin_service.list_dashboard(status="active", kind="preference")
    detail = await engine.admin_service.get_dashboard_detail("active")

    assert listed["backend"] == "memory2"
    assert [item["id"] for item in listed["items"]] == ["active"]
    assert detail is not None
    assert detail["embedding_present"] is True
    assert "embedding" not in detail
    assert "embedding_json" not in detail["metadata"]


@pytest.mark.asyncio
async def test_admin_update_syncs_index_memory2_vector_and_markdown(tmp_path) -> None:
    config = MemoryConfig(enable_vector_memory=True, vector_score_threshold=0.0)
    engine = DefaultMemoryEngine(
        tmp_path / "memory",
        config=config,
        embedder=DeterministicFakeEmbedder(),
    )
    await engine.initialize()
    item = _item("alpha", "alpha old preference")
    engine.store.write_index([item])
    await engine.mutate(MemoryMutation(kind="sync"))

    updated = await engine.admin_service.update_dashboard_memory(
        "alpha",
        {
            "summary": "beta updated preference",
            "importance": 0.9,
            "confidence": 0.95,
            "tags": ["updated"],
            "metadata": {"source_refs": ["turn:new"]},
        },
    )

    index_item = engine.store.read_index()[0]
    row = await engine.memory2_store.get_item("alpha")
    memory_md = engine.markdown_store.memory_md.read_text(encoding="utf-8")
    assert updated["summary"] == "beta updated preference"
    assert index_item.text == "beta updated preference"
    assert index_item.importance == 0.9
    assert index_item.tags == ["updated"]
    assert row is not None
    assert row["summary"] == "beta updated preference"
    assert row["embedding"] == [0.0, 1.0]
    assert "beta updated preference" in memory_md
    assert "alpha old preference" not in memory_md

    with pytest.raises(ValueError):
        await engine.admin_service.update_dashboard_memory(
            "alpha",
            {"embedding_json": [1.0, 0.0]},
        )


@pytest.mark.asyncio
async def test_admin_batch_delete_soft_deletes_and_renders_once(tmp_path, monkeypatch) -> None:
    spy_vector = SpyVectorStore()
    engine = DefaultMemoryEngine(
        tmp_path / "memory",
        config=MemoryConfig(),
        vector_store=spy_vector,
    )
    await engine.initialize()
    one = _item("one", "First memory")
    two = _item("two", "Second memory")
    engine.store.write_index([one, two])
    await engine.memory2_store.upsert_item(one)
    await engine.memory2_store.upsert_item(two)
    render_count = 0
    original_render = engine.markdown_store.render_active_memories

    def render_once(items):
        nonlocal render_count
        render_count += 1
        original_render(items)

    monkeypatch.setattr(engine.markdown_store, "render_active_memories", render_once)

    result = await engine.admin_service.batch_delete(["one", "missing"], reason="cleanup")

    row = await engine.memory2_store.get_item("one")
    assert result["affected_ids"] == ["one"]
    assert result["missing_ids"] == ["missing"]
    assert row is not None
    assert row["status"] == "deleted"
    assert spy_vector.deleted == ["one"]
    assert render_count == 1


@pytest.mark.asyncio
async def test_admin_find_similar_vector_and_keyword_fallback(tmp_path) -> None:
    config = MemoryConfig(enable_vector_memory=True, vector_score_threshold=0.0)
    engine = DefaultMemoryEngine(
        tmp_path / "memory",
        config=config,
        embedder=DeterministicFakeEmbedder(),
    )
    await engine.initialize()
    engine.store.write_index([
        _item("alpha1", "alpha first memory"),
        _item("alpha2", "alpha second memory"),
    ])
    await engine.mutate(MemoryMutation(kind="sync"))

    vector_result = await engine.admin_service.find_similar(text="alpha query", limit=2)
    keyword_engine = DefaultMemoryEngine(tmp_path / "keyword-memory", config=MemoryConfig())
    await keyword_engine.initialize()
    keyword_engine.store.write_index([_item("k1", "keyword fallback memory")])
    keyword_result = await keyword_engine.admin_service.find_similar(
        memory_id="k1",
        limit=2,
    )

    assert vector_result["backend"] == "vector"
    assert vector_result["items"]
    assert keyword_result["backend"] == "keyword"
    assert keyword_result["query_source"] == "memory_id"
    assert "keyword fallback" in keyword_result["disabled_reason"]


@pytest.mark.asyncio
async def test_admin_list_event_range_memory2_and_history_fallback(tmp_path) -> None:
    engine = DefaultMemoryEngine(tmp_path / "memory", config=MemoryConfig())
    await engine.initialize()
    event_time = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
    event = _item("event", "Alpha event happened", kind="event", happened_at=event_time)
    engine.store.write_index([event])
    await engine.memory2_store.upsert_item(event)

    memory2_events = await engine.admin_service.list_event_range(
        start="2026-01-01T00:00:00+00:00",
        end="2026-01-03T00:00:00+00:00",
    )

    store = MemoryStore(tmp_path / "fallback")
    markdown = MarkdownMemoryStore(tmp_path / "fallback")
    await store.initialize()
    await markdown.initialize()
    markdown.append_history_entry(
        "Fallback history event",
        happened_at="2026-01-02T12:00:00+00:00",
        source_ref="turn:history",
    )
    fallback_admin = MemoryAdminService(
        store=store,
        markdown_store=markdown,
        memory2_store=None,
        vector_store=None,
        retriever=None,
        config=MemoryConfig(),
    )
    history_events = await fallback_admin.list_event_range(start="2026-01-01", end="2026-01-03")

    assert memory2_events["backend"] == "memory2"
    assert [event["id"] for event in memory2_events["events"]] == ["event"]
    assert history_events["backend"] == "history"
    assert "Fallback history event" in history_events["events"][0]["summary"]


@pytest.mark.asyncio
async def test_admin_stats_counts_statuses_and_pending_review(tmp_path) -> None:
    engine = DefaultMemoryEngine(tmp_path / "memory", config=MemoryConfig())
    await engine.initialize()
    engine.store.write_index([
        _item("active", "Active"),
        _item("deleted", "Deleted", status="deleted"),
        _item("superseded", "Superseded", status="superseded"),
    ])
    engine.markdown_store.append_pending_candidate("preference", "Pending item")
    engine.markdown_store.append_pending_candidate("correction", "Needs review")

    stats = await engine.admin_service.get_stats()

    assert stats["active"] == 1
    assert stats["deleted"] == 1
    assert stats["superseded"] == 1
    assert stats["pending_candidates"] == 1
    assert stats["requires_review"] == 1
    assert "vector_enabled" in stats
    assert "embedding_enabled" in stats
