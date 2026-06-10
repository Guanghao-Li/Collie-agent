from __future__ import annotations

from drift.tasks import (
    MemoryConsolidationTask,
    MemoryDecayTask,
    ProactiveIdeaTask,
    RecentContextSummaryTask,
    ReflectionTask,
)
from plugins.context import PluginContext


class DriftPlugin:
    name = "drift_plugin"

    async def setup(self, context: PluginContext) -> None:
        await context.drift_runtime.add_task(MemoryConsolidationTask())
        await context.drift_runtime.add_task(RecentContextSummaryTask())
        await context.drift_runtime.add_task(ReflectionTask())
        await context.drift_runtime.add_task(ProactiveIdeaTask())
        await context.drift_runtime.add_task(MemoryDecayTask())

        async def trigger_drift_cycle() -> list[dict[str, object]]:
            results = await context.drift_runtime.run_once(force=True)
            return [
                {
                    "task_name": result.task_name,
                    "success": result.success,
                    "summary": result.summary,
                    "created_candidates": result.created_candidates,
                    "updated_memories": result.updated_memories,
                }
                for result in results
            ]

        context.tool_registry.register(
            "trigger_drift_cycle",
            "立即运行一次 Drift cycle。",
            {"type": "object", "properties": {}},
            trigger_drift_cycle,
        )


plugin = DriftPlugin()
