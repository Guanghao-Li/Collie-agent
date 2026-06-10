from __future__ import annotations

from typing import Any
import json
import logging
import re

from agent.llm import LLMProvider
from memory.models import MemoryItem


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
    ) -> list[MemoryItem]:
        provider = self.fast_llm_provider
        if provider is None or getattr(provider, "name", "") == "echo":
            return self._extract_with_rules(session_id, user_message)
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
                return self._parse_items(session_id, response)
            except Exception:
                self._logger.warning("记忆抽取使用 %s provider 失败。", label, exc_info=True)
        return []

    def _extract_with_rules(self, session_id: str, user_message: str) -> list[MemoryItem]:
        patterns = [
            r"remember that (?P<text>.+)",
            r"remember: (?P<text>.+)",
            r"please remember (?P<text>.+)",
            r"记住[:：]?\s*(?P<text>.+)",
            r"请记住[:：]?\s*(?P<text>.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, user_message, flags=re.IGNORECASE)
            if match:
                text = match.group("text").strip()
                return [
                    MemoryItem(
                        type=_guess_type(text),
                        text=text,
                        tags=["extracted"],
                        importance=0.7,
                        confidence=0.75,
                        source=f"session:{session_id}",
                        status="pending",
                    )
                ]
        return []

    def _build_prompt(self, user_message: str, assistant_message: str, recent_context: str) -> str:
        return (
            "请从这一轮对话中抽取值得长期保存的用户记忆。只输出 JSON。\n"
            "保留稳定偏好、长期目标、重要项目、明确指令、重要关系和未来需要使用的上下文。"
            "不要抽取临时闲聊内容。\n\n"
            f"近期上下文：\n{recent_context}\n\n"
            f"用户：{user_message}\n助手：{assistant_message}\n\n"
            "JSON 格式：[{\"type\":\"preference\",\"text\":\"...\",\"tags\":[\"...\"],"
            "\"importance\":0.8,\"confidence\":0.9}]"
        )

    def _parse_items(self, session_id: str, response: str) -> list[MemoryItem]:
        try:
            data: Any = json.loads(response)
        except json.JSONDecodeError:
            self._logger.warning("记忆抽取 JSON 解析失败。")
            return []
        if not isinstance(data, list):
            return []
        items: list[MemoryItem] = []
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("text"):
                continue
            items.append(
                MemoryItem(
                    type=entry.get("type", "fact"),
                    text=str(entry["text"]),
                    tags=[str(tag) for tag in entry.get("tags", [])],
                    importance=float(entry.get("importance", 0.5)),
                    confidence=float(entry.get("confidence", 0.5)),
                    source=f"session:{session_id}",
                    status="pending",
                )
            )
        return items


def _guess_type(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ["prefer", "like", "favorite", "喜欢", "偏好"]):
        return "preference"
    if any(word in lowered for word in ["goal", "want to", "planning to", "目标", "计划"]):
        return "goal"
    if any(word in lowered for word in ["project", "repo", "app", "项目", "仓库", "应用"]):
        return "project"
    if any(word in lowered for word in ["always", "never", "do not", "don't", "总是", "不要", "永远"]):
        return "instruction"
    return "fact"
