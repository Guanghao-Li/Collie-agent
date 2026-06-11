from __future__ import annotations

from bus.event_bus import AfterTurnEvent
from memory.engine import MemoryMutation, MemoryQuery
from memory.models import MemoryItem
from plugins.context import PluginContext


class MemoryPlugin:
    name = "memory_plugin"

    async def setup(self, context: PluginContext) -> None:
        async def remember(text: str, stable: bool = False) -> str:
            item = MemoryItem(
                type="requested_memory" if stable else "fact",
                text=text,
                tags=["tool"],
                importance=0.75,
                confidence=0.85,
                source="tool:remember",
                status="pending",
            )
            result = await context.memory_runtime.engine.mutate(
                MemoryMutation(kind="remember", item=item, stable=stable)
            )
            return result.item_id

        async def search_memory(query: str, limit: int = 8) -> list[dict[str, object]]:
            results = await context.memory_runtime.search(query, limit)
            return [item.to_dict() for item in results]

        async def summarize_memory() -> str:
            profile = await context.memory_runtime.read_profile()
            memory = await context.memory_runtime.read_core_memory()
            return f"{profile.strip()}\n\n{memory.strip()}".strip()

        async def optimize_memory() -> dict[str, object]:
            result = await context.memory_runtime.optimize_pending()
            return {
                "processed": result.processed,
                "added": result.added,
                "merged": result.merged,
                "skipped": result.skipped,
                "requires_review": result.requires_review,
                "summary": result.summary,
            }

        async def recall_memory(
            query: str,
            intent: str = "context",
            limit: int = 8,
        ) -> dict[str, object]:
            allowed_intents = {"answer", "timeline", "procedure", "context"}
            safe_intent = intent if intent in allowed_intents else "context"
            result = await context.memory_runtime.engine.query(
                MemoryQuery(intent=safe_intent, text=query, limit=limit)  # type: ignore[arg-type]
            )
            return {
                "content": result.content,
                "items": [item.to_dict() for item in result.items],
                "records": [
                    {
                        "id": record.id,
                        "kind": record.kind,
                        "summary": record.summary,
                        "score": record.score,
                    }
                    for record in result.records
                ],
                "metadata": result.metadata,
            }

        async def forget_memory(ids: list[str], reason: str = "") -> dict[str, object]:
            result = await context.memory_runtime.engine.mutate(
                MemoryMutation(kind="forget", ids=tuple(ids), reason=reason)
            )
            return {
                "ok": result.ok,
                "affected_ids": result.affected_ids,
                "missing_ids": result.missing_ids,
                "status": result.status,
            }

        context.tool_registry.register(
            "remember",
            "写入一条待整理或稳定的长期记忆。",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}, "stable": {"type": "boolean"}},
                "required": ["text"],
            },
            remember,
        )
        context.tool_registry.register(
            "search_memory",
            "搜索本地长期记忆。",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
            search_memory,
        )
        context.tool_registry.register(
            "summarize_memory",
            "返回用户画像和记忆摘要。",
            {"type": "object", "properties": {}},
            summarize_memory,
        )
        context.tool_registry.register(
            "optimize_memory",
            "手动运行低频记忆优化器。",
            {"type": "object", "properties": {}},
            optimize_memory,
        )
        context.tool_registry.register(
            "recall_memory",
            "按意图召回本地长期记忆。",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "intent": {
                        "type": "string",
                        "enum": ["answer", "timeline", "procedure", "context"],
                    },
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            recall_memory,
        )
        context.tool_registry.register(
            "forget_memory",
            "按 ID 删除长期记忆。",
            {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["ids"],
            },
            forget_memory,
        )

        async def extract_after_turn(event: AfterTurnEvent) -> None:
            if event.metadata.get("memory_extracted"):
                return
            await context.memory_runtime.extract_from_turn(
                event.session_id,
                event.user_message,
                event.assistant_message,
            )
            event.metadata["memory_extracted"] = True

        context.event_bus.subscribe(AfterTurnEvent, extract_after_turn)


plugin = MemoryPlugin()
