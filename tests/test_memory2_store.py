from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

import memory.memory2_store as memory2_store_module
from memory.memory2_store import SQLiteMemory2Store
from memory.models import MemoryItem


def _item(
    memory_id: str,
    text: str,
    *,
    kind: str = "preference",
    importance: float = 0.5,
    status: str = "active",
) -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        type=kind,  # type: ignore[arg-type]
        text=text,
        tags=["test"],
        importance=importance,
        confidence=0.8,
        source="test",
        source_ref=f"turn:{memory_id}",
        metadata={"title": f"title {memory_id}"},
        status=status,  # type: ignore[arg-type]
    )


class FakeSqliteVec:
    def load(self, conn: sqlite3.Connection) -> None:  # noqa: ARG002
        return None

    def serialize_float32(self, values: list[float]) -> bytes:
        return json.dumps(values).encode("utf-8")


@pytest.mark.asyncio
async def test_memory2_initialize_creates_db_tables_and_indexes(tmp_path) -> None:
    store = SQLiteMemory2Store(tmp_path / "memory2.db")

    await store.initialize()

    assert store.db_path.exists()
    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert {
        "memory_items",
        "memory_replacements",
        "consolidation_events",
        "memory_access_events",
    } <= tables
    assert {
        "idx_memory_status",
        "idx_memory_type",
        "idx_memory_source_ref",
        "idx_memory_happened_at",
        "idx_memory_content_hash",
    } <= indexes


@pytest.mark.asyncio
async def test_memory2_upsert_get_list_update_and_soft_delete(tmp_path) -> None:
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    await store.initialize()

    await store.upsert_item(_item("m1", "User likes detailed examples"))
    row = await store.get_item("m1")
    assert row is not None
    assert row["summary"] == "User likes detailed examples"
    assert row["extra"]["tags"] == ["test"]

    active = await store.list_items()
    assert [item["id"] for item in active] == ["m1"]

    assert await store.update_item("m1", {"summary": "Updated summary"}) is True
    assert (await store.get_item("m1"))["summary"] == "Updated summary"  # type: ignore[index]

    assert await store.delete_item("m1") is True
    assert await store.list_items() == []
    deleted = await store.list_items(status="deleted")
    assert deleted[0]["id"] == "m1"


@pytest.mark.asyncio
async def test_memory2_batch_delete_reports_missing_ids(tmp_path) -> None:
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    await store.initialize()
    await store.upsert_item(_item("m1", "one"))

    result = await store.batch_delete(["m1", "missing"])

    assert result == {"deleted": ["m1"], "missing": ["missing"]}


@pytest.mark.asyncio
async def test_memory2_keyword_search_returns_scored_results(tmp_path) -> None:
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    await store.initialize()
    await store.upsert_item(_item("m1", "User likes Python automation", importance=0.5))
    await store.upsert_item(_item("m2", "User likes gardening", importance=0.9))

    results = await store.keyword_search("Python", limit=5)

    assert [item["id"] for item in results] == ["m1"]
    assert results[0]["score"] > 0


@pytest.mark.asyncio
async def test_memory2_vector_search_uses_numpy_cosine_ranking(tmp_path) -> None:
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    await store.initialize()
    await store.upsert_item(_item("near", "near vector"), embedding=[1.0, 0.0])
    await store.upsert_item(_item("far", "far vector"), embedding=[0.0, 1.0])
    await store.upsert_item(_item("empty", "no vector"))

    results = await store.vector_search([1.0, 0.0], limit=5)

    assert [item["id"] for item in results] == ["near", "far"]
    assert results[0]["score"] > results[1]["score"]


@pytest.mark.asyncio
async def test_memory2_sqlite_vec_unavailable_keeps_numpy_fallback(tmp_path, monkeypatch) -> None:
    def missing_sqlite_vec() -> Any:
        raise ImportError("sqlite-vec missing")

    monkeypatch.setattr(memory2_store_module, "_import_sqlite_vec", missing_sqlite_vec)
    store = SQLiteMemory2Store(tmp_path / "memory2.db")

    await store.initialize()
    await store.upsert_item(_item("near", "near vector"), embedding=[1.0, 0.0])
    await store.upsert_item(_item("far", "far vector"), embedding=[0.0, 1.0])
    description = await store.describe()
    results = await store.vector_search([1.0, 0.0], limit=5)

    assert description["sqlite_vec_available"] is False
    assert description["vector_mode"] == "numpy_fallback"
    assert "sqlite-vec unavailable" in description["fallback_reason"]
    assert [item["id"] for item in results] == ["near", "far"]


@pytest.mark.asyncio
async def test_memory2_sqlite_vec_create_failure_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        memory2_store_module,
        "_import_sqlite_vec",
        lambda: FakeSqliteVec(),
    )
    store = SQLiteMemory2Store(tmp_path / "memory2.db", embedding_dimension=2)

    await store.initialize()
    await store.upsert_item(_item("near", "near vector"), embedding=[1.0, 0.0])
    description = await store.describe()
    results = await store.vector_search([1.0, 0.0], limit=5)

    assert description["sqlite_vec_available"] is True
    assert description["sqlite_vec_enabled"] is False
    assert description["vector_mode"] == "numpy_fallback"
    assert "sqlite-vec" in description["fallback_reason"]
    assert results[0]["id"] == "near"


@pytest.mark.asyncio
async def test_memory2_sqlite_vec_enabled_upsert_search_and_delete(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        memory2_store_module,
        "_import_sqlite_vec",
        lambda: FakeSqliteVec(),
    )
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    vector_upserts: list[tuple[str, list[float]]] = []
    vector_deletes: list[str] = []
    sqlite_search_calls: list[list[float]] = []
    monkeypatch.setattr(store, "_create_sqlite_vec_table", lambda conn, dimension: None)
    monkeypatch.setattr(
        store,
        "_upsert_vector_row",
        lambda conn, memory_id, vector: vector_upserts.append((memory_id, vector.tolist())),
    )
    monkeypatch.setattr(
        store,
        "_delete_vector_row",
        lambda conn, memory_id: vector_deletes.append(memory_id),
    )

    def fake_sqlite_vec_search(query_vector, *, kinds=None, limit=12):
        sqlite_search_calls.append(query_vector.tolist())
        rows = [
            {
                "id": "near",
                "type": "preference",
                "summary": "near vector",
                "status": "active",
                "score": memory2_store_module._distance_to_score(0.1),
            },
            {
                "id": "far",
                "type": "preference",
                "summary": "far vector",
                "status": "active",
                "score": memory2_store_module._distance_to_score(2.0),
            },
        ]
        return rows[:limit]

    monkeypatch.setattr(store, "_sqlite_vec_search", fake_sqlite_vec_search)

    await store.initialize()
    await store.upsert_item(_item("near", "near vector"), embedding=[1.0, 0.0])
    await store.upsert_item(_item("far", "far vector"), embedding=[0.0, 1.0])
    results = await store.vector_search([1.0, 0.0], limit=5)
    await store.delete_item("near")
    description = await store.describe()

    assert store._vec_enabled is True
    assert description["vector_mode"] == "sqlite_vec"
    assert vector_upserts == [("near", [1.0, 0.0]), ("far", [0.0, 1.0])]
    assert sqlite_search_calls == [[1.0, 0.0]]
    assert [item["id"] for item in results] == ["near", "far"]
    assert results[0]["score"] > results[1]["score"]
    assert vector_deletes == ["near"]


@pytest.mark.asyncio
async def test_memory2_rebuild_vector_index_rebuilds_active_embeddings_only(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        memory2_store_module,
        "_import_sqlite_vec",
        lambda: FakeSqliteVec(),
    )
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    rebuilt: list[str] = []
    cleared: list[bool] = []
    monkeypatch.setattr(store, "_create_sqlite_vec_table", lambda conn, dimension: None)
    monkeypatch.setattr(store, "_try_clear_vector_table", lambda conn: cleared.append(True))
    monkeypatch.setattr(
        store,
        "_upsert_vector_row",
        lambda conn, memory_id, vector: rebuilt.append(memory_id),
    )

    await store.initialize()
    await store.upsert_item(_item("active", "active vector"), embedding=[1.0, 0.0])
    await store.upsert_item(_item("deleted", "deleted vector", status="deleted"), embedding=[0.0, 1.0])
    await store.upsert_item(
        _item("superseded", "superseded vector", status="superseded"),
        embedding=[0.5, 0.5],
    )
    await store.upsert_item(_item("bad", "bad vector"))
    await store.update_item("bad", {"embedding_json": "not-json"})
    rebuilt.clear()

    result = await store.rebuild_vector_index()

    assert cleared == [True]
    assert rebuilt == ["active"]
    assert result["rebuilt"] == 1
    assert result["skipped"] == 1
    assert any("bad: invalid embedding_json" in error for error in result["errors"])


@pytest.mark.asyncio
async def test_memory2_embedding_dimension_is_inferred_and_mismatch_is_nonfatal(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        memory2_store_module,
        "_import_sqlite_vec",
        lambda: FakeSqliteVec(),
    )
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    vector_upserts: list[str] = []
    monkeypatch.setattr(store, "_create_sqlite_vec_table", lambda conn, dimension: None)
    monkeypatch.setattr(
        store,
        "_upsert_vector_row",
        lambda conn, memory_id, vector: vector_upserts.append(memory_id),
    )

    await store.initialize()
    await store.upsert_item(_item("first", "first vector"), embedding=[1.0, 0.0])
    await store.upsert_item(_item("second", "second vector"), embedding=[0.0, 1.0])
    await store.upsert_item(_item("mismatch", "mismatch vector"), embedding=[1.0, 0.0, 0.0])
    description = await store.describe()
    mismatch_row = await store.get_item("mismatch")

    assert description["embedding_dimension"] == 2
    assert vector_upserts == ["first", "second"]
    assert mismatch_row is not None
    assert mismatch_row["embedding"] == [1.0, 0.0, 0.0]
    assert "embedding dimension mismatch" in description["last_vector_error"]


@pytest.mark.asyncio
async def test_memory2_reinforce_and_record_replacement(tmp_path) -> None:
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    await store.initialize()
    await store.upsert_item(_item("m1", "one"))

    assert await store.reinforce_item("m1", amount=3) is True
    row = await store.get_item("m1")
    assert row is not None
    assert row["reinforcement"] == 3

    await store.record_replacement("old", "m1", "merged")
    with sqlite3.connect(store.db_path) as conn:
        replacement = conn.execute(
            "SELECT old_id, new_id, reason FROM memory_replacements"
        ).fetchone()
    assert replacement == ("old", "m1", "merged")


@pytest.mark.asyncio
async def test_memory2_stage3b_helpers_merge_json_and_record_access(tmp_path) -> None:
    store = SQLiteMemory2Store(tmp_path / "memory2.db")
    await store.initialize()
    await store.upsert_item(
        _item(
            "m1",
            "User likes Python automation",
            kind="procedure",
        )
    )
    row = await store.get_item("m1")
    assert row is not None

    found = await store.find_by_content_hash(row["content_hash"])
    by_type = await store.find_active_by_type("procedure")
    await store.update_item("m1", {"extra_json": {"source_refs": ["turn:new"]}})
    updated = await store.get_item("m1")
    await store.record_replacement("old", "m1", "supersede")
    replacements = await store.list_replacements("m1")
    await store.record_access("m1", "Python", 0.9)

    with sqlite3.connect(store.db_path) as conn:
        access_count = conn.execute(
            "SELECT COUNT(*) FROM memory_access_events WHERE memory_id = 'm1'"
        ).fetchone()[0]

    assert found is not None
    assert found["id"] == "m1"
    assert [item["id"] for item in by_type] == ["m1"]
    assert updated is not None
    assert updated["extra"]["title"] == "title m1"
    assert updated["extra"]["source_refs"] == ["turn:new"]
    assert replacements[0]["old_id"] == "old"
    assert access_count == 1
