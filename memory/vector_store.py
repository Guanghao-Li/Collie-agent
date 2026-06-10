from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


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
