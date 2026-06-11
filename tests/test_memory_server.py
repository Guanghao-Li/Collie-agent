from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from bootstrap.config import MemoryConfig
from memory.engine import MemoryMutation, MemoryQueryResult, MemoryRecord
from memory.models import MemoryItem
from memory.runtime import MemoryRuntime
from memory.server import create_memory_app


def _item(memory_id: str, text: str, *, kind: str = "preference") -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        type=kind,  # type: ignore[arg-type]
        text=text,
        tags=[kind],
        importance=0.7,
        confidence=0.8,
        source="test",
        source_ref=f"turn:{memory_id}",
        status="active",
    )


async def _runtime_with_memory(tmp_path, config: MemoryConfig | None = None) -> MemoryRuntime:
    runtime = MemoryRuntime(tmp_path, config or MemoryConfig())
    await runtime.initialize()
    items = [
        _item("pref", "User prefers API examples", kind="preference"),
        _item("event", "API event happened", kind="event"),
    ]
    runtime.store.write_index(items)
    await runtime.engine.mutate(MemoryMutation(kind="sync"))
    return runtime


class _FakeVectorStore:
    def is_enabled(self) -> bool:
        return True


class _FakeAdmin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get_stats(self) -> dict[str, object]:
        self.calls.append(("get_stats", {}))
        return {"active": 1}

    async def list_dashboard(
        self,
        *,
        limit: int,
        offset: int,
        kind: str | None,
        status: str,
        query: str | None,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "list_dashboard",
                {
                    "limit": limit,
                    "offset": offset,
                    "kind": kind or "",
                    "status": status,
                    "query": query or "",
                },
            )
        )
        return {"items": [], "has_more": False, "backend": "fake"}

    async def get_dashboard_detail(self, memory_id: str) -> dict[str, object] | None:
        self.calls.append(("get_dashboard_detail", {"memory_id": memory_id}))
        return {"id": memory_id, "summary": "detail"}

    async def update_dashboard_memory(
        self,
        memory_id: str,
        fields: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append(
            ("update_dashboard_memory", {"memory_id": memory_id, "fields": fields})
        )
        return {"id": memory_id, **fields}

    async def delete_dashboard_memory(
        self,
        memory_id: str,
        reason: str = "",
    ) -> dict[str, object]:
        self.calls.append(
            ("delete_dashboard_memory", {"memory_id": memory_id, "reason": reason})
        )
        return {"ok": True, "affected_ids": [memory_id], "missing_ids": []}

    async def batch_delete(self, ids: list[str], reason: str = "") -> dict[str, object]:
        self.calls.append(("batch_delete", {"ids": ids, "reason": reason}))
        return {"ok": True, "affected_ids": ids[:1], "missing_ids": ids[1:]}

    async def find_similar(
        self,
        memory_id: str | None = None,
        text: str | None = None,
        limit: int = 10,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "find_similar",
                {"memory_id": memory_id or "", "text": text or "", "limit": limit},
            )
        )
        return {"items": [{"id": "similar"}], "backend": "fake"}

    async def list_event_range(
        self,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        self.calls.append(
            ("list_event_range", {"start": start or "", "end": end or "", "limit": limit})
        )
        return {"events": [], "backend": "fake"}


class _FakeEngine:
    def __init__(self) -> None:
        self.admin_service = _FakeAdmin()
        self.vector_store = _FakeVectorStore()
        self.queries = []
        self.mutations = []

    async def query(self, query):
        self.queries.append(query)
        return MemoryQueryResult(
            content="fake memory block",
            records=[
                MemoryRecord(
                    id="record-1",
                    kind="preference",
                    summary="User likes API examples",
                    score=0.9,
                    engine_kind="fake",
                    injected=True,
                )
            ],
            metadata={"intent": query.intent},
        )

    async def mutate(self, mutation):
        self.mutations.append(mutation)
        return {
            "ok": True,
            "accepted": True,
            "item_id": "pending-1",
            "actual_kind": mutation.memory_kind,
            "status": "pending",
        }


class _FakeRuntime:
    def __init__(self) -> None:
        self.config = MemoryConfig()
        self.engine = _FakeEngine()
        self.optimize_calls = 0

    async def optimize_pending(self):
        self.optimize_calls += 1
        return {"ok": True, "added": 1}


@pytest.mark.asyncio
async def test_memory_server_health_and_auth_modes(tmp_path) -> None:
    open_runtime = await _runtime_with_memory(tmp_path / "open")
    open_client = TestClient(create_memory_app(open_runtime, open_runtime.config))

    assert open_client.get("/health").json()["ok"] is True
    assert open_client.get("/memory/stats").status_code == 200

    config = MemoryConfig(memory_server_api_key="secret")
    locked_runtime = await _runtime_with_memory(tmp_path / "locked", config)
    locked_client = TestClient(create_memory_app(locked_runtime, locked_runtime.config))

    assert locked_client.get("/health").status_code == 200
    missing = locked_client.get("/memory/stats")
    wrong = locked_client.get("/memory/stats", headers={"Authorization": "Bearer nope"})
    bearer = locked_client.get("/memory/stats", headers={"Authorization": "Bearer secret"})
    header = locked_client.get("/memory/stats", headers={"X-API-Key": "secret"})

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "forbidden"
    assert bearer.status_code == 200
    assert header.status_code == 200


def test_memory_server_routes_delegate_to_admin_and_runtime() -> None:
    runtime = _FakeRuntime()
    client = TestClient(create_memory_app(runtime, runtime.config))

    health = client.get("/health")
    stats = client.get("/memory/stats")
    listed = client.get(
        "/memory",
        params={
            "kind": "preference",
            "status": "active",
            "query": "api",
            "limit": 7,
            "offset": 2,
        },
    )
    detail = client.get("/memory/mem-1")
    updated = client.patch("/memory/mem-1", json={"summary": "updated"})
    deleted = client.request("DELETE", "/memory/mem-1", json={"reason": "cleanup"})
    batch = client.post(
        "/memory/batch-delete",
        json={"ids": ["mem-2", "mem-3"], "reason": "batch"},
    )
    similar = client.post(
        "/memory/find-similar",
        json={"id": "mem-2", "text": "ignored when id is present", "limit": 4},
    )
    events = client.get(
        "/memory/events",
        params={"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z", "limit": 9},
    )
    recall = client.post(
        "/memory/recall",
        json={
            "query": "api examples",
            "intent": "context",
            "memory_kind": "preference",
            "limit": 3,
        },
    )
    memorize = client.post(
        "/memory/memorize",
        json={
            "summary": "remember this through pending",
            "memory_kind": "preference",
            "importance": 0.9,
            "confidence": 0.8,
            "source_ref": "manual:fake",
        },
    )
    optimize = client.post("/memory/optimize")
    scheduler_state = client.get("/memory/optimizer/state")

    assert health.json()["vector_enabled"] is True
    assert health.json()["admin_enabled"] is True
    assert stats.json()["active"] == 1
    assert listed.json()["backend"] == "fake"
    assert detail.json()["id"] == "mem-1"
    assert updated.json()["summary"] == "updated"
    assert deleted.json()["affected_ids"] == ["mem-1"]
    assert batch.json()["missing_ids"] == ["mem-3"]
    assert similar.json()["items"][0]["id"] == "similar"
    assert events.json()["backend"] == "fake"
    assert recall.json()["text_block"] == "fake memory block"
    assert recall.json()["records"][0]["id"] == "record-1"
    assert memorize.json()["ok"] is True
    assert memorize.json()["status"] == "pending"
    assert optimize.json()["added"] == 1
    assert scheduler_state.json() == {}

    admin_calls = runtime.engine.admin_service.calls
    assert [name for name, _ in admin_calls] == [
        "get_stats",
        "list_dashboard",
        "get_dashboard_detail",
        "update_dashboard_memory",
        "delete_dashboard_memory",
        "batch_delete",
        "find_similar",
        "list_event_range",
    ]
    assert admin_calls[1][1] == {
        "limit": 7,
        "offset": 2,
        "kind": "preference",
        "status": "active",
        "query": "api",
    }
    assert admin_calls[4][1] == {"memory_id": "mem-1", "reason": "cleanup"}
    assert admin_calls[6][1] == {
        "memory_id": "mem-2",
        "text": "ignored when id is present",
        "limit": 4,
    }

    query = runtime.engine.queries[0]
    assert query.intent == "context"
    assert query.text == "api examples"
    assert query.limit == 3
    assert query.filters.kinds == ("preference",)

    mutation = runtime.engine.mutations[0]
    assert mutation.kind == "remember"
    assert mutation.summary == "remember this through pending"
    assert mutation.memory_kind == "preference"
    assert mutation.stable is True
    assert mutation.metadata == {"importance": 0.9, "confidence": 0.8}
    assert runtime.optimize_calls == 1


@pytest.mark.asyncio
async def test_memory_server_list_detail_and_404(tmp_path) -> None:
    runtime = await _runtime_with_memory(tmp_path)
    client = TestClient(create_memory_app(runtime, runtime.config))

    listed = client.get("/memory", params={"query": "prefers", "limit": 10})
    detail = client.get("/memory/pref")
    missing = client.get("/memory/missing")

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == ["pref"]
    assert detail.status_code == 200
    assert detail.json()["id"] == "pref"
    assert "embedding" not in detail.json()
    assert missing.status_code == 404
    assert missing.json() == {
        "ok": False,
        "error": {"code": "not_found", "message": "Memory not found"},
    }


@pytest.mark.asyncio
async def test_memory_server_update_rejects_forbidden_fields(tmp_path) -> None:
    runtime = await _runtime_with_memory(tmp_path)
    client = TestClient(create_memory_app(runtime, runtime.config))

    updated = client.patch(
        "/memory/pref",
        json={"summary": "User prefers updated API examples", "tags": ["api"]},
    )
    forbidden_id = client.patch("/memory/pref", json={"id": "other"})
    forbidden_embedding = client.patch("/memory/pref", json={"embedding_json": [1.0]})

    assert updated.status_code == 200
    assert updated.json()["summary"] == "User prefers updated API examples"
    assert runtime.store.read_index()[0].text == "User prefers updated API examples"
    assert forbidden_id.status_code == 400
    assert forbidden_id.json()["error"]["code"] == "invalid_request"
    assert forbidden_embedding.status_code == 400


@pytest.mark.asyncio
async def test_memory_server_delete_and_batch_delete(tmp_path) -> None:
    runtime = await _runtime_with_memory(tmp_path)
    client = TestClient(create_memory_app(runtime, runtime.config))

    deleted = client.request("DELETE", "/memory/pref", json={"reason": "cleanup"})
    batch = client.post(
        "/memory/batch-delete",
        json={"ids": ["event", "missing"], "reason": "batch cleanup"},
    )

    assert deleted.status_code == 200
    assert deleted.json()["affected_ids"] == ["pref"]
    assert batch.status_code == 200
    assert batch.json()["affected_ids"] == ["event"]
    assert batch.json()["missing_ids"] == ["missing"]


@pytest.mark.asyncio
async def test_memory_server_find_similar_events_and_stats(tmp_path) -> None:
    runtime = await _runtime_with_memory(tmp_path)
    client = TestClient(create_memory_app(runtime, runtime.config))

    invalid = client.post("/memory/find-similar", json={})
    invalid_recall = client.post("/memory/recall", json={"intent": "answer"})
    similar = client.post("/memory/find-similar", json={"text": "API examples", "limit": 5})
    events = client.get("/memory/events", params={"limit": 5})
    stats = client.get("/memory/stats")

    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"
    assert invalid_recall.status_code == 400
    assert invalid_recall.json()["error"]["code"] == "invalid_request"
    assert similar.status_code == 200
    assert similar.json()["items"]
    assert events.status_code == 200
    assert events.json()["events"][0]["id"] == "event"
    assert stats.status_code == 200
    assert stats.json()["active"] == 2


@pytest.mark.asyncio
async def test_memory_server_recall_memorize_optimize_and_scheduler_state(tmp_path) -> None:
    runtime = MemoryRuntime(tmp_path, MemoryConfig(optimizer_state_path="optimizer_state.json"))
    await runtime.initialize()
    client = TestClient(create_memory_app(runtime, runtime.config))

    memorize = client.post(
        "/memory/memorize",
        json={
            "summary": "User prefers server tests",
            "memory_kind": "preference",
            "source_ref": "manual:test",
        },
    )
    recall_before = client.post(
        "/memory/recall",
        json={"query": "server tests", "intent": "answer", "limit": 5},
    )

    assert memorize.status_code == 200
    assert memorize.json()["status"] == "pending"
    assert runtime.store.read_index() == []
    assert recall_before.status_code == 200
    assert recall_before.json()["records"] == []

    optimize = client.post("/memory/optimize", json={"force": True})
    recall_after = client.post(
        "/memory/recall",
        json={"query": "server tests", "intent": "answer", "limit": 5},
    )
    state = client.get("/memory/optimizer/state")

    assert optimize.status_code == 200
    assert optimize.json()["added"] == 1
    assert recall_after.status_code == 200
    assert recall_after.json()["records"][0]["id"]
    assert state.status_code == 200
    assert state.json() == {}
