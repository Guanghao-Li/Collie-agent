from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from drift.models import DriftContext, DriftResult
from proactive.sources import ManualCandidateSource


def _has_real_fast_model(ctx: DriftContext) -> bool:
    return (
        ctx.fast_llm_provider is not ctx.main_llm_provider
        and getattr(ctx.fast_llm_provider, "name", "") != "echo"
    )


@dataclass(slots=True)
class MemoryConsolidationTask:
    name: str = "memory_consolidation"
    interval_seconds: int = 1800
    requires_main_model: bool = False

    async def should_run(self, ctx: DriftContext) -> bool:
        stats = await ctx.memory_runtime.stats()
        return stats["pending"] > 0

    async def run(self, ctx: DriftContext) -> DriftResult:
        result = await ctx.memory_runtime.consolidate()
        return DriftResult(
            task_name=self.name,
            success=True,
            summary=result.summary,
            updated_memories=result.added + result.merged,
        )


@dataclass(slots=True)
class RecentContextSummaryTask:
    name: str = "recent_context_summary"
    interval_seconds: int = 3600
    requires_main_model: bool = False

    async def should_run(self, ctx: DriftContext) -> bool:
        return bool(ctx.session_manager.list_sessions())

    async def run(self, ctx: DriftContext) -> DriftResult:
        snippets: list[str] = []
        for session_id in ctx.session_manager.list_sessions()[-5:]:
            for message in ctx.session_manager.get_messages(session_id, limit=4):
                snippets.append(f"{session_id} {message.role}: {message.content}")

        raw_context = "\n".join(snippets[-20:])
        summary = raw_context or "暂无近期会话上下文。"
        if raw_context and _has_real_fast_model(ctx):
            try:
                summary = await ctx.fast_llm_provider.complete(
                    [
                        {
                            "role": "user",
                            "content": "请把下面近期会话整理成简短上下文摘要：\n" + raw_context,
                        }
                    ],
                    temperature=0.0,
                    timeout_seconds=15,
                    purpose="drift_recent_context_summary",
                )
            except Exception:
                pass
        await ctx.memory_runtime.update_recent_context(summary)
        return DriftResult(self.name, True, "已更新近期上下文摘要。")


@dataclass(slots=True)
class ReflectionTask:
    name: str = "reflection"
    interval_seconds: int = 7200
    requires_main_model: bool = False

    async def should_run(self, ctx: DriftContext) -> bool:
        return True

    async def run(self, ctx: DriftContext) -> DriftResult:
        stats = await ctx.memory_runtime.stats()
        summary = (
            f"反思：截至 {ctx.current_time.isoformat(timespec='seconds')}，"
            f"当前有 {stats['active']} 条活跃记忆和 {stats['pending']} 条待整理记忆。"
        )
        if _has_real_fast_model(ctx):
            try:
                summary = await ctx.fast_llm_provider.complete(
                    [{"role": "user", "content": f"请基于这些统计写一条简短后台反思：{summary}"}],
                    temperature=0.0,
                    timeout_seconds=15,
                    purpose="drift_reflection_draft",
                )
            except Exception:
                pass
        await ctx.memory_runtime.append_reflection(summary)
        return DriftResult(self.name, True, "已写入一条轻量反思。")


@dataclass(slots=True)
class ProactiveIdeaTask:
    name: str = "proactive_idea"
    interval_seconds: int = 3600
    requires_main_model: bool = False

    async def should_run(self, ctx: DriftContext) -> bool:
        stats = await ctx.memory_runtime.stats()
        return stats["active"] > 0

    async def run(self, ctx: DriftContext) -> DriftResult:
        source = ctx.proactive_runtime.source_registry.sources.get("manual")
        if not isinstance(source, ManualCandidateSource):
            source = ManualCandidateSource()
            await ctx.proactive_runtime.add_source(source)

        idea = "检查近期目标或项目，找出一个有用的下一步行动。"
        if _has_real_fast_model(ctx):
            try:
                idea = await ctx.fast_llm_provider.complete(
                    [{"role": "user", "content": "请生成一条简短的主动跟进候选。"}],
                    temperature=0.0,
                    timeout_seconds=15,
                    purpose="drift_proactive_idea_draft",
                )
            except Exception:
                pass
        source.add_candidate("Drift 跟进建议", idea)
        return DriftResult(self.name, True, "已创建 1 条主动推送候选。", created_candidates=1)


@dataclass(slots=True)
class MemoryDecayTask:
    name: str = "memory_decay"
    interval_seconds: int = 86400
    requires_main_model: bool = False

    async def should_run(self, ctx: DriftContext) -> bool:
        return True

    async def run(self, ctx: DriftContext) -> DriftResult:
        items = ctx.memory_runtime.store.read_index()
        now = datetime.now(timezone.utc)
        updated = 0
        for item in items:
            if item.status != "active" or item.last_used_at is not None:
                continue
            age_days = max((now - item.updated_at).total_seconds() / 86400, 0)
            if age_days > 180 and item.importance < 0.5 and item.confidence < 0.6:
                item.confidence = max(item.confidence - 0.05, 0.1)
                item.updated_at = now
                updated += 1
        if updated:
            ctx.memory_runtime.store.write_index(items)
        return DriftResult(self.name, True, f"已衰减 {updated} 条长期未使用记忆。", updated_memories=updated)
