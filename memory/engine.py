from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping, Protocol

from memory.models import MemoryItem, MemorySearchTrace
from session.models import SessionMessage

MemoryQueryIntent = Literal[
    "context",
    "answer",
    "timeline",
    "interest",
    "procedure",
]

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
class MemoryScope:
    session_key: str = ""
    channel: str = ""
    chat_id: str = ""


@dataclass(slots=True)
class MemoryQueryFilters:
    kinds: tuple[str, ...] = ()
    time_start: datetime | None = None
    time_end: datetime | None = None
    hints: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kinds = tuple(str(item) for item in self.kinds if str(item).strip())
        self.hints = dict(self.hints)


@dataclass(slots=True)
class EvidenceRef:
    kind: Literal["message", "message_range", "turn", "external"] = "message"
    refs: list[str] = field(default_factory=list)
    resolver: str = "session"
    source_ref: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryRecord:
    id: str
    kind: str
    summary: str
    score: float
    engine_kind: str
    evidence: list[EvidenceRef] = field(default_factory=list)
    signals: dict[str, object] = field(default_factory=dict)
    injected: bool = False


@dataclass(slots=True)
class MemoryQuery:
    kind: MemoryQueryKind | None = None
    text: str | None = None
    limit: int = 8
    recent_messages: list[SessionMessage] = field(default_factory=list)
    intent: MemoryQueryIntent | None = None
    scope: MemoryScope = field(default_factory=MemoryScope)
    filters: MemoryQueryFilters = field(default_factory=MemoryQueryFilters)
    context: dict[str, object] = field(default_factory=dict)
    timestamp: datetime | None = None

    def effective_kind(self) -> str:
        if self.kind:
            return self.kind
        if self.intent == "answer":
            return "search"
        if self.intent == "context":
            return "context"
        if self.intent == "timeline":
            return "timeline"
        if self.intent == "procedure":
            return "procedure"
        if self.intent == "interest":
            return "search"
        return "context"


@dataclass(slots=True)
class MemoryQueryResult:
    items: list[MemoryItem] = field(default_factory=list)
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    trace: MemorySearchTrace | None = None
    records: list[MemoryRecord] = field(default_factory=list)
    text_block: str = ""
    raw: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.content and not self.text_block:
            self.text_block = self.content
        elif self.text_block and not self.content:
            self.content = self.text_block


@dataclass(slots=True)
class MemoryMutation:
    kind: MemoryMutationKind
    item: MemoryItem | None = None
    memory_id: str | None = None
    reason: str = ""
    content: str = ""
    stable: bool = True
    scope: MemoryScope = field(default_factory=MemoryScope)
    summary: str = ""
    memory_kind: str = ""
    source_ref: str = ""
    ids: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ids = tuple(str(item) for item in self.ids if str(item).strip())
        self.metadata = dict(self.metadata)


@dataclass(slots=True)
class MemoryMutationResult:
    ok: bool = False
    item: MemoryItem | None = None
    affected_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    accepted: bool | None = None
    item_id: str = ""
    actual_kind: str = ""
    status: str = ""
    missing_ids: list[str] = field(default_factory=list)
    items: list[dict[str, object]] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.accepted is None:
            self.accepted = self.ok
        else:
            self.ok = bool(self.accepted)
        if self.item is not None and not self.item_id:
            self.item_id = self.item.id
        if self.item is not None and not self.actual_kind:
            self.actual_kind = self.item.type
        if self.item is not None and not self.status:
            self.status = self.item.status


class MemoryEngine(Protocol):
    async def initialize(self) -> None: ...

    async def query(self, query: MemoryQuery) -> MemoryQueryResult: ...

    async def mutate(self, mutation: MemoryMutation) -> MemoryMutationResult: ...

    async def describe(self) -> dict[str, Any]: ...
