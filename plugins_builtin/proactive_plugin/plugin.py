from __future__ import annotations

from plugins.context import PluginContext
from proactive.sources import ManualCandidateSource, MemoryReminderSource, RecentContextSource


class ProactivePlugin:
    name = "proactive_plugin"

    async def setup(self, context: PluginContext) -> None:
        await context.proactive_runtime.add_source(MemoryReminderSource(context.memory_runtime))
        await context.proactive_runtime.add_source(RecentContextSource(context.memory_runtime))
        await context.proactive_runtime.add_source(ManualCandidateSource())

        async def trigger_proactive_check() -> list[dict[str, object]]:
            decisions = await context.proactive_runtime.check_once()
            return [
                {
                    "candidate_id": decision.candidate.id,
                    "should_push": decision.should_push,
                    "score": decision.score,
                    "reason": decision.reason,
                    "message": decision.message,
                }
                for decision in decisions
            ]

        context.tool_registry.register(
            "trigger_proactive_check",
            "立即运行一次主动推送检查。",
            {"type": "object", "properties": {}},
            trigger_proactive_check,
        )


plugin = ProactivePlugin()
