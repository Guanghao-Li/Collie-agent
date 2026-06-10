from __future__ import annotations

from bus.event_bus import AfterTurnEvent
from memory.models import MemoryItem
from plugins.context import PluginContext


class MemoryPlugin:
    name = "memory_plugin"

    async def setup(self, context: PluginContext) -> None:
        async def remember(text: str, stable: bool = False) -> str:
            item = MemoryItem(
                type="fact",
                text=text,
                tags=["tool"],
                importance=0.75,
                confidence=0.85,
                source="tool:remember",
                status="active" if stable else "pending",
            )
            if stable:
                await context.memory_runtime.add_memory(item)
            else:
                await context.memory_runtime.append_pending_memory(item)
            return item.id

        async def search_memory(query: str, limit: int = 8) -> list[dict[str, object]]:
            results = await context.memory_runtime.search(query, limit)
            return [item.to_dict() for item in results]

        async def summarize_memory() -> str:
            profile = await context.memory_runtime.read_profile()
            memory = await context.memory_runtime.read_core_memory()
            return f"{profile.strip()}\n\n{memory.strip()}".strip()

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
