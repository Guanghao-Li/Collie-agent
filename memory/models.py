from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

MemoryType = Literal[
    "fact",
    "preference",
    "goal",
    "project",
    "relationship",
    "habit",
    "instruction",
    "event",
    "summary",
    "reflection",
]
MemoryStatus = Literal["active", "pending", "deleted", "superseded", "lowered_confidence"]


@dataclass(slots=True)
class MemoryItem:
    type: MemoryType
    text: str
    tags: list[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.5
    source: str = "unknown"
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime | None = None
    status: MemoryStatus = "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "tags": self.tags,
            "importance": self.importance,
            "confidence": self.confidence,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryItem":
        return cls(
            id=str(data.get("id") or uuid4()),
            type=data.get("type", "fact"),
            text=str(data.get("text", "")),
            tags=[str(tag) for tag in data.get("tags", [])],
            importance=float(data.get("importance", 0.5)),
            confidence=float(data.get("confidence", 0.5)),
            source=str(data.get("source", "unknown")),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
            last_used_at=_parse_optional_datetime(data.get("last_used_at")),
            status=data.get("status", "active"),
        )


@dataclass(slots=True)
class ConsolidationResult:
    processed: int = 0
    added: int = 0
    merged: int = 0
    conflicts: int = 0
    discarded: int = 0
    summary: str = ""


@dataclass(slots=True)
class MemoryGateDecision:
    should_search: bool
    reason: str
    query_type: str = "none"
    suggested_query: str | None = None
    memory_types: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemorySearchTrace:
    gate_decision: MemoryGateDecision
    original_query: str
    rewritten_query: str | None = None
    hyde_document: str | None = None
    used_fast_model: bool = False
    fast_model_name: str | None = None
    selected_memory_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemorySearchResult:
    memories: list[MemoryItem]
    trace: MemorySearchTrace


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value)
