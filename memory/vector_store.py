from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from memory.embedder import DisabledEmbedder, Embedder
from memory.memory2_store import SQLiteMemory2Store
from memory.models import MemoryItem


@dataclass(slots=True)
class VectorMemoryRecord:
    item_id: str
    memory_type: str
    summary: str
    source_ref: str = ""
    happened_at: datetime | None = None
    status: str = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    embedding: list[float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VectorMemoryMatch:
    record: VectorMemoryRecord
    score: float


class VectorMemoryStore(Protocol):
    async def initialize(self) -> None: ...

    def is_enabled(self) -> bool: ...

    async def upsert(self, record: VectorMemoryRecord) -> None: ...

    async def delete(self, item_id: str) -> None: ...

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        score_threshold: float,
    ) -> list[VectorMemoryMatch]: ...

    async def describe(self) -> dict[str, Any]: ...


class NullVectorMemoryStore:
    def __init__(self, *, requested: bool = False, reason: str = "vector memory disabled") -> None:
        self.requested = requested
        self.reason = reason

    async def initialize(self) -> None:
        return None

    def is_enabled(self) -> bool:
        return False

    async def upsert(self, record: VectorMemoryRecord) -> None:
        return None

    async def delete(self, item_id: str) -> None:
        return None

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        score_threshold: float,
    ) -> list[VectorMemoryMatch]:
        return []

    async def describe(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "requested": self.requested,
            "backend": "null",
            "reason": self.reason,
        }


class DisabledVectorMemoryStore(NullVectorMemoryStore):
    def __init__(self, *, requested: bool = False, reason: str = "vector memory disabled") -> None:
        super().__init__(requested=requested, reason=reason)


class SQLiteVectorMemoryStore:
    def __init__(
        self,
        *,
        memory2_store: SQLiteMemory2Store,
        embedder: Embedder,
        fallback_mode: str = "numpy-cosine",
    ) -> None:
        self.memory2_store = memory2_store
        self.embedder = embedder
        self.fallback_mode = fallback_mode
        self._enabled = not isinstance(embedder, DisabledEmbedder)
        self._last_error = ""

    async def initialize(self) -> None:
        await self.memory2_store.initialize()

    def is_enabled(self) -> bool:
        return self._enabled

    async def upsert(self, record: VectorMemoryRecord) -> None:
        if not self._enabled:
            return None
        embedding = record.embedding
        if embedding is None:
            [embedding] = await self.embedder.embed_texts([record.summary])
        item = self._record_to_memory_item(record)
        await self.memory2_store.upsert_item(item, embedding=embedding)

    async def delete(self, item_id: str) -> None:
        await self.memory2_store.delete_item(item_id, soft=True)

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        score_threshold: float,
    ) -> list[VectorMemoryMatch]:
        if not self._enabled or not query.strip():
            return []
        [embedding] = await self.embedder.embed_texts([query])
        rows = await self.memory2_store.vector_search(embedding, limit=top_k)
        matches: list[VectorMemoryMatch] = []
        for row in rows:
            score = float(row.get("score") or 0.0)
            if score < score_threshold:
                continue
            matches.append(
                VectorMemoryMatch(
                    record=self._row_to_record(row),
                    score=score,
                )
            )
        return matches

    async def describe(self) -> dict[str, Any]:
        embedder_status = await self.embedder.describe()
        memory2_status = await self.memory2_store.describe()
        return {
            "enabled": self._enabled,
            "requested": True,
            "backend": "sqlite-memory2",
            "db_path": str(self.memory2_store.db_path),
            "embedder": embedder_status,
            "memory2": memory2_status,
            "fallback_mode": self.fallback_mode,
            "last_error": self._last_error,
        }

    def _record_to_memory_item(self, record: VectorMemoryRecord) -> MemoryItem:
        metadata = dict(record.metadata)
        return MemoryItem(
            id=record.item_id,
            type=record.memory_type,  # type: ignore[arg-type]
            text=record.summary,
            tags=[str(tag) for tag in metadata.get("tags", [])]
            if isinstance(metadata.get("tags"), list)
            else [],
            importance=_coerce_float(metadata.get("importance"), default=0.5),
            confidence=_coerce_float(metadata.get("confidence"), default=0.7),
            source=str(metadata.get("source") or "vector_memory"),
            source_ref=record.source_ref,
            happened_at=record.happened_at,
            emotional_weight=_coerce_int(metadata.get("emotional_weight"), default=0),
            metadata=metadata,
            created_at=record.created_at or datetime.now(),
            updated_at=record.updated_at or datetime.now(),
            status=record.status,  # type: ignore[arg-type]
        )

    def _row_to_record(self, row: dict[str, Any]) -> VectorMemoryRecord:
        extra = row.get("extra")
        metadata = dict(extra) if isinstance(extra, dict) else {}
        metadata.update(
            {
                "importance": row.get("importance"),
                "confidence": row.get("confidence"),
                "reinforcement": row.get("reinforcement"),
            }
        )
        return VectorMemoryRecord(
            item_id=str(row.get("id") or ""),
            memory_type=str(row.get("type") or "fact"),
            summary=str(row.get("summary") or ""),
            source_ref=str(row.get("source_ref") or ""),
            happened_at=_parse_datetime(row.get("happened_at")),
            status=str(row.get("status") or "active"),
            created_at=_parse_datetime(row.get("created_at")),
            updated_at=_parse_datetime(row.get("updated_at")),
            embedding=row.get("embedding") if isinstance(row.get("embedding"), list) else None,
            metadata=metadata,
        )


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
