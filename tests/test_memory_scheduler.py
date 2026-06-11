from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bootstrap.config import MemoryConfig
from memory.models import MemoryItem, OptimizationResult
from memory.runtime import MemoryRuntime
from memory.scheduler import MemoryOptimizerScheduler


class DummyOptimizer:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls = 0

    async def optimize(self) -> OptimizationResult:
        self.calls += 1
        if self.raises:
            raise RuntimeError("optimizer failed")
        return OptimizationResult(ok=True, processed=1, added=1, summary="ok")


def _scheduler(tmp_path, config: MemoryConfig) -> MemoryOptimizerScheduler:
    return MemoryOptimizerScheduler(
        config=config,
        state_path=tmp_path / "optimizer_state.json",
    )


def test_scheduler_auto_run_false_does_not_run(tmp_path) -> None:
    scheduler = _scheduler(tmp_path, MemoryConfig(optimizer_auto_run=False))

    assert scheduler.should_run(10, datetime.now(timezone.utc)) is False


def test_scheduler_pending_below_minimum_does_not_run(tmp_path) -> None:
    config = MemoryConfig(optimizer_auto_run=True, optimizer_min_pending=2)
    scheduler = _scheduler(tmp_path, config)

    assert scheduler.should_run(1, datetime.now(timezone.utc)) is False


def test_scheduler_interval_not_elapsed_does_not_run(tmp_path) -> None:
    config = MemoryConfig(optimizer_auto_run=True, optimizer_interval_seconds=100)
    scheduler = _scheduler(tmp_path, config)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scheduler.write_state({"last_run_at": (now - timedelta(seconds=50)).isoformat()})

    assert scheduler.should_run(1, now) is False


def test_scheduler_invalid_state_json_falls_back(tmp_path) -> None:
    config = MemoryConfig(optimizer_auto_run=True)
    scheduler = _scheduler(tmp_path, config)
    scheduler.state_path.write_text("{broken", encoding="utf-8")

    assert scheduler.read_state() == {}
    assert scheduler.should_run(1, datetime.now(timezone.utc)) is True


@pytest.mark.asyncio
async def test_scheduler_runs_when_due_and_updates_state(tmp_path) -> None:
    config = MemoryConfig(optimizer_auto_run=True, optimizer_interval_seconds=100)
    scheduler = _scheduler(tmp_path, config)
    optimizer = DummyOptimizer()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    result = await scheduler.run_if_due(optimizer, pending_count=1, now=now)  # type: ignore[arg-type]

    state = scheduler.read_state()
    assert result is not None
    assert optimizer.calls == 1
    assert state["last_run_at"] == now.isoformat()
    assert state["last_result"]["added"] == 1
    assert state["last_error"] == ""


@pytest.mark.asyncio
async def test_scheduler_records_error_without_raising(tmp_path) -> None:
    config = MemoryConfig(optimizer_auto_run=True)
    scheduler = _scheduler(tmp_path, config)
    optimizer = DummyOptimizer(raises=True)

    result = await scheduler.run_if_due(optimizer, pending_count=1)  # type: ignore[arg-type]

    state = scheduler.read_state()
    assert result is None
    assert optimizer.calls == 1
    assert state["last_error"] == "optimizer failed"


@pytest.mark.asyncio
async def test_runtime_consolidate_scheduler_error_does_not_break_flow(tmp_path, monkeypatch) -> None:
    config = MemoryConfig(
        optimizer_auto_run=True,
        optimizer_state_path="optimizer_state.json",
    )
    runtime = MemoryRuntime(tmp_path, config)
    await runtime.initialize()
    await runtime.append_pending_memory(
        MemoryItem(
            type="preference",
            text="Scheduled optimizer candidate.",
            source_ref="turn:scheduler#pending:1",
            metadata={"batch_source_ref": "turn:scheduler", "tag": "preference"},
        )
    )

    async def fail_optimize(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime.optimizer, "optimize", fail_optimize)

    result = await runtime.consolidate()
    state = runtime.scheduler.read_state()

    assert result.added == 1
    assert state["last_error"] == "boom"
