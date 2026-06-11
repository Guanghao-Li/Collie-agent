from __future__ import annotations

from bus.event_bus import AfterTurnEvent
from memory.engine import MemoryMutation, MemoryQuery, MemoryQueryFilters
from memory.models import MemoryItem
from plugins.context import PluginContext


class MemoryPlugin:
    name = "memory_plugin"

    async def setup(self, context: PluginContext) -> None:
        def admin_service():
            service = getattr(context.memory_runtime.engine, "admin_service", None)
            if service is None:
                raise RuntimeError("memory admin service is unavailable")
            return service

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

        async def memorize(
            summary: str,
            memory_kind: str = "preference",
            importance: float = 0.7,
            confidence: float = 0.8,
            source_ref: str | None = None,
        ) -> dict[str, object]:
            result = await context.memory_runtime.engine.mutate(
                MemoryMutation(
                    kind="remember",
                    summary=summary,
                    memory_kind=memory_kind,
                    source_ref=source_ref or "tool:memorize",
                    stable=True,
                    metadata={
                        "importance": float(importance),
                        "confidence": float(confidence),
                    },
                )
            )
            return {
                "ok": result.ok,
                "accepted": result.accepted,
                "item_id": result.item_id,
                "actual_kind": result.actual_kind,
                "status": result.status,
                "pending_written": result.metadata.get("pending_written", False),
            }

        async def search_memory(query: str, limit: int = 8) -> list[dict[str, object]]:
            results = await context.memory_runtime.search(query, limit)
            return [item.to_dict() for item in results]

        async def summarize_memory() -> str:
            profile = await context.memory_runtime.read_profile()
            memory = await context.memory_runtime.read_core_memory()
            return f"{profile.strip()}\n\n{memory.strip()}".strip()

        async def optimize_memory(
            dry_run: bool = False,
            force: bool = False,
        ) -> dict[str, object]:
            result = await context.memory_runtime.optimize_pending(dry_run=dry_run)
            return {
                "ok": result.ok,
                "processed": result.processed,
                "added": result.added,
                "merged": result.merged,
                "superseded": result.superseded,
                "skipped": result.skipped,
                "requires_review": result.requires_review,
                "archived": result.archived,
                "summary": result.summary,
                "errors": result.errors,
                "force": bool(force),
            }

        async def recall_memory(
            query: str,
            intent: str = "answer",
            memory_kind: str | None = None,
            limit: int = 8,
        ) -> dict[str, object]:
            allowed_intents = {"answer", "context", "timeline", "procedure", "interest"}
            safe_intent = intent if intent in allowed_intents else "answer"
            result = await context.memory_runtime.engine.query(
                MemoryQuery(
                    intent=safe_intent,  # type: ignore[arg-type]
                    text=query,
                    limit=limit,
                    filters=MemoryQueryFilters(
                        kinds=(memory_kind,) if memory_kind else (),
                    ),
                )
            )
            return {
                "content": result.text_block or result.content,
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

        async def list_memory(
            kind: str | None = None,
            status: str = "active",
            query: str | None = None,
            limit: int = 20,
            offset: int = 0,
        ) -> dict[str, object]:
            return await admin_service().list_dashboard(
                limit=limit,
                offset=offset,
                kind=kind,
                status=status,
                query=query,
            )

        async def get_memory(id: str) -> dict[str, object] | None:
            return await admin_service().get_dashboard_detail(id)

        async def update_memory(id: str, fields: dict[str, object]) -> dict[str, object]:
            return await admin_service().update_dashboard_memory(id, fields)

        async def delete_memory(id: str, reason: str = "") -> dict[str, object]:
            return await admin_service().delete_dashboard_memory(id, reason=reason)

        async def find_similar_memory(
            id: str | None = None,
            text: str | None = None,
            limit: int = 10,
        ) -> dict[str, object]:
            if not id and not text:
                raise ValueError("id or text is required")
            return await admin_service().find_similar(memory_id=id, text=text, limit=limit)

        async def list_memory_events(
            start: str | None = None,
            end: str | None = None,
            limit: int = 50,
        ) -> dict[str, object]:
            return await admin_service().list_event_range(start=start, end=end, limit=limit)

        context.tool_registry.register(
            "remember",
            "Write a long-term memory candidate.",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}, "stable": {"type": "boolean"}},
                "required": ["text"],
            },
            remember,
        )
        context.tool_registry.register(
            "memorize",
            "Write a pending memory candidate without directly activating it.",
            {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "memory_kind": {"type": "string"},
                    "importance": {"type": "number"},
                    "confidence": {"type": "number"},
                    "source_ref": {"type": "string"},
                },
                "required": ["summary"],
            },
            memorize,
        )
        context.tool_registry.register(
            "search_memory",
            "Search local long-term memory.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["query"],
            },
            search_memory,
        )
        context.tool_registry.register(
            "summarize_memory",
            "Return profile and stable memory summary.",
            {"type": "object", "properties": {}},
            summarize_memory,
        )
        context.tool_registry.register(
            "optimize_memory",
            "Run the low-frequency memory optimizer.",
            {
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean"},
                    "force": {"type": "boolean"},
                },
            },
            optimize_memory,
        )
        context.tool_registry.register(
            "recall_memory",
            "Recall memory by intent.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "memory_kind": {"type": "string"},
                    "intent": {
                        "type": "string",
                        "enum": ["answer", "context", "timeline", "procedure", "interest"],
                    },
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            recall_memory,
        )
        context.tool_registry.register(
            "forget_memory",
            "Forget memory ids.",
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
        context.tool_registry.register(
            "list_memory",
            "List memory dashboard items.",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "status": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
            },
            list_memory,
        )
        context.tool_registry.register(
            "get_memory",
            "Get one memory dashboard detail.",
            {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            get_memory,
        )
        context.tool_registry.register(
            "update_memory",
            "Update one memory item through the admin service.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "fields": {"type": "object"},
                },
                "required": ["id", "fields"],
            },
            update_memory,
        )
        context.tool_registry.register(
            "delete_memory",
            "Soft delete one memory item through the admin service.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id"],
            },
            delete_memory,
        )
        context.tool_registry.register(
            "find_similar_memory",
            "Find memories similar to an id or text.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            find_similar_memory,
        )
        context.tool_registry.register(
            "list_memory_events",
            "List memory timeline events.",
            {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
            list_memory_events,
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
