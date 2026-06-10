from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from drift.models import DriftContext, DriftResult


class DriftTask(Protocol):
    name: str
    interval_seconds: int
    requires_main_model: bool

    async def should_run(self, ctx: DriftContext) -> bool:
        ...

    async def run(self, ctx: DriftContext) -> DriftResult:
        ...


@dataclass(slots=True)
class DriftTaskRegistry:
    tasks: dict[str, DriftTask] = field(default_factory=dict)

    def register(self, task: DriftTask) -> None:
        self.tasks[task.name] = task

    def list_tasks(self) -> list[DriftTask]:
        return list(self.tasks.values())
