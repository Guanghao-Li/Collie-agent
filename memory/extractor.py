from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
import hashlib
import json
import logging
import re

from agent.llm import LLMProvider
from memory.markdown_store import ALLOWED_PENDING_TAGS
from memory.models import MemoryItem


@dataclass(slots=True)
class HistoryEntry:
    summary: str
    emotional_weight: int = 0
    happened_at: str | None = None
    source_ref: str = ""


@dataclass(slots=True)
class PendingMemoryCandidate:
    tag: str
    content: str
    confidence: float = 0.5
    importance: float = 0.5
    source_ref: str = ""


@dataclass(slots=True)
class ExtractedMemoryBatch:
    history_entries: list[HistoryEntry] = field(default_factory=list)
    pending_items: list[PendingMemoryCandidate] = field(default_factory=list)
    source_ref: str = ""
    raw: dict[str, object] = field(default_factory=dict)

    def to_memory_items(self, session_id: str) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        base_source_ref = self.source_ref or f"session:{session_id}"
        for index, entry in enumerate(self.history_entries):
            summary = entry.summary.strip()
            if not summary:
                continue
            source_ref = entry.source_ref or _entry_source_ref(base_source_ref, "history", index, summary)
            items.append(
                MemoryItem(
                    type="event",
                    text=summary,
                    tags=["history_entry", "extracted"],
                    importance=0.5,
                    confidence=0.8,
                    source=f"session:{session_id}",
                    source_ref=source_ref,
                    happened_at=_parse_optional_datetime(entry.happened_at),
                    emotional_weight=_coerce_emotional_weight(entry.emotional_weight),
                    status="pending",
                    metadata={
                        "batch_source_ref": base_source_ref,
                        "extraction_kind": "history_entry",
                    },
                )
            )
        for index, candidate in enumerate(self.pending_items):
            content = candidate.content.strip()
            tag = candidate.tag.strip().lower()
            if tag not in ALLOWED_PENDING_TAGS or not content:
                continue
            source_ref = (
                candidate.source_ref
                or _entry_source_ref(base_source_ref, "pending", index, content)
            )
            items.append(
                MemoryItem(
                    type=_memory_type_from_tag(tag),
                    text=content,
                    tags=[tag, "extracted"],
                    importance=float(candidate.importance),
                    confidence=float(candidate.confidence),
                    source=f"session:{session_id}",
                    source_ref=source_ref,
                    status="pending",
                    metadata={
                        "tag": tag,
                        "batch_source_ref": base_source_ref,
                        "extraction_kind": "pending_item",
                        "correction": tag == "correction",
                        "requires_review": tag == "correction",
                    },
                )
            )
        return items


class MemoryExtractor:
    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        *,
        main_llm_provider: LLMProvider | None = None,
        fast_llm_provider: LLMProvider | None = None,
    ) -> None:
        self.main_llm_provider = main_llm_provider or llm_provider
        self.fast_llm_provider = fast_llm_provider or self.main_llm_provider
        self._logger = logging.getLogger(__name__)

    async def extract(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        recent_context: str = "",
        source_ref: str = "",
    ) -> list[MemoryItem]:
        batch = await self.extract_batch(
            session_id=session_id,
            user_message=user_message,
            assistant_message=assistant_message,
            recent_context=recent_context,
            source_ref=source_ref,
        )
        return batch.to_memory_items(session_id)

    async def extract_batch(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        recent_context: str = "",
        source_ref: str = "",
    ) -> ExtractedMemoryBatch:
        source_ref = source_ref or _fallback_source_ref(session_id, user_message, assistant_message)
        provider = self.fast_llm_provider
        if provider is None or getattr(provider, "name", "") == "echo":
            return self._extract_with_rules(
                session_id=session_id,
                user_message=user_message,
                source_ref=source_ref,
            )
        prompt = self._build_prompt(user_message, assistant_message, recent_context)
        for candidate_provider, label in (
            (self.fast_llm_provider, "fast"),
            (self.main_llm_provider, "main"),
        ):
            if candidate_provider is None:
                continue
            try:
                response = await candidate_provider.complete(
                    [{"role": "user", "content": prompt}],
                    temperature=0.0,
                    timeout_seconds=15,
                    purpose=f"memory_extraction_{label}",
                )
                return self._parse_batch(response, source_ref=source_ref)
            except Exception:
                self._logger.warning("记忆抽取使用 %s provider 失败。", label, exc_info=True)
        return ExtractedMemoryBatch(source_ref=source_ref)

    def _extract_with_rules(
        self,
        *,
        session_id: str,
        user_message: str,
        source_ref: str,
    ) -> ExtractedMemoryBatch:
        normalized = user_message.strip()
        if self._looks_like_temporary_task(normalized):
            return ExtractedMemoryBatch(source_ref=source_ref)

        if _looks_like_correction_or_procedure(normalized):
            tag = "correction" if _looks_like_correction(normalized) else "procedure"
            return ExtractedMemoryBatch(
                pending_items=[
                    PendingMemoryCandidate(
                        tag=tag,
                        content=normalized,
                        confidence=0.75,
                        importance=0.7,
                        source_ref=_entry_source_ref(source_ref, "pending", 0, normalized),
                    )
                ],
                source_ref=source_ref,
            )

        patterns = [
            r"remember that (?P<text>.+)",
            r"remember: (?P<text>.+)",
            r"please remember (?P<text>.+)",
            r"记住[，,:：]?\s*(?P<text>.+)",
            r"请记住[，,:：]?\s*(?P<text>.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, user_message, flags=re.IGNORECASE)
            if not match:
                continue
            text = match.group("text").strip()
            if not text or self._looks_like_temporary_task(text):
                return ExtractedMemoryBatch(source_ref=source_ref)
            tag = "requested_memory"
            if _guess_type(text) == "preference":
                tag = "preference"
            return ExtractedMemoryBatch(
                pending_items=[
                    PendingMemoryCandidate(
                        tag=tag,
                        content=text,
                        confidence=0.8,
                        importance=0.75,
                        source_ref=_entry_source_ref(source_ref, "pending", 0, text),
                    )
                ],
                source_ref=source_ref,
            )
        return ExtractedMemoryBatch(source_ref=source_ref)

    def _build_prompt(self, user_message: str, assistant_message: str, recent_context: str) -> str:
        return (
            "你是记忆提取代理。请从这一轮对话中严格抽取两类记忆，只输出合法 JSON，"
            "不要输出 markdown 代码块。\n\n"
            "必须遵守：只抽取用户明确表达的信息；不要把 assistant 的建议、猜测、"
            "推理或知识问答写成用户事实；不要保存临时任务，例如“今天帮我查一下”；"
            "如果没有值得长期保存的内容，两个数组都返回空。\n\n"
            "history_entries 表示时间线事件，字段：summary, emotional_weight, happened_at。\n"
            "pending_items 表示长期事实候选，字段：tag, content, confidence, importance。\n"
            "允许 tag: identity, preference, key_info, health_long_term, requested_memory, "
            "correction, procedure。\n"
            "procedure 只保存以后遇到类似事情应怎么做的长期流程；correction 用于用户明确纠错；"
            "requested_memory 用于用户明确说“记住...”的内容。\n\n"
            f"近期上下文：\n{recent_context}\n\n"
            f"用户：{user_message}\n助手：{assistant_message}\n\n"
            "JSON 格式：\n"
            "{\n"
            '  "history_entries": [\n'
            '    {"summary": "...", "emotional_weight": 0, "happened_at": null}\n'
            "  ],\n"
            '  "pending_items": [\n'
            '    {"tag": "preference", "content": "...", "confidence": 0.8, "importance": 0.7}\n'
            "  ]\n"
            "}"
        )

    def _parse_batch(self, response: str, *, source_ref: str) -> ExtractedMemoryBatch:
        text = _strip_json_text(response)
        try:
            data: Any = json.loads(text)
        except json.JSONDecodeError:
            self._logger.warning("记忆抽取 JSON 解析失败。")
            return ExtractedMemoryBatch(source_ref=source_ref, raw={"parse_error": response[:300]})

        if isinstance(data, list):
            return ExtractedMemoryBatch(
                pending_items=self._parse_legacy_items(data, source_ref=source_ref),
                source_ref=source_ref,
                raw={"legacy_items": data},
            )
        if not isinstance(data, dict):
            return ExtractedMemoryBatch(source_ref=source_ref, raw={"unexpected": data})

        history_entries: list[HistoryEntry] = []
        for index, entry in enumerate(_as_list(data.get("history_entries"))):
            if isinstance(entry, str):
                summary = entry.strip()
                emotional_weight = 0
                happened_at = None
            elif isinstance(entry, dict):
                summary = str(entry.get("summary") or "").strip()
                emotional_weight = _coerce_emotional_weight(entry.get("emotional_weight"))
                happened_at = (
                    str(entry.get("happened_at")).strip()
                    if entry.get("happened_at") is not None
                    else None
                )
            else:
                continue
            if not summary:
                continue
            history_entries.append(
                HistoryEntry(
                    summary=summary,
                    emotional_weight=emotional_weight,
                    happened_at=happened_at,
                    source_ref=_entry_source_ref(source_ref, "history", index, summary),
                )
            )

        pending_items: list[PendingMemoryCandidate] = []
        for index, entry in enumerate(_as_list(data.get("pending_items"))):
            if not isinstance(entry, dict):
                continue
            tag = str(entry.get("tag") or "").strip().lower()
            content = str(entry.get("content") or "").strip()
            if tag not in ALLOWED_PENDING_TAGS or not content:
                continue
            pending_items.append(
                PendingMemoryCandidate(
                    tag=tag,
                    content=content,
                    confidence=_coerce_float(entry.get("confidence"), default=0.5),
                    importance=_coerce_float(entry.get("importance"), default=0.5),
                    source_ref=_entry_source_ref(source_ref, "pending", index, content),
                )
            )

        return ExtractedMemoryBatch(
            history_entries=history_entries,
            pending_items=pending_items,
            source_ref=source_ref,
            raw=data,
        )

    def _parse_legacy_items(
        self,
        entries: list[object],
        *,
        source_ref: str,
    ) -> list[PendingMemoryCandidate]:
        candidates: list[PendingMemoryCandidate] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not entry.get("text"):
                continue
            content = str(entry["text"]).strip()
            tag = _tag_from_memory_type(str(entry.get("type", "fact")))
            candidates.append(
                PendingMemoryCandidate(
                    tag=tag,
                    content=content,
                    confidence=_coerce_float(entry.get("confidence"), default=0.5),
                    importance=_coerce_float(entry.get("importance"), default=0.5),
                    source_ref=_entry_source_ref(source_ref, "pending", index, content),
                )
            )
        return candidates

    @staticmethod
    def _looks_like_temporary_task(text: str) -> bool:
        lowered = text.lower()
        temporary_markers = ["今天", "今晚", "明天", "这次", "当前", "帮我查", "查一下"]
        return any(marker in lowered for marker in temporary_markers)


def _guess_type(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ["prefer", "like", "favorite", "喜欢", "偏好", "希望"]):
        return "preference"
    if any(word in lowered for word in ["goal", "want to", "planning to", "目标", "计划"]):
        return "goal"
    if any(word in lowered for word in ["project", "repo", "app", "项目", "仓库", "应用"]):
        return "project"
    if any(word in lowered for word in ["always", "never", "do not", "don't", "总是", "不要", "永远"]):
        return "instruction"
    return "fact"


def _memory_type_from_tag(tag: str) -> str:
    if tag == "preference":
        return "preference"
    if tag == "procedure":
        return "procedure"
    if tag == "correction":
        return "correction"
    if tag == "identity":
        return "identity"
    if tag == "requested_memory":
        return "requested_memory"
    if tag == "key_info":
        return "key_info"
    if tag == "health_long_term":
        return "health_long_term"
    return "fact"


def _tag_from_memory_type(memory_type: str) -> str:
    if memory_type in ALLOWED_PENDING_TAGS:
        return memory_type
    if memory_type in {"instruction", "procedure"}:
        return "procedure"
    if memory_type in {"preference", "goal", "habit"}:
        return "preference"
    return "requested_memory"


def _fallback_source_ref(session_id: str, user_message: str, assistant_message: str) -> str:
    digest = hashlib.sha1(
        f"{session_id}\0{user_message}\0{assistant_message}".encode("utf-8")
    ).hexdigest()[:16]
    return f"session:{session_id}:turn:{digest}"


def _entry_source_ref(base_source_ref: str, kind: str, index: int, text: str) -> str:
    digest = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:12] if text.strip() else "empty"
    return f"{base_source_ref}#{kind}:{index}:{digest}"


def _strip_json_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return stripped


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _coerce_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_emotional_weight(value: object) -> int:
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return 0


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _looks_like_correction(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ["不对", "错了", "纠正", "更正", "不要再", "forget", "wrong"])


def _looks_like_correction_or_procedure(text: str) -> bool:
    lowered = text.lower()
    return (
        _looks_like_correction(text)
        and any(marker in lowered for marker in ["以后", "下次", "再遇到", "from now on", "next time"])
    )
