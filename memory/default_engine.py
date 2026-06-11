from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bootstrap.config import MemoryConfig
from memory.embedder import DisabledEmbedder, Embedder, OpenAICompatibleEmbedder
from memory.admin import MemoryAdminService
from memory.engine import (
    EvidenceRef,
    MemoryMutation,
    MemoryMutationResult,
    MemoryQuery,
    MemoryQueryResult,
    MemoryRecord,
)
from memory.markdown_store import MarkdownMemoryStore
from memory.memory2_store import SQLiteMemory2Store
from memory.models import MemoryItem
from memory.retriever import MemoryRetriever
from memory.search import MemorySearch
from memory.store import MemoryStore
from memory.vector_store import (
    DisabledVectorMemoryStore,
    SQLiteVectorMemoryStore,
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
        memory2_store: SQLiteMemory2Store | None = None,
        embedder: Embedder | None = None,
        vector_store: VectorMemoryStore | None = None,
    ) -> None:
        self.memory_dir = Path(memory_dir)
        self.config = config or MemoryConfig()
        self.store = store or MemoryStore(self.memory_dir)
        self.search_engine = search_engine or MemorySearch()
        self.markdown_store = MarkdownMemoryStore(self.memory_dir)
        self.memory2_store = memory2_store or SQLiteMemory2Store(
            self._resolve_vector_db_path(),
            embedding_dimension=self.config.embedding_dimension,
        )
        self.embedder = embedder or self._build_default_embedder()
        self.vector_store = vector_store or self._build_default_vector_store()
        self.retriever = MemoryRetriever(
            store=self.store,
            markdown_store=self.markdown_store,
            memory2_store=self.memory2_store,
            vector_store=self.vector_store,
            search=self.search_engine,
            config=self.config,
        )
        self.admin_service = MemoryAdminService(
            store=self.store,
            markdown_store=self.markdown_store,
            memory2_store=self.memory2_store,
            vector_store=self.vector_store,
            retriever=self.retriever,
            config=self.config,
        )
        self._last_vector_error = ""

    async def initialize(self) -> None:
        await self.store.initialize()
        await self.markdown_store.initialize()
        await self.memory2_store.initialize()
        await self.vector_store.initialize()
        index = self.store.read_index()
        self.markdown_store.sync_active_from_index(index)
        await self._sync_memory_backends(index)

    async def query(self, query: MemoryQuery) -> MemoryQueryResult:
        kind = query.effective_kind()
        if kind == "core":
            return MemoryQueryResult(content=self.markdown_store.read_text(self.markdown_store.memory_md))
        if kind == "profile":
            return MemoryQueryResult(content=self.markdown_store.read_profile())
        if kind == "recent_context":
            return MemoryQueryResult(content=self.markdown_store.read_recent_context())
        if kind in {"timeline", "procedure", "search"} or query.intent in {
            "answer",
            "timeline",
            "procedure",
            "interest",
        }:
            return await self.retriever.retrieve(query)
        if kind == "stats":
            index = self.store.read_index()
            transient_pending = self.store.read_pending()
            pending_candidates = self.markdown_store.parse_pending_candidates()
            memory2_stats = await self.memory2_store.describe()
            stats = {
                "active": sum(1 for item in index if item.status == "active"),
                "pending": len(transient_pending) + len(pending_candidates),
                "pending_transient": len(transient_pending),
                "pending_candidates": len(pending_candidates),
                "deleted": sum(1 for item in index if item.status == "deleted"),
                "memory2_total": int(memory2_stats.get("items", {}).get("total", 0)),
                "memory2_active": int(memory2_stats.get("items", {}).get("active", 0)),
            }
            return MemoryQueryResult(metadata={"stats": stats})
        if kind == "context":
            profile = self.markdown_store.read_profile().strip()
            recent_context = self.markdown_store.read_recent_context().strip()
            result = await self.retriever.build_injection(query)
            lines = [profile, recent_context, result.text_block.strip()]
            content = "\n\n".join(line for line in lines if line)
            result.content = content
            result.text_block = content
            return result
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
                    status="pending",
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
            result = await self.admin_service.batch_delete(ids, reason=mutation.reason)
            return MemoryMutationResult(
                ok=bool(result["affected_ids"]),
                affected_ids=list(result["affected_ids"]),
                missing_ids=list(result["missing_ids"]),
                status="deleted" if result["affected_ids"] else "missing",
            )
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
            self.markdown_store.sync_active_from_index(index, force=True)
            await self._sync_memory_backends(index)
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
            "memory2": await self.memory2_store.describe(),
            "embedder": await self.embedder.describe(),
            "vector_memory": await self.vector_store.describe(),
            "retriever": {
                "enabled": True,
                "retrieval_mode": (
                    "hybrid" if self.vector_store.is_enabled() else "keyword_only"
                ),
                "vector_enabled": self.vector_store.is_enabled(),
                "memory2_enabled": self.memory2_store.db_path.exists(),
                "injection_budget_chars": self.config.memory_injection_budget_chars,
            },
            "admin": {
                "enabled": True,
                "backend": "memory2" if self.memory2_store.db_path.exists() else "index",
                "methods": [
                    "list_dashboard",
                    "get_dashboard_detail",
                    "update_dashboard_memory",
                    "delete_dashboard_memory",
                    "batch_delete",
                    "find_similar",
                    "list_event_range",
                    "get_stats",
                ],
            },
            "scheduled_optimizer": {
                "enabled": self.config.optimizer_auto_run,
                "interval_seconds": self.config.optimizer_interval_seconds,
                "state_path": self.config.optimizer_state_path,
            },
            "files": {
                "memory": str(self.markdown_store.memory_md),
                "self": str(self.markdown_store.self_md),
                "history": str(self.markdown_store.history_md),
                "recent_context": str(self.markdown_store.recent_context_md),
                "pending": str(self.markdown_store.pending_md),
                "profile": str(self.store.profile_md),
                "pending_queue": str(self.store.pending_jsonl),
                "index": str(self.store.index_json),
            },
        }

    async def _remember(self, item: MemoryItem, *, stable: bool) -> MemoryMutationResult:
        now = datetime.now(timezone.utc)
        item.updated_at = now
        item.status = "pending"
        if stable:
            item.importance = max(item.importance, 0.75)
            if item.type == "fact":
                item.type = "requested_memory"
            if "requested_memory" not in item.tags and item.type == "requested_memory":
                item.tags.append("requested_memory")
        tag = _pending_tag_from_item(item)
        written = self.markdown_store.append_pending_candidate(
            tag,
            item.text,
            source_ref=item.source_ref or item.source,
            confidence=item.confidence,
            importance=item.importance,
            metadata={**item.metadata, "tags": list(item.tags)},
        )
        return MemoryMutationResult(
            ok=True,
            item=item,
            affected_ids=[item.id],
            item_id=item.id,
            actual_kind=item.type,
            status=item.status,
            metadata={"pending_written": written, "duplicate": not written},
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
        await self.memory2_store.batch_delete(affected_ids, soft=True)
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
        result = await self.retriever.retrieve(
            MemoryQuery(kind="search", text=query, limit=limit)
        )
        return result.items, result.metadata

    def _build_default_vector_store(self) -> VectorMemoryStore:
        if not self.config.enable_vector_memory:
            return DisabledVectorMemoryStore(reason="disabled by config")
        if isinstance(self.embedder, DisabledEmbedder):
            return DisabledVectorMemoryStore(
                requested=True,
                reason=self.embedder.reason,
            )
        return SQLiteVectorMemoryStore(
            memory2_store=self.memory2_store,
            embedder=self.embedder,
        )

    def _build_default_embedder(self) -> Embedder:
        if not self.config.enable_vector_memory:
            return DisabledEmbedder(reason="vector memory disabled by config")
        missing = []
        if not self.config.embedding_model:
            missing.append("embedding_model")
        if not self.config.embedding_api_key:
            missing.append("embedding_api_key")
        if not self.config.embedding_base_url:
            missing.append("embedding_base_url")
        if missing:
            return DisabledEmbedder(
                requested=True,
                reason="embedding config missing: " + ", ".join(missing),
            )
        return OpenAICompatibleEmbedder(
            model=self.config.embedding_model,
            api_key=self.config.embedding_api_key,
            base_url=self.config.embedding_base_url,
            timeout_seconds=self.config.embedding_timeout_seconds,
            dimension=self.config.embedding_dimension,
        )

    def _resolve_vector_db_path(self) -> Path:
        db_path = Path(self.config.vector_db_path)
        if db_path.is_absolute():
            return db_path
        return self.memory_dir.parent / db_path

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
                "reinforcement": int(item.metadata.get("reinforcement") or 0),
                "source_refs": list(item.metadata.get("source_refs", []))
                if isinstance(item.metadata.get("source_refs"), list)
                else [],
                "source": item.source,
                "emotional_weight": item.emotional_weight,
            },
        )

    async def _sync_memory_backends(self, items: list[MemoryItem]) -> None:
        for item in items:
            try:
                if item.status == "active":
                    if self.vector_store.is_enabled():
                        await self.vector_store.upsert(self._to_vector_record(item))
                    else:
                        await self.memory2_store.upsert_item(item)
                else:
                    await self.memory2_store.delete_item(item.id, soft=True)
                    await self.vector_store.delete(item.id)
            except Exception as exc:
                self._last_vector_error = str(exc)

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


def _pending_tag_from_item(item: MemoryItem) -> str:
    raw = str(item.metadata.get("tag") or "").strip().lower()
    if raw:
        return raw
    if item.type in {
        "identity",
        "preference",
        "key_info",
        "health_long_term",
        "requested_memory",
        "correction",
        "procedure",
    }:
        return item.type
    if item.type == "instruction":
        return "procedure"
    if item.type in {"goal", "project", "habit", "relationship"}:
        return "preference"
    return "requested_memory"
