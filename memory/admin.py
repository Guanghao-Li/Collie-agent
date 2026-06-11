from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from bootstrap.config import MemoryConfig
from memory.engine import MemoryQuery
from memory.markdown_store import MarkdownMemoryStore
from memory.memory2_store import SQLiteMemory2Store, memory_item_from_row
from memory.models import MemoryItem
from memory.retriever import MemoryRetriever
from memory.search import MemorySearch
from memory.store import MemoryStore
from memory.vector_store import VectorMemoryRecord, VectorMemoryStore


ALLOWED_UPDATE_FIELDS = {
    "summary",
    "body",
    "text",
    "type",
    "kind",
    "importance",
    "confidence",
    "tags",
    "metadata",
    "status",
}


class MemoryAdminService:
    def __init__(
        self,
        *,
        store: MemoryStore,
        markdown_store: MarkdownMemoryStore,
        memory2_store: SQLiteMemory2Store | None,
        vector_store: VectorMemoryStore | None,
        retriever: MemoryRetriever | None,
        config: MemoryConfig,
    ) -> None:
        self.store = store
        self.markdown_store = markdown_store
        self.memory2_store = memory2_store
        self.vector_store = vector_store
        self.retriever = retriever
        self.config = config
        self.search = MemorySearch()

    async def list_dashboard(
        self,
        limit: int = 50,
        offset: int = 0,
        kind: str | None = None,
        status: str = "active",
        query: str | None = None,
    ) -> dict[str, Any]:
        limit = _clamp_limit(limit, default=50)
        offset = max(int(offset), 0)
        clean_kind = _clean_optional(kind)
        clean_status = _clean_optional(status) or "active"
        clean_query = _clean_optional(query)
        if self.memory2_store is not None and self.memory2_store.db_path.exists():
            rows = await self._list_memory2_rows(
                limit=limit,
                offset=offset,
                kind=clean_kind,
                status=clean_status,
                query=clean_query,
            )
            has_more = len(rows) > limit
            return {
                "items": [_sanitize_memory2_row(row) for row in rows[:limit]],
                "has_more": has_more,
                "limit": limit,
                "offset": offset,
                "backend": "memory2",
            }
        items = self._list_index_items(
            limit=limit,
            offset=offset,
            kind=clean_kind,
            status=clean_status,
            query=clean_query,
        )
        return {
            "items": [_item_to_dashboard(item) for item in items[:limit]],
            "has_more": len(items) > limit,
            "limit": limit,
            "offset": offset,
            "backend": "index",
        }

    async def get_dashboard_detail(self, memory_id: str) -> dict[str, Any] | None:
        clean_id = str(memory_id or "").strip()
        if not clean_id:
            return None
        if self.memory2_store is not None and self.memory2_store.db_path.exists():
            row = await self.memory2_store.get_item(clean_id)
            if row is not None:
                detail = _sanitize_memory2_row(row, detail=True)
                detail["replacements"] = await self.memory2_store.list_replacements(clean_id)
                return detail
        item = self._find_index_item(clean_id)
        if item is None:
            return None
        detail = _item_to_dashboard(item, detail=True)
        detail["replacements"] = []
        detail["embedding_present"] = False
        return detail

    async def update_dashboard_memory(
        self,
        memory_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        clean_id = str(memory_id or "").strip()
        if not clean_id:
            raise ValueError("memory_id is required")
        disallowed = sorted(set(fields) - ALLOWED_UPDATE_FIELDS)
        if disallowed:
            raise ValueError(f"unsupported update fields: {', '.join(disallowed)}")

        items = self.store.read_index()
        item = next((existing for existing in items if existing.id == clean_id), None)
        if item is None:
            item = await self._load_memory2_item(clean_id)
            if item is None:
                raise KeyError(f"memory not found: {clean_id}")
            items.append(item)

        text_changed = False
        if "summary" in fields or "text" in fields:
            new_text = str(fields.get("summary", fields.get("text")) or "").strip()
            if new_text and new_text != item.text:
                item.text = new_text
                text_changed = True
        if "body" in fields:
            item.metadata["body"] = str(fields["body"])
            text_changed = True
        if "type" in fields or "kind" in fields:
            item.type = str(fields.get("type", fields.get("kind")) or item.type)  # type: ignore[assignment]
        if "importance" in fields:
            item.importance = _coerce_float(fields["importance"], default=item.importance)
        if "confidence" in fields:
            item.confidence = _coerce_float(fields["confidence"], default=item.confidence)
        if "tags" in fields:
            item.tags = _coerce_str_list(fields["tags"])
        if "metadata" in fields:
            metadata = fields["metadata"]
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            item.metadata.update(dict(metadata))
        if "status" in fields:
            item.status = str(fields["status"] or item.status)  # type: ignore[assignment]
        item.updated_at = datetime.now(timezone.utc)

        self.store.write_index(items)
        self.markdown_store.render_active_memories(items)
        await self._sync_item(item, text_changed=text_changed)
        return await self.get_dashboard_detail(clean_id) or _item_to_dashboard(item, detail=True)

    async def delete_dashboard_memory(
        self,
        memory_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        result = await self.batch_delete([memory_id], reason=reason)
        return {
            "ok": bool(result["affected_ids"]),
            "affected_ids": result["affected_ids"],
            "missing_ids": result["missing_ids"],
            "reason": reason,
        }

    async def batch_delete(self, ids: list[str], reason: str = "") -> dict[str, Any]:
        clean_ids = _dedupe_ids(ids)
        items = self.store.read_index()
        targets = set(clean_ids)
        affected: list[str] = []
        now = datetime.now(timezone.utc)
        for item in items:
            if item.id not in targets or item.status == "deleted":
                continue
            item.status = "deleted"
            item.updated_at = now
            self.store.append_deleted(item, reason)
            affected.append(item.id)
        missing = [memory_id for memory_id in clean_ids if memory_id not in affected]
        self.store.write_index(items)
        self.markdown_store.render_active_memories(items)
        if self.memory2_store is not None:
            await self.memory2_store.batch_delete(affected, soft=True)
        if self.vector_store is not None:
            for memory_id in affected:
                await self.vector_store.delete(memory_id)
        return {
            "ok": bool(affected),
            "affected_ids": affected,
            "missing_ids": missing,
            "reason": reason,
        }

    async def find_similar(
        self,
        memory_id: str | None = None,
        text: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        limit = _clamp_limit(limit, default=10)
        query_source = "text"
        query_text = _clean_optional(text)
        if memory_id:
            detail = await self.get_dashboard_detail(memory_id)
            if detail is None:
                return {
                    "backend": "none",
                    "query_source": "memory_id",
                    "items": [],
                    "disabled_reason": "memory not found",
                }
            query_text = str(detail.get("summary") or detail.get("body") or "")
            query_source = "memory_id"
        if not query_text:
            return {
                "backend": "none",
                "query_source": query_source,
                "items": [],
                "disabled_reason": "memory_id or text is required",
            }
        if self.retriever is not None:
            result = await self.retriever.retrieve(
                MemoryQuery(kind="search", text=query_text, limit=limit)
            )
            return {
                "backend": result.metadata.get("search_backend", "retriever"),
                "query_source": query_source,
                "items": [
                    _record_to_dashboard(record)
                    for record in result.records
                    if record.id != memory_id
                ][:limit],
                "disabled_reason": (
                    "vector disabled; keyword fallback used"
                    if not (self.vector_store and self.vector_store.is_enabled())
                    else ""
                ),
            }
        items = self.search.search(self.store.read_index(), query_text, limit)
        return {
            "backend": "keyword",
            "query_source": query_source,
            "items": [_item_to_dashboard(item) for item in items if item.id != memory_id],
            "disabled_reason": "retriever unavailable",
        }

    async def list_event_range(
        self,
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        limit = _clamp_limit(limit, default=100)
        start_dt = _parse_datetime(start)
        end_dt = _parse_datetime(end)
        if self.memory2_store is not None and self.memory2_store.db_path.exists():
            rows = await self.memory2_store.find_active_by_type("event", limit=limit * 3)
            events = [
                _sanitize_memory2_row(row)
                for row in rows
                if _within_range(_parse_datetime(row.get("happened_at")), start_dt, end_dt)
            ][:limit]
            return {"backend": "memory2", "events": events, "limit": limit}
        events = [
            event
            for event in self._history_events()
            if _within_range(_parse_datetime(event.get("happened_at")), start_dt, end_dt)
        ][:limit]
        return {"backend": "history", "events": events, "limit": limit}

    async def get_stats(self) -> dict[str, Any]:
        index = self.store.read_index()
        pending = self.markdown_store.parse_pending_candidates()
        vector_description: dict[str, Any] = {}
        if self.vector_store is not None:
            vector_description = await self.vector_store.describe()
        memory2_description: dict[str, Any] = {}
        if self.memory2_store is not None:
            memory2_description = await self.memory2_store.describe()
        state = _read_json_file(self._optimizer_state_path())
        return {
            "active": sum(1 for item in index if item.status == "active"),
            "pending_candidates": sum(
                1
                for item in pending
                if str(item.get("section")) != "requires_review"
                and not bool(item.get("requires_review"))
            ),
            "requires_review": sum(
                1
                for item in pending
                if str(item.get("section")) == "requires_review"
                or bool(item.get("requires_review"))
            ),
            "deleted": sum(1 for item in index if item.status == "deleted"),
            "superseded": sum(1 for item in index if item.status == "superseded"),
            "memory2": memory2_description.get("items", {}),
            "vector_enabled": bool(vector_description.get("enabled", False)),
            "embedding_enabled": bool(
                vector_description.get("embedder", {}).get("enabled", False)
            ),
            "last_optimizer_run": state.get("last_run_at", ""),
            "last_optimizer_error": state.get("last_error", ""),
        }

    async def _list_memory2_rows(
        self,
        *,
        limit: int,
        offset: int,
        kind: str | None,
        status: str,
        query: str | None,
    ) -> list[dict[str, Any]]:
        assert self.memory2_store is not None
        if query and status == "active":
            rows = await self.memory2_store.keyword_search(
                query,
                kinds=[kind] if kind else None,
                limit=limit + 1 + offset,
            )
            return rows[offset : offset + limit + 1]
        rows = await self.memory2_store.list_items(
            status=status,
            kind=kind,
            limit=limit + 1,
            offset=offset,
        )
        if query:
            rows = [row for row in rows if _matches_query(row, query)]
        return rows

    def _list_index_items(
        self,
        *,
        limit: int,
        offset: int,
        kind: str | None,
        status: str,
        query: str | None,
    ) -> list[MemoryItem]:
        items = [
            item
            for item in self.store.read_index()
            if item.status == status and (kind is None or item.type == kind)
        ]
        if query:
            terms = set(_terms(query))
            items = [
                item
                for item in items
                if terms & (_terms(item.text) | {tag.lower() for tag in item.tags})
            ]
        items.sort(key=lambda item: (item.updated_at, item.created_at), reverse=True)
        return items[offset : offset + limit + 1]

    async def _load_memory2_item(self, memory_id: str) -> MemoryItem | None:
        if self.memory2_store is None:
            return None
        row = await self.memory2_store.get_item(memory_id)
        return memory_item_from_row(row) if row is not None else None

    def _find_index_item(self, memory_id: str) -> MemoryItem | None:
        return next((item for item in self.store.read_index() if item.id == memory_id), None)

    async def _sync_item(self, item: MemoryItem, *, text_changed: bool) -> None:
        if item.status != "active":
            if self.memory2_store is not None:
                await self.memory2_store.update_item(
                    item.id,
                    {
                        "status": item.status,
                        "summary": item.text,
                        "type": item.type,
                        "importance": item.importance,
                        "confidence": item.confidence,
                        "extra_json": {**item.metadata, "tags": list(item.tags)},
                    },
                )
            if self.vector_store is not None:
                await self.vector_store.delete(item.id)
            return
        if self.vector_store is not None and self.vector_store.is_enabled():
            await self.vector_store.upsert(_item_to_vector_record(item))
        elif self.memory2_store is not None:
            await self.memory2_store.upsert_item(item)

    def _history_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for index, line in enumerate(self.markdown_store.read_text(self.markdown_store.history_md).splitlines()):
            stripped = line.strip()
            if not stripped.startswith("["):
                continue
            happened_at = ""
            summary = stripped
            match = re.match(r"\[([^\]]+)\]\s*(.*)", stripped)
            if match:
                happened_at = match.group(1).strip()
                summary = match.group(2).strip()
            events.append(
                {
                    "id": f"history:{index}",
                    "type": "event",
                    "kind": "event",
                    "summary": summary,
                    "body": summary,
                    "text": summary,
                    "status": "active",
                    "happened_at": happened_at,
                    "source_ref": "HISTORY.md",
                    "metadata": {},
                    "tags": [],
                }
            )
        return events

    def _optimizer_state_path(self):
        raw_path = getattr(self.config, "optimizer_state_path", ".collie/memory/optimizer_state.json")
        path = self.markdown_store.memory_dir.parent / raw_path
        return path


def _item_to_vector_record(item: MemoryItem) -> VectorMemoryRecord:
    return VectorMemoryRecord(
        item_id=item.id,
        memory_type=item.type,
        summary=item.text,
        source_ref=item.source_ref or item.source,
        happened_at=item.happened_at or item.last_used_at or item.created_at,
        status=item.status,
        created_at=item.created_at,
        updated_at=item.updated_at,
        metadata={
            **item.metadata,
            "tags": list(item.tags),
            "importance": item.importance,
            "confidence": item.confidence,
            "source": item.source,
            "emotional_weight": item.emotional_weight,
        },
    )


def _sanitize_memory2_row(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    tags = extra.get("tags", [])
    payload = {
        "id": str(row.get("id") or ""),
        "type": str(row.get("type") or ""),
        "kind": str(row.get("type") or ""),
        "summary": str(row.get("summary") or ""),
        "body": str(row.get("body") or row.get("summary") or ""),
        "text": str(row.get("summary") or ""),
        "status": str(row.get("status") or ""),
        "importance": float(row.get("importance") or 0.0),
        "confidence": float(row.get("confidence") or 0.0),
        "reinforcement": int(row.get("reinforcement") or 0),
        "source_ref": str(row.get("source_ref") or ""),
        "happened_at": row.get("happened_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "tags": [str(tag) for tag in tags] if isinstance(tags, list) else [],
        "metadata": _trim_metadata(extra),
    }
    if detail:
        payload["embedding_present"] = bool(row.get("embedding"))
    return payload


def _item_to_dashboard(item: MemoryItem, *, detail: bool = False) -> dict[str, Any]:
    payload = {
        "id": item.id,
        "type": item.type,
        "kind": item.type,
        "summary": item.text,
        "body": str(item.metadata.get("body") or item.text),
        "text": item.text,
        "status": item.status,
        "importance": item.importance,
        "confidence": item.confidence,
        "reinforcement": int(item.metadata.get("reinforcement") or 0),
        "source_ref": item.source_ref,
        "happened_at": item.happened_at.isoformat() if item.happened_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "tags": list(item.tags),
        "metadata": _trim_metadata(item.metadata),
    }
    if detail:
        payload["embedding_present"] = False
    return payload


def _record_to_dashboard(record) -> dict[str, Any]:
    return {
        "id": record.id,
        "type": record.kind,
        "kind": record.kind,
        "summary": record.summary,
        "score": record.score,
        "status": record.signals.get("status", "active"),
        "source_ref": record.signals.get("source_ref", ""),
        "signals": {
            key: value
            for key, value in record.signals.items()
            if key not in {"embedding", "embedding_json"}
        },
    }


def _trim_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in metadata.items()
        if key not in {"embedding", "embedding_json"} and len(str(value)) < 1000
    }


def _matches_query(row: dict[str, Any], query: str) -> bool:
    needle = query.lower()
    haystack = " ".join(
        str(row.get(key) or "").lower()
        for key in ("summary", "body", "type", "source_ref")
    )
    return needle in haystack or bool(set(_terms(query)) & set(_terms(haystack)))


def _terms(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", text.lower())


def _clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clamp_limit(value: int, *, default: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, 500))


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None:
        return []
    return [str(value)]


def _dedupe_ids(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in ids:
        memory_id = str(raw).strip()
        if memory_id and memory_id not in seen:
            seen.add(memory_id)
            deduped.append(memory_id)
    return deduped


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _within_range(
    value: datetime | None,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    if value is None:
        return True
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True


def _read_json_file(path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
