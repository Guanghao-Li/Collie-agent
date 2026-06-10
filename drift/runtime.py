from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import logging

from agent.llm import LLMProvider
from bootstrap.config import Settings
from bus.event_bus import AfterDriftTaskEvent, BeforeDriftTaskEvent, EventBus
from drift.models import DriftContext, DriftResult
from drift.registry import DriftTask, DriftTaskRegistry
from memory.runtime import MemoryRuntime
from proactive.runtime import ProactiveRuntime
from session.manager import SessionManager


class DriftRuntime:
    def __init__(
        self,
        config: Settings,
        task_registry: DriftTaskRegistry,
        memory_runtime: MemoryRuntime,
        session_manager: SessionManager,
        proactive_runtime: ProactiveRuntime,
        llm_provider: LLMProvider,
        event_bus: EventBus,
        fast_llm_provider: LLMProvider | None = None,
    ) -> None:
        self.config = config
        self.task_registry = task_registry
        self.memory_runtime = memory_runtime
        self.session_manager = session_manager
        self.proactive_runtime = proactive_runtime
        self.llm_provider = llm_provider
        self.main_llm_provider = llm_provider
        self.fast_llm_provider = fast_llm_provider or llm_provider
        self.event_bus = event_bus
        self.last_user_activity_at = datetime.now(timezone.utc)
        self.last_run_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._logger = logging.getLogger(__name__)

    async def start(self) -> None:
        if self._running or not self.config.drift.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self.run_loop(), name="drift-runtime")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except Exception:
                self._logger.exception("Drift cycle 执行失败")
            await asyncio.sleep(self.config.drift.interval_seconds)

    async def run_once(self, force: bool = False) -> list[DriftResult]:
        if not force and self.config.drift.run_only_when_idle and not self._is_idle():
            return []
        now = datetime.now(timezone.utc)
        results: list[DriftResult] = []
        for task in self.task_registry.list_tasks():
            if len(results) >= self.config.drift.max_tasks_per_cycle:
                break
            selected_provider = (
                self.main_llm_provider
                if getattr(task, "requires_main_model", False)
                else self.fast_llm_provider
            )
            ctx = DriftContext(
                memory_runtime=self.memory_runtime,
                session_manager=self.session_manager,
                proactive_runtime=self.proactive_runtime,
                llm_provider=selected_provider,
                main_llm_provider=self.main_llm_provider,
                fast_llm_provider=self.fast_llm_provider,
                current_time=now,
                last_user_activity_at=self.last_user_activity_at,
            )
            if not await task.should_run(ctx):
                continue
            await self.event_bus.publish(BeforeDriftTaskEvent(task_name=task.name))
            try:
                result = await task.run(ctx)
            except Exception as exc:
                result = DriftResult(task.name, False, f"任务执行失败：{exc}")
                self._logger.exception("Drift 任务执行失败：%s", task.name)
            await self.event_bus.publish(
                AfterDriftTaskEvent(
                    task_name=result.task_name,
                    success=result.success,
                    summary=result.summary,
                )
            )
            results.append(result)
        if results:
            self.last_run_at = now
        return results

    async def add_task(self, task: DriftTask) -> None:
        self.task_registry.register(task)

    def touch_user_activity(self) -> None:
        self.last_user_activity_at = datetime.now(timezone.utc)

    def _is_idle(self) -> bool:
        delta = datetime.now(timezone.utc) - self.last_user_activity_at
        return delta.total_seconds() > self.config.drift.idle_after_seconds
