from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bootstrap.config import MemoryConfig
from memory.engine import (
    EvidenceRef,
    MemoryMutation,
    MemoryMutationResult,
    MemoryQuery,
    MemoryQueryResult,
    MemoryRecord,
)
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
        pending_for_markdown = (
            [] if self.config.consolidation_mode == "aka_like" else self.store.read_pending()
        )
        self.markdown_store.sync_from_legacy(index, pending_for_markdown)
        await self._sync_vector_store(index)

    async def query(self, query: MemoryQuery) -> MemoryQueryResult:
        kind = query.effective_kind()
        if kind == "core":
            return MemoryQueryResult(content=self.markdown_store.read_text(self.markdown_store.memory_md))
        if kind == "profile":
            return MemoryQueryResult(content=self.markdown_store.read_profile())
        if kind == "recent_context":
            return MemoryQueryResult(content=self.markdown_store.read_recent_context())
        if kind == "timeline":
            return self._query_timeline(query)
        if kind == "procedure":
            items, metadata = await self._search(query.text or "", query.limit)
            items = [
                item
                for item in items
                if item.type in {"procedure", "instruction"} or "procedure" in item.tags
            ]
            metadata.update({"intent": query.intent or "procedure", "count": len(items)})
            return MemoryQueryResult(
                items=items,
                metadata=metadata,
                records=self._items_to_records(items),
                raw={"items": [item.to_dict() for item in items]},
            )
        if kind == "search":
            items, metadata = await self._search(query.text or "", query.limit)
            if query.intent:
                metadata["intent"] = query.intent
            return MemoryQueryResult(
                items=items,
                metadata=metadata,
                records=self._items_to_records(items),
                raw={"items": [item.to_dict() for item in items]},
            )
        if kind == "stats":
            index = self.store.read_index()
            pending = self.store.read_pending()
            stats = {
                "active": sum(1 for item in index if item.status == "active"),
                "pending": len(pending),
                "deleted": sum(1 for item in index if item.status == "deleted"),
            }
            return MemoryQueryResult(metadata={"stats": stats})
        if kind == "context":
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
                records=self._items_to_records(items, injected=True),
                raw={"items": [item.to_dict() for item in items]},
            )
        raise ValueError(f"unsupported memory query kind: {kind}")

    async def mutate(self, mutation: MemoryMutation) -> MemoryMutationResult:
        if mutation.kind == "remember":
            if mutation.item is None and mutation.summary:
                mutation.item = MemoryItem(
                    type=mutation.memory_kind or "fact",
                    text=mutation.summary,
                    tags=[str(tag) for tag in mutation.metadata.get("tags", [])]
                    if isinstance(mutation.metadata.get("tags"), list)
                    else [],
                    importance=float(mutation.metadata.get("importance", 0.7)),
                    confidence=float(mutation.metadata.get("confidence", 0.8)),
                    source=mutation.source_ref or "memory_mutation",
                    source_ref=mutation.source_ref,
                    metadata=dict(mutation.metadata),
                    status="active" if mutation.stable else "pending",
                )
            if mutation.item is None:
                raise ValueError("remember mutation requires an item")
            return await self._remember(mutation.item, stable=mutation.stable)
        if mutation.kind == "forget":
            ids = list(mutation.ids)
            if mutation.memory_id:
                ids.insert(0, mutation.memory_id)
            ids = _dedupe_ids(ids)
            if not ids:
                raise ValueError("forget mutation requires memory_id or ids")
            return await self._forget_many(ids, mutation.reason)
        if mutation.kind == "replace_recent_context":
            self.markdown_store.write_recent_context(mutation.content)
            return MemoryMutationResult(ok=True)
        if mutation.kind == "append_history":
            self.markdown_store.append_history(
                mutation.content,
                source_ref=mutation.source_ref,
                happened_at=mutation.metadata.get("happened_at"),
                emotional_weight=int(mutation.metadata.get("emotional_weight", 0)),
            )
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
            "profile": "collie_compat_memory_engine",
            "capabilities": [
                "retrieve.context_block",
                "retrieve.structured_hits",
                "manage.history",
                "manage.update",
                "manage.delete",
            ],
            "tool_profile": {
                "recall": "search_memory",
                "memorize": "remember",
                "forget": "forget via memory mutation",
            },
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
            if self.config.consolidation_mode != "aka_like":
                self.markdown_store.render_pending_memories(self.store.read_pending())
        if stable:
            await self.vector_store.upsert(self._to_vector_record(item))
        return MemoryMutationResult(
            ok=True,
            item=item,
            affected_ids=[item.id],
            item_id=item.id,
            actual_kind=item.type,
            status=item.status,
            items=[item.to_dict()],
        )

    async def _forget_many(self, memory_ids: list[str], reason: str) -> MemoryMutationResult:
        items = self.store.read_index()
        now = datetime.now(timezone.utc)
        affected_ids: list[str] = []
        missing_ids: list[str] = []
        targets = set(memory_ids)
        for item in items:
            if item.id not in targets:
                continue
            item.status = "deleted"
            item.updated_at = now
            self.store.append_deleted(item, reason)
            affected_ids.append(item.id)
        missing_ids = [item_id for item_id in memory_ids if item_id not in affected_ids]
        self.store.write_index(items)
        self.markdown_store.render_active_memories(items)
        for memory_id in affected_ids:
            await self.vector_store.delete(memory_id)
        return MemoryMutationResult(
            ok=bool(affected_ids),
            affected_ids=affected_ids,
            missing_ids=missing_ids,
            status="deleted" if affected_ids else "missing",
        )

    async def _forget(self, memory_id: str, reason: str) -> MemoryMutationResult:
        return await self._forget_many([memory_id], reason)

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
            source_ref=item.source_ref or item.source,
            happened_at=item.happened_at or item.last_used_at or item.created_at,
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

    def _query_timeline(self, query: MemoryQuery) -> MemoryQueryResult:
        history = self.markdown_store.read_text(self.markdown_store.history_md)
        lines = [line for line in history.splitlines() if line.strip().startswith("[")]
        if query.text:
            needle = query.text.lower()
            lines = [line for line in lines if needle in line.lower()]
        selected = lines[-query.limit :]
        records = [
            MemoryRecord(
                id=f"history:{index}",
                kind="event",
                summary=line,
                score=1.0,
                engine_kind="default",
            )
            for index, line in enumerate(selected)
        ]
        content = "\n".join(selected)
        return MemoryQueryResult(
            content=content,
            metadata={"intent": query.intent or "timeline", "count": len(selected)},
            records=records,
            raw={"history_lines": selected},
        )

    def _items_to_records(
        self,
        items: list[MemoryItem],
        *,
        injected: bool = False,
    ) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for item in items:
            source_ref = item.source_ref or item.source
            evidence = []
            if source_ref:
                evidence.append(
                    EvidenceRef(
                        kind="turn" if source_ref.startswith("session:") else "external",
                        refs=[source_ref],
                        source_ref=source_ref,
                        metadata={"source": item.source},
                    )
                )
            records.append(
                MemoryRecord(
                    id=item.id,
                    kind=item.type,
                    summary=item.text,
                    score=1.0,
                    engine_kind="default",
                    evidence=evidence,
                    signals={
                        "importance": item.importance,
                        "confidence": item.confidence,
                        **item.metadata,
                    },
                    injected=injected,
                )
            )
        return records


def _dedupe_ids(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in ids:
        item_id = str(raw).strip()
        if item_id and item_id not in seen:
            seen.add(item_id)
            deduped.append(item_id)
    return deduped
