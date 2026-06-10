from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from bootstrap.app import build_app_runtime
from bootstrap.config import Settings
from drift.models import DriftContext, DriftResult


@dataclass(slots=True)
class AlwaysTask:
    name: str
    interval_seconds: int = 1
    requires_main_model: bool = False

    async def should_run(self, ctx: DriftContext) -> bool:
        return True

    async def run(self, ctx: DriftContext) -> DriftResult:
        return DriftResult(self.name, True, "已运行")


@pytest.mark.asyncio
async def test_drift_runtime_skips_when_not_idle(tmp_path) -> None:
    config = Settings()
    config.drift.run_only_when_idle = True
    config.drift.idle_after_seconds = 600
    runtime = build_app_runtime(config, tmp_path)
    await runtime.drift_runtime.add_task(AlwaysTask("task1"))

    assert await runtime.drift_runtime.run_once() == []
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_drift_runtime_runs_when_idle_and_limits_tasks(tmp_path) -> None:
    config = Settings()
    config.drift.run_only_when_idle = True
    config.drift.idle_after_seconds = 1
    config.drift.max_tasks_per_cycle = 1
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    await runtime.session_manager.initialize()
    await runtime.drift_runtime.add_task(AlwaysTask("task1"))
    await runtime.drift_runtime.add_task(AlwaysTask("task2"))
    runtime.drift_runtime.last_user_activity_at = datetime.now(timezone.utc) - timedelta(seconds=5)

    results = await runtime.drift_runtime.run_once()

    assert [result.task_name for result in results] == ["task1"]
    await runtime.llm_provider.close()


class NamedProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.model = name

    async def complete(self, messages, *, temperature=None, timeout_seconds=None, purpose=None):
        return self.name

    async def close(self) -> None:
        return None


@dataclass(slots=True)
class CaptureProviderTask:
    name: str
    requires_main_model: bool = False
    interval_seconds: int = 1
    seen_provider_name: str | None = None

    async def should_run(self, ctx: DriftContext) -> bool:
        self.seen_provider_name = ctx.llm_provider.name
        assert ctx.fast_llm_provider is not None
        return True

    async def run(self, ctx: DriftContext) -> DriftResult:
        self.seen_provider_name = ctx.llm_provider.name
        return DriftResult(self.name, True, "已运行")


@pytest.mark.asyncio
async def test_drift_context_uses_fast_provider_by_default(tmp_path) -> None:
    config = Settings()
    config.drift.run_only_when_idle = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    await runtime.session_manager.initialize()
    runtime.drift_runtime.fast_llm_provider = NamedProvider("fast")
    task = CaptureProviderTask("capture_fast")
    await runtime.drift_runtime.add_task(task)

    await runtime.drift_runtime.run_once()

    assert task.seen_provider_name == "fast"
    await runtime.llm_provider.close()


@pytest.mark.asyncio
async def test_drift_context_uses_main_provider_when_required(tmp_path) -> None:
    config = Settings()
    config.drift.run_only_when_idle = False
    runtime = build_app_runtime(config, tmp_path)
    await runtime.memory_runtime.initialize()
    await runtime.session_manager.initialize()
    runtime.drift_runtime.fast_llm_provider = NamedProvider("fast")
    task = CaptureProviderTask("capture_main", requires_main_model=True)
    await runtime.drift_runtime.add_task(task)

    await runtime.drift_runtime.run_once()

    assert task.seen_provider_name == runtime.llm_provider.name
    await runtime.llm_provider.close()
