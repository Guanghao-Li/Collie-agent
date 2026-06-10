from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bootstrap.config import MemoryConfig
from memory.engine import MemoryMutation, MemoryMutationResult, MemoryQuery, MemoryQueryResult
from memory.markdown_store import MarkdownMemoryStore
from memory.models import MemoryItem
from memory.search import MemorySearch
from memory.store import MemoryStore
from memory.vector_store import (
    DisabledVectorMemoryStore,
    VectorMemoryRecord,
    VectorMemoryStore,
)


class DefaultMemoryEngine:
    def __init__(
        self,
        memory_dir: str | Path,
        *,
        config: MemoryConfig | None = None,
        store: MemoryStore | None = None,
        search_engine: MemorySearch | None = None,
        vector_store: VectorMemoryStore | None = None,
    ) -> None:
        self.memory_dir = Path(memory_dir)
        self.config = config or MemoryConfig()
        self.store = store or MemoryStore(self.memory_dir)
        self.search_engine = search_engine or MemorySearch()
        self.markdown_store = MarkdownMemoryStore(self.memory_dir)
        self.vector_store = vector_store or self._build_default_vector_store()

    async def initialize(self) -> None:
        await self.store.initialize()
        await self.markdown_store.initialize()
        await self.vector_store.initialize()
        index = self.store.read_index()
        self.markdown_store.sync_from_legacy(index, self.store.read_pending())
        await self._sync_vector_store(index)

    async def query(self, query: MemoryQuery) -> MemoryQueryResult:
        if query.kind == "core":
            return MemoryQueryResult(content=self.markdown_store.read_text(self.markdown_store.memory_md))
        if query.kind == "profile":
            return MemoryQueryResult(content=self.markdown_store.read_profile())
        if query.kind == "recent_context":
            return MemoryQueryResult(content=self.markdown_store.read_recent_context())
        if query.kind == "search":
            items, metadata = await self._search(query.text or "", query.limit)
            return MemoryQueryResult(items=items, metadata=metadata)
        if query.kind == "stats":
            index = self.store.read_index()
            pending = self.store.read_pending()
            stats = {
                "active": sum(1 for item in index if item.status == "active"),
                "pending": len(pending),
                "deleted": sum(1 for item in index if item.status == "deleted"),
            }
            return MemoryQueryResult(metadata={"stats": stats})
        if query.kind == "context":
            profile = self.markdown_store.read_profile().strip()
            recent_context = self.markdown_store.read_recent_context().strip()
            items, metadata = await self._search(query.text or "", query.limit)
            lines = [profile, recent_context]
            if items:
                lines.append("Relevant memories:")
                lines.extend(f"- {item.text}" for item in items)
            return MemoryQueryResult(
                items=items,
                content="\n\n".join(line for line in lines if line),
                metadata=metadata,
            )
        raise ValueError(f"unsupported memory query kind: {query.kind}")

    async def mutate(self, mutation: MemoryMutation) -> MemoryMutationResult:
        if mutation.kind == "remember":
            if mutation.item is None:
                raise ValueError("remember mutation requires an item")
            return await self._remember(mutation.item, stable=mutation.stable)
        if mutation.kind == "forget":
            if not mutation.memory_id:
                raise ValueError("forget mutation requires memory_id")
            return await self._forget(mutation.memory_id, mutation.reason)
        if mutation.kind == "replace_recent_context":
            self.markdown_store.write_recent_context(mutation.content)
            return MemoryMutationResult(ok=True)
        if mutation.kind == "append_history":
            self.markdown_store.append_history(mutation.content)
            return MemoryMutationResult(ok=True)
        if mutation.kind == "sync":
            index = self.store.read_index()
            self.markdown_store.sync_from_legacy(
                index,
                self.store.read_pending(),
                force=True,
            )
            await self._sync_vector_store(index)
            return MemoryMutationResult(ok=True)
        raise ValueError(f"unsupported memory mutation kind: {mutation.kind}")

    async def describe(self) -> dict[str, Any]:
        return {
            "name": "default",
            "backend": "memory_store+memory_search",
            "memory_dir": str(self.memory_dir),
            "vector_memory": await self.vector_store.describe(),
            "files": {
                "memory": str(self.markdown_store.memory_md),
                "self": str(self.markdown_store.self_md),
                "history": str(self.markdown_store.history_md),
                "recent_context": str(self.markdown_store.recent_context_md),
                "pending": str(self.markdown_store.pending_md),
                "profile_legacy": str(self.store.profile_md),
                "pending_legacy": str(self.store.pending_jsonl),
                "index_legacy": str(self.store.index_json),
            },
        }

    async def _remember(self, item: MemoryItem, *, stable: bool) -> MemoryMutationResult:
        now = datetime.now(timezone.utc)
        item.updated_at = now
        if stable:
            item.status = "active"
            items = self.store.read_index()
            items = [existing for existing in items if existing.id != item.id]
            items.append(item)
            self.store.write_index(items)
            self.markdown_store.render_active_memories(items)
        else:
            item.status = "pending"
            self.store.append_pending(item)
            self.markdown_store.render_pending_memories(self.store.read_pending())
        if stable:
            await self.vector_store.upsert(self._to_vector_record(item))
        return MemoryMutationResult(ok=True, item=item, affected_ids=[item.id])

    async def _forget(self, memory_id: str, reason: str) -> MemoryMutationResult:
        items = self.store.read_index()
        now = datetime.now(timezone.utc)
        affected_ids: list[str] = []
        for item in items:
            if item.id != memory_id:
                continue
            item.status = "deleted"
            item.updated_at = now
            self.store.append_deleted(item, reason)
            affected_ids.append(item.id)
            break
        self.store.write_index(items)
        self.markdown_store.render_active_memories(items)
        await self.vector_store.delete(memory_id)
        return MemoryMutationResult(ok=bool(affected_ids), affected_ids=affected_ids)

    async def _search(self, query: str, limit: int) -> tuple[list[MemoryItem], dict[str, Any]]:
        if not query.strip():
            return [], {"count": 0, "search_backend": "none", "vector_enabled": False}
        if self.vector_store.is_enabled():
            matches = await self.vector_store.search(
                query,
                top_k=min(limit, self.config.vector_top_k),
                score_threshold=self.config.vector_score_threshold,
            )
            if matches:
                items = self._load_items_by_id(match.record.item_id for match in matches)
                return items, {
                    "count": len(items),
                    "search_backend": "vector",
                    "vector_enabled": True,
                }
        items = self.store.read_index()
        results = self.search_engine.search(items, query, limit)
        if not results:
            return [], {
                "count": 0,
                "search_backend": "keyword",
                "vector_enabled": self.vector_store.is_enabled(),
            }
        now = datetime.now(timezone.utc)
        result_ids = {item.id for item in results}
        for item in items:
            if item.id in result_ids:
                item.last_used_at = now
        self.store.write_index(items)
        return results, {
            "count": len(results),
            "search_backend": "keyword",
            "vector_enabled": self.vector_store.is_enabled(),
        }

    def _build_default_vector_store(self) -> VectorMemoryStore:
        if not self.config.enable_vector_memory:
            return DisabledVectorMemoryStore(reason="vector memory disabled in config")
        return DisabledVectorMemoryStore(
            requested=True,
            reason="vector memory requested but no backend is configured",
        )

    def _load_items_by_id(self, item_ids: Any) -> list[MemoryItem]:
        items_by_id = {item.id: item for item in self.store.read_index() if item.status == "active"}
        resolved: list[MemoryItem] = []
        for item_id in item_ids:
            item = items_by_id.get(item_id)
            if item is not None:
                resolved.append(item)
        return resolved

    def _to_vector_record(self, item: MemoryItem) -> VectorMemoryRecord:
        return VectorMemoryRecord(
            item_id=item.id,
            memory_type=item.type,
            summary=item.text,
            source_ref=item.source,
            happened_at=item.last_used_at or item.created_at,
            status=item.status,
            created_at=item.created_at,
            updated_at=item.updated_at,
            embedding=None,
            metadata={
                "tags": list(item.tags),
                "importance": item.importance,
                "confidence": item.confidence,
            },
        )

    async def _sync_vector_store(self, items: list[MemoryItem]) -> None:
        for item in items:
            if item.status == "active":
                await self.vector_store.upsert(self._to_vector_record(item))
            else:
                await self.vector_store.delete(item.id)
