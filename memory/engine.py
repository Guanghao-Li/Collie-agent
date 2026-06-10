from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from memory.models import MemoryItem, MemorySearchTrace
from session.models import SessionMessage

MemoryQueryKind = Literal[
    "core",
    "profile",
    "recent_context",
    "context",
    "search",
    "stats",
]

MemoryMutationKind = Literal[
    "remember",
    "forget",
    "replace_recent_context",
    "append_history",
    "sync",
]


@dataclass(slots=True)
class MemoryQuery:
    kind: MemoryQueryKind
    text: str | None = None
    limit: int = 8
    recent_messages: list[SessionMessage] = field(default_factory=list)


@dataclass(slots=True)
class MemoryQueryResult:
    items: list[MemoryItem] = field(default_factory=list)
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    trace: MemorySearchTrace | None = None


@dataclass(slots=True)
class MemoryMutation:
    kind: MemoryMutationKind
    item: MemoryItem | None = None
    memory_id: str | None = None
    reason: str = ""
    content: str = ""
    stable: bool = True


@dataclass(slots=True)
class MemoryMutationResult:
    ok: bool
    item: MemoryItem | None = None
    affected_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryEngine(Protocol):
    async def initialize(self) -> None: ...

    async def query(self, query: MemoryQuery) -> MemoryQueryResult: ...

    async def mutate(self, mutation: MemoryMutation) -> MemoryMutationResult: ...

    async def describe(self) -> dict[str, Any]: ...
