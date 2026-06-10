from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import json
import logging
import re

from agent.llm import LLMProvider
from memory.models import ConsolidationResult, MemoryItem
from memory.store import MemoryStore


class MemoryConsolidator:
    def __init__(
        self,
        store: MemoryStore,
        main_llm_provider: LLMProvider | None = None,
        fast_llm_provider: LLMProvider | None = None,
    ) -> None:
        self.store = store
        self.main_llm_provider = main_llm_provider
        self.fast_llm_provider = fast_llm_provider or main_llm_provider
        self._logger = logging.getLogger(__name__)

    async def consolidate(self) -> ConsolidationResult:
        pending = self.store.read_pending()
        index = self.store.read_index()
        result = ConsolidationResult(processed=len(pending))
        log_lines = [f"\n## {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"]

        for item in pending:
            item = await self._enrich_pending_memory(item)
            if item.confidence < 0.25:
                result.discarded += 1
                log_lines.append(f"- 丢弃低置信度记忆：{item.text}\n")
                continue
            duplicate = _find_duplicate(index, item)
            if duplicate:
                duplicate.importance = max(duplicate.importance, item.importance)
                duplicate.confidence = max(duplicate.confidence, item.confidence)
                duplicate.updated_at = datetime.now(timezone.utc)
                duplicate.tags = sorted(set(duplicate.tags) | set(item.tags))
                result.merged += 1
                log_lines.append(f"- 合并重复记忆到 {duplicate.id}：{item.text}\n")
                continue
            conflict = _find_possible_conflict(index, item)
            if conflict:
                conflict.status = "lowered_confidence"
                conflict.confidence = min(conflict.confidence, 0.4)
                conflict.updated_at = datetime.now(timezone.utc)
                result.conflicts += 1
                log_lines.append(
                    f"- 可能存在冲突：降低 {conflict.id} 的置信度；新增记忆 {item.text}\n"
                )
            item.status = "active"
            item.updated_at = datetime.now(timezone.utc)
            index.append(item)
            result.added += 1

        self.store.write_index(index)
        self._render_memory_files(index)
        self.store.append_text(self.store.consolidation_log_md, "".join(log_lines))
        self.store.clear_pending()
        result.summary = (
            f"已处理 {result.processed} 条，新增 {result.added} 条，合并 {result.merged} 条，"
            f"冲突 {result.conflicts} 条，丢弃 {result.discarded} 条。"
        )
        return result

    def fast_llm_available(self) -> bool:
        return self.fast_llm_provider is not None and getattr(self.fast_llm_provider, "name", "") != "echo"

    async def _enrich_pending_memory(self, item: MemoryItem) -> MemoryItem:
        if not self.fast_llm_available():
            return item
        prompt = (
            "请对这条候选记忆做轻量分类和评分。只返回 JSON。\n"
            "JSON 字段：type(str), text(str), tags(list[str]), importance(float), confidence(float)。\n"
            "type 可选：fact, preference, goal, project, relationship, habit, instruction, event, summary, reflection。\n\n"
            f"候选记忆：{item.text}\n"
            f"当前类型：{item.type}\n"
            f"当前标签：{item.tags}"
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
            self._logger.warning("fast model 记忆整理草稿失败，继续使用规则整理。", exc_info=True)
        return item

    def _render_memory_files(self, items: list[MemoryItem]) -> None:
        active = [item for item in items if item.status == "active"]
        memory_lines = ["# 长期记忆\n\n"]
        profile_lines = ["# 用户画像\n\n"]
        for item in active:
            line = f"- [{item.type}] {item.text} (id: {item.id}, confidence: {item.confidence:.2f})\n"
            memory_lines.append(line)
            if item.type in {"preference", "goal", "project", "habit", "instruction"}:
                profile_lines.append(line)
        self.store.write_text(self.store.memory_md, "".join(memory_lines))
        self.store.write_text(self.store.profile_md, "".join(profile_lines))


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
    negated = (
        {"not", "never", "no", "don't", "do", "doesn't", "no longer"} & candidate_words
    ) or any(marker in candidate.text for marker in ["不", "不要", "不再", "讨厌"])
    for item in items:
        if item.status != "active" or item.type != candidate.type:
            continue
        if not (set(item.tags) & set(candidate.tags)):
            continue
        item_text = _normalize(item.text)
        item_words = set(item_text.split())
        item_negated = (
            {"not", "never", "no", "don't", "do", "doesn't", "no longer"} & item_words
        ) or any(marker in item.text for marker in ["不", "不要", "不再", "讨厌"])
        shared_words = len(candidate_words & item_words)
        shared_chars = len(set(candidate_text) & set(item_text))
        if bool(negated) != bool(item_negated) and (shared_words >= 2 or shared_chars >= 4):
            return item
    return None
