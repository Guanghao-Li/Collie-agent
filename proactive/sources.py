from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from memory.runtime import MemoryRuntime
from proactive.models import ProactiveCandidate


class ProactiveSource(Protocol):
    name: str

    async def fetch(self) -> list[ProactiveCandidate]:
        ...


@dataclass(slots=True)
class ProactiveSourceRegistry:
    sources: dict[str, ProactiveSource] = field(default_factory=dict)

    def register(self, source: ProactiveSource) -> None:
        self.sources[source.name] = source

    def list_sources(self) -> list[ProactiveSource]:
        return list(self.sources.values())


class MemoryReminderSource:
    name = "memory_reminder"

    def __init__(self, memory_runtime: MemoryRuntime) -> None:
        self.memory_runtime = memory_runtime

    async def fetch(self) -> list[ProactiveCandidate]:
        items = await self.memory_runtime.search("目标 项目 事件 跟进 提醒 goal project event follow up reminder", limit=5)
        return [
            ProactiveCandidate(
                source=self.name,
                title=f"跟进 {item.type}",
                content=item.text,
                metadata={"memory_id": item.id},
            )
            for item in items
            if item.type in {"goal", "project", "event"}
        ]


class RecentContextSource:
    name = "recent_context"

    def __init__(self, memory_runtime: MemoryRuntime) -> None:
        self.memory_runtime = memory_runtime

    async def fetch(self) -> list[ProactiveCandidate]:
        context = (await self.memory_runtime.read_recent_context()).strip()
        if len(context) < 40:
            return []
        return [
            ProactiveCandidate(
                source=self.name,
                title="近期上下文跟进",
                content=context[:1200],
            )
        ]


class ManualCandidateSource:
    name = "manual"

    def __init__(self) -> None:
        self._candidates: list[ProactiveCandidate] = []

    def add_candidate(self, title: str, content: str, url: str = "") -> ProactiveCandidate:
        candidate = ProactiveCandidate(source=self.name, title=title, content=content, url=url)
        self._candidates.append(candidate)
        return candidate

    async def fetch(self) -> list[ProactiveCandidate]:
        candidates = list(self._candidates)
        self._candidates.clear()
        return candidates
