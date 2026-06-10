from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import json
import logging
import re

from agent.llm import LLMProvider
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
    ) -> None:
        self.store = store
        self.markdown_store = markdown_store or MarkdownMemoryStore(store.memory_dir)
        self.main_llm_provider = main_llm_provider
        self.fast_llm_provider = fast_llm_provider or main_llm_provider
        self._logger = logging.getLogger(__name__)

    async def consolidate(self) -> ConsolidationResult:
        pending = self.store.read_pending()
        index = self.store.read_index()
        now = datetime.now(timezone.utc)
        result = ConsolidationResult(processed=len(pending))
        history_lines = [f"- Consolidated {len(pending)} pending memories."]
        log_lines = [f"\n## {now.isoformat(timespec='seconds')}\n"]

        for item in pending:
            item = await self._enrich_pending_memory(item)
            if item.confidence < 0.25:
                result.discarded += 1
                history_lines.append(
                    f"- Discarded low-confidence candidate: [{item.type}] {item.text}"
                )
                log_lines.append(f"- discarded: [{item.type}] {item.text}\n")
                continue

            duplicate = _find_duplicate(index, item)
            if duplicate:
                duplicate.importance = max(duplicate.importance, item.importance)
                duplicate.confidence = max(duplicate.confidence, item.confidence)
                duplicate.updated_at = now
                duplicate.tags = sorted(set(duplicate.tags) | set(item.tags))
                result.merged += 1
                history_lines.append(
                    f"- Merged duplicate into {duplicate.id}: [{item.type}] {item.text}"
                )
                log_lines.append(
                    f"- merged into {duplicate.id}: [{item.type}] {item.text}\n"
                )
                continue

            conflict = _find_possible_conflict(index, item)
            if conflict:
                conflict.status = "lowered_confidence"
                conflict.confidence = min(conflict.confidence, 0.4)
                conflict.updated_at = now
                result.conflicts += 1
                history_lines.append(
                    f"- Flagged a conflict with {conflict.id}: [{item.type}] {item.text}"
                )
                log_lines.append(
                    f"- conflict with {conflict.id}: [{item.type}] {item.text}\n"
                )

            item.status = "active"
            item.updated_at = now
            index.append(item)
            result.added += 1
            history_lines.append(f"- Added stable memory: [{item.type}] {item.text}")
            log_lines.append(f"- added: [{item.type}] {item.text}\n")

        self.store.write_index(index)
        self._render_memory_files(index)
        self.store.append_text(self.store.consolidation_log_md, "".join(log_lines))
        self.store.clear_pending()
        self.markdown_store.render_pending_memories([])
        self.markdown_store.append_history_lines(
            history_lines,
            now=now,
            title="Memory Consolidation",
        )

        result.summary = (
            f"processed={result.processed}, added={result.added}, merged={result.merged}, "
            f"conflicts={result.conflicts}, discarded={result.discarded}"
        )
        return result

    def fast_llm_available(self) -> bool:
        return self.fast_llm_provider is not None and getattr(
            self.fast_llm_provider, "name", ""
        ) != "echo"

    async def _enrich_pending_memory(self, item: MemoryItem) -> MemoryItem:
        if not self.fast_llm_available():
            return item

        prompt = (
            "Classify and lightly score this candidate memory. "
            "Return JSON only with keys: type, text, tags, importance, confidence.\n\n"
            f"Candidate: {item.text}\n"
            f"Current type: {item.type}\n"
            f"Current tags: {item.tags}"
        )
        try:
            response = await self.fast_llm_provider.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                timeout_seconds=15,
                purpose="memory_consolidation_enrich",
            )
            data: dict[str, Any] = json.loads(response)
            item.type = data.get("type", item.type)
            item.text = str(data.get("text", item.text))
            if isinstance(data.get("tags"), list):
                item.tags = [str(tag) for tag in data["tags"]]
            item.importance = float(data.get("importance", item.importance))
            item.confidence = float(data.get("confidence", item.confidence))
        except Exception:
            self._logger.warning(
                "fast consolidation enrichment failed; keeping the original candidate",
                exc_info=True,
            )
        return item

    def _render_memory_files(self, items: list[MemoryItem]) -> None:
        self.markdown_store.render_active_memories(items)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _find_duplicate(items: list[MemoryItem], candidate: MemoryItem) -> MemoryItem | None:
    normalized = _normalize(candidate.text)
    for item in items:
        if item.status != "active":
            continue
        if _normalize(item.text) == normalized:
            return item
    return None


def _find_possible_conflict(items: list[MemoryItem], candidate: MemoryItem) -> MemoryItem | None:
    candidate_words = set(_normalize(candidate.text).split())
    candidate_text = _normalize(candidate.text)
    negation_markers = {"not", "never", "no", "don't", "doesn't", "no longer", "不", "别"}
    negated = bool(negation_markers & candidate_words) or any(
        marker in candidate.text for marker in ["不", "不要", "不再", "讨厌"]
    )

    for item in items:
        if item.status != "active" or item.type != candidate.type:
            continue
        if not (set(item.tags) & set(candidate.tags)):
            continue

        item_text = _normalize(item.text)
        item_words = set(item_text.split())
        item_negated = bool(negation_markers & item_words) or any(
            marker in item.text for marker in ["不", "不要", "不再", "讨厌"]
        )
        shared_words = len(candidate_words & item_words)
        shared_chars = len(set(candidate_text) & set(item_text))
        if bool(negated) != bool(item_negated) and (shared_words >= 2 or shared_chars >= 4):
            return item
    return None
