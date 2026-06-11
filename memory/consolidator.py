from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging

from agent.llm import LLMProvider
from bootstrap.config import MemoryConfig
from memory.markdown_store import MarkdownMemoryStore
from memory.models import ConsolidationResult, MemoryItem
from memory.store import MemoryStore


class MemoryConsolidator:
    def __init__(
        self,
        store: MemoryStore,
        main_llm_provider: LLMProvider | None = None,
        fast_llm_provider: LLMProvider | None = None,
        markdown_store: MarkdownMemoryStore | None = None,
        config: MemoryConfig | None = None,
    ) -> None:
        self.store = store
        self.markdown_store = markdown_store or MarkdownMemoryStore(store.memory_dir)
        self.config = config or MemoryConfig()
        self.main_llm_provider = main_llm_provider
        self.fast_llm_provider = fast_llm_provider or main_llm_provider
        self._logger = logging.getLogger(__name__)

    async def consolidate(self) -> ConsolidationResult:
        pending = self.store.read_pending()
        now = datetime.now(timezone.utc)
        result = ConsolidationResult(processed=len(pending))
        if not pending:
            result.summary = "processed=0, history_entries=0, pending_items=0"
            return result

        self.markdown_store.snapshot_pending()
        grouped: dict[str, list[MemoryItem]] = {}
        for item in pending:
            source_ref = _base_source_ref(item)
            grouped.setdefault(source_ref, []).append(item)

        had_failure = False
        recent_summaries: list[str] = []
        for source_ref, items in grouped.items():
            if self.markdown_store.has_processed_source_ref(source_ref):
                result.discarded += len(items)
                self.markdown_store.append_consolidation_log(
                    source_ref=source_ref,
                    history_count=0,
                    pending_count=0,
                    skipped=True,
                    now=now,
                )
                continue

            history_count = 0
            pending_count = 0
            try:
                for item in items:
                    if _is_history_item(item):
                        entry_source_ref = item.source_ref or _item_source_ref(source_ref, item)
                        if self.markdown_store.append_history_entry(
                            item.text,
                            happened_at=item.happened_at,
                            source_ref=entry_source_ref,
                            emotional_weight=item.emotional_weight,
                        ):
                            history_count += 1
                            recent_summaries.append(item.text)
                        continue

                    tag = _pending_tag(item)
                    if self.markdown_store.append_pending_candidate(
                        tag,
                        item.text,
                        source_ref=item.source_ref or _item_source_ref(source_ref, item),
                        confidence=item.confidence,
                        importance=item.importance,
                        metadata=item.metadata,
                    ):
                        pending_count += 1
                        recent_summaries.append(item.text)

                self.markdown_store.update_recent_context_sections(
                    compression=_build_recent_context_summary(recent_summaries),
                )
                self.markdown_store.record_processed_source_ref(
                    source_ref,
                    history_count=history_count,
                    pending_count=pending_count,
                    now=now,
                )
                self.markdown_store.append_consolidation_log(
                    source_ref=source_ref,
                    history_count=history_count,
                    pending_count=pending_count,
                    now=now,
                )
                result.added += history_count + pending_count
            except Exception as exc:
                had_failure = True
                result.conflicts += 1
                self._logger.warning(
                    "memory consolidation failed for source_ref=%s",
                    source_ref,
                    exc_info=True,
                )
                self.markdown_store.append_consolidation_log(
                    source_ref=source_ref,
                    history_count=history_count,
                    pending_count=pending_count,
                    failed=True,
                    error=str(exc),
                    now=now,
                )

        if had_failure:
            self.markdown_store.restore_pending_snapshot()
        else:
            self.store.clear_pending()
            self.markdown_store.clear_pending_snapshot()

        result.summary = (
            f"processed={result.processed}, added={result.added}, "
            f"skipped={result.discarded}, failures={result.conflicts}"
        )
        return result


def _base_source_ref(item: MemoryItem) -> str:
    raw = str(item.metadata.get("batch_source_ref") or item.source_ref or "").strip()
    if raw:
        return raw.split("#", 1)[0] if raw.startswith("session:") else raw
    source = str(item.source or "").strip()
    if source and source not in {"unknown", "tool:remember", "memory_mutation"}:
        return source
    digest = hashlib.sha1(f"{item.id}\0{item.text}".encode("utf-8")).hexdigest()[:16]
    return f"memory-item:{digest}"


def _item_source_ref(source_ref: str, item: MemoryItem) -> str:
    digest = hashlib.sha1(item.text.strip().encode("utf-8")).hexdigest()[:12]
    return f"{source_ref}#item:{item.id}:{digest}"


def _is_history_item(item: MemoryItem) -> bool:
    return (
        item.type == "event"
        or item.metadata.get("extraction_kind") == "history_entry"
        or "history_entry" in item.tags
    )


def _pending_tag(item: MemoryItem) -> str:
    raw = str(item.metadata.get("tag") or "").strip().lower()
    if raw:
        return raw
    if item.type in {
        "identity",
        "preference",
        "key_info",
        "health_long_term",
        "requested_memory",
        "correction",
        "procedure",
    }:
        return item.type
    if item.type == "instruction":
        return "procedure"
    if item.type in {"goal", "project", "habit", "relationship"}:
        return "preference"
    return "requested_memory"


def _build_recent_context_summary(items: list[str]) -> str:
    cleaned = []
    seen = set()
    for item in items[-6:]:
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(f"- {text}")
    if not cleaned:
        return ""
    return "Recent memory consolidation:\n" + "\n".join(cleaned)
